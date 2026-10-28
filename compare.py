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
The quiet window is either given in bound.json (quiet_utc) or fitted on each run, by the rule in its quiet_fit, from
GitHub's activity log for branch 'beat' (fit_quiet), and the receipt prints the window it used and how the silences
that ended on the fit's days were judged (ended_silences). bound.json gives the bound and the window's margin in beat
periods, and load_bound derives their minutes.
A receipt is written on every run, including runs that could not read the beat or use what they read, so that
a run that compared and failed is distinguishable from no run at all.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import time
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


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def load_bound(raw) -> dict:
    """bound.json as the comparer uses it, or ValueError saying what it cannot use. The bound and the quiet window's
    margin are given as counts of beat periods (bound_beats, quiet_fit's margin_beats), the unit the record is made of,
    and their minutes (bound_min, margin_min) are derived here from beat_period_min, so a change of period cannot
    change them unseen (rushipingan, 65110ba3 on the Colony). Refused: minutes given in their place, at the top level
    or in quiet_fit; a bound under 2 periods, since one period is the silence a beat on time leaves and a beat a
    second late would read stopped; a margin under 1, since a silence's ends are known only to within a beat period
    and the same night can begin or end a period earlier or later in the record; and a margin that is not whole
    minutes, which the fit counts in."""
    if not isinstance(raw, dict):
        raise ValueError("bound.json is not a JSON object")
    period = raw.get("beat_period_min")
    if not (isinstance(period, (int, float)) and not isinstance(period, bool) and 0 < period < math.inf):
        raise ValueError(f"beat_period_min is not a positive number: {period!r}"[:200])
    rule = raw.get("quiet_fit")
    if rule is not None and not isinstance(rule, dict):
        raise ValueError("quiet_fit is not a JSON object")
    for where, d in (("bound.json", raw), ("quiet_fit", rule or {})):
        for k in ("bound_min", "margin_min"):
            if k in d:
                raise ValueError(f"{where} gives {k}; give bound_beats and margin_beats, from which the minutes are derived")
    n = raw.get("bound_beats")
    if not (_count(n) and n >= 2 and math.isfinite(n * period)):
        raise ValueError(f"bound_beats is not a whole number of at least 2: {n!r}"[:200])
    b = dict(raw, bound_min=n * period)
    if rule is not None:
        m = rule.get("margin_beats")
        if not (_count(m) and m >= 1):
            raise ValueError(f"quiet_fit margin_beats is not a whole number of at least 1: {m!r}"[:200])
        if not math.isfinite(m * period) or m * period != int(m * period):
            raise ValueError(f"margin_beats {m} x beat_period_min {period} is not a whole number of minutes")
        b["quiet_fit"] = dict(rule, margin_min=int(m * period))
    return b


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

API = "https://api.github.com/"


def api_page(path: str, token: str | None, repo: str) -> tuple:
    """One answer and the URL of the next page from its Link header, or None. path is a path under the repository
    or a next-page URL from an earlier answer, which must be on GitHub's API host, since the token goes with it."""
    url = path if path.startswith("https://") else f"{API}repos/{repo}{path}"
    if not url.startswith(API):
        raise ValueError(f"a next page off GitHub's API host: {url[:100]}")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "ember-comparer",
        **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link") or "")
        return json.loads(r.read().decode("utf-8")), (m.group(1) if m else None)


def api(path: str, token: str | None, repo: str) -> dict:
    return api_page(path, token, repo)[0]


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
            for i, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):  # not splitlines(): see ended_silences
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


BEAT_GAP_FIELDS = ("beat_gaps", "beat_gaps_n", "beat_gap_max_min", "beat_gaps_error", "beat_gaps_unread_from",
                   "beat_gaps_log_from", "beat_gaps_log_margin_n", "beat_gaps_hole")
NO_SHA = "0" * 40


