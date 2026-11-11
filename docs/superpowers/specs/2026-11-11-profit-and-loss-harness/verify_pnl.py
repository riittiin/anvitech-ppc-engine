"""Profit and Loss verification on a COPY of a local store built from the owner's
workbook (build_store.py). Drives the REAL API (TestClient on api.main) at a fixed
IST clock; the source store is never written.

Checks (spec section 13 / task 7):
  A. the snapshot holds exactly the active orders with ZERO punches that have a
     counted (CNC/VMC/inspection) step; orders punched before the first snapshot
     are absent;
  B. clubbed batches: for every counted op with 2+ lines, the members' setup + run
     minutes sum to the op's setup + Q x ORIGINAL cycle time (unpadded); and the
     op's setup is the plan's setup (90 min CNC/VMC, 0 inspection) where derivable;
  C. frozen at first punch: orders punched AFTER the first snapshot keep their
     snapshot byte-for-byte on the next /run, while unpunched orders are refreshed;
  D. one order end to end: every routing step punched through POST /actuals (with
     machine, shift, shift_minutes, operator; one counted step split over two
     overbooked punches on the same machine-shift, one with downtime), marked
     complete, then its /profit-loss row recomputed by hand from the raw inputs;
  E. role gate: /profit-loss 403 for the user role; /machines carries no rate for
     the user role, and does for admin.

Usage: DEFAULT_SCHEDULER=new .venv/bin/python -B verify_pnl.py <store_dir>
       (PNL_KEEP_STORE=<new dir> keeps the end-state store)
"""
import json, os, shutil, sys, tempfile
from collections import defaultdict
from datetime import datetime

SRC = sys.argv[1]
work = tempfile.mkdtemp(prefix="pnl-verify-")
shutil.copytree(SRC, os.path.join(work, "s"))
for k in ("MONGODB_URI", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN",
          "GITHUB_DISPATCH_TOKEN", "OPTIMIZE_WORKER_SECRET"):
    os.environ.pop(k, None)
ADMIN = ("verify-admin", "verify-admin-pw-" + os.urandom(4).hex())
USER = ("verify-user", "verify-user-pw-" + os.urandom(4).hex())
os.environ.update(DEFAULT_SCHEDULER="new", AUTO_OPTIMIZE="0", STORE_DIR=os.path.join(work, "s"),
                  ADMIN_USERNAME=ADMIN[0], ADMIN_PASSWORD=ADMIN[1],
                  USER_USERNAME=USER[0], USER_PASSWORD=USER[1])
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient  # noqa: E402
from api import main as m  # noqa: E402
from engine import book_store, orderbook  # noqa: E402
from engine.loaders import normalize_process_name  # noqa: E402
from engine.planning_time import planning_cycle_time  # noqa: E402
from ppc_engine.loaders.normalize import machine_kind_from_type  # noqa: E402

CLOCK = {"now": datetime(2026, 11, 11, 7, 0)}
m._ist_now = lambda: CLOCK["now"]
OFF = ("OS / Outsourced", "Off-machine")
FAILS = []


def check(name, ok, **nums):
    print(("PASS " if ok else "FAIL ") + name + "  " + json.dumps(nums, default=str)[:600])
    if not ok:
        FAILS.append(name)


def login(u):
    c = TestClient(m.app)
    assert c.post("/login", data={"username": u[0], "password": u[1]},
                  follow_redirects=False).status_code == 303
    return c


A, U = login(ADMIN), login(USER)


def run():
    m._PLAN_CACHE["key"] = None
    m._PLAN_CACHE["result"] = None
    r = A.post("/run", json={})
    assert r.status_code == 200, r.text[:300]
    art = m._PLAN_CACHE["artifacts"]
    return art["plan_run"], art["masters"], art["config"]


def kinds():
    doc = book_store.load_machines_doc() or {"machines": {}}
    return {mid: machine_kind_from_type((r or {}).get("type")).value
            for mid, r in doc["machines"].items()}


def punch(so, item, process, machine, operator, qty, shift="1st shift", minutes=0.0,
          complete=False, day=None, **extra):
    body = {"so_no": so, "item_code": item,
            "entry_date": day or CLOCK["now"].date().isoformat(),
            "shift": shift, "process": process, "operator": operator, "qty_produced": qty,
            "machine": machine, "shift_minutes": minutes, "mark_complete": complete, **extra}
    r = A.post("/actuals", json=body)
    assert r.status_code == 200, (so, item, process, r.text[:300])


