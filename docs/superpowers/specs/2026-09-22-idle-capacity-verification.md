# Idle capacity in the delay report — verification record (2026-09-22)

Owner escalation: the 22-09-2026 delay justification report showed 46.9 order-days
of `IDLE (capacity free)` ("machine and operator both free, nothing scheduled"), for
whole shifts at a time, on orders 16 to 22 days late. This is the evidence behind the
CLAUDE.md banner entry of that date, every harness inline so the next person re-runs
them instead of trusting the numbers.

Inputs (NOT the live store — no credential in the session): `~/Desktop/Latest
Master/Test9 (5) (1) (1) (1).xlsx` (the workbook the owner uploaded 17-09),
`~/Desktop/Anvitech_actuals_backup_2026-09-17.json` (punches to 17-09), the
workbook's operator sheet as the crew (the live Settings table may differ), plan
start 22-09-2026 08:00, `apply_operator_logic=True`, overlap 88, consolidation
window 1 day; plus Test5 (no WIP) and Test8 (30 orders part-finished by punching
the first step to 40%). "Before" = a git worktree at HEAD (the same code minus the
fix), so the comparison is about the placement change alone.

## 1. What the report's own rows said

1,128 h of IDLE over 256 windows, 463 h of it in full-shift rows on DTC2 / MD1 /
MLATHE. Every manual station was run by Anturam alone, in series; Sanjay (qualified
for all of them) had no work until 17-10; HP2 none at all. Prediction, to be
confirmed in Settings > Operator absences: Sanjay is on leave 22-09 through 16-10.

## 2. Cause 1 — the report never saw leave (fixed)

`build_delay_report` had no absences argument; `_staffing_split` counted anyone with
no booking as free. Fixed: `absences=`, `_leave_days`, `_shift_anchor_day`, and a
state of its own, `WAITING (crew on leave)`, naming the person. On the Sep-17 book a
leave for Sanjay turns 404 h of crew shortage into "idle" in an absence-blind report
(`repro.py`). Tests: `tests/test_delay_report_attribution.py`.

## 3. Cause 2 — the placement step produced the holes (fixed at the source)

The owner withdrew his 2026-08-09 approval of a post-pass that fills holes (it was
built, measured, and unwound the same day): the engine must not produce them.

**The metric (`holes.py`):** hours where a machine is idle inside a working window,
an operation whose previous routing step has FINISHED could run on it (the machine
is among its routing options), and a qualified person on that shift is free by the
engine's own rule (pool member, on shift, not on leave, no booking). Two traps in
earlier versions of it: treating an op's whole span as busy hid the windows a
machine idled inside a job (fixed: real cutting segments), and counting a FINISHED
op as waiting added 4,872 phantom hours (fixed: an op waits only until its end).

