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
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from engine import new_engine, optimizer
from engine.models import PlanRun, SOLine
from engine.pipeline import run_forward

# Above this many new lines, trying every ordering costs more than it is worth
# (5 lines = 120 orderings). At or below it, every arrangement is tried and the
# answer is provably the best available under the given occupancy. Above it we
# try the typed order plus its rotations only, so the search stays cheap.
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


def _as_so_line(line: QuoteLine, plan_start: date) -> SOLine:
    """A new order line as the SOLine the rules consume. The delivery date is the
    director's target when he has one, else the plan start, a placeholder that
    affects only Rule 2's sort among the NEW lines (stage 2 has nothing else in
    it), never a promise."""
    return SOLine(so_no=line.so_no, item_code=line.item_code,
                  item_name=line.item_name, qty=float(line.qty),
                  delivery_date=line.target_date or plan_start)


def _arrangements(lines: list[QuoteLine]) -> list[list[QuoteLine]]:
    """The orderings of the new lines to try. Exhaustive while that is cheap."""
    if len(lines) <= 1:
        return [list(lines)]
    if len(lines) <= _EXHAUSTIVE_MAX:
        return [list(p) for p in itertools.permutations(lines)]
    out = [list(lines)]
    for i in range(1, min(_SAMPLED_ARRANGEMENTS, len(lines))):
        out.append(lines[i:] + lines[:i])
    return out


def _stage2(lines, occupancy, config, masters, flexible, reserved):
    """Plan the new lines only, around the occupancy. Returns (entries, expected).

    ``frozen`` is deliberately never passed here: ``ppc_engine.scheduler.decode``
    refuses occupancy together with a non-empty frozen set (a second-stage plan
    and a physically in-progress operation can never both be pinned onto the
    same op), and stage 2 always carries occupancy. A frozen set belongs to
    stage 1, which the caller already computed."""
    cfg = replace(config, flexible_machines=flexible)
    pr = PlanRun(so_lines=[_as_so_line(l, config.plan_start_date) for l in lines])
    run_forward(pr, cfg, masters, occupancy=occupancy, reserved=reserved)
    return pr.schedule, optimizer.expected_completion(pr.schedule)


def _score(lines, expected):
    """Lower is better: total days late against each line's target (0 when he has
    not named one, which is the normal case for a first quote), then the latest
    completion, then the sum, so an arrangement that finishes everything sooner
    wins."""
    late = 0
    latest = date.min
    total = 0
    for line in lines:
        got = expected.get((line.so_no, line.item_code))
        if got is None:
            return (10**9, date.max, 10**9)
        if line.target_date and got > line.target_date:
            late += (got - line.target_date).days
        latest = max(latest, got)
        total += got.toordinal()
    return (late, latest, total)


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
    themselves. Three checks, any of which can genuinely fail:

      1. no stage-2 machine interval overlaps a stage-1 interval on that machine,
      2. no stage-2 operator segment overlaps a stage-1 segment for that person,
      3. the stage-2 order keys are disjoint from the stage-1 order keys.

    Returns a list of plain-English descriptions, one per violation found.
    Empty means the guarantee held."""
    problems: list[str] = []

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
    """
    routable, unroutable = [], []
    for line in new_lines:
        (routable if line.item_code in masters.routings else unroutable).append(line)

    rows = {l.so_no + "\x1f" + l.item_code:
            {"so_no": l.so_no, "item_code": l.item_code, "item_name": l.item_name,
             "qty": l.qty, "target_date": l.target_date, "completion": None,
             "error": "no routing found for this item, it cannot be scheduled"}
            for l in unroutable}

    best_score = best_expected = best_entries = failure = None
    if routable:
        occupancy = new_engine.occupancy_from_entries(existing_entries, config)
        for flexible in (False, True):
            for arrangement in _arrangements(routable):
                try:
                    entries, expected = _stage2(arrangement, occupancy, config,
                                                masters, flexible, reserved)
                except Exception as exc:            # noqa: BLE001 - report, never 500
                    failure = failure or str(exc)
                    continue
                score = _score(arrangement, expected)
                if best_score is None or score < best_score:
                    best_score, best_expected, best_entries = score, expected, entries
        for line in routable:
            got = best_expected.get((line.so_no, line.item_code)) if best_expected else None
            rows[line.so_no + "\x1f" + line.item_code] = {
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
    ordered = [rows[l.so_no + "\x1f" + l.item_code] for l in new_lines]
    return QuoteResult(lines=ordered, moved=moved, violations=violations,
                       verified=not moved and not violations,
                       existing_count=len(existing_expected))
