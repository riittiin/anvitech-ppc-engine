"""Fixed plan (2026-10-06 spec): pure helpers for what the admin is told.

Nothing here changes a plan. ``downtime_waits`` finds machine breaks entered AFTER
the plan was published that hold up planned work (the "Optimize recommended"
banner, D5); ``alerts_from_notes`` lifts the engine's fixed-plan problem lines out
of the rule-6 notes; ``date_changes`` is the delivery-date list an Optimize result
shows before Apply (D4)."""
from __future__ import annotations

from datetime import date


def _dmy(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d-%m-%Y")


def _runs(rows, mid):
    """(start, end, so_refs) of every well-formed row on machine ``mid``."""
    from datetime import datetime
    for r in rows or []:
        if r.get("machine") != mid:
            continue
        try:
            s, e = datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])
        except (KeyError, ValueError, TypeError):
            continue
        yield s, e, r.get("so_refs") or []


def downtime_waits(published_rows, repaired_rows, downtime_rows, known_ids, today,
                   first_window=None) -> list[dict]:
    """Machine breaks entered AFTER the plan was published, with the orders they hold
    up. Read from the CURRENT (repaired) plan, because the published plan ages between
    Optimize clicks (A0 review I1): a job that has since slid into the break must be
    named, and a job already finished must not. An order is waiting on the break when

    - its repaired run on that machine overlaps the break, [from_date 00:00, the day
      after to_date 00:00), or
    - the break PUSHED it: its repaired run on that machine starts in the machine's
      first working window after the break (``first_window(machine, after)`` returns
      that window's (start, end), or None), while its published run there started
      before the break ended.

    A job published and still planned to run after the break is not waiting on it
    (final review I7). Breaks whose id was on file at publish time (``known_ids``)
    were already planned around; finished and malformed breaks, and malformed rows,
    are skipped."""
    from datetime import datetime, timedelta
    out = []
    for d in downtime_rows or []:
        if d.get("id") in known_ids:
            continue
        try:
            f = date.fromisoformat(d["from_date"])
            t = date.fromisoformat(d["to_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if t < f:
            f, t = t, f
        if t < today:
            continue
        mid = d.get("machine", "")
        lo = datetime.combine(f, datetime.min.time())
        hi = datetime.combine(t + timedelta(days=1), datetime.min.time())
        win = first_window(mid, hi) if first_window else None
        due_before = set()
        for s, _e, refs in _runs(published_rows, mid):
            if s < hi:
                due_before.update(refs)
        orders = set()
        for s, e, refs in _runs(repaired_rows, mid):
            if s < hi and e > lo:
                orders.update(refs)
            elif win and win[0] <= s < win[1]:
                orders.update(x for x in refs if x in due_before)
        if orders:
            out.append({"machine": mid, "from_date": f.isoformat(),
                        "to_date": t.isoformat(), "orders": sorted(orders)})
    return out


def downtime_alert_text(w: dict) -> str:
    n = len(w["orders"])
    return (f"{w['machine']} is marked down {_dmy(w['from_date'])} to {_dmy(w['to_date'])} "
            f"and {n} order{'s are' if n != 1 else ' is'} waiting on it "
            f"({', '.join(w['orders'])}). Press Optimize to decide whether to wait or "
            f"move {'them' if n != 1 else 'it'}.")


def alerts_from_notes(notes) -> list[str]:
    from engine.new_engine import FIXED_PLAN_PREFIX
    seen, out = set(), []
    for n in notes or []:
        if isinstance(n, str) and n.startswith(FIXED_PLAN_PREFIX):
            text = n[len(FIXED_PLAN_PREFIX):]
            if text not in seen:
                seen.add(text)
                out.append(text)
    return out


def date_changes(now: dict, after: dict) -> list[dict]:
    rows = []
    for k in now.keys() & after.keys():
        a, b = date.fromisoformat(now[k]), date.fromisoformat(after[k])
        if a != b:
            so, item = k.split("\x1f", 1)
            rows.append({"so": so, "item": item, "now": now[k], "after": after[k],
                         "days": (b - a).days})
    rows.sort(key=lambda r: (-abs(r["days"]), r["so"], r["item"]))
    return rows
