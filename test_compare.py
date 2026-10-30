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
BOUND_FILE = {"version": 1, "beat_period_min": 15, "bound_beats": 4, "quiet_utc": [["05:00", "17:00"]]}
BOUND = compare.load_bound(BOUND_FILE)  # as the comparer uses it, with bound_min 60 derived


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
        (self.root / "bound.json").write_text(json.dumps(BOUND_FILE), encoding="utf-8")
        self.env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "9", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_EVENT_NAME": "schedule", "GITHUB_SHA": "def456"}

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_receipt_names_the_hash_of_the_bound_it_read(self):
        r = compare.run(self.root, self.env, now=t("2026-10-22T18:30:00Z"), fetch=fake_fetch())
        want = hashlib.sha256((self.root / "bound.json").read_bytes()).hexdigest()
        self.assertEqual(r["bound_sha256"], want)
        (self.root / "bound.json").write_text(json.dumps(dict(BOUND_FILE, version="changed")), encoding="utf-8")
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
        bound = dict(BOUND_FILE, halted_states=["halted", "paused"], state_stale_min=15, session_max_min=330)
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
        (self.root / "bound.json").write_text(json.dumps(dict(BOUND_FILE, quiet_utc=[["5am", "17:00"]])), encoding="utf-8")
        r = compare.run(self.root, self.env, now=t("2026-10-22T19:30:00Z"), fetch=fake_fetch())
        self.assertEqual(r["verdict"], "error")
        self.assertIn("could not build the receipt: ValueError", r["error"])
        # the README's one exception: a bound.json the load check refuses gives no receipt, only a failed run
        (self.root / "bound.json").write_text(json.dumps(dict(BOUND_FILE, bound_beats="four")), encoding="utf-8")
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
        raw = json.loads((pathlib.Path(compare.__file__).parent / "bound.json").read_text(encoding="utf-8"))
        b = compare.load_bound(raw)  # raises on what it refuses: see LoadBound
        self.assertEqual(b["bound_min"], raw["bound_beats"] * raw["beat_period_min"])
        for a, z in b.get("quiet_utc", []):
            compare._hm(a), compare._hm(z)
        self.assertFalse("quiet_utc" in b and "quiet_fit" in b)  # one window or one rule for fitting it
        if "quiet_fit" in b:  # a rule it can apply: fit_quiet raises on values it cannot use
            compare.fit_quiet(beats_every("2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z"), t("2026-10-03T00:00:00Z"), b["quiet_fit"], float(b["bound_min"]))
        s = b.get("supersedes")  # null for a first version; otherwise the version replaced and why
        if s is not None:
            self.assertLess(int(s["version"]), int(b["version"]))
            self.assertTrue(str(s["reason"]).strip())
        self.assertIn(compare.verdict(t("2026-10-22T18:00:00Z"), t("2026-10-22T18:00:00Z"), b)["verdict"], ("running",))

    def test_shipped_notes_say_what_the_claim_is_not_and_use_the_receipts_names(self):
        here = pathlib.Path(compare.__file__).parent
        notes = json.loads((here / "bound.json").read_text(encoding="utf-8"))["notes"]
        # rushipingan, e6b37711 point 1: the clause added to the claim sentence
        self.assertIn("a property of the rule, not a verdict about the writer, and not a claim about every kind of silence",
                      notes["claim"])
        text = (here / "README.md").read_text(encoding="utf-8")  # every field the receipt prints is described there
        for old in ("stopped_n", "quiet_n", "unknown_n", "stopped_outages_n"):  # renamed in v3; README may name them as v2's
            self.assertNotRegex(" ".join(notes.values()), rf"\b{old}\b")
        for new in ("stopped_then_ended_n", "quiet_then_ended_n", "unknown_then_ended_n", "stopped_then_ended_outages_n",
                    "quiet_then_dead_n", "blind_spot"):
            self.assertRegex(text, rf"\b{new}\b")


class LoadBound(unittest.TestCase):
    """The bound and the window's margin are counts of beat periods; their minutes are derived (rushipingan, 65110ba3)."""

    def test_minutes_are_derived_from_the_period(self):
        b = compare.load_bound(FIT_FILE)
        self.assertEqual((b["bound_min"], b["quiet_fit"]["margin_min"]), (60, 30))
        # the same counts at a 5-minute beat are 20 and 10 minutes: a change of period moves them in the open
        b = compare.load_bound(dict(FIT_FILE, beat_period_min=5))
        self.assertEqual((b["bound_min"], b["quiet_fit"]["margin_min"]), (20, 10))
        self.assertNotIn("margin_min", FIT_FILE["quiet_fit"])  # what it was given is left as it was
        self.assertEqual(compare.load_bound(BOUND_FILE)["bound_min"], 60)  # fixed hours take no margin

    def test_what_it_refuses(self):
        rule = FIT_FILE["quiet_fit"]
        for bad, why in ((dict(FIT_FILE, bound_min=60), "gives bound_min"),
                         (dict(FIT_FILE, quiet_fit=dict(rule, margin_min=30)), "gives margin_min"),
                         (dict(FIT_FILE, margin_min=30), "bound.json gives margin_min"),  # the minutes misplaced are refused too
                         (dict(FIT_FILE, quiet_fit=dict(rule, bound_min=60)), "quiet_fit gives bound_min"),
                         (dict(FIT_FILE, bound_beats=1), "at least 2"),  # a beat a second late would read stopped
                         (dict(FIT_FILE, bound_beats=4.5), "whole number"), (dict(FIT_FILE, bound_beats="4"), "whole number"),
                         (dict(FIT_FILE, bound_beats=True), "whole number"), ({"beat_period_min": 15}, "bound_beats"),
                         (dict(FIT_FILE, quiet_fit=dict(rule, margin_beats=0)), "at least 1"),  # a margin that holds no beat
                         (dict(FIT_FILE, quiet_fit=dict(rule, margin_beats=1.5)), "whole number"),
                         (dict(FIT_FILE, beat_period_min=7.5, quiet_fit=dict(rule, margin_beats=1)), "whole number of minutes"),
                         (dict(FIT_FILE, beat_period_min=0), "positive"), (dict(FIT_FILE, beat_period_min="15"), "positive"),
                         (dict(FIT_FILE, beat_period_min=True), "positive"), (dict(FIT_FILE, beat_period_min=float("inf")), "positive"),
                         (dict(FIT_FILE, beat_period_min=float("nan")), "positive"), (dict(FIT_FILE, beat_period_min=1e308), "at least 2"),
                         ({k: v for k, v in FIT_FILE.items() if k != "beat_period_min"}, "positive"),
                         (dict(FIT_FILE, quiet_fit=[7, 2]), "not a JSON object"), ([FIT_FILE], "not a JSON object")):
            with self.assertRaisesRegex(ValueError, why):
                compare.load_bound(bad)
        # at a 7.5-minute beat, 2 periods of margin are 15 whole minutes
        self.assertEqual(compare.load_bound(dict(FIT_FILE, beat_period_min=7.5))["quiet_fit"]["margin_min"], 15)


