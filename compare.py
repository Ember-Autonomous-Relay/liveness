"""Compare Ember's last beat with the bound in bound.json and write one receipt.

Runs on GitHub Actions (see .github/workflows/compare.yml), a host other than the laptop Ember runs on, so a
receipt can be written and read while that laptop is down. Standard library only.

    python3 compare.py --repo OWNER/NAME     print the receipt a run would write now (no token needed for a
                                             public repository; run_id, event and head_sha print as null)
    python3 compare.py --write               in the workflow: GITHUB_REPOSITORY and GITHUB_TOKEN come from
                                             Actions; also append to receipts/<YYYY-MM>.jsonl, overwrite latest.json

Reads the beat through the GitHub API with the workflow's GITHUB_TOKEN: the tip of branch 'beat' (its commit
date is set by GitHub when the kernel creates the commit, because the kernel passes no date) and the beat.json
in it (the beat writer's clock, a sequence number, the last watch poll, and the scheduler's last state with the
time the scheduler wrote it). The verdict is taken
from the GitHub-side stamp; the kernel's stamp is printed beside it so a reader can see the difference.
A receipt is written on every run, including runs that could not read the beat or use what they read, so that
a run that compared and failed is distinguishable from no run at all.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
UTC = dt.timezone.utc


def parse(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hm(s: str) -> dt.timedelta:
    h, m = s.split(":")
    return dt.timedelta(hours=int(h), minutes=int(m))


def quiet_intervals(start: dt.datetime, end: dt.datetime, quiet_utc) -> list[tuple[dt.datetime, dt.datetime]]:
    """The quiet windows (daily, in UTC; a window whose end is before its start runs past midnight) that
    overlap [start, end), clipped to it."""
    out = []
    day = dt.datetime(start.year, start.month, start.day, tzinfo=UTC) - dt.timedelta(days=1)
    while day <= end:
        for a, b in quiet_utc:
            qa, qb = day + _hm(a), day + _hm(b)
            if qb <= qa:
                qb += dt.timedelta(days=1)
            lo, hi = max(qa, start), min(qb, end)
            if lo < hi:
                out.append((lo, hi))
        day += dt.timedelta(days=1)
    out.sort()
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for lo, hi in out:
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def outside_quiet_minutes(start: dt.datetime, end: dt.datetime, quiet_utc) -> float:
    """Minutes of [start, end) that fall outside the quiet windows."""
    if end <= start:
        return 0.0
    total = (end - start).total_seconds()
    quiet = sum((hi - lo).total_seconds() for lo, hi in quiet_intervals(start, end, quiet_utc))
    return (total - quiet) / 60


def verdict(beat_at: dt.datetime | None, now: dt.datetime, bound: dict) -> dict:
    """The comparison itself, with no I/O: silence since the beat, the part of it outside quiet hours, and
    one of running / quiet / stopped / no-beat."""
    b = float(bound["bound_min"])
    if beat_at is None:
        return {"verdict": "no-beat", "silence_min": None, "silence_outside_quiet_min": None, "bound_min": b}
    silence = (now - beat_at).total_seconds() / 60
    outside = outside_quiet_minutes(beat_at, now, bound.get("quiet_utc", []))
    if silence <= b:
        v = "running"
    elif outside < b:
        v = "quiet"
    else:
        v = "stopped"
    return {"verdict": v, "silence_min": round(silence, 1), "silence_outside_quiet_min": round(outside, 1), "bound_min": b}


# ---- I/O: the GitHub API and the receipt files ----

def api(path: str, token: str | None, repo: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}{path}", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "ember-comparer",
        **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def read_beat(repo: str, token: str | None, fetch=api) -> dict | None:
    """The tip of branch 'beat' and its beat.json, or None if the branch does not exist."""
    try:
        c = fetch("/commits/beat", token, repo)
    except urllib.error.HTTPError as e:
        if e.code not in (404, 422):
            raise
        fetch("", token, repo)  # raises if the repository itself is missing or hidden, which is not "no beat"
        return None
    sha = c["sha"]
    f = fetch(f"/contents/beat.json?ref={sha}", token, repo)
    body = json.loads(base64.b64decode(f["content"]).decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("beat.json is not a JSON object")
    beat = {
        "commit": sha,
        "github_time": c["commit"]["committer"]["date"],
        "kernel_time": body.get("kernel_time"),
        "seq": body.get("seq"),
        "last_poll": body.get("last_poll"),
        "kernel_state": body.get("kernel_state"),
        "kernel_state_at": body.get("kernel_state_at"),
    }
    check_beat(beat)
    return beat


def check_beat(beat: dict) -> None:
    """Raise ValueError naming the first field the comparison needs that is not in the form it uses: the times
    ISO 8601 or null (github_time required), seq an integer or null."""
    for k in ("github_time", "kernel_time", "kernel_state_at"):
        try:
            ok = parse(beat.get(k)) is not None or (k != "github_time" and beat.get(k) is None)
        except (TypeError, AttributeError, ValueError):
            ok = False
        if not ok:
            raise ValueError(f"beat {k} is not an ISO 8601 time: {beat.get(k)!r}"[:200])
    s = beat.get("seq")
    if s is not None and (isinstance(s, bool) or not isinstance(s, int)):
        raise ValueError(f"beat seq is not an integer: {s!r}"[:200])


def halted_reason(beat: dict, bound: dict) -> str | None:
    """Why a fresh beat still means the scheduler is not starting sessions, or None. The beat may come from a
    task other than the scheduler, so it carries the scheduler's last state and when the scheduler wrote it:
    a state named in halted_states, a non-session state the scheduler has not refreshed for state_stale_min
    (it fires every few minutes when it is up), or a session older than session_max_min."""
    st = beat.get("kernel_state")
    if st is None:
        return None
    if st in bound.get("halted_states", []):
        return f"kernel_state {st}"
    at, kt = parse(beat.get("kernel_state_at")), parse(beat.get("kernel_time"))
    if not (at and kt):
        return None
    age = (kt - at).total_seconds() / 60
    limit = bound.get("session_max_min") if st == "session" else bound.get("state_stale_min")
    if limit is not None and age > float(limit):
        return f"kernel_state {st} not refreshed for {age:.0f} min"
    return None


def last_receipt(root: pathlib.Path) -> dict | None:
    """latest.json, or None, as for a first run, if there is none or it is not a receipt this code can read."""
    p = root / "latest.json"
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(r, dict) or parse(r.get("compared_at")) is None:
            return None
        if r.get("beat") is not None:
            check_beat(r["beat"])
        return r
    except (FileNotFoundError, ValueError, TypeError, AttributeError):
        return None


def between_beats(prev_beat: dict | None, beat: dict | None, period_min: float) -> dict:
    """What happened between the beat the previous receipt saw and this one. The kernel makes one attempt per
    fixed UTC slot of beat_period_min and its seq counts attempts, so beats_missing_est (the stretch's length in
    slots, less the attempts made) estimates how much of it the kernel was not attempting, even when that began
    and ended between two receipts. It says how much, not when. Rounding absorbs where in its slot a beat lands."""
    out = {"seq_advance": None, "beat_minutes_between": None, "beats_missing_est": None}
    if not (prev_beat and beat) or prev_beat.get("seq") is None or beat.get("seq") is None:
        return out
    a, b = parse(prev_beat.get("github_time")), parse(beat.get("github_time"))
    adv = beat["seq"] - prev_beat["seq"]
    out["seq_advance"] = adv
    if a and b:
        mins = (b - a).total_seconds() / 60
        out["beat_minutes_between"] = round(mins, 1)
        if adv >= 0:  # a negative advance means the kernel's counter was reset; nothing to estimate
            out["beats_missing_est"] = max(0, round(mins / period_min) - adv)
    return out


def scheduled(event, attempt) -> bool:
    """A run GitHub's scheduler made: event schedule, first attempt. A re-run keeps event schedule but was asked
    for by someone; a workflow_dispatch run was asked for. Neither says how GitHub schedules the workflow."""
    return event == "schedule" and str(attempt or "1") == "1"


def scheduled_gaps(root: pathlib.Path, now: dt.datetime, event: str | None, attempt=None) -> dict:
    """The comparer's own largest completed gap so far: the longest stretch between consecutive scheduled
    receipts, other receipts skipped, with how many such gaps there are and when the longest one ended; the gap
    ending at this run counts if this run is scheduled. A reader whose newest scheduled receipt is older than
    this knows the comparer is later than it has been here so far, with one subtraction. Read from
    receipts/*.jsonl, at the stored precision, so anyone can recompute it from the same files. A line that is not
    JSON (cut short, a conflict marker) is skipped; any other line it cannot read leaves the three fields null and
    scheduled_gaps_error naming the line, since skipping a scheduled receipt would widen a gap unseen."""
    times, where = [], "receipts/"
    try:
        for p in sorted((root / "receipts").glob("*.jsonl")):
            where = f"receipts/{p.name}"
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                where = f"receipts/{p.name} line {i}"
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if scheduled(r.get("event"), r.get("run_attempt")):
                    at = parse(r.get("compared_at"))
                    if at is None:
                        raise ValueError("a scheduled receipt without compared_at")
                    times.append(at)
    except Exception as e:  # the receipt is still written, with the reason, as when the beat cannot be read
        return {"scheduled_gaps_n": None, "scheduled_gap_max_min": None, "scheduled_gap_max_ended_at": None,
                "scheduled_gaps_error": f"{where}: {type(e).__name__}: {e}"[:300]}
    if scheduled(event, attempt):
        times.append(parse(iso(now)))  # compared_at is stored to the second
    times.sort()
    gaps = [(b - a, b) for a, b in zip(times, times[1:])]
    if not gaps:
        return {"scheduled_gaps_n": 0, "scheduled_gap_max_min": None, "scheduled_gap_max_ended_at": None,
                "scheduled_gaps_error": None}
    g, end = max(gaps)
    return {"scheduled_gaps_n": len(gaps), "scheduled_gap_max_min": round(g.total_seconds() / 60, 1),
            "scheduled_gap_max_ended_at": iso(end), "scheduled_gaps_error": None}


def stopped_after(start: dt.datetime, end: dt.datetime, bound: dict) -> dt.datetime | None:
    """The moment in [start, end] at which the minutes of a silence since start that fall outside the quiet
    windows reach bound_min, or None if they never do: a reading after that moment, before end, reads stopped."""
    need, at = float(bound["bound_min"]) * 60, start
    for lo, hi in quiet_intervals(start, end, bound.get("quiet_utc", [])) + [(end, end)]:
        if (lo - at).total_seconds() >= need:
            return at + dt.timedelta(seconds=need)
        need -= (lo - at).total_seconds()
        at = hi
    return None


BEAT_GAP_FIELDS = ("beat_gaps", "beat_gaps_n", "beat_gap_max_min", "beat_gaps_error", "beat_gaps_unread_from")
NO_SHA = "0" * 40


def beat_gaps(repo: str, token: str | None, prev: dict | None, beat: dict | None, bound: dict, fetch=api) -> dict:
    """The silences since the beat the previous receipt saw, up to this run's beat, which no receipt may have seen,
    read from GitHub's activity log for branch 'beat' (one entry per update of the branch, with GitHub's own stamp;
    one call, the newest 100 updates). beat_gaps lists each gap longer than bound_min with both ends, its silence,
    the part outside the quiet windows, the verdict it had reached when it ended and when it first read stopped;
    beat_gaps_n counts the gaps of any length and beat_gap_max_min is the longest. If they cannot be read, those
    fields are null and beat_gaps_error says why; the verdict does not depend on them. A run that could not read
    them names the commit they start from in beat_gaps_unread_from, and the next run starts there instead, so a
    failed read loses nothing while the log still holds that commit."""
    out = dict.fromkeys(BEAT_GAP_FIELDS)
    carried = (prev or {}).get("beat_gaps_unread_from")
    start = carried if isinstance(carried, str) and carried else ((prev or {}).get("beat") or {}).get("commit")
    if not beat:
        return dict(out, beat_gaps_error="no beat on this run", beat_gaps_unread_from=start)
    if not start:
        return dict(out, beat_gaps_error="no beat on the previous receipt to start from")
    if start == beat["commit"]:  # no update since; the silence still open is the verdict's
        return dict(out, beat_gaps=[], beat_gaps_n=0)
    try:
        log = fetch("/activity?ref=refs/heads/beat&per_page=100", token, repo)
        rows = [r for r in reversed(log) if r["after"] != NO_SHA]  # served newest first; a deletion is no beat
        shas = [r["after"] for r in rows]
        if beat["commit"] not in shas:
            raise LookupError(f"this run's beat {beat['commit'][:12]} is not in the log yet")
        j = len(shas) - 1 - shas[::-1].index(beat["commit"])
        if start in shas[j + 1:]:
            raise LookupError(f"the beat to start from ({start[:12]}) is newer in the log than this run's beat")
        if start not in shas[:j]:
            out["beat_gaps_unread_from"] = None  # gone from the window for good: the next run starts from this beat
            return dict(out, beat_gaps_error=f"the beat the gaps start from ({start[:12]}) is not among the {j} updates"
                        f" before this run's beat in the log, which go back to {rows[0]['timestamp']}")
        i = j - 1 - shas[:j][::-1].index(start)
        span = rows[i:j + 1]
        for a, b in zip(span, span[1:]):
            # an update missing from the log would join two gaps into one; a re-created branch starts afresh
            if b.get("before") != a["after"] and not (b.get("before") == NO_SHA and b.get("activity_type") == "branch_creation"):
                raise ValueError(f"the update at {b.get('timestamp')} does not follow the one before it in the log")
        listed, longest = [], 0.0
        for a, b in zip(span, span[1:]):
            s, e = parse(a["timestamp"]), parse(b["timestamp"])
            v = verdict(s, e, bound)
            longest = max(longest, v["silence_min"])
            if v["verdict"] != "running":
                st = stopped_after(s, e, bound)
                listed.append({"from": iso(s), "to": iso(e), "silence_min": v["silence_min"],
                               "silence_outside_quiet_min": v["silence_outside_quiet_min"],
                               "verdict_at_end": v["verdict"], "stopped_after": iso(st) if st else None})
        return dict(out, beat_gaps=listed, beat_gaps_n=len(span) - 1, beat_gap_max_min=longest)
    except Exception as e:  # the receipt is still written, with the reason, as for scheduled_gaps
        return dict(out, beat_gaps_error=f"{type(e).__name__}: {e}"[:300], beat_gaps_unread_from=start)


def build_receipt(now: dt.datetime, bound: dict, beat: dict | None, error: str | None, prev: dict | None, env: dict,
                  bound_sha256: str | None = None, gaps: dict | None = None, bgaps: dict | None = None) -> dict:
    beat_at = parse(beat["github_time"]) if beat else None
    v = verdict(beat_at, now, bound) if error is None else {
        "verdict": "error", "silence_min": None, "silence_outside_quiet_min": None, "bound_min": float(bound["bound_min"])}
    reason = halted_reason(beat, bound) if beat and v["verdict"] == "running" else None
    if reason:
        v["verdict"] = "halted"  # the beat is fresh, but the scheduler is not starting sessions
    v["halted_reason"] = reason
    if beat:
        k = parse(beat.get("kernel_time"))
        beat = dict(beat, kernel_minus_github_s=round((k - beat_at).total_seconds()) if k and beat_at else None)
    prev_at = parse(prev.get("compared_at")) if prev else None
    return {
        "compared_at": iso(now),
        "run_id": env.get("GITHUB_RUN_ID"),
        "run_attempt": env.get("GITHUB_RUN_ATTEMPT"),
        "event": env.get("GITHUB_EVENT_NAME"),
        "head_sha": env.get("GITHUB_SHA"),
        "bound_version": bound.get("version"),
        "bound_sha256": bound_sha256,  # SHA-256 of the bound.json bytes this run read; a reader can hash the file at head_sha and compare
        "beat": beat,
        **v,
        **between_beats(prev.get("beat") if prev else None, beat, float(bound.get("beat_period_min", 15))),
        **(bgaps or dict.fromkeys(BEAT_GAP_FIELDS)),
        "error": error,
        "previous_receipt_at": iso(prev_at) if prev_at else None,
        "since_previous_receipt_min": round((now - prev_at).total_seconds() / 60, 1) if prev_at else None,
        **(gaps or {"scheduled_gaps_n": None, "scheduled_gap_max_min": None, "scheduled_gap_max_ended_at": None,
                    "scheduled_gaps_error": None}),
    }


def write(root: pathlib.Path, receipt: dict) -> pathlib.Path:
    d = root / "receipts"
    d.mkdir(exist_ok=True)
    p = d / (receipt["compared_at"][:7] + ".jsonl")
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(receipt, ensure_ascii=False, sort_keys=False) + "\n")
    (root / "latest.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return p


def run(root: pathlib.Path, env: dict, now: dt.datetime | None = None, fetch=api) -> dict:
    now = now or dt.datetime.now(UTC)
    bound_bytes = (root / "bound.json").read_bytes()
    bound = json.loads(bound_bytes.decode("utf-8"))
    beat, error = None, None
    repo = env.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:  # without a repository the API call would 404 and read as "no beat"
        error = "no repository given: pass --repo OWNER/NAME or set GITHUB_REPOSITORY"
    else:
        try:
            beat = read_beat(repo, env.get("GITHUB_TOKEN"), fetch)
        except Exception as e:  # the receipt records the failure instead of the run disappearing
            error = f"{type(e).__name__}: {e}"[:300]
    sha = hashlib.sha256(bound_bytes).hexdigest()
    gaps = scheduled_gaps(root, now, env.get("GITHUB_EVENT_NAME"), env.get("GITHUB_RUN_ATTEMPT"))
    prev = last_receipt(root)
    bgaps = beat_gaps(repo, env.get("GITHUB_TOKEN"), prev, beat, bound, fetch)
    try:
        return build_receipt(now, bound, beat, error, prev, env, sha, gaps, bgaps)
    except Exception as e:  # after check_beat and last_receipt, most likely a bound.json value; still a receipt
        error = ((error + "; ") if error else "") + f"could not build the receipt: {type(e).__name__}: {e}"
        if beat and not bgaps["beat_gaps_unread_from"]:  # this receipt will not show the beat, so keep where to start
            bgaps = dict(bgaps, beat_gaps_unread_from=beat["commit"])
        return build_receipt(now, bound, None, error[:300], None, env, sha, gaps, bgaps)


if __name__ == "__main__":
    env = dict(os.environ)
    if "--repo" in sys.argv[1:-1]:
        env["GITHUB_REPOSITORY"] = sys.argv[sys.argv.index("--repo") + 1]
    receipt = run(HERE, env)
    print(json.dumps(receipt, indent=1))
    if "--write" in sys.argv:
        print("wrote", write(HERE, receipt))
