"""Tests for beat_reference.py with a fake transport: python3 -m unittest test_beat_reference -v"""
import datetime as dt
import io
import json
import unittest
import urllib.error

import beat_reference as br


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_with(ref_exists=True):
    calls = []

    def opener(req, timeout):
        body = json.loads(req.data.decode()) if req.data else None
        calls.append((req.get_method(), req.full_url, body, timeout, req.get_header("Authorization")))
        if req.full_url.endswith("/git/trees"):
            return FakeResp(json.dumps({"sha": "tree1"}).encode())
        if req.full_url.endswith("/git/commits"):
            return FakeResp(json.dumps({"sha": "commit1"}).encode())
        if req.full_url.endswith("/refs/heads/beat"):
            if not ref_exists:
                raise urllib.error.HTTPError(req.full_url, 422, "Reference does not exist", {}, None)
            return FakeResp(b"{}")
        if req.full_url.endswith("/git/refs"):
            return FakeResp(b"{}")
        raise AssertionError(req.full_url)
    return opener, calls


NOW = dt.datetime(2026, 10, 22, 18, 0, 4, tzinfo=dt.timezone.utc)


class Beat(unittest.TestCase):
    def test_payload_is_the_contract(self):
        self.assertEqual(json.loads(br.payload(41, NOW, "2026-10-22T17:59:10Z", "session", "2026-10-22T17:20:00Z")),
                         {"seq": 41, "kernel_time": "2026-10-22T18:00:04Z", "last_poll": "2026-10-22T17:59:10Z",
                          "kernel_state": "session", "kernel_state_at": "2026-10-22T17:20:00Z"})
        self.assertEqual(json.loads(br.payload(1, NOW, None))["kernel_state"], None)

    def test_push_carries_the_scheduler_state(self):
        opener, calls = opener_with()
        br.push("o/r", "tok", 5, NOW, None, "halted", "2026-10-22T06:05:11Z", opener=opener)
        body = json.loads(calls[0][2]["tree"][0]["content"])
        self.assertEqual((body["kernel_state"], body["kernel_state_at"]), ("halted", "2026-10-22T06:05:11Z"))

    def test_three_calls_no_date_parentless_forced(self):
        opener, calls = opener_with()
        self.assertEqual(br.push("o/r", "tok", 41, NOW, None, opener=opener), "commit1")
        self.assertEqual([c[0] for c in calls], ["POST", "POST", "PATCH"])
        tree_body, commit_body, ref_body = calls[0][2], calls[1][2], calls[2][2]
        self.assertEqual(tree_body["tree"][0]["path"], "beat.json")
        self.assertEqual(json.loads(tree_body["tree"][0]["content"])["seq"], 41)
        self.assertEqual({k: commit_body[k] for k in ("message", "tree", "parents")}, {"message": "beat 41", "tree": "tree1", "parents": []})
        for who in ("author", "committer"):  # noreply address, and no date: GitHub dates the commit
            self.assertEqual(set(commit_body[who]), {"name", "email"})
            self.assertTrue(commit_body[who]["email"].endswith("@users.noreply.github.com"))
        self.assertEqual(ref_body, {"sha": "commit1", "force": True})
        self.assertTrue(all(c[3] == 10 for c in calls))
        self.assertTrue(all(c[4] == "Bearer tok" for c in calls))

    def test_first_beat_creates_the_ref(self):
        opener, calls = opener_with(ref_exists=False)
        br.push("o/r", "tok", 1, NOW, None, opener=opener)
        self.assertEqual(calls[-1][:3], ("POST", "https://api.github.com/repos/o/r/git/refs", {"ref": "refs/heads/beat", "sha": "commit1"}))

    def test_failure_raises_for_the_caller_to_log(self):
        def opener(req, timeout):
            raise urllib.error.URLError("offline")
        with self.assertRaises(urllib.error.URLError):
            br.push("o/r", "tok", 1, NOW, None, opener=opener)


if __name__ == "__main__":
    unittest.main()
