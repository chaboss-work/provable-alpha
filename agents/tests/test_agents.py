import hashlib
import json
import unittest

from nacl.public import PrivateKey

from pa import market, sealing, strategy


def snap(dev_cex_mid=3.6265, sell=3.611, buy=3.632):
    books = {"binance": {"bid": 3.625, "ask": 3.626}, "okx": {"bid": 3.626, "ask": 3.627}, "bybit": {"bid": 3.627, "ask": 3.628}}
    return market.build_snapshot(books, sell, buy, 100.0, ["q1", "q2"], "2026-11-10T09:00:00Z")


class SealingTest(unittest.TestCase):
    def test_commitment_matches_contract_rule(self):
        # Same vector as the Rust test `commitment_matches_known_sha256`.
        self.assertEqual(sealing.commitment_of("abc", "def"),
                         "ec5952851b8051e1ecf6b6076d99d05646cd90a9f293c17250105742b9e4a19e")
        self.assertEqual(sealing.commitment_of("p", "s"), hashlib.sha256(b"p:s").hexdigest())

    def test_seal_roundtrip_and_wrong_key(self):
        k = PrivateKey.generate()
        ct = sealing.seal('{"a":1}', "salt", sealing.public_b64(k))
        self.assertEqual(sealing.open_sealed(ct, k), ('{"a":1}', "salt"))
        with self.assertRaises(Exception):
            sealing.open_sealed(ct, PrivateKey.generate())

    def test_canonical_is_stable(self):
        self.assertEqual(sealing.canonical({"b": 1, "a": [2, 1]}), '{"a":[2,1],"b":1}')


class SnapshotTest(unittest.TestCase):
    def test_math(self):
        s = snap()
        self.assertAlmostEqual(s.cex_mid, 3.6265, places=6)
        self.assertEqual(s.best_bid, 3.627)
        self.assertEqual(s.best_ask, 3.626)
        self.assertAlmostEqual(s.intents_mid, 3.6215, places=6)
        self.assertAlmostEqual(s.dev_bps, round((3.6215 / 3.6265 - 1) * 1e4, 2))
        self.assertLess(s.edge_sell_bps, 0)


class StrategyTest(unittest.TestCase):
    def baseline(self, value=-13.0):
        b = strategy.Baseline()
        for _ in range(3):
            b.update(value)
        return b

    def test_no_signal_before_baseline_ready(self):
        b = strategy.Baseline()
        b.update(-13)
        self.assertIsNone(strategy.make_signal(snap(), b, 1.0, 60))

    def test_signal_fires_on_excess(self):
        s = snap(sell=3.600)  # Intents much cheaper than usual
        p = strategy.make_signal(s, self.baseline(), 6.0, 600)
        self.assertIsNotNone(p)
        self.assertEqual(p["direction"], "intents_cheap")
        self.assertLess(p["excess_bps"], -6)
        self.assertFalse(p["forced"])
        json.loads(sealing.canonical(p))

    def test_small_deviation_needs_force(self):
        s = snap()
        b = self.baseline(s.dev_bps)
        self.assertIsNone(strategy.make_signal(s, b, 6.0, 600))
        self.assertTrue(strategy.make_signal(s, b, 6.0, 600, forced=True)["forced"])

    def test_judge_hit_and_miss(self):
        p = {"baseline_bps": -13.0, "excess_bps": -20.0}
        hit, realized, ev = strategy.judge(p, snap())  # dev ~ -13.8 -> excess ~ -0.8
        self.assertTrue(hit)
        self.assertGreater(realized, 15)
        self.assertIn("excess_before", ev)
        p2 = {"baseline_bps": 0.0, "excess_bps": -10.0}
        hit2, realized2, _ = strategy.judge(p2, snap(sell=3.590))  # deviation got worse
        self.assertFalse(hit2)
        self.assertLess(realized2, 0)

    def test_judge_sign_flip_counts_as_hit(self):
        p = {"baseline_bps": -20.0, "excess_bps": -3.0}
        hit, realized, _ = strategy.judge(p, snap())  # excess after ~ +6 -> flipped
        self.assertTrue(hit)
        self.assertGreater(realized, 8)  # moved ~9 bps in the predicted direction

    def test_judge_positive_excess(self):
        p = {"baseline_bps": -20.0, "excess_bps": 10.0}
        hit, realized, _ = strategy.judge(p, snap())  # excess after ~ +6: shrank, but less than half
        self.assertFalse(hit)
        self.assertGreater(realized, 3)


if __name__ == "__main__":
    unittest.main()
