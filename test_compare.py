"""Tests for compare.py. Standard library only: python3 -m unittest test_compare -v"""
import base64
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

import compare

UTC = dt.timezone.utc
BOUND = {"version": 1, "bound_min": 60, "quiet_utc": [["05:00", "17:00"]]}


def t(s):
    return compare.parse(s)


class OutsideQuiet(unittest.TestCase):
    def test_all_outside(self):
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T18:00:00Z"), t("2026-10-22T19:30:00Z"), BOUND["quiet_utc"]), 90)

    def test_all_inside(self):
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T06:00:00Z"), t("2026-10-22T16:00:00Z"), BOUND["quiet_utc"]), 0)

    def test_straddles_start_of_quiet(self):
        # 04:30-10:00: only 04:30-05:00 is outside
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T04:30:00Z"), t("2026-10-22T10:00:00Z"), BOUND["quiet_utc"]), 30)

    def test_straddles_end_of_quiet(self):
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T16:30:00Z"), t("2026-10-22T17:45:00Z"), BOUND["quiet_utc"]), 45)

    def test_two_days(self):
        # 2026-10-22 04:00 to 2026-10-23 18:00 = 38 h; quiet 05-17 twice = 24 h; outside 14 h
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T04:00:00Z"), t("2026-10-23T18:00:00Z"), BOUND["quiet_utc"]), 14 * 60)

    def test_window_past_midnight(self):
        q = [["22:00", "02:00"]]
        # 21:00 to 03:00 = 6 h, quiet 22-02 = 4 h
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T21:00:00Z"), t("2026-10-23T03:00:00Z"), q), 120)
        # start inside a window that began the day before
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-23T01:00:00Z"), t("2026-10-23T02:30:00Z"), q), 30)

    def test_no_quiet(self):
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T04:00:00Z"), t("2026-10-22T05:00:00Z"), []), 60)

    def test_empty_or_reversed(self):
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T05:00:00Z"), t("2026-10-22T05:00:00Z"), BOUND["quiet_utc"]), 0)
        self.assertEqual(compare.outside_quiet_minutes(t("2026-10-22T06:00:00Z"), t("2026-10-22T05:00:00Z"), BOUND["quiet_utc"]), 0)


class Verdict(unittest.TestCase):
    def v(self, beat, now):
        return compare.verdict(t(beat) if beat else None, t(now), BOUND)

    def test_running(self):
        self.assertEqual(self.v("2026-10-22T18:00:00Z", "2026-10-22T18:59:00Z")["verdict"], "running")

    def test_at_bound_is_running(self):
        self.assertEqual(self.v("2026-10-22T18:00:00Z", "2026-10-22T19:00:00Z")["verdict"], "running")

    def test_stopped_in_daytime(self):
        r = self.v("2026-10-22T18:00:00Z", "2026-10-22T19:01:00Z")
        self.assertEqual((r["verdict"], r["silence_min"], r["silence_outside_quiet_min"]), ("stopped", 61.0, 61.0))

    def test_quiet_overnight(self):
        # last beat 04:50Z (23:50 Chicago, daylight time), read at 12:00Z: 430 min silent, 10 outside quiet
        r = self.v("2026-10-22T04:50:00Z", "2026-10-22T12:00:00Z")
        self.assertEqual((r["verdict"], r["silence_min"], r["silence_outside_quiet_min"]), ("quiet", 430.0, 10.0))

    def test_quiet_ends_then_stopped(self):
        self.assertEqual(self.v("2026-10-22T04:50:00Z", "2026-10-22T17:49:00Z")["verdict"], "quiet")
        self.assertEqual(self.v("2026-10-22T04:50:00Z", "2026-10-22T17:50:00Z")["verdict"], "stopped")

    def test_beat_inside_quiet_then_daytime(self):
        # a beat at 16:30Z, then silence: 30 min of quiet, then 60 outside -> stopped at 18:00Z
        self.assertEqual(self.v("2026-10-22T16:30:00Z", "2026-10-22T17:59:00Z")["verdict"], "quiet")
        self.assertEqual(self.v("2026-10-22T16:30:00Z", "2026-10-22T18:00:00Z")["verdict"], "stopped")

    def test_no_beat(self):
        self.assertEqual(self.v(None, "2026-10-22T18:00:00Z")["verdict"], "no-beat")


def fake_fetch(beat_time="2026-10-22T18:00:00Z", kernel_time="2026-10-22T18:00:04Z", missing=False, broken=False, no_repo=False):
    def fetch(path, token, repo):
        if broken:
            raise OSError("network unreachable")
        if no_repo or (missing and path == "/commits/beat"):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        if path == "":
            return {"full_name": repo}
        if path == "/commits/beat":
            return {"sha": "abc123", "commit": {"committer": {"date": beat_time}}}
        if path.startswith("/contents/beat.json?ref=abc123"):
            body = {"seq": 41, "kernel_time": kernel_time, "last_poll": "2026-10-22T17:59:10Z"}
            return {"content": base64.b64encode(json.dumps(body).encode()).decode()}
        raise AssertionError(path)
    return fetch


