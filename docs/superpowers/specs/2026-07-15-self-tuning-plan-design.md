# Self-tuning plan — auto re-optimize, one-pool promise-ceiling contest, operator absence entry

**Date:** 2026-07-15 · **Status:** approved by the owner (this session) · **Builds on:**
`2026-07-15-optimize-settings-sweep-design.md` (fair contest + cloud compute),
`2026-07-14-promise-recovery-committed-resequencing-design.md`,
`2026-07-13-optimize-plan-sequence-search-design.md` (committed lanes / two-pass).

## The owner's laws (verbatim intent)

1. **A promise is a ceiling, not a cage.** Promised 9 Aug → finishing 8 or 9 Aug is
   fine (early/on-time always welcome); finishing after 9 Aug is not. When a change
   threatens a promise, the system must first try to absorb it by re-ordering
   everything it controls; only when truly impossible does it accept the delay,
   minimize it, and flag it red.
2. **Do not differentiate lanes for capacity.** Freed time belongs to ALL orders —
   committed orders may move earlier into it just like open ones. Protection is the
   ceiling above, not a frozen schedule.
3. **The plan should always optimize itself.** When production changes reality
   (early finish, punches, uploads, deletes, absences), the plan re-optimizes
   without anyone remembering to click.
4. **Effort concentrates on the latest orders** — satisfied by the existing score
   (total-late-days dominant, 10:1 over makespan; measured best on the real books).
   No scoring change.

## The three layers (the mental model — keep them separate)

- **Layer 1 · Facts:** punches record *how much is done*. Quantity-only — never
  optimized, never projected.
- **Layer 2 · Projection:** plans always project remaining work at the **standard
  cycle times from the process master** (owner re-confirmed 2026-07-15). A slow
  operator moves expected dates only by his measured shortfall; the system never
  assumes tomorrow's pace from yesterday's. **Promised date never moves; Current
  expected moves; red slip flag = expected > promised.**
- **Layer 3 · Choice:** the only optimized thing is the *choice* of job order on
  the machines (+ overlap %, + operator assignment). The contest searches Layer 3
  only.

## Phase 1 — Auto re-optimize (self-tuning trigger + auto-apply)

**Fingerprint.** Every applied optimization stores a `book_sig`: hash of the active
order keys + each order's remaining qty and per-process remaining + commitment
lane + promised dates + operator absences (Phase 3) — alongside the existing
`inputs_sig` (masters + plan-shaping config). Book changed ⇒ `book_sig` differs.

**Triggers.** After any state-changing action that alters the fingerprint:
actuals save (incl. mark-complete + rollback), upload merge, order delete/clear,
commitment change, absence change. Implementation: one `_maybe_auto_optimize()`
hook called from those endpoints.

**Debounce (protects the GitHub free budget).** Punches arrive in bursts. An auto
contest starts only after a **quiet window** (default 10 min without further book
changes) and with a **minimum spacing** (default 60 min between auto contest
starts). One contest at a time (existing lock); a change landing mid-run schedules
the next. Both knobs env-tunable (`AUTO_OPTIMIZE_QUIET_MIN`,
`AUTO_OPTIMIZE_SPACING_MIN`). Manual Optimize is never throttled.
Budget check: worst case ≈ 9-10 auto contests/day ≈ 60 GH-minutes/day — inside
the 2,000 free minutes/month with margin.

**Auto-apply.** When the contest finishes: compute the incumbent = today's plan
as the users would see it (the applied optimization replayed on TODAY'S book, or
the plain plan if none is applied) and compare scores. The winner auto-applies
**only if strictly better** — including persisting the winning overlap % into the
saved plan config (owner-approved; the note records e.g. "overlap 80 → 70").
Otherwise nothing changes. Both outcomes write a one-line note (stored at
`anvitech:auto_optimize_note`, shown on the Orders tab):
*"Plan auto-re-optimized 18:12 — 445 late-days (was 471), overlap 80 → 70"* or
*"Checked 18:12 — current plan still best."*

**Kill switch.** `AUTO_OPTIMIZE=0` disables the trigger entirely (tests set this;
the deployed default is on). **Auto contests are cloud-only**: with the cloud
unavailable (dispatch fails, budget exhausted, env unset) the auto run is
SKIPPED with a note ("will retry on the next change") — never a 40-min local
burn of the free instance's 0.1 CPU in the background. The manual button keeps
its local fallback. A finished contest is re-validated on TODAY'S book before
applying (feasible + strictly better) — a result made stale by mid-run book
changes is discarded and a fresh contest scheduled.

