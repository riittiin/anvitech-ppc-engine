"""Delay justification — pure, post-hoc reconstruction of WHY each order is delayed,
from the FINISHED plan (no scheduler state, no I/O). For every (SO No, Item Code) order,
time from the plan start to the order's completion is partitioned into RUNNING and
WAITING intervals, and every wait is attributed to a concrete cause:

  * outsourced     — the order is away at a vendor (OS step); nothing in-house can move
  * machine busy   — the machine the next step needs is occupied by other orders
                     (each blocking order named; '(higher priority)' when it ranks ahead)
  * off-hours      — that machine is outside its working window (night / weekly off / holiday)
  * crew           — machine free within working hours and EVERY qualified operator was
                     already busy elsewhere
  * crew on leave  — the same, where at least one qualified operator was on recorded
                     leave (its own state, so the directors read "on leave", never
                     "machine free, operator free" for a shift nobody could have worked)
  * idle capacity  — machine free, a qualified operator free too, and still nothing was
                     scheduled. Spare capacity, not a shortage of anything.

RUNNING + OUTSOURCED + all WAIT == the order's whole span, so every hour is accounted
for. See docs/superpowers/specs/2026-07-28-delay-justification-report-design.md.

**The 2026-08-09 rewrite (owner audit).** Three defects, all measured on the live
export before being fixed:
  1. `crew` was a FALLBACK, not a finding — `_classify_free` took no operator data at
     all and printed "waiting for a free qualified operator" for any machine-free hour.
     Of 3,142.6 h so labelled, **1,331.1 h (55 days, 308 windows, all 57 orders) had a
     qualified operator sitting free**; the directors' summary read 130.9 days of crew
     loss. It now consults `operator_coverage.qualified_operators` — the SAME rule Rule 6
     staffs by — and only says "crew" when nobody could actually have run the machine.
  2. Outsourcing was INVISIBLE: off-lane entries were dropped, so a 96-hour OS block
     became a gap billed to the next in-house machine (0 of 1,648 rows ever named an OS
     step, though items carry 48-264 h of it). OS is now its own state.
  3. The clock started at MIDNIGHT of the plan-start date while the plan really begins
     at the plan-start floor, charging every order the hours before the plan existed —
     607 h across 57 orders. It now starts at the plan's first scheduled moment.

**Leave is not free capacity (2026-09-22, owner escalation).** The report never
received the absences table, so a person on recorded leave had no bookings and looked
FREE: on the live export MD1 and DTC2 read "idle, machine and a qualified operator
free" for whole shifts, day after day (463 h in full-shift rows), while the only other
helper was away for 3.5 weeks and the one remaining helper was busy on the other
manual stations. A real crew shortage was published as spare capacity — the exact
class of defect 1 above, in the other direction. `build_delay_report` now takes the
absences and `_staffing_split` treats a person on leave that day as unavailable, the
same rule the engine applies (`ShopCalendar.is_operator_available`).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from .operator_coverage import eligible_window, qualified_operators
from .optimizer import expected_completion
from .worktime import WorkClock

_OS_LANE = "OS / Outsourced"
_OFF_LANES = {_OS_LANE, "Off-machine"}


def _hours(a, b):
    return (b - a).total_seconds() / 3600.0


def _merge(intervals):
    """Merge overlapping/adjacent (start, end) intervals."""
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _gaps(start, end, busy):
    """Complement of merged `busy` intervals within [start, end]."""
    gaps, cur = [], start
    for s, e in _merge(busy):
        if s > cur:
            gaps.append((cur, min(s, end)))
        cur = max(cur, e)
    if cur < end:
        gaps.append((cur, end))
    return [(a, b) for a, b in gaps if b > a]


def _order_ops(schedule, so, item):
    ops = [e for e in schedule if e.item_code == item and so in (e.so_refs or [])
           and e.machine not in _OFF_LANES and e.end > e.start]
    return sorted(ops, key=lambda e: e.start)


def _rank_by_key(batches_prioritized):
    """Priority position of each (SO, item): lower index = higher priority."""
    rank = {}
    for i, b in enumerate(batches_prioritized or []):
        for so in (b.source_so_refs or []):
            rank[(so, b.item_code)] = i
    return rank


def _next_op(ops, gap_end):
    """The order's NEXT operation (starting at/after the gap), or its last one."""
    nxt = min((e for e in ops if e.start >= gap_end), key=lambda e: e.start, default=None)
    return nxt if nxt else (ops[-1] if ops else None)


