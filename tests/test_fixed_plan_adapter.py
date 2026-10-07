"""Fixed plan through the pipeline: published rows hold machines; problems are named."""
import io
from dataclasses import replace
from datetime import date

from engine import book_store, freeze, loaders, new_engine
from engine.config import Config
from engine.models import SOLine
from engine.pipeline import PlanRun, run_forward
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2, build_new_sample_bytes


def _setup():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    masters = loaders.load_all(io.BytesIO(wb))[1]
    cfg = replace(Config(), scheduler="new", plan_start_date=date(2025, 3, 3),
                  apply_operator_logic=True)
    lines = [SOLine(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
             SOLine(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21))]
    return masters, cfg, lines


def _plan(masters, cfg, lines, published=None, ranks=None):
    pr = PlanRun(so_lines=list(lines))
    trace = run_forward(pr, cfg, masters, published=published, priority_rank=ranks)
    return pr.schedule, trace


def _machine_of(sched, item, step):
    return {e.machine for e in sched if e.item_code == item and e.process_name == step}


def _other_machine_rows(m, cfg, lines):
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    chosen = _machine_of(free, ITEM_B, "CNC SECOND SIDE").pop()
    other = "CNC2" if chosen == "CNC1" else "CNC1"
    for r in rows:
        if r["item_code"] == ITEM_B and r["process_name"] == "CNC SECOND SIDE":
            r["machine"] = other
    return free, rows, other


def test_no_published_plan_is_byte_identical():
    m, cfg, lines = _setup()
    a, _ = _plan(m, cfg, lines)
    b, _ = _plan(m, cfg, lines, published=[])
    assert [e.as_row() for e in a] == [e.as_row() for e in b]


def test_a_published_machine_is_kept():
    """ITEM_B's CNC SECOND SIDE may run on CNC1 or CNC2. Publish it on whichever the
    free plan did NOT choose; the repair must keep the published one."""
    m, cfg, lines = _setup()
    _free, rows, other = _other_machine_rows(m, cfg, lines)
    fixed, _ = _plan(m, cfg, lines, published=rows)
    assert _machine_of(fixed, ITEM_B, "CNC SECOND SIDE") == {other}


def _queues(sched):
    """machine -> ordered list of (item, step, so_refs): which machine, and whose turn."""
    out = {}
    for e in sorted(sched, key=lambda e: (e.start, e.item_code, e.process_name)):
        if e.machine in new_engine.OFF_LANES:
            continue
        out.setdefault(e.machine, []).append((e.item_code, e.process_name, tuple(e.so_refs)))
    return out


def test_reversed_priority_does_not_move_a_published_job():
    """Eight competing orders. Reversed ranks MUST change machines or queue order when
    nothing is published (asserted, so this can never go vacuous), and must change
    nothing when the earlier plan is published."""
    m, cfg, _ = _setup()
    cfg = replace(cfg, consolidation_window_days=0)
    lines = [SOLine(f"NSO-{i:03d}", it, it, 100 + 10 * i, date(2025, 3, 15 + i))
             for i in range(1, 9) for it in [ITEM_B if i % 2 else ITEM_A]]
    fwd = {f"{l.so_no}\x1f{l.item_code}": i for i, l in enumerate(lines)}
    rev = {k: len(lines) - v for k, v in fwd.items()}
    base, _ = _plan(m, cfg, lines, ranks=fwd)
    assert _queues(_plan(m, cfg, lines, ranks=rev)[0]) != _queues(base), "fixture is vacuous"
    rows = freeze.schedule_projection(base)
    fixed, _ = _plan(m, cfg, lines, published=rows, ranks=rev)
    assert _queues(fixed) == _queues(base)


def test_a_published_batch_split_into_two_keeps_the_machine_in_both_halves():
    """Published as ONE clubbed batch (two SOs, same item) on the machine the free plan
    did not choose. Now Rule 1 no longer clubs them: BOTH batches keep that machine."""
    m, cfg, _ = _setup()
    lines = [SOLine(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21)),
             SOLine("NSO-003", ITEM_B, ITEM_B, 60, date(2025, 4, 10))]
    clubbed, _ = _plan(m, replace(cfg, consolidation_window_days=60), lines)
    assert len({e.batch_id for e in clubbed if e.item_code == ITEM_B}) == 1
    chosen = _machine_of(clubbed, ITEM_B, "CNC SECOND SIDE").pop()
    other = "CNC2" if chosen == "CNC1" else "CNC1"
    rows = freeze.schedule_projection(clubbed)
    for r in rows:
        if r["process_name"] == "CNC SECOND SIDE":
            r["machine"] = other
    fixed, _ = _plan(m, replace(cfg, consolidation_window_days=10), lines, published=rows)
    assert len({e.batch_id for e in fixed if e.item_code == ITEM_B}) == 2
    assert _machine_of(fixed, ITEM_B, "CNC SECOND SIDE") == {other}