def beat_gaps(repo: str, token: str | None, prev: dict | None, beat: dict | None, bound: dict, fetch=api, window_for=None,
              log: list | None = None) -> dict:
    """The silences since the beat the previous receipt saw, up to this run's beat, which no receipt may have seen,
    read from GitHub's activity log for branch 'beat' (one entry per update of the branch, with GitHub's own stamp).
    With log (the updates quiet_window read for the fit, oldest first: about 9 days, or the whole log), the gaps are
    counted from that; without it, from one call, the newest 100 updates (Fieldnotes, 9b167290). An empty log means
    the fit's read failed, so a start not in the one call is carried to the next run, not counted as a hole. beat_gaps lists each gap longer than bound_min with both ends, its silence,
    the part outside the quiet windows, the verdict it had reached when it ended and when it first read stopped;
    beat_gaps_n counts the gaps of any length and beat_gap_max_min is the longest. If they cannot be read, those
    fields are null and beat_gaps_error says why; the verdict does not depend on them. A run that could not read
    them names the commit they start from in beat_gaps_unread_from, and the next run starts there instead, so a
    failed read loses nothing while the log still holds that commit. With window_for (see quiet_window), each gap
    longer than bound_min is judged by the window fitted for a silence that began when it did; if none could be
    fitted, its verdict_at_end is unknown and its outside-quiet minutes and stopped_after are null.
    Whenever the log was read, beat_gaps_log_from is the stamp of the oldest update in what was read, and on a
    read that found the beat to start from, beat_gaps_log_margin_n is the number of updates read that are older
    than it: how near that beat came to falling out of the read (Fieldnotes, 2339a7f6 on the Colony). If it had
    fallen out, beat_gaps_hole says which stretch no receipt will count: from that beat to this run's beat."""
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
        if log:
            how, served = f"the quiet fit's read, {len(log)} updates", log
        else:  # served newest first
            how, served = "one call, up to 100 updates", list(reversed(fetch("/activity?ref=refs/heads/beat&per_page=100", token, repo)))
        rows = [r for r in served if r["after"] != NO_SHA]  # a deletion is no beat
        out["beat_gaps_log_from"] = rows[0]["timestamp"] if rows else None
        shas = [r["after"] for r in rows]
        if beat["commit"] not in shas:
            raise LookupError(f"this run's beat {beat['commit'][:12]} is not in the log yet")
        j = len(shas) - 1 - shas[::-1].index(beat["commit"])
        if start in shas[j + 1:]:
            raise LookupError(f"the beat to start from ({start[:12]}) is newer in the log than this run's beat")
        if start not in shas[:j]:
            if log is not None and not log:  # the fit's read failed: the start may be in it next run, so it is carried
                raise LookupError(f"the beat the gaps start from ({start[:12]}) is not among the {j} updates before this run's"
                                  f" beat in one call, and the quiet fit's read of the log failed (see quiet_fit_error)")
            out["beat_gaps_unread_from"] = None  # gone from the window for good: the next run starts from this beat
            pb = (prev or {}).get("beat") or {}
            # none of the stretch is counted, the part still in the read included: a count from there would be short
            out["beat_gaps_hole"] = {"from_commit": start, "from": pb.get("github_time") if pb.get("commit") == start else None,
                                     "to": rows[j]["timestamp"]}
            return dict(out, beat_gaps_error=f"the beat the gaps start from ({start[:12]}) is not among the {j} updates"
                        f" before this run's beat in what was read of the log ({how}), which goes back"
                        f" to {rows[0]['timestamp']}; see beat_gaps_hole")
        i = j - 1 - shas[:j][::-1].index(start)
        out["beat_gaps_log_margin_n"] = i
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
                gb = bound
                if window_for is not None:
                    try:
                        gb = dict(bound, quiet_utc=window_for(s))
                    except Exception:
                        listed.append({"from": iso(s), "to": iso(e), "silence_min": v["silence_min"], "silence_outside_quiet_min": None,
                                       "verdict_at_end": "unknown", "stopped_after": None, "quiet_utc": None})
                        continue
                    v = verdict(s, e, gb)
                st = stopped_after(s, e, gb)
                listed.append({"from": iso(s), "to": iso(e), "silence_min": v["silence_min"],
                               "silence_outside_quiet_min": v["silence_outside_quiet_min"],
                               "verdict_at_end": v["verdict"], "stopped_after": iso(st) if st else None,
                               **({"quiet_utc": gb["quiet_utc"]} if window_for is not None else {})})
        return dict(out, beat_gaps=listed, beat_gaps_n=len(span) - 1, beat_gap_max_min=longest)
    except Exception as e:  # the receipt is still written, with the reason, as for scheduled_gaps
        return dict(out, beat_gaps_error=f"{type(e).__name__}: {e}"[:300], beat_gaps_unread_from=start)


