"""Fixed-plan mutation sweep (Task 8, 2026-10-06; Task 9 amendment added): every
load-bearing mutation the Task 1-6b and Task 9 reports listed, plus the four spec 5.9
names (strict turn now its amended form), each applied ALONE to the
final code, the fixed-plan test set run, the file restored. A mutation is
load-bearing when at least one test fails.

Run from the repo root:  python3.12 -B docs/superpowers/specs/2026-10-06-fixed-plan-harness/mutation_sweep.py
Restores every file (also on Ctrl-C) and waits 1.1 s between edits so stale
bytecode cannot mask a mutation (-B plus -p no:cacheprovider as well).
"""
import signal
import subprocess
import sys
import time

# A kill must never leave a file mutated: turn SIGTERM into an exception so the
# `finally` below restores the source.
signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))

FS = "ppc_engine/scheduler/flow_scheduler.py"
NE = "engine/new_engine.py"
API = "api/main.py"
FZ = "engine/freeze.py"

M = [
    # --- spec 5.9 -----------------------------------------------------------
    ("5.9 machine pin: options not filtered to the published machine", FS,
     "        options = [(i, mid) for i, mid in options if mid == pin.machine_id]\n",
     "        pass\n"),
    # Strict turn was replaced by "next ready job in published order" (owner, spec
    # section 8, Task 9): its mutation is putting the head gating back.
    ("5.9 (amended) head-of-queue gating back", FS,
     "        cands = remaining\n",
     "        def _dn(ck):\n            return idx_of[ck[0]] > [o.seq for o in ops_of[ck[0]]].index(ck[1])\n"
     "        cands = [k for k in remaining if not _queued(k) or next(c for c in queue[_pin(k).machine_id] "
     "if not _dn(c)) == (k, ops_of[k][idx_of[k]].seq)] or remaining[:1]\n"),
    ("5.9 no release on a down machine: repair releases like Optimize", NE,
     '    _hold = {"release_on_downtime": False} if published else {}',
     "    _hold = {}"),
    ("5.9 published plan written on every plan, not only on apply", API,
     "    _PLAN_CACHE.update(key=_fp, result=result,\n",
     "    if published and not _seeding:\n"
     "        _publish(freeze.schedule_projection(plan_run.schedule))\n"
     "    _PLAN_CACHE.update(key=_fp, result=result,\n"),
    # --- Task 1 ---------------------------------------------------------------
    ("T1/T9 frozen-first floor ignored in placement", FS,
     "            earliest = max(earliest, turn_floor)\n", "            pass\n"),
    ("T1 finished step keeps its turn (<=0 skip removed)", FS,
     "        if (pr.get(ck[1], order.qty) if pr is not None else order.qty) <= 0:\n"
     "            continue\n", ""),
    ("T1 frozen step queued too (skip removed)", FS,
     "        if ck in skip:\n            continue\n", ""),
    ("T1/T9 frozen-first floor not seeded from frozen ops", FS,
     "            frozen_end[seg.machine_id] = max(frozen_end.get(seg.machine_id, seg.end), seg.end)\n",
     "            pass\n"),
    ("T1 pin to an unmanned machine kept", FS,
     "            continue   # nobody can man it: planned normally, never a crash\n",
     "            pass\n"),
    # --- Task 2 ---------------------------------------------------------------
    ("T2 row maps to the first batch only", NE, "        for k in keys:\n", "        for k in keys[:1]:\n"),
    ("T2 'no longer allowed' not named", NE,
     "            problems.append(\n                f\"{FIXED_PLAN_PREFIX}{refs} {r.get('item_code')} \"\n"
     "                f\"'{op.name}' was planned on {mid}, which is no longer allowed",
     "            (lambda *a: None)(\n                f\"{FIXED_PLAN_PREFIX}{refs} {r.get('item_code')} \"\n"
     "                f\"'{op.name}' was planned on {mid}, which is no longer allowed"),
    ("T2 unmanned machine not named", NE,
     "            problems.append(\n                f\"{FIXED_PLAN_PREFIX}{refs} {r.get('item_code')} \"\n"
     "                f\"'{op.name}' was planned on {mid}, but nobody",
     "            (lambda *a: None)(\n                f\"{FIXED_PLAN_PREFIX}{refs} {r.get('item_code')} \"\n"
     "                f\"'{op.name}' was planned on {mid}, but nobody"),
    ("T2 pins never reach decode", NE,
     "    sched = decode(orders, sequence, new_masters, sched_cfg, frozen=ppc_frozen,\n                   pins=ppc_pins)",
     "    sched = decode(orders, sequence, new_masters, sched_cfg, frozen=ppc_frozen,\n                   pins=None)"),
    # --- Task 4 ---------------------------------------------------------------
    ("T4 seed keyed on rows, not meta", API,
     "    if book_store.load_published_meta():\n        return\n    config = _load_plan_config()",
     "    if book_store.load_last_applied_schedule():\n        return\n    config = _load_plan_config()"),
    ("T4 apply snapshots at the OLD settings", API,
     'cand = _repaired_candidate(res["ranks"], target, extra_orders=extra_orders,',
     'cand = _repaired_candidate(res["ranks"], cfg, extra_orders=extra_orders,'),
    # --- Task 5 ---------------------------------------------------------------
    ("T5 published never used in _plan", API,
     "published = [] if _seeding else (_published_rows(config) or [])", "published = []"),
    ("T5 Done starts a search again", API,
     "    by = getattr(request.state, \"user\", \"\") or \"\"\n    try:\n        _compute_and_store_frozen()",
     "    by = getattr(request.state, \"user\", \"\") or \"\"\n    _try_start_auto(by=by)\n    try:\n        _compute_and_store_frozen()"),
    ("T5 published plan not in the cache key", API,
     '''        "published": hashlib.sha256(json.dumps(
            book_store.load_last_applied_schedule(), sort_keys=True,
            default=str).encode("utf-8")).hexdigest(),''', ""),
    ("T5 breaks known at publish not remembered", API,
     '''            set(book_store.load_published_meta().get("downtime_ids") or []),''', "            set(),"),
    ("T5 empty book never marks the seed done", API,
     '''    _publish(freeze.schedule_projection(plan_run.schedule)
             if plan_run is not None and plan_run.schedule else [])''',
     '''    if plan_run is not None and plan_run.schedule:
        _publish(freeze.schedule_projection(plan_run.schedule))'''),
    ("T5 new order not appended to the published plan", API,
     "    if add:\n        # Read-modify-write under the publish lock",
     "    if False:\n        # Read-modify-write under the publish lock"),
    ("T5 incumbent measured free", API,
     '''    schedule, all_lines = _all_lines_schedule(setup, setup.masters, ranks,
                                              published=_published_rows(setup.config))''',
     '''    schedule, all_lines = _all_lines_schedule(setup, setup.masters, ranks)'''),
    ("T5 stage 2 unpinned", API,
     '''                                  priority_rank=group_rank,
                                  published=published or None)''',
     '''                                  priority_rank=group_rank)'''),
    ("T5 new-order append through _publish", API,
     "        book_store.save_last_applied_schedule(existing + add)",
     "        _publish(existing + add)"),
    ("T5 scheduler guard removed", API,
     '''    if getattr(config, "scheduler", "classic") != "new":
        return None
    return book_store.load_last_applied_schedule() or None''',
     '''    return book_store.load_last_applied_schedule() or None'''),
    ("T5 pin failure swallowed", API,
     "    except Exception as e:  # noqa: BLE001 — saved order; report, do not raise\n        sos = ",
     "    except Exception as e:  # noqa: BLE001 — saved order; report, do not raise\n        pass\n    if False:\n        sos = "),
    # --- Task 6 ---------------------------------------------------------------
    ("T6 date list from the FREE candidate", API,
     "        ranks, _candidate_config(overlap, flexible)).repaired)",
     "        ranks, _candidate_config(overlap, flexible)).free)"),
    # --- Task 6b --------------------------------------------------------------
    ("6b M1 Giffler-Thompson pick back in the repair", FS,
     "        if queued:\n            key = _pick(cands, placements)\n",
     "        if False:\n            key = _pick(cands, placements)\n"),
    ("6b M2 no per-window person", FS, "staff=pin.staff if pin is not None else ())", "staff=())"),
    ("6b M3 projection placed=0", FZ, '"placed": int(getattr(e, "placed", 0) or 0),', '"placed": 0,'),
    ("6b M4 projection staff empty", FZ, '"staff": [[s.isoformat', '"staff": [] and [[s.isoformat'),
    ("6b M5 pins ignore rank", NE, "rank=None if (k, s) in unranked else rank.get((k, s)),", "rank=None,"),
    ("6b M6 no offset for appended new orders", API,
     "                r[\"placed\"] = base + r[\"placed\"]", "                r[\"placed\"] = r[\"placed\"]"),
    ("6b M7 no offset at the stage merge", API, "                e.placed += base", "                e.placed += 0"),
    ("6b M8 entries placed=0", NE, "                placed=placed,\n", "                placed=0,\n"),
    # --- Task 9 (owner amendment, spec section 8, + carried fixes) ------------
    ("T9 A2 any ready job first, even ahead of an on-time job", FS,
     "        return r if r > _pin(k).prev_start + _LATE_TOLERANCE else None",
     "        return r"),
    ("T9 A3 a late job never lets a ready one go first", FS,
     "            r = _late(key)\n", "            r = None\n"),
    ("T9 A5 no tolerance on a published time stored to the second", FS,
     "        return r if r > _pin(k).prev_start + _LATE_TOLERANCE else None",
     "        return r if r > _pin(k).prev_start else None"),
    ("T9 A6 late-job pick judged on the unguarded start", FS,
     "                    placements[k] = _guarded(k, placements[k])\n",
     "                    pass\n"),
    ("T9 I1 give-way on every machine, not only the late job's own", FS,
     "                    if k != key and _queued(k) and _pin(k).machine_id == mid]\n",
     "                    if k != key]\n"),
    ("T9 I2 behind a late job, published order whether ready or not", FS,
     "            j = min((k for k in same if max(ready_of[k], config.plan_start) <= t0),\n                    key=rk)",
     "            j = min(ahead, key=rk)"),
    ("T9 I2b behind a late job, earliest start even among ready jobs", FS,
     "            j = min((k for k in same if max(ready_of[k], config.plan_start) <= t0),\n                    key=rk)",
     "            j = min(ahead, key=lambda k: (placements[k][\"start\"], rk(k)))"),
    ("T9 I2c ready jobs by priority sequence, not published order", FS,
     "            j = min((k for k in same if max(ready_of[k], config.plan_start) <= t0),\n                    key=rk)",
     "            j = min((k for k in same if max(ready_of[k], config.plan_start) <= t0),\n                    key=lambda k: (priority[k], k))"),
    ("T9 I2d ready pool only the jobs that start before the late one is ready", FS,
     "            j = min((k for k in same if max(ready_of[k], config.plan_start) <= t0),",
     "            j = min((k for k in ahead if max(ready_of[k], config.plan_start) <= t0),"),
    ("T9 R2d the late job loses to a job that cannot start sooner", FS,
     "            if placements[j][\"start\"] >= placements[key][\"start\"]:\n                return key\n",
     "            if False:\n                return key\n"),
    ("T9 R2e old Done status lingers", "web/app.js",
     "  if (activeView === \"entry\" && v !== \"entry\") lastDoneStatus = \"\";\n", ""),
    ("T9 R3a a later job takes the late machine before earlier work elsewhere", FS,
     "            if g is not None and g != j and rk(g) < rk(j):\n",
     "            if False and g is not None and g != j and rk(g) < rk(j):\n"),
    ("T9 R3b the published-order step also takes this machine's own jobs", FS,
     "            rest = [c for c in cands if c not in deferred and c not in same]",
     "            rest = [c for c in cands if c not in deferred]"),
    ("T9 R3e earlier work elsewhere only if it starts no later than j", FS,
     "            if g is not None and g != j and rk(g) < rk(j):\n",
     "            if g is not None and g != j and rk(g) < rk(j) and placements[g][\"start\"] <= js:\n"),
    ("T9 R3f earlier work elsewhere placed first even when nothing here waits for it", FS,
     "            if not any(idx_of[ck[0]] < pos_of[ck] and _order_key(pin_of[ck]) < mine\n",
     "            if False and not any(idx_of[ck[0]] < pos_of[ck] and _order_key(pin_of[ck]) < mine\n"),
    ("T9 R3g earlier work elsewhere placed even if it jumps its own machine", FS,
     "                if _jumps_its_machine(g, placements):\n",
     "                if False:\n"),
    ("T9 R3h blocked earlier work: give way anyway", FS,
     "                    # (the published order, as if it were on time).\n                    return key\n",
     "                    # (the published order, as if it were on time).\n                    key = j\n                    continue\n"),
    ("T9 R3i a late job ahead on that machine blocks the earlier work too", FS,
     "            if (idx_of[ck[0]] == pos_of[ck] and _late(ck[0]) is not None\n",
     "            if (False and idx_of[ck[0]] == pos_of[ck] and _late(ck[0]) is not None\n"),
    ("T9 R3c on the late machine, an earlier job must START by then, not just be ready", FS,
     "                  and max(ready_of[c], config.plan_start) <= js]",
     "                  and placements[c][\"start\"] <= js]"),
    ("T9 R3d on the late machine, no earlier ready job goes before the picked one", FS,
     "            if up:\n                deferred.add(key)\n                key = min(up, key=rk)\n                continue\n            deferred.add(key)\n",
     "            if False:\n                pass\n            deferred.add(key)\n"),
    ("T9 B1 engine ignores the sent date", FS,
     "        sent = (order.os_sent or {}).get(op.seq) if dur > 0 else None\n",
     "        sent = None\n"),
    ("T9 B2 sent = first punch, not the completing one", "engine/orderbook.py",
     "            if before < need <= run:\n                on = d\n",
     "            if on is None:\n                on = d\n"),
    ("T9 B3 batch sent when ANY line is sent", "engine/rules/rule1_consolidate.py",
     "    keys = set(dicts[0]).intersection(*dicts[1:]) if dicts else set()\n"
     "    return {k: max(d[k] for d in dicts) for k in keys} or None\n",
     "    keys = set().union(*dicts)\n"
     "    return {k: max(d[k] for d in dicts if k in d) for k in keys} or None\n"),
    ("T9 B4 batch takes the earliest line date", "engine/rules/rule1_consolidate.py",
     "    return {k: max(d[k] for d in dicts) for k in keys} or None\n",
     "    return {k: min(d[k] for d in dicts) for k in keys} or None\n"),
    ("T9 B5 OS return may fall before it is reached", FS,
     "            end = max(ready, sent + timedelta(minutes=dur))\n",
     "            end = sent + timedelta(minutes=dur)\n"),
    ("T9 B6 adapter drops the sent date", NE, "            os_sent=os_sent,\n",
     "            os_sent=None,\n"),
    ("T9 C1 a moved job resumes with no setup", FS,
     "        setup = (config.setup_min if fo.setup and op.kind == OperationKind.MACHINING\n",
     "        setup = (0.0 if fo.setup and op.kind == OperationKind.MACHINING\n"),
    ("T9 C2 a moved manual job pays a setup", FS,
     "        setup = (config.setup_min if fo.setup and op.kind == OperationKind.MACHINING\n",
     "        setup = (config.setup_min if fo.setup\n"),
    ("T9 C3 Apply does not mark moved rows", API,
     "    rows = freeze.mark_moved(freeze.schedule_projection(free), setup.frozen, good)\n",
     "    rows = freeze.schedule_projection(free)\n"),
    ("T9 C4 setup owed even after a new punch", FZ,
     " and mv.get(\"good\") == good:\n", ":\n"),
    ("T9 C5 an owed setup is not carried across a second publish", FZ,
     "            f.get(\"setup_from\") or f.get(\"machine\"))\n", "            f.get(\"machine\"))\n"),
    ("T9 C6 adapter drops the setup flag", NE,
     "        setup = bool(moved_from.get((key, op_seq), set()) - {mid})\n",
     "        setup = False\n"),
    ("T9 C7 next step released as if no setup", FS,
     "                         qty=fo.remaining_qty, setup_min=setup))\n",
     "                         qty=fo.remaining_qty, setup_min=0.0))\n"),
    ("T9 D1 Go back catches every ValueError", API,
     "        except optimize_service.NothingToOptimize:   # nothing to plan: an empty plan\n",
     "        except ValueError:   # nothing to plan: an empty plan\n"),
    ("T9 D2 Go back forgets the ranks before publishing", API,
     "    new = getattr(config, \"scheduler\", \"classic\") == \"new\"\n",
     "    new = getattr(config, \"scheduler\", \"classic\") == \"new\"\n"
     "    book_store.clear_plan_priority()\n"),
    ("T9 D3 empty ranks keep the free number", API,
     "    if cand is not None:\n        try:\n            best = _candidate_metrics(cand)\n",
     "    if ranks and cand is not None:\n        try:\n            best = _candidate_metrics(cand)\n"),
]

