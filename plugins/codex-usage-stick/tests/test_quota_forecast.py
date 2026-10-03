import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "codex_usage_ble_bridge.py"
SPEC = importlib.util.spec_from_file_location("forecast_bridge_test", SCRIPT)
bridge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bridge
SPEC.loader.exec_module(bridge)

DAY = 86400
T = 1_800_000_000
SHORT = "secondary_forecast_48h"
LONG = "secondary_forecast_14d"
TTL = "secondary_forecast_valid_until"
REMAINING_SHORT = "secondary_remaining_48h_bp"
REMAINING_LONG = "secondary_remaining_14d_bp"
KEY = ["codex", "account-v1:test-account"]


def snapshot(at, percent, reset=7 * DAY):
    return bridge.UsageSnapshot(
        tokens=999, primary=0, secondary=percent, primary_resets_at=0,
        secondary_resets_at=T + reset, source=bridge.APP_SERVER_USAGE_SOURCE,
        event_ts=1, limit_id="codex", limit_name=None, quota_observed_at=T + at,
        quota_account=KEY[1], quota_live=True,
    )


def log_snapshot(at, percent, reset=7 * DAY):
    s = snapshot(at, percent, reset)
    s.quota_observed_at = None
    s.event_ts = T + at
    s.quota_log_valid = True
    return s


class QuotaForecastTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quota_history.json"
        self.history = bridge.QuotaForecast(self.path)

    def observe(self, at, percent, reset=7 * DAY, key=KEY):
        return self.history.packet_fields(snapshot(at, percent, reset), T + at, key)

    def test_warmup_uses_available_history_for_both_windows(self):
        self.assertEqual(self.observe(0, 10), {})
        self.assertEqual(self.observe(1800, 10), {})
        fields = self.observe(3600, 10)
        self.assertEqual((fields[SHORT], fields[LONG]), (10, 10))

    def test_seed_uses_existing_logs_without_waiting(self):
        current = snapshot(DAY, 20)
        logs = [log_snapshot(0, 10), log_snapshot(3600, 10)]
        self.history.seed(current, logs, KEY)
        fields = self.observe(DAY, 20)
        self.assertEqual((fields[SHORT], fields[LONG]), (80, 80))

    def test_seed_is_persisted_even_when_latest_log_is_less_than_five_minutes_old(self):
        current = snapshot(DAY, 20)
        logs = [log_snapshot(0, 10), log_snapshot(DAY - 5, 20)]
        self.history.seed(current, logs, KEY)
        fields = self.observe(DAY, 20)
        self.assertEqual(fields[LONG], 80)
        self.assertEqual(bridge.QuotaForecast(self.path).points, self.history.points)

    def test_seed_rejects_other_cycles_limits_invalid_and_future_records(self):
        other = log_snapshot(0, 1)
        other.limit_id = "other"
        malformed = log_snapshot(3600, 0)
        malformed.quota_log_valid = False
        logs = [other, malformed, log_snapshot(0, 1, 6 * DAY), log_snapshot(2 * DAY, 20)]
        self.history.seed(snapshot(DAY, 20), logs, KEY)
        self.assertEqual(self.history.points, [])

    def test_seed_ignores_records_before_authentication_change(self):
        logs = [log_snapshot(0, 10), log_snapshot(3600, 11)]
        self.history.seed(snapshot(DAY, 20), logs, KEY, earliest=T + 3000)
        self.assertEqual(len(self.history.points), 1)
        self.assertEqual(self.history.points[0][0], T + 3600)

    def test_seed_sorts_deduplicates_and_rejects_percentage_dips(self):
        logs = [log_snapshot(7200, 12), log_snapshot(0, 10), log_snapshot(0, 10),
                log_snapshot(3600, 0), log_snapshot(5, 10)]
        self.history.seed(snapshot(DAY, 20), logs, KEY)
        self.assertEqual([p[1] for p in self.history.points], [10, 12])

    def test_seed_does_not_replace_saved_history_or_inconsistent_live_quota(self):
        self.history.seed(snapshot(DAY, 10), [log_snapshot(0, 20)], KEY)
        self.assertEqual(self.history.points, [])
        self.observe(0, 10)
        self.history.seed(snapshot(DAY, 20), [log_snapshot(0, 1)], KEY)
        self.assertEqual(self.history.points[0][1], 10)

    def test_seed_switches_identity_without_inheriting_other_accounts_latest_observation(self):
        self.observe(2 * DAY, 70, key=["codex", "account-v1:other-account"])
        self.history.seed(snapshot(DAY, 20), [log_snapshot(0, 10), log_snapshot(3600, 10)], KEY)
        self.assertEqual(self.history.key, KEY)
        self.assertEqual([point[0] for point in self.history.points], [T, T + 3600])
        self.assertEqual(self.observe(DAY, 20)[LONG], 80)
        restored = bridge.QuotaForecast(self.path)
        self.assertEqual(restored.key, KEY)
        self.assertEqual(restored.points, self.history.points)

    def test_one_second_reset_jitter_preserves_history_and_restart(self):
        self.observe(0, 10)
        fields = self.observe(DAY, 20, 7 * DAY + 1)
        self.assertEqual(fields[LONG], 80)
        self.history = bridge.QuotaForecast(self.path)
        self.assertEqual(len(self.history.points), 2)
        self.assertEqual(self.observe(2 * DAY, 30)[LONG], 80)

    def test_seed_accepts_rounding_difference_in_reset_timestamp(self):
        self.history.seed(snapshot(DAY, 20), [log_snapshot(0, 10, 7 * DAY + 1)], KEY)
        self.assertEqual(self.observe(DAY, 20)[LONG], 80)

    def test_extract_log_validates_quota_but_never_marks_it_live(self):
        log = self.path.parent / "rollout.jsonl"
        event = {"timestamp": "2027-01-15T08:00:00Z", "payload": {
            "type": "token_count", "rate_limits": {"limit_id": "codex", "primary": {
                "used_percent": 10, "window_minutes": 10080, "resets_at": T + 7 * DAY}}}}
        log.write_text(json.dumps(event) + "\n")
        s = bridge.extract_token_counts(log, 1024)[0]
        self.assertTrue(s.quota_log_valid)
        self.assertIsNone(s.quota_observed_at)
        event["payload"]["rate_limits"]["primary"]["used_percent"] = None
        log.write_text(json.dumps(event) + "\n")
        self.assertFalse(bridge.extract_token_counts(log, 1024)[0].quota_log_valid)

    def test_forecasts_current_usage_plus_remaining_time(self):
        self.observe(0, 10)
        fields = self.observe(DAY, 20)
        self.assertEqual((fields[SHORT], fields[LONG]), (80, 80))

    def test_48h_and_14d_diverge_across_weekly_reset(self):
        # First seven days: 4 points/day. Then six days: 10 points/day.
        for hour in range(14 * 24):
            day = hour / 24
            if day < 7:
                fields = self.observe(hour * 3600, day * 4)
            else:
                fields = self.observe(hour * 3600, (day - 7) * 10, 14 * DAY)
        self.assertEqual(fields[SHORT], 70)
        self.assertEqual(fields[LONG], 70)  # Near reset, both converge.
        # At day 13 one day remains: recent 10/day vs roughly 7/day overall.
        self.history = bridge.QuotaForecast(self.path)
        points = [p for p in self.history.points if p[0] <= T + 13 * DAY]
        self.history.points = points
        self.history._latest = points[-1]
        fields = self.observe(13 * DAY, 60, 14 * DAY)
        self.assertEqual(fields[SHORT], 70)
        self.assertEqual(fields[LONG], 67)

    def test_horizons_clip_old_history(self):
        # 21 days with near-continuous observations and correctly separated cycles.
        with patch.object(self.history, "save"):
            for hour in range(21 * 24 + 1):
                day = hour / 24
                cycle = int(day // 7)
                rate = 2 if cycle == 0 else 4
                fields = self.observe(hour * 3600, (day % 7) * rate, (cycle + 1) * 7 * DAY)
        self.assertEqual(fields[LONG], 28)
        self.assertLessEqual(self.history.points[0][0], T + 7 * DAY)
        self.assertGreater(self.history.points[1][0], T + 7 * DAY)

    def test_real_zero_and_overflow_are_distinct(self):
        self.observe(0, 0)
        fields = self.observe(3600, 0)
        self.assertEqual(fields[SHORT], 0)
        fields = self.observe(7200, 2)
        self.assertEqual(fields[SHORT], 101)

    def test_exactly_100_is_not_overflow(self):
        self.observe(0, 30)
        fields = self.observe(DAY, 40)
        self.assertEqual(fields[SHORT], 100)
        self.assertEqual(fields[REMAINING_SHORT], 0)

    def test_remaining_precision_and_strict_scale_boundaries(self):
        # Observe one hour at a known rate, then extrapolate to the reset.
        # Recreate the history so each case is independent.
        for projected, expected in ((0, 5100), (49.999, 5100), (50, 5000),
                                    (99.75, 25), (100, 0), (100.25, -25),
                                    (101, -100), (149.999, -5000),
                                    (150, -5000), (150.001, -5100), (290, -5100)):
            with self.subTest(projected=projected):
                self.history.points = []
                self.history._latest = None
                # Current usage stays valid even for projections above 100%.
                current = projected / 2
                if current > 100:
                    current = projected / 4
                    reset = 4 * 3600
                else:
                    reset = 2 * 3600
                self.observe(0, 0, reset)
                fields = self.observe(3600, current, reset)
                self.assertEqual(fields[REMAINING_SHORT], expected)
                self.assertEqual(fields[REMAINING_LONG], expected)

    def test_remaining_is_omitted_when_its_horizon_is_unavailable(self):
        self.observe(0, 10)
        fields = self.observe(3 * DAY, 40)
        self.assertNotIn(REMAINING_SHORT, fields)
        self.assertEqual(fields[REMAINING_LONG], 2000)

    def test_temporary_decrease_does_not_double_count(self):
        self.observe(0, 40)
        self.assertEqual(self.observe(DAY, 0), {})
        fields = self.observe(2 * DAY, 40)
        self.assertEqual((fields[SHORT], fields[LONG]), (40, 40))

    def test_reset_is_not_counted_as_consumption(self):
        self.observe(6 * DAY, 60)
        self.observe(7 * DAY, 2, 14 * DAY)
        fields = self.observe(8 * DAY, 12, 14 * DAY)
        # Half this interval is unknown across the reset: hide instead of guessing.
        self.assertEqual(fields, {})

    def test_manual_reset_preserves_rate_immediately_and_after_restart(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        fields = self.observe(DAY + 5, 0, 8 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (70, 70))
        self.assertEqual(len(self.history.points), 3)
        self.history = bridge.QuotaForecast(self.path)
        self.assertEqual(len(self.history.points), 3)
        self.assertEqual(self.observe(DAY + 10, 0, 8 * DAY)[LONG], 70)

    def test_multiple_manual_resets_keep_distinct_rolling_rates(self):
        with patch.object(self.history, "save"):
            for hour in range(12 * 24 + 1):
                day = hour / 24
                cycle_start = int(day // 3) * 3
                rate = 2 if day < 9 else 10
                fields = self.observe(hour * 3600, (day - cycle_start) * rate,
                                      (cycle_start + 7) * DAY)
        # At the latest reset, use prior history immediately: 10/day recently,
        # versus 4/day over the whole available 12 days.
        self.assertEqual((fields[SHORT], fields[LONG]), (70, 28))
        self.history.save()
        self.assertEqual(bridge.QuotaForecast(self.path).points, self.history.points)

    def test_reset_deadline_can_move_earlier_without_erasing_history(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        fields = self.observe(DAY + 5, 0, 6 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (50, 50))
        self.history = bridge.QuotaForecast(self.path)
        self.assertEqual(len(self.history.points), 3)

    def test_rolling_zero_deadline_preserves_history_and_sampling_after_early_reset(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        before_reset = self.history.points[:]
        # An unused new week can have a deadline of "now + seven days" on
        # every poll. The deadline is metadata, not a unique quota cycle.
        with patch.object(self.history, "save") as save:
            for at in range(DAY + 7, DAY + 10 * 3600 + 1, 7):
                fields = self.observe(at, 0, at + 7 * DAY)
        self.assertEqual(self.history.status, "ready")
        self.assertIn(SHORT, fields)
        self.assertIn(LONG, fields)
        self.assertGreater(fields[LONG], 0)
        self.assertLessEqual(save.call_count, 121)
        self.assertLessEqual(len(self.history.points), len(before_reset) + 121)
        self.assertEqual(self.history.points[:len(before_reset)], before_reset)
        self.assertEqual(len({point[3] for point in self.history.points}), 2)

        self.history.save()
        self.history = bridge.QuotaForecast(self.path)
        at = DAY + 10 * 3600 + 7
        fields = self.observe(at, 0, at + 7 * DAY)
        self.assertEqual(self.history.status, "ready")
        self.assertIn(SHORT, fields)
        self.assertIn(LONG, fields)
        self.assertEqual(len({point[3] for point in self.history.points}), 2)

    def test_positive_deadline_change_keeps_prior_rate_but_excludes_crossing(self):
        for reset, expected in ((6 * DAY, 70), (8 * DAY, 90)):
            with self.subTest(reset=reset):
                self.history.points = []
                self.history._latest = None
                self.observe(0, 10)
                self.observe(DAY, 20)
                # A positive counter with a changed deadline could conceal a
                # reset. Preserve the known prior rate, without counting this
                # uncertain interval as additional usage or idle time.
                fields = self.observe(DAY + 300, 20, reset)
                self.assertEqual((fields[SHORT], fields[LONG]), (expected, expected))
                self.assertEqual(self.history.points[-1][2], T + reset)
                self.assertNotEqual(self.history.points[0][3], self.history.points[-1][3])
                self.history.save()
                self.history = bridge.QuotaForecast(self.path)
                self.assertEqual(self.observe(DAY + 305, 20, reset)[LONG], expected)

    def test_unused_week_and_first_usage_deadline_changes_preserve_cycle(self):
        self.observe(0, 0)
        fields = self.observe(DAY, 0, 8 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (0, 0))
        fields = self.observe(DAY + 3600, 10, 8 * DAY + 3600)
        self.assertEqual((fields[SHORT], fields[LONG]), (77, 77))
        self.assertEqual(len({point[3] for point in self.history.points}), 1)

    def test_increased_usage_with_changed_deadline_across_gap_is_not_invented_consumption(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        # The quota might have reset and accumulated 25% during this gap.
        # Treating it as a 5% same-cycle increase would produce a false rate.
        fields = self.observe(2 * DAY, 25, 9 * DAY)
        self.assertEqual(fields, {})
        self.assertEqual(self.history.status, "unavailable")
        self.assertNotEqual(self.history.points[-2][3], self.history.points[-1][3])

    def test_deadline_only_polls_do_not_bypass_sampling_but_update_projection(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        with patch.object(self.history, "save") as save:
            earlier = self.observe(DAY + 7, 20, 6 * DAY)
            later = self.observe(DAY + 14, 20, 8 * DAY)
            # Persist the first uncertain boundary so a restart cannot turn
            # it back into a measured interval. Later metadata is coalesced.
            save.assert_called_once()
        self.assertEqual(len(self.history.points), 2)
        self.assertEqual(earlier[LONG], 70)
        self.assertEqual(later[LONG], 90)
        self.assertEqual(earlier[TTL], T + DAY + 7 + 900)
        self.assertEqual(later[TTL], T + DAY + 14 + 900)

    def test_unsampled_ambiguous_boundary_survives_restart_and_deadline_reversion(self):
        self.observe(0, 10, 6 * DAY)
        self.observe(7, 10, 6 * DAY + 60)
        self.observe(14, 10, 6 * DAY)
        self.assertEqual(len(self.history.points), 1)
        restored = bridge.QuotaForecast(self.path)
        current = snapshot(21, 11, 6 * DAY)
        expected = self.history.packet_fields(current, T + 21, KEY)
        self.assertEqual(restored.packet_fields(current, T + 21, KEY), expected)
        # After warmup, the uncertain crossing must still be excluded. A
        # restart that forgets the unsampled boundary invents a usable hour.
        current = snapshot(3600, 12, 6 * DAY)
        expected = self.history.packet_fields(current, T + 3600, KEY)
        self.assertEqual(expected, {})
        self.assertEqual(self.history.status, "unavailable")
        self.assertEqual(restored.packet_fields(current, T + 3600, KEY), expected)
        self.assertEqual(restored.status, "unavailable")

    def test_unsampled_live_high_water_mark_rejects_later_percentage_dip(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        with patch.object(self.history, "save") as save:
            self.observe(DAY + 7, 40)
            self.assertEqual(self.observe(DAY + 14, 30), {})
            self.assertEqual(self.history.status, "unavailable")
            save.assert_not_called()
        self.assertEqual(len(self.history.points), 2)
        self.assertIn(LONG, self.observe(DAY + 300, 40))

    def test_legacy_rolling_zero_history_migrates_without_false_reset_boundaries(self):
        points = [[T, 10, T + 7 * DAY], [T + DAY, 20, T + 7 * DAY]]
        for at in range(DAY + 7, DAY + 6 * 3600 + 1, 7):
            points.append([T + at, 0, T + at + 7 * DAY])
        self.path.write_text(json.dumps({"version": 1, "key": KEY, "points": points}))
        self.history = bridge.QuotaForecast(self.path)
        self.assertLessEqual(len(self.history.points), 75)
        self.assertEqual(len({point[3] for point in self.history.points}), 2)
        self.assertEqual(self.history.points[:2],
                         [[T, 10, T + 7 * DAY, self.history.points[0][3]],
                          [T + DAY, 20, T + 7 * DAY, self.history.points[0][3]]])
        at = DAY + 6 * 3600 + 7
        fields = self.observe(at, 0, at + 7 * DAY)
        self.assertIn(SHORT, fields)
        self.assertIn(LONG, fields)
        self.history.save()
        self.assertEqual(json.loads(self.path.read_text())["version"], 2)
        self.assertEqual(bridge.QuotaForecast(self.path).points, self.history.points)

    def test_version_two_history_rejects_invalid_cycle_state(self):
        invalid_points = (
            [[T, 10, T + 7 * DAY, True]],
            [[T, 10, T + 7 * DAY, -1]],
            [[T, 10, T + 7 * DAY, 1], [T + DAY, 20, T + 7 * DAY, 0]],
            [[T, 10, T + 7 * DAY, 0], [T + DAY, 0, T + 7 * DAY, 0]],
            [[T, 10, T + 7 * DAY, 0], [T - 1, 20, T + 7 * DAY, 0]],
        )
        for points in invalid_points:
            with self.subTest(points=points):
                self.path.write_text(json.dumps({"version": 2, "key": KEY, "points": points}))
                self.assertEqual(bridge.QuotaForecast(self.path).points, [])

    def test_version_two_history_rejects_null_or_inconsistent_latest_state(self):
        points = [[T, 10, T + 7 * DAY, 1]]
        invalid_latest = (None, [T - 1, 10, T + 7 * DAY, 1],
                          [T, 10, T + 7 * DAY, 2],
                          [T + 7, 0, T + 7 * DAY, 1],
                          [T + 7, 10, T + 7 * DAY, 0])
        for latest in invalid_latest:
            with self.subTest(latest=latest):
                self.path.write_text(json.dumps({"version": 2, "key": KEY,
                                                 "points": points, "latest": latest}))
                self.assertEqual(bridge.QuotaForecast(self.path).points, [])

    def test_malformed_suffix_discards_entire_history_without_partial_import(self):
        for version in (1, 2):
            with self.subTest(version=version):
                points = [[T + at * DAY, 10 + at * 10, T + 7 * DAY] for at in range(3)]
                if version == 2:
                    points = [point + [0] for point in points]
                self.path.write_text(json.dumps({"version": version, "key": KEY,
                                                 "points": points + [["invalid"]]}))
                restored = bridge.QuotaForecast(self.path)
                self.assertEqual(restored.points, [])
                self.assertIsNone(restored.key)

    def test_missing_entire_cycles_are_not_zero_usage(self):
        self.observe(0, 20)
        self.observe(DAY, 30)
        self.assertEqual(self.observe(13 * DAY, 20, 14 * DAY), {})

    def test_same_cycle_sleep_gap_preserves_known_total(self):
        self.observe(0, 10)
        self.assertEqual(self.observe(DAY, 20)[LONG], 80)

    def test_long_gap_is_not_interpolated_across_short_window_boundary(self):
        self.observe(0, 10)
        fields = self.observe(3 * DAY, 40)
        self.assertNotIn(SHORT, fields)
        self.assertEqual(fields[LONG], 80)

    def test_clock_rollback_preserves_history(self):
        self.observe(DAY, 10)
        self.observe(2 * DAY, 20)
        self.assertEqual(self.observe(DAY, 10), {})
        self.assertEqual(len(self.history.points), 2)

    def test_expired_future_and_stale_samples_are_ignored(self):
        self.observe(0, 10)
        s = snapshot(DAY, 20)
        for now in (T + DAY - 1, T + DAY + 900, T + 7 * DAY):
            self.assertEqual(self.history.packet_fields(s, now, KEY), {})
        s.quota_observed_at = None
        self.assertEqual(self.history.packet_fields(s, T + DAY, KEY), {})
        self.assertEqual(len(self.history.points), 1)

    def test_expiry_is_bounded_by_reset(self):
        self.observe(7 * DAY - 7200, 90)
        fields = self.observe(7 * DAY - 60, 90)
        self.assertEqual(fields[TTL], T + 7 * DAY)

    def test_restart_and_sampling_cadence(self):
        self.observe(0, 10)
        with patch.object(self.history, "save") as save:
            self.observe(5, 10)
            save.assert_not_called()
            self.observe(300, 10)
            save.assert_called_once()
        self.observe(DAY, 20)
        self.history = bridge.QuotaForecast(self.path)
        fields = self.observe(DAY + 5, 20)
        self.assertEqual(fields[LONG], 80)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_auth_or_limit_change_clears_history(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        self.assertEqual(self.observe(2 * DAY, 30, key=["codex", [1, 2, 3]]), {})
        self.assertEqual(len(self.history.points), 1)

    def test_invalid_history_is_discarded(self):
        for contents in ("broken", "null", "[]", '{"version": 1, "points": null}',
                         json.dumps({"version": 1, "points": [[T, float("nan"), T + DAY]]}),
                         json.dumps({"version": 1, "points": [[T, 10, T + DAY], [T - 1, 20, T + DAY]]})):
            self.path.write_text(contents)
            self.assertEqual(bridge.QuotaForecast(self.path).points, [])

    def test_atomic_save_failure_keeps_previous_file(self):
        self.observe(0, 10)
        before = self.path.read_bytes()
        with patch.object(bridge.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.observe(DAY, 20)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_save_retries_before_next_sampling_interval_and_recovers_forecast(self):
        self.observe(0, 10)
        before = self.path.read_bytes()
        args = argparse.Namespace(snapshot_cache_path=self.path, codex_home=self.path.parent,
                                  verbose=False, _quota_forecast=self.history)
        with patch.object(bridge.os, "replace", side_effect=OSError), \
             patch.object(bridge.time, "time", return_value=T + DAY):
            fields = bridge.forecast_packet_fields(args, snapshot(DAY, 20))
        self.assertEqual(fields, {"secondary_forecast_status": "unavailable"})
        self.assertEqual(self.path.read_bytes(), before)

        with patch.object(bridge.os, "replace", wraps=bridge.os.replace) as replace, \
             patch.object(bridge.time, "time", return_value=T + DAY + 7):
            fields = bridge.forecast_packet_fields(args, snapshot(DAY + 7, 20))
        replace.assert_called_once()
        self.assertEqual(fields["secondary_forecast_status"], "ready")
        self.assertEqual((fields[SHORT], fields[LONG]), (80, 80))
        self.assertEqual(bridge.QuotaForecast(self.path).points, self.history.points)

    def test_auth_refresh_does_not_clear_history_even_after_restart(self):
        args = argparse.Namespace(snapshot_cache_path=self.path, codex_home=self.path.parent,
                                  verbose=False)
        auth = self.path.parent / "auth.json"
        auth.write_text("{}")
        for at, percent in ((0, 10), (DAY, 20)):
            with patch.object(bridge.time, "time", return_value=T + at):
                fields = bridge.forecast_packet_fields(args, snapshot(at, percent))
        self.assertEqual(fields[LONG], 80)
        auth.unlink()
        auth.write_text('{"refreshed":true}')
        del args._quota_forecast
        with patch.object(bridge.time, "time", return_value=T + DAY + 5):
            self.assertEqual(bridge.forecast_packet_fields(args, snapshot(DAY + 5, 20))[LONG], 80)

    def test_matching_legacy_identity_is_migrated_without_history_loss(self):
        auth = self.path.parent / "auth.json"
        auth.write_text("{}")
        st = auth.stat()
        legacy = ["codex", [st.st_ino, st.st_mtime_ns, st.st_size]]
        self.observe(0, 10, key=legacy)
        self.observe(DAY, 20, key=legacy)
        args = argparse.Namespace(snapshot_cache_path=self.path, codex_home=self.path.parent,
                                  verbose=False)
        with patch.object(bridge.time, "time", return_value=T + DAY + 5):
            self.assertEqual(bridge.forecast_packet_fields(args, snapshot(DAY + 5, 20))[LONG], 80)
        self.assertEqual(bridge.QuotaForecast(self.path).key, KEY)

    def test_unknown_account_hides_ticks_without_erasing_history(self):
        self.observe(0, 10)
        self.observe(DAY, 20)
        before = self.path.read_bytes()
        args = argparse.Namespace(snapshot_cache_path=self.path, codex_home=self.path.parent,
                                  verbose=False)
        current = snapshot(DAY + 5, 20)
        current.quota_account = None
        self.assertEqual(bridge.forecast_packet_fields(args, current),
                         {"secondary_forecast_status": "unavailable"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_account_identity_is_stable_private_and_plan_scoped(self):
        def identity(email="user@example.test", plan="pro"):
            return bridge.forecast_account_identity({"account": {
                "type": "chatgpt", "email": email, "planType": plan}})
        key = identity()
        self.assertEqual(key, identity(" USER@example.test "))
        self.assertNotIn("user@example.test", key)
        self.assertNotEqual(key, identity(plan="plus"))
        self.assertNotEqual(key, identity("other@example.test"))
        self.assertIsNone(identity(plan="unknown"))
        self.assertIsNone(identity(email=None))
        self.assertIsNone(bridge.forecast_account_identity(None))

    def test_appserver_reads_batched_responses_in_both_orders(self):
        fake = self.path.parent / "codex"
        quota = {"id": "codex-usage-rate-limits", "result": {"rateLimits": {
            "limitId": "codex", "primary": {"usedPercent": 20,
            "windowDurationMins": 10080, "resetsAt": T + 7 * DAY}}}}
        account = {"id": "codex-usage-account", "result": {"account": {
            "type": "chatgpt", "email": "test@example.test", "planType": "pro"}}}
        error = {"id": "codex-usage-account", "error": {"message": "unavailable"}}
        args = argparse.Namespace(no_appserver_usage=False, verbose=False,
                                  codex_home=self.path.parent, limit_id="codex", appserver_timeout=2)
        for responses in ([account, quota], [quota, None, [], account], [error, quota]):
            fake.write_text("#!" + sys.executable + "\nimport json, sys\n"
                            "requests = [json.loads(sys.stdin.readline()) for _ in range(3)]\n"
                            "assert requests[1]['params'] == {'refreshToken': False}\n"
                            "sys.stdout.write(" + repr("\n".join(json.dumps(r) for r in responses) + "\n") + ")\n"
                            "sys.stdout.flush()\nsys.stdin.read()\n")
            fake.chmod(0o700)
            real_read = os.read
            chunk_size = 7 if responses[0] == quota else 65536
            with patch.object(bridge, "codex_cli_path", return_value=fake), \
                 patch.object(bridge.os, "read", side_effect=lambda fd, n: real_read(fd, min(n, chunk_size))), \
                 patch.object(bridge.time, "time", return_value=T):
                current = bridge.read_app_server_usage(args, None)
            self.assertIsNotNone(current)
            self.assertEqual(current.secondary, 20)
            self.assertEqual(current.quota_observed_at, T)
            self.assertEqual(current.quota_account,
                             None if error in responses else bridge.forecast_account_identity(account["result"]))

    def test_storage_failure_does_not_break_usage_delivery(self):
        args = argparse.Namespace(snapshot_cache_path=self.path, codex_home=self.path.parent,
                                  verbose=False)
        with patch.object(bridge.QuotaForecast, "save", side_effect=OSError), patch.object(bridge.time, "time", return_value=T):
            self.assertEqual(bridge.forecast_packet_fields(args, snapshot(0, 10)),
                             {"secondary_forecast_status": "unavailable"})

    def test_live_observation_uses_wall_time_not_activity_time(self):
        result = {"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 10, "windowDurationMins": 10080, "resetsAt": T + 7 * DAY}}}
        with patch.object(bridge.time, "time", return_value=T):
            s = bridge.app_server_usage_snapshot_from_result(result, "codex", snapshot(0, 10))
        self.assertEqual(s.event_ts, 1)
        self.assertEqual(s.quota_observed_at, T)
        self.assertEqual(bridge.snapshot_to_cache(s)["quota_observed_at"], T)

    def test_invalid_live_percent_cannot_enter_history(self):
        for percent in (None, -1, 101, float("inf"), float("nan"), True, "10"):
            result = {"rateLimits": {"primary": {"usedPercent": percent,
                      "windowDurationMins": 10080, "resetsAt": T + 7 * DAY}}}
            with patch.object(bridge.time, "time", return_value=T):
                s = bridge.app_server_usage_snapshot_from_result(result, "codex", None)
            self.assertIsNone(s)

    def test_fallback_cache_never_becomes_fresh_observation(self):
        args = argparse.Namespace(codex_home=self.path.parent, thread_id=None,
                                  thread_scan_limit=12, rollout=None, limit_id="codex",
                                  snapshot_cache_path=self.path)
        args._last_usage = snapshot(0, 10)
        args._last_usage.auth_scope = bridge.auth_scope(args)
        with patch.object(bridge, "latest_rollout_paths", return_value=[]), \
             patch.object(bridge, "read_app_server_usage", return_value=None), \
             patch.object(bridge.time, "time", return_value=T):
            cached = bridge.read_usage(args)
            self.assertEqual(cached.quota_observed_at, T)
            self.assertFalse(cached.quota_live)

    def test_send_update_keeps_existing_packet_and_adds_optional_fields(self):
        args = argparse.Namespace(state="idle", dry_run=False, verbose=False,
                                  snapshot_cache_path=self.path, codex_home=self.path.parent)
        self.observe(0, 10)
        args._quota_forecast = self.history
        ble = argparse.Namespace(write_json=AsyncMock())
        with patch.object(bridge, "read_usage", return_value=snapshot(DAY, 20)), \
             patch.object(bridge.time, "time", return_value=T + DAY):
            asyncio.run(bridge.send_usage_update(args, bridge.ActivityTracker(), ble))
        packet = ble.write_json.call_args.args[0]
        self.assertEqual(packet["secondary"], 20)
        self.assertEqual(packet["tokens"], 999)
        self.assertEqual(packet[SHORT], 80)
        self.assertEqual(packet[LONG], 80)
        self.assertEqual(packet[REMAINING_SHORT], 2000)
        self.assertEqual(packet[REMAINING_LONG], 2000)
        self.assertEqual(packet["quota_status"], "fresh")
        self.assertEqual(packet["secondary_forecast_status"], "ready")
        self.assertLess(len(json.dumps(packet).encode()), 768)


if __name__ == "__main__":
    unittest.main()