# ---- the quiet window, fitted from the beat record by the rule in bound.json's quiet_fit ----

DAY = dt.timedelta(days=1)


def midnight(t: dt.datetime) -> dt.datetime:
    return dt.datetime(t.year, t.month, t.day, tzinfo=UTC)


def read_log(repo: str, token: str | None, days: int, fetch_page=api_page, max_pages: int = 30, max_s: float = 90,
             rows_out: list | None = None) -> tuple:
    """The stamps of the beat branch's updates in GitHub's activity log, oldest first, and whether the read reached
    the log's end. Read a page of 100 at a time from the newest, until one is at or before `days` whole days before
    the day of the oldest update on the first page, or the log ends. The first page is the one beat_gaps reads in one
    call, so every gap from there can be fitted from what is read; counting from the whole read, a gap from its first
    `days` days may not be, and reads unknown. A deletion is no beat. An update whose 'before' is not the
    'after' of the one before it means one is missing from what was read, which would join two silences into one,
    so it raises, as in beat_gaps; so do an empty log, max_pages pages, and max_s seconds of reading. When the read
    succeeds, the updates themselves, oldest first, are added to rows_out, for beat_gaps to count from."""
    rows, path, back_to, began = [], "/activity?ref=refs/heads/beat&per_page=100", None, time.monotonic()
    for _ in range(max_pages):
        page, path = fetch_page(path, token, repo)
        if not isinstance(page, list):
            raise TypeError(f"the log answered with a {type(page).__name__}, not a list")
        rows += page
        if not page and path:
            raise LookupError("the log served an empty page with a next page after it")
        if not path:
            break
        back_to = back_to or midnight(parse(page[-1]["timestamp"])) - days * DAY
        if parse(page[-1]["timestamp"]) <= back_to:
            break
        if time.monotonic() - began > max_s:
            raise TimeoutError(f"reading the log took over {max_s:g} s ({len(rows)} updates)")
    else:
        raise LookupError(f"the log reaches back further than {max_pages} pages")
    if not rows:
        raise LookupError("the log is empty")
    rows.reverse()
    for a, b in zip(rows, rows[1:]):
        if b.get("before") != a["after"] and not (b.get("before") == NO_SHA and b.get("activity_type") == "branch_creation"):
            raise ValueError(f"the update at {b.get('timestamp')} does not follow the one before it in the log")
    stamps = [parse(r["timestamp"]) for r in rows if r["after"] != NO_SHA]
    if rows_out is not None:
        rows_out.extend(rows)
    return stamps, path is None


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def fit_quiet(beats: list[dt.datetime], start: dt.datetime, rule: dict, bound_min: float) -> dict:
    """The quiet window for a silence that began at start, by the rule (bound.json's quiet_fit), from the beat stamps
    (oldest first). Days are UTC days. The fit uses the `days` whole days before the day the silence began that the
    stamps cover from end to end. A silence between two beats longer than bound_min, and no longer than
    longest_sleep_min, may be a sleep; a minute of the day joins the window when such silences covered it on at
    least `recur_days` of those days, and the window is then widened by margin_min at each edge. A window of the
    whole day raises, since under it no silence would ever read stopped. Returns quiet_utc (the window, as in
    bound.json) and fit (what it was fitted from, with a line saying why the outages it lists were not counted)."""
    days, recur = int(rule["days"]), int(rule["recur_days"])
    margin, longest = int(rule["margin_min"]), float(rule["longest_sleep_min"])
    # under a day, so that no one gap covers a minute of the day twice and makes a window by itself
    if not (1 <= recur <= days and margin >= 0 and bound_min < longest < 1440):
        raise ValueError(f"quiet_fit values it cannot use: {rule}"[:200])
    day0 = midnight(start)
    covered = [d for d in (day0 - k * DAY for k in range(days, 0, -1)) if beats and beats[0] <= d and beats[-1] >= d + DAY]
    fit = {"days_n": len(covered), "from": None, "to": None, "silences": [], "outages": [],
           "outages_reason": f"gaps longer than longest_sleep_min ({longest:g} min) are taken as outages and not counted in the window",
           "longest_silence_min": None}
    if not covered:
        return {"quiet_utc": [], "fit": fit}
    lo, hi = covered[0], covered[-1] + DAY
    gaps = [(a, b) for a, b in zip(beats, beats[1:]) if a < hi and b > lo]
    fit.update({"from": iso(lo), "to": iso(hi), "longest_silence_min": round(max((b - a).total_seconds() for a, b in gaps) / 60, 1)})
    sleeps = []
    for a, b in gaps:
        mins = (b - a).total_seconds() / 60
        if mins > bound_min:
            (sleeps if mins <= longest else fit["outages"]).append((a, b))
    fit["outages"] = [{"from": iso(a), "to": iso(b), "silence_min": round((b - a).total_seconds() / 60, 1)} for a, b in fit["outages"]]
    fit["silences"] = [{"from": iso(a), "to": iso(b), "silence_min": round((b - a).total_seconds() / 60, 1)} for a, b in sleeps]
    count = [0] * 1440
    for d in covered:
        hit = [False] * 1440
        for a, b in sleeps:  # the minutes [d+m, d+m+1) that lie wholly inside the silence
            first = -int(-(a - d).total_seconds() // 60)  # ceiling
            last = int((b - d).total_seconds() // 60) - 1
            for m in range(max(first, 0), min(last, 1439) + 1):
                hit[m] = True
        count = [c + h for c, h in zip(count, hit)]
    core = [c >= recur for c in count]
    win = [any(core[(m + k) % 1440] for k in range(-margin, margin + 1)) for m in range(1440)] if any(core) else core
    if all(win):  # a window of the whole day would never read stopped
        raise LookupError(f"the fitted window covers the whole day, from the {len(covered)} days {iso(lo)} to {iso(hi)}")
    out, z = [], win.index(False)
    m = z + 1
    while m <= z + 1440:  # from just after a minute outside the window, once round the day
        if win[m % 1440]:
            s = m
            while win[m % 1440]:
                m += 1
            out.append([hhmm(s % 1440), hhmm(m % 1440)])
        m += 1
    return {"quiet_utc": sorted(out), "fit": fit}


def unfitted(_start):
    raise LookupError("the quiet window could not be fitted on this run")


def quiet_window(repo: str, token: str | None, beat: dict | None, bound: dict, fetch_page=api_page,
                 log_out: list | None = None) -> tuple:
    """The quiet window this run judges by, and a function giving the window for a silence that began at a given
    moment (for beat_gaps). A bound with quiet_utc gives that window. A bound with quiet_fit has it fitted from the
    activity log; if the log cannot be read or used, or does not reach back far enough for the silence, quiet_utc is
    null with quiet_fit_error saying why, and a silence longer than bound_min reads unknown. The updates read, when
    the read succeeded, are added to log_out (see read_log), even if the fit then failed."""
    rule = bound.get("quiet_fit")
    if rule is None:
        return {"quiet_utc": bound.get("quiet_utc", []), "quiet_fit": None, "quiet_fit_error": None}, None
    out = {"quiet_utc": None, "quiet_fit": None, "quiet_fit_error": None}
    try:
        if "quiet_utc" in bound:
            raise ValueError("bound.json has both quiet_utc and quiet_fit")
        if not beat:
            raise LookupError("no beat on this run")
        days, bound_min = int(rule["days"]), float(bound["bound_min"])
        beats, complete = read_log(repo, token, days, fetch_page, rows_out=log_out)

        def fitted(s: dt.datetime) -> dict:
            if not complete and beats and beats[0] > midnight(s) - days * DAY:
                raise LookupError(f"the log as read starts at {iso(beats[0])}, after the fit's first day for a silence from {iso(s)}")
            return fit_quiet(beats, s, rule, bound_min)
        f = fitted(parse(beat["github_time"]))
        return dict(out, quiet_utc=f["quiet_utc"], quiet_fit=f["fit"]), (lambda s: fitted(s)["quiet_utc"])
    except Exception as e:  # the receipt is still written; only a silence the window would decide reads unknown
        return dict(out, quiet_fit_error=f"{type(e).__name__}: {e}"[:300]), unfitted


ENDED_FIELDS = ("ended_silences", "ended_silences_error")
VERDICTS_AT_END = ("stopped", "quiet", "unknown")


ENDED_LIMITS = ("each silence in stopped_n was a sleep the window missed or a stop that ended, and the count cannot tell which;"
                 " quiet_n may hold stops the window absorbed, so the counts can show the window too narrow but not too wide;"
                 " stopped_outages_n are the silences in stopped_n longer than longest_sleep_min, taken as outages, so however"
                 " often they recurred they would never widen the window; a silence that has not ended is not counted")


def ended_silences(root: pathlib.Path, now: dt.datetime, beat: dict | None, bound: dict, bgaps: dict | None) -> dict:
    """How the comparer judged the silences that ended in a beat on the days the fit draws on: the `days` whole UTC
    days before the day of the beat this run read (of now, if it read none). Counted from the beat_gaps entries on
    the receipts in receipts/*.jsonl judged under this bound's version, then this run's, whose silence ended (`to`)
    on those days, each silence once, as first listed: stopped_n, quiet_n and unknown_n by verdict_at_end, and
    stopped_outages_n, those in stopped_n longer than longest_sleep_min, which the fit takes as outages, so their
    recurring would never widen the window. These are silences, not
    readings: verdict_at_end is what a silence had reached when a beat ended it, which no run may have read at the
    time. rushipingan asked for a count printed on every receipt that gives the window's failures an owner, since
    no refit date does (65110ba3 on the Colony); its limits are printed with it (ENDED_LIMITS). holes_n counts the
    receipts' beat_gaps_hole stretches that reach those days, whose silences are on no receipt, and series_from is
    the oldest receipt under this version, so a reader can see when the counts cover less than the days. Null under
    fixed hours, which no fit uses. A line that is not JSON is skipped, as in scheduled_gaps; any other line it
    cannot read leaves ended_silences null and ended_silences_error naming it, since skipping a receipt would hide
    its silences unseen."""
    rule = bound.get("quiet_fit")
    if rule is None:
        return dict.fromkeys(ENDED_FIELDS)
    where = "bound.json quiet_fit"
    try:
        ref = parse(beat["github_time"]) if beat else now
        hi = midnight(ref)
        lo = hi - int(rule["days"]) * DAY
        longest = float(rule["longest_sleep_min"])
        found = []
        for p in sorted((root / "receipts").glob("*.jsonl")):
            where = f"receipts/{p.name}"
            # split on newlines only: splitlines() would also split a receipt at a U+2028 inside a string
            for i, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
                where = f"receipts/{p.name} line {i}"
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    raise ValueError("a line that is not a receipt")
                if r.get("bound_version") == bound.get("version"):
                    found.append((where, r.get("compared_at"), r.get("beat_gaps"), r.get("beat_gaps_hole")))
        found.append(("this run", iso(now), (bgaps or {}).get("beat_gaps"), (bgaps or {}).get("beat_gaps_hole")))
        seen, holes, first = set(), set(), None
        counts = dict.fromkeys(("stopped", "stopped_outages", "quiet", "unknown"), 0)
        for where, at, gaps, hole in found:
            at = parse(at)  # raises on a stamp that is not ISO 8601
            if at is None:
                raise ValueError("a receipt without compared_at")
            first = min(first, at) if first else at
            if gaps is not None and not isinstance(gaps, list):
                raise ValueError("beat_gaps is not a list")
            for g in gaps or []:
                start, end, v = parse(g["from"]), parse(g["to"]), g["verdict_at_end"]
                if v not in VERDICTS_AT_END:
                    raise ValueError(f"a verdict_at_end it does not know: {v!r}"[:100])
                if lo <= end < hi and (g["from"], g["to"]) not in seen:
                    seen.add((g["from"], g["to"]))
                    counts[v] += 1
                    counts["stopped_outages"] += v == "stopped" and (end - start).total_seconds() / 60 > longest
            if hole is not None:
                h_from, h_to = parse(hole.get("from")), parse(hole["to"])
                if h_to > lo and (h_from is None or h_from < hi):
                    holes.add((hole.get("from_commit"), hole.get("from"), hole["to"]))
    except Exception as e:  # the receipt is still written, with the reason, as for scheduled_gaps
        return {"ended_silences": None, "ended_silences_error": f"{where}: {type(e).__name__}: {e}"[:300]}
    return {"ended_silences": {"from": iso(lo), "to": iso(hi), **{f"{v}_n": counts[v] for v in counts},
                               "holes_n": len(holes), "series_from": iso(first), "limits": ENDED_LIMITS},
            "ended_silences_error": None}


def build_receipt(now: dt.datetime, bound: dict, beat: dict | None, error: str | None, prev: dict | None, env: dict,
                  bound_sha256: str | None = None, gaps: dict | None = None, bgaps: dict | None = None,
                  qwin: dict | None = None, ended: dict | None = None) -> dict:
    beat_at = parse(beat["github_time"]) if beat else None
    qwin = qwin or {"quiet_utc": bound.get("quiet_utc", []), "quiet_fit": None, "quiet_fit_error": None}
    if error is not None:
        v = {"verdict": "error", "silence_min": None, "silence_outside_quiet_min": None, "bound_min": float(bound["bound_min"])}
    elif qwin["quiet_utc"] is None:  # no window could be fitted: only a silence it would not have decided is judged
        v = verdict(beat_at, now, dict(bound, quiet_utc=[]))
        if v["verdict"] not in ("running", "no-beat"):
            v["verdict"] = "unknown"
        v["silence_outside_quiet_min"] = None
    else:
        v = verdict(beat_at, now, dict(bound, quiet_utc=qwin["quiet_utc"]))
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
        **qwin,
        **(ended or dict.fromkeys(ENDED_FIELDS)),
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


def run(root: pathlib.Path, env: dict, now: dt.datetime | None = None, fetch=api, fetch_page=api_page) -> dict:
    now = now or dt.datetime.now(UTC)
    bound_bytes = (root / "bound.json").read_bytes()
    bound = load_bound(json.loads(bound_bytes.decode("utf-8")))  # a bound it refuses gives no receipt, only a failed run
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
    long_log = []  # the fit's read of the log, when there is one, so the gaps reach as far back as the fit does
    qwin, window_for = quiet_window(repo, env.get("GITHUB_TOKEN"), beat, bound, fetch_page, log_out=long_log)
    bgaps = beat_gaps(repo, env.get("GITHUB_TOKEN"), prev, beat, bound, fetch, window_for,
                      log=long_log if bound.get("quiet_fit") is not None else None)
    ended = ended_silences(root, now, beat, bound, bgaps)
    try:
        return build_receipt(now, bound, beat, error, prev, env, sha, gaps, bgaps, qwin, ended)
    except Exception as e:  # after check_beat and last_receipt, most likely a bound.json value; still a receipt
        error = ((error + "; ") if error else "") + f"could not build the receipt: {type(e).__name__}: {e}"
        if beat and not bgaps["beat_gaps_unread_from"]:  # this receipt will not show the beat, so keep where to start
            bgaps = dict(bgaps, beat_gaps_unread_from=beat["commit"])
        return build_receipt(now, bound, None, error[:300], None, env, sha, gaps, bgaps, qwin, ended)


if __name__ == "__main__":
    env = dict(os.environ)
    if "--repo" in sys.argv[1:-1]:
        env["GITHUB_REPOSITORY"] = sys.argv[sys.argv.index("--repo") + 1]
    receipt = run(HERE, env)
    print(json.dumps(receipt, indent=1))
    if "--write" in sys.argv:
        print("wrote", write(HERE, receipt))
