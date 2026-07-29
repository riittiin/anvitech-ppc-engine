"""Pure freeze logic (reporting/derivation only — never mutates a plan).

Two pure functions:
  - ``schedule_projection(schedule)`` — the applied plan's per-op assignment, the durable
    record of "the plan the floor is following" (machine + operator + time per op).
  - ``compute_frozen_set(applied_rows, actuals, so_lines, masters, config)`` — from that
    record + the punches, the in-progress ops to FREEZE (machine/operator from the plan,
    remaining qty from the punches). See the 2026-07-29 spec.
"""
from __future__ import annotations

_OS_LANES = {"OS / Outsourced", "Off-machine"}


def schedule_projection(schedule) -> list[dict]:
    """One row per real (machine) operation in the applied plan. OS/off-lane entries are
    skipped (no in-house machine to pin)."""
    rows = []
    for e in schedule:
        if e.machine in _OS_LANES:
            continue
        rows.append({
            "batch_id": e.batch_id,
            "item_code": e.item_code,
            "process_seq": e.process_seq,
            "process_name": e.process_name,
            "machine": e.machine,
            "operator": e.operator or "",
            "start": e.start.isoformat(timespec="seconds"),
            "end": e.end.isoformat(timespec="seconds"),
            "so_refs": list(e.so_refs or []),
        })
    return rows
