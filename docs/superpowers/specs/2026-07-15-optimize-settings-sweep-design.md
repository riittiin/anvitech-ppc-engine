# Optimize settings sweep — auto-tune the overlap % (design)

**Date:** 2026-07-15 · **Status:** approved by owner · **Branch:** `optimize-settings-sweep`

## What it is

Today the **Optimize** button searches only the batch *sequence* under whatever
Settings the admin happens to have. The owner wants the settings themselves tuned:
when he clicks Optimize, the engine should also try the **overlap %** values and
return the combination of *sequence + overlap* that yields the best plan. One click,
no new controls to operate.

Owner decisions (2026-07-15):
- **Scorecard:** identical to Optimize — `total_late_days + 10 × makespan_days`
  (delivery gaps dominant). One definition of "best" everywhere.
- **Promises:** sweep even when Committed/Urgent orders exist, but a candidate
  overlap is eligible **only if the committed orders keep their promises at least
  as well as under the current setting** (guard on Pass 1, see below).
- **Scope:** overlap % only (candidates 50–100 step 10, plus the current value).
  The sweep helper is written so another dial is a small addition later.

## How the budget is spent (floor-first, probe, deepen)

> **CONTRACT STRENGTHENED (2026-07-15, same day, after a live regression).** The
> shipped v1 gave the current setting only ~half the budget and let a challenger
> dethrone that weakened result: on the real 65-order book (start 11-07) Deep
> returned **753 late-days / winner "Overlap 60"** where the pre-sweep button
> found **713 late-days at Overlap 80** — and at full depth Overlap 80 in fact
> beats 60. The guarantee the owner expects is *"one click is never worse than
> the pre-sweep Optimize button."* So the budget shape is now:

1. **Floor first:** the CURRENT overlap runs FIRST at the FULL advertised budget
   (Quick 150 / Deep 400). With the fixed seed this run is *identical* to the
   pre-sweep Optimize button, so the sweep's floor is exactly that result — and
   an early **Stop** still leaves the user's own setting as the deepest-searched
   candidate.
2. **Probe (extra):** the non-current candidates share an EXTRA ~quarter pool —
   `probe_each = max(1, (budget // 4) // n_others)` evaluations each. Probes are
   cheap and noisy; they only nominate, never win outright.
3. **Deepen (extra):** the best probe (the challenger) gets a further ~quarter.
   It replaces the current setting **only if its deepened score strictly beats
   the current setting's full-depth score** — ties keep the current setting.
4. **Total** = `sweep_total_evals(budget)` ≈ 1.5 × the advertised budget
   (Quick ~225 / Deep ~600 plans; the extra wall time is the price of tuning the
   overlap without ever gambling away the plain button's quality).
5. **Deterministic:** all seeds fixed, budgets are eval counts. **Stop & keep
   best** works: cancellation is polled between evaluations across the whole
   sweep; the best (sequence, overlap) found so far is kept.

## Promise guard (Pass-1 eligibility)

With protected (Committed/Urgent) orders present, for each candidate overlap `v`:
run the unchanged Pass 1 (protected orders only) under `v` and compute
`promise_slip_metrics`. The candidate is eligible only if BOTH its total
promise-slip-days and its broken-promise count are **≤ the current setting's**
Pass-1 values. Ineligible candidates are skipped (recorded in the sweep table).
The open-pass search for an eligible candidate uses reservations from *that
candidate's* Pass 1, so the searched plan is exactly the plan Apply would produce.
An all-open book has no guard (every candidate eligible, `reserved=None`).

## Apply persists both — and stays transparent

- Apply saves the winning **ranks** (as today, `anvitech:plan_priority`) AND writes
  the winning **overlap % into the saved plan config** (`anvitech:plan_config`), the
  single source every Plan already loads. The Settings panel therefore openly shows
  the new value — no hidden state; users plan with it automatically.
- The persisted `inputs_sig` fingerprint is computed against the **winning** config,
  so the staleness banner stays quiet right after Apply and fires exactly when the
  admin later hand-edits Settings or re-uploads masters.
- The Optimize result panel adds one line: *"Best setting found: Overlap X%
  (currently Y%)"* (or notes the current setting already wins). `/optimize/status`
  exposes `best_overlap` for the UI.

## Where the code goes

- `engine/optimizer.py` — pure `sweep_optimize(so_lines, config, masters, *,
  budget_evals, seed, on_progress, should_cancel, candidate_setup, candidates)`:
  orchestrates probe-then-deepen over `replace(config, overlap_percent=v)`; the
  `candidate_setup(cfg) -> (reserved, eligible)` hook keeps API concerns (Pass 1,
  promise guard) out of the pure module. Returns the winning overlap, the merged
  best `OptimizeResult`, and a per-candidate table.
- `api/main.py` — `_start_optimize` calls `sweep_optimize` with a candidate_setup
  that builds Pass-1 reservations + enforces the promise guard; the result carries
  `best_overlap` + `inputs_sig` (winning config); `_optimize_apply` persists the
  overlap into the saved plan config.
- `web/app.js` — one line in the Optimize result panel; no new controls.

## Safety / testing

- No applied optimization → every plan byte-identical (golden untouched).
- New `tests/test_optimize_sweep.py`: sweep picks the better overlap on a book where
  overlap measurably changes the score; determinism (same inputs → same winner);
  never-worse (current always a candidate); ineligible candidates skipped; Apply
  persists overlap + `optimize_meta.inputs_changed` stays False right after Apply;
  promise guard rejects an overlap that breaks a committed promise.
- Full suite + golden must stay green; browser-verify the result panel line.