def _next_machine(ops, gap_end):
    """The machine the order's NEXT operation (starting at/after the gap) needs."""
    nxt = _next_op(ops, gap_end)
    return nxt.machine if nxt else ""


def _is_setup_machine(machine_id):
    """CNC/VMC: a job is one engagement with a 90-minute setup and is never split
    around another job (mirrors ``ppc_engine`` MachineKind.MACHINING by id)."""
    return str(machine_id or "").upper().startswith(("CNC", "VMC"))


def _idle_why(machine, ps, pe, nxt):
    """Why a free machine with a free qualified operator still ran nothing. Since
    2026-09-22 the engine lays ready work in any stretch a qualified person is
    free for, so what is left is a stretch the waiting job cannot use: on a CNC/VMC
    a job runs as ONE engagement (a second setup is never paid), so a stretch
    shorter than the job stays idle; elsewhere, a stretch under the 30-minute
    minimum run. Named here so the directors read the reason, not a contradiction."""
    stretch_h = _hours(ps, pe)
    if nxt is not None and _is_setup_machine(machine):
        need_h = float(getattr(nxt, "occupancy_min", 0) or 0) / 60.0
        if need_h > stretch_h:
            return (f"Machine free and a qualified operator free — the next step "
                    f"({nxt.process_name}) needs {need_h:.1f} h as one run (a CNC/VMC job "
                    f"is never split around another job) and this stretch is only "
                    f"{stretch_h:.1f} h")
    if stretch_h * 60.0 < 30.0:
        return ("Machine free and a qualified operator free — under the 30-minute "
                "minimum run, so nothing was started here")
    return ("Machine free and a qualified operator free — spare capacity, nothing was "
            "scheduled here")


def _machine_busy(a, b, machine, schedule, this_rank, rank):
    """Rows for every OTHER op occupying `machine` during [a, b] (each blocker named),
    plus the machine-free remainder of [a, b]."""
    others = [e for e in schedule if e.machine == machine and not (e.end <= a or e.start >= b)]
    others.sort(key=lambda e: e.start)
    rows, occupied = [], []
    for e in others:
        os, oe = max(e.start, a), min(e.end, b)
        if oe <= os:
            continue
        occupied.append((os, oe))
        bso = (e.so_refs or [""])[0]
        hp = rank.get((bso, e.item_code), 10 ** 9) < this_rank
        why = (f"{machine} busy with {bso} / {e.item_code} — {e.process_name}"
               + (" (higher priority)" if hp else ""))
        rows.append({"State": "WAITING (machine busy)", "Process": "", "Machine": machine,
                     "Operator": "", "From": os, "To": oe, "Hours": round(_hours(os, oe), 2),
                     "Why": why})
    free = _gaps(a, b, occupied)
    return rows, free


def _offlane_ops(schedule, so, item):
    """The order's OUTSOURCED / off-machine steps that actually consume time. These are
    the order's real constraint while they run, and they used to be dropped entirely."""
    ops = [e for e in schedule if e.item_code == item and so in (e.so_refs or [])
           and e.machine in _OFF_LANES and e.end > e.start]
    return sorted(ops, key=lambda e: e.start)


def _operator_bookings(schedule):
    """Every operator's booked intervals across the WHOLE plan, from the per-shift
    segments the scheduler actually committed. This is what makes "no operator was
    free" a checkable claim instead of an assumption."""
    busy = defaultdict(list)
    for e in schedule:
        segs = getattr(e, "op_segments", None) or []
        if segs:
            for s, t, name in segs:
                if name:
                    busy[name].append((s, t))
        elif e.operator:
            busy[e.operator].append((e.start, e.end))
    return {k: _merge(v) for k, v in busy.items()}


def _next_shift_boundary(t, config):
    """The next 08:00 / 19:00 / 05:00 after `t` — qualification depends on which shift
    a moment falls in, so a window straddling a change must be split."""
    hours = sorted({config.first_shift_start_hour, config.first_shift_end_hour,
                    config.second_shift_end_hour})
    for day_off in (0, 1):
        d = (t + timedelta(days=day_off)).date()
        for h in hours:
            cand = datetime.combine(d, datetime.min.time()) + timedelta(hours=h)
            if cand > t:
                return cand
    return t + timedelta(days=1)