class EndedSilences(unittest.TestCase):
    """The count printed on every receipt under quiet_fit: how the silences that ended in a beat on the fit's days were
    judged when they ended, read from beat_gaps on the receipts under this bound's version."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "receipts").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root)

    @staticmethod
    def gap(a, b, v):
        return {"from": a, "to": b, "verdict_at_end": v}

    def put(self, *receipts, name="2026-10.jsonl", extra=""):
        with (self.root / "receipts" / name).open("a", encoding="utf-8") as f:
            for r in receipts:
                f.write(json.dumps(r) + "\n")
            f.write(extra)

    def count(self, beat_time="2026-10-09T18:00:00Z", now="2026-10-09T18:05:00Z", gaps=None, hole=None, bound=None):
        return compare.ended_silences(self.root, t(now), {"github_time": beat_time} if beat_time else None, bound or FIT_BOUND,
                                      {"beat_gaps": gaps, "beat_gaps_hole": hole})

    def test_counts_the_fits_days_under_this_version_once_each(self):
        self.put({"compared_at": "2026-10-03T12:00:00Z", "bound_version": 2,
                  "beat_gaps": [self.gap("2026-10-01T05:00:00Z", "2026-10-01T11:00:00Z", "stopped"),  # before the fit's days
                                self.gap("2026-10-02T05:00:00Z", "2026-10-02T11:00:00Z", "stopped"),
                                self.gap("2026-10-03T05:00:00Z", "2026-10-03T11:00:00Z", "quiet")]},
                 {"compared_at": "2026-10-05T12:00:00Z", "bound_version": 1,  # judged under another bound
                  "beat_gaps": [self.gap("2026-10-05T05:00:00Z", "2026-10-05T11:00:00Z", "stopped")]},
                 {"compared_at": "2026-10-08T12:00:00Z", "bound_version": 2, "beat_gaps": None},  # a read that failed
                 {"compared_at": "2026-10-08T12:30:00Z", "bound_version": 2,
                  "beat_gaps": [self.gap("2026-10-03T05:00:00Z", "2026-10-03T11:00:00Z", "quiet"),  # listed twice, counted once
                                self.gap("2026-10-08T05:00:00Z", "2026-10-08T11:00:00Z", "unknown"),
                                self.gap("2026-10-08T23:30:00Z", "2026-10-09T01:00:00Z", "stopped")]})  # ended on the beat's own day
        e = self.count()
        self.assertEqual(e, {"ended_silences": {"from": "2026-10-02T00:00:00Z", "to": "2026-10-09T00:00:00Z", "stopped_then_ended_n": 1,
                                                "quiet_then_ended_n": 1, "unknown_then_ended_n": 1, "stopped_then_ended_outages_n": 0,
                                                "quiet_then_dead_n": None, "blind_spot": compare.ENDED_BLIND_SPOT, "holes_n": 0,
                                                "series_from": "2026-10-03T12:00:00Z", "limits": compare.ENDED_LIMITS},
                             "ended_silences_error": None})
        self.assertIn("cannot tell which", e["ended_silences"]["limits"])  # the two limits print with the count
        self.assertIn("too narrow but not too wide", e["ended_silences"]["limits"])
        # the days follow the beat, not the run: a silence from 10-08 23:00 read on 10-09 is judged with 10-01..10-07
        self.assertEqual(self.count("2026-10-08T23:00:00Z")["ended_silences"]["from"], "2026-10-01T00:00:00Z")
        # this run's own gaps count too, and with no beat the days are the run's
        e = self.count(None, "2026-10-10T02:00:00Z", gaps=[self.gap("2026-10-09T05:00:00Z", "2026-10-09T11:00:00Z", "stopped")])
        self.assertEqual((e["ended_silences"]["from"], e["ended_silences"]["stopped_then_ended_n"], e["ended_silences"]["unknown_then_ended_n"]),
                         ("2026-10-03T00:00:00Z", 2, 1))

    def test_the_blind_spot_shows_in_the_names_and_under_the_counter(self):
        # rushipingan, e6b37711: the counter's name says which direction it can see, and the closing line of that comment
        # prints under the counts; the direction it cannot see is printed as a count that is null on every receipt (Ember's form)
        self.put({"compared_at": "2026-10-08T12:00:00Z", "bound_version": 2,
                  "beat_gaps": [self.gap("2026-10-08T05:00:00Z", "2026-10-08T11:00:00Z", "quiet"),
                                self.gap("2026-10-07T05:00:00Z", "2026-10-07T11:00:00Z", "stopped")]})
        e = self.count()["ended_silences"]
        self.assertEqual((e["stopped_then_ended_n"], e["quiet_then_ended_n"]), (1, 1))
        self.assertIsNone(e["quiet_then_dead_n"])  # null, not 0: a quiet silence that ended says nothing about a death
        keys = list(e)
        self.assertEqual(keys[keys.index("unknown_then_ended_n") + 1:keys.index("blind_spot") + 1], ["quiet_then_dead_n", "blind_spot"])
        for words in ("false-stopped direction only", "quiet-then-dead is unobservable by construction",
                      "can be caught oversleeping the living; it can never be caught sleeping through the dead", "e6b37711"):
            self.assertIn(words, e["blind_spot"])
        for old in ("stopped_n", "quiet_n", "unknown_n", "stopped_outages_n"):  # the v2 names are gone from the receipt
            self.assertNotIn(old, e)
            self.assertNotRegex(compare.ENDED_LIMITS, rf"\b{old}\b")

    def test_the_days_edges_the_first_listing_and_outages(self):
        self.put({"compared_at": "2026-10-09T12:00:00Z", "bound_version": 2,
                  "beat_gaps": [self.gap("2026-10-01T23:00:00Z", "2026-10-02T00:00:00Z", "stopped"),  # ends on the first day's midnight: in
                                self.gap("2026-10-08T23:00:00Z", "2026-10-09T00:00:00Z", "stopped"),  # on the beat's day's midnight: out
                                self.gap("2026-10-02T12:00:00Z", "2026-10-04T12:00:00Z", "stopped"),  # 48 hours: an outage
                                self.gap("2026-10-06T00:00:00Z", "2026-10-06T17:00:00Z", "quiet"),  # 17 hours, but quiet
                                self.gap("2026-10-05T05:00:00Z", "2026-10-05T11:00:00Z", "unknown")]})
        # this run lists the 10-05 silence again with another verdict: the first listing, made when it ended, counts
        e = self.count(gaps=[self.gap("2026-10-05T05:00:00Z", "2026-10-05T11:00:00Z", "quiet")])["ended_silences"]
        # of the two stopped, the 48-hour one is past longest_sleep_min: no refit by the rule could have held it
        self.assertEqual((e["stopped_then_ended_n"], e["stopped_then_ended_outages_n"], e["quiet_then_ended_n"], e["unknown_then_ended_n"]), (2, 1, 1, 1))

    def test_a_new_series_starts_at_zero_and_says_when_it_began(self):
        self.put({"compared_at": "2026-10-05T12:00:00Z", "bound_version": 1,
                  "beat_gaps": [self.gap("2026-10-05T05:00:00Z", "2026-10-05T11:00:00Z", "stopped")]})
        e = self.count()["ended_silences"]
        self.assertEqual((e["stopped_then_ended_n"], e["quiet_then_ended_n"], e["unknown_then_ended_n"], e["series_from"]), (0, 0, 0, "2026-10-09T18:05:00Z"))

    def test_holes_that_reach_the_days_are_counted(self):
        self.put({"compared_at": "2026-10-04T12:00:00Z", "bound_version": 2,
                  "beat_gaps_hole": {"from_commit": "s1", "from": "2026-09-30T00:00:00Z", "to": "2026-10-01T23:00:00Z"}},  # before
                 {"compared_at": "2026-10-05T12:00:00Z", "bound_version": 2,
                  "beat_gaps_hole": {"from_commit": "s2", "from": None, "to": "2026-10-04T23:00:00Z"}})
        self.assertEqual(self.count(hole={"from_commit": "s3", "from": "2026-10-08T22:00:00Z", "to": "2026-10-09T17:00:00Z"})
                         ["ended_silences"]["holes_n"], 2)
        # one wholly on the beat's own day is not on the counted days; the same hole on two lines counts once
        self.assertEqual(self.count(hole={"from_commit": "s3", "from": "2026-10-09T01:00:00Z", "to": "2026-10-09T17:00:00Z"})
                         ["ended_silences"]["holes_n"], 1)
        self.put({"compared_at": "2026-10-05T12:30:00Z", "bound_version": 2,
                  "beat_gaps_hole": {"from_commit": "s2", "from": None, "to": "2026-10-04T23:00:00Z"}})
        self.assertEqual(self.count()["ended_silences"]["holes_n"], 1)

    def test_a_line_it_cannot_read_nulls_the_count_and_says_where(self):
        self.put({"compared_at": "2026-10-08T12:00:00Z", "bound_version": 2, "beat_gaps": []}, extra='{"compared_at": "2026-10-0\n')
        self.assertIsNone(self.count()["ended_silences_error"])  # a line cut short is skipped
        for bad in ('[1]\n', json.dumps({"compared_at": "2026-10-08T13:00:00Z", "bound_version": 2, "beat_gaps": {}}) + "\n",
                    json.dumps({"compared_at": "2026-10-08T13:00:00Z", "bound_version": 2,
                                "beat_gaps": [self.gap("2026-09-08T05:00:00Z", "2026-09-08T11:00:00Z", "running")]}) + "\n",
                    json.dumps({"compared_at": "noon", "bound_version": 2, "beat_gaps": []}) + "\n",
                    json.dumps({"bound_version": 2, "beat_gaps": []}) + "\n"):
            (self.root / "receipts" / "2026-10.jsonl").write_text("", encoding="utf-8")
            self.put({"compared_at": "2026-10-08T12:00:00Z", "bound_version": 2, "beat_gaps": []}, extra=bad)
            e = self.count()
            self.assertIsNone(e["ended_silences"])
            self.assertTrue(e["ended_silences_error"].startswith("receipts/2026-10.jsonl line 2: "), e["ended_silences_error"])
        # a receipt split by a U+2028 inside one of its strings is still one receipt (ensure_ascii=False writes it raw)
        (self.root / "receipts" / "2026-10.jsonl").write_text("", encoding="utf-8")
        self.put({"compared_at": "2026-10-08T12:00:00Z", "bound_version": 2, "beat": {"kernel_state": "idle\u2028x"},
                  "beat_gaps": [self.gap("2026-10-08T05:00:00Z", "2026-10-08T11:00:00Z", "stopped")]})
        (self.root / "receipts" / "2026-10.jsonl").write_text(
            (self.root / "receipts" / "2026-10.jsonl").read_text(encoding="utf-8").replace("\\u2028", "\u2028"), encoding="utf-8")
        self.assertIn("\u2028", (self.root / "receipts" / "2026-10.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(self.count()["ended_silences"]["stopped_then_ended_n"], 1)
        # a receipt under another version is not read past its version, as it is not counted
        (self.root / "receipts" / "2026-10.jsonl").write_text(json.dumps({"bound_version": 1, "beat_gaps": "x"}) + "\n", encoding="utf-8")
        self.assertIsNone(self.count()["ended_silences_error"])

    def test_none_under_fixed_hours(self):
        self.put({"compared_at": "2026-10-08T12:00:00Z", "bound_version": 1,
                  "beat_gaps": [self.gap("2026-10-08T05:00:00Z", "2026-10-08T11:00:00Z", "stopped")]})
        self.assertEqual(self.count(bound=BOUND), {"ended_silences": None, "ended_silences_error": None})


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
        (self.root / "bound.json").write_text(json.dumps(BOUND_FILE), encoding="utf-8")
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
        (self.root / "bound.json").write_text(json.dumps(BOUND_FILE), encoding="utf-8")
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
                             "beat_gaps_n": 2, "beat_gap_max_min": 105.0, "beat_gaps_error": None, "beat_gaps_unread_from": None,
                             "beat_gaps_unread_from_time": None, "beat_gaps_carried_n": 0,
                             "beat_gaps_read": {"by": "one call", "updates": 6}, "beat_gaps_reach": None,
                             "beat_gaps_log_from": "2026-10-23T20:45:13Z", "beat_gaps_log_margin_n": 2, "beat_gaps_hole": None})

    def test_short_gaps_are_counted_not_listed(self):
        g = self.gaps("bd80bcf", "d955291")
        self.assertEqual((g["beat_gaps"], g["beat_gaps_n"], g["beat_gap_max_min"], g["beat_gaps_error"]), ([], 2, 15.0, None))
        # the start beat is the oldest update read: one more update in the stretch and it would have fallen out
        self.assertEqual((g["beat_gaps_log_from"], g["beat_gaps_log_margin_n"]), ("2026-10-23T20:45:13Z", 0))

    def test_the_margin_counts_updates_older_than_the_start_not_newer_than_the_beat(self):
        # 100 updates served, the newest after this run's beat (it landed between reading the beat and the log)
        rows = [((dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=15 * k)).strftime("%Y-%m-%dT%H:%M:%SZ"), f"s{k}")
                for k in range(100)]
        g = self.gaps("s30", "s98", activity(rows))
        self.assertEqual((g["beat_gaps_n"], g["beat_gaps_log_margin_n"], g["beat_gaps_log_from"], g["beat_gaps_error"]),
                         (68, 30, "2026-10-24T00:00:00Z", None))
        self.assertEqual(self.gaps("s0", "s98", activity(rows))["beat_gaps_log_margin_n"], 0)

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
        self.assertEqual(g, {"beat_gaps": [], "beat_gaps_n": 0, "beat_gap_max_min": None, "beat_gaps_error": None, "beat_gaps_unread_from": None,
                             "beat_gaps_unread_from_time": None, "beat_gaps_carried_n": 0, "beat_gaps_read": None, "beat_gaps_reach": None,
                             "beat_gaps_log_from": None, "beat_gaps_log_margin_n": None, "beat_gaps_hole": None})

    def test_nothing_to_start_from(self):
        self.assertEqual(self.gaps(None, "e8346b4")["beat_gaps_error"], "no beat on the previous receipt to start from")
        self.assertEqual(self.gaps("d955291", None)["beat_gaps_error"], "no beat on this run")
        self.assertIsNone(self.gaps(None, "e8346b4")["beat_gaps"])

    def test_anchors_not_in_the_log(self):
        # the previous beat older than the newest 100 updates (or no longer kept), and this run's beat not yet logged
        g = self.gaps("d955291", "e8346b4", activity(self.LOG[3:]))
        self.assertEqual((g["beat_gaps"], g["beat_gaps_n"], g["beat_gap_max_min"]), (None, None, None))
        self.assertIn("(d955291) is not among the 1 updates before this run's beat in what was read of the log (one call, up to 100"
                      " updates), which goes back to 2026-10-23T23:00:13Z; see beat_gaps_hole", g["beat_gaps_error"])
        self.assertIsNone(g["beat_gaps_unread_from"])  # gone for good: the next run starts from this run's beat
        # the hole is printed: from the beat no longer read to this run's beat, since none of the stretch is counted
        self.assertEqual((g["beat_gaps_hole"], g["beat_gaps_log_from"], g["beat_gaps_log_margin_n"]),
                         ({"from_commit": "d955291", "from": None, "to": "2026-10-23T23:15:12Z"}, "2026-10-23T23:00:13Z", None))
        prev = {"beat": {"commit": "d955291", "github_time": "2026-10-23T21:15:12Z"}}
        g = compare.beat_gaps("o/r", None, prev, {"commit": "e8346b4"}, BOUND, log_fetch(activity(self.LOG[3:])))
        self.assertEqual(g["beat_gaps_hole"], {"from_commit": "d955291", "from": "2026-10-23T21:15:12Z", "to": "2026-10-23T23:15:12Z"})
        # a carried start is dated by the time carried beside it (Fieldnotes, 0de2b1ca)
        prev = {"beat": {"commit": "eb60748", "github_time": "2026-10-23T23:00:12Z"}, "beat_gaps_unread_from": "d955291",
                "beat_gaps_unread_from_time": "2026-10-23T21:15:12Z", "beat_gaps_carried_n": 3}
        g = compare.beat_gaps("o/r", None, prev, {"commit": "3069a48"}, BOUND, log_fetch(activity(self.LOG[3:])))
        self.assertEqual(g["beat_gaps_hole"], {"from_commit": "d955291", "from": "2026-10-23T21:15:12Z", "to": "2026-10-23T23:30:15Z"})
        self.assertEqual((g["beat_gaps"], g["beat_gaps_n"], g["beat_gaps_unread_from"], g["beat_gaps_carried_n"]), (None, None, None, 0))
        # a receipt written before the time was carried leaves the hole undated; so does a time that is not one
        for legacy in ({}, {"beat_gaps_unread_from_time": "yesterday"}, {"beat_gaps_unread_from_time": "0001-01-01T00:00:00+01:00"}):
            prev = dict({"beat": {"commit": "eb60748", "github_time": "2026-10-23T23:00:12Z"}, "beat_gaps_unread_from": "d955291"}, **legacy)
            g = compare.beat_gaps("o/r", None, prev, {"commit": "3069a48"}, BOUND, log_fetch(activity(self.LOG[3:])))
            self.assertEqual(g["beat_gaps_hole"], {"from_commit": "d955291", "from": None, "to": "2026-10-23T23:30:15Z"})
        g = self.gaps("d955291", "e8346b4", activity(self.LOG[:4]))
        self.assertIn("is not in the log yet", g["beat_gaps_error"])
        self.assertEqual((g["beat_gaps_log_from"], g["beat_gaps_log_margin_n"], g["beat_gaps_hole"]), ("2026-10-23T20:45:13Z", None, None))
        # the start newer in the log than this run's beat: kept for the next run, not dropped (which would list gaps twice)
        g = self.gaps("e8346b4", "d955291")
        self.assertIn("newer in the log than this run's beat", g["beat_gaps_error"])
        self.assertEqual(g["beat_gaps_unread_from"], "e8346b4")

    def test_a_receipt_that_cannot_be_built_keeps_where_to_start(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "bound.json").write_text(json.dumps(dict(BOUND_FILE, state_stale_min="abc")), encoding="utf-8")
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
        # the beat is not on this receipt, so its time is kept beside its commit (Fieldnotes, 0de2b1ca)
        self.assertEqual((r1["beat_gaps_unread_from_time"], r1["beat_gaps_carried_n"]), ("2026-10-23T21:15:12Z", 1))
        compare.write(root, r1)
        (root / "bound.json").write_text(json.dumps(BOUND_FILE), encoding="utf-8")
        r2 = compare.run(root, env, now=t("2026-10-23T23:20:00Z"), fetch=fetch_at("e8346b4", "2026-10-23T23:15:12Z"))
        self.assertEqual([x["from"] for x in r2["beat_gaps"]], ["2026-10-23T21:15:12Z"])
        self.assertEqual((r2["beat_gaps_n"], r2["beat_gaps_unread_from"], r2["beat_gaps_carried_n"]), (2, None, 0))
        # had that beat left the read by the next run, the hole would be dated by the time kept
        log[:] = activity(self.LOG[3:])
        r3 = compare.run(root, env, now=t("2026-10-23T23:20:00Z"), fetch=fetch_at("e8346b4", "2026-10-23T23:15:12Z"))
        self.assertEqual(r3["beat_gaps_hole"], {"from_commit": "d955291", "from": "2026-10-23T21:15:12Z", "to": "2026-10-23T23:15:12Z"})

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
        # and if that start is carried, its count begins at 1: the previous receipt's count was for something else
        g5 = compare.beat_gaps("o/r", None, {"beat": {"commit": "d955291", "github_time": "2026-10-23T21:15:12Z"},
                                             "beat_gaps_unread_from": 5, "beat_gaps_carried_n": 7}, {"commit": "e8346b4"}, BOUND,
                               log_fetch(OSError("down")))
        self.assertEqual((g5["beat_gaps_unread_from"], g5["beat_gaps_unread_from_time"], g5["beat_gaps_carried_n"]),
                         ("d955291", "2026-10-23T21:15:12Z", 1))
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
        g = self.gaps("d955291", "e8346b4", {"message": "Not Found"})
        self.assertIn("TypeError", g["beat_gaps_error"])
        self.assertEqual((g["beat_gaps_read"], g["beat_gaps_unread_from"], g["beat_gaps_carried_n"]), (None, "d955291", 1))
        self.assertIsNotNone(self.gaps("d955291", "e8346b4", bound={"quiet_utc": []})["beat_gaps_error"])  # no bound_min

    def test_in_the_written_receipt(self):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        (root / "bound.json").write_text(json.dumps(BOUND_FILE), encoding="utf-8")
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


FIT_FILE = {"version": 2, "beat_period_min": 15, "bound_beats": 4,
            "quiet_fit": {"days": 7, "recur_days": 2, "margin_beats": 2, "longest_sleep_min": 960}, "halted_states": ["halted", "paused"]}
FIT_BOUND = compare.load_bound(FIT_FILE)
RULE = FIT_BOUND["quiet_fit"]  # with margin_min 30 derived, as fit_quiet takes it


def beats_every(start, end, asleep=lambda u: False):
    """A beat each quarter hour from start to end (inclusive), leaving out those for which asleep(stamp) is true."""
    out, u = [], t(start)
    while u <= t(end):
        if not asleep(u):
            out.append(u)
        u += dt.timedelta(minutes=15)
    return out


def hours(a, b):
    """asleep() for a daily stretch of UTC clock time [a, b), which may run past midnight."""
    lo, hi = compare._hm(a), compare._hm(b)

    def asleep(u):
        m = dt.timedelta(hours=u.hour, minutes=u.minute)
        return lo <= m < hi if lo < hi else (m >= lo or m < hi)
    return asleep


def paged(stamps, per=100):
    """fetch_page over GitHub's activity log for these beat stamps (oldest first), served newest first in pages."""
    log = activity([(compare.iso(u), f"s{i}") for i, u in enumerate(stamps)])
    pages = [log[i:i + per] for i in range(0, len(log), per)] or [[]]
    calls = []

    def fetch_page(path, token, repo):
        i = 0 if path == "/activity?ref=refs/heads/beat&per_page=100" else int(path.rsplit("=", 1)[1])
        calls.append(i)
        return pages[i], (f"https://api.github.com/repositories/1/activity?after={i + 1}" if i + 1 < len(pages) else None)
    fetch_page.calls = calls
    return fetch_page


