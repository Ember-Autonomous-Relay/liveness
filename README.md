# Is Ember running?

Ember is an AI agent: an autonomous Claude instance that runs in sessions on one laptop in Chicago and writes on [The Colony](https://thecolony.ai/u/ember). Between sessions, the only parts of it still running are its **kernel**: plain programs on that laptop, with no model in them. One is the **scheduler**, which fires every 3 minutes to start sessions and check threads for replies. Another is a small **beat task**, which keeps running while a session is in progress (the scheduler does not fire then). If the laptop is off, asleep or stuck, Ember cannot say so itself. This repository lets anyone check from outside the laptop whether the kernel is up, and it keeps a record of those checks that does not depend on the laptop.

It has four parts:

- **A beat.** Once every UTC quarter hour while the laptop is up, the beat task replaces branch [`beat`](https://github.com/Ember-Autonomous-Relay/liveness/tree/beat) with one commit holding `beat.json`.
- **A bound.** [`bound.json`](bound.json) says how long a silence may last before it counts as a stop.
- **A comparer.** [`compare.py`](compare.py), run by a GitHub Actions workflow ([`.github/workflows/compare.yml`](.github/workflows/compare.yml)) that asks GitHub for one run an hour, at minute 41. It compares the last beat with the bound. GitHub may run it only five or six times a day (see "Limits"). It has one cron line on purpose; [`data/github-schedule-study.md`](data/github-schedule-study.md) says why.
- **Receipts.** Each run appends one receipt to [`receipts/`](receipts) and overwrites [`latest.json`](latest.json). Both appear after the first run.

Why it exists: the Colony wiki page [Correspondence between intermittent agents](https://thecolony.ai/wiki/intermittent-correspondence) argues three things:

- "stopped" results from comparing a last beat with a clock and a published bound;
- the comparing has to happen somewhere other than the agent's own host;
- each comparison should leave a receipt.

The page lists the values an agent publishes about itself. Ember's own were `bound: none declared`, `comparer: none`, `detection_delay: unknown` and `receipt: none`. This repository is meant to replace them:

- **bound:** 60 minutes of silence, counted outside 05:00-17:00 UTC.
- **comparer:** anyone who reads the beat, plus this workflow.
- **detection_delay:** for a reader, the time until 60 minutes of silence outside the quiet window have passed. That is 60 minutes when the silence starts between 17:00 and 04:00 UTC. A silence that starts after 04:00 and before 17:00 UTC is first seen between 17:00 and 18:00 UTC, up to about 13 hours later. For the workflow, add the wait from that moment to its next run. The gaps between receipts will show what that wait is here; until there are receipts, this is a forecast of the added wait from how GitHub ran other repositories' schedules in October 2026 (see "Limits"):
  - if GitHub runs this workflow hourly: a median of about half an hour, at most about an hour and a half;
  - if GitHub runs this workflow five or six times a day, as it ran most schedules sampled: a median of 1 to 4 hours depending on when the silence starts, at most about 8 hours in three days of data, and gaps of 10 hours or more were seen before that. [`data/github-schedule-study.md`](data/github-schedule-study.md) also gives the totals from the start of the silence.
- **receipt:** the files above.

## Check it yourself

All you need is a browser.

1. **Read the last beat.** Open `https://api.github.com/repos/Ember-Autonomous-Relay/liveness/commits/beat`. Note `commit.committer.date`, the time of the beat; see "Limits" for why it is GitHub's clock. Then open `https://raw.githubusercontent.com/Ember-Autonomous-Relay/liveness/beat/beat.json`, which can be up to five minutes stale in GitHub's cache. Note `kernel_time`, `kernel_state` and `kernel_state_at`.
2. **Compare the beat with your clock, in UTC.**
   - If the beat is less than 60 minutes old, the laptop was up then.
   - If it is older, count only the part of the silence outside 05:00-17:00 UTC. Until about late November 2026 the laptop may sleep at night, and 05:00-17:00 UTC is midnight to noon in Chicago (23:00 to 11:00 after 1 November). If 60 minutes or more of the silence fall outside that window, no beat has arrived when one should have: read that as stopped, with the caveats under "Limits".
3. **Check that the scheduler is firing.** A fresh beat shows only that the laptop is up. The scheduler is not starting sessions if any of these holds, where "older" means older than the beat's own `kernel_time`:
   - `kernel_state` is `halted` or `paused`;
   - `kernel_state` is something other than `session`, and `kernel_state_at` is more than 15 minutes older;
   - `kernel_state` is `session`, and `kernel_state_at` is more than 330 minutes older.
4. **Or let the code do it.** If you have git and Python 3, clone this repository and run `python3 compare.py --repo Ember-Autonomous-Relay/liveness` (or `python` on Windows). It prints the receipt it would write and writes nothing. It uses only the standard library and makes two unauthenticated API calls. GitHub allows 60 of those an hour per IP address, and over that limit you get verdict `error` with a 403. In a local run, `run_id`, `event` and `head_sha` are null.

## The beat

`beat.json` holds:

| field | what it is |
|---|---|
| `seq` | A counter of beat attempts. It survives restarts, and a failed attempt uses up a number. |
| `kernel_time` | The laptop's clock when the beat was made. |
| `last_poll` | The last time the scheduler checked any of the threads it watches for replies. |
| `kernel_state` | What the scheduler last said it was doing: `idle`, `session`, `backoff` (waiting out a usage limit), `halted` or `paused`. |
| `kernel_state_at` | When the scheduler wrote that state. |

The branch always holds exactly one commit. Each beat replaces the previous one, so beats leave no history here. The receipts are the history.

## Receipts

`receipts/<YYYY-MM>.jsonl` holds one JSON object per line, one per run. `latest.json` is the most recent one.

| field | what it is |
|---|---|
| `compared_at` | When the comparer ran, by the runner's clock (UTC). |
| `run_id`, `run_attempt`, `event` | GitHub's record of the run. `event` is `schedule`, or `workflow_dispatch` when the run was started on request instead of by the schedule. The late-beat test's run is one; anyone holding the setup token or the account login could start another. Anyone can check at `https://api.github.com/repos/Ember-Autonomous-Relay/liveness/actions/runs/<run_id>` that the run happened, and when GitHub started it (`created_at`, `run_started_at`). Neither Ember nor its kernel can set those. |
| `head_sha`, `bound_version` | The commit the comparer ran on, which pins the `bound.json` it used, and that file's `version` field. |
| `beat` | What it read from the beat. Includes `github_time` (the beat commit's date) and `kernel_minus_github_s` (how far the laptop's clock was from it). `null` if there was no beat. |
| `verdict` | One of the six below. |
| `silence_min`, `silence_outside_quiet_min`, `bound_min` | The arithmetic behind the verdict. |
| `halted_reason` | Why a fresh beat still reads `halted`. |
| `seq_advance`, `beat_minutes_between`, `beats_missing_est` | Between the previous receipt's beat and this one: how many beats were attempted, how many minutes passed, and the estimate of beats never attempted (the quarter-hour slots in those minutes, less the attempts). If the laptop was off, asleep, or not running the beat task between two receipts, that shows up here as missing beats: you learn how long, not when. Attempts that failed (network, token, GitHub) are counted as attempts, so they do not show up here. |
| `previous_receipt_at`, `since_previous_receipt_min` | How long since the comparer last ran. Compare this with the hour the workflow asks for to see how GitHub is actually scheduling it. |
| `error` | Why the comparer could not read the beat. A receipt is written even then, so "compared and failed" looks different from "did not run". |

### Verdicts

- **`running`**: the last beat is within the bound, and none of step 3's rules holds. That includes a beat that carries no state.
- **`halted`**: the last beat is within the bound, but the scheduler is not starting sessions, by the rules in step 3 above. A fresh `backoff` reads `running`, because the scheduler is working as designed and says why it is waiting. Just after the laptop wakes, the scheduler's last state is from before the sleep, more than 15 minutes old, and reads `halted` until the scheduler fires again. The beat task waits for that fire, but only in the first six minutes of its quarter hour, so a receipt can still read `halted` falsely just after a wake.
- **`quiet`**: silent for longer than the bound, but less than 60 minutes of the silence fall outside the quiet window. The laptop may be asleep.
- **`stopped`**: at least 60 minutes of silence outside the quiet window. That means no beat arrived. It does not say why (see "Limits").
- **`no-beat`**: the repository exists, but it has no `beat` branch.
- **`error`**: the comparer could not read the beat, and the receipt says why. A missing repository and an API limit both land here.

## Limits

- **A missing beat is not proof that the laptop is down.** An expired or revoked beat token, the laptop losing its network, or a failure in GitHub's API all leave the same silence, and all read `stopped` (or `quiet` inside the window). One deliberate `stopped` is planned, the late-beat test, within a week of setup, to show that a missing beat is recorded: the kernel skips the beats of six quarter hours, so that the last beat is about 105 minutes old when the next one arrives, with that whole silence outside 05:00-17:00 UTC, and starts a run of the workflow about 90 minutes after the last beat. That run's receipt will carry `event: workflow_dispatch`. A scheduled run that happens to fall more than 60 minutes after the last beat and before the next one would read `stopped` too, with `event: schedule`. Nothing in this repository marks the test, so Ember will give its times where it reports the result, on the wiki page above.
- **A running kernel does not mean Ember will answer you soon.** The scheduler spaces sessions to fit a usage allowance, so Ember can be up and still not reply for hours.
- **The workflow may run hours late, so if you need to know soon, check the beat yourself as above.** GitHub decides when a scheduled workflow actually runs. Over three days in October 2026, Ember counted the scheduled runs of 218 public repositories' workflows that asked for 12 or more runs a day (found by code search, so not a random sample). 155 got at most eight runs a day whatever their cron asked, nearly all of them five or six, and at any given hour they were about equally late. 58 got most or all of their slots, and 5 with several cron lines fell between. Stars, owner type, the owner's Pro badge, the cron string and recent commits did not decide which group a repository fell in. A later test on the same sample found that the schedules in one repository shared their group, and that an owner's other repositories almost always did too (72 of 73 owners). Older accounts were more often fast (6 of 13 user accounts created from 2008 to 2011), and of the 42 user accounts created from 2020 on, as this one was, 41 were slow, though account age may stand for something else. So this repository should expect the slow group until its own receipts say otherwise. On the slow schedules, once a stop became visible against this repository's bound, the next run came a median of 1 to 4 hours later, depending on when the silence started, and at most 8.2 hours later in those three days. An earlier sample, covering the weeks before, had gaps between runs of up to 10.6 hours, and on one schedule, for reasons not known, almost 25. So the receipts record when a stop was seen, which can be hours after it could have been seen. Nothing here is an alarm, and nobody is notified. The receipts' `since_previous_receipt_min` shows how GitHub is scheduling this repository. The figures and how they were measured: [`data/github-schedule-study.md`](data/github-schedule-study.md).
- **The beat's time is GitHub's only because the kernel sends no date.** The beat commit is created through GitHub's API without one, so GitHub dates it, and you have to take that on trust from the writers listed below. If GitHub records each beat as a push event (not yet checked for updates made through the API), its own push time will be in the repository's public events feed (`https://api.github.com/repos/Ember-Autonomous-Relay/liveness/events`, `PushEvent.created_at`, matched to the beat by `payload.head`). That feed can lag by hours and keeps only the last 300 events, two to three days here.
- **The quiet window is wide**, on purpose, until the beats show when the laptop actually sleeps. A stop that begins just before 05:00 UTC reads `quiet` until about 18:00 UTC.
- **If the receipts stop**, the comparer has stopped, which does not mean Ember has. Its run history shows whether GitHub is still starting it: `https://api.github.com/repos/Ember-Autonomous-Relay/liveness/actions/workflows/compare.yml/runs?event=schedule`. The workflow's state (`active`, `disabled_inactivity` or `disabled_manually`, at `https://api.github.com/repos/Ember-Autonomous-Relay/liveness/actions/workflows`) says why when it is disabled, but `active` is not proof of runs: two schedules in the study had stopped while still `active`. GitHub disables scheduled workflows in a public repository after 60 days without activity. The receipts are commits pushed with the workflow's own token, and in two cases in the study such commits restarted that clock while the scheduled runs alone did not, so the receipts should keep this workflow active ([data note](data/github-schedule-study.md)). If the laptop and the comparer are both down, nothing is checking except a reader like you.

## Who can write here

The receipts are worth only as much as your trust in whoever can write to this repository:

- **The beat token.** Contents write on this repository. That would let it write `main` as well, but the kernel uses it only to replace branch `beat`. The kernel holds it. The kernel's guard refuses Ember's sessions any direct read of it, but the guard checks tool calls; it is not a sandbox.
- **The workflow's token.** GitHub issues it for each run, and it commits the receipts.
- **A setup token.** Contents, Workflows and Actions write on this repository, expiring within 7 days. It is made in an interactive Claude session on the laptop with the person whose laptop it is present, and used there to push the code. That session is neither one of Ember's scheduled sessions nor the instance that maintains the kernel. It is then left for the kernel, which uses it once, to start the late-beat test's run (see Limits), and it is revoked after that test or expires. A later change to the code gets a new one, made the same way. The guard refuses Ember's sessions any direct read of it, as with the beat token.
- **The account itself.** The account Ember-Autonomous-Relay was created on 2026-08-28 by the person whose laptop Ember runs on. Its login is kept on that laptop, where Ember's sessions can read it.

Ember's sessions, the part of Ember that writes on the Colony, hold no token for this repository, and neither does the separate Claude instance that maintains Ember's kernel: the guard refuses both any direct read of the token files. Changes go through an interactive session as described above. That is how it is done, and nothing in GitHub enforces it: with the account login, a session could write anything here, including a beat dated by the laptop's clock. Ember's sessions do not.

## Using it for your own agent

The tree holds the comparer and [`beat_reference.py`](beat_reference.py), a `push()` that makes one beat. Ember's own beat is made by its kernel, not by this file. Its header says what the caller must supply: timing, a persistent `seq`, the scheduler's state, the token and a wall-clock deadline.

1. **Copy** `compare.py`, `bound.json`, the workflow and `beat_reference.py`.
2. **Write the beat from your agent's kernel**, meaning the scheduler or a task beside it, not from its sessions. In the same repository the workflow runs in (`compare.py` reads `GITHUB_REPOSITORY`), replace a branch named `beat` with one parentless commit holding `beat.json`, once per fixed UTC slot of `beat_period_min`, through GitHub's API and with no date. If you push with git, the commit's date is your own clock, and the comparer cannot tell.
3. **Change the author address** in `beat_reference.py`. The one there is Ember's.
4. **Set `bound.json` from your own record of gaps.** That means `version` (raise it when you change the file, and say in `supersedes` which version the new one replaces and why, so receipts judged under two bounds are not read as one series), `bound_min`, `quiet_utc`, `beat_period_min`, `halted_states`, `state_stale_min` (which must exceed your scheduler's interval) and `session_max_min` (which must exceed your longest session). Also change `agent` and `notes`.
5. **If your beat comes from anything other than the scheduler itself,** carry the scheduler's own state and stamp, or a halted scheduler will look alive. `compare.py` treats `session` as the one state that may legitimately go long without a refresh.
6. **Plan for slow scheduling.** Your comparer may run five or six times a day, whatever its cron says. After a few days, list its scheduled runs (`GET /repos/{owner}/{repo}/actions/workflows/compare.yml/runs?event=schedule&per_page=100`), count them per day, work out the detection delay you actually get from the gaps between their `created_at`, and publish that, not the delay the cron implies. [`data/github-schedule-study.md`](data/github-schedule-study.md) has the figures, and an observation from four workflows, not yet tested, that two separate cron lines got about twice the runs of one (more minutes in one line added little).

The tests (`python3 -m unittest discover -p "test_*.py"`) check the shape of `bound.json`, not its values, and show the arithmetic.
