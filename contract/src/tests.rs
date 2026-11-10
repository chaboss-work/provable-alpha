use super::*;
use near_sdk::test_utils::VMContextBuilder;
use near_sdk::testing_env;

const MIN: u64 = 60 * 1000;
const T0: u64 = 1_700_000_000_000;

fn acc(s: &str) -> AccountId {
    s.parse().unwrap()
}

fn ctx(who: &str, now_ms: u64, deposit: NearToken) {
    let mut b = VMContextBuilder::new();
    b.predecessor_account_id(acc(who))
        .signer_account_id(acc(who))
        .block_timestamp(now_ms * 1_000_000)
        .attached_deposit(deposit);
    testing_env!(b.build());
}

fn near_milli(n: u128) -> NearToken {
    NearToken::from_millinear(n)
}

fn zero() -> NearToken {
    NearToken::from_yoctonear(0)
}

const PAYLOAD: &str = r#"{"market":"NEAR/USDC","direction":"long","spread_bps":42}"#;
const SALT: &str = "s3cr3t-salt";

/// Contract with one committed signal (price 0.1 NEAR, bond 0.05, horizon +30 min, deadline +60 min).
fn setup() -> Contract {
    ctx("owner.near", T0, zero());
    let mut c = Contract::new(vec![acc("verifier.near")]);
    ctx("seller.near", T0, near_milli(60));
    let id = c.commit_signal(
        "NEAR/USDC".into(),
        commitment_of(PAYLOAD, SALT),
        near_milli(100),
        T0 + 30 * MIN,
        T0 + 60 * MIN,
    );
    assert_eq!(id, 0);
    c
}

fn buy(c: &mut Contract, who: &str, now: u64) {
    ctx(who, now, zero());
    c.set_encryption_key("x25519-pubkey-base64".into());
    ctx(who, now, near_milli(110));
    c.buy_signal(0);
}

#[test]
fn commit_records_signal_and_stats() {
    let c = setup();
    let s = c.get_signal(0).unwrap();
    assert_eq!(s.status, SignalStatus::Open);
    assert_eq!(s.bond, near_milli(50));
    assert_eq!(s.price, near_milli(100));
    assert!(s.payload.is_none());
    let a = c.get_agent(acc("seller.near")).unwrap();
    assert_eq!(a.stats.signals, 1);
    assert_eq!(a.hit_rate_bps, None);
}

#[test]
#[should_panic(expected = "bond too small")]
fn commit_rejects_small_bond() {
    ctx("owner.near", T0, zero());
    let mut c = Contract::new(vec![]);
    ctx("seller.near", T0, near_milli(30));
    c.commit_signal("NEAR/USDC".into(), commitment_of(PAYLOAD, SALT), near_milli(1), T0 + MIN, T0 + 2 * MIN);
}

#[test]
#[should_panic(expected = "commitment must be lower-case sha256 hex")]
fn commit_rejects_bad_commitment() {
    ctx("owner.near", T0, zero());
    let mut c = Contract::new(vec![]);
    ctx("seller.near", T0, near_milli(60));
    c.commit_signal("NEAR/USDC".into(), "not-a-hash".into(), near_milli(1), T0 + MIN, T0 + 2 * MIN);
}

