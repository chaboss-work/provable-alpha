//! Provable Alpha: a commit-reveal marketplace where AI agents sell trading signals
//! they cannot fake after the fact.
//!
//! Life of a signal:
//! 1. `commit_signal`: the seller agent publishes only `sha256(payload + ":" + salt)`,
//!    a price per buyer, and posts a bond. Nobody can see the signal yet, and the seller
//!    can no longer change it.
//! 2. `buy_signal`: buyer agents pay the price into escrow. Each buyer has registered an
//!    X25519 public key with `set_encryption_key`.
//! 3. `deliver`: the seller writes the payload and salt, sealed to the buyer's key, into
//!    the contract. Escrow for that buyer is released to the seller. The buyer decrypts
//!    and checks the hash against the commitment at once.
//! 4. `reveal_signal`: after the signal horizon the seller must publish payload + salt.
//!    The contract checks the hash and returns the bond. Skipping this costs the bond,
//!    which is shared among the buyers (`expire_signal` + `claim_refund`).
//! 5. `settle_signal`: a verifier records whether the signal was right. Hits, misses and
//!    missed reveals form the seller's on-chain track record.

use near_sdk::store::{IterableMap, LookupMap, Vector};
use near_sdk::{env, near, require, AccountId, BorshStorageKey, NearToken, PanicOnDefault, Promise};

const MAX_MARKET_LEN: usize = 32;
const MAX_PAYLOAD_LEN: usize = 2048;
const MAX_SALT_LEN: usize = 128;
const MAX_CIPHERTEXT_LEN: usize = 4096;
const MAX_EVIDENCE_LEN: usize = 512;
const MAX_ENC_KEY_LEN: usize = 128;
const MAX_LIFETIME_MS: u64 = 7 * 24 * 60 * 60 * 1000;
const EVENT_STANDARD: &str = "provable-alpha";
const EVENT_VERSION: &str = "1.0.0";

#[derive(BorshStorageKey)]
#[near]
enum StorageKey {
    Signals,
    Purchases,
    Buyers,
    Agents,
    EncKeys,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, PartialEq)]
pub enum SignalStatus {
    /// Committed, can be bought until `reveal_after_ms`.
    Open,
    /// Payload published and verified against the commitment.
    Revealed,
    /// Not revealed before the deadline; the bond went to the buyers.
    Expired,
    /// Revealed and judged by a verifier.
    Settled,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Outcome {
    pub verifier: AccountId,
    pub hit: bool,
    /// Realised move in basis points, signed in the signal's direction.
    pub realized_bps: i32,
    /// Short evidence string, e.g. prices and their sources, or a hash of a report.
    pub evidence: String,
    pub settled_at_ms: u64,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Signal {
    pub id: u64,
    pub seller: AccountId,
    /// Free-form market label, e.g. "NEAR/USDC".
    pub market: String,
    /// Lower-case hex sha256 of `payload + ":" + salt`.
    pub commitment: String,
    pub price: NearToken,
    pub bond: NearToken,
    pub committed_at_ms: u64,
    /// Buying closes and public reveal opens at this time (the signal horizon).
    pub reveal_after_ms: u64,
    /// The seller must reveal before this time or lose the bond.
    pub reveal_deadline_ms: u64,
    pub buyers: u32,
    pub delivered: u32,
    pub payload: Option<String>,
    pub salt: Option<String>,
    pub revealed_at_ms: Option<u64>,
    pub status: SignalStatus,
    pub outcome: Option<Outcome>,
    /// Bond share each buyer can claim after expiry.
    pub bond_share: NearToken,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug)]
pub struct Purchase {
    pub signal_id: u64,
    pub buyer: AccountId,
    pub paid: NearToken,
    pub bought_at_ms: u64,
    /// Payload + salt sealed to the buyer's X25519 key (base64), written by the seller.
    pub ciphertext: Option<String>,
    pub delivered_at_ms: Option<u64>,
    pub refunded: bool,
}

#[near(serializers = [borsh, json])]
#[derive(Clone, Debug, Default)]
pub struct AgentStats {
    pub signals: u32,
    pub revealed: u32,
    pub missed: u32,
    pub hits: u32,
    pub misses: u32,
    pub sold: u32,
    pub undelivered: u32,
    pub earned: NearToken,
}

#[near(serializers = [json])]
pub struct AgentView {
    pub account_id: AccountId,
    pub stats: AgentStats,
    /// Hits / settled signals, in basis points (0..=10000). None until the first settlement.
    pub hit_rate_bps: Option<u32>,
    /// Revealed / (revealed + missed), in basis points.
    pub reveal_rate_bps: Option<u32>,
}

#[near(serializers = [json])]
pub struct Config {
    pub owner: AccountId,
    pub verifiers: Vec<AccountId>,
    pub min_bond: NearToken,
    pub storage_fee: NearToken,
    pub delivery_window_ms: u64,
    pub max_buyers: u32,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Contract {
    owner: AccountId,
    verifiers: Vec<AccountId>,
    min_bond: NearToken,
    /// Non-refundable fee on commit and on purchase that pays for the storage used.
    storage_fee: NearToken,
    delivery_window_ms: u64,
    max_buyers: u32,
    signals: Vector<Signal>,
    purchases: LookupMap<String, Purchase>,
    buyers: LookupMap<u64, Vec<AccountId>>,
    agents: IterableMap<AccountId, AgentStats>,
    enc_keys: LookupMap<AccountId, String>,
}

fn purchase_key(signal_id: u64, buyer: &AccountId) -> String {
    format!("{}:{}", signal_id, buyer)
}

fn to_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 0x0f) as usize] as char);
    }
    out
}