def first_step_entries(sched, masters, skip):
    first = {}
    for e in sched:
        if e.machine in OFF or not e.operator:
            continue
        lo = min(p.seq for p in masters.routings[e.item_code].processes)
        if e.process_seq != lo:
            continue
        for so in e.so_refs:
            k = (so, e.item_code)
            if k not in skip and k not in first:
                first[k] = e
    return first


# ---------------------------------------------------------------- A. snapshot set
real_snapshot = m._snapshot_pnl
m._snapshot_pnl = lambda *a, **k: None
pr, masters, cfg = run()
assert book_store.load_pnl_planned() == {}
pre = sorted(first_step_entries(pr.schedule, masters, set()))[:3]
fs = first_step_entries(pr.schedule, masters, set())
for k in pre:
    e = fs[k]
    punch(k[0], k[1], e.process_name, e.machine, e.operator, 1, minutes=60)
m._snapshot_pnl = real_snapshot
A.get("/machines")                    # seeds default rates (no plan effect, see plan_hash)
pr, masters, cfg = run()
snap1 = book_store.load_pnl_planned()
K = kinds()
punched = {a.key for a in book_store.load_actuals()}
active = set(book_store.load_active_orders())
counted_lines = set()
for e in pr.schedule:
    if K.get(e.machine) in ("machining", "inspection"):
        for so in (e.piece_refs or e.so_refs):
            counted_lines.add((so, e.item_code))
expected = {f"{so}\x1f{it}" for so, it in (active - punched) & counted_lines}
no_counted = (active - punched) - counted_lines
check("A1 snapshot == active, zero-punch orders with a counted step",
      set(snap1) == expected, snapshot=len(snap1), expected=len(expected),
      active=len(active), punched=len(punched), unpunched_without_counted_step=len(no_counted),
      extra=sorted(set(snap1) - expected)[:5], missing=sorted(expected - set(snap1))[:5])
check("A2 every order punched before the first snapshot is absent",
      not any(f"{so}\x1f{it}" in snap1 for so, it in punched), punched_before=pre)

# ---------------------------------------------------------------- B. clubbed ops
ops = defaultdict(lambda: {"occ": 0.0})
for e in pr.schedule:
    if K.get(e.machine) not in ("machining", "inspection"):
        continue
    op = ops[(e.batch_id, e.process_seq, bool(e.resumed))]
    op.setdefault("e", e)
    op["occ"] += float(e.occupancy_min)
n_club = mism = 0
setups = defaultdict(int)
details = []
for (bid, seq, res), op in ops.items():
    e = op["e"]
    members = list(e.piece_refs or e.so_refs)
    if len(members) < 2:
        continue
    step = normalize_process_name(e.process_name)
    proc = next(p for p in masters.routings[e.item_code].processes
                if normalize_process_name(p.name) == step)
    q = float(e.qty)
    setup_op = max(0.0, op["occ"] - q * planning_cycle_time(proc, cfg))
    setups[(K[e.machine], round(setup_op, 1))] += 1
    want = setup_op + q * float(proc.cycle_time)
    got = 0.0
    for so in members:
        doc = snap1.get(f"{so}\x1f{e.item_code}")
        if doc is None:
            got = None
            break
        got += sum(s["setup_min"] + s["run_min"] for s in doc["steps"]
                   if s["step"] == step and s["machine"] == e.machine)
    if got is None:
        continue                     # a member is started: not snapshotted (by design)
    n_club += 1
    if abs(got - want) > 1e-6:
        mism += 1
        details.append((bid, e.process_name, e.machine, members, round(got, 3), round(want, 3)))
check("B1 clubbed ops: members' planned minutes sum to setup + Q x original cycle",
      n_club > 0 and mism == 0, clubbed_ops_checked=n_club, mismatches=mism, first=details[:3])
check("B2 op setup derived from the plan is 90 (CNC/VMC) or 0 (inspection)",
      all((k == "machining" and abs(s - 90) < 0.05) or (k == "inspection" and s == 0)
          for (k, s) in setups), setup_distribution={f"{k}:{s}": n for (k, s), n in setups.items()})