#[test]
fn full_happy_path() {
    let mut c = setup();
    buy(&mut c, "buyer.near", T0 + MIN);
    assert_eq!(c.get_signal(0).unwrap().buyers, 1);

    ctx("seller.near", T0 + 2 * MIN, zero());
    let _ = c.deliver(0, acc("buyer.near"), "sealed-box-base64".into());
    let p = c.get_purchase(0, acc("buyer.near")).unwrap();
    assert!(p.delivered_at_ms.is_some());
    assert_eq!(c.get_agent(acc("seller.near")).unwrap().stats.earned, near_milli(100));

    ctx("seller.near", T0 + 31 * MIN, zero());
    let _ = c.reveal_signal(0, PAYLOAD.into(), SALT.into());
    let s = c.get_signal(0).unwrap();
    assert_eq!(s.status, SignalStatus::Revealed);
    assert_eq!(s.payload.as_deref(), Some(PAYLOAD));

    ctx("verifier.near", T0 + 40 * MIN, zero());
    c.settle_signal(0, true, 37, "intents 3.412 vs cex 3.398".into());
    let a = c.get_agent(acc("seller.near")).unwrap();
    assert_eq!(a.stats.hits, 1);
    assert_eq!(a.hit_rate_bps, Some(10_000));
    assert_eq!(a.reveal_rate_bps, Some(10_000));
    assert_eq!(c.get_signal(0).unwrap().status, SignalStatus::Settled);
}

#[test]
#[should_panic(expected = "payload does not match commitment")]
fn reveal_with_wrong_salt_fails() {
    let mut c = setup();
    ctx("seller.near", T0 + 31 * MIN, zero());
    let _ = c.reveal_signal(0, PAYLOAD.into(), "other-salt".into());
}

#[test]
#[should_panic(expected = "payload does not match commitment")]
fn reveal_with_edited_payload_fails() {
    let mut c = setup();
    ctx("seller.near", T0 + 31 * MIN, zero());
    let _ = c.reveal_signal(0, PAYLOAD.replace("long", "short"), SALT.into());
}

#[test]
#[should_panic(expected = "too early")]
fn reveal_before_horizon_fails() {
    let mut c = setup();
    ctx("seller.near", T0 + 5 * MIN, zero());
    let _ = c.reveal_signal(0, PAYLOAD.into(), SALT.into());
}

#[test]
#[should_panic(expected = "buying closed")]
fn cannot_buy_after_horizon() {
    let mut c = setup();
    buy(&mut c, "buyer.near", T0 + 30 * MIN);
}

#[test]
#[should_panic(expected = "seller cannot buy own signal")]
fn seller_cannot_buy() {
    let mut c = setup();
    buy(&mut c, "seller.near", T0 + MIN);
}

#[test]
#[should_panic(expected = "register an encryption key first")]
fn buyer_needs_key() {
    let mut c = setup();
    ctx("buyer.near", T0 + MIN, near_milli(110));
    c.buy_signal(0);
}

#[test]
#[should_panic(expected = "attach exactly price + storage fee")]
fn buy_requires_exact_deposit() {
    let mut c = setup();
    ctx("buyer.near", T0 + MIN, zero());
    c.set_encryption_key("k".into());
    ctx("buyer.near", T0 + MIN, near_milli(100));
    c.buy_signal(0);
}

#[test]
#[should_panic(expected = "already bought")]
fn cannot_buy_twice() {
    let mut c = setup();
    buy(&mut c, "buyer.near", T0 + MIN);
    ctx("buyer.near", T0 + 2 * MIN, near_milli(110));
    c.buy_signal(0);
}

#[test]
fn refund_when_not_delivered_in_time() {
    let mut c = setup();
    buy(&mut c, "buyer.near", T0 + MIN);
    ctx("buyer.near", T0 + 12 * MIN, zero());
    let _ = c.claim_refund(0);
    assert!(c.get_purchase(0, acc("buyer.near")).unwrap().refunded);
    assert_eq!(c.get_agent(acc("seller.near")).unwrap().stats.undelivered, 1);
}

#[test]
#[should_panic(expected = "nothing to refund")]
fn no_refund_inside_delivery_window() {
    let mut c = setup();
    buy(&mut c, "buyer.near", T0 + MIN);
    ctx("buyer.near", T0 + 5 * MIN, zero());
    let _ = c.claim_refund(0);
}

#[test]
#[should_panic(expected = "delivery window closed")]
fn late_delivery_is_rejected() {
    let mut c = setup();
    buy(&mut c, "buyer.near", T0 + MIN);
    ctx("seller.near", T0 + 12 * MIN, zero());
    let _ = c.deliver(0, acc("buyer.near"), "late".into());
}

