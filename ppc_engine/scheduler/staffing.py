"""Operator↔machine staffing — this is where the STABILITY rule is enforced.

The rule that this whole rebuild exists for (RULES.md Rule 1, LESSONS.md "the
decisive one"): **one operator mans one machine for a whole shift; no hour-by-hour
hopping.** This board makes that true by construction:

  - An assignment is keyed by (machine, shift-date, shift). Once a person is put on a
    machine for a shift, that is fixed for the whole shift.
  - A person can be committed to at most ONE machine per shift (tracked in ``_busy``).
  - The decoder only ever asks this board for an operator *per shift*, never
    per-hour — so a ping-ponging schedule simply cannot be expressed.

The board is READ-ONLY during evaluation: the decoder computes a tentative placement
by reading committed assignments and accumulating the *new* assignments it would make
in a local list, committing them only for the placement it actually chooses. (Within
one operation each (machine, shift-date, shift) is touched at most once, so no local
overlay is needed while laying a single operation.)
"""

from __future__ import annotations

from datetime import date, datetime

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.resources import Machine, Operator, ROLE_FOR_KIND, Shift
from ppc_engine.scheduler.duration import operation_duration_min
from ppc_engine.worktime import effective_shift


def build_machine_pools(masters: Masters) -> dict[str, tuple[Operator, ...]]:
    """Precompute, per machine, the operators who could ever run it — role-matched and
    qualified — pre-sorted scarce-first (fewest qualified machines, then name).

    This is static for a given shop, so computing it once turns the per-window operator
    search from "scan all 19 people and sort" into "scan ~6 pre-sorted candidates".
    """
    pools: dict[str, tuple[Operator, ...]] = {}
    for mid, machine in masters.machines.items():
        role = ROLE_FOR_KIND[machine.kind]
        eligible = [o for o in masters.operators if o.role == role and mid in o.qualified_machines]
        eligible.sort(key=lambda o: (o.flexibility, o.name))
        pools[mid] = tuple(eligible)
    return pools


def machine_demand(orders, masters: Masters, config: PlanConfig) -> dict[str, float]:
    """Per-machine remaining processing minutes — the 'how busy is each machine' signal
    for the demand-aware 'bottleneck' pick. For every in-house op of every order, take
    the op's duration at its remaining qty (the same duration the scheduler uses) and add
    an expected share (duration / number of machine options) to each option's total.
    OS/DISPATCH ops (no machine_options) contribute nothing; an order whose item has no
    routing is skipped. Static for a plan — computed once."""
    demand: dict[str, float] = {}
    for order in orders:
        routing = masters.routings.get(order.item_code)
        if routing is None:
            continue
        for op in routing.operations:
            if not op.machine_options:
                continue
            op_qty = order.qty
            pr = getattr(order, "process_remaining", None)
            if pr is not None:
                op_qty = pr.get(op.seq, order.qty)
            dur = operation_duration_min(op, op_qty, config)
            if dur <= 0:
                continue
            share = dur / len(op.machine_options)
            for mid in op.machine_options:
                demand[mid] = demand.get(mid, 0.0) + share
    return demand