# ---------------------------------------------------------------- C. freeze at first punch
fs = first_step_entries(pr.schedule, masters, punched)
later = [k for k in sorted(fs) if f"{k[0]}\x1f{k[1]}" in snap1][:3]
for k in later:
    e = fs[k]
    punch(k[0], k[1], e.process_name, e.machine, e.operator, 1, minutes=60)
CLOCK["now"] = datetime(2026, 11, 11, 7, 30)
pr2, masters, cfg = run()
snap2 = book_store.load_pnl_planned()
from engine import profit_loss as pl  # noqa: E402
fresh2 = pl.planned_steps(pr2.schedule, pr2.batches_prioritized, masters, cfg, K)
frozen_same = all(snap2[f"{so}\x1f{it}"] == snap1[f"{so}\x1f{it}"] for so, it in later)
moved = sum(1 for k in later if fresh2.get(k) != snap1[f"{k[0]}\x1f{k[1]}"]["steps"])
refreshed = sum(1 for kk, v in snap2.items()
                if tuple(kk.split("\x1f")) not in set(later) | punched and v["at"] != snap1.get(kk, {}).get("at"))
check("C1 orders punched after the first snapshot keep it byte-for-byte",
      frozen_same, later_punched=later, whose_plan_figure_changed_since=moved)
check("C2 unpunched orders are refreshed (new timestamp) on the next compute",
      refreshed == len(snap2) - len(later), refreshed=refreshed, snapshot=len(snap2))

# ---------------------------------------------------------------- D. one order end to end
rates = pl.rates_from_doc(book_store.load_machines_doc())
cands = []
has_punch = {a.key for a in book_store.load_actuals()}
for kk, doc in snap2.items():
    so, it = kk.split("\x1f")
    if (so, it) in has_punch:
        continue                      # punched in phase A/C: not a clean end-to-end case
    st = doc["steps"]
    mach = {s["machine"] for s in st}
    ents = [e for e in pr2.schedule if so in e.so_refs and e.item_code == it]
    if (len(st) >= 2 and len(mach) >= 2 and all(x in rates for x in mach)
            and all(len(e.piece_refs or e.so_refs) == 1 for e in ents)
            and any(K[x] == "machining" for x in mach)):
        cands.append((-len(st), kk))
assert cands, "no unclubbed multi-step fully-rated order in the snapshot"
kk = sorted(cands)[0][1]
so, it = kk.split("\x1f")
order = book_store.load_active_orders()[(so, it)]
Q = order.ordered_qty
plan_e = {}
for e in pr2.schedule:
    if so in e.so_refs and e.item_code == it:
        plan_e.setdefault(e.process_seq, e)
procs = sorted(masters.routings[it].processes, key=lambda p: p.seq)
ops_on_file = {o.name for o in masters.operators}
any_op = sorted(ops_on_file)[0]
hand_actual = defaultdict(float)   # (step, machine) -> minutes, computed by hand below
split_done = False
for i, p in enumerate(procs):
    e = plan_e.get(p.seq)
    last = i == len(procs) - 1
    mach = e.machine if e is not None and e.machine not in OFF else ""
    oper = e.operator if e is not None and e.operator else any_op
    if orderbook.process_is_outsourced(masters.routings.get(it), p.name):
        oper = ""                      # OS steps take no operator (owner rule, 2026-07-26)
    kind = K.get(mach)
    step = normalize_process_name(p.name)
    # Each step on its own past date, so no two steps (and no punch from phases A/C)
    # share a machine-shift: the only overbooked shift is the deliberate one below.
    day = f"2026-10-{10 + i:02d}"
    if kind in ("machining", "inspection") and not split_done and kind == "machining":
        # Overbooked shift: two punches on the same machine/date/shift claiming
        # 400 + 400 > 630 working minutes; the second has 30 min of downtime.
        h1, h2 = Q // 2, Q - Q // 2
        punch(so, it, p.name, mach, oper, h1, minutes=400, actual_setup_min=90, day=day)
        punch(so, it, p.name, mach, oper, h2, minutes=400, tool_problem_min=30, complete=last, day=day)
        w1, w2 = h1 * p.cycle_time + 90, h2 * p.cycle_time
        hand_actual[(step, mach)] += 630 * w1 / (w1 + w2) + (630 * w2 / (w1 + w2) - 30)
        split_done = True
    elif kind in ("machining", "inspection"):
        mins = 300 if kind == "machining" else 120
        punch(so, it, p.name, mach, oper, Q, shift="2nd shift", minutes=mins, no_power_min=15,
              complete=last, day=day)
        hand_actual[(step, mach)] += mins - 15
    else:
        punch(so, it, p.name, mach, oper, Q, minutes=0, complete=last, day=day)
