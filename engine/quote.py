"""Quote a delivery date for a new order, against the plan actually in force.

Pure: no store, no HTTP, no side effects. See
docs/superpowers/specs/2026-09-08-add-new-orders-quote-design.md.

The one idea: the plan is computed in TWO stages. Stage 1 is today's plan over
today's book, the caller passes it in, already computed. Stage 2 plans the new
orders against a shop whose machines and people stage 1 has already occupied.

Existing orders cannot move because they are not in stage 2 at all. That is a
guarantee by construction, not a hope. Two independent checks confirm it before
a date is ever shown:

  1. ``verify_unmoved`` recomputes stage 1's own expected completion from
     ``existing_entries`` and compares it to what the caller says stage 1
     produced. This can only catch a caller passing mismatched inputs (it
     cannot catch anything stage 2 did, since it never looks at stage 2).
  2. ``structural_violations`` looks at stage 2's OWN OUTPUT and proves it
     never touches a machine minute or an operator minute stage 1 already
     claimed, and never reuses an existing order's key. This is the check
     that can actually fail if stage 2 goes wrong, because a promise nobody
     checks is how the delay report ended up blaming the crew for
     outsourcing (2026-08-09).

``quote`` refuses to show a date (``verified=False``) if either check fails.

Searching the new lines' sequence: this engine does not read job order off list
order at all. Rule 1 (``rule1_consolidate``) regroups lines by item and Rule 2
(``rule2_sort_by_date``) sorts the resulting batches by delivery date before
anything is scheduled, so the order ``quote()`` happens to build its internal
SOLine list in has no effect on the plan. The only lever that actually moves
sequence is ``priority_rank`` (see ``engine.pipeline.apply_priority_rank``),
the same rank map the Optimize feature produces and replays. Measured (see the
task report): three rank maps over two competing items produced two distinct
schedules and two distinct completion dates, so the search below varies
``priority_rank``, never list order.

The machine set (``flexible_machines``) is deliberately NOT searched here,
unlike the Optimize contest. A quote must equal what the Orders tab will show
the moment the order is entered, and the plan that actually runs afterwards
uses the SAVED ``config.flexible_machines``, never a value discovered by this
quote alone. Stage 2 is pinned to ``config.flexible_machines`` as given.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import date, datetime

from engine import new_engine, optimizer
from engine.models import PlanRun, SOLine
from engine.pipeline import run_forward

# Above this many new lines, trying every permutation of priority_rank costs more
# than it is worth (5 lines = 120 permutations). At or below it, every ordering
# is tried and the answer is provably the best available under the given
# occupancy and machine set. Above it we try the typed order plus its
# rotations only, so the search stays cheap.
_EXHAUSTIVE_MAX = 4
_SAMPLED_ARRANGEMENTS = 24


@dataclass(frozen=True)
class QuoteLine:
    """One new order line the director typed. ``target_date`` is set only on the
    preponed path, where it becomes the line's delivery date."""
    so_no: str
    item_code: str
    item_name: str
    qty: float
    target_date: date | None = None


@dataclass(frozen=True)
class QuoteResult:
    lines: list = field(default_factory=list)
    moved: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    verified: bool = True
    existing_count: int = 0


def _rank_key(so_no: str, item_code: str) -> str:
    return f"{so_no}\x1f{item_code}"


def _as_so_line(line: QuoteLine, plan_start: date) -> SOLine:
    """A new order line as the SOLine the rules consume. The delivery date is the
    director's target when he has one, else the plan start, a placeholder that
    affects only Rule 2's sort among the NEW lines (stage 2 has nothing else in
    it), never a promise."""
    return SOLine(so_no=line.so_no, item_code=line.item_code,
                  item_name=line.item_name, qty=float(line.qty),
                  delivery_date=line.target_date or plan_start)


def _rank_orderings(lines: list[QuoteLine]) -> list[dict | None]:
    """The ``priority_rank`` maps to try. Sequence in this engine is expressed by
    priority_rank, not by list order (see the module docstring), so this is the
    real search dimension for "which new order goes first". Each returned dict
    assigns a distinct 1-based rank to every line, one permutation per entry.
    ``None`` means "nothing to search" (0 or 1 lines: only one possible order)."""
    n = len(lines)
    if n <= 1:
        return [None]
    if n <= _EXHAUSTIVE_MAX:
        index_orders = list(itertools.permutations(range(n)))
    else:
        index_orders = [tuple(range(n))]
        for i in range(1, min(_SAMPLED_ARRANGEMENTS, n)):
            index_orders.append(tuple(range(i, n)) + tuple(range(i)))
    return [
        {_rank_key(lines[idx].so_no, lines[idx].item_code): rank
         for rank, idx in enumerate(order, start=1)}
        for order in index_orders
    ]


