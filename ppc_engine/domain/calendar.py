"""The shop calendar — which days nobody/nothing works.

From the "Weekly off & holiday master" (RULES.md Part 1 §1): every Thursday is off,
plus named holidays (whole shop closed), plus per-operator leave (only that person is
out).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Monday=0 … Sunday=6 (Python's date.weekday()). Thursday = 3.
THURSDAY = 3


@dataclass(frozen=True)
class ShopCalendar:
    """Non-working days.

    Attributes:
        weekly_off_weekday: The weekly off day as a Python weekday int (default
                            Thursday = 3).
        holidays:           Dates the whole shop is closed.
        leaves:             Map of operator name → set of dates that person is on
                            leave (only that person is unavailable those days).
        machine_downtime:   Map of machine id -> set of dates that machine is out of
                            service for maintenance (only that machine stops; the shop
                            keeps running). Empty by default, so a shop with no
                            maintenance on file behaves exactly as before.
    """

    weekly_off_weekday: int = THURSDAY
    holidays: frozenset[date] = field(default_factory=frozenset)
    leaves: dict[str, frozenset[date]] = field(default_factory=dict)
    machine_downtime: dict[str, frozenset[date]] = field(default_factory=dict)

    def is_working_day(self, day: date) -> bool:
        """True if the shop runs at all on ``day`` (not the weekly off, not a holiday)."""
        if day.weekday() == self.weekly_off_weekday:
            return False
        if day in self.holidays:
            return False
        return True

    def is_operator_available(self, operator_name: str, day: date) -> bool:
        """True if ``operator_name`` can work on ``day``.

        A person is available when the shop is open that day AND they are not on
        personal leave that day.
        """
        if not self.is_working_day(day):
            return False
        return day not in self.leaves.get(operator_name, frozenset())

    def is_machine_available(self, machine_id: str, day: date) -> bool:
        """True if ``machine_id`` can run on ``day``.

        A machine runs when the shop is open that day AND it is not out of service
        for maintenance. Mirror of ``is_operator_available`` — a person's leave and
        a machine's maintenance are the same idea applied to the two resources an
        operation needs.
        """
        if not self.is_working_day(day):
            return False
        return day not in self.machine_downtime.get(machine_id, frozenset())
