"""Dashboard validity depends on quota observations, not transport heartbeats."""

import argparse
import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_usage_ble_bridge.py"
SPEC = importlib.util.spec_from_file_location("dashboard_bridge_test", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)
T = 1_800_000_000
DAY = 86400


def credit(expires=T + DAY, **changes):
    return {"resetType": "codexRateLimits", "status": "available", "expiresAt": expires, **changes}


def gift(credits, count=0):
    return {"rateLimitResetCredits": {"availableCount": count, "credits": credits}}


class DashboardDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = argparse.Namespace(
            codex_home=self.root, snapshot_cache_path=self.root / "last_usage_snapshot.json",
            limit_id="codex", thread_id=None, thread_scan_limit=12, rollout=None,
            tail_bytes=4096, verbose=False, state="idle", dry_run=False,
        )

    def snapshot(self, at=T, used=49, reset=T + 7 * DAY, **changes):
        return bridge.UsageSnapshot(
            tokens=100, primary=0, secondary=used, primary_resets_at=0,
            secondary_resets_at=reset, source=bridge.APP_SERVER_USAGE_SOURCE,
            event_ts=T - DAY, limit_id="codex", limit_name=None,
            quota_observed_at=at, quota_live=True, quota_account="account:test",
            auth_scope=bridge.auth_scope(self.args), **changes,
        )

    def read(self, live=None, logs=(), now=T):
        with patch.object(bridge, "latest_rollout_paths", return_value=[self.root]), \
             patch.object(bridge, "extract_token_counts", return_value=list(logs)), \
             patch.object(bridge, "read_app_server_usage", return_value=live), \
             patch.object(bridge.time, "time", return_value=now):
            return bridge.read_usage(self.args)

    def packet(self, snapshot, now=T):
        with patch.object(bridge.time, "time", return_value=now):
            return snapshot.packet("idle")

    def test_credits_select_nearest_available_codex_expiry_not_count(self):
        result = gift([
            credit(T + 10 * DAY), credit(T + 3 * DAY), credit(T - 1),
            credit(T + 1, status="used"), credit(T + 2, resetType="other"),
        ], count=0)
        self.assertEqual(bridge.gift_reset_snapshot(result, T), (T + 3 * DAY, T))

    def test_empty_and_expired_credit_lists_prove_none(self):
        for credits in ([], [credit(T)], [credit(T - 1)], [credit(status="used")]):
            with self.subTest(credits=credits):
                self.assertEqual(bridge.gift_reset_snapshot(gift(credits, count=3), T), (0, T))

    def test_missing_or_malformed_credits_are_unknown(self):
        for result in ({}, {"rateLimitResetCredits": None},
                       {"rateLimitResetCredits": {"availableCount": 0}},
                       gift(None), gift([None]), gift([{}]),
                       *(gift([credit(value)]) for value in (None, "tomorrow", True, float("nan"), float("inf"), -1))):
            with self.subTest(result=result):
                self.assertEqual(bridge.gift_reset_snapshot(result, T), (None, None))

    def test_expiring_cached_credit_becomes_unknown_not_confirmed_none(self):
        snapshot = self.snapshot(gift_reset_expires_at=T + 20, gift_observed_at=T)
        self.assertEqual(self.packet(snapshot, T + 19)["gift_reset_expires_at"], T + 20)
        packet = self.packet(snapshot, T + 20)
        self.assertNotIn("gift_reset_expires_at", packet)
        self.assertEqual(packet["gift_observed_at"], T)

    def test_real_zero_quota_and_zero_credits_remain_displayable(self):
        packet = self.packet(self.snapshot(used=0, gift_reset_expires_at=0, gift_observed_at=T))
        self.assertEqual(packet["secondary"], 0)
        self.assertEqual(packet["gift_reset_expires_at"], 0)

    def test_fresh_cached_expired_without_renewing_observation(self):
        current = self.read(self.snapshot())
        self.assertEqual(self.packet(current)["quota_status"], "fresh")
        for now, status in ((T + 10, "cached"), (T + 899, "cached"), (T + 900, "unavailable")):
            cached = self.read(now=now)
            packet = self.packet(cached, now)
            self.assertEqual(packet["quota_status"], status)
            self.assertEqual(packet["quota_observed_at"], T)
            self.assertEqual(packet["quota_valid_until"], T + 900)
            self.assertEqual("secondary" in packet, status != "unavailable")

    def test_cache_restart_keeps_quota_and_credit_age_but_never_live_flag(self):
        self.read(self.snapshot(gift_reset_expires_at=T + DAY, gift_observed_at=T - 20))
        del self.args._last_usage
        cached = self.read(now=T + 100)
        self.assertEqual(cached.quota_observed_at, T)
        self.assertFalse(cached.quota_live)
        self.assertEqual(cached.gift_observed_at, T - 20)
        self.assertEqual(cached.gift_reset_expires_at, T + DAY)

    def test_quota_expires_at_reset_even_if_observation_is_recent(self):
        snapshot = self.snapshot(reset=T + 10)
        self.assertEqual(self.packet(snapshot)["quota_valid_until"], T + 10)
        self.assertNotIn("secondary", self.packet(snapshot, T + 10))

    def test_legacy_cache_is_not_assumed_fresh(self):
        data = bridge.snapshot_to_cache(self.snapshot())
        data.pop("version")
        data["saved_at"] = T
        self.args.snapshot_cache_path.write_text(json.dumps(data))
        cached = self.read()
        self.assertEqual(self.packet(cached)["quota_status"], "unavailable")

    def test_corrupt_cache_is_not_normalized_into_displayable_zero(self):
        data = bridge.snapshot_to_cache(self.snapshot())
        for percent in (None, "", True, -1, 101, float("nan")):
            with self.subTest(percent=percent):
                data["secondary"] = percent
                self.args.snapshot_cache_path.write_text(json.dumps(data))
                self.assertIsNone(bridge.snapshot_from_cache(self.args.snapshot_cache_path))

    def test_repeated_task_log_does_not_renew_known_quota_age(self):
        self.read(self.snapshot())
        log = replace(self.snapshot(), event_ts=T + 300, quota_observed_at=None,
                      quota_log_valid=True, quota_live=False)
        cached = self.read(logs=[log], now=T + 300)
        self.assertEqual(cached.quota_observed_at, T)
        self.assertEqual(cached.event_ts, T + 300)

    def test_rollout_fallback_uses_oldest_matching_event_and_is_never_live(self):
        log = replace(self.snapshot(), event_ts=T - 200, quota_observed_at=None,
                      quota_log_valid=True, quota_live=False)
        fallback = self.read(logs=[log, replace(log, event_ts=T - 10)])
        self.assertEqual(fallback.quota_observed_at, T - 200)
        self.assertEqual(self.packet(fallback)["quota_status"], "cached")
        self.assertIsNone(fallback.quota_account)

    def test_no_source_returns_status_instead_of_raising(self):
        packet = self.packet(self.read())
        self.assertEqual(packet["quota_status"], "unavailable")
        self.assertEqual(packet["state"], "idle")
        self.assertNotIn("quota_observed_at", packet)

    def test_auth_change_discards_quota_and_credits(self):
        self.read(self.snapshot(gift_reset_expires_at=T + DAY, gift_observed_at=T))
        (self.root / "auth.json").write_text("changed")
        packet = self.packet(self.read())
        self.assertEqual(packet["quota_status"], "unavailable")
        self.assertNotIn("gift_reset_expires_at", packet)

    def test_account_switch_does_not_stabilize_zero_against_old_account(self):
        self.read(self.snapshot(used=40, gift_reset_expires_at=T + DAY, gift_observed_at=T))
        new = replace(self.snapshot(used=0), quota_account="account:other")
        current = self.read(new)
        self.assertEqual(current.secondary, 0)
        self.assertIsNone(current.gift_observed_at)
        self.assertEqual(current.quota_account, "account:other")

    def test_known_changed_account_without_quota_does_not_reuse_cache(self):
        self.read(self.snapshot())
        self.args._account_read = True
        self.args._usage_account = "account:other"
        self.assertEqual(self.packet(self.read())["quota_status"], "unavailable")
        self.args._account_read = False
        self.args._usage_account = None
        log = replace(self.snapshot(), event_ts=T + 10, quota_observed_at=None,
                      quota_log_valid=True, quota_live=False)
        self.assertEqual(self.packet(self.read(logs=[log]))["quota_status"], "unavailable")

    def test_confirmed_logout_blocks_old_logs_across_outage_restart_and_auth_change(self):
        log = replace(self.snapshot(), event_ts=T, quota_observed_at=None,
                      quota_log_valid=True, quota_live=False)
        self.args._account_read = True
        self.args._usage_account = None
        logged_out = self.read(logs=[log])
        self.assertTrue(logged_out.account_checked)
        self.assertEqual(self.packet(logged_out)["quota_status"], "unavailable")

        self.args._account_read = False
        for change in ("outage", "restart", "auth_changed", "restart_again"):
            with self.subTest(change=change):
                if change.startswith("restart"):
                    del self.args._last_usage
                if change == "auth_changed":
                    auth = self.root / "auth.json"
                    auth.write_text("new-auth-metadata")
                    # A carried-forward old payload can be logged after an
                    # auth change, so timestamp filtering alone is not enough.
                    log.event_ts = auth.stat().st_mtime + 1
                current = self.read(logs=[log])
                self.assertTrue(current.account_checked)
                self.assertEqual(self.packet(current)["quota_status"], "unavailable")

        self.args._account_read = True
        self.args._usage_account = "account:test"
        recovered = self.read(self.snapshot(used=20))
        self.assertEqual(self.packet(recovered)["quota_status"], "fresh")
        self.assertEqual(recovered.secondary, 20)

    def test_unscoped_log_never_replaces_an_authoritative_account_snapshot(self):
        self.read(self.snapshot(used=40))
        log = replace(self.snapshot(used=20), event_ts=T + 100, quota_observed_at=None,
                      quota_log_valid=True, quota_live=False)
        cached = self.read(logs=[log], now=T + 100)
        self.assertEqual(cached.secondary, 40)
        self.assertEqual(cached.quota_observed_at, T)

    def test_new_weekly_only_api_result_clears_old_primary_window(self):
        self.read(replace(self.snapshot(), primary=40, primary_resets_at=T + DAY))
        current = self.read(self.snapshot(used=30))
        self.assertNotIn("primary", self.packet(current))

    def test_gift_read_refreshes_independently_of_quota(self):
        self.read(self.snapshot(gift_reset_expires_at=T + DAY, gift_observed_at=T))
        self.args._gift_snapshot = (0, T + 100)
        current = self.read(now=T + 100)
        self.assertEqual(current.quota_observed_at, T)
        self.assertEqual(current.gift_observed_at, T + 100)
        self.assertEqual(current.gift_reset_expires_at, 0)

    def test_cached_forecast_is_fixed_and_does_not_extend_history_or_expiry(self):
        first = self.snapshot(at=T - DAY, used=10, reset=T + 6 * DAY)
        current = self.snapshot(used=20, reset=T + 6 * DAY)
        with patch.object(bridge.time, "time", return_value=T - DAY):
            bridge.forecast_packet_fields(self.args, first)
        with patch.object(bridge.time, "time", return_value=T):
            expected = bridge.forecast_packet_fields(self.args, current)
        history = self.args._quota_forecast
        before = history.path.read_bytes()
        cached = replace(current, quota_live=False)
        for now in (T + 10, T + 800):
            with patch.object(bridge.time, "time", return_value=now):
                self.assertEqual(bridge.forecast_packet_fields(self.args, cached), expected)
            self.assertEqual(history.path.read_bytes(), before)
        with patch.object(bridge.time, "time", return_value=T + 900):
            self.assertEqual(bridge.forecast_packet_fields(self.args, cached),
                             {"secondary_forecast_status": "unavailable"})

    def test_cached_observation_cannot_seed_empty_history(self):
        self.args._quota_log_samples = [replace(self.snapshot(at=T - DAY), quota_log_valid=True,
                                              event_ts=T - DAY)]
        with patch.object(bridge.time, "time", return_value=T):
            fields = bridge.forecast_packet_fields(self.args, replace(self.snapshot(), quota_live=False))
        self.assertEqual(fields, {"secondary_forecast_status": "unavailable"})
        self.assertEqual(self.args._quota_forecast.points, [])

    def test_only_real_warmup_reports_learning(self):
        for at, expected in ((T, "learning"), (T + 1800, "learning"), (T + 3600, "ready")):
            with patch.object(bridge.time, "time", return_value=at):
                fields = bridge.forecast_packet_fields(self.args, self.snapshot(at=at))
            self.assertEqual(fields["secondary_forecast_status"], expected)

    def test_bad_coverage_reports_unavailable_not_learning(self):
        # A day before a reset and a day after it contain a long interval
        # whose consumption is unknowable, despite two days of history.
        for at, used, reset in ((T, 60, T + DAY), (T + DAY, 2, T + 8 * DAY),
                                (T + 2 * DAY, 12, T + 8 * DAY)):
            with patch.object(bridge.time, "time", return_value=at):
                fields = bridge.forecast_packet_fields(self.args, self.snapshot(at, used, reset))
        self.assertEqual(fields, {"secondary_forecast_status": "unavailable"})

    def test_rejected_observation_during_warmup_is_not_learning(self):
        with patch.object(bridge.time, "time", return_value=T):
            bridge.forecast_packet_fields(self.args, self.snapshot(used=40))
        with patch.object(bridge.time, "time", return_value=T + 30):
            fields = bridge.forecast_packet_fields(self.args, self.snapshot(at=T + 30, used=20))
        self.assertEqual(fields, {"secondary_forecast_status": "unavailable"})

    def test_poll_exception_still_sends_unavailable_heartbeat(self):
        ble = argparse.Namespace(write_json=AsyncMock())
        with patch.object(bridge, "read_usage", side_effect=OSError("unreadable")), \
             patch.object(bridge.time, "time", return_value=T):
            asyncio.run(bridge.send_usage_update(self.args, bridge.ActivityTracker(), ble))
        packet = ble.write_json.call_args.args[0]
        self.assertEqual(packet["quota_status"], "unavailable")
        self.assertEqual(packet["secondary_forecast_status"], "unavailable")
        self.assertEqual(packet["now"], T)

    def test_poll_exception_preserves_usable_cache_with_warning(self):
        self.read(self.snapshot())
        ble = argparse.Namespace(write_json=AsyncMock())
        with patch.object(bridge, "read_usage", side_effect=OSError("unreadable")), \
             patch.object(bridge.time, "time", return_value=T + 100):
            asyncio.run(bridge.send_usage_update(self.args, bridge.ActivityTracker(), ble))
        packet = ble.write_json.call_args.args[0]
        self.assertEqual(packet["quota_status"], "cached")
        self.assertEqual(packet["quota_observed_at"], T)
        self.assertEqual(packet["secondary"], 49)

    def test_slow_poll_sends_status_and_reuses_one_pending_task(self):
        self.args.update_timeout = 0.01
        current = self.snapshot()
        ble = argparse.Namespace(write_json=AsyncMock())

        async def scenario():
            ready = asyncio.Event()

            async def slow_read(*_):
                await ready.wait()
                return current

            with patch.object(bridge.asyncio, "to_thread", side_effect=slow_read) as read:
                await bridge.send_usage_update(self.args, bridge.ActivityTracker(), ble)
                await bridge.send_usage_update(self.args, bridge.ActivityTracker(), ble)
                self.assertEqual(read.call_count, 1)
                self.assertEqual(ble.write_json.call_args.args[0]["quota_status"], "unavailable")
                ready.set()
                await asyncio.sleep(0)
                await bridge.send_usage_update(self.args, bridge.ActivityTracker(), ble)
                self.assertEqual(read.call_count, 1)
                self.assertIsNone(self.args._usage_poll)
                self.assertEqual(ble.write_json.call_args.args[0]["quota_status"], "fresh")

        with patch.object(bridge.time, "time", return_value=T):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
