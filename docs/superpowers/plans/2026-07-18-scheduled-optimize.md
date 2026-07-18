# Scheduled Optimize Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Automatic optimization runs ONLY Monday + Friday 11:00 IST via a GitHub cron; all event triggers removed; Done button keeps its floor meaning (update facts, no contest).

**Architecture:** GHA cron workflow → `POST /optimize/scheduled` (worker-secret) → existing `_try_start_auto()` with all guards. Event-trigger machinery (`_bump_book_changed`, `_AUTO` pending, `/optimize/done`) removed. Spec: `docs/superpowers/specs/2026-07-18-scheduled-optimize-design.md`.

## Global Constraints
- `python3 -m pytest` green at every task end; golden untouched (no regen). Baseline 362 passed, 1 skipped.
- Commits end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Branch `scheduled-optimize`. No push.
- Cron exact: `30 5 * * 1,5` (= Mon & Fri 11:00 IST). IST offset for note display: UTC+5:30.
- `AUTO_OPTIMIZE=0` still disables `_try_start_auto` (test isolation unchanged).

### Task 1: API — scheduled endpoint in, event triggers out
**Files:** Modify `api/main.py`, `tests/test_auto_optimize.py`; grep-remove every `_bump_book_changed()` call (upload, orders delete/clear, commit/urgent/uncommit, /run persist branch).
- Add endpoint after the worker endpoints:
```python
@app.post("/optimize/scheduled")
def optimize_scheduled_ep(request: Request):
    """The twice-weekly trigger (GitHub cron; worker-secret auth). All of
    _try_start_auto's guards apply — cloud-only, one-at-a-time, and the
    book-fingerprint skip when nothing changed since the last applied plan."""
    _require_worker(request)
    started = _try_start_auto()
    return {"started": started, "state": _optimize_status()["state"]}
```
- Gatekeeper bypass list: add `("POST", "/optimize/scheduled")` alongside progress/result.
- Delete: `_bump_book_changed`, `_drain_pending_auto` (+ its call in `_finalize_optimize`), the `_AUTO` dict/lock, `/optimize/done` endpoint. `_try_start_auto` loses the pending branch: when a contest is already running it just returns False.
- `_auto_note_write`: timestamps in IST — `(_dt.utcnow() + timedelta(hours=5, minutes=30))`, note text keeps `%H:%M`; `_auto_apply_result` stamp same. Skip-note copy → "cloud compute unavailable; will retry on the next scheduled run."
- Tests (rewrite `tests/test_auto_optimize.py` keeping `_api`/`_seed_book`/`_auto_env` helpers): scheduled endpoint 401 without secret / starts with secret (monkeypatched `_start_optimize`); admin mutations (upload via TestClient, commit) do NOT start contests; `/optimize/done` → 404/405; sig-match ⇒ scheduled returns started=False; AUTO_OPTIMIZE=0 ⇒ started=False; auto-note timestamp is IST (freeze: compare against utcnow+5:30 within tolerance).
- Steps: RED → implement → focused file → full suite → commit.

### Task 2: Workflow + web
**Files:** Create `.github/workflows/scheduled-optimize.yml`; modify `web/app.js`, `web/index.html`.
```yaml
name: scheduled-optimize
on:
  schedule:
    - cron: "30 5 * * 1,5"   # Mon & Fri 11:00 IST — after the ~10:00 feedback entry
  workflow_dispatch: {}
concurrency: optimize
jobs:
  trigger:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Wake the app and start the scheduled optimization
        env:
          APP_URL: ${{ secrets.APP_URL }}
          SECRET: ${{ secrets.OPTIMIZE_WORKER_SECRET }}
        run: |
          for i in $(seq 1 10); do
            code=$(curl -s -o /tmp/resp.json -w '%{http_code}' -X POST \
              -H "X-Worker-Secret: $SECRET" --max-time 90 \
              "$APP_URL/optimize/scheduled" || echo 000)
            echo "attempt $i: HTTP $code $(cat /tmp/resp.json 2>/dev/null)"
            [ "$code" = "200" ] && exit 0
            sleep 30
          done
          echo "app never answered"; exit 1
```
- Web: button label → "Done entering — update plan"; onclick → `runPlan(false)` then status "Entries saved — plan updated. Next optimization: <next Mon/Fri 11:00, DD-MM-YYYY>" via a small `nextScheduledOptimize()` JS helper (compute in local time; Mon=1, Fri=5, 11:00; if today is Mon/Fri before 11:00, today). Remove the `/optimize/done` fetch.
- Steps: implement → full suite (unchanged count) → commit.

### Task 3: Docs + local E2E
- Local E2E: server with `GITHUB_DISPATCH_TOKEN=manual AUTO_OPTIMIZE=1` + secret; upload sample; POST /optimize/scheduled with secret → running/auto; punch an actual + commit an order → NO contest starts; /optimize/done → 404. Kill server.
- Docs: CLAUDE.md (self-tuning bullet: triggers = Mon/Fri cron only; Done button = facts only), RULES.md self-tuning section, HANDOFF latest-session block append.
- Full suite; commit.