def _stage2(lines, occupancy, config, masters, priority_rank, reserved):
    """Plan the new lines only, around the occupancy, replaying one candidate
    ``priority_rank``. Returns (entries, expected).

    Pinned to ``config.flexible_machines`` as given, never varied: the quote
    must equal what the Orders tab shows the moment the order is added, and the
    later two-stage plan runs at the saved setting, not one this quote alone
    discovered (see the module docstring).

    ``frozen`` is deliberately never passed here: ``ppc_engine.scheduler.decode``
    refuses occupancy together with a non-empty frozen set (a second-stage plan
    and a physically in-progress operation can never both be pinned onto the
    same op), and stage 2 always carries occupancy. A frozen set belongs to
    stage 1, which the caller already computed."""
    pr = PlanRun(so_lines=[_as_so_line(l, config.plan_start_date) for l in lines])
    run_forward(pr, config, masters, occupancy=occupancy, reserved=reserved,
                priority_rank=priority_rank)
    return pr.schedule, optimizer.expected_completion(pr.schedule)


def _score(lines, expected):
    """Lower is better. The count of UNSCHEDULED lines is compared first, so an
    arrangement that schedules more lines always beats one that schedules
    fewer, even when both leave something unscheduled, there is no shared
    sentinel that would otherwise make every partial failure look identical
    and freeze the search on the first one tried. Then total days late against
    each line's target (0 when he has not named one, the normal case for a
    first quote), then the latest completion, then the sum, so an arrangement
    that finishes everything sooner wins."""
    unscheduled = 0
    late = 0
    latest = date.min
    total = 0
    for line in lines:
        got = expected.get((line.so_no, line.item_code))
        if got is None:
            unscheduled += 1
            continue
        if line.target_date and got > line.target_date:
            late += (got - line.target_date).days
        latest = max(latest, got)
        total += got.toordinal()
    return (unscheduled, late, latest, total)


def verify_unmoved(existing_expected, existing_expected_now):
    """Every existing order whose expected completion changed, comparing the
    caller's stage 1 dates against dates recomputed from the SAME stage 1
    entries. This only catches a caller passing mismatched inputs; it cannot
    catch anything stage 2 did (see ``structural_violations`` for that). MUST
    be empty."""
    moved = []
    for key, before in existing_expected.items():
        after = existing_expected_now.get(key)
        if after is not None and after != before:
            moved.append({"so_no": key[0], "item_code": key[1],
                          "before": before, "after": after,
                          "days": (after - before).days})
    moved.sort(key=lambda m: abs(m["days"]), reverse=True)
    return moved


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def structural_violations(existing_entries, stage2_entries, config) -> list[str]:
    """The real proof that stage 2 never moved an existing order, computed over
    stage 2's OWN OUTPUT rather than by comparing the caller's numbers to
    themselves. Checks, each of which can genuinely fail:

      1. no stage-2 machine interval overlaps a stage-1 interval on that machine,
      2. no stage-2 operator segment overlaps a stage-1 segment for that person,
      3. every stage-2 entry carries at least one SO reference (an entry with
         none could otherwise slip past check 4 unseen),
      4. the stage-2 order keys (so_no, item_code) are disjoint from the
         stage-1 order keys.

    Returns a list of plain-English descriptions, one per violation found.
    Empty means the guarantee held."""
    problems: list[str] = []

    # Both sides go through the SAME occupancy_from_entries helper, so this
    # check cannot catch that helper under-reporting stage 1's own occupancy.
    # Task 6's own tests cover that independently
    # (test_occupancy_lists_every_machine_block_and_skips_outsourcing,
    # test_occupancy_lists_every_operator_segment).
    stage1_occ = new_engine.occupancy_from_entries(existing_entries, config)
    stage2_occ = new_engine.occupancy_from_entries(stage2_entries, config)

    for machine, new_intervals in stage2_occ["machine"].items():
        old_intervals = stage1_occ["machine"].get(machine, ())
        for ns, ne in new_intervals:
            for os_, oe in old_intervals:
                if _overlaps(ns, ne, os_, oe):
                    problems.append(
                        f"machine {machine} is double-booked: the new plan runs "
                        f"{ns} to {ne} while the existing plan already runs "
                        f"{os_} to {oe} on it")

    for op, new_intervals in stage2_occ["operator"].items():
        old_intervals = stage1_occ["operator"].get(op, ())
        for ns, ne in new_intervals:
            for os_, oe in old_intervals:
                if _overlaps(ns, ne, os_, oe):
                    problems.append(
                        f"operator {op} is double-booked: the new plan books "
                        f"{ns} to {ne} while the existing plan already books "
                        f"{os_} to {oe}")

    for e in (stage2_entries or []):
        if not e.so_refs:
            problems.append(
                f"a new-order schedule entry for item {e.item_code} (seq "
                f"{e.process_seq}) carries no SO reference, so it cannot be "
                f"verified against the existing plan")

    existing_keys = {(ref, e.item_code) for e in (existing_entries or [])
                      for ref in (e.so_refs or [])}
    stage2_keys = {(ref, e.item_code) for e in (stage2_entries or [])
                    for ref in (e.so_refs or [])}
    for so_no, item_code in sorted(existing_keys & stage2_keys):
        problems.append(
            f"order {so_no} / {item_code} appears in both the existing plan "
            f"and the new lines' schedule, so it cannot be a NEW order")

    return problems