def _shift_anchor_day(t, config):
    """The calendar day a moment's shift is ANCHORED to: the night shift that started
    19:00 on D and runs past midnight still belongs to D, and that is the day the
    engine checks a person's leave against (`worktime.iter_windows` /
    `ShopCalendar.is_operator_available`)."""
    if t.hour < config.first_shift_start_hour:
        return t.date() - timedelta(days=1)
    return t.date()


def _staffing_split(a, b, machine, masters, config, op_busy, leave=None):
    """Split [a, b] into (start, end, someone_was_free, on_leave) using the SAME
    qualification rule Rule 6 staffs by. `someone_was_free` means at least one operator
    qualified for this machine on this shift had no other booking then AND was not on
    recorded leave that day; `on_leave` names the qualified people who were away."""
    leave = leave or {}
    out, cur = [], a
    while cur < b:
        nxt = min(b, _next_shift_boundary(cur, config))
        day = _shift_anchor_day(cur, config)
        names = qualified_operators(machine, cur, masters, config)
        away = tuple(n for n in names if day in leave.get(n, ()))
        free = []
        for n in names:
            if n in away:
                continue
            free.extend(_gaps(cur, nxt, op_busy.get(n, [])))
        free = _merge(free)
        out.extend((s, e, True, away) for s, e in free)
        out.extend((s, e, False, away) for s, e in _gaps(cur, nxt, free))
        cur = nxt
    return sorted(out)


