"""Reproduces the one failed check (variant B): after Optimize + Apply, the first Done with no punch re-times in-progress jobs because the frozen set is rebuilt from the NEW published plan. Run from the repo root: DEFAULT_SCHEDULER=new python3.12 -B <this> <STORE_DIR_TO_COPY> 10 15 --os-on-send"""
import sys, json
H="docs/superpowers/specs/2026-10-06-fixed-plan-harness/verify_fixed_plan.py"
src=open(H).read().split("# =================================================================== PHASE 1")[0]
nd, bud = int(sys.argv[2]), int(sys.argv[3])
sys.argv=[H, sys.argv[1], "/dev/null"] + sys.argv[4:]
exec(compile(src, H, "exec"))
j, pr, sl, cf, ms = run()
D = date.fromisoformat(j["resolved_plan_start"])
for k in range(nd):
    CLOCK["now"] = datetime.combine(D, datetime.min.time()) + timedelta(hours=23)
    for e,q,ends,who in day_plan(pr.schedule, D):
        punch(e,q,D,who,ms,ADMIN)
    ADMIN.post("/optimize/done"); j, pr, sl, cf, ms = run(); D = next_working(D, ms.calendar)
fz0 = book_store.load_frozen_ops()
m._start_optimize(bud, "deep", background=False)
st = ADMIN.get("/optimize/status").json()
print("opt", st["state"], (st.get("best") or {}).get("total_late_days"))
print("apply", ADMIN.post("/optimize/apply").status_code)
j1, p1, *_ = run()
fz1 = book_store.load_frozen_ops()
ADMIN.post("/optimize/done")
j2, p2, *_ = run()
fz2 = book_store.load_frozen_ops()
print("hash", plan_hash(p1.schedule), plan_hash(p2.schedule))
def norm(f): return sorted(json.dumps(x, sort_keys=True, default=str) for x in f)
print("frozen same before/after Done:", norm(fz1)==norm(fz2), len(fz1), len(fz2), "frozen at optimize start == after apply:", norm(fz0)==norm(fz1))
for a,b in zip(sorted(norm(fz1)), sorted(norm(fz2))):
    if a!=b: print("  fz1", a[:300]); print("  fz2", b[:300])
mv = moved(dates_of(j1), dates_of(j2))
print("dates moved by Done:", len(mv), list(mv.items())[:6])
sm1, sm2 = step_machines(p1.schedule), step_machines(p2.schedule)
print("machine changes:", sum(1 for k in sm1 if k in sm2 and sm1[k]!=sm2[k]))
rows = book_store.load_last_applied_schedule()
print("turn audit p1", {k:v for k,v in machine_and_turn_audit(p1.schedule, rows, frozen_keys_now()).items() if not k.endswith("_ex")})
print("turn audit p2", {k:v for k,v in machine_and_turn_audit(p2.schedule, rows, frozen_keys_now()).items() if not k.endswith("_ex")})
