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

from datetime import date

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.resources import Machine, Operator, ROLE_FOR_KIND, Shift
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


class StaffingBoard:
    """Tracks which operator mans which machine, per shift."""

    def __init__(self, pools: dict[str, tuple[Operator, ...]] | None = None) -> None:
        # (machine_id, shift_date, shift) -> operator name currently manning it.
        self._assign: dict[tuple[str, date, Shift], str] = {}
        # (shift_date, shift) -> set of operator names already committed that shift.
        self._busy: dict[tuple[date, Shift], set[str]] = {}
        # machine id -> pre-sorted eligible operators (scarce-first). See build_machine_pools.
        self._pools: dict[str, tuple[Operator, ...]] = pools or {}
        # operator name -> cumulative committed busy minutes (for the "balanced" pick).
        self._load: dict[str, float] = {}

    def add_load(self, name: str, minutes: float) -> None:
        """Record committed work for an operator (drives the 'balanced' pick policy)."""
        self._load[name] = self._load.get(name, 0.0) + minutes

    def operator_for(self, machine_id: str, day: date, shift: Shift) -> str | None:
        """The operator already manning ``machine_id`` on this shift, or None."""
        return self._assign.get((machine_id, day, shift))

    def _is_free(self, name: str, day: date, shift: Shift) -> bool:
        """True if ``name`` is not already committed to some machine this shift."""
        return name not in self._busy.get((day, shift), ())

    def candidate_operator(
        self,
        machine: Machine,
        day: date,
        shift: Shift,
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
            and self._is_free(op.name, day, shift)
        ]
        if not free:
            return None

        pick = getattr(config, "operator_pick", "scarce")
        if pick == "flexible":
            return free[-1].name  # most flexible (pool sorted ascending flexibility)
        if pick == "balanced":
            # least cumulative load; ties → scarce (fewer machines) → name.
            return min(free, key=lambda o: (self._load.get(o.name, 0.0), o.flexibility, o.name)).name
        return free[0].name  # "scarce" (default): least flexible

    def commit(self, machine_id: str, day: date, shift: Shift, name: str) -> None:
        """Fix ``name`` onto ``machine_id`` for this shift (idempotent for the same
        person; enforces one-machine-per-person-per-shift for a different person)."""
        key = (machine_id, day, shift)
        existing = self._assign.get(key)
        if existing == name:
            return
        assert existing is None, (
            f"machine {machine_id} already staffed by {existing} on {day}/{shift.value}"
        )
        self._assign[key] = name
        self._busy.setdefault((day, shift), set()).add(name)