TESTS = ["tests/test_fixed_plan_engine.py", "tests/test_fixed_plan_adapter.py",
         "tests/test_fixed_plan_logic.py", "tests/test_fixed_plan_api.py",
         "tests/test_fixed_plan_repair_fidelity.py", "tests/test_fixed_plan_ui.py",
         "tests/test_auto_optimize.py", "tests/test_plan_update_visibility.py",
         "tests/test_new_orders_api.py", "tests/test_machine_downtime_freeze.py",
         "tests/test_machine_downtime_engine.py", "tests/test_freeze_adapter.py",
         "tests/test_new_engine.py", "tests/test_no_idle_holes.py",
         "tests/test_fixed_plan_final_fixes.py", "tests/test_os_sent_date.py",
         "tests/test_resume_setup_after_move.py", "tests/test_fixed_plan_part_d.py"]

# --full: run the whole suite instead of the fixed-plan set (minus the one known,
# pre-existing environment failure), for mutations whose guard lives in older tests.
FULL = "--full" in sys.argv
if FULL:
    TESTS = ["tests", "--deselect",
             "tests/test_production_analysis.py::test_monthly_report_json_and_excel"]
only = set(a for a in sys.argv[1:] if a != "--full")
tally = []
for name, path, a, b in M:
    if only and not any(o in name for o in only):
        continue
    src = open(path).read()
    if src.count(a) != 1:
        print(f"SKIP {name}: anchor found {src.count(a)} times")
        tally.append((name, None))
        continue
    open(path, "w").write(src.replace(a, b))
    try:
        r = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                            "-p", "no:warnings", *TESTS], capture_output=True, text=True)
        fails = [l for l in r.stdout.splitlines() if l.startswith("FAILED") or l.startswith("ERROR")]
        last = [l for l in r.stdout.splitlines() if " passed" in l or " failed" in l or " error" in l]
        print(f"{'BITES' if r.returncode else 'NO TEST FAILS'}  {name}  ->  "
              f"{fails[0][:140] if fails else ''} {last[-1] if last else r.stdout[-200:]}", flush=True)
        tally.append((name, r.returncode != 0))
    finally:
        open(path, "w").write(src)
    time.sleep(1.1)
n = sum(1 for _, x in tally if x)
print(f"\n{n} of {sum(1 for _, x in tally if x is not None)} load-bearing; "
      f"skipped (anchor missing): {[t for t, x in tally if x is None]}")
