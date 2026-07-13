import importlib.util
import sys
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_usage_ble_bridge.py"
SPEC = importlib.util.spec_from_file_location("codex_usage_ble_bridge_test", SCRIPT)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)


FUTURE_RESET = int(time.time()) + 86400


def snapshot(
    primary: int,
    secondary: int,
    tokens: int = 1,
    primary_reset: int = FUTURE_RESET,
    secondary_reset: int = FUTURE_RESET,
):
    return bridge.UsageSnapshot(
        tokens=tokens,
        primary=primary,
        secondary=secondary,
        primary_resets_at=primary_reset,
        secondary_resets_at=secondary_reset,
        source=Path("test"),
        event_ts=1.0,
        limit_id="codex",
        limit_name=None,
    )


class ZeroQuotaStabilizerTest(unittest.TestCase):
    def test_accepts_zero_on_third_consecutive_snapshot(self):
        accepted = snapshot(8, 2, tokens=10)
        pending = {}

        for tokens in (11, 12):
            stable, was_accepted = bridge.stabilize_zero_quota(
                snapshot(0, 0, tokens=tokens), accepted, pending
            )
            self.assertFalse(was_accepted)
            self.assertEqual((stable.primary, stable.secondary), (8, 2))
            self.assertEqual(stable.tokens, tokens)

        stable, was_accepted = bridge.stabilize_zero_quota(
            snapshot(0, 0, tokens=13), accepted, pending
        )
        self.assertTrue(was_accepted)
        self.assertEqual((stable.primary, stable.secondary), (0, 0))
        self.assertEqual(pending, {})

    def test_nonzero_or_partial_zero_is_accepted_immediately(self):
        accepted = snapshot(8, 2)
        for primary, secondary in ((0, 4), (4, 0), (4, 2)):
            with self.subTest(primary=primary, secondary=secondary):
                pending = {"matches": 2}
                stable, was_accepted = bridge.stabilize_zero_quota(
                    snapshot(primary, secondary), accepted, pending
                )
                self.assertTrue(was_accepted)
                self.assertEqual((stable.primary, stable.secondary), (primary, secondary))
                self.assertEqual(pending, {})

    def test_zero_is_immediate_after_zero_has_been_accepted(self):
        pending = {"matches": 2}
        stable, was_accepted = bridge.stabilize_zero_quota(
            snapshot(0, 0, tokens=2), snapshot(0, 0), pending
        )
        self.assertTrue(was_accepted)
        self.assertEqual(stable.tokens, 2)
        self.assertEqual(pending, {})

    def test_weekly_only_zero_is_accepted_immediately(self):
        pending = {"matches": 2}
        stable, was_accepted = bridge.stabilize_zero_quota(
            snapshot(0, 0, primary_reset=0), snapshot(8, 2), pending
        )
        self.assertTrue(was_accepted)
        self.assertEqual((stable.primary, stable.secondary), (0, 0))
        self.assertEqual(pending, {})


if __name__ == "__main__":
    unittest.main()