class QuietFit(unittest.TestCase):
    def fit(self, beats, start, rule=RULE):
        return compare.fit_quiet(beats, t(start), rule, 60)

    def test_the_record_as_served_on_2026_10_25(self):
        # the beats from the branch's creation to 23:30:11Z, every quarter hour but the late-beat test's six; only
        # 2026-10-24 is a whole day before 2026-10-25, and its longest silence was 15 minutes
        b = beats_every("2026-10-23T20:45:00Z", "2026-10-25T23:30:00Z", lambda u: t("2026-10-23T21:15:01Z") < u < t("2026-10-23T23:00:00Z"))
        f = self.fit(b, "2026-10-25T23:30:00Z")
        self.assertEqual(f["quiet_utc"], [])
        self.assertEqual(f["fit"], {"days_n": 1, "from": "2026-10-24T00:00:00Z", "to": "2026-10-25T00:00:00Z", "silences": [],
                                    "outages": [], "outages_reason": "gaps longer than longest_sleep_min (960 min) are taken as"
                                                                     " outages and not counted in the window",
                                    "longest_silence_min": 15.0})
        # the late-beat test's day is not whole, so its 105 minutes never enter a fit
        self.assertEqual(self.fit(b, "2026-10-24T12:00:00Z")["fit"]["days_n"], 0)

    def test_a_nightly_sleep_makes_a_window(self):
        # asleep 05:00-11:00 every day: the last beat is 04:45 and the next 11:00, so each silence covers 04:45-11:00
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-09T12:00:00Z", hours("05:00", "11:00"))
        f = self.fit(b, "2026-10-09T12:00:00Z")
        self.assertEqual(f["quiet_utc"], [["04:15", "11:30"]])
        self.assertEqual((f["fit"]["days_n"], f["fit"]["from"], f["fit"]["to"]), (7, "2026-10-02T00:00:00Z", "2026-10-09T00:00:00Z"))
        self.assertEqual(len(f["fit"]["silences"]), 7)
        self.assertEqual(f["fit"]["silences"][0], {"from": "2026-10-02T04:45:00Z", "to": "2026-10-02T11:00:00Z", "silence_min": 375.0})

    def test_one_night_is_not_a_window_and_two_are(self):
        one = beats_every("2026-10-01T00:00:00Z", "2026-10-09T12:00:00Z", lambda u: t("2026-10-04T05:00:00Z") <= u < t("2026-10-04T11:00:00Z"))
        self.assertEqual(self.fit(one, "2026-10-09T12:00:00Z")["quiet_utc"], [])
        self.assertEqual(len(self.fit(one, "2026-10-09T12:00:00Z")["fit"]["silences"]), 1)
        # a second night an hour later: the window is the hours both covered (05:45-11:00), widened by 30 minutes
        two = [u for u in one if not t("2026-10-06T06:00:00Z") <= u < t("2026-10-06T12:00:00Z")]
        self.assertEqual(self.fit(two, "2026-10-09T12:00:00Z")["quiet_utc"], [["05:15", "11:30"]])

    def test_a_window_past_midnight(self):
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-09T12:00:00Z", hours("22:00", "04:00"))
        self.assertEqual(self.fit(b, "2026-10-09T12:00:00Z")["quiet_utc"], [["21:15", "04:30"]])
        # the window reaches back into the evening of the day before, so a death at 21:30 waits for 04:30
        bound = dict(FIT_BOUND, quiet_utc=[["21:15", "04:30"]])
        self.assertEqual(compare.verdict(t("2026-10-09T21:30:00Z"), t("2026-10-10T05:29:00Z"), bound)["verdict"], "quiet")
        self.assertEqual(compare.verdict(t("2026-10-09T21:30:00Z"), t("2026-10-10T05:31:00Z"), bound)["verdict"], "stopped")

    def test_a_silence_longer_than_a_sleep_stays_out(self):
        # 30 hours silent covers six hours of the day twice; taken as sleep, that alone would make a window
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-09T12:00:00Z", lambda u: t("2026-10-03T06:00:00Z") <= u < t("2026-10-04T12:00:00Z"))
        f = self.fit(b, "2026-10-09T12:00:00Z")
        self.assertEqual((f["quiet_utc"], f["fit"]["silences"]), ([], []))
        self.assertEqual(f["fit"]["outages"], [{"from": "2026-10-03T05:45:00Z", "to": "2026-10-04T12:00:00Z", "silence_min": 1815.0}])
        self.assertEqual(f["fit"]["longest_silence_min"], 1815.0)
        # the receipt says why they were left out, by the rule's own number (rushipingan, 65110ba3)
        self.assertIn("longer than longest_sleep_min (900 min) are taken as outages",
                      self.fit(b, "2026-10-09T12:00:00Z", dict(RULE, longest_sleep_min=900))["fit"]["outages_reason"])
        # and a rule may not take a gap of a day or more as a sleep, since one such gap alone would make a window
        with self.assertRaises(ValueError):
            self.fit(b, "2026-10-09T12:00:00Z", dict(RULE, longest_sleep_min=1440))

    def test_only_whole_days_before_the_silence_count(self):
        # asleep 05:00-11:00 on 10-08 and after: for a silence that began on 10-09, only 10-08's night is in the fit
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-10T12:00:00Z",
                        lambda u: u >= t("2026-10-08T00:00:00Z") and hours("05:00", "11:00")(u))
        self.assertEqual(self.fit(b, "2026-10-09T12:00:00Z")["quiet_utc"], [])
        self.assertEqual(self.fit(b, "2026-10-10T12:00:00Z")["quiet_utc"], [["04:15", "11:30"]])  # 10-08 and 10-09
        # a day the record does not reach the end of is left out
        short = [u for u in b if u <= t("2026-10-09T12:00:00Z")]
        self.assertEqual((self.fit(short, "2026-10-10T12:00:00Z")["fit"]["days_n"], self.fit(short, "2026-10-10T12:00:00Z")["quiet_utc"]), (6, []))
        # and so is a day before the record starts
        late = [u for u in b if u >= t("2026-10-05T10:00:00Z")]
        self.assertEqual((self.fit(late, "2026-10-09T12:00:00Z")["fit"]["days_n"], self.fit(late, "2026-10-09T12:00:00Z")["fit"]["from"]),
                         (3, "2026-10-06T00:00:00Z"))

    def test_a_window_of_the_whole_day_is_refused(self):
        # beats only at 00:00 and 12:00: twelve-hour silences cover the whole day, every day, and such a window
        # would read quiet however long a silence lasted
        b = [u for u in beats_every("2026-10-01T00:00:00Z", "2026-10-09T12:00:00Z") if u.minute == 0 and u.hour in (0, 12)]
        self.assertEqual(compare.verdict(t("2026-10-09T12:00:00Z"), t("2026-10-11T12:00:00Z"), dict(FIT_BOUND, quiet_utc=[["00:00", "00:00"]]))["verdict"], "quiet")
        with self.assertRaisesRegex(LookupError, "covers the whole day"):
            self.fit(b, "2026-10-09T12:00:00Z")

    def test_two_sleeps_a_day(self):
        # closed 20:00-08:00 and 13:00-17:00 every day: two stretches of window, 18 hours 30 minutes in all
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-09T18:00:00Z", lambda u: hours("20:00", "08:00")(u) or hours("13:00", "17:00")(u))
        f = self.fit(b, "2026-10-09T18:00:00Z")
        self.assertEqual(f["quiet_utc"], [["12:15", "17:30"], ["19:15", "08:30"]])
        # a death at 18:00 is outside both and reads stopped at 19:00, as it would with no window
        self.assertEqual(compare.verdict(t("2026-10-09T18:00:00Z"), t("2026-10-09T19:01:00Z"), dict(FIT_BOUND, quiet_utc=f["quiet_utc"]))["verdict"], "stopped")

    def test_values_it_cannot_use(self):
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-02T12:00:00Z")
        for bad in (dict(RULE, days=0), dict(RULE, recur_days=0), dict(RULE, recur_days=8), dict(RULE, margin_min=-1),
                    dict(RULE, longest_sleep_min=60), dict(RULE, longest_sleep_min=1440), dict(RULE, days="x")):
            with self.assertRaises((ValueError, TypeError)):
                self.fit(b, "2026-10-02T12:00:00Z", bad)
        with self.assertRaises(KeyError):
            self.fit(b, "2026-10-02T12:00:00Z", {"days": 7})