def quote(existing_entries, existing_expected, new_lines,
          config, masters, *, reserved=None, frozen=None) -> QuoteResult:
    """Quote each new line's completion date against the plan already in force.

    ``existing_entries`` is stage 1's finished schedule (the plan on screen) and
    ``existing_expected`` its per-order completion dates, both computed by the
    caller so the quote never plans the existing book a second way.

    ``reserved`` is forwarded to stage 2's own Rule 6 pass exactly like every
    other planning call (for example, operator absences that fall inside the
    quote window). ``frozen`` is accepted for interface symmetry with
    ``run_forward`` but is never forwarded to stage 2, see ``_stage2``.

    ``config.plan_start_date`` must already be resolved to a real date. The
    pure engine must never see ``None`` for it (an auto-mode config is the
    API's job to resolve before it reaches any pure planning entry point), so
    quote() raises rather than letting Rule 2's sort fail on a ``None``
    delivery date deep inside the rules.
    """
    if config.plan_start_date is None:
        raise ValueError(
            "quote() requires a resolved config.plan_start_date, not None. "
            "Resolve auto mode ('start from today') to a real date at the API "
            "boundary before calling quote(), the same rule every other pure "
            "planning entry point follows.")

    routable, unroutable = [], []
    for line in new_lines:
        (routable if line.item_code in masters.routings else unroutable).append(line)

    rows = {_rank_key(l.so_no, l.item_code):
            {"so_no": l.so_no, "item_code": l.item_code, "item_name": l.item_name,
             "qty": l.qty, "target_date": l.target_date, "completion": None,
             "error": "no routing found for this item, it cannot be scheduled"}
            for l in unroutable}

    best_score = best_expected = best_entries = failure = None
    if routable:
        occupancy = new_engine.occupancy_from_entries(existing_entries, config)
        for rank in _rank_orderings(routable):
            try:
                entries, expected = _stage2(routable, occupancy, config, masters,
                                            rank, reserved)
            except Exception as exc:            # noqa: BLE001 - report, never 500
                failure = failure or str(exc)
                continue
            score = _score(routable, expected)
            if best_score is None or score < best_score:
                best_score, best_expected, best_entries = score, expected, entries
        for line in routable:
            got = best_expected.get((line.so_no, line.item_code)) if best_expected else None
            rows[_rank_key(line.so_no, line.item_code)] = {
                "so_no": line.so_no, "item_code": line.item_code,
                "item_name": line.item_name, "qty": line.qty,
                "target_date": line.target_date, "completion": got,
                "error": None if got else (
                    failure or "could not be scheduled: no machine with a "
                               "qualified operator is available for one of its steps"),
            }

    moved = verify_unmoved(existing_expected,
                           optimizer.expected_completion(existing_entries))
    violations = structural_violations(existing_entries, best_entries or [], config)
    ordered = [rows[_rank_key(l.so_no, l.item_code)] for l in new_lines]
    return QuoteResult(lines=ordered, moved=moved, violations=violations,
                       verified=not moved and not violations,
                       existing_count=len(existing_expected))