class Run(unittest.TestCase):
    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "bound.json").write_text(json.dumps(BOUND), encoding="utf-8")
        self.env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "schedule", "GITHUB_SHA": "def456"}

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_receipt_names_the_hash_of_the_bound_it_read(self):
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
        want = hashlib.sha256((self.root / "bound.json").read_bytes()).hexdigest()
        self.assertEqual(r["bound_sha256"], want)
        (self.root / "bound.json").write_text(json.dumps(dict(BOUND, version="changed")), encoding="utf-8")
        r2 = compare.run(self.root, self.env, now=t("2026-10-22T18:31:00Z"), fetch=fake_fetch())
        self.assertNotEqual(r2["bound_sha256"], want)

    def test_receipt_running_and_written(self):
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
        self.assertEqual(r["verdict"], "running")
        self.assertEqual(r["beat"]["seq"], 41)
        self.assertEqual(r["beat"]["kernel_minus_github_s"], 4)
        self.assertEqual((r["run_id"], r["event"], r["head_sha"]), ("9", "schedule", "def456"))
        self.assertIsNone(r["previous_receipt_at"])
        p = compare.write(self.root, r)
        self.assertEqual(p.name, "2026-10.jsonl")
        # the second receipt names the first and the gap between them
        r2 = compare.run(self.root, self.env, now=t("2026-10-22T23:45:00Z"), fetch=fake_fetch())
        self.assertEqual(r2["previous_receipt_at"], "2026-10-22T18:30:00Z")
        self.assertEqual(r2["since_previous_receipt_min"], 315.0)
        self.assertEqual(r2["verdict"], "stopped")
        compare.write(self.root, r2)
        lines = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(x)["compared_at"] for x in lines], ["2026-10-22T18:30:00Z", "2026-10-22T23:45:00Z"])
        self.assertEqual(json.loads((self.root / "latest.json").read_text(encoding="utf-8"))["compared_at"], "2026-10-22T23:45:00Z")

    def test_outage_between_receipts_shows_as_missing_beats(self):
        # receipt 1 sees seq 41 at 18:00; by receipt 2 the beat at 23:00 is seq 49: 300 min is 20 periods, 8 attempts,
        # so about 12 beats (3 h) were never attempted, though both receipts read "running"
        def fetch_at(seq, when):
            def fetch(path, token, repo):
                if path == "/commits/beat":
                    return {"sha": "abc123", "commit": {"committer": {"date": when}}}
                body = {"seq": seq, "kernel_time": when, "last_poll": when}
                return {"content": base64.b64encode(json.dumps(body).encode()).decode()}
            return fetch
        r1 = compare.run(self.root, self.env, now=t("2026-10-22T18:10:00Z"), fetch=fetch_at(41, "2026-10-22T18:00:00Z"))
        self.assertIsNone(r1["seq_advance"])
        compare.write(self.root, r1)
        r2 = compare.run(self.root, self.env, now=t("2026-10-22T23:10:00Z"), fetch=fetch_at(49, "2026-10-22T23:00:00Z"))
        self.assertEqual((r2["verdict"], r2["seq_advance"], r2["beat_minutes_between"], r2["beats_missing_est"]), ("running", 8, 300.0, 12))

    def test_missing_estimate_absorbs_jitter_within_a_slot(self):
        # beats land up to a tick (3 min) into their slot: 18:02:40 -> 23:00:10 is 297.5 min, still 20 slots
        b = compare.between_beats({"seq": 41, "github_time": "2026-10-22T18:02:40Z"}, {"seq": 61, "github_time": "2026-10-22T23:00:10Z"}, 15)
        self.assertEqual(b["beats_missing_est"], 0)

    def _state_fetch(self, state, state_at):
        def fetch(path, token, repo):
            if path == "/commits/beat":
                return {"sha": "abc123", "commit": {"committer": {"date": "2026-10-22T18:00:00Z"}}}
            body = {"seq": 7, "kernel_time": "2026-10-22T18:00:01Z", "last_poll": "2026-10-22T11:00:00Z",
                    "kernel_state": state, "kernel_state_at": state_at}
            return {"content": base64.b64encode(json.dumps(body).encode()).decode()}
        return fetch

    def _bound(self):
        bound = dict(BOUND, halted_states=["halted", "paused"], state_stale_min=15, session_max_min=330)
        (self.root / "bound.json").write_text(json.dumps(bound), encoding="utf-8")

    def test_fresh_beat_from_a_halted_kernel(self):
        self._bound()
        fetch = self._state_fetch("halted", "2026-10-22T11:00:00Z")
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:10:00Z"), fetch=fetch)
        self.assertEqual((r["verdict"], r["halted_reason"]), ("halted", "kernel_state halted"))
        # a stale beat is judged by its silence whatever it last said
        r = compare.run(self.root, self.env, now=t("2026-10-22T19:30:00Z"), fetch=fetch)
        self.assertEqual((r["verdict"], r["halted_reason"]), ("stopped", None))

    def test_scheduler_not_firing_behind_a_live_beat(self):
        # the beat comes from another task; the scheduler last wrote "idle" 40 min before the beat (18:00:01)
        self._bound()
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:10:00Z"), fetch=self._state_fetch("idle", "2026-10-22T17:20:00Z"))
        self.assertEqual(r["verdict"], "halted")
        self.assertIn("not refreshed for 40 min", r["halted_reason"])

    def test_long_session_is_running_until_its_cap(self):
        self._bound()
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:10:00Z"), fetch=self._state_fetch("session", "2026-10-22T14:00:00Z"))
        self.assertEqual((r["verdict"], r["halted_reason"]), ("running", None))
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:10:00Z"), fetch=self._state_fetch("session", "2026-10-22T12:00:00Z"))
        self.assertEqual(r["verdict"], "halted")

    def test_backoff_and_fresh_idle_read_running(self):
        self._bound()
        for state in ("backoff", "idle"):
            r = compare.run(self.root, self.env, now=t("2026-10-22T18:10:00Z"), fetch=self._state_fetch(state, "2026-10-22T17:58:00Z"))
            self.assertEqual((r["verdict"], r["beat"]["kernel_state"]), ("running", state))

    def test_counter_reset_gives_no_estimate(self):
        b = compare.between_beats({"seq": 50, "github_time": "2026-10-22T18:00:00Z"}, {"seq": 3, "github_time": "2026-10-22T19:00:00Z"}, 15)
        self.assertEqual((b["seq_advance"], b["beats_missing_est"]), (-47, None))

    def test_month_rollover_new_file(self):
        r = compare.run(self.root, self.env, now=t("2026-11-01T00:10:00Z"), fetch=fake_fetch(beat_time="2026-10-31T23:50:00Z", kernel_time="2026-10-31T23:50:01Z"))
        self.assertEqual(compare.write(self.root, r).name, "2026-11.jsonl")

    def test_no_beat_branch(self):
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch(missing=True))
        self.assertEqual((r["verdict"], r["beat"], r["error"]), ("no-beat", None, None))

    def test_missing_repository_is_an_error_not_no_beat(self):
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch(no_repo=True))
        self.assertEqual(r["verdict"], "error")
        self.assertIn("404", r["error"])

    def test_unreadable_beat_still_writes_a_receipt(self):
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch(broken=True))
        self.assertEqual(r["verdict"], "error")
        self.assertIn("network unreachable", r["error"])
        compare.write(self.root, r)
        self.assertTrue((self.root / "latest.json").exists())

    def test_a_beat_in_another_form_is_an_error_receipt(self):
        compare.write(self.root, compare.run(self.root, self.env, now=t("2026-10-22T18:10:00Z"), fetch=fake_fetch()))

        def body_fetch(body):
            def fetch(path, token, repo):
                if path == "/commits/beat":
                    return {"sha": "abc123", "commit": {"committer": {"date": "2026-10-22T18:00:00Z"}}}
                return {"content": base64.b64encode(json.dumps(body).encode()).decode()}
            return fetch
        for body, says in (([1], "not a JSON object"), ({"seq": 41, "kernel_time": "half past six"}, "kernel_time"),
                           ({"seq": "41", "kernel_time": "2026-10-22T18:00:04Z"}, "seq is not an integer"),
                           ({"seq": 41, "kernel_state": "idle", "kernel_state_at": 5}, "kernel_state_at")):
            r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=body_fetch(body))
            self.assertEqual((r["verdict"], r["beat"]), ("error", None), body)
            self.assertIn(says, r["error"])
            self.assertEqual(r["previous_receipt_at"], "2026-10-22T18:10:00Z")  # the previous receipt is still read

    def test_a_latest_json_that_is_not_a_receipt_reads_as_a_first_run(self):
        for text in ("[1]", '{"compared_at": "noon"}', '{"compared_at": "2026-10-22T18:10:00Z", "beat": {"seq": "x"}}'):
            (self.root / "latest.json").write_text(text, encoding="utf-8")
            r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
            self.assertEqual((r["verdict"], r["previous_receipt_at"], r["seq_advance"], r["error"]), ("running", None, None, None), text)

    def test_any_other_failure_still_leaves_a_receipt(self):
        real = compare.between_beats
        with mock.patch.object(compare, "between_beats", side_effect=[RuntimeError("unforeseen"), real(None, None, 15)]):
            r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
        self.assertEqual((r["verdict"], r["beat"], r["previous_receipt_at"]), ("error", None, None))
        self.assertEqual(r["error"], "could not build the receipt: RuntimeError: unforeseen")
        self.assertEqual(r["scheduled_gaps_n"], 0)  # still counted from receipts/
        compare.write(self.root, r)
        self.assertEqual(json.loads((self.root / "latest.json").read_text(encoding="utf-8"))["verdict"], "error")
        # a bound.json value the comparison cannot use lands here too, in a fork's bound for instance
        (self.root / "bound.json").write_text(json.dumps(dict(BOUND, quiet_utc=[["5am", "17:00"]])), encoding="utf-8")
        r = compare.run(self.root, self.env, now=t("2026-10-22T19:30:00Z"), fetch=fake_fetch())
        self.assertEqual(r["verdict"], "error")
        self.assertIn("could not build the receipt: ValueError", r["error"])
        # the README's one exception: a bound.json with no usable bound_min gives no receipt, only a failed run
        (self.root / "bound.json").write_text(json.dumps(dict(BOUND, bound_min="sixty")), encoding="utf-8")
        with self.assertRaises(ValueError):
            compare.run(self.root, self.env, now=t("2026-10-22T19:30:00Z"), fetch=fake_fetch())

    def test_verdict_uses_github_time_not_kernel_time(self):
        # kernel clock 2 h ahead of GitHub's: silence is still counted from GitHub's stamp
        r = compare.run(self.root, self.env, now=t("2026-10-22T19:30:00Z"), fetch=fake_fetch(kernel_time="2026-10-22T20:00:00Z"))
        self.assertEqual((r["verdict"], r["silence_min"], r["beat"]["kernel_minus_github_s"]), ("stopped", 90.0, 7200))

    def test_no_repository_is_an_error_not_no_beat(self):
        env = dict(self.env)
        del env["GITHUB_REPOSITORY"]
        r = compare.run(self.root, env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
        self.assertEqual(r["verdict"], "error")
        self.assertIn("--repo", r["error"])

    def test_shipped_bound_is_well_formed(self):
        # checks the shape, not Ember's values, so a fork can set its own
        b = json.loads((pathlib.Path(compare.__file__).parent / "bound.json").read_text(encoding="utf-8"))
        self.assertGreater(float(b["bound_min"]), 0)
        self.assertGreater(float(b["beat_period_min"]), 0)
        for a, z in b.get("quiet_utc", []):
            compare._hm(a), compare._hm(z)
        s = b.get("supersedes")  # null for a first version; otherwise the version replaced and why
        if s is not None:
            self.assertLess(int(s["version"]), int(b["version"]))
            self.assertTrue(str(s["reason"]).strip())
        self.assertIn(compare.verdict(t("2026-10-22T18:00:00Z"), t("2026-10-22T18:00:00Z"), b)["verdict"], ("running",))


class ScheduledGaps(unittest.TestCase):
    # the stamps and events of this repository's first ten receipts, as served on 2026-10-25
    HISTORY = [("2026-10-23T20:45:39Z", "workflow_dispatch"), ("2026-10-23T22:45:17Z", "workflow_dispatch"),
               ("2026-10-23T23:26:31Z", "schedule"), ("2026-10-24T01:55:21Z", "schedule"),
               ("2026-10-24T07:34:28Z", "schedule"), ("2026-10-24T12:12:23Z", "schedule"),
               ("2026-10-24T16:12:39Z", "schedule"), ("2026-10-24T18:57:31Z", "schedule"),
               ("2026-10-24T21:50:36Z", "schedule"), ("2026-10-25T00:30:02Z", "schedule")]

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "receipts").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root)

    def put(self, rows, name="2026-10.jsonl", extra=""):
        with (self.root / "receipts" / name).open("a", encoding="utf-8") as f:
            for at, ev in rows:
                f.write(json.dumps({"compared_at": at, "event": ev}) + "\n")
            f.write(extra)

    def test_no_history(self):
        self.assertEqual(compare.scheduled_gaps(self.root, t("2026-10-23T20:45:39Z"), "workflow_dispatch"),
                         {"scheduled_gaps_n": 0, "scheduled_gap_max_min": None, "scheduled_gap_max_ended_at": None,
                          "scheduled_gaps_error": None})
        # a first scheduled run has no gap yet either
        self.assertEqual(compare.scheduled_gaps(self.root, t("2026-10-23T23:26:31Z"), "schedule")["scheduled_gaps_n"], 0)

    def test_real_history_leaves_out_runs_started_on_request(self):
        self.put(self.HISTORY)
        g = compare.scheduled_gaps(self.root, t("2026-10-25T01:00:00Z"), "workflow_dispatch")
        # 01:55:21 -> 07:34:28 is 339.1 min, the largest of the 7 gaps between the 8 scheduled receipts
        self.assertEqual(g, {"scheduled_gaps_n": 7, "scheduled_gap_max_min": 339.1, "scheduled_gap_max_ended_at": "2026-10-24T07:34:28Z",
                             "scheduled_gaps_error": None})

    def test_a_scheduled_run_counts_its_own_gap(self):
        self.put(self.HISTORY)
        g = compare.scheduled_gaps(self.root, t("2026-10-25T06:30:02Z"), "schedule")  # 360 min after 00:30:02
        self.assertEqual(g, {"scheduled_gaps_n": 8, "scheduled_gap_max_min": 360.0, "scheduled_gap_max_ended_at": "2026-10-25T06:30:02Z",
                             "scheduled_gaps_error": None})

    def test_across_month_files_and_a_damaged_line(self):
        self.put([("2026-10-31T22:00:00Z", "schedule")], "2026-10.jsonl", extra='{"compared_at": "2026-10-31T23:\n')
        self.put([("2026-11-01T04:00:00Z", "schedule"), ("2026-11-01T05:00:00Z", None)], "2026-11.jsonl")
        g = compare.scheduled_gaps(self.root, t("2026-11-01T06:00:00Z"), "schedule")
        self.assertEqual(g, {"scheduled_gaps_n": 2, "scheduled_gap_max_min": 360.0, "scheduled_gap_max_ended_at": "2026-11-01T04:00:00Z",
                             "scheduled_gaps_error": None})

    def test_a_stored_line_it_cannot_read_nulls_the_fields_and_says_where(self):
        for bad in ('[1, 2]\n', '{"compared_at": "yesterday", "event": "schedule"}\n', '{"event": "schedule"}\n'):
            (self.root / "receipts" / "2026-10.jsonl").write_text("", encoding="utf-8")
            self.put(self.HISTORY[2:4], extra=bad)
            g = compare.scheduled_gaps(self.root, t("2026-10-24T07:34:28Z"), "schedule")
            self.assertEqual((g["scheduled_gaps_n"], g["scheduled_gap_max_min"], g["scheduled_gap_max_ended_at"]), (None, None, None))
            self.assertTrue(g["scheduled_gaps_error"].startswith("receipts/2026-10.jsonl line 3: "), g["scheduled_gaps_error"])
        # a run with such a line still writes its receipt, with the beat read as usual
        (self.root / "bound.json").write_text(json.dumps(BOUND), encoding="utf-8")
        r = compare.run(self.root, {"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "schedule"}, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
        self.assertEqual((r["verdict"], r["error"], r["scheduled_gaps_n"]), ("running", None, None))
        self.assertIn("without compared_at", r["scheduled_gaps_error"])

    def test_a_rerun_is_not_a_scheduled_run(self):
        self.put(self.HISTORY[2:4])  # 23:26:31 and 01:55:21
        with (self.root / "receipts" / "2026-10.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"compared_at": "2026-10-24T05:00:00Z", "event": "schedule", "run_attempt": "2"}) + "\n")
        g = compare.scheduled_gaps(self.root, t("2026-10-24T07:34:28Z"), "schedule", "1")
        self.assertEqual((g["scheduled_gaps_n"], g["scheduled_gap_max_min"]), (2, 339.1))  # the re-run does not split the gap
        self.assertEqual(compare.scheduled_gaps(self.root, t("2026-10-24T09:00:00Z"), "schedule", "2")["scheduled_gaps_n"], 1)

    def test_stored_precision(self):
        self.put(self.HISTORY[2:3])
        g = compare.scheduled_gaps(self.root, t("2026-10-23T23:30:31Z") + dt.timedelta(microseconds=999999), "schedule")
        self.assertEqual(g["scheduled_gap_max_min"], 4.0)

    def test_in_the_written_receipt(self):
        (self.root / "bound.json").write_text(json.dumps(BOUND), encoding="utf-8")
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "schedule"}
        r1 = compare.run(self.root, env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
        self.assertEqual((r1["scheduled_gaps_n"], r1["scheduled_gap_max_min"]), (0, None))
        compare.write(self.root, r1)
        r2 = compare.run(self.root, dict(env, GITHUB_EVENT_NAME="workflow_dispatch"), now=t("2026-10-22T19:00:00Z"), fetch=fake_fetch())
        self.assertEqual((r2["scheduled_gaps_n"], r2["scheduled_gap_max_min"]), (0, None))
        compare.write(self.root, r2)
        r3 = compare.run(self.root, env, now=t("2026-10-22T22:45:00Z"), fetch=fake_fetch())
        self.assertEqual((r3["scheduled_gaps_n"], r3["scheduled_gap_max_min"], r3["scheduled_gap_max_ended_at"]),
                         (1, 255.0, "2026-10-22T22:45:00Z"))
        self.assertEqual(r3["since_previous_receipt_min"], 225.0)  # since the dispatched run, which the gap leaves out


def activity(rows):
    """GitHub's activity log for branch 'beat' as the API serves it, newest first, from (stamp, sha) oldest first."""
    out, before = [], "0000000"
    for at, sha in rows:
        out.append({"before": before, "after": sha, "timestamp": at, "ref": "refs/heads/beat",
                    "activity_type": "force_push" if out else "branch_creation"})
        before = sha
    return out[::-1]


def log_fetch(log):
    def fetch(path, token, repo):
        if path != "/activity?ref=refs/heads/beat&per_page=100":
            raise AssertionError(path)
        if isinstance(log, Exception):
            raise log
        return log
    return fetch


class BeatGaps(unittest.TestCase):
    # this repository's first six beat updates as GitHub's activity log served them on 2026-10-25 (shas shortened);
    # the six beats from 21:30 to 22:45 were held for the late-beat test
    LOG = [("2026-10-23T20:45:13Z", "bd80bcf"), ("2026-10-23T21:00:12Z", "89396fc"), ("2026-10-23T21:15:12Z", "d955291"),
           ("2026-10-23T23:00:13Z", "eb60748"), ("2026-10-23T23:15:12Z", "e8346b4"), ("2026-10-23T23:30:15Z", "3069a48")]

    def gaps(self, prev, cur, log=None, bound=BOUND):
        return compare.beat_gaps("o/r", None, {"beat": {"commit": prev}} if prev else None, {"commit": cur} if cur else None, bound,
                                 log_fetch(activity(self.LOG) if log is None else log))

    def test_the_late_beat_test(self):
        # the receipt at 22:45:17 saw the 21:15:12 beat and the one at 23:26:31 the 23:15:12 beat; the log's newer
        # update at 23:30:15 came after this run read its beat and is left out
        g = self.gaps("d955291", "e8346b4")
        self.assertEqual(g, {"beat_gaps": [{"from": "2026-10-23T21:15:12Z", "to": "2026-10-23T23:00:13Z", "silence_min": 105.0,
                                            "silence_outside_quiet_min": 105.0, "verdict_at_end": "stopped",
                                            "stopped_after": "2026-10-23T22:15:12Z"}],
                             "beat_gaps_n": 2, "beat_gap_max_min": 105.0, "beat_gaps_error": None, "beat_gaps_unread_from": None})

    def test_short_gaps_are_counted_not_listed(self):
        g = self.gaps("bd80bcf", "d955291")
        self.assertEqual((g["beat_gaps"], g["beat_gaps_n"], g["beat_gap_max_min"], g["beat_gaps_error"]), ([], 2, 15.0, None))

    def test_a_gap_that_ended_quiet_and_one_that_ended_stopped(self):
        log = [("2026-10-22T04:50:00Z", "a"), ("2026-10-22T12:00:00Z", "b"), ("2026-10-22T12:15:00Z", "c"), ("2026-10-22T18:30:00Z", "d")]
        g = self.gaps("a", "d", activity(log))
        self.assertEqual(g["beat_gaps"], [
            {"from": "2026-10-22T04:50:00Z", "to": "2026-10-22T12:00:00Z", "silence_min": 430.0, "silence_outside_quiet_min": 10.0,
             "verdict_at_end": "quiet", "stopped_after": None},
            # quiet until 17:00, then 60 minutes outside it: stopped from 18:00
            {"from": "2026-10-22T12:15:00Z", "to": "2026-10-22T18:30:00Z", "silence_min": 375.0, "silence_outside_quiet_min": 90.0,
             "verdict_at_end": "stopped", "stopped_after": "2026-10-22T18:00:00Z"}])
        self.assertEqual((g["beat_gaps_n"], g["beat_gap_max_min"]), (3, 430.0))

    def test_stopped_after_agrees_with_the_verdict(self):
        for a, b in (("2026-10-22T16:30:00Z", "2026-10-22T19:00:00Z"), ("2026-10-22T04:50:00Z", "2026-10-23T03:00:00Z"),
                     ("2026-10-22T18:00:00Z", "2026-10-23T18:00:00Z")):
            st = compare.stopped_after(t(a), t(b), BOUND)
            self.assertEqual(compare.verdict(t(a), st - dt.timedelta(seconds=1), BOUND)["verdict"] != "stopped", True, (a, st))
            self.assertEqual(compare.verdict(t(a), st + dt.timedelta(seconds=1), BOUND)["verdict"], "stopped", (a, st))
        self.assertEqual(compare.stopped_after(t("2026-10-22T16:30:00Z"), t("2026-10-22T19:00:00Z"), BOUND), t("2026-10-22T18:00:00Z"))
        self.assertIsNone(compare.stopped_after(t("2026-10-22T04:50:00Z"), t("2026-10-22T17:49:00Z"), BOUND))

    def test_no_new_beat_needs_no_call(self):
        g = self.gaps("e8346b4", "e8346b4", OSError("must not be called"))
        self.assertEqual(g, {"beat_gaps": [], "beat_gaps_n": 0, "beat_gap_max_min": None, "beat_gaps_error": None, "beat_gaps_unread_from": None})

    def test_nothing_to_start_from(self):
        self.assertEqual(self.gaps(None, "e8346b4")["beat_gaps_error"], "no beat on the previous receipt to start from")
        self.assertEqual(self.gaps("d955291", None)["beat_gaps_error"], "no beat on this run")
        self.assertIsNone(self.gaps(None, "e8346b4")["beat_gaps"])

    def test_anchors_not_in_the_log(self):
        # the previous beat older than the newest 100 updates (or no longer kept), and this run's beat not yet logged
        g = self.gaps("d955291", "e8346b4", activity(self.LOG[3:]))
        self.assertEqual((g["beat_gaps"], g["beat_gaps_n"], g["beat_gap_max_min"]), (None, None, None))
        self.assertIn("(d955291) is not among the 1 updates before this run's beat in the log, which go back to 2026-10-23T23:00:13Z", g["beat_gaps_error"])
        self.assertIsNone(g["beat_gaps_unread_from"])  # gone for good: the next run starts from this run's beat
        self.assertIn("is not in the log yet", self.gaps("d955291", "e8346b4", activity(self.LOG[:4]))["beat_gaps_error"])
        # the start newer in the log than this run's beat: kept for the next run, not dropped (which would list gaps twice)
        g = self.gaps("e8346b4", "d955291")
        self.assertIn("newer in the log than this run's beat", g["beat_gaps_error"])
        self.assertEqual(g["beat_gaps_unread_from"], "e8346b4")

    def test_a_receipt_that_cannot_be_built_keeps_where_to_start(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "bound.json").write_text(json.dumps(dict(BOUND, state_stale_min="abc")), encoding="utf-8")
        log = activity(self.LOG)

        def fetch_at(sha, when):
            def fetch(path, token, repo):
                if path == "/commits/beat":
                    return {"sha": sha, "commit": {"committer": {"date": when}}}
                if path.startswith("/contents/beat.json"):
                    return {"content": base64.b64encode(json.dumps({"seq": 3, "kernel_time": when, "kernel_state": "idle",
                                                                    "kernel_state_at": when}).encode()).decode()}
                return log_fetch(log)(path, token, repo)
            return fetch
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "schedule"}
        compare.write(root, {"compared_at": "2026-10-23T21:20:00Z", "beat": {"commit": "89396fc", "github_time": "2026-10-23T21:00:12Z"}})
        r1 = compare.run(root, env, now=t("2026-10-23T21:20:00Z"), fetch=fetch_at("d955291", "2026-10-23T21:15:12Z"))
        self.assertEqual((r1["verdict"], r1["beat"], r1["beat_gaps_n"], r1["beat_gaps_unread_from"]), ("error", None, 1, "d955291"))
        compare.write(root, r1)
        (root / "bound.json").write_text(json.dumps(BOUND), encoding="utf-8")
        r2 = compare.run(root, env, now=t("2026-10-23T23:20:00Z"), fetch=fetch_at("e8346b4", "2026-10-23T23:15:12Z"))
        self.assertEqual([x["from"] for x in r2["beat_gaps"]], ["2026-10-23T21:15:12Z"])
        self.assertEqual((r2["beat_gaps_n"], r2["beat_gaps_unread_from"]), (2, None))

    def test_a_failed_read_is_made_up_by_the_next_run(self):
        # run 1 reads too soon after its beat (not yet logged); run 2 starts from where run 1 would have
        g1 = self.gaps("d955291", "eb60748", activity(self.LOG[:3]))
        self.assertEqual((g1["beat_gaps"], g1["beat_gaps_unread_from"]), (None, "d955291"))
        prev = {"beat": {"commit": "eb60748"}, "beat_gaps_unread_from": "d955291"}
        g2 = compare.beat_gaps("o/r", None, prev, {"commit": "e8346b4"}, BOUND, log_fetch(activity(self.LOG)))
        self.assertEqual(([x["from"] for x in g2["beat_gaps"]], g2["beat_gaps_n"], g2["beat_gaps_unread_from"]),
                         (["2026-10-23T21:15:12Z"], 2, None))
        # a carried value that is not a commit (a hand-edited latest.json) falls back to the previous receipt's beat
        g4 = compare.beat_gaps("o/r", None, {"beat": {"commit": "d955291"}, "beat_gaps_unread_from": 5}, {"commit": "e8346b4"}, BOUND,
                               log_fetch(activity(self.LOG)))
        self.assertEqual((g4["beat_gaps_n"], g4["beat_gaps_error"]), (2, None))
        # a run with no beat passes the start on too
        g3 = compare.beat_gaps("o/r", None, prev, None, BOUND, log_fetch(OSError("not called")))
        self.assertEqual((g3["beat_gaps_error"], g3["beat_gaps_unread_from"]), ("no beat on this run", "d955291"))

    def test_a_branch_deleted_and_made_again(self):
        log = activity(self.LOG[:3])[::-1]  # oldest first
        log.append({"before": "d955291", "after": compare.NO_SHA, "timestamp": "2026-10-23T22:00:00Z", "activity_type": "branch_deletion"})
        log.append({"before": compare.NO_SHA, "after": "eb60748", "timestamp": "2026-10-23T23:00:13Z", "activity_type": "branch_creation"})
        g = self.gaps("d955291", "eb60748", log[::-1])
        self.assertEqual([(x["from"], x["to"]) for x in g["beat_gaps"]], [("2026-10-23T21:15:12Z", "2026-10-23T23:00:13Z")])
        self.assertEqual(g["beat_gaps_n"], 1)

    def test_an_update_missing_from_the_log(self):
        log = activity(self.LOG)
        del log[2]  # the 23:00:13 update: 21:15:12 -> 23:15:12 would read as one gap
        self.assertIn("does not follow", self.gaps("d955291", "e8346b4", log)["beat_gaps_error"])

    def test_a_failed_call_or_odd_answer_leaves_the_verdict_alone(self):
        self.assertIn("HTTPError", self.gaps("d955291", "e8346b4", urllib.error.HTTPError("u", 403, "rate limited", {}, None))["beat_gaps_error"])
        self.assertIn("TypeError", self.gaps("d955291", "e8346b4", {"message": "Not Found"})["beat_gaps_error"])
        self.assertIsNotNone(self.gaps("d955291", "e8346b4", bound={"quiet_utc": []})["beat_gaps_error"])  # no bound_min

    def test_in_the_written_receipt(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "bound.json").write_text(json.dumps(BOUND), encoding="utf-8")
        log = activity(self.LOG)

        def fetch_at(sha, when, seq):
            def fetch(path, token, repo):
                if path == "/commits/beat":
                    return {"sha": sha, "commit": {"committer": {"date": when}}}
                if path.startswith("/contents/beat.json"):
                    return {"content": base64.b64encode(json.dumps({"seq": seq, "kernel_time": when}).encode()).decode()}
                return log_fetch(log)(path, token, repo)
            return fetch
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "workflow_dispatch"}
        r1 = compare.run(root, env, now=t("2026-10-23T22:45:17Z"), fetch=fetch_at("d955291", "2026-10-23T21:15:12Z", 3))
        self.assertEqual((r1["verdict"], r1["beat_gaps"], r1["beat_gaps_error"]), ("stopped", None, "no beat on the previous receipt to start from"))
        compare.write(root, r1)
        r2 = compare.run(root, dict(env, GITHUB_EVENT_NAME="schedule"), now=t("2026-10-23T23:26:31Z"), fetch=fetch_at("e8346b4", "2026-10-23T23:15:12Z", 5))
        self.assertEqual((r2["verdict"], r2["beats_missing_est"], r2["beat_gaps_n"]), ("running", 6, 2))
        self.assertEqual([(x["from"], x["to"], x["verdict_at_end"]) for x in r2["beat_gaps"]],
                         [("2026-10-23T21:15:12Z", "2026-10-23T23:00:13Z", "stopped")])
        # a failed log read changes nothing else on the receipt
        log[:] = []
        r3 = compare.run(root, dict(env, GITHUB_EVENT_NAME="schedule"), now=t("2026-10-23T23:26:31Z"), fetch=fetch_at("e8346b4", "2026-10-23T23:15:12Z", 5))
        self.assertIn("not in the log yet", r3["beat_gaps_error"])
        self.assertEqual({k: v for k, v in r3.items() if not k.startswith("beat_gap")}, {k: v for k, v in r2.items() if not k.startswith("beat_gap")})


if __name__ == "__main__":
    unittest.main()
