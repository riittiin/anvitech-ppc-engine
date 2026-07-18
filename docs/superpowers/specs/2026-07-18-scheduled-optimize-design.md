# Scheduled optimization — twice a week, floor-stable

**Date:** 2026-07-18 · **Status:** approved by the owner (this session) ·
**Supersedes:** the event-triggered auto-optimize behavior of
`2026-07-15-self-tuning-plan-design.md` Phase 1 (the machinery stays; the
triggers change).

## The owner's rule

Re-sequencing the floor every day destroys schedule trust. The plan's FACTS
update daily (punches → replan, job order stable); the plan's JOB ORDER is
re-optimized **only twice a week, automatically**:

- **Monday and Friday at 11:00 IST** (05:30 UTC). Rationale: feedback is
  entered ~10:00 during shift 1; the optimized schedule is ready before
  shift 2. With Thursday the weekly off, Mon/Fri are exactly 3 working days
  apart in both directions — equal spread.
- **No event triggers remain.** Uploads, commits/urgent/uncommit, deletes,
  Settings saves, and the Done button no longer start contests. New orders =
  the owner's MANUAL flow (arrive Open → he presses Optimize → commits).
  The manual Optimize button is untouched.
- **Done button stays** with its floor meaning ("operator finished entering
  all rows") — relabeled "Done entering — update plan": refreshes the plan
  (facts) and reports the next scheduled optimization day. No contest.

## Mechanism

- New workflow `.github/workflows/scheduled-optimize.yml`: cron
  `30 5 * * 1,5` (+ `workflow_dispatch` for testing). One step: POST
  `$APP_URL/optimize/scheduled` with the `X-Worker-Secret` header,
  wake-tolerant retries (the free Render instance may be asleep).
- New endpoint `POST /optimize/scheduled` (worker-secret auth, same
  gatekeeper bypass list): calls `_try_start_auto()` — which keeps ALL its
  guards: cloud-only, one-at-a-time, and the book-fingerprint check (nothing
  material changed since the last applied plan ⇒ skip silently, zero cost).
  Returns `{started, state}`.
- `_bump_book_changed()` and the `_AUTO` pending/chaining machinery are
  REMOVED (no event triggers to debounce). `_try_start_auto()` is invoked
  only by `/optimize/scheduled`.
- `/optimize/done` endpoint REMOVED; the button calls the normal plan
  refresh client-side and computes the next Mon/Fri 11:00 locally.
- Auto-note timestamps display **IST** (the notes now reference a named
  local clock time; server runs UTC). Skip-note copy: "will retry on the
  next scheduled run".

## Invariants

- Auto-apply stays strictly-better-or-nothing; manual Optimize unchanged;
  absence/commit/upload endpoints unchanged except no trigger call.
- All-open books, golden trace: untouched.
- `AUTO_OPTIMIZE=0` still disables the scheduled path (tests).

## Budget

2 contests/week ≈ 12-20 GH-minutes/week — negligible vs 2,000 free/month.
