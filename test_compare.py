"""Tests for compare.py. Standard library only: python3 -m unittest test_compare -v"""
import base64
import datetime as dt
import json
import pathlib
import shutil
import tempfile
import unittest
import urllib.error

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


if __name__ == "__main__":
    unittest.main()
