# Anvitech PPC Engine

A Production Planning & Control engine for Anvitech, a precision-machining job
shop. It reads customer sales orders and the shop's masters from `Test2.xlsx`,
runs **9 sequenced rules** to produce a machine-by-machine schedule, then
re-plans (MRP re-run) as daily actuals come in.

The defining feature: **everything is testable and debuggable rule-by-rule.**
Each rule is a pure function and the pipeline snapshots every rule's input and
output into a trace that the per-rule frontend tabs render.

> Authority docs: [`CLAUDE.md`](CLAUDE.md) · [`RULES.md`](RULES.md) ·
> [design spec](docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md) ·
> [`implementation.md`](implementation.md)

## Quick start

```bash
pip install -r requirements.txt

# Run the tests (engine + API)
pytest

# Start the app (engine API + frontend tabs) at http://127.0.0.1:8000
uvicorn api.main:app --reload
```

Open `http://127.0.0.1:8000`, set the config knobs, and click **Run plan**. Each
numbered tab shows that rule's input table, output table, config used, and the
decision notes it logged. A failing rule shows a red error on its tab and marks
downstream tabs "not reached".

## Login (whole app is gated)

The entire app — UI, API, and static assets — sits behind HTTP Basic Auth (one id
+ password). Credentials come from env vars; the browser prompts once per session.

- `APP_USERNAME` (default `anvitech`)
- `APP_PASSWORD` (default `ppc2025`)

Change them locally with `APP_USERNAME=… APP_PASSWORD=… uvicorn api.main:app`, and
set them in the Vercel project settings for production. **Change the defaults before
exposing the app.**

## Free public deployment (Render + Upstash — no credit card)

This is the recommended free, public, **persistent** setup. Render hosts the app
(free web service, public HTTPS); Upstash Redis (free) stores actuals + uploaded
workbooks so nothing is lost. Persistence is **opt-in via env vars** — set them in
production; locally the app keeps using `data/actuals.json` + memory.

1. **Put the code on GitHub.** `git init && git add -A && git commit -m "init"`,
   create a repo, and push (Render deploys from Git).
2. **Pick a durable store** (the engine selects: MongoDB > Upstash > local file):
   - **MongoDB Atlas (recommended, 5 GB free):** create a free M0 cluster, a DB
     user, allow network access `0.0.0.0/0`, copy the `mongodb+srv://…` string →
     set env var `MONGODB_URI`. (Needs `pymongo[srv]`, already in requirements.)
   - **or Upstash Redis (256 MB free):** create a DB → copy its **REST URL** and
     **REST TOKEN** → set `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`.
3. **Create the Render service:** render.com → New → **Blueprint** → pick this repo
   (it reads `render.yaml`). Render builds and gives you a public `…onrender.com` URL.
4. **Set env vars** in the Render service (Settings → Environment):
   - `APP_USERNAME`, `APP_PASSWORD` — your login.
   - `UPSTASH_REDIS_REST_URL`, `UPSTASH_REDIS_REST_TOKEN` — from step 2.
   Save → Render redeploys.
5. Open the URL, log in, upload your Excel, and use it. Actuals + uploads now persist.

Free-tier note: the Render free service **sleeps after ~15 min idle**, so the first
visit after a quiet period takes ~30–60s to wake — fine for an internal tool. The
data itself lives in Upstash, so sleeping never loses it.

## Deploying to Vercel (alternative)

Files already in place: `vercel.json` (build + routes), `api/index.py` (serverless
entrypoint re-exporting the FastAPI app), `.vercelignore`, slim `requirements.txt`.

1. Install the CLI and log in: `npm i -g vercel && vercel login`.
2. From the project root: `vercel` (preview) then `vercel --prod` (production).
   Or push to GitHub and "Import Project" in the Vercel dashboard.
3. In **Project → Settings → Environment Variables**, set `APP_USERNAME` and
   `APP_PASSWORD` (and optionally `ACTUALS_PATH`). Redeploy.