def test_a_published_machine_no_longer_allowed_is_named_not_fatal():
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    for r in rows:
        if r["item_code"] == ITEM_A and r["process_name"] == "VMC FIRST SIDE":
            r["machine"] = "CNC2"          # not an option for that step
    fixed, trace = _plan(m, cfg, lines, published=rows)
    assert _machine_of(fixed, ITEM_A, "VMC FIRST SIDE") == {"VMC1"}
    notes = [n for n in trace["rule6"]["notes"] if n.startswith(new_engine.FIXED_PLAN_PREFIX)]
    assert any("VMC FIRST SIDE" in n and "CNC2" in n and "no longer allowed" in n
               for n in notes), notes


def test_a_published_machine_nobody_can_run_is_named_not_fatal():
    """The step still allows CNC2, but Settings no longer qualifies anyone for it."""
    m, cfg, lines = _setup()
    _free, rows, other = _other_machine_rows(m, cfg, lines)
    if other != "CNC2":                      # make the published machine the one we unman
        for r in rows:
            if r["item_code"] == ITEM_B and r["process_name"] == "CNC SECOND SIDE":
                r["machine"] = "CNC2"
    def strip(o):
        raw = ",".join(x.strip() for x in o.preferred_machines_raw.split(",")
                       if x.strip().upper() != "CNC2")
        return replace(o, preferred_machines_raw=raw,
                       machines=[x for x in o.machines if x != "CNC2"])
    m = replace(m, operators=[strip(o) for o in m.operators])
    fixed, trace = _plan(m, cfg, lines, published=rows)
    assert _machine_of(fixed, ITEM_B, "CNC SECOND SIDE") == {"CNC1"}
    notes = [n for n in trace["rule6"]["notes"] if n.startswith(new_engine.FIXED_PLAN_PREFIX)]
    assert any("CNC SECOND SIDE" in n and "nobody in Settings can run CNC2" in n
               and "another allowed machine if there is one" in n
               for n in notes), notes


def test_rows_for_unknown_orders_and_machines_are_ignored():
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free) + [
        {"batch_id": "B999", "item_code": "GONE", "process_seq": 1, "process_name": "X",
         "machine": "CNC1", "operator": "", "start": "2025-03-03T08:00:00",
         "end": "2025-03-03T09:00:00", "so_refs": ["NSO-999"]},
        {"batch_id": "B998", "item_code": ITEM_A, "process_seq": 1,
         "process_name": "CNC FIRST SIDE", "machine": "CNC99", "operator": "",
         "start": "2025-03-03T08:00:00", "end": "2025-03-03T09:00:00", "so_refs": ["NSO-998"]}]
    fixed, trace = _plan(m, cfg, lines, published=rows)
    assert trace["rule6"]["error"] is None
    assert fixed


def test_a_clubbed_batch_finds_its_pin_through_any_shared_so():
    """Published when SO2 alone; now NSO-000 (same item, sorts FIRST) clubs into the
    batch, so its so_refs are {NSO-000, SO2}. The pin is still found through SO2."""
    m, cfg, lines = _setup()
    _free, rows, other = _other_machine_rows(m, cfg, lines)
    more = lines + [SOLine("NSO-000", ITEM_B, ITEM_B, 10, date(2025, 3, 20))]
    fixed, _ = _plan(m, cfg, more, published=rows)
    assert _machine_of(fixed, ITEM_B, "CNC SECOND SIDE") == {other}


def test_published_plan_is_refused_off_the_new_engine():
    import pytest
    from engine.pipeline import OccupancyRequiresNewEngineError
    m, cfg, lines = _setup()
    with pytest.raises(OccupancyRequiresNewEngineError):
        _plan(m, replace(cfg, scheduler="classic"), lines, published=[{"x": 1}])


def test_frozen_pin_on_a_down_machine_waits_in_a_repair():
    """D11: a half-finished job on a machine marked down is NOT released by a repair."""
    from tests.test_machine_downtime_freeze import _ctx
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    masters = loaders.load_all(io.BytesIO(wb))[1]
    rows, orders, bbk, nm = _ctx(masters, [{"machine": "CNC1", "from_date": "2025-03-03",
                                            "to_date": "2025-03-04"}])
    kept = new_engine._ppc_frozen(rows, orders, bbk, nm, date(2025, 3, 3),
                                  release_on_downtime=False)
    gone = new_engine._ppc_frozen(rows, orders, bbk, nm, date(2025, 3, 3))
    assert len(kept) == 1 and gone == []