class StaffingBoard:
    """Tracks which operator mans which machine, and each operator's busy TIME intervals.

    Short-job exception (2026-07-24): an operator is reserved for a machine only for the
    actual DURATION of the work, not the whole shift. So a LONG job keeps its operator
    busy all shift (one-operator-per-machine-per-shift stability is preserved by
    construction — they can't be anywhere else), while a SHORT job frees the operator to
    man another idle machine later that shift. Availability is an interval-overlap check.
    """

    def __init__(self, pools: dict[str, tuple[Operator, ...]] | None = None,
                 demand: dict[str, float] | None = None) -> None:
        # (machine_id, shift_date, shift) -> operator name that (last) manned it — a soft
        # preference for machine stability, not a hard lock (short jobs may share).
        self._assign: dict[tuple[str, date, Shift], str] = {}
        # operator name -> list of committed busy (start, end) intervals.
        self._intervals: dict[str, list[tuple[datetime, datetime]]] = {}
        # machine id -> pre-sorted eligible operators (scarce-first). See build_machine_pools.
        self._pools: dict[str, tuple[Operator, ...]] = pools or {}
        # operator name -> cumulative committed busy minutes (for the "balanced" pick).
        self._load: dict[str, float] = {}
        # machine id -> total remaining processing minutes (the 'bottleneck' pick's
        # demand signal). Empty => bottleneck degrades to scarce. See machine_demand().
        self._demand: dict[str, float] = demand or {}

    def add_load(self, name: str, minutes: float) -> None:
        """Record committed work for an operator (drives the 'balanced' pick policy)."""
        self._load[name] = self._load.get(name, 0.0) + minutes

    def operator_for(self, machine_id: str, day: date, shift: Shift) -> str | None:
        """The operator that (last) manned ``machine_id`` on this shift, or None. A
        machine-stability PREFERENCE — reuse them only if still free for the new interval."""
        return self._assign.get((machine_id, day, shift))

    def free_during(self, name: str, start: datetime, end: datetime) -> bool:
        """True if ``name`` has no committed busy interval overlapping [start, end)."""
        for s, e in self._intervals.get(name, ()):
            if s < end and start < e:
                return False
        return True

    def candidate_operator(
        self,
        machine: Machine,
        day: date,
        shift: Shift,
        start: datetime,
        end: datetime,
        masters: Masters,
        config: PlanConfig,
    ) -> str | None:
        """Pick the best free, qualified operator to man ``machine`` this shift.

        An operator is eligible when ALL hold:
          - their role matches the machine kind (operator↔machining, helper↔manual,
            inspector↔inspection),
          - they are qualified for this machine (it's in their qualified set),
          - they are on THIS shift on this date (after Friday rotation), and
          - they are available (shop open + not on personal leave), and
          - they are not already manning another machine this shift.

        Among eligible people, pick the **least flexible** first (scarce-first — keeps
        flexible people free for machines only they can run; a measured win from the
        old build). Ties break by name for determinism.

        Returns the chosen operator name, or None if nobody is available (in which
        case the machine cannot run this shift and the operation must wait).
        """
        # Free, eligible operators for this machine+shift (pool is pre-sorted
        # scarce-first: ascending flexibility, then name).
        free = [
            op
            for op in self._pools.get(machine.id, ())
            if effective_shift(op, day, config) == shift
            and masters.calendar.is_operator_available(op.name, day)
            and self.free_during(op.name, start, end)
        ]
        if not free:
            return None

        pick = getattr(config, "operator_pick", "scarce")
        if pick == "flexible":
            return free[-1].name  # most flexible (pool sorted ascending flexibility)
        if pick == "balanced":
            # least cumulative load; ties → scarce (fewer machines) → name.
            return min(free, key=lambda o: (self._load.get(o.name, 0.0), o.flexibility, o.name)).name
        if pick == "bottleneck":
            return self._bottleneck_pick(machine, free, day, shift, start, end, masters, config)
        return free[0].name  # "scarce" (default): least flexible

    def _bottleneck_pick(self, machine, free, day, shift, start, end, masters, config):
        """Assign the free operator we can most SPARE: the one whose OTHER machines carry
        the least demand. If an operator is the ONLY free cover for one of their other
        busy machines, that machine's full demand counts against pulling them here
        (strands it); if at least one other qualified operator is also free to cover it
        right now, it doesn't count at all (one-step look-ahead — someone else can take
        it, so pulling this candidate here doesn't strand anything). Ties -> the scarce
        order (flexibility, name) -> identical to scarce when demand is flat/empty.

        Note: this is a hard strand check (0 vs full demand), not a continuous decay —
        d / (1 + others_free) can never reach exactly 0 for any finite backup count when
        d > 0, so a smooth discount can never let a candidate with real demand elsewhere
        out-cost one with none (see test_bottleneck_strand_discount_when_others_cover)."""
        def others_free(mprime, cand):
            return sum(
                1 for op in self._pools.get(mprime, ())
                if op.name != cand.name
                and effective_shift(op, day, config) == shift
                and masters.calendar.is_operator_available(op.name, day)
                and self.free_during(op.name, start, end))

        def cost(cand):
            total = 0.0
            for mprime in cand.qualified_machines:
                if mprime == machine.id:
                    continue
                d = self._demand.get(mprime, 0.0)
                if d <= 0:
                    continue
                if others_free(mprime, cand) == 0:
                    total += d
            return total

        return min(free, key=lambda o: (cost(o), o.flexibility, o.name)).name

    def commit(self, machine_id: str, day: date, shift: Shift, name: str,
               start: datetime, end: datetime) -> None:
        """Book ``name`` onto ``machine_id`` for the interval [start, end) this shift.
        The operator is now busy for exactly that time (not the whole shift), so a short
        job leaves them free to man another machine later. ``_assign`` records them as the
        machine's shift operator (a stability preference for the next op on this machine)."""
        self._assign[(machine_id, day, shift)] = name
        self._intervals.setdefault(name, []).append((start, end))
