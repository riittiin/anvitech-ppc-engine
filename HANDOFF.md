# HANDOFF — Anvitech PPC Engine

Orientation for a **new Claude Code session** taking over this project. The goal is
to pick up **exactly where the last session left off — same standards, same way of
working with the user, same grasp of the domain**, not just the same codebase. Read
this whole file (especially **"How to pick up where the last session left off"**
below), then [`CLAUDE.md`](CLAUDE.md) for the technical reference (design principles,
data flow, code map).

---

## TL;DR

A **Production Planning & Control (PPC) engine** for Anvitech, a precision-machining
job shop. **Built, tested, and deployed live.**

- **Live app:** https://anvitech-ppc.onrender.com (login-gated).
- **Host:** Render (free web service). **Database:** MongoDB Atlas (free M0).
- **Repo:** GitHub `riittiin/anvitech-ppc-engine` (private). Push to `main` →
  Render auto-redeploys.
- **65 tests pass** (`pytest`). FastAPI backend + vanilla HTML/JS frontend, plain
  Python engine.

## How to pick up where the last session left off (behavioral context)

This project was built across one long, iterative session. To continue smoothly,
**work the way that session did** — the patterns below are what kept it on track.
This section matters as much as the architecture.

### Who the user is
- The **owner/operator of Anvitech** (a precision-machining job shop) — a domain
  expert on the shop floor, **not a software engineer**. He talks in business terms:
  sales orders, SO numbers, delivery dates, shifts, downtime, machines.
- He wants a **real, free, deployed** tool his workers + planner will actually use.
  Cost must be **$0** (hence free tiers: Render + MongoDB Atlas free).
- **Decisive and iterative.** He surfaces edge cases and "loopholes" himself and
  wants to **understand the *why*** before changing things.
- He **does the external-service signups himself** (Render, Atlas, etc.) and expects
  you to do **all the code** and give him **only his manual steps** — one at a time,
  with exact clicks, marked "do now" vs "after I deploy." He gets understandably
  frustrated when stuck navigating a dashboard ("where is X?") — be **patient and
  precise**, point at the exact button, and ask what he sees if unsure.

### How to behave (the working norms that worked)
1. **Verify before you claim anything works.** Run `pytest`, do a `curl` smoke of the
   real flow (upload → plan → actual → mark-complete → delete), load the UI and check
   the browser console, and hit the live URL (`401` = up). **Evidence before
   assertions** — never say "it works" without it.
2. **Plain language, business framing.** No jargon. Use **tables** and **short numbered
   steps**. Explain shop-floor impact, not implementation detail, unless he asks.
3. **Be honest — including about your own mistakes.** State tradeoffs and caveats up
   front; correct yourself when wrong (e.g. MongoDB free is **512 MB, not 5 GB** — a
   real mid-session correction). Never paper over a limitation to sound finished.
