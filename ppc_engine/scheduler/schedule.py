"""The schedule result — what the decoder produces.

A Schedule is a flat list of Segments (each a piece of an operation running on a
machine, by an operator, over a time window) plus convenience lookups. It is the
engine's *complete* output — the API/web only render it, they never rebuild it
(LESSONS.md: engine owns its output).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ppc_engine.domain.routing import OperationKind


@dataclass(frozen=True)
class Segment:
    """One contiguous chunk of an operation running over one time window.

    A single operation can span several segments when it crosses shift/day
    boundaries (e.g. a long machining run continuing into the night shift with a
    different operator). OUTSOURCED runs as one continuous segment; DISPATCH is a
    zero-length milestone segment.

    Attributes:
        order_key:  (so_no, item_code) — which order this belongs to.
        op_seq:     The operation's position in the routing.
        op_name:    The operation name (for display).
        kind:       OperationKind.
        machine_id: Machine id, or None for OUTSOURCED / DISPATCH.
        operator:   Operator name, or None for OUTSOURCED / DISPATCH.
        start:      Segment start datetime.
        end:        Segment end datetime.
        qty:        Pieces this operation is scheduled for (the op's remaining qty on a
                    re-plan; the order qty on a fresh plan). 0 for milestones.
        resume_from: Set only on the RESUMED part of an in-progress step whose batch
                    still owes an earlier step (2026-09-27): the seq of that earlier
                    step. This part runs only the pieces already past it; the rest
                    of the step is a separate, unmarked operation laid later.
    """

    order_key: tuple[str, str]
    op_seq: int
    op_name: str
    kind: OperationKind
    machine_id: str | None
    operator: str | None
    start: datetime
    end: datetime
    qty: int = 0
    resume_from: int | None = None


@dataclass(frozen=True)
class FrozenOp:
    """An in-progress operation pinned in place for a re-plan: it must finish on its
    machine (from the last-applied plan) before that machine takes any new work; its
    owning order's next step waits for it. ``operator`` is the planned operator ("" =
    let staffing pick). ``prev_start`` (last-applied start) orders multiple frozen ops
    on one machine (previous-plan order). ``setup`` = the job was moved to this
    machine since it was last worked (an applied Optimize moved it off a machine that
    went down): a machining job pays its setup again on resume (2026-08-31 rule)."""
    order_key: tuple[str, str]
    op_seq: int
    machine_id: str
    operator: str
    remaining_qty: int
    prev_start: datetime
    setup: bool = False


@dataclass(frozen=True)
class PinnedOp:
    """A published operation held to its machine and its place in the queue (fixed
    plan, 2026-10-06; spec section 8: next ready job in published order).

    ``machine_id`` is where the admin's last applied plan put it; ``prev_start`` is
    when it was due to start there: a job whose order reaches it later than that is
    late, and jobs that can start before it is ready go first.
    Times are never pinned: a repair recomputes them from the punches. ``operator``
    is preferred while they may still man the machine; otherwise whoever qualified
    is free takes it (people are swapped, never machines)."""
    order_key: tuple[str, str]
    op_seq: int
    machine_id: str
    operator: str
    prev_start: datetime
    # The order the published plan PLACED this op in (None = unknown, e.g. a plan
    # published before this field existed). A repair places jobs in this order:
    # people are booked first come at placement time, so any other order hands one
    # job's person to another and the whole plan drifts.
    rank: int | None = None
    # (start, end, person) stretches the published plan gave this op: in each
    # window the person who ran it then is preferred (empty = ``operator``).
    staff: tuple = ()


@dataclass(frozen=True)
class Schedule:
    """The full schedule for a set of orders.

    Attributes:
        segments:     All segments, in the order they were placed.
        completion:   Map of order key → the datetime that order finishes (its
                      DISPATCH time, i.e. when the whole order is done). This is what
                      lateness is measured against.
    """

    segments: tuple[Segment, ...] = field(default_factory=tuple)
    completion: dict[tuple[str, str], datetime] = field(default_factory=dict)

    def makespan_end(self) -> datetime | None:
        """The latest completion across all orders (the plan's finish datetime)."""
        if not self.completion:
            return None
        return max(self.completion.values())