## Phase 2 — One-pool contest with the promise veto

**Search space.** The contest reorders **all active orders in one sequence**
(open + committed + urgent interleaved), evaluated as ONE forward pass
(expedite off), replacing the search-open-pass-only rule.

**The veto.** Every candidate plan is checked: any committed/urgent order whose
projected end lands **after its promised date** ⇒ the candidate is rejected
outright (score = ∞). No trade-offs against it. Early/on-time = always allowed.

**Seeds.** The search seeds include the current shape (committed date-order first,
then open in current applied order) plus the standard seeds; only
promise-feasible candidates can win.

**Replay safety (drift can never break a promise silently).** `_plan` replaying a
joint ranking re-validates the veto on today's book *every time*. If reality has
drifted so the replayed plan would break a promise: discard the ranking for this
plan, fall back to the protected two-pass shape (unchanged from today), and
trigger an auto contest. The two-pass machinery is retained permanently as the
fallback shape.

**Impossible promises** (absence/disruption makes any-plan infeasible): the veto
rejects everything ⇒ two-pass fallback + the existing promise-recovery search
in least-damage mode — **fewest broken promises first, then fewest slip-days**
(keepable promises stay kept; only the unkeepable are minimized); red
Promised-vs-expected flags + recovery note show which customers to call and by
how many days. Auto-apply in this mode compares damage, not score: apply only
on strictly fewer broken, or equal broken with strictly fewer slip-days.

**Measurement gate before shipping Phase 2:** on the real book, the one-pool
contest must beat or match today's open-only contest (score) while passing the
veto on 100% of applied plans. If it measures worse, stop and report — do not ship.

## Phase 3 — Operator absence entry

**Store.** `anvitech:absences` in the book store: list of
`{id, operator, from_date, to_date}` (dates inclusive, DD-MM-YYYY in UI, ISO in
store). The masters workbook stays read-only.

**Orphans.** If a masters re-upload removes an operator who still has absence
rows, those rows are ignored and reported in the non-blocking report (same
forgiving pattern as provisional machines) — never fatal to a plan.

**Engine.** An absence becomes a **blocked interval for that person** in every
plan: injected as operator reservations (the same `reserved=` mechanism Rule 6
already honors for committed-pass protection), for every pass and every contest
candidate. The cloud payload carries absences. Analytics subtracts absence hours
from the operator's available capacity (utilization stays honest); shift-wise
download follows automatically because it reuses the plan's assignments.

**API/UI.** Admin-only endpoints (`/absences` list/add/delete, password not
required — non-destructive and reversible). Settings panel control: operator
dropdown (from masters) + from/to date pickers + current-absences list with
remove buttons. User role: read-only visibility.

**Interaction.** Absence add/remove changes the fingerprint → auto contest →
promises re-checked under the new capacity → keep-all if possible, else
least-damage mode. Absence ending (or removal) restores capacity the same way.

## Invariants & testing

- All-open book, no absences, auto mode off ⇒ **byte-identical plans to today**
  (golden trace untouched).
- The veto: property test — no applied/auto-applied plan ever shows a committed
  order past its promise; drift test — a stale joint ranking on a mutated book
  falls back rather than breaking a promise.
- Auto-apply: strictly-better-or-nothing; note written on both outcomes.
- Debounce: burst of punches ⇒ one contest after the quiet window; spacing
  honored; `AUTO_OPTIMIZE=0` ⇒ no background threads (test isolation).
- Absences: person never assigned inside an absence; analytics capacity reduced;
  payload round-trips absences; cloud == local byte-identical still holds.
- Phase 2 real-book measurement gate (above) before its deploy.

## Explicitly out of scope

- Learning per-operator pace and projecting it forward (owner chose standard
  cycle times — quantity-only feedback stands).
- Machine-assignment search (measured prize ≈ 13 days — standing do-not-build).
- Scoring changes (keep late-days + 10×makespan; a worst-order penalty can be
  measured separately later if wanted).
- Absence approval workflows / half-day granularity (day granularity v1).
