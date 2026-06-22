"""Rule 5 — Operation overlap mode (calc helper, consumed inside Rule 6).

Global per-run toggle deciding when the NEXT process may start relative to the
previous one:
  * sequential  — next starts only after the previous is fully complete (100%).
  * overlap     — next starts after overlap_percent (default 50%) of the
                  previous process's occupancy has elapsed.

``elapsed_before_next`` returns how many minutes of the previous process must
pass before the next can begin. Rule 6 then walks that many *working* minutes
forward from the previous process's start on the working calendar.
"""
from __future__ import annotations

from ..config import OVERLAP_SEQUENTIAL


def elapsed_before_next(prev_occupancy_min: float, config=None) -> float:
    if config is None or config.overlap_mode == OVERLAP_SEQUENTIAL:
        return prev_occupancy_min
    return prev_occupancy_min * (config.overlap_percent / 100.0)


def run(item, config=None, notes=None, masters=None, **kw):
    """Test/trace shim: ``item`` is a dict with prev_occupancy_min."""
    notes = notes if notes is not None else []
    prev = item.get("prev_occupancy_min", 0.0)
    offset = elapsed_before_next(prev, config)
    mode = config.overlap_mode if config else "sequential"
    notes.append(
        f"overlap mode='{mode}': next process may start after {offset} of "
        f"{prev} min of the previous process"
    )
    return {"elapsed_before_next_min": offset}