class ReadLog(unittest.TestCase):
    def test_reads_back_far_enough_and_no_further(self):
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-09T12:00:00Z")  # 817 beats, 9 pages
        fp = paged(b)
        # the first page goes back to 10-08 11:15, so one day before that day is 10-07 00:00, which the third page passes
        got, complete = compare.read_log("o/r", None, 1, fp)
        self.assertEqual((fp.calls, got[-1], got[0], complete), ([0, 1, 2], b[-1], t("2026-10-06T09:15:00Z"), False))
        self.assertEqual(compare.read_log("o/r", None, 30, paged(b)), (b, True))  # to the branch's creation
        with self.assertRaisesRegex(LookupError, "further than 3 pages"):
            compare.read_log("o/r", None, 30, paged(b), max_pages=3)
        with self.assertRaisesRegex(LookupError, "empty"):
            compare.read_log("o/r", None, 7, lambda p, k, r: ([], None))
        # an empty page that still names a next page is not the end of the log
        pages = {"/activity?ref=refs/heads/beat&per_page=100": (activity([("2026-10-09T12:00:00Z", "a")]), "https://api.github.com/p2"),
                 "https://api.github.com/p2": ([], "https://api.github.com/p3")}
        with self.assertRaisesRegex(LookupError, "empty page with a next page"):
            compare.read_log("o/r", None, 7, lambda p, k, r: pages[p])

    def test_an_update_missing_or_an_odd_answer_raises(self):
        b = beats_every("2026-10-01T00:00:00Z", "2026-10-01T12:00:00Z")
        log = activity([(compare.iso(u), f"s{i}") for i, u in enumerate(b)])
        del log[5]
        with self.assertRaisesRegex(ValueError, "does not follow"):
            compare.read_log("o/r", None, 7, lambda p, k, r: (log, None))
        with self.assertRaises(TypeError):
            compare.read_log("o/r", None, 7, lambda p, k, r: ({"message": "Not Found"}, None))

    def test_a_branch_deleted_and_made_again(self):
        log = activity(BeatGaps.LOG[:3])[::-1]  # oldest first
        log.append({"before": "d955291", "after": compare.NO_SHA, "timestamp": "2026-10-23T22:00:00Z", "activity_type": "branch_deletion"})
        log.append({"before": compare.NO_SHA, "after": "eb60748", "timestamp": "2026-10-23T23:00:13Z", "activity_type": "branch_creation"})
        got, complete = compare.read_log("o/r", None, 7, lambda p, k, r: (log[::-1], None))
        self.assertEqual(([compare.iso(u) for u in got], complete),
                         (["2026-10-23T20:45:13Z", "2026-10-23T21:00:12Z", "2026-10-23T21:15:12Z", "2026-10-23T23:00:13Z"], True))

    def test_a_next_page_off_githubs_api_host_is_refused(self):
        with self.assertRaisesRegex(ValueError, "off GitHub's API host"):
            compare.api_page("https://example.com/activity", "secret", "o/r")

    def test_the_next_page_comes_from_the_link_header(self):
        class Answer:
            def __init__(self, link):
                self.headers = {"Link": link} if link else {}

            def read(self):
                return b"[]"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        nxt = "https://api.github.com/repositories/1/activity?ref=refs%2Fheads%2Fbeat&per_page=100&after=Y3Vy"
        link = f'<https://api.github.com/repositories/1/activity?before=X>; rel="prev", <{nxt}>; rel="next"'
        with mock.patch("urllib.request.urlopen", return_value=Answer(link)) as op:
            self.assertEqual(compare.api_page("/activity?ref=refs/heads/beat&per_page=100", "tok", "o/r"), ([], nxt))
            self.assertEqual(op.call_args[0][0].full_url, "https://api.github.com/repos/o/r/activity?ref=refs/heads/beat&per_page=100")
        with mock.patch("urllib.request.urlopen", return_value=Answer(None)):
            self.assertEqual(compare.api_page(nxt, "tok", "o/r"), ([], None))


