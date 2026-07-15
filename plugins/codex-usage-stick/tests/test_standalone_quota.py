import importlib.util
import sys
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "tools" / "codex_usage_ble_bridge.py"
SPEC = importlib.util.spec_from_file_location("standalone_quota_test", SCRIPT)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)

FUTURE_RESET = int(time.time()) + 86400


def snapshot(primary: int, secondary: int, tokens: int = 1):
    return bridge.UsageSnapshot(
        tokens=tokens,
        primary=primary,
        secondary=secondary,
        primary_resets_at=FUTURE_RESET,
        secondary_resets_at=FUTURE_RESET,
        source=Path("test"),
        event_ts=1.0,
        limit_id="codex",
        limit_name=None,
    )


class StandaloneQuotaTest(unittest.TestCase):
    def test_maps_weekly_window_by_duration(self):
        values = bridge.normalize_rate_limit_windows(
            {
                "primary": {
                    "used_percent": 5,
                    "window_minutes": 10080,
                    "resets_at": FUTURE_RESET,
                }
            }
        )
        self.assertEqual(values, (0, 5, 0, FUTURE_RESET))

    def test_stabilizes_first_zero_snapshot(self):
        stable, accepted = bridge.stabilize_zero_quota(
            snapshot(0, 0, tokens=2),
            snapshot(8, 2),
            {},
        )
        self.assertFalse(accepted)
        self.assertEqual((stable.primary, stable.secondary, stable.tokens), (8, 2, 2))


if __name__ == "__main__":
    unittest.main()