def test_a_repair_does_not_release_a_frozen_pin_but_a_free_plan_does(monkeypatch):
    """The pipeline passes release_on_downtime=False exactly when a plan is published."""
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    seen = []
    real = new_engine._ppc_frozen

    def spy(*a, **kw):
        seen.append(kw.get("release_on_downtime", True))
        return real(*a, **kw)

    monkeypatch.setattr(new_engine, "_ppc_frozen", spy)
    frozen = [{"so_no": SO1, "item_code": ITEM_A, "process": "VMC FIRST SIDE", "op_seq": 1,
               "machine": "VMC1", "operator": "", "remaining_qty": 5,
               "prev_start": "2025-03-03T08:00:00", "prev_end": "2025-03-04T08:00:00"}]
    pr = PlanRun(so_lines=list(lines))
    run_forward(pr, cfg, m, frozen=frozen, published=rows)
    run_forward(PlanRun(so_lines=list(lines)), cfg, m, frozen=frozen)
    assert seen == [False, True]


def test_a_published_queue_in_reverse_order_is_planned_in_full_on_its_machines():
    """Was the out-of-turn note test. D6 amended by the owner (spec section 8): a
    machine runs the next READY job in published order, so nothing ever waits for a
    turn and there is no turn to let through. Even a published plan whose queues run
    against the routing (every machine's queue reversed) is planned in full, every
    step on its published machine, with no note about turns."""
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    by_machine = {}
    for r in rows:
        by_machine.setdefault(r["machine"], []).append(r)
    for rs in by_machine.values():
        starts = sorted(r["start"] for r in rs)
        for r, st in zip(sorted(rs, key=lambda r: r["start"]), reversed(starts)):
            r["start"] = st
    fixed, trace = _plan(m, cfg, lines, published=rows)
    published = {(r["item_code"], r["process_name"]): r["machine"] for r in rows}
    for (item, step), mid in published.items():
        assert _machine_of(fixed, item, step) == {mid}, (item, step)
    assert not [n for n in trace["rule6"]["notes"] if "turn" in n]


def test_a_published_machine_gone_from_the_master_is_named():
    """Final review I5: a published machine that is no longer in the Machines list
    used to be dropped silently; the step was re-planned with nobody told."""
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    for r in rows:
        if r["item_code"] == ITEM_B and r["process_name"] == "CNC SECOND SIDE":
            r["machine"] = "CNC9"          # not in the master at all
    fixed, trace = _plan(m, cfg, lines, published=rows)
    assert _machine_of(fixed, ITEM_B, "CNC SECOND SIDE") <= {"CNC1", "CNC2"}
    notes = [n for n in trace["rule6"]["notes"] if n.startswith(new_engine.FIXED_PLAN_PREFIX)]
    assert any("CNC SECOND SIDE" in n and "CNC9" in n and "no longer in the Machines list" in n
               and "Press Optimize" in n for n in notes), notes
    assert not any("—" in n for n in notes)


def test_a_published_step_gone_from_the_routing_is_named():
    """Final review I5: a published step whose name is no longer in the item's
    routing used to be dropped silently."""
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    for r in rows:
        if r["item_code"] == ITEM_B and r["process_name"] == "CNC SECOND SIDE":
            r["process_name"] = "CNC THIRD SIDE"      # renamed in the routing since
    _fixed, trace = _plan(m, cfg, lines, published=rows)
    notes = [n for n in trace["rule6"]["notes"] if n.startswith(new_engine.FIXED_PLAN_PREFIX)]
    assert any("'CNC THIRD SIDE' is no longer in this item's routing" in n
               and "Press Optimize" in n for n in notes), notes


def test_a_published_row_for_an_order_not_in_this_plan_stays_silent():
    """Only orders in THIS plan are named: a row for a finished order is not news."""
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    for r in rows:
        if r["item_code"] == ITEM_B:
            r["machine"] = "CNC9"
            r["process_name"] = "GONE"
    _fixed, trace = _plan(m, cfg, lines[:1], published=rows)
    notes = [n for n in trace["rule6"]["notes"] if n.startswith(new_engine.FIXED_PLAN_PREFIX)]
    assert notes == [], notes
