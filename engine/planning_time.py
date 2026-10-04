"""Planning cycle time: the ONE place the CNC/VMC planning allowance lives.

Owner's rule (2026-10-04): the Item's process Master holds the ORIGINAL cycle times
and is never changed. Production PLANNING schedules every CNC/VMC (machining) step at
cycle x ``CNC_VMC_PLANNING_FACTOR``. Production analysis, operator efficiency and the
master as displayed read the Excel value unchanged. Manual, inspection, outsourced and
dispatch steps are never padded.

"CNC/VMC step" is ``OperationKind.MACHINING`` exactly as the planner classifies it
(``ppc_engine.loaders.normalize.classify_operation``), decided on the ORIGINAL number,
so the allowance can never turn a step into an outsourced one or the reverse.
"""

from __future__ import annotations

from dataclasses import replace

from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders.normalize import classify_operation

CNC_VMC_PLANNING_FACTOR = 1.30


def pad_routings(routings: dict) -> dict:
    """New-engine routings with every MACHINING op's cycle x the factor. Pure: the
    input routings are never mutated."""
    out = {}
    for code, routing in routings.items():
        ops = tuple(replace(op, cycle_min=op.cycle_min * CNC_VMC_PLANNING_FACTOR)
                    if op.kind == OperationKind.MACHINING else op
                    for op in routing.operations)
        out[code] = replace(routing, operations=ops)
    return out


def planning_cycle_time(proc, config):
    """A classic-loader ``Process``'s cycle time as the PLANNER uses it.

    Padded only for the new engine (``config.scheduler == "new"``, what production
    runs); the retired classic/flow engines and a call with no config read the Excel
    value, so their plans and the golden trace are unchanged."""
    ct = proc.cycle_time
    if not isinstance(ct, (int, float)) or getattr(config, "scheduler", None) != "new":
        return ct
    kind = classify_operation(proc.name, proc.suggested_machine, proc.allotted_machine, ct)
    return ct * CNC_VMC_PLANNING_FACTOR if kind == OperationKind.MACHINING else ct
