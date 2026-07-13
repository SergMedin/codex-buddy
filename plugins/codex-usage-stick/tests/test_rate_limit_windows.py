import importlib.util
import sys
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_usage_ble_bridge.py"
SPEC = importlib.util.spec_from_file_location("codex_usage_ble_bridge_window_test", SCRIPT)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)

FUTURE_RESET = int(time.time()) + 86400


def snapshot(
    primary: int = 0,
    secondary: int = 0,
    primary_reset: int = 0,
    secondary_reset: int = FUTURE_RESET,
    event_ts: float | None = None,
):
    return bridge.UsageSnapshot(
        tokens=123,
        primary=primary,
        secondary=secondary,
        primary_resets_at=primary_reset,
        secondary_resets_at=secondary_reset,
        source=Path("test"),
        event_ts=time.time() if event_ts is None else event_ts,
        limit_id="codex",
        limit_name=None,
    )


class RateLimitWindowTest(unittest.TestCase):
    def test_maps_weekly_window_from_primary_rollout_slot(self):
        values = bridge.normalize_rate_limit_windows(
            {
                "primary": {
                    "used_percent": 5,
                    "window_minutes": 10080,
                    "resets_at": FUTURE_RESET,
                },
                "secondary": None,
            }
        )
        self.assertEqual(values, (0, 5, 0, FUTURE_RESET))

    def test_maps_camel_case_dual_windows_by_duration(self):
        values = bridge.normalize_rate_limit_windows(
            {
                "primary": {
                    "usedPercent": 11,
                    "windowDurationMins": 300,
                    "resetsAt": FUTURE_RESET,
                },
                "secondary": {
                    "usedPercent": 22,
                    "windowDurationMins": 10080,
                    "resetsAt": FUTURE_RESET + 1,
                },
            }
        )
        self.assertEqual(values, (11, 22, FUTURE_RESET, FUTURE_RESET + 1))

    def test_legacy_dual_windows_keep_positional_mapping(self):
        values = bridge.normalize_rate_limit_windows(
            {
                "primary": {"usedPercent": 11, "resetsAt": FUTURE_RESET},
                "secondary": {"usedPercent": 22, "resetsAt": FUTURE_RESET + 1},
            }
        )
        self.assertEqual(values, (11, 22, FUTURE_RESET, FUTURE_RESET + 1))

    def test_weekly_window_is_required_for_a_valid_snapshot(self):
        self.assertTrue(bridge.snapshot_has_rate_limit(snapshot(secondary=5), "codex"))
        self.assertFalse(
            bridge.snapshot_has_rate_limit(
                snapshot(primary=5, primary_reset=FUTURE_RESET, secondary_reset=0),
                "codex",
            )
        )

    def test_packet_omits_unavailable_primary_window(self):
        packet = snapshot(secondary=5).packet("idle")
        self.assertNotIn("primary", packet)
        self.assertNotIn("primary_resets_at", packet)
        self.assertEqual(packet["secondary"], 5)
        self.assertEqual(packet["secondary_resets_at"], FUTURE_RESET)

    def test_packet_keeps_real_weekly_zero(self):
        packet = snapshot(secondary=0).packet("idle")
        self.assertEqual(packet["secondary"], 0)
        self.assertEqual(packet["secondary_resets_at"], FUTURE_RESET)

    def test_packet_does_not_roll_expired_window_forward(self):
        packet = snapshot(secondary=5, secondary_reset=int(time.time()) - 1).packet("idle")
        self.assertNotIn("secondary", packet)
        self.assertNotIn("secondary_resets_at", packet)

    def test_app_server_accepts_current_weekly_only_shape(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 5,
                    "windowDurationMins": 10080,
                    "resetsAt": FUTURE_RESET,
                },
                "secondary": None,
            }
        }
        parsed = bridge.app_server_usage_snapshot_from_result(result, "codex", None)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual((parsed.primary, parsed.secondary), (0, 5))
        self.assertEqual((parsed.primary_resets_at, parsed.secondary_resets_at), (0, FUTURE_RESET))

    def test_app_server_rejects_five_hour_only_shape(self):
        result = {
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 5,
                    "windowDurationMins": 300,
                    "resetsAt": FUTURE_RESET,
                },
                "secondary": None,
            }
        }
        self.assertIsNone(
            bridge.app_server_usage_snapshot_from_result(result, "codex", None)
        )

    def test_new_weekly_snapshot_clears_stale_primary_window(self):
        old = snapshot(
            primary=27,
            secondary=19,
            primary_reset=FUTURE_RESET,
            secondary_reset=FUTURE_RESET,
            event_ts=1,
        )
        latest = snapshot(secondary=6, event_ts=2)
        merged = bridge.merge_latest_rate_limits(old, latest)
        self.assertEqual((merged.primary, merged.secondary), (0, 6))
        self.assertEqual(
            (merged.primary_resets_at, merged.secondary_resets_at),
            (0, FUTURE_RESET),
        )
        self.assertNotIn("primary", merged.packet("idle"))


if __name__ == "__main__":
    unittest.main()