def _leave_days(absences):
    """Absence rows -> {operator: set(date)}: the days each person is away, expanded
    exactly as `optimize_service.absence_reservations` expands them for the engine
    (from_date through to_date inclusive). Malformed rows are skipped, never raised."""
    out: dict = {}
    for a in absences or []:
        try:
            f = date.fromisoformat(a["from_date"]); t = date.fromisoformat(a["to_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if t < f:
            f, t = t, f
        name = a.get("operator") or ""
        if not name:
            continue
        days = out.setdefault(name, set())
        cur = f
        while cur <= t:
            days.add(cur)
            cur += timedelta(days=1)
    return out


def _classify_free(a, b, clock, machine=None, masters=None, config=None, op_busy=None,
                   down_days=None, leave=None, nxt=None):
    """Split a machine-free interval into off-hours (outside the machine's working
    window) and, inside it, maintenance, a genuine crew shortage, or plain idle
    capacity.

    Windows are bucketed by the day they are ANCHORED on, which is how the engine
    blocks a maintenance day: a two-shift machine's night window opens 19:00 on D and
    closes 05:00 on D+1, and the whole of it belongs to D. Bucketing by anchor day
    (rather than splitting at midnight) is what makes the report agree with the plan
    to the minute."""
    down_days = down_days or set()
    # Start a day early: a two-shift machine's night window (e.g. 19:00→05:00) belongs to
    # the PREVIOUS day but overflows into this one, so it can cover the early morning of `a`.
    up, down, d = [], [], a.date() - timedelta(days=1)
    while datetime.combine(d, datetime.min.time()) < b:
        for ws, we in clock._windows_for_day(d):
            s, e = max(ws, a), min(we, b)
            if e > s:
                (down if d in down_days else up).append((s, e))
        d = d + timedelta(days=1)
    up, down = _merge(up), _merge(down)
    rows = []
    for s, e in down:
        rows.append({
            "State": "MAINTENANCE (machine down)", "Process": "",
            "Machine": machine or "", "Operator": "", "From": s, "To": e,
            "Hours": round(_hours(s, e), 2),
            "Why": "Machine out of service for maintenance"})
    for s, e in up:
        if masters is None or config is None:
            pieces = [(s, e, False, ())]    # no staffing data — keep the old behaviour
        else:
            pieces = _staffing_split(s, e, machine, masters, config, op_busy or {},
                                     leave)
        for ps, pe, someone_free, away in pieces:
            if pe <= ps:
                continue
            if someone_free:
                rows.append({
                    "State": "IDLE (capacity free)", "Process": "", "Machine": machine or "",
                    "Operator": "", "From": ps, "To": pe,
                    "Hours": round(_hours(ps, pe), 2),
                    "Why": _idle_why(machine, ps, pe, nxt)})
            elif away:
                # Its own state, so the directors read "on leave" in the State column
                # and never "machine free, operator free" for a shift nobody could
                # have worked. Counted in the crew bucket: it is a crew shortage.
                rows.append({
                    "State": "WAITING (crew on leave)", "Process": "",
                    "Machine": machine or "", "Operator": ", ".join(away),
                    "From": ps, "To": pe, "Hours": round(_hours(ps, pe), 2),
                    "Why": (f"Machine free — {', '.join(away)} on leave; every other "
                            "qualified operator was busy elsewhere")})
            else:
                rows.append({
                    "State": "WAITING (crew)", "Process": "", "Machine": "", "Operator": "",
                    "From": ps, "To": pe, "Hours": round(_hours(ps, pe), 2),
                    "Why": "Machine free — every qualified operator was busy elsewhere"})
    for s, e in _gaps(a, b, up + down):
        rows.append({"State": "WAITING (off-hours)", "Process": "", "Machine": "", "Operator": "",
                     "From": s, "To": e, "Hours": round(_hours(s, e), 2),
                     "Why": "Outside working hours (night / weekly off / holiday)"})
    return rows


def _why_summary(days_late, buckets):
    if days_late <= 0:
        return "On time."
    parts = []
    if buckets["machine"] > 0:
        parts.append(f"{buckets['machine']}d machines busy (higher-priority orders)")
    if buckets["off"] > 0:
        parts.append(f"{buckets['off']}d off-hours")
    if buckets["crew"] > 0:
        parts.append(f"{buckets['crew']}d waiting for operators")
    if buckets.get("outsourced", 0) > 0:
        parts.append(f"{buckets['outsourced']}d outsourced (at a vendor)")
    if buckets.get("idle", 0) > 0:
        parts.append(f"{buckets['idle']}d machine and operator both free")
    if buckets.get("maintenance", 0) > 0:
        parts.append(f"{buckets['maintenance']}d machine out for maintenance")
    return f"{days_late} days late — " + ", ".join(parts) if parts else f"{days_late} days late"


def build_delay_report(schedule, so_lines, batches_prioritized, config, masters,
                       downtime=None, absences=None):
    """See module docstring. Returns {'summary': [row], 'detail': [row]}.

    ``downtime`` and ``absences`` are the SAME rows the engine planned with
    (`book_store.load_machine_downtime` / `load_absences`): a report that checks
    fewer constraints than the plan describes a different shop, and calls whatever
    the plan could not do "spare capacity"."""
    # The plan's FIRST SCHEDULED MOMENT, not midnight. The engine starts at the
    # plan-start floor (the next full hour after an optimization lands), so measuring
    # from midnight charged every order the hours before the plan existed — 607 h
    # across 57 orders on the live export, all of it landing in the crew bucket.
    plan_start = (min(e.start for e in schedule) if schedule
                  else datetime.combine(config.plan_start_date, datetime.min.time()))
    rank = _rank_by_key(batches_prioritized)
    op_busy = _operator_bookings(schedule)
    leave = _leave_days(absences)
    clock_cache = {}

    # Maintenance days per machine, as dates — so a machine-free window on a day the
    # machine was out of service is attributed to maintenance, never to the crew.
    down_by_machine: dict = {}
    for d in downtime or []:
        try:
            f = date.fromisoformat(d["from_date"]); t = date.fromisoformat(d["to_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if t < f:
            f, t = t, f
        days = down_by_machine.setdefault(d.get("machine", ""), set())
        cur = f
        while cur <= t:
            days.add(cur)
            cur += timedelta(days=1)
    down_by_machine.pop("", None)

    def clock_for(mid):
        if mid not in clock_cache:
            mac = masters.machines.get(mid)
            iv = eligible_window(mac, config) if mac is not None else []
            clock_cache[mid] = WorkClock(masters.calendar, iv)
        return clock_cache[mid]

    # THE shared completion definition (engine/optimizer.expected_completion) — over
    # ALL of an order's entries, OS/dispatch lanes included. The wait analysis below
    # still runs over real machine ops only (there is no machine to wait for on an
    # off-lane), but the DATE this report publishes must be the same date the Gantt
    # and the Orders tab publish, or the same order reads two ways (live 2026-08-07).
    completion_by_key = expected_completion(schedule)

    detail, summary = [], []
    for line in so_lines:
        so, item = line.so_no, line.item_code
        ops = _order_ops(schedule, so, item)
        offlane = _offlane_ops(schedule, so, item)
        if not ops and not offlane and (so, item) not in completion_by_key:
            continue          # genuinely not in this plan at all — nothing to explain
        # Time at a vendor is NOT a wait on anything in-house: count it as occupied so
        # it can never be re-billed to the next machine's operators.
        running = _merge([(e.start, e.end) for e in ops])
        occupied = _merge([(e.start, e.end) for e in ops + offlane])
        # A fully-OUTSOURCED order has no in-house op but is still scheduled and still
        # has a real completion date. It used to be dropped here, so it vanished from
        # the delay report while appearing on the Orders tab and the Gantt (the same
        # silent-omission class as the missing operator/machine rows, 2026-08-07).
        completion = (max(e.end for e in ops + offlane) if (ops or offlane)
                      else datetime.combine(completion_by_key[(so, item)],
                                            datetime.min.time()))
        this_rank = rank.get((so, item), 10 ** 9)
        rows = []
        for e in ops:
            rows.append({"State": "RUNNING", "Process": f"{e.process_seq}. {e.process_name}",
                         "Machine": e.machine, "Operator": e.operator_label(),
                         "From": e.start, "To": e.end, "Hours": round(_hours(e.start, e.end), 2),
                         "Why": ""})
        for e in offlane:
            out = e.machine == _OS_LANE
            rows.append({
                "State": "OUTSOURCED" if out else "OFF-MACHINE",
                "Process": f"{e.process_seq}. {e.process_name}", "Machine": e.machine,
                "Operator": "", "From": e.start, "To": e.end,
                "Hours": round(_hours(e.start, e.end), 2),
                "Why": (f"At the outsourcing vendor — {e.process_name}" if out
                        else f"Off-machine step — {e.process_name}")})
        for (a, b) in _gaps(plan_start, completion, occupied):
            nxt = _next_op(ops, b)
            machine = nxt.machine if nxt else ""
            busy, free = _machine_busy(a, b, machine, schedule, this_rank, rank)
            rows.extend(busy)
            for (fa, fb) in free:
                rows.extend(_classify_free(fa, fb, clock_for(machine), machine,
                                           masters, config, op_busy,
                                           down_by_machine.get(machine), leave, nxt))
        rows.sort(key=lambda r: r["From"])
        for r in rows:
            r["SO No"], r["Item Code"] = so, item

        # 'Working' = MERGED running wall-clock (an order's ops can run concurrently —
        # parallel split / overlap — so summing each RUNNING row would double-count).
        # Waits are the complement of the merged running, so work + waits == span exactly.
        buckets = {"machine": 0.0, "off": 0.0, "crew": 0.0, "outsourced": 0.0,
                   "idle": 0.0, "maintenance": 0.0,
                   "work": sum(_hours(s, e) for s, e in running)}
        for r in rows:
            if r["State"] == "WAITING (machine busy)":
                buckets["machine"] += r["Hours"]
            elif r["State"] == "WAITING (off-hours)":
                buckets["off"] += r["Hours"]
            elif r["State"] in ("WAITING (crew)", "WAITING (crew on leave)"):
                buckets["crew"] += r["Hours"]
            elif r["State"] in ("OUTSOURCED", "OFF-MACHINE"):
                buckets["outsourced"] += r["Hours"]
            elif r["State"] == "IDLE (capacity free)":
                buckets["idle"] += r["Hours"]
            elif r["State"] == "MAINTENANCE (machine down)":
                buckets["maintenance"] += r["Hours"]
        days = {k: round(v / 24.0, 1) for k, v in buckets.items()}
        completion_date = completion_by_key.get((so, item), completion.date())
        days_late = (completion_date - line.delivery_date).days
        summary.append({
            "SO No": so, "Item Code": item, "Item Name": line.item_name,
            "Ordered Qty": int(line.qty), "SO Delivery Date": line.delivery_date,
            "Expected Completion": completion_date, "Days Late": days_late,
            "Working (days)": days["work"], "Waiting: machine (days)": days["machine"],
            "Waiting: off-hours (days)": days["off"], "Waiting: crew (days)": days["crew"],
            "Outsourced (days)": days["outsourced"],
            "Idle: capacity free (days)": days["idle"],
            "Maintenance (days)": days["maintenance"],
            "Why": _why_summary(days_late, days)})
        detail.extend(rows)

    summary.sort(key=lambda s: -s["Days Late"])
    pos = {(s["SO No"], s["Item Code"]): i for i, s in enumerate(summary)}
    detail.sort(key=lambda r: (pos[(r["SO No"], r["Item Code"])], r["From"]))
    # normalize detail column order
    cols = ["SO No", "Item Code", "State", "Process", "Machine", "Operator",
            "From", "To", "Hours", "Why"]
    detail = [{c: r[c] for c in cols} for r in detail]
    return {"summary": summary, "detail": detail}
