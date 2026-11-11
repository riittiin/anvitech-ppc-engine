"""Mutation sweep for Profit and Loss: revert each load-bearing part ONE AT A TIME,
run the feature's tests, record whether at least one test fails, restore the file
byte-for-byte. Python runs with -B and the sweep sleeps 1.1 s after every write, so
stale bytecode can never mask a mutation (it did once, 2026-09-22).

Usage (repo root): .venv/bin/python -B mutation_sweep.py
"""
import subprocess, sys, time

TESTS = ["tests/test_profit_loss.py", "tests/test_profit_loss_api.py",
         "tests/test_shop_masters.py", "tests/test_role_parity.py"]
PL, API = "engine/profit_loss.py", "api/main.py"
MUTATIONS = [
    ("zero-punch gate in merge_snapshot (freeze at first punch)", PL,
     "        if (so, item) in started:\n            continue\n", ""),
    ("unpad: run = pieces x original cycle -> occupancy-based (padded)", PL,
     "                run = pieces[so] * co\n",
     "                run = (occ - setup_op) * share\n"),
    ("downtime deduction", PL,
     "        m = max(0.0, hours[i] - down)\n", "        m = max(0.0, hours[i])\n"),
    ("overbooked-shift scaling", PL,
     "        if cap is not None and sum(hs.values()) > cap:\n", "        if False:\n"),
    ("counted-kind filter, planned side", PL,
     "        if kinds.get(e.machine) not in COUNTED_KINDS:\n            continue\n        key =",
     "        key ="),
    ("counted-kind filter, actual side", PL,
     "        if kinds.get(a.machine) not in COUNTED_KINDS:\n            continue\n", ""),
    ("require_admin on GET /profit-loss", API,
     "    require_admin(request)\n    from engine import profit_loss as _pl\n",
     "    from engine import profit_loss as _pl\n"),
    ("model_fields_set keep-rate rule (an edit without rate keeps the rate)", API,
     '    if "rate" in req.model_fields_set:\n        item["rate"] = req.rate\n    else:\n'
     '        item["rate"] = doc["machines"].get(mid, {}).get("rate")\n',
     '    item["rate"] = req.rate\n'),
    ("user-role rate strip on /machines", API,
     "    if role == auth.ADMIN:\n        view[\"rate\"] = m.get(\"rate\")\n",
     "    view[\"rate\"] = m.get(\"rate\")\n"),
    ("_snapshot_pnl wired into _plan's compute path", API,
     "    _snapshot_pnl(plan_run, masters, ranked_config, actuals)\n", ""),
]

rows = []
for name, path, old, new in MUTATIONS:
    src = open(path, encoding="utf-8").read()
    assert src.count(old) == 1, f"{name}: anchor not unique in {path}"
    try:
        open(path, "w", encoding="utf-8").write(src.replace(old, new))
        time.sleep(1.1)
        p = subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "-x", "--no-header", *TESTS], capture_output=True, text=True)
        tail = [l for l in p.stdout.splitlines() if l.strip()][-1:]
        failed = [l.split(" ")[1] for l in p.stdout.splitlines() if l.startswith("FAILED")]
        rows.append((name, p.returncode != 0, failed[:3], tail))
    finally:
        open(path, "w", encoding="utf-8").write(src)
        time.sleep(1.1)
    print(("LOAD-BEARING " if rows[-1][1] else "NOT CAUGHT   ") + name, rows[-1][2], rows[-1][3])

clean = subprocess.run(["git", "diff", "--quiet", "--", PL, API]).returncode == 0
print(f"\n{sum(r[1] for r in rows)} of {len(rows)} load-bearing; sources restored clean: {clean}")
