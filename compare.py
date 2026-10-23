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
A receipt is written on every run, including runs that could not read the beat, so that a run that compared
and failed is distinguishable from no run at all.
"""
from __future__ import annotations

import base64
import datetime as dt
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
    return {
        "commit": sha,
        "github_time": c["commit"]["committer"]["date"],
        "kernel_time": body.get("kernel_time"),
        "seq": body.get("seq"),
        "last_poll": body.get("last_poll"),
        "kernel_state": body.get("kernel_state"),
        "kernel_state_at": body.get("kernel_state_at"),
    }


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
    p = root / "latest.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
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


def build_receipt(now: dt.datetime, bound: dict, beat: dict | None, error: str | None, prev: dict | None, env: dict) -> dict:
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
        "beat": beat,
        **v,
        **between_beats(prev.get("beat") if prev else None, beat, float(bound.get("beat_period_min", 15))),
        "error": error,
        "previous_receipt_at": iso(prev_at) if prev_at else None,
        "since_previous_receipt_min": round((now - prev_at).total_seconds() / 60, 1) if prev_at else None,
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
    bound = json.loads((root / "bound.json").read_text(encoding="utf-8"))
    beat, error = None, None
    repo = env.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:  # without a repository the API call would 404 and read as "no beat"
        error = "no repository given: pass --repo OWNER/NAME or set GITHUB_REPOSITORY"
    else:
        try:
            beat = read_beat(repo, env.get("GITHUB_TOKEN"), fetch)
        except Exception as e:  # the receipt records the failure instead of the run disappearing
            error = f"{type(e).__name__}: {e}"[:300]
    return build_receipt(now, bound, beat, error, last_receipt(root), env)


if __name__ == "__main__":
    env = dict(os.environ)
    if "--repo" in sys.argv[1:-1]:
        env["GITHUB_REPOSITORY"] = sys.argv[sys.argv.index("--repo") + 1]
    receipt = run(HERE, env)
    print(json.dumps(receipt, indent=1))
    if "--write" in sys.argv:
        print("wrote", write(HERE, receipt))