**What the placement saw (`audit.py`, old code, Test9 real / Test8):** of the
idle-window hours with an op waiting on the machine, 1,366 / 3,387 h had somebody
free for the WHOLE window (the machine's pointer had jumped past the hole), 1,290 /
1,751 h somebody free for PART of it (the window was refused unless one person was
free for all of it), 576 / 1,253 h nobody free (a real crew shortage).

**The fix** (`ppc_engine/scheduler/flow_scheduler.py`, `staffing.py`): stretch-based
laying (`_lay_windows`, `_next_stretch`), per-machine committed spans instead of
one "free from" datetime (`machine_spans`, `_free_runs`), manual/inspection work
laid around other jobs and CNC/VMC work whole (`_lay_around`), the frozen path
through the same rule (`_lay_pinned`); sorted bookings with bisect and a cached
eligible-people roster on the board.

| book | dispatch | late-days before → after | hole-hours before → after |
|---|---|---|---|
| Test9-latest, real punches | gt (shipped) | 958 → 868 | 308.1 → 76.0 |
| Test9-latest, real punches | nondelay | 958 → 881 | 308.1 → 65.7 |
| Test9-latest, wip=all | gt | 1839 → 1768 | 443.0 → 86.2 |
| Test9-latest, wip=all | nondelay | 1839 → 1867 | 443.0 → 74.3 |
| Test5, wip=0 | gt | 4799 → 4648 | 585.8 → 73.9 |
| Test5, wip=0 | nondelay | 4799 → 4653 | 585.8 → 95.4 |
| Test8, wip=30 | gt | 5476 → 5429 | 349.3 → 135.7 |
| Test8, wip=30 | nondelay | 5476 → 5657 | 349.3 → 88.1 |

Routing / qualification / batch-quantity violations: 0 in every run. The
Giffler-Thompson dispatch stays: non-delay has fewer holes on three books but
worse late-days on three, and late-days are the owner's objective.

**Residual (`residual.py`, gt):** Test9 real 224 of 241 h, Test8 481 of 500 h are
CNC/VMC stretches shorter than the waiting job — left because a CNC job is never
split around another job (a second 90-minute setup). The report now names this on
the row. Splitting and paying the setup would be a one-line change in `_lay_around`,
the owner's call.

**Cost (`timing.py`, best of 3, nothing else running):** Test5 288 → 264 ms,
Test9-latest 474 → 668 ms per plan.

**Mutation testing (`tests/test_no_idle_holes.py`):** whole-window rule reverted →
1 test fails; scalar pointer → 4; manual work never around a job → 2; CNC job split
around a job → 1. A mutation written within the same second as the restore was
masked by stale bytecode: sleep 1 s or run pytest with `-B`.

## Harnesses

### `repro.py`

```python
"""Reproduce the 22-09-2026 delay-report pattern on the latest workbook with the
real September punches (Desktop backup of 2026-09-17), in a throwaway store."""
import io, json, os, sys, tempfile, collections
from datetime import date, datetime, timedelta
os.environ["STORE_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, os.getcwd())
from engine import loaders, new_engine, orderbook, book_store, freeze, delay_report as dr
from engine.config import Config
from engine.models import PlanRun, Order, Actual
from engine.pipeline import run_forward
from engine.optimize_service import absence_reservations
from engine.operator_coverage import qualified_operators

WB = "/Users/ritinwadekar/Desktop/Latest Master/Test9 (5) (1) (1) (1).xlsx"
ACT = "/Users/ritinwadekar/Desktop/Anvitech_actuals_backup_2026-09-17.json"
PLAN_START = date(2026, 9, 22)
raw = open(WB, "rb").read()
book_store.save_masters_bytes(raw); new_engine.set_masters_bytes(raw)
so_lines, masters = loaders.load_all(io.BytesIO(raw))
cfg = Config(scheduler="new", plan_start_date=PLAN_START, apply_operator_logic=True,
             overlap_percent=88, consolidation_window_days=1)
orders = {}
for l in so_lines:
    o = Order(so_no=l.so_no, item_code=l.item_code, item_name=l.item_name,
              ordered_qty=float(l.qty), delivery_date=l.delivery_date)
    orders[o.key] = o
acts = [Actual.from_json(d) for d in json.load(open(ACT))]
acts = [a for a in acts if (a.so_no, a.item_code) in orders]
print(f"book: {len(orders)} orders, {len(acts)} punches on them, "
      f"operators in masters: {len(masters.operators)}")

def derive_frozen(lines, c):
    pr0 = PlanRun(so_lines=list(lines)); run_forward(pr0, c, masters)
    applied = freeze.schedule_projection(pr0.schedule)
    gbs = {}
    for a in acts:
        k = (a.so_no, a.item_code, loaders.normalize_process_name(a.process))
        gbs[k] = gbs.get(k, 0.0) + a.good_qty()
    return freeze.compute_frozen_set(applied, lines, gbs, masters)

def plan(absences):
    lines = orderbook.active_so_lines(orders, acts, masters)
    reserved = absence_reservations(absences) or None
    frozen = derive_frozen(lines, cfg)
    pr = PlanRun(so_lines=lines)
    run_forward(pr, cfg, masters, reserved=reserved, frozen=frozen or None)
    return pr, lines

def summarize(tag, pr, lines, absences):
    rep = dr.build_delay_report(pr.schedule, lines, pr.batches_prioritized, cfg, masters,
                                None, absences)
    st = collections.Counter(); hrs = collections.Counter()
    idle_m = collections.Counter(); idle_long = 0.0
    for r in rep["detail"]:
        st[r["State"]] += 1; hrs[r["State"]] += r["Hours"]
        if r["State"].startswith("IDLE"):
            idle_m[r["Machine"]] += r["Hours"]
            if r["Hours"] >= 8: idle_long += r["Hours"]
    busy = dr._operator_bookings(pr.schedule)
    who = {n: round(sum((e - s).total_seconds() / 3600 for s, e in v), 1) for n, v in busy.items()}
    print(f"\n=== {tag}: {len(lines)} active lines, {len(pr.schedule)} entries, "
          f"late-days {sum(max(0, s['Days Late']) for s in rep['summary'])}")
    print("  hours by state:", {k: round(v, 1) for k, v in hrs.items()})
    print("  idle hours by machine:", {k: round(v, 1) for k, v in idle_m.most_common(8)},
          "| in rows >= 8h:", round(idle_long, 1))
    print("  helper hours:", {n: who.get(n, 0) for n in ("Anturam", "Sanjay", "Sandeep Kumar", "HP1", "HP2")})
    first = {n: min((s for s, e in v), default=None) for n, v in busy.items()}
    print("  Sanjay first booking:", first.get("Sanjay"))
    return rep

pr0, lines0 = plan([])
rep0 = summarize("NO absences", pr0, lines0, [])
absn = [{"id": "x", "operator": "Sanjay", "from_date": "2026-09-22", "to_date": "2026-10-16"}]
pr1, lines1 = plan(absn)
rep1 = summarize("Sanjay on leave 22-09..16-10", pr1, lines1, absn)
rep1_blind = dr.build_delay_report(pr1.schedule, lines1, pr1.batches_prioritized, cfg, masters)
blind_idle = sum(r["Hours"] for r in rep1_blind["detail"] if r["State"].startswith("IDLE"))
aware_idle = sum(r["Hours"] for r in rep1["detail"] if r["State"].startswith("IDLE"))
print(f"\nSame plan, report WITHOUT absences: idle {blind_idle:.1f} h; WITH: {aware_idle:.1f} h")

# ---- RC2 evidence: idle machine windows where a qualified, present operator was
# free for the REST of the window and an op for that machine was ready (its
# predecessor had ended) but was placed later.
def harvestable(pr, lines, absences):
    leave = dr._leave_days(absences)
    busy = dr._operator_bookings(pr.schedule)
    rep = dr.build_delay_report(pr.schedule, lines, pr.batches_prioritized, cfg, masters, None, absences)
    # ready ops per order: (machine, ready_at, actual start)
    by_order = collections.defaultdict(list)
    for e in pr.schedule:
        if e.machine in dr._OFF_LANES or e.end <= e.start: continue
        by_order[(e.batch_id, e.item_code)].append(e)
    waits = []   # (machine, ready, start, entry)
    for k, es in by_order.items():
        es.sort(key=lambda e: e.process_seq)
        prev_end = None
        for e in es:
            ready = prev_end if prev_end else e.start
            if ready < e.start: waits.append((e.machine, ready, e.start, e))
            prev_end = max(prev_end or e.end, e.end)
    total = 0.0; examples = []
    for r in rep["detail"]:
        if not r["State"].startswith("IDLE"): continue
        m, a, b = r["Machine"], r["From"], r["To"]
        cand = [w for w in waits if w[0] == m and w[1] <= a and w[2] >= b]
        if cand:
            total += r["Hours"]; examples.append((m, a, b, r["Hours"], cand[0][3].batch_id, cand[0][3].process_name, cand[0][2]))
    print(f"  IDLE hours with a READY op waiting for that machine: {total:.1f} h over {len(examples)} windows")
    for ex in sorted(examples, key=lambda x: -x[3])[:6]:
        print("   ", ex)
print("\nRC2 (engine) — no absences:"); harvestable(pr0, lines0, [])
print("RC2 (engine) — Sanjay on leave:"); harvestable(pr1, lines1, absn)
```

### `holes.py`

```python
"""HOLES: hours where a machine is idle inside a working window, an operation whose
previous step has FINISHED could run on it (the machine is among its routing options),
and a qualified operator on that shift is free by the engine's own rule (pool member,
on shift, not on leave, no booking). Compared across dispatch policies."""
import io, json, os, sys, tempfile, collections, functools
from datetime import date, datetime, timedelta
os.environ["STORE_DIR"] = tempfile.mkdtemp(); sys.path.insert(0, os.getcwd())
from engine import loaders, new_engine, orderbook, book_store, freeze, delay_report as dr
from engine.config import Config
from engine.models import PlanRun, Order, Actual
from engine.pipeline import run_forward
from engine.optimizer import expected_completion
from ppc_engine.scheduler import decode as real_decode
from ppc_engine.scheduler.staffing import build_machine_pools
from ppc_engine.worktime import effective_shift, iter_windows

OFF = dr._OFF_LANES
def cfg(): return Config(scheduler="new", plan_start_date=date(2026, 9, 22), apply_operator_logic=True, overlap_percent=88, consolidation_window_days=1)
def load(wb):
    raw = open(wb, "rb").read(); book_store.save_masters_bytes(raw); new_engine.set_masters_bytes(raw)
    return loaders.load_all(io.BytesIO(raw))
def synthetic_book(so_lines, masters, wip):
    orders, actuals = {}, []
    for i, l in enumerate(so_lines):
        o = Order(so_no=l.so_no, item_code=l.item_code, item_name=l.item_name, ordered_qty=float(l.qty), delivery_date=l.delivery_date); orders[o.key] = o
        if i >= wip: continue
        routing = masters.routings.get(l.item_code)
        if routing is None or not routing.processes: continue
        good = max(1.0, round(float(l.qty) * 0.4)); good = min(good, max(1.0, float(l.qty) - 1.0))
        actuals.append(Actual(so_no=l.so_no, item_code=l.item_code, entry_date=date(2026, 9, 15), qty_produced=good, process=routing.processes[0].name, operator="", shift="1st shift", id=f"wip-{i}"))
    return orders, actuals
def real_book(so_lines, masters, path):
    orders = {}
    for l in so_lines:
        o = Order(so_no=l.so_no, item_code=l.item_code, item_name=l.item_name, ordered_qty=float(l.qty), delivery_date=l.delivery_date); orders[o.key] = o
    acts = [a for a in (Actual.from_json(d) for d in json.load(open(path))) if (a.so_no, a.item_code) in orders]
    return orders, acts
def frozen_for(lines, acts, masters, c):
    if not acts: return []
    pr0 = PlanRun(so_lines=list(lines)); run_forward(pr0, c, masters)
    gbs = {}
    for a in acts:
        k = (a.so_no, a.item_code, loaders.normalize_process_name(a.process)); gbs[k] = gbs.get(k, 0.0) + a.good_qty()
    return freeze.compute_frozen_set(freeze.schedule_projection(pr0.schedule), lines, gbs, masters)

def plan(orders, acts, masters, dispatch, frozen):
    c = cfg(); lines = orderbook.active_so_lines(orders, acts, masters)
    new_engine.decode = functools.partial(real_decode, dispatch=dispatch)
    pr = PlanRun(so_lines=lines); run_forward(pr, c, masters, frozen=frozen or None)
    return pr, lines

def holes(pr, lines, masters, frozen):
    c = cfg(); nm = new_engine._apply_app_operators(new_engine._new_masters(False), masters); pcfg = new_engine._plan_config(c)
    pools = build_machine_pools(nm)
    busy_m = collections.defaultdict(list); busy_o = dr._operator_bookings(pr.schedule)
    by_batch = collections.defaultdict(list)
    for e in pr.schedule:
        if e.machine in OFF or e.end <= e.start:
            if e.machine in OFF and e.end > e.start: by_batch[e.batch_id].append(e)
            continue
        # REAL cutting time (per-shift segments), not the entry's span: a window the
        # machine idled inside a job, waiting for its operator, is a hole too.
        for ss, se, _n in (e.op_segments or [(e.start, e.end, "")]):
            if se > ss: busy_m[e.machine].append((ss, se))
        by_batch[e.batch_id].append(e)
    busy_m = {k: dr._merge(v) for k, v in busy_m.items()}
    frozen_keys = {(r["so_no"], r["item_code"], r["op_seq"]) for r in (frozen or [])}
    # candidates: (machine option, ready_at = predecessor end, start, batch, seq, planned machine, frozen?)
    cands = collections.defaultdict(list)
    for bid, es in by_batch.items():
        es.sort(key=lambda e: (e.process_seq, e.start)); prev_end = None
        for e in es:
            if e.machine in OFF: prev_end = max(prev_end or e.end, e.end); continue
            ready = prev_end if prev_end else pcfg.plan_start
            rt = nm.routings.get(e.item_code); op = next((o for o in rt.operations if o.seq == e.process_seq), None) if rt else None
            opts = op.machine_options if op else (e.machine,)
            fr = any((so, e.item_code, e.process_seq) in frozen_keys for so in (e.so_refs or []))
            segs = [(ss, se) for ss, se, _n in (e.op_segments or [(e.start, e.end, "")]) if se > ss]
            real_start = min(ss for ss, se in segs) if segs else e.start
            real_end = max(se for ss, se in segs) if segs else e.end
            for m in opts:
                if m in nm.machines: cands[m].append((ready, real_start, bid, e.process_seq, e.machine, fr, segs, real_end))
            prev_end = max(prev_end or real_end, real_end)
    total = 0.0; src = collections.Counter(); per_machine = collections.Counter(); examples = []
    plan_end = max(e.end for e in pr.schedule)
    for m in nm.machines:
        mac = nm.machines[m]
        for win in iter_windows(mac, pcfg.plan_start, nm.calendar, pcfg):
            if win.start >= plan_end: break
            for a, b in dr._gaps(max(win.start, pcfg.plan_start), win.end, busy_m.get(m, [])):
                # operator free stretches (engine rule)
                free = []
                for o in pools.get(m, ()):
                    if effective_shift(o, win.shift_date, pcfg) != win.shift or not nm.calendar.is_operator_available(o.name, win.shift_date): continue
                    free.extend(dr._gaps(a, b, busy_o.get(o.name, [])))
                free = dr._merge(free)
                if not free: continue
                stretches = []
                tags = set()
                for ready, start, bid, seq, pm, fr, segs, real_end in cands.get(m, ()):
                    # the op is waiting whenever it is not cutting: before its first
                    # segment, and in the holes between its own segments — and only
                    # until it is FINISHED (a done op waits for nothing).
                    if real_end <= a: continue
                    waits = dr._gaps(max(a, ready), min(b, real_end), segs) if pm == m else [(max(a, ready), min(b, start))]
                    for s0, s1 in waits:
                        if s1 - s0 < timedelta(minutes=30): continue
                        for fs, fe in free:
                            x, y = max(s0, fs), min(s1, fe)
                            if y > x:
                                stretches.append((x, y))
                                tags.add("frozen op" if fr else ("other machine chosen" if pm != m else ("inside its own job" if s0 > start else "same machine, placed later")))
                stretches = dr._merge(stretches)
                h = sum((y - x).total_seconds() / 3600 for x, y in stretches)
                if h > 0:
                    total += h; per_machine[m] += h
                    for t in tags: src[t] += h / len(tags)
                    examples.append((h, m, a, b))
    return total, per_machine, src, sorted(examples, reverse=True)[:3]

if __name__ == "__main__":
    books = [("Test9-latest real punches", "/Users/ritinwadekar/Desktop/Latest Master/Test9 (5) (1) (1) (1).xlsx", "real"),
             ("Test9-latest wip=all", "/Users/ritinwadekar/Desktop/Latest Master/Test9 (5) (1) (1) (1).xlsx", "all"),
             ("Test5 wip=0", "Test5.xlsx", 0), ("Test8 wip=30", "Test8.xlsx", 30)]
    for name, path, kind in books:
        so_lines, masters = load(path)
        if kind == "real": orders, acts = real_book(so_lines, masters, "/Users/ritinwadekar/Desktop/Anvitech_actuals_backup_2026-09-17.json")
        else: orders, acts = synthetic_book(so_lines, masters, len(so_lines) if kind == "all" else kind)
        lines = orderbook.active_so_lines(orders, acts, masters)
        new_engine.decode = real_decode
        frozen = frozen_for(lines, acts, masters, cfg())
        for dispatch in ("gt", "nondelay"):
            pr, lines = plan(orders, acts, masters, dispatch, frozen)
            comp = expected_completion(pr.schedule)
            late = sum(max(0, (comp[(l.so_no, l.item_code)] - l.delivery_date).days) for l in lines if (l.so_no, l.item_code) in comp)
            total, pm, src, ex = holes(pr, lines, masters, frozen)
            viol = len(new_engine.routing_order_violations(pr.schedule, masters))
            print(f"{name:26s} {dispatch:8s} late-days {late:5d} | HOLE hours {total:7.1f} | by source {dict((k, round(v,1)) for k, v in src.most_common())} | top machines {[(k, round(v,1)) for k, v in pm.most_common(4)]} | routing viol {viol}", flush=True)
```

### `audit.py`

```python
"""Classify every hole-hour by what _lay_on_machine saw: was a qualified person free
for the WHOLE working window (then skipping it is a plain bug), or only for PART of
it (the whole-segment requirement refused the window), and was that person the
machine's own shift operator or another qualified person?"""
import sys, os, collections
SP = os.environ["SP"]; BOOK = os.environ.get("BOOK", "real"); sys.argv = [sys.argv[0]]
exec(open(SP + "/holes.py").read().split('if __name__ == "__main__":')[0])
if BOOK == "real":
    so_lines, masters = load("/Users/ritinwadekar/Desktop/Latest Master/Test9 (5) (1) (1) (1).xlsx")
    orders, acts = real_book(so_lines, masters, "/Users/ritinwadekar/Desktop/Anvitech_actuals_backup_2026-09-17.json")
else:
    so_lines, masters = load(BOOK); orders, acts = synthetic_book(so_lines, masters, 30)
lines = orderbook.active_so_lines(orders, acts, masters)
new_engine.decode = real_decode
frozen = frozen_for(lines, acts, masters, cfg())
pr, lines = plan(orders, acts, masters, os.environ.get("DISPATCH", "gt"), frozen)
c = cfg(); nm = new_engine._apply_app_operators(new_engine._new_masters(False), masters); pcfg = new_engine._plan_config(c)
pools = build_machine_pools(nm); busy_o = dr._operator_bookings(pr.schedule)
# who mans which machine in which window, from the segments
seg_by_machine = collections.defaultdict(list)
for e in pr.schedule:
    if e.machine in OFF: continue
    for ss, se, n in (e.op_segments or []):
        if se > ss and n: seg_by_machine[e.machine].append((ss, se, n, e.batch_id, e.process_name))
total, pm, src, ex = holes(pr, lines, masters, frozen)
print(f"{os.environ.get('DISPATCH', 'gt')} on {BOOK}: HOLE hours {total:.1f}, sources {dict((k, round(v,1)) for k, v in src.most_common())}")
# re-derive the stretches with detail (same loop as holes(), but keep them)
cat = collections.Counter(); samples = collections.defaultdict(list)
plan_end = max(e.end for e in pr.schedule)
busy_m = collections.defaultdict(list)
for m_, segs in seg_by_machine.items():
    busy_m[m_] = dr._merge([(s, t) for s, t, *_ in segs])
for m in nm.machines:
    mac = nm.machines[m]
    for win in iter_windows(mac, pcfg.plan_start, nm.calendar, pcfg):
        if win.start >= plan_end: break
        ws, we = max(win.start, pcfg.plan_start), win.end
        on_shift = [o.name for o in pools.get(m, ()) if effective_shift(o, win.shift_date, pcfg) == win.shift and nm.calendar.is_operator_available(o.name, win.shift_date)]
        owner = next((n for s, t, n, *_ in seg_by_machine.get(m, []) if s < we and t > ws), None)
        for a, b in dr._gaps(ws, we, busy_m.get(m, [])):
            if (b - a).total_seconds() < 1800: continue
            # is any op waiting on m during [a,b)? (planned on m, ready before b)
            waiting = False
            for e in pr.schedule:
                if e.machine != m: continue
                segs = [(s, t) for s, t, _n in (e.op_segments or []) if t > s]
                if not segs: continue
                first = min(s for s, _ in segs); last = max(t for _, t in segs)
                if first >= b or (first < a and last > b): waiting = True; break
            if not waiting: continue
            whole = [n for n in on_shift if not any(s < we and ws < t for s, t in busy_o.get(n, []))]
            part = [n for n in on_shift if n not in whole and dr._gaps(a, b, busy_o.get(n, []))]
            h = (b - a).total_seconds() / 3600
            if whole: key = "someone free the WHOLE window (owner)" if owner in whole else "someone free the WHOLE window (not the machine's operator)"
            elif part: key = "free only PART of the window (owner)" if owner in part else ("free only PART of the window (other person)" if part else "?")
            else: key = "nobody free at all"
            cat[key] += h
            if len(samples[key]) < 3:
                who = (whole or part)[:2]
                det = {n: [(s.strftime('%d %H:%M'), t.strftime('%H:%M'), next((mm for mm, sg in seg_by_machine.items() for x, y, nn, *_ in sg if nn == n and x == s), '?')) for s, t in busy_o.get(n, []) if s < we and ws < t] for n in who}
                samples[key].append((m, a.strftime('%d-%m %H:%M'), b.strftime('%H:%M'), round(h, 1), "owner=" + str(owner), det))
print("idle-window hours with an op waiting on the machine, by what the placement saw:")
for k, v in cat.most_common(): print(f"  {v:7.1f} h  {k}")
for k, v in samples.items():
    print(k)
    for smp in v: print("    ", smp)
```

### `residual.py`

```python
import sys, os, collections
SP = os.environ["SP"]; sys.argv = [sys.argv[0]]
exec(open(SP + "/holes.py").read().split('if __name__ == "__main__":')[0])
from datetime import timedelta
for name, path, kind in (("Test9 real", "/Users/ritinwadekar/Desktop/Latest Master/Test9 (5) (1) (1) (1).xlsx", "real"), ("Test8 wip=30", "Test8.xlsx", 30)):
    so_lines, masters = load(path)
    orders, acts = real_book(so_lines, masters, "/Users/ritinwadekar/Desktop/Anvitech_actuals_backup_2026-09-17.json") if kind == "real" else synthetic_book(so_lines, masters, kind)
    lines = orderbook.active_so_lines(orders, acts, masters); new_engine.decode = real_decode
    frozen = frozen_for(lines, acts, masters, cfg()); pr, lines = plan(orders, acts, masters, "gt", frozen)
    c = cfg(); nm = new_engine._apply_app_operators(new_engine._new_masters(False), masters); pcfg = new_engine._plan_config(c)
    pools = build_machine_pools(nm); busy_o = dr._operator_bookings(pr.schedule)
    busy_m = collections.defaultdict(list); ops_on = collections.defaultdict(list); by_batch = collections.defaultdict(list)
    for e in pr.schedule:
        if e.machine in OFF or e.end <= e.start: by_batch[e.batch_id].append(e); continue
        for ss, se, _n in (e.op_segments or []):
            if se > ss: busy_m[e.machine].append((ss, se))
        by_batch[e.batch_id].append(e)
    busy_m = {k: dr._merge(v) for k, v in busy_m.items()}
    reasons = collections.Counter()
    plan_end = max(e.end for e in pr.schedule)
    for bid, es in by_batch.items():
        es.sort(key=lambda e: (e.process_seq, e.start)); prev_end = None
        for e in es:
            if e.machine in OFF: prev_end = max(prev_end or e.end, e.end); continue
            segs = [(ss, se) for ss, se, _n in (e.op_segments or []) if se > ss]
            if not segs: continue
            ready = prev_end if prev_end else pcfg.plan_start; start = min(s for s, _ in segs); end_ = max(t for _, t in segs)
            prev_end = max(prev_end or end_, end_)
            if start <= ready: continue
            work = sum((t - s).total_seconds() / 60 for s, t in segs)
            m = e.machine; mac = nm.machines[m]
            for win in iter_windows(mac, ready, nm.calendar, pcfg):
                if win.start >= start: break
                for a, b in dr._gaps(max(win.start, ready), min(win.end, start), busy_m.get(m, [])):
                    if b - a < timedelta(minutes=30): continue
                    free = dr._merge([g for o in pools.get(m, ()) if effective_shift(o, win.shift_date, pcfg) == win.shift and nm.calendar.is_operator_available(o.name, win.shift_date) for g in dr._gaps(a, b, busy_o.get(o.name, []))])
                    h = sum((y - x).total_seconds() / 3600 for x, y in free)
                    if h <= 0: continue
                    if e.machine.startswith(("CNC", "VMC")):
                        gap_len = (b - a).total_seconds() / 60
                        reasons["CNC/VMC: idle stretch shorter than the whole job (no second setup)" if gap_len < work else "CNC/VMC: stretch long enough — other"] += h
                    else:
                        reasons["manual/inspection: other"] += h
    print(name, {k: round(v, 1) for k, v in reasons.most_common()}, "total", round(sum(reasons.values()), 1))
```

### `timing.py`

```python
import io, os, sys, tempfile, time
os.environ["STORE_DIR"] = tempfile.mkdtemp(); sys.path.insert(0, os.getcwd())
from datetime import date
from engine import loaders, new_engine, orderbook, book_store
from engine.config import Config
from engine.models import PlanRun, Order
from engine.pipeline import run_forward
for wb in ("Test5.xlsx", "/Users/ritinwadekar/Desktop/Latest Master/Test9 (5) (1) (1) (1).xlsx"):
    raw = open(wb, "rb").read(); book_store.save_masters_bytes(raw); new_engine.set_masters_bytes(raw)
    so_lines, masters = loaders.load_all(io.BytesIO(raw))
    cfg = Config(scheduler="new", plan_start_date=date(2026, 9, 22), apply_operator_logic=True, overlap_percent=88, consolidation_window_days=1)
    orders = {}
    for l in so_lines:
        o = Order(so_no=l.so_no, item_code=l.item_code, item_name=l.item_name, ordered_qty=float(l.qty), delivery_date=l.delivery_date); orders[o.key] = o
    lines = orderbook.active_so_lines(orders, [], masters)
    run_forward(PlanRun(so_lines=list(lines)), cfg, masters)   # warm caches
    ts = []
    for _ in range(3):
        t = time.perf_counter(); run_forward(PlanRun(so_lines=list(lines)), cfg, masters); ts.append(time.perf_counter() - t)
    print(f"{os.path.basename(wb)[:12]:12s} plan {min(ts)*1000:6.0f} ms (best of 3)")
```