4. **Decide-and-state; ask only when it matters.** Make sensible defaults and say so.
   Ask a **single focused question** only when genuinely ambiguous AND it changes the
   outcome (SO# identity, archive-vs-delete, overdue handling were worth asking; most
   things weren't).
5. **Plan big features first.** For substantive work use brainstorming → spec → plan:
   one question at a time, present the design, get approval, write the spec to
   `docs/superpowers/specs/`, *then* implement. The order book was built exactly this way.
6. **Protect the live site.** It's deployed and holds real data. Build risky/large
   changes on a **branch**, merge to `main` only when approved. **Commit/push to `main`
   only when the user asks** (push = deploy).
7. **Keep the UI minimal.** He values an uncluttered interface (he had the rule tabs
   stripped to just input / output / notes). Add nothing that doesn't serve the flow.
8. **Reuse, never duplicate.** Rules stay pure; the order book is the only stateful
   layer; "Plan" reuses Rules 1–6.
9. **Keep tests, the golden snapshot, and docs in lockstep** with every change.

### Communication shape
- Lead with the result/answer, then the detail.
- Use ✅ / ⚠️ and tables so status is scannable.
- End with a clear next step or **one** focused question — not a wall of options.

## What it is now (architecture)

The 9 business rules (see [`RULES.md`](RULES.md)) turn sales orders into a
machine-by-machine schedule + Gantt, re-planning as actual production comes in. The
engine is a **persistent, shared Order Book** (keyed by unique SO number) sitting
**above the unchanged pure Rules 1–6**:

- **Upload** an Excel → orders **merge** into the book (new SO# → Pending; a
  known/completed/intra-file-duplicate SO# → flagged, never double-counted).
- **Plan** (one button; unifies the old "Run" + "Rerun MRP") → schedules every
  *active* order at its **remaining** qty (ordered − good produced) through Rules 1–6.
- **Rule 7** daily entry records production; the first actual flips an order
  Pending → Running; ticking **"mark complete"** on an entry → Complete (archived).
- **Delete** (single / multiple / all) permanently removes from the database.
- **Login:** HTTP Basic Auth gates the whole app. **Persistence:**
  `engine/storage.py` picks **MongoDB > Upstash > local file** by env var.

## Run / test / deploy

```bash
pip install -r requirements.txt
pytest                                   # 65 tests
REGEN_GOLDEN=1 pytest -k golden          # only after an intentional logic change
uvicorn api.main:app --reload            # http://127.0.0.1:8000
```
Locally, with no store env vars set, data goes to `data/store/` (gitignored).
Default login is `anvitech` / `ppc2025`. **Deploy:** push to `main`.

## Git, GitHub & deploy workflow

- This is a **git repo**; `origin` = GitHub **`riittiin/anvitech-ppc-engine`** (private).
- **`gh` CLI is installed and authenticated** as `riittiin`, and git's author identity
  is set globally — so a session here can `git commit`, `git push`, and use `gh`
  (PRs/issues) **without any setup**.
- **Deploy = push to `main`.** Render watches `main` and auto-redeploys from
  `render.yaml` (runs `uvicorn api.main:app`). There is **no separate deploy step** —
  pushing IS deploying.
- **Commit message convention:** end every commit message with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Commit / push to `main` ONLY when the user asks** — it goes straight to the live
  site. For risky/large work, branch first (`git checkout -b feature`) and merge to
  `main` once approved (that earlier order-book build was done on a branch first).
- **Verify a deploy:** Render dashboard → the service's **Events** tab shows the
  build; or from a shell:
  `curl -s -o /dev/null -w '%{http_code}\n' https://anvitech-ppc.onrender.com/` →
  **401** = up & healthy (the login gate); **502/503** = it crashed (check Render **Logs**).

## Live deployment specifics (no secrets in this file)

- **Render** service `anvitech-ppc` — free web service; **sleeps after ~15 min idle**
  (first hit after that takes ~30–60s to wake; data is unaffected).
- **MongoDB Atlas** free **M0 (512 MB)** — database `anvitech`, collections `hash`
  (orders), `list` (actuals), `kv` (masters). **Atlas Network Access includes
  `0.0.0.0/0`** (required because Render's free IPs are dynamic).
- **Env vars set on Render:** `APP_USERNAME`, `APP_PASSWORD`, `MONGODB_URI`. (Upstash
  vars may also be present but are **ignored** — Mongo takes priority.)

## Domain rules to honor (confirmed with the user)

- **SO number is the unique order key** (one SO# = one order line). Repeats are
  flagged, never double-counted; an order is **never auto-deleted** for being absent
  from an upload.
- **Status is derived:** Pending (no actuals) → Running (≥1 actual) → Complete
  (**explicit — only via the Rule 7 "mark complete" tick; the engine NEVER
  auto-completes**, not even at remaining ≤ 0).
- **Rule 3 priority = least slack** = (working-time-until-due) − (work-needed); no
  window by default; on equal dates it reduces to "more process time first." Metric
  (`slack`/`critical_ratio`/`process_time`) and window are configurable.
- **Rule 3 "total process time" = sum of per-process _cycle_ times** (data-confirmed
  against the SO-Remarks oracle).
- **Rule 6 = non-delay scheduler** — a machine never idles while an operation is
  ready for it.
- **Masters** are latest-wins on upload, but kept if a file omits them.

## Done vs deferred

**Done:** order book + lifecycle, upload-merge + dedup, completion via Rule 7,
unified Plan, hour-resolution Gantt with status labels, login, Render + MongoDB
deploy, permanent delete (single/multi/all), append-safe storage.

**Deferred (explicitly, per the user):**
- Applying **revisions** to existing orders (changed qty/date) — currently **flagged
  only**, original kept.
- Explicit **cancel** action (orders leave only via complete or delete).
- Optional **export-then-purge** of completed orders to keep the DB lean — not needed
  at current scale (512 MB ≈ decades for one shop).

## Gotchas / operational notes

- **MongoDB Atlas free is 512 MB** (the 5 GB "Flex" tier is *paid*). At ~10–20 MB/year
  for one shop, this is decades of room — not a concern.
- **The bundled `Test2.xlsx` is test-only** and intentionally reuses SO# `24-25SO121A`
  across two dates (an old consolidation-test artifact) — that's why uploading it
  shows "1 flagged." Real production data has unique SO#s, so nothing flags.
- **`requirements.txt` includes `pymongo[srv]`** (needed for the `mongodb+srv://` URI).
- Local shell is **zsh**: when scripting `curl` with credentials, put
  `-u user:pass` **literally on each curl** — an unquoted `$VAR` won't word-split.
- Static assets are served with `Cache-Control: no-cache`; after a deploy a normal
  refresh picks up new JS/CSS.

## Where to look

- [`CLAUDE.md`](CLAUDE.md) — design principles, data flow, **code map**, commands. **Read first.**
- [`RULES.md`](RULES.md) — the 9 business rules (source of truth for rule logic).
- [`docs/superpowers/specs/2026-06-22-order-book-design.md`](docs/superpowers/specs/2026-06-22-order-book-design.md) — **current** architecture design.
- `docs/superpowers/specs/2026-06-19-…` — original (pre-order-book) design; historical reference.

## Making changes safely

- For substantive logic changes, update `RULES.md` / the relevant spec **first**, then code.
- Keep the rules **pure**; the order book (`orderbook.py` / `book_store.py`) is the only stateful layer.
- Run `pytest`; regenerate the golden trace only for **intentional** logic changes.
- **Commit/push to `main` only when the user asks** — it auto-deploys to the live site.
