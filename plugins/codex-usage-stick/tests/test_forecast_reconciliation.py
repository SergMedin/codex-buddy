"""Forecast regressions with consumption derived from the observed scenario."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_quota_forecast import DAY, KEY, LONG, SHORT, T, TTL, bridge, snapshot


class ForecastReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "quota_history.json"
        self.history = bridge.QuotaForecast(self.path)

    def observe(self, at, used, deadline, *, live=True, key=KEY, now=None):
        return self.history.packet_fields(snapshot(at, used, deadline),
                                          T + at if now is None else now,
                                          key, record=live)

    def restart(self):
        self.history = bridge.QuotaForecast(self.path)

    def fresh(self):
        self.path.unlink(missing_ok=True)
        self.restart()

    def test_known_window_replays_never_create_consumption_even_after_every_restart(self):
        for jitter in (-5, 0, 5):
            for restart in (False, True):
                with self.subTest(jitter=jitter, restart=restart):
                    self.fresh()
                    self.observe(0, 40, 6 * DAY)
                    self.observe(DAY, 40, 6 * DAY)
                    fields = self.observe(DAY + 7, 0, 8 * DAY)
                    # The only accepted within-window change so far is zero.
                    self.assertEqual((fields[SHORT], fields[LONG]), (0, 0))
                    if restart:
                        self.restart()
                    # Returning to the remembered window is a baseline. Its
                    # old high-water value is not another 40 points consumed.
                    fields = self.observe(DAY + 14, 40, 6 * DAY + jitter)
                    self.assertEqual((fields[SHORT], fields[LONG]), (40, 40))
                    if restart:
                        self.restart()
                    for at in (DAY + 21, DAY + 22):
                        before = self.path.read_bytes()
                        self.assertEqual(self.observe(at, 0, 6 * DAY + jitter), {})
                        self.assertEqual(self.path.read_bytes(), before)
                        if restart:
                            self.restart()
                    fields = self.observe(DAY + 28, 0, 8 * DAY)
                    self.assertEqual((fields[SHORT], fields[LONG]), (0, 0))
                    if restart:
                        self.restart()
                    fields = self.observe(2 * DAY, 2, 8 * DAY)
                    # Two real points consumed in two days, six days left:
                    # current 2 + 1/day * 6 = 8, with short reset gaps omitted.
                    self.assertEqual((fields[SHORT], fields[LONG]), (8, 8))

    def test_unseen_earlier_manual_reset_and_old_window_return_keep_separate_counters(self):
        self.observe(0, 10, 7 * DAY)
        self.observe(DAY, 20, 7 * DAY)
        fields = self.observe(DAY + 7, 0, 6 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (50, 50))
        self.restart()
        fields = self.observe(DAY + 14, 20, 7 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (80, 80))
        fields = self.observe(DAY + 21, 0, 6 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (50, 50))

    def test_isolated_false_zero_does_not_permanently_hide_the_returning_window(self):
        self.observe(0, 38, 6 * DAY)
        self.observe(DAY, 40, 6 * DAY)
        self.observe(DAY + 7, 0, 8 * DAY)
        self.restart()
        fields = self.observe(DAY + 14, 40, 6 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (50, 50))
        self.restart()
        fields = self.observe(2 * DAY, 40, 6 * DAY)
        # Two consumed points and almost two days of observed time now:
        # current40 +1/day *four days remaining =44, not an outage oroverflow.
        self.assertEqual((fields[SHORT], fields[LONG]), (44, 44))

    def test_repeated_switching_between_remembered_windows_never_recounts_the_counter(self):
        self.observe(0, 40, 6 * DAY)
        self.observe(DAY, 40, 6 * DAY)
        for step in range(1, 13):
            used, deadline = (0, 8 * DAY) if step % 2 else (40, 6 * DAY)
            fields = self.observe(DAY + step * 7, used, deadline)
            self.assertEqual((fields[SHORT], fields[LONG]), (used, used))
            self.restart()

    def test_returning_counter_jump_is_unknown_but_following_monotone_delta_is_measured(self):
        self.observe(0, 40, 6 * DAY)
        self.observe(DAY, 40, 6 * DAY)
        self.observe(DAY + 7, 0, 8 * DAY)
        fields = self.observe(DAY + 14, 45, 6 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (45, 45))
        self.restart()
        fields = self.observe(2 * DAY, 46, 6 * DAY)
        # The crossing cannot place the 40->45 change in observed time.
        # Only the subsequent45->46 is measured:1 over almost2 observed days.
        self.assertEqual((fields[SHORT], fields[LONG]), (48, 48))

    def test_overlapping_rounding_ranges_are_rejected_without_phantom_38(self):
        self.observe(0, 40, 6 * DAY)
        self.observe(10, 0, 6 * DAY + 6)
        self.observe(20, 1, 6 * DAY + 6)
        before = self.path.read_bytes()
        self.assertEqual(self.observe(30, 2, 6 * DAY + 5), {})
        self.assertEqual(self.history.status, "unavailable")
        self.assertEqual(self.path.read_bytes(), before)
        self.restart()
        self.assertEqual(self.observe(40, 40, 6 * DAY + 1), {})
        self.assertEqual(self.path.read_bytes(), before)
        # The first increment belongs to an unambiguous new window. Neither
        # later observation identifies a window uniquely, so neither trains.
        self.assertEqual(self.history.history.measure(T, T + 40), (1, 10))

    def test_rounding_drift_does_not_move_canonical_identity_arbitrarily(self):
        self.observe(0, 40, 6 * DAY)
        self.observe(10, 41, 6 * DAY + 4)
        self.observe(20, 42, 6 * DAY + 8)
        self.observe(30, 43, 6 * DAY + 12)
        self.restart()
        self.observe(40, 44, 6 * DAY)
        # Changes exceeding the original rounding range are new baselines.
        # Only40->41 and42->43 fall in known same-window intervals.
        self.assertEqual(self.history.history.measure(T, T + 40), (2, 20))

    def test_rounded_zero_can_lag_first_use_anchor_without_permanent_rejection(self):
        self.observe(0, 10, 6 * DAY)
        self.observe(DAY, 20, 6 * DAY)
        self.observe(DAY + 7, 0, DAY + 7 + 7 * DAY)
        self.observe(DAY + 3600, 0, DAY + 3600 + 7 * DAY)
        # A rounded 0% does not prove there was no use. The server may reveal
        # an anchor earlier than the latest zero observation when 1% arrives.
        anchored = DAY + 1800 + 7 * DAY
        fields = self.observe(DAY + 7200, 1, anchored)
        self.assertIn(LONG, fields)
        self.assertLessEqual(fields[LONG], 71)
        self.restart()
        fields = self.observe(DAY + 10800, 1, anchored)
        self.assertIn(LONG, fields)
        self.assertLessEqual(fields[LONG], 71)

    def test_account_change_clears_retired_lineage_and_cached_reads_do_not_write(self):
        self.observe(0, 40, 6 * DAY)
        self.observe(DAY, 40, 6 * DAY)
        self.observe(DAY + 7, 0, 8 * DAY)
        other = ["codex", "account-v1:another-account"]
        self.observe(DAY + 14, 40, 6 * DAY, key=other)
        fields = self.observe(DAY + 3614, 40, 6 * DAY, key=other)
        self.assertEqual((fields[SHORT], fields[LONG]), (40, 40))
        before = self.path.read_bytes()
        cached = self.observe(DAY + 3614, 40, 6 * DAY, live=False, key=other,
                              now=T + DAY + 3614 + 800)
        self.assertEqual(cached, fields)
        self.assertEqual(cached[TTL], fields[TTL])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.observe(DAY + 3614, 40, 6 * DAY, live=False, key=KEY), {})
        self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_migration_removes_replayed_40_but_preserves_real_consumption(self):
        raw = [[T, 38, T + 6 * DAY], [T + DAY, 40, T + 6 * DAY],
               [T + DAY + 7, 0, T + 8 * DAY],
               [T + DAY + 14, 40, T + 6 * DAY],
               [T + DAY + 21, 0, T + 8 * DAY],
               [T + 2 * DAY, 2, T + 8 * DAY]]
        for version in (1, 2):
            with self.subTest(version=version):
                points = raw if version == 1 else [p + [c] for p, c in zip(raw, (0, 0, 1, 1, 2, 2))]
                self.path.write_text(json.dumps({"version": version, "key": KEY, "points": points}))
                self.restart()
                before = self.path.read_bytes()
                fields = self.observe(2 * DAY, 2, 8 * DAY, live=False)
                self.assertEqual((fields[SHORT], fields[LONG]), (14, 14))
                self.assertEqual(self.path.read_bytes(), before)
                fields = self.observe(2 * DAY + 7, 2, 8 * DAY)
                # 2 pp before reset + 2 pp after reset, over two days:
                # current 2 + 2/day * six days remaining = 14.
                self.assertEqual(fields[LONG], 14)
                self.assertNotIn(SHORT, fields)
                self.assertEqual(json.loads(self.path.read_text())["version"], 3)
                self.restart()
                self.assertEqual(self.observe(2 * DAY + 14, 2, 8 * DAY)[LONG], 14)

    def test_full_14_days_survive_hourly_resets_and_minute_observations(self):
        # Three weeks with rates of 2, 4, then 8 pp/day. Reset each hour so
        # raw reset endpoints exceed the old point-count budget. The last
        # fourteen days must average 6 pp/day; the last two average 8 pp/day.
        with patch.object(self.history, "save"):
            for at in range(0, 21 * DAY + 1, 60):
                week = min(at // (7 * DAY), 2)
                rate = (2, 4, 8)[week]
                hour_start = at // 3600 * 3600
                used = (at - hour_start) / DAY * rate
                fields = self.observe(at, used, hour_start + 7 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (56, 42))
        self.history.save()
        self.assertLess(self.path.stat().st_size, 1024 * 1024)
        self.restart()
        fields = self.observe(21 * DAY + 7, 0, 28 * DAY)
        self.assertEqual((fields[SHORT], fields[LONG]), (56, 42))

    def test_unknown_long_gap_crossing_short_horizon_remains_unavailable_after_restart(self):
        self.observe(0, 10, 7 * DAY)
        fields = self.observe(3 * DAY, 40, 7 * DAY)
        self.assertNotIn(SHORT, fields)
        self.assertEqual(fields[LONG], 80)
        self.restart()
        fields = self.observe(3 * DAY + 7, 40, 7 * DAY)
        self.assertNotIn(SHORT, fields)
        self.assertEqual(fields[LONG], 80)

    def test_partial_bucket_with_unknown_switch_gaps_does_not_invent_coverage(self):
        self.observe(0, 40, 6 * DAY)
        self.observe(100, 40, 6 * DAY)
        self.observe(120, 0, 7 * DAY)
        self.observe(140, 40, 6 * DAY)
        self.observe(300, 41, 6 * DAY)
        # Exactly100 known idle seconds, two unknown20-second crossings,
        # then160 known seconds consuming1pp. Compaction knows the totals,
        # but not where the missing40 seconds fall inside a partial query.
        self.assertEqual(self.history.history.measure(T, T + 300), (1, 260))
        self.assertEqual(self.history.history.measure(T + 150, T + 300), (0, 0))
        before = self.path.read_bytes()
        self.observe(300, 41, 6 * DAY, live=False, now=T + 300 + 800)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.history.history.measure(T, T + 300), (1, 260))
        self.restart()
        self.assertEqual(self.history.history.measure(T, T + 300), (1, 260))
        self.assertEqual(self.history.history.measure(T + 150, T + 300), (0, 0))

    def test_current_schema_rejects_corruption_instead_of_partially_training(self):
        self.observe(0, 10, 7 * DAY)
        self.observe(3600, 20, 7 * DAY)
        valid = json.loads(self.path.read_text())
        invalid_documents = []

        def corrupted():
            return json.loads(json.dumps(valid))

        item = corrupted()
        item["latest"] = None
        invalid_documents.append(item)
        item = corrupted()
        item["latest"][3] = 999999
        invalid_documents.append(item)
        item = corrupted()
        item["windows"][0][2] = -1
        invalid_documents.append(item)
        item = corrupted()
        item["history"]["buckets"][0][1] = -1
        invalid_documents.append(item)
        item = corrupted()
        item["history"]["buckets"][0][2] = 301
        invalid_documents.append(item)
        item = corrupted()
        item["history"]["buckets"].append(["invalid"])
        invalid_documents.append(item)

        for item in invalid_documents:
            with self.subTest(document=item):
                self.path.write_text(json.dumps(item))
                self.restart()
                self.assertIsNone(self.history.latest)
                self.assertIsNone(self.history.key)
                self.assertEqual(self.history.history.buckets, {})
                self.assertEqual(self.observe(3607, 20, 7 * DAY), {})
                self.assertEqual(self.history.status, "learning")


if __name__ == "__main__":
    unittest.main()