**Important Vercel constraints (serverless):**
- `Test2.xlsx` is bundled read-only via `includeFiles` — reads work fine.
- The filesystem is read-only except `/tmp` (ephemeral). For durable actuals +
  uploads, set `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` (free Upstash)
  — the same opt-in storage layer used for Render. Without them, actuals fall back
  to `/tmp` (wiped on cold starts). Note: Vercel's free Hobby plan is
  **non-commercial only**; Render's free tier has no such restriction.
- In-memory run cache isn't shared across invocations, so `GET /trace/{id}` may miss
  on a different instance — the UI doesn't rely on it (it uses the `/run` response).

## How it works

```
so_lines → R1 consolidate → R2 sort by date → R3 tiebreak (reads routing)
        → R6 allocate (uses R4 setup, R5 overlap, R7 parallel + masters)
        → R8 capture actuals → R9 rerun MRP (calls R1..R7 again)
```

- **Forward chain:** `1 → 2 → 3 → 6`. Rules **4, 5, 7** are calc helpers consumed
  inside Rule 6. Rule 3 also reads the routing master.
- **Rule 6 is a non-delay scheduler:** it places work at the operation level and
  never leaves a machine idle while an operation is ready for it — the moment a
  machine frees up, the next batch that needs it runs. Priority (Rules 1–3) only
  breaks ties between equally-ready operations. The Rule 6 tab shows the schedule
  plus a **machine-wise view** (per-machine queue + utilization %) so you can see
  machines run continuously (the CNC bottleneck sits near 97% utilization).
- **Rule 9 reuses Rules 1–7** — it imports and re-calls them with balance
  quantities, so any fix to 1–7 flows into the loop automatically. The reuse test
  (`tests/test_rule9.py`) proves re-running with zero actuals reproduces the
  original schedule.

### Layers

| Layer | What |
|---|---|
| `engine/` | Pure Python: `config`, `models`, `loaders`, `worktime`, `pipeline`, `rules/` |
| `api/` | Thin FastAPI wrapper: `/run`, `/trace/{id}`, `/actuals`, `/rerun`, `/report` |
| `web/` | Per-rule tabs that render the trace (no per-rule UI code) + a worker-facing **📊 Gantt** tab |
| `tests/` | One file per rule + pipeline, golden snapshot, and API tests |

## Key design facts

- **`Test2.xlsx` is read-only** and is the **test/demo default**. In production the
  user **uploads** their masters/SO Excel via the "Production Excel → Upload & use"
  control; it's parsed to an in-memory `dataset_id` that every run is tagged with
  (falls back to `Test2.xlsx` when nothing is uploaded). Uploaded datasets are
  in-memory only for now — durable storage is a deferred task. The only writable
  data is `data/actuals.json` (Rule 8).
- **"Total process time" (Rule 3) = sum of per-process _cycle_ times.** This was
  confirmed against the SO Remarks oracle in the sheet — it reproduces the
  documented priorities (item `61240807-01` highest, `61247047-01` lowest); the
  sparse "Total time" column does not.
- **Machine-name normalization.** Routings write `CNC4`/`VMC1`; the master writes
  `CNC 4`/`VMC 1`. Both collapse to one canonical key, so they match with no alias
  table.
- **Fail loud, fail localized:**
  - *Loader-level* gaps are non-blocking and collected into a report:
    `PENDING_MASTER_DATA` (a routing references a machine not yet in the master,
    e.g. `CNC7` → registered as a **provisional** machine; add it to the Excel
    later with no code change) and `NO_ROUTING` (an SO item with no recipe is
    skipped). Surfaced in the UI's loader-report panel.
  - *Rule-level* contract violations raise `RuleError` — the pipeline records it
    in that rule's trace entry and stops the chain.

## Configurable parameters

| Parameter | Default | Rule |
|---|---|---|
| Consolidation window | 10 days | Rule 1 |
| Setup time per process | 90 min | Rule 4 |
| Operation overlap mode | sequential / 50% | Rule 5 |
| Parallel machine trigger | qty > 400 | Rule 7 |

All validated at run start. Edit them live in the frontend or pass a `config`
object to `POST /run`.

## Regenerating the golden snapshot

`tests/test_pipeline_golden.py` compares the forward trace to
`tests/golden_trace.json`. After an intentional logic change:

```bash
REGEN_GOLDEN=1 pytest -k golden
```