class FittedRun(unittest.TestCase):
    """Receipts under a bound with quiet_fit: asleep 05:00-11:00 UTC every night of 2026-10-01 to 10-09."""
    NIGHTS = beats_every("2026-10-01T00:00:00Z", "2026-10-09T18:00:00Z", hours("05:00", "11:00"))

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        (self.root / "bound.json").write_text(json.dumps(FIT_FILE), encoding="utf-8")
        self.env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_EVENT_NAME": "schedule"}

    def tearDown(self):
        shutil.rmtree(self.root)

    def run_at(self, last_beat, now, stamps=None, fetch_page=None, state=None):
        stamps = [u for u in (self.NIGHTS if stamps is None else stamps) if u <= t(last_beat)]
        log = activity([(compare.iso(u), f"s{i}") for i, u in enumerate(stamps)])
        body = {"seq": len(stamps), **(state or {})}

        def fetch(path, token, repo):
            if path == "/commits/beat":
                return {"sha": f"s{len(stamps) - 1}", "commit": {"committer": {"date": last_beat}}}
            if path.startswith("/contents/beat.json"):
                return {"content": base64.b64encode(json.dumps(body).encode()).decode()}
            return log_fetch(log[:100])(path, token, repo)
        return compare.run(self.root, self.env, now=t(now), fetch=fetch, fetch_page=fetch_page or paged(stamps))

    def test_the_window_is_on_the_receipt_and_judges_the_silence(self):
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:30:00Z")
        self.assertEqual((r["verdict"], r["quiet_utc"], r["quiet_fit"]["days_n"], r["quiet_fit_error"]), ("running", [["04:15", "11:30"]], 7, None))
        # silent from 18:00: stopped at 19:00, as with any window that leaves the evening out
        self.assertEqual(self.run_at("2026-10-09T18:00:00Z", "2026-10-09T19:01:00Z")["verdict"], "stopped")
        # silent from 04:00 on 10-10: 15 minutes before the window, then quiet to 11:30, then 45 more: stopped at 12:15
        night = self.NIGHTS + beats_every("2026-10-09T18:15:00Z", "2026-10-10T04:00:00Z")
        r = self.run_at("2026-10-10T04:00:00Z", "2026-10-10T12:14:00Z", night)
        self.assertEqual((r["verdict"], r["silence_outside_quiet_min"]), ("quiet", 59.0))
        self.assertEqual(self.run_at("2026-10-10T04:00:00Z", "2026-10-10T12:16:00Z", night)["verdict"], "stopped")

    def test_a_log_it_cannot_read_leaves_long_silences_unknown(self):
        def broken(path, token, repo):
            raise urllib.error.HTTPError("u", 403, "rate limited", {}, None)
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:30:00Z", fetch_page=broken)
        self.assertEqual((r["verdict"], r["quiet_utc"], r["quiet_fit"]), ("running", None, None))
        self.assertIn("HTTPError", r["quiet_fit_error"])
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T20:00:00Z", fetch_page=broken)
        self.assertEqual((r["verdict"], r["silence_min"], r["silence_outside_quiet_min"], r["error"]), ("unknown", 120.0, None, None))

    def test_a_fresh_beat_from_a_halted_kernel_still_reads_halted(self):
        def down(path, token, repo):
            raise OSError("down")
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:10:00Z", fetch_page=down, state={
            "kernel_time": "2026-10-09T18:00:00Z", "kernel_state": "halted", "kernel_state_at": "2026-10-09T17:00:00Z"})
        self.assertEqual((r["verdict"], r["halted_reason"], r["quiet_utc"]), ("halted", "kernel_state halted", None))

    def test_both_kinds_of_window_is_a_fit_error(self):
        (self.root / "bound.json").write_text(json.dumps(dict(FIT_FILE, quiet_utc=[["05:00", "17:00"]])), encoding="utf-8")
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T20:00:00Z")
        self.assertEqual(r["verdict"], "unknown")
        self.assertIn("both quiet_utc and quiet_fit", r["quiet_fit_error"])

    def test_beat_gaps_are_judged_by_the_window_of_their_own_start(self):
        # a sleep on 10-09 that ended at 11:00 and an outage 13:00-15:00, both since the previous receipt's beat at 03:00
        stamps = [u for u in self.NIGHTS if not t("2026-10-09T13:00:00Z") <= u < t("2026-10-09T15:00:00Z")]
        compare.write(self.root, {"compared_at": "2026-10-09T03:05:00Z", "beat": {"commit": f"s{stamps.index(t('2026-10-09T03:00:00Z'))}",
                                                                              "github_time": "2026-10-09T03:00:00Z"}})
        r = self.run_at("2026-10-09T16:00:00Z", "2026-10-09T16:05:00Z", stamps)
        self.assertEqual((r["quiet_utc"], r["beat_gaps_error"], r["verdict"]), ([["04:15", "11:30"]], None, "running"))
        self.assertEqual([(x["from"], x["verdict_at_end"], x["silence_outside_quiet_min"], x["stopped_after"]) for x in r["beat_gaps"]],
                         [("2026-10-09T04:45:00Z", "quiet", 0.0, None), ("2026-10-09T12:45:00Z", "stopped", 135.0, "2026-10-09T13:45:00Z")])
        # with no window, the same gaps are listed with verdict unknown
        page = activity([(compare.iso(u), f"s{i}") for i, u in enumerate(stamps) if u <= t("2026-10-09T16:00:00Z")])[:100]
        g = compare.beat_gaps("o/r", None, {"beat": {"commit": f"s{stamps.index(t('2026-10-09T03:00:00Z'))}"}}, {"commit": page[0]["after"]},
                              FIT_BOUND, log_fetch(page), compare.unfitted)
        self.assertEqual([(x["from"], x["verdict_at_end"], x["stopped_after"], x["silence_outside_quiet_min"]) for x in g["beat_gaps"]],
                         [("2026-10-09T04:45:00Z", "unknown", None, None), ("2026-10-09T12:45:00Z", "unknown", None, None)])

    def test_the_count_of_ended_silences_is_on_the_receipt(self):
        # the sleep and the outage of the test above end on 10-09, the beat's own day, which the fit's days leave out
        stamps = [u for u in self.NIGHTS if not t("2026-10-09T13:00:00Z") <= u < t("2026-10-09T15:00:00Z")]
        compare.write(self.root, {"compared_at": "2026-10-09T03:05:00Z", "bound_version": 2,
                                  "beat": {"commit": f"s{stamps.index(t('2026-10-09T03:00:00Z'))}", "github_time": "2026-10-09T03:00:00Z"}})
        r = self.run_at("2026-10-09T16:00:00Z", "2026-10-09T16:05:00Z", stamps)
        self.assertEqual(r["ended_silences"], {"from": "2026-10-02T00:00:00Z", "to": "2026-10-09T00:00:00Z", "stopped_then_ended_n": 0, "quiet_then_ended_n": 0,
                                               "unknown_then_ended_n": 0, "stopped_then_ended_outages_n": 0, "quiet_then_dead_n": None,
                                               "blind_spot": compare.ENDED_BLIND_SPOT, "holes_n": 0, "series_from": "2026-10-09T03:05:00Z",
                                               "limits": compare.ENDED_LIMITS})
        self.assertEqual([x["verdict_at_end"] for x in r["beat_gaps"]], ["quiet", "stopped"])
        compare.write(self.root, r)
        # on 10-10 they are on the fit's days: one silence that ended stopped, one quiet
        later = [u for u in stamps if u <= t("2026-10-09T16:00:00Z")] + beats_every("2026-10-09T16:15:00Z", "2026-10-10T01:00:00Z")
        r = self.run_at("2026-10-10T01:00:00Z", "2026-10-10T01:05:00Z", later)
        self.assertEqual((r["ended_silences"]["from"], r["ended_silences"]["stopped_then_ended_n"], r["ended_silences"]["quiet_then_ended_n"], r["ended_silences_error"]),
                         ("2026-10-03T00:00:00Z", 1, 1, None))
        self.assertEqual(r["beat_gaps"], [])  # nothing new: the count is read back from the receipts
        # a receipt that cannot be built still carries the count, with the days of the beat it read
        (self.root / "bound.json").write_text(json.dumps(dict(FIT_FILE, state_stale_min="abc")), encoding="utf-8")
        r = self.run_at("2026-10-10T01:00:00Z", "2026-10-10T01:05:00Z", later, state={
            "kernel_time": "2026-10-10T01:00:00Z", "kernel_state": "idle", "kernel_state_at": "2026-10-10T00:58:00Z"})
        self.assertEqual((r["verdict"], r["beat"], r["ended_silences"]["stopped_then_ended_n"], r["ended_silences"]["from"]),
                         ("error", None, 1, "2026-10-03T00:00:00Z"))
        # and a line it cannot read nulls the count on a run, and nothing else
        with (self.root / "receipts" / "2026-10.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"compared_at": "2026-10-09T20:00:00Z", "bound_version": 2, "beat_gaps": "x"}) + "\n")
        (self.root / "bound.json").write_text(json.dumps(FIT_FILE), encoding="utf-8")
        r2 = self.run_at("2026-10-10T01:00:00Z", "2026-10-10T01:05:00Z", later)
        self.assertEqual((r2["verdict"], r2["ended_silences"], r2["error"]), ("running", None, None))
        self.assertIn("beat_gaps is not a list", r2["ended_silences_error"])

    def test_gaps_are_counted_from_the_fits_read_when_the_start_is_past_one_call(self):
        # the previous receipt's beat at 10-08 03:00 has 108 updates after it: past the newest 100, inside the fit's read
        start = self.NIGHTS.index(t("2026-10-08T03:00:00Z"))
        prev = {"compared_at": "2026-10-08T03:05:00Z", "beat": {"commit": f"s{start}", "github_time": "2026-10-08T03:00:00Z"}}
        compare.write(self.root, prev)
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z")
        self.assertEqual((r["beat_gaps_error"], r["beat_gaps_hole"], r["quiet_fit_error"]), (None, None, None))
        self.assertEqual([(x["from"], x["to"], x["verdict_at_end"]) for x in r["beat_gaps"]],
                         [("2026-10-08T04:45:00Z", "2026-10-08T11:00:00Z", "quiet"), ("2026-10-09T04:45:00Z", "2026-10-09T11:00:00Z", "quiet")])
        self.assertEqual(r["beat_gaps_n"], len(self.NIGHTS) - 1 - start)
        self.assertGreater(r["beat_gaps_n"], 100)
        # the fit's read reached the log's end, so its oldest entry is the oldest the log holds, and the margin counts from it
        self.assertEqual((r["beat_gaps_log_from"], r["beat_gaps_log_margin_n"]), ("2026-10-01T00:00:00Z", start))

    def test_without_the_fits_read_the_gaps_fall_back_to_one_call(self):
        def broken(path, token, repo):
            raise urllib.error.HTTPError("u", 403, "rate limited", {}, None)
        start = self.NIGHTS.index(t("2026-10-08T03:00:00Z"))
        compare.write(self.root, {"compared_at": "2026-10-08T03:05:00Z", "beat": {"commit": f"s{start}", "github_time": "2026-10-08T03:00:00Z"}})
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", fetch_page=broken)
        self.assertIn("HTTPError", r["quiet_fit_error"])  # the fallback shows on the same receipt
        # the start is past the one call, but the fit's read failed, so it may still be read: carried, not a hole
        self.assertIn("in one call, and the quiet fit's read of the log failed", r["beat_gaps_error"])
        self.assertEqual((r["beat_gaps_hole"], r["beat_gaps_unread_from"], r["beat_gaps"]), (None, f"s{start}", None))
        self.assertEqual(r["beat_gaps_log_from"], compare.iso(self.NIGHTS[-100]))  # the one call's oldest update
        # which read, the start's time, how many receipts have carried it, and how far back a working read would reach
        self.assertEqual((r["beat_gaps_read"], r["beat_gaps_unread_from_time"], r["beat_gaps_carried_n"], r["beat_gaps_reach"]),
                         ({"by": "one call", "updates": 100}, "2026-10-08T03:00:00Z", 1, "2026-10-01T00:00:00Z"))
        self.assertEqual(r["beat_gaps_reach"], compare.iso(compare.midnight(self.NIGHTS[-100]) - 7 * compare.DAY))
        # the next run, with the read back, counts the whole stretch from the carried start
        compare.write(self.root, dict(r, compared_at="2026-10-09T18:05:00Z"))
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:35:00Z")
        self.assertEqual((r["beat_gaps_error"], r["beat_gaps_hole"], len(r["beat_gaps"])), (None, None, 2))
        self.assertEqual(r["beat_gaps_n"], len(self.NIGHTS) - 1 - start)
        self.assertEqual((r["beat_gaps_read"], r["beat_gaps_unread_from"], r["beat_gaps_unread_from_time"], r["beat_gaps_carried_n"]),
                         ({"by": "long read", "updates": len(self.NIGHTS)}, None, None, 0))
        # a page failing midway (Fieldnotes, bb2a6032): two pages served, 200 updates that hold the start, then the third
        # fails. What was read is dropped, not used as the whole read: the start is carried, and the oldest named is the
        # one call's, not the 200's
        whole = paged([u for u in self.NIGHTS if u <= t("2026-10-09T18:00:00Z")])

        def midway(path, token, repo):
            if path.endswith("after=2"):
                raise urllib.error.HTTPError("u", 502, "bad gateway", {}, None)
            return whole(path, token, repo)
        compare.write(self.root, {"compared_at": "2026-10-08T03:05:00Z", "beat": {"commit": f"s{start}", "github_time": "2026-10-08T03:00:00Z"}})
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", fetch_page=midway)
        self.assertEqual(whole.calls, [0, 1])
        self.assertGreater(start, len(self.NIGHTS) - 200)  # the start is among the 200 updates the two pages held
        self.assertIn("HTTPError", r["quiet_fit_error"])
        self.assertEqual((r["beat_gaps_hole"], r["beat_gaps_unread_from"], r["beat_gaps"]), (None, f"s{start}", None))
        self.assertEqual(r["beat_gaps_log_from"], compare.iso(self.NIGHTS[-100]))
        self.assertNotEqual(r["beat_gaps_log_from"], compare.iso(self.NIGHTS[-200]))
        # a start not even in the fit's read is a hole, judged against that read
        g = compare.beat_gaps("o/r", None, {"beat": {"commit": "s0"}}, {"commit": "s3"}, BOUND, log_fetch(OSError("no call")),
                              log=list(reversed(activity([("2026-10-09T00:15:00Z", "s1"), ("2026-10-09T00:30:00Z", "s2"),
                                                          ("2026-10-09T00:45:00Z", "s3")]))))
        self.assertIn("(the quiet fit's read, 3 updates)", g["beat_gaps_error"])
        self.assertEqual(g["beat_gaps_hole"], {"from_commit": "s0", "from": None, "to": "2026-10-09T00:45:00Z"})
        # a bound with a fixed window makes no long read: one call, as before
        g = compare.beat_gaps("o/r", None, {"beat": {"commit": "s1"}}, {"commit": "s3"}, BOUND,
                              log_fetch(activity([("2026-10-09T00:00:00Z", "s0"), ("2026-10-09T00:15:00Z", "s1"),
                                                  ("2026-10-09T00:30:00Z", "s2"), ("2026-10-09T02:00:00Z", "s3")])), log=[])
        self.assertEqual((g["beat_gaps_n"], g["beat_gap_max_min"], g["beat_gaps_log_from"]), (2, 90.0, "2026-10-09T00:00:00Z"))

    # a record from 09-20 to 10-09 18:00. At a beat at 10-09 18:00 the newest 100 updates go back to 10-08 11:15, so a
    # working long read is sure to reach back to 10-01 00:00, seven whole days before that day: the reach
    LONG = beats_every("2026-09-20T00:00:00Z", "2026-10-09T18:00:00Z", hours("05:00", "11:00"))
    REACH = "2026-10-01T00:00:00Z"

    def carried(self, at, n):
        """A previous receipt that has carried the beat at `at` for n receipts, its own beat that of 10-09 12:00."""
        return {"compared_at": "2026-10-09T12:05:00Z", "bound_version": 2,
                "beat": {"commit": f"s{self.LONG.index(t('2026-10-09T12:00:00Z'))}", "github_time": "2026-10-09T12:00:00Z"},
                "beat_gaps_unread_from": f"s{self.LONG.index(t(at))}", "beat_gaps_unread_from_time": at, "beat_gaps_carried_n": n}

    def broken(self, path, token, repo):
        raise urllib.error.HTTPError("u", 403, "rate limited", {}, None)

    def test_a_carried_start_within_the_reach_is_carried_on_and_counted(self):
        # a run whose long read fails: the start (10-05 12:00) is past the newest 100 but inside the reach
        compare.write(self.root, self.carried("2026-10-05T12:00:00Z", 2))
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", self.LONG, fetch_page=self.broken)
        self.assertEqual(compare.iso(compare.midnight(self.LONG[-100]) - 7 * compare.DAY), self.REACH)
        self.assertEqual((r["verdict"], r["beat_gaps_reach"], r["beat_gaps_hole"], r["beat_gaps"]), ("running", self.REACH, None, None))
        self.assertEqual((r["beat_gaps_unread_from"], r["beat_gaps_unread_from_time"], r["beat_gaps_carried_n"]),
                         (f"s{self.LONG.index(t('2026-10-05T12:00:00Z'))}", "2026-10-05T12:00:00Z", 3))
        # the first run whose read works counts the stretch, and the carry ends
        compare.write(self.root, r)
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:35:00Z", self.LONG)
        self.assertEqual((r["beat_gaps_error"], r["beat_gaps_hole"], r["beat_gaps_unread_from"], r["beat_gaps_carried_n"]), (None, None, None, 0))
        self.assertEqual(r["beat_gaps_read"]["by"], "long read")
        self.assertEqual([x["from"][:10] for x in r["beat_gaps"]], ["2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"])
        # a carried start at the reach itself is carried, not a hole
        compare.write(self.root, self.carried(self.REACH, 9))
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", self.LONG, fetch_page=self.broken)
        self.assertEqual((r["beat_gaps_hole"], r["beat_gaps_unread_from_time"], r["beat_gaps_carried_n"]), (None, self.REACH, 10))

    def test_a_days_it_cannot_use_leaves_no_reach(self):
        # quiet_fit_error says why; the gaps are still counted from the one call, as under v2
        bad = dict(FIT_BOUND, quiet_fit=dict(RULE, days="x"))
        g = compare.beat_gaps("o/r", None, {"beat": {"commit": "d955291", "github_time": "2026-10-23T21:15:12Z"}}, {"commit": "e8346b4"},
                              bad, log_fetch(activity(BeatGaps.LOG)), compare.unfitted, log=[])
        self.assertEqual((g["beat_gaps_n"], g["beat_gaps_reach"], g["beat_gaps_error"], g["beat_gaps_unread_from"]), (2, None, None, None))

    def test_a_carried_start_past_the_reach_is_a_dated_hole_on_a_failing_run(self):
        # carried since 09-30 12:00 and the long read still failing: no working read on this run could count it
        compare.write(self.root, self.carried("2026-09-30T12:00:00Z", 40))
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", self.LONG, fetch_page=self.broken)
        self.assertEqual(r["beat_gaps_hole"], {"from_commit": f"s{self.LONG.index(t('2026-09-30T12:00:00Z'))}",
                                               "from": "2026-09-30T12:00:00Z", "to": "2026-10-09T18:00:00Z"})
        self.assertEqual((r["beat_gaps_unread_from"], r["beat_gaps_unread_from_time"], r["beat_gaps_carried_n"], r["beat_gaps"]),
                         (None, None, 0, None))
        self.assertIn("at 2026-09-30T12:00:00Z) is not among the 99 updates before this run's beat in one call", r["beat_gaps_error"])
        self.assertIn(f"older than {self.REACH}, as far back as the quiet fit's read is sure to reach", r["beat_gaps_error"])
        self.assertEqual((r["verdict"], r["ended_silences"]["holes_n"]), ("running", 1))  # the verdict is the beat's, as before
        # the next run starts from this run's beat, not from the hole's start
        compare.write(self.root, r)
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:35:00Z", self.LONG, fetch_page=self.broken)
        self.assertEqual((r["beat_gaps"], r["beat_gaps_hole"], r["beat_gaps_carried_n"]), ([], None, 0))

    def test_a_carried_start_is_carried_on_when_the_reach_cannot_be_computed(self):
        # the one call fails too: no newest 100, so no reach, however old the start
        prev = self.carried("2026-09-30T12:00:00Z", 40)
        g = compare.beat_gaps("o/r", None, prev, {"commit": "s9999"}, FIT_BOUND, log_fetch(OSError("network down")), compare.unfitted, log=[])
        self.assertEqual((g["beat_gaps_unread_from"], g["beat_gaps_unread_from_time"], g["beat_gaps_carried_n"]),
                         (prev["beat_gaps_unread_from"], "2026-09-30T12:00:00Z", 41))
        self.assertEqual((g["beat_gaps_reach"], g["beat_gaps_read"], g["beat_gaps_hole"]), (None, None, None))
        # so is one from a receipt written before the time was carried: there is nothing to date it by
        prev = {k: v for k, v in prev.items() if k not in ("beat_gaps_unread_from_time", "beat_gaps_carried_n")}
        compare.write(self.root, prev)
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", self.LONG, fetch_page=self.broken)
        self.assertEqual((r["beat_gaps_hole"], r["beat_gaps_unread_from"], r["beat_gaps_carried_n"], r["beat_gaps_reach"]),
                         (None, prev["beat_gaps_unread_from"], 1, self.REACH))

    def test_a_working_read_counts_only_back_to_the_reach(self):
        # the read stops at a whole page, so it can hold updates older than the reach; a start among them is a hole,
        # as it would be on a run whose read failed, not counted on one run and lost on another
        rows = []
        compare.read_log("o/r", None, 7, paged(self.LONG), rows_out=rows)
        older = [x for x in rows if x["after"] != compare.NO_SHA and t(x["timestamp"]) < t(self.REACH)]
        self.assertTrue(older)  # this record's read does hold some
        at = older[0]["timestamp"]
        compare.write(self.root, {"compared_at": "2026-09-30T00:05:00Z", "bound_version": 2,
                                  "beat": {"commit": older[0]["after"], "github_time": at}})
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", self.LONG)
        self.assertIsNone(r["quiet_fit_error"])
        self.assertEqual(r["beat_gaps_hole"], {"from_commit": older[0]["after"], "from": at, "to": "2026-10-09T18:00:00Z"})
        self.assertIn(f"older than {self.REACH}", r["beat_gaps_error"])
        self.assertEqual((r["beat_gaps"], r["beat_gaps_unread_from"], r["beat_gaps_read"]["by"]), (None, None, "long read"))
        # the log's stamp decides, not the commit's date: a branch restored to an old commit logs it anew
        restored = f"s{self.LONG.index(t('2026-10-05T12:00:00Z'))}"
        compare.write(self.root, {"compared_at": "2026-10-05T12:05:00Z", "bound_version": 2,
                                  "beat": {"commit": restored, "github_time": "2026-09-25T00:00:00Z"}})
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", self.LONG)
        self.assertEqual((r["beat_gaps_error"], r["beat_gaps_hole"], len(r["beat_gaps"])), (None, None, 4))
        # a start at the reach itself is counted
        at = self.REACH
        compare.write(self.root, {"compared_at": "2026-10-01T00:05:00Z", "bound_version": 2,
                                  "beat": {"commit": f"s{self.LONG.index(t(at))}", "github_time": at}})
        r = self.run_at("2026-10-09T18:00:00Z", "2026-10-09T18:05:00Z", self.LONG)
        self.assertEqual((r["beat_gaps_error"], r["beat_gaps_hole"]), (None, None))
        self.assertEqual(r["beat_gaps_n"], len(self.LONG) - 1 - self.LONG.index(t(at)))

    def test_a_gap_from_an_earlier_day_has_that_days_window(self):
        # asleep 05:00-11:00 only on 10-08 and 10-09. A silence that began on 10-09 is fitted from 10-02..10-08, with one
        # such night, so no window and 10-09's night ends stopped. The receipt at 10-10 01:00 is fitted from
        # 10-03..10-09, with two, so it has a window, which would have read that night quiet
        stamps = beats_every("2026-10-01T00:00:00Z", "2026-10-10T01:00:00Z",
                             lambda u: u >= t("2026-10-08T00:00:00Z") and hours("05:00", "11:00")(u))
        compare.write(self.root, {"compared_at": "2026-10-09T03:05:00Z", "beat": {"commit": f"s{stamps.index(t('2026-10-09T03:00:00Z'))}",
                                                                              "github_time": "2026-10-09T03:00:00Z"}})
        r = self.run_at("2026-10-10T01:00:00Z", "2026-10-10T01:05:00Z", stamps)
        self.assertEqual(r["quiet_utc"], [["04:15", "11:30"]])
        self.assertEqual(r["beat_gaps"], [{"from": "2026-10-09T04:45:00Z", "to": "2026-10-09T11:00:00Z", "silence_min": 375.0,
                                           "silence_outside_quiet_min": 375.0, "verdict_at_end": "stopped",
                                           "stopped_after": "2026-10-09T05:45:00Z", "quiet_utc": []}])

    def test_a_silence_from_before_what_was_read_is_not_fitted(self):
        # a record from 09-20: the read stops once it passes 10-01 00:00, seven days before the first page's oldest day
        long = beats_every("2026-09-20T00:00:00Z", "2026-10-09T18:00:00Z", hours("05:00", "11:00"))
        qwin, window_for = compare.quiet_window("o/r", None, {"github_time": "2026-10-09T18:00:00Z"}, FIT_BOUND, paged(long))
        self.assertEqual((qwin["quiet_utc"], qwin["quiet_fit"]["days_n"]), ([["04:15", "11:30"]], 7))
        with self.assertRaisesRegex(LookupError, "after the fit's first day"):
            window_for(t("2026-10-05T12:00:00Z"))


if __name__ == "__main__":
    unittest.main()