check("D0 order completed through POST /actuals", (so, it) in book_store.load_completed_orders(),
      order=kk, qty=Q, steps=len(procs))
pr3, _, _ = run()                     # the finished order leaves the plan
check("D1 the finished order's snapshot survives leaving the plan",
      book_store.load_pnl_planned().get(kk) == snap2[kk])
rep = A.get("/profit-loss")
assert rep.status_code == 200, rep.text[:300]
rep = rep.json()
row = next(r for r in rep["rows"] if r["so_no"] == so and r["item_code"] == it)
plan_by = defaultdict(float)
for s in snap2[kk]["steps"]:
    plan_by[(s["step"], s["machine"])] += s["setup_min"] + s["run_min"]
hand_pc = sum(v / 60 * rates[mm] for (_, mm), v in plan_by.items())
hand_ac = sum(v / 60 * rates[mm] for (_, mm), v in hand_actual.items())
hand_ph = sum(plan_by.values()) / 60
hand_ah = sum(hand_actual.values()) / 60
r2 = lambda v: round(v, 2)  # noqa: E731
check("D2 /profit-loss row matches the hand calculation",
      row["planned_hours"] == r2(hand_ph) and row["actual_hours"] == r2(hand_ah)
      and row["planned_cost"] == r2(hand_pc) and row["actual_cost"] == r2(hand_ac)
      and row["diff_cost"] == r2(hand_ac - hand_pc) and not row["not_punched"]
      and not row["rates_missing"],
      row={k: row[k] for k in ("so_no", "item_code", "qty", "completed_on", "planned_hours",
                               "planned_cost", "actual_hours", "actual_cost", "diff_cost",
                               "diff_pct", "not_punched", "rates_missing")},
      hand={"planned_hours": r2(hand_ph), "planned_cost": r2(hand_pc),
            "actual_hours": r2(hand_ah), "actual_cost": r2(hand_ac)},
      steps=[(s["process"], s["planned_machine"], s["planned_hours"], s["actual_machines"],
              s["actual_hours"], s["status"]) for s in row["steps"]])
# Independent of the snapshot: planned minutes from the ROUTING alone (Excel cycle
# time x qty, plus the plan's 90-minute setup on CNC/VMC, none on inspection).
route_min = 0.0
for p in procs:
    e = plan_e.get(p.seq)
    kd = K.get(e.machine) if e is not None else None
    if kd in ("machining", "inspection"):
        route_min += (cfg.setup_time_min if kd == "machining" else 0) + Q * float(p.cycle_time)
check("D2b planned hours equal the routing by hand (Excel cycle x qty + setup, unpadded)",
      row["planned_hours"] == r2(route_min / 60), row=row["planned_hours"],
      routing_by_hand=r2(route_min / 60))
check("D3 totals count the one complete row", rep["totals"]["orders"] == 1
      and rep["totals"]["counted"] == 1, totals=rep["totals"], not_shown=rep["not_shown"],
      older=rep["older_count"])

# ---------------------------------------------------------------- E. role gate
gu = U.get("/machines").json()["machines"]
ga = A.get("/machines").json()["machines"]
check("E1 /profit-loss is 403 for the user role", U.get("/profit-loss").status_code == 403)
check("E2 user /machines has no rate, admin's does",
      all("rate" not in x for x in gu) and all("rate" in x for x in ga),
      admin_rates={x["id"]: x["rate"] for x in ga if x.get("rate")})

print("RESULT", "ALL PASS" if not FAILS else f"FAILED: {FAILS}")
if os.environ.get("PNL_KEEP_STORE"):          # keep the end state, e.g. to open in a browser
    shutil.copytree(os.path.join(work, "s"), os.environ["PNL_KEEP_STORE"])
shutil.rmtree(work, ignore_errors=True)
