"""The shop calendar — which days nobody/nothing works.

From the "Weekly off & holiday master" (RULES.md Part 1 §1): every Thursday is off,
plus named holidays (whole shop closed), plus per-operator leave (only that person is
out).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ppc_engine.domain.resources import Shift

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
        machine_busy:       Map of machine id → tuple of (start, end) blocks when the
                            machine is already committed by an earlier planning stage.
                            Empty by default, so every ordinary plan behaves as before.
        operator_busy:      Map of operator name → tuple of (start, end) blocks when
                            that person is already committed by an earlier planning
                            stage. Empty by default.
        machine_shift_operator: Map of (machine id, date, Shift) → operator name for
                                staff already assigned. Empty by default.
    """

    # --- Occupancy: what an EARLIER planning stage already committed -------------
    # Empty on every ordinary plan, so nothing below changes. Populated only by the
    # two-stage plan behind the Add New Orders quote (2026-09-08 spec): stage 1's
    # placements become stage 2's occupied time, which is how a new order can be
    # fitted in without any existing order moving.

    weekly_off_weekday: int = THURSDAY
    holidays: frozenset[date] = field(default_factory=frozenset)
    leaves: dict[str, frozenset[date]] = field(default_factory=dict)
    machine_downtime: dict[str, frozenset[date]] = field(default_factory=dict)
    machine_busy: dict[str, tuple[tuple[datetime, datetime], ...]] = field(default_factory=dict)
    operator_busy: dict[str, tuple[tuple[datetime, datetime], ...]] = field(default_factory=dict)
    machine_shift_operator: dict[tuple[str, date, Shift], str] = field(default_factory=dict)

    # Private cache: each machine's ``machine_busy`` blocks, sorted and merged, once.
    # `free_runs` is called roughly once per candidate machine per dispatch decision
    # (~90,000 times over a plan), and stage 2 of the Add New Orders quote seeds one
    # block per stage-1 segment — hundreds on a busy machine. Only the SORT+MERGE is
    # amortized here — building the runs list from the merged blocks is still
    # O(blocks) on every call, since it depends on the query's own `after` (see
    # `free_runs` below). Measured at 500 blocks: 2.3x faster overall when `after`
    # lands near the start of the range (most of the cost was runs-building anyway),
    # 8.0x when it lands near the end (most of the cost WAS the now-skipped
    # sort+merge). `compare=False`/`repr=False` so it never affects equality or
    # `repr` — it is a memoization detail, not shop state.
    # `ShopCalendar` is frozen, so the FIELD is never rebound after construction —
    # only the dict's own contents are populated lazily, which a frozen dataclass
    # still allows. ``init=False`` is load-bearing, not cosmetic: `dataclasses.
    # replace()` (used throughout this codebase to build a modified calendar) copies
    # every INIT field's current value into the new instance — with `init=True` a
    # `replace()` that changes `machine_busy` would carry the OLD cache along with
    # it, serving stale merged blocks for any machine already cached. `init=False`
    # means every new instance (constructed directly or via `replace()`) gets its
    # own fresh, empty cache from `default_factory`, so a cache can never outlive
    # the calendar whose blocks it was built from.
    _merged_busy_cache: dict[str, tuple[tuple[datetime, datetime], ...]] = field(
        default_factory=dict, compare=False, repr=False, init=False)

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

    def _merged_busy(self, machine_id: str) -> tuple[tuple[datetime, datetime], ...]:
        """``machine_id``'s busy blocks, sorted and merged (overlapping/touching
        blocks combined), computed once and cached. Independent of ``after`` —
        `free_runs` applies the query's start point afterward — so the cached value
        is reusable across every call for this machine regardless of where in time
        each caller starts looking.
        """
        cached = self._merged_busy_cache.get(machine_id)
        if cached is not None:
            return cached
        blocks = self.machine_busy.get(machine_id) or ()
        merged: list[list[datetime]] = []
        for start, end in sorted(blocks):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        result = tuple((start, end) for start, end in merged)
        self._merged_busy_cache[machine_id] = result
        return result

    def free_runs(self, machine_id: str, after: datetime) -> list[tuple[datetime, datetime | None]]:
        """The stretches of time ``machine_id`` is NOT already occupied, on/after
        ``after``, in time order. The final run is open-ended (``end is None``).

        With no occupancy on file this is a single unbounded run starting at
        ``after`` — which is exactly "no restriction", so every ordinary plan behaves
        as it always has.

        A job is laid inside ONE run and never across two (see the spec's gap rule):
        crossing a run boundary would mean the machine was torn down for another job
        in between, and the setup is not paid twice.
        """
        merged = self._merged_busy(machine_id)
        if not merged:
            return [(after, None)]
        runs: list[tuple[datetime, datetime | None]] = []
        cursor = after
        for start, end in merged:
            if end <= after:
                continue
            if start > cursor:
                runs.append((cursor, start))
            cursor = max(cursor, end)
        runs.append((cursor, None))
        return runs
