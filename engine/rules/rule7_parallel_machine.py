"""Rule 7 — Parallel machine for large batches (calc helper, nested in Rule 6).

If batch size > parallel_trigger_qty (default 400), a large CNC batch should run
on a SEPARATE preferred machine for the next CNC setup so it runs in parallel
rather than queueing behind itself on a single machine.

``should_parallelize`` reports whether the trigger fires. ``pick_parallel_machine``
chooses an alternate machine of the same family (e.g. another free CNC) when one
is available, so the next CNC process doesn't pile onto the busiest machine.
"""
from __future__ import annotations


def should_parallelize(qty, config=None) -> bool:
    trigger = config.parallel_trigger_qty if config else 400
    return (qty or 0) > trigger


def pick_parallel_machine(primary_id, candidate_ids, machine_free):
    """Return the most free alternate among ``candidate_ids`` (excluding the
    primary). Falls back to the primary if no alternate exists."""
    alternates = [c for c in candidate_ids if c != primary_id]
    if not alternates:
        return primary_id
    return min(alternates, key=lambda m: machine_free.get(m, None) or 0)


def run(item, config=None, notes=None, masters=None, **kw):
    """Test/trace shim: ``item`` is a dict with qty."""
    notes = notes if notes is not None else []
    fires = should_parallelize(item.get("qty"), config)
    trigger = config.parallel_trigger_qty if config else 400
    notes.append(
        f"qty={item.get('qty')} {'>' if fires else '<='} trigger {trigger}: "
        f"{'allot parallel machine' if fires else 'no parallel machine'}"
    )
    return {"parallelize": fires}