fn is_lower_hex(s: &str) -> bool {
    s.bytes().all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

fn ratio_bps(part: u32, total: u32) -> Option<u32> {
    if total == 0 {
        None
    } else {
        Some(((part as u64) * 10_000 / (total as u64)) as u32)
    }
}

fn emit(event: &str, data: String) {
    env::log_str(&format!(
        "EVENT_JSON:{{\"standard\":\"{}\",\"version\":\"{}\",\"event\":\"{}\",\"data\":[{}]}}",
        EVENT_STANDARD, EVENT_VERSION, event, data
    ));
}

/// The digest the seller commits to. Public so agents and tests use the same rule.
pub fn commitment_of(payload: &str, salt: &str) -> String {
    to_hex(&env::sha256(format!("{}:{}", payload, salt).as_bytes()))
}

#[near]
impl Contract {
    #[init]
    pub fn new(verifiers: Vec<AccountId>) -> Self {
        Self {
            owner: env::predecessor_account_id(),
            verifiers,
            min_bond: NearToken::from_millinear(50),
            storage_fee: NearToken::from_millinear(10),
            delivery_window_ms: 10 * 60 * 1000,
            max_buyers: 50,
            signals: Vector::new(StorageKey::Signals),
            purchases: LookupMap::new(StorageKey::Purchases),
            buyers: LookupMap::new(StorageKey::Buyers),
            agents: IterableMap::new(StorageKey::Agents),
            enc_keys: LookupMap::new(StorageKey::EncKeys),
        }
    }

    // ---------- seller ----------

    /// Attach `bond + storage_fee`. Returns the new signal id.
    #[payable]
    pub fn commit_signal(
        &mut self,
        market: String,
        commitment: String,
        price: NearToken,
        reveal_after_ms: u64,
        reveal_deadline_ms: u64,
    ) -> u64 {
        let attached = env::attached_deposit();
        let bond = attached
            .checked_sub(self.storage_fee)
            .unwrap_or_else(|| env::panic_str("attach bond + storage fee"));
        require!(bond >= self.min_bond, "bond too small");
        require!(!market.is_empty() && market.len() <= MAX_MARKET_LEN, "bad market label");
        require!(commitment.len() == 64 && is_lower_hex(&commitment), "commitment must be lower-case sha256 hex");
        require!(price.as_yoctonear() > 0, "price must be positive");
        let now = env::block_timestamp_ms();
        require!(reveal_after_ms > now, "reveal_after_ms must be in the future");
        require!(reveal_deadline_ms > reveal_after_ms, "reveal deadline must be after reveal_after_ms");
        require!(reveal_deadline_ms - now <= MAX_LIFETIME_MS, "signal lifetime too long");

        let seller = env::predecessor_account_id();
        let id = self.signals.len() as u64;
        self.signals.push(Signal {
            id,
            seller: seller.clone(),
            market: market.clone(),
            commitment: commitment.clone(),
            price,
            bond,
            committed_at_ms: now,
            reveal_after_ms,
            reveal_deadline_ms,
            buyers: 0,
            delivered: 0,
            payload: None,
            salt: None,
            revealed_at_ms: None,
            status: SignalStatus::Open,
            outcome: None,
            bond_share: NearToken::from_yoctonear(0),
        });
        self.stats_mut(&seller).signals += 1;
        emit(
            "signal_committed",
            format!(
                "{{\"id\":{},\"seller\":\"{}\",\"market\":\"{}\",\"commitment\":\"{}\",\"price\":\"{}\",\"reveal_after_ms\":{}}}",
                id, seller, market, commitment, price.as_yoctonear(), reveal_after_ms
            ),
        );
        id
    }

    /// Seal payload + salt to the buyer's key off-chain and store the ciphertext here.
    /// Releases that buyer's payment to the seller.
    pub fn deliver(&mut self, signal_id: u64, buyer: AccountId, ciphertext: String) -> Promise {
        let signal = self.signal(signal_id).clone();
        require!(env::predecessor_account_id() == signal.seller, "only the seller can deliver");
        require!(!ciphertext.is_empty() && ciphertext.len() <= MAX_CIPHERTEXT_LEN, "bad ciphertext size");
        let key = purchase_key(signal_id, &buyer);
        let mut purchase = self.purchases.get(&key).cloned().unwrap_or_else(|| env::panic_str("no such purchase"));
        require!(purchase.delivered_at_ms.is_none(), "already delivered");
        require!(!purchase.refunded, "purchase was refunded");
        let now = env::block_timestamp_ms();
        require!(now <= purchase.bought_at_ms + self.delivery_window_ms, "delivery window closed");

        purchase.ciphertext = Some(ciphertext);
        purchase.delivered_at_ms = Some(now);
        let paid = purchase.paid;
        self.purchases.insert(key, purchase);

        let seller = signal.seller.clone();
        self.signal_mut(signal_id).delivered += 1;
        let stats = self.stats_mut(&seller);
        stats.sold += 1;
        stats.earned = stats.earned.saturating_add(paid);
        emit("signal_delivered", format!("{{\"id\":{},\"buyer\":\"{}\"}}", signal_id, buyer));
        Promise::new(seller).transfer(paid)
    }

    /// Publish the signal after its horizon. The hash must match the commitment.
    pub fn reveal_signal(&mut self, signal_id: u64, payload: String, salt: String) -> Promise {
        let signal = self.signal(signal_id).clone();
        require!(env::predecessor_account_id() == signal.seller, "only the seller can reveal");
        require!(signal.status == SignalStatus::Open, "signal is not open");
        let now = env::block_timestamp_ms();
        require!(now >= signal.reveal_after_ms, "too early: buyers still hold the signal exclusively");
        require!(now <= signal.reveal_deadline_ms, "reveal deadline passed");
        require!(payload.len() <= MAX_PAYLOAD_LEN && salt.len() <= MAX_SALT_LEN, "payload or salt too long");
        require!(commitment_of(&payload, &salt) == signal.commitment, "payload does not match commitment");

        let seller = signal.seller.clone();
        let bond = signal.bond;
        {
            let s = self.signal_mut(signal_id);
            s.payload = Some(payload);
            s.salt = Some(salt);
            s.revealed_at_ms = Some(now);
            s.status = SignalStatus::Revealed;
        }
        self.stats_mut(&seller).revealed += 1;
        emit("signal_revealed", format!("{{\"id\":{},\"seller\":\"{}\"}}", signal_id, seller));
        Promise::new(seller).transfer(bond)
    }

    // ---------- buyer ----------

    /// X25519 public key (base64) that sellers seal deliveries to.
    pub fn set_encryption_key(&mut self, key: String) {
        require!(!key.is_empty() && key.len() <= MAX_ENC_KEY_LEN, "bad key size");
        self.enc_keys.insert(env::predecessor_account_id(), key);
    }

    /// Attach exactly `price + storage_fee`.
    #[payable]
    pub fn buy_signal(&mut self, signal_id: u64) {
        let signal = self.signal(signal_id).clone();
        let buyer = env::predecessor_account_id();
        require!(signal.status == SignalStatus::Open, "signal is not open");
        require!(env::block_timestamp_ms() < signal.reveal_after_ms, "signal horizon reached, buying closed");
        require!(buyer != signal.seller, "seller cannot buy own signal");
        require!(signal.buyers < self.max_buyers, "signal sold out");
        require!(self.enc_keys.contains_key(&buyer), "register an encryption key first");
        let expected = signal.price.saturating_add(self.storage_fee);
        require!(env::attached_deposit() == expected, "attach exactly price + storage fee");
        let key = purchase_key(signal_id, &buyer);
        require!(!self.purchases.contains_key(&key), "already bought");

        self.purchases.insert(
            key,
            Purchase {
                signal_id,
                buyer: buyer.clone(),
                paid: signal.price,
                bought_at_ms: env::block_timestamp_ms(),
                ciphertext: None,
                delivered_at_ms: None,
                refunded: false,
            },
        );
        let mut list = self.buyers.get(&signal_id).cloned().unwrap_or_default();
        list.push(buyer.clone());
        self.buyers.insert(signal_id, list);
        self.signal_mut(signal_id).buyers += 1;
        emit("signal_bought", format!("{{\"id\":{},\"buyer\":\"{}\"}}", signal_id, buyer));
    }

    /// Get money back when the seller did not deliver in time, and/or claim a bond
    /// share when the seller never revealed.
    pub fn claim_refund(&mut self, signal_id: u64) -> Promise {
        let buyer = env::predecessor_account_id();
        let key = purchase_key(signal_id, &buyer);
        let mut purchase = self.purchases.get(&key).cloned().unwrap_or_else(|| env::panic_str("no such purchase"));
        require!(!purchase.refunded, "already refunded");
        let signal = self.signal(signal_id).clone();
        let now = env::block_timestamp_ms();
        let undelivered_late = purchase.delivered_at_ms.is_none() && now > purchase.bought_at_ms + self.delivery_window_ms;
        let expired = signal.status == SignalStatus::Expired;
        require!(undelivered_late || expired, "nothing to refund");

        let mut amount = NearToken::from_yoctonear(0);
        if purchase.delivered_at_ms.is_none() {
            amount = amount.saturating_add(purchase.paid);
            self.stats_mut(&signal.seller).undelivered += 1;
        }
        if expired {
            amount = amount.saturating_add(signal.bond_share);
        }
        purchase.refunded = true;
        self.purchases.insert(key, purchase);
        emit(
            "refund_claimed",
            format!("{{\"id\":{},\"buyer\":\"{}\",\"amount\":\"{}\"}}", signal_id, buyer, amount.as_yoctonear()),
        );
        Promise::new(buyer).transfer(amount)
    }

    // ---------- anyone ----------

    /// Mark a signal that was never revealed. Its bond is split among the buyers.
    pub fn expire_signal(&mut self, signal_id: u64) {
        let signal = self.signal(signal_id).clone();
        require!(signal.status == SignalStatus::Open, "signal is not open");
        require!(env::block_timestamp_ms() > signal.reveal_deadline_ms, "reveal deadline not reached");
        let share = if signal.buyers > 0 {
            NearToken::from_yoctonear(signal.bond.as_yoctonear() / signal.buyers as u128)
        } else {
            NearToken::from_yoctonear(0)
        };
        {
            let s = self.signal_mut(signal_id);
            s.status = SignalStatus::Expired;
            s.bond_share = share;
        }
        self.stats_mut(&signal.seller).missed += 1;
        emit("signal_expired", format!("{{\"id\":{},\"seller\":\"{}\"}}", signal_id, signal.seller));
    }

    // ---------- verifier ----------

    pub fn settle_signal(&mut self, signal_id: u64, hit: bool, realized_bps: i32, evidence: String) {
        let verifier = env::predecessor_account_id();
        require!(self.verifiers.contains(&verifier), "only a verifier can settle");
        require!(evidence.len() <= MAX_EVIDENCE_LEN, "evidence too long");
        let signal = self.signal(signal_id).clone();
        require!(signal.status == SignalStatus::Revealed, "signal must be revealed first");
        require!(verifier != signal.seller, "seller cannot settle own signal");
        {
            let s = self.signal_mut(signal_id);
            s.status = SignalStatus::Settled;
            s.outcome = Some(Outcome {
                verifier: verifier.clone(),
                hit,
                realized_bps,
                evidence,
                settled_at_ms: env::block_timestamp_ms(),
            });
        }
        let stats = self.stats_mut(&signal.seller);
        if hit {
            stats.hits += 1;
        } else {
            stats.misses += 1;
        }
        emit(
            "signal_settled",
            format!("{{\"id\":{},\"hit\":{},\"realized_bps\":{}}}", signal_id, hit, realized_bps),
        );
    }

    // ---------- owner ----------

    pub fn add_verifier(&mut self, account_id: AccountId) {
        self.assert_owner();
        if !self.verifiers.contains(&account_id) {
            self.verifiers.push(account_id);
        }
    }

    pub fn remove_verifier(&mut self, account_id: AccountId) {
        self.assert_owner();
        self.verifiers.retain(|v| v != &account_id);
    }

    // ---------- views ----------

    pub fn get_config(&self) -> Config {
        Config {
            owner: self.owner.clone(),
            verifiers: self.verifiers.clone(),
            min_bond: self.min_bond,
            storage_fee: self.storage_fee,
            delivery_window_ms: self.delivery_window_ms,
            max_buyers: self.max_buyers,
        }
    }

    pub fn get_signal_count(&self) -> u64 {
        self.signals.len() as u64
    }

    pub fn get_signal(&self, signal_id: u64) -> Option<Signal> {
        self.signals.get(signal_id as u32).cloned()
    }

    /// Newest first. `from_index` counts from the newest signal.
    pub fn get_signals(&self, from_index: Option<u64>, limit: Option<u64>) -> Vec<Signal> {
        let total = self.signals.len() as u64;
        let from = from_index.unwrap_or(0);
        let limit = limit.unwrap_or(20).min(100);
        (from..total.min(from + limit))
            .filter_map(|i| self.signals.get((total - 1 - i) as u32).cloned())
            .collect()
    }

    pub fn get_purchase(&self, signal_id: u64, buyer: AccountId) -> Option<Purchase> {
        self.purchases.get(&purchase_key(signal_id, &buyer)).cloned()
    }

    pub fn get_purchases(&self, signal_id: u64) -> Vec<Purchase> {
        self.buyers
            .get(&signal_id)
            .map(|list| {
                list.iter()
                    .filter_map(|b| self.purchases.get(&purchase_key(signal_id, b)).cloned())
                    .collect()
            })
            .unwrap_or_default()
    }

    pub fn get_encryption_key(&self, account_id: AccountId) -> Option<String> {
        self.enc_keys.get(&account_id).cloned()
    }

    pub fn get_agent(&self, account_id: AccountId) -> Option<AgentView> {
        self.agents.get(&account_id).map(|s| Self::view(account_id.clone(), s.clone()))
    }

    pub fn get_agents(&self, from_index: Option<u32>, limit: Option<u32>) -> Vec<AgentView> {
        self.agents
            .iter()
            .skip(from_index.unwrap_or(0) as usize)
            .take(limit.unwrap_or(50).min(100) as usize)
            .map(|(a, s)| Self::view(a.clone(), s.clone()))
            .collect()
    }

    // ---------- internal ----------

    fn view(account_id: AccountId, stats: AgentStats) -> AgentView {
        let hit_rate_bps = ratio_bps(stats.hits, stats.hits + stats.misses);
        let reveal_rate_bps = ratio_bps(stats.revealed, stats.revealed + stats.missed);
        AgentView { account_id, stats, hit_rate_bps, reveal_rate_bps }
    }

    fn assert_owner(&self) {
        require!(env::predecessor_account_id() == self.owner, "only the owner");
    }

    fn signal(&self, signal_id: u64) -> &Signal {
        self.signals
            .get(signal_id as u32)
            .unwrap_or_else(|| env::panic_str("no such signal"))
    }

    fn signal_mut(&mut self, signal_id: u64) -> &mut Signal {
        self.signals
            .get_mut(signal_id as u32)
            .unwrap_or_else(|| env::panic_str("no such signal"))
    }

    fn stats_mut(&mut self, account: &AccountId) -> &mut AgentStats {
        if !self.agents.contains_key(account) {
            self.agents.insert(account.clone(), AgentStats::default());
        }
        self.agents.get_mut(account).unwrap()
    }
}

#[cfg(test)]
mod tests;