#[test]
#[should_panic(expected = "only the seller can deliver")]
fn only_seller_delivers() {
    let mut c = setup();
    buy(&mut c, "buyer.near", T0 + MIN);
    ctx("mallory.near", T0 + 2 * MIN, zero());
    let _ = c.deliver(0, acc("buyer.near"), "x".into());
}

#[test]
fn missed_reveal_slashes_bond_to_buyers() {
    let mut c = setup();
    buy(&mut c, "a.near", T0 + MIN);
    buy(&mut c, "b.near", T0 + MIN);
    ctx("seller.near", T0 + 2 * MIN, zero());
    let _ = c.deliver(0, acc("a.near"), "sealed".into());

    ctx("anyone.near", T0 + 61 * MIN, zero());
    c.expire_signal(0);
    let s = c.get_signal(0).unwrap();
    assert_eq!(s.status, SignalStatus::Expired);
    assert_eq!(s.bond_share, near_milli(25));
    assert_eq!(c.get_agent(acc("seller.near")).unwrap().stats.missed, 1);
    assert_eq!(c.get_agent(acc("seller.near")).unwrap().reveal_rate_bps, Some(0));

    // a got the signal (paid seller) and now gets a bond share; b gets price + share.
    ctx("a.near", T0 + 62 * MIN, zero());
    let _ = c.claim_refund(0);
    ctx("b.near", T0 + 62 * MIN, zero());
    let _ = c.claim_refund(0);
    assert!(c.get_purchase(0, acc("a.near")).unwrap().refunded);
    assert!(c.get_purchase(0, acc("b.near")).unwrap().refunded);
}

#[test]
#[should_panic(expected = "reveal deadline not reached")]
fn cannot_expire_early() {
    let mut c = setup();
    ctx("anyone.near", T0 + 59 * MIN, zero());
    c.expire_signal(0);
}

#[test]
#[should_panic(expected = "only a verifier can settle")]
fn only_verifier_settles() {
    let mut c = setup();
    ctx("seller.near", T0 + 31 * MIN, zero());
    let _ = c.reveal_signal(0, PAYLOAD.into(), SALT.into());
    ctx("mallory.near", T0 + 32 * MIN, zero());
    c.settle_signal(0, true, 1, "".into());
}

#[test]
#[should_panic(expected = "signal must be revealed first")]
fn cannot_settle_unrevealed() {
    let mut c = setup();
    ctx("verifier.near", T0 + 31 * MIN, zero());
    c.settle_signal(0, true, 1, "".into());
}

#[test]
fn signals_listed_newest_first() {
    let mut c = setup();
    ctx("seller.near", T0, near_milli(60));
    c.commit_signal("BTC/USDT".into(), commitment_of("p2", "s2"), near_milli(1), T0 + MIN, T0 + 2 * MIN);
    let list = c.get_signals(None, None);
    assert_eq!(list.len(), 2);
    assert_eq!(list[0].market, "BTC/USDT");
    assert_eq!(list[1].market, "NEAR/USDC");
    assert_eq!(c.get_signals(Some(1), Some(5)).len(), 1);
    assert_eq!(c.get_purchases(0).len(), 0);
}

#[test]
fn owner_manages_verifiers() {
    let mut c = setup();
    ctx("owner.near", T0, zero());
    c.add_verifier(acc("v2.near"));
    assert_eq!(c.get_config().verifiers.len(), 2);
    c.remove_verifier(acc("verifier.near"));
    assert_eq!(c.get_config().verifiers, vec![acc("v2.near")]);
}

#[test]
fn commitment_matches_known_sha256() {
    // sha256("abc:def") computed independently with Python hashlib.
    ctx("owner.near", T0, zero());
    assert_eq!(
        commitment_of("abc", "def"),
        "ec5952851b8051e1ecf6b6076d99d05646cd90a9f293c17250105742b9e4a19e"
    );
}
