"""Plan-identity probe: is the schedule the same with and without Profit and Loss?

Copies a store into a scratch dir (the source is never written), drives the REAL API
(TestClient on api.main) at a fixed IST clock, and prints one hash per stage of the
schedule /run built (machine, operator, every per-shift operator segment, start, end,
qty, SO refs per entry). Run from the repo root of the commit under test, so it works
on the base commit (no Profit and Loss) and on the feature branch alike.

Stages: (1) first /run; (2) GET /machines (seeds the default rates on the feature
branch), then /run; (3) three punches (first counted step of three orders, through
POST /actuals with machine/shift/shift_minutes/operator), then /run; (4) a second
round of punches, then /run. The plan cache is cleared before every /run so each one
recomputes (a cache hit would prove nothing).

  --noop   monkeypatch api.main._snapshot_pnl to a no-op (feature branch only)

Usage: DEFAULT_SCHEDULER=new .venv/bin/python -B plan_hash.py <store_dir> [--noop]
"""
import hashlib, json, os, shutil, sys, tempfile
from datetime import datetime

SRC = sys.argv[1]
NOOP = "--noop" in sys.argv
work = tempfile.mkdtemp(prefix="pnl-hash-")
shutil.copytree(SRC, os.path.join(work, "s"))
for k in ("MONGODB_URI", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
          "GITHUB_DISPATCH_TOKEN", "OPTIMIZE_WORKER_SECRET"):
    os.environ.pop(k, None)
ADMIN = ("verify-admin", "verify-admin-pw-" + os.urandom(4).hex())
os.environ.update(DEFAULT_SCHEDULER="new", AUTO_OPTIMIZE="0", STORE_DIR=os.path.join(work, "s"),
                  ADMIN_USERNAME=ADMIN[0], ADMIN_PASSWORD=ADMIN[1])
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from api import main as m  # noqa: E402

m._ist_now = lambda: datetime(2026, 11, 11, 7, 0)
if NOOP:
    assert hasattr(m, "_snapshot_pnl"), "--noop on a commit without the feature"
    m._snapshot_pnl = lambda *a, **k: None
c = TestClient(m.app)
assert c.post("/login", data={"username": ADMIN[0], "password": ADMIN[1]},
              follow_redirects=False).status_code == 303


def run():
    m._PLAN_CACHE["key"] = None
    m._PLAN_CACHE["result"] = None
    r = c.post("/run", json={})
    assert r.status_code == 200, r.text[:300]
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    rows = sorted(json.dumps([e.batch_id, e.process_seq, e.machine, e.operator,
                              [[s.isoformat(), t.isoformat(), o] for s, t, o in (e.op_segments or [])],
                              e.start.isoformat(), e.end.isoformat(), float(e.qty),
                              sorted(e.so_refs)], default=str) for e in sched)
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16], len(sched), sched


def punch_round(sched, skip, n=3):
    """Punch qty 1 on the FIRST routing step of n orders not yet punched, on the
    machine and by the operator the plan gives that step."""
    first = {}
    for e in sched:
        if e.machine in ("OS / Outsourced", "Off-machine") or not e.operator:
            continue
        for so in e.so_refs:
            k = (so, e.item_code)
            if k in skip:
                continue
            if k not in first or e.process_seq < first[k].process_seq:
                first[k] = e
    done = []
    for k in sorted(first):
        e = first[k]
        if e.process_seq != min(p.seq for p in m._current_masters().routings[e.item_code].processes):
            continue
        r = c.post("/actuals", json={
            "so_no": k[0], "item_code": k[1], "entry_date": "2026-11-11", "shift": "1st shift",
            "process": e.process_name, "operator": e.operator, "qty_produced": 1,
            "machine": e.machine, "shift_minutes": 120})
        assert r.status_code == 200, (k, r.text[:300])
        done.append(k)
        if len(done) == n:
            break
    return done


out = []
h, n, s = run(); out.append(("first /run", h, n))
c.get("/machines")
h, n, s = run(); out.append(("after GET /machines", h, n))
p1 = punch_round(s, set())
h, n, s = run(); out.append(("after 3 punches", h, n))
p2 = punch_round(s, set(p1))
h, n, s = run(); out.append(("after 6 punches", h, n))
print(json.dumps({"noop": NOOP, "stages": out, "punched": p1 + p2}))
shutil.rmtree(work, ignore_errors=True)
