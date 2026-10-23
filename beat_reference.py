"""A reference beat writer: the push() a scheduler calls to make one beat that compare.py can read. Ember's own
beat is made by its kernel, not by this file. This file fixes the contract:

  - branch 'beat' holds exactly one commit, replaced on every beat (the ref is force-updated to a new parentless
    commit), so beats add no history;
  - that commit holds one file, beat.json: {"seq": int, "kernel_time": "...Z", "last_poll": "...Z" | null,
    "kernel_state": str | null, "kernel_state_at": "...Z" | null};
  - the commit is created through GitHub's API with a fixed noreply author and committer and no date, so GitHub
    dates it with its own clock; compare.py takes the verdict from that date and prints kernel_time beside it. (With
    no author given, GitHub would use the account's own email address. AUTHOR below is Ember's; use your own.)

What the caller supplies, because this file does not:
  - timing: one call per fixed UTC slot of bound.json's beat_period_min (Ember: :00, :15, :30, :45), from a task
    that keeps running while the agent's sessions run;
  - seq: a counter of attempts kept across restarts, so that a failed attempt uses a number and a shortfall
    between two receipts means nothing was attempting; a slot skipped on purpose (a test hold) uses none;
  - kernel_state and kernel_state_at: copied from what the scheduler itself last wrote, if the beat is not made by
    the scheduler (otherwise a halted scheduler looks alive);
  - a token with Contents: write on the one repository, and nothing else;
  - a wall-clock deadline (a subprocess with timeout=30, say): urllib's 10 s timeout is per socket operation and
    does not cover a DNS lookup.

Three API calls a beat (four on the first), no retry: a failed beat is a missed beat, which is what the bound is
for. Standard library only.

    python beat_reference.py --dry-run   print the requests a beat would make
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.error
import urllib.request


def payload(seq: int, now: dt.datetime, last_poll: str | None, kernel_state: str | None = None,
            kernel_state_at: str | None = None) -> str:
    """kernel_state is one word the scheduler writes for what it is doing (idle, session,
    backoff, halted, paused) and kernel_state_at is when it last wrote it. A beat written by a task other than
    the scheduler carries both, so compare.py can tell a live laptop from a live scheduler. null if not sent."""
    return json.dumps({"seq": seq, "kernel_time": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "last_poll": last_poll, "kernel_state": kernel_state, "kernel_state_at": kernel_state_at},
                      separators=(",", ":")) + "\n"


# The account's noreply address (its public id is 322218329); no date, so GitHub dates the commit itself.
AUTHOR = {"name": "ember-kernel", "email": "322218329+Ember-Autonomous-Relay@users.noreply.github.com"}


def requests_for(repo: str, text: str, seq: int) -> list[tuple[str, str, dict | None]]:
    """The three calls, with placeholders for the shas the earlier calls return."""
    base = f"https://api.github.com/repos/{repo}/git"
    return [
        ("POST", f"{base}/trees", {"tree": [{"path": "beat.json", "mode": "100644", "type": "blob", "content": text}]}),
        ("POST", f"{base}/commits", {"message": f"beat {seq}", "tree": "<tree sha>", "parents": [],
                                     "author": dict(AUTHOR), "committer": dict(AUTHOR)}),
        ("PATCH", f"{base}/refs/heads/beat", {"sha": "<commit sha>", "force": True}),
    ]


def _call(method: str, url: str, body: dict | None, token: str, opener=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "ember-kernel-beat", "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"})
    with opener(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def push(repo: str, token: str, seq: int, now: dt.datetime, last_poll: str | None, kernel_state: str | None = None,
         kernel_state_at: str | None = None, opener=urllib.request.urlopen) -> str:
    """One beat. Returns the new commit sha; raises on any failure (the caller logs it and moves on)."""
    text = payload(seq, now, last_poll, kernel_state, kernel_state_at)
    (m1, u1, b1), (m2, u2, b2), (m3, u3, b3) = requests_for(repo, text, seq)
    tree = _call(m1, u1, b1, token, opener)["sha"]
    commit = _call(m2, u2, dict(b2, tree=tree), token, opener)["sha"]
    try:
        _call(m3, u3, dict(b3, sha=commit), token, opener)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 422):  # the ref does not exist yet (first beat): create it
            raise
        _call("POST", u3.rsplit("/refs/", 1)[0] + "/refs", {"ref": "refs/heads/beat", "sha": commit}, token, opener)
    return commit


if __name__ == "__main__":
    if "--dry-run" in sys.argv:
        now = dt.datetime.now(dt.timezone.utc)
        for m, u, b in requests_for("Ember-Autonomous-Relay/<repo>", payload(1, now, None), 1):
            print(m, u, json.dumps(b))
