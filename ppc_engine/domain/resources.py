"""Machines and people — the two resources every operation may need.

This is a dual-resource shop: a machining operation needs BOTH a machine and a
qualified operator. See RULES.md Rule 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class MachineKind(Enum):
    """What category a machine belongs to — this decides who can run it and whether
    it carries a setup time.

    - MACHINING  : CNC lathes and VMCs. Run by an *Operator*. Charge 90-min setup.
                   Can run on BOTH shifts (operators cover both shifts).
    - MANUAL     : band saw, deburring, punching, packing, washing, assembly, lathe,
                   drilling. Run by a *Helper*. No setup. First shift only.
    - INSPECTION : the MI stations and the CMM. Run by an *Inspector*. No setup.
                   First shift only.
    """

    MACHINING = "machining"
    MANUAL = "manual"
    INSPECTION = "inspection"


@dataclass(frozen=True)
class Machine:
    """A single physical (or, for MW/MPK/MI, "artificial") machine station.

    Attributes:
        id:              Canonical machine id, e.g. ``"CNC3"``, ``"VMC1"``, ``"MI1"``.
                         Names are normalised so ``"CNC 3"`` and ``"CNC3"`` are one id.
        type_text:       The raw machine-type text from the master (for display), e.g.
                         ``"CNC lathe"``.
        kind:            MachineKind — drives operator role, setup, and shift coverage.
        available_hrs_per_day: Hours/day the machine may run (from the master:
                         19.5 for CNC/VMC, 9.5 for the rest). Reserved for a later
                         break/capacity reconciliation (see ARCHITECTURE.md v1 scope).
    """

    id: str
    type_text: str
    kind: MachineKind
    available_hrs_per_day: float

    # Mirror of engine/models.py::Machine.is_two_shift's default. The threshold lives
    # in Config.two_shift_threshold_hours, which a dataclass property cannot reach, so
    # it is duplicated here; the two must stay equal (tests/test_second_shift_rule.py).
    _TWO_SHIFT_THRESHOLD_HRS = 12.0

    @property
    def runs_second_shift(self) -> bool:
        """True if this machine can run on the night (second) shift.

        Decided by the master's **Available Hrs/Day**, never by machine kind
        (2026-09-05, owner correction). The old rule was
        ``self.kind == MachineKind.MACHINING`` — "only machining runs at night" — and
        that is not Anvitech's rule. Non-machining work (deburring, drilling, the
        centre lathe, packing) DOES run at night whenever somebody qualified is
        rostered on that shift; the shift is gated by STAFFING, not by the kind of
        work. `staffing.candidate_operator` is that gate and returns None when nobody
        qualified is on shift, so the operation simply waits.

        The old rule silently erased anyone rostered on nights for non-machining work.
        Measured on the live book: one such operator (13 stations, second shift) was
        given ZERO work, three stations he covers starved for 393 hours, and the book
        carried 57 late-days that this rule alone was responsible for.

        This now matches ``engine/models.py::Machine.is_two_shift`` exactly, so the
        engine and the reporting side (``operator_coverage.eligible_window``, used by
        Analytics and the delay justification report) finally share ONE rule instead of
        agreeing by coincidence. Blank/None → two-shift, an explicit 0 stays
        single-shift — same back-compat as the app side.
        """
        if self.available_hrs_per_day is None:
            return True
        return self.available_hrs_per_day >= self._TWO_SHIFT_THRESHOLD_HRS

    @property
    def needs_setup(self) -> bool:
        """True if a 90-min setup is charged per operation (machining only)."""
        return self.kind == MachineKind.MACHINING


class Role(Enum):
    """The three job roles at Anvitech (RULES.md)."""

    OPERATOR = "operator"    # runs CNC/VMC machining
    HELPER = "helper"        # runs manual stations (first shift only)
    INSPECTOR = "inspector"  # runs MI / CMM inspection (first shift only)


# Which role is allowed to run which kind of machine.
ROLE_FOR_KIND = {
    MachineKind.MACHINING: Role.OPERATOR,
    MachineKind.MANUAL: Role.HELPER,
    MachineKind.INSPECTION: Role.INSPECTOR,
}


class Shift(Enum):
    """The two shifts. Timings live in PlanConfig; these are just the labels.

    FIRST  : day shift   (default 08:00 → 19:00)
    SECOND : night shift (default 19:00 → 05:00 next day)
    """

    FIRST = "first"
    SECOND = "second"


@dataclass(frozen=True)
class Operator:
    """A person on the floor.

    Attributes:
        name:              Unique display name (also the leave key in the calendar).
        role:              Operator / Helper / Inspector.
        qualified_machines: Set of canonical machine ids this person may run. A person
                           can only run a machine listed here (RULES.md Rule 1).
        base_shift:        The person's shift *as of the plan start*. Operators flip
                           this every Friday (rotation); helpers/inspectors never
                           rotate and are always FIRST. See worktime.effective_shift.
    """

    name: str
    role: Role
    qualified_machines: frozenset[str] = field(default_factory=frozenset)
    base_shift: Shift = Shift.FIRST

    @property
    def flexibility(self) -> int:
        """How many machines this person can run.

        Lower = scarcer/less flexible. The scheduler assigns scarce people first so
        flexible people stay free for machines only they can run (a measured win from
        the old build — see OPTIMIZATION.md / LESSONS.md).
        """
        return len(self.qualified_machines)
