"""The pipeline + trace backbone — the engine's debug spine (Design spec §5).

``run_rule`` wraps every rule call and snapshots its input table, output table,
config, and decision notes into a trace dict. The per-rule frontend tabs render
that trace, so rules themselves need no UI code.

A rule that detects a contract violation raises ``RuleError``; the pipeline
records it in that rule's trace entry and STOPS the chain (fail loud, fail
localized). Downstream rules are marked "not reached".
"""
from __future__ import annotations

from datetime import date, datetime

from .config import Config
from .models import Masters, PlanRun
from .rules import (
    rule1_consolidate,
    rule2_sort_by_date,
    rule3_tiebreak_process_time,
    rule6_allocate,
)


class RuleError(Exception):
    """A typed, localized rule-contract violation (Design spec §8)."""

    def __init__(self, rule: str, record_id: str, message: str):
        self.rule = rule
        self.record_id = record_id
        self.message = message
        super().__init__(f"[{rule}] record {record_id}: {message}")


# --------------------------------------------------------------------------- #
# Trace serialization
# --------------------------------------------------------------------------- #
def _cell(value):
    if isinstance(value, datetime):
        return value.strftime("%d-%m-%Y %H:%M")     # DD-MM-YYYY HH:MM (app-wide)
    if isinstance(value, date):
        return value.strftime("%d-%m-%Y")           # DD-MM-YYYY
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return value


def to_table(data):
    """Turn a model / list of models / list of dicts into ``{columns, rows}``.

    Every model exposes ``as_row()``; we union the keys across rows to form the
    column list so sparse rows still line up.
    """
    if data is None:
        return {"columns": [], "rows": []}

    items = data if isinstance(data, (list, tuple)) else [data]

    rows = []
    columns: list[str] = []
    seen = set()
    for item in items:
        if hasattr(item, "as_row"):
            row = item.as_row()
        elif isinstance(item, dict):
            row = item
        else:
            row = {"value": item}
        for k in row.keys():
            if k not in seen:
                seen.add(k)
                columns.append(k)
        rows.append(row)

    norm_rows = [[_cell(row.get(c)) for c in columns] for row in rows]
    return {"columns": columns, "rows": norm_rows}


# --------------------------------------------------------------------------- #
# Rule wrapper
# --------------------------------------------------------------------------- #
def run_rule(trace: dict, name: str, fn, input_data, *, config=None, **kw):
    """Run one rule, snapshotting in/out/config/notes into ``trace[name]``.

    The rule may append human-readable strings to a ``notes`` list it receives
    via kw. On RuleError the entry records the error and the exception is
    re-raised so the pipeline can stop the chain.
    """
    notes: list[str] = []
    entry = {
        "input": to_table(input_data),
        "output": {"columns": [], "rows": []},
        "config": config.to_dict() if isinstance(config, Config) else config,
        "notes": notes,
        "error": None,
        "reached": True,
    }
    trace[name] = entry
    try:
        output = fn(input_data, config=config, notes=notes, **kw)
    except RuleError as err:
        entry["error"] = {
            "rule": err.rule,
            "record_id": err.record_id,
            "message": err.message,
        }
        raise
    entry["output"] = to_table(output)
    return output


# --------------------------------------------------------------------------- #
# Forward chain
# --------------------------------------------------------------------------- #
RULE_NAMES = [
    "rule1", "rule2", "rule3", "rule4", "rule5", "rule6", "rule7", "rule8",
]


def _mark_not_reached(trace: dict, from_index: int):
    for nm in RULE_NAMES[from_index:]:
        if nm not in trace:
            trace[nm] = {
                "input": {"columns": [], "rows": []},
                "output": {"columns": [], "rows": []},
                "config": None,
                "notes": [],
                "error": None,
                "reached": False,
            }


def run_forward(plan_run: PlanRun, config: Config, masters: Masters,
                machine_lost_min: dict | None = None) -> dict:
    """Run the forward planning chain 1 → 2 → 3 → 6, returning the trace.

    Rules 4/5 are consumed inside Rule 6 (their effect is logged in rule6's
    notes); they are surfaced as helper trace entries by the API layer if needed.

    ``machine_lost_min`` (optional) maps machine id → working minutes lost to
    recorded downtime/setup overrun; Rule 6 seeds it into machine availability so
    lost time loops back into the plan. ``None`` → no effect (default).
    """
    config.validate()
    trace: dict = {}

    try:
        plan_run.batches = run_rule(
            trace, "rule1", rule1_consolidate.run, plan_run.so_lines,
            config=config, masters=masters,
        )
        plan_run.batches_sorted = run_rule(
            trace, "rule2", rule2_sort_by_date.run, plan_run.batches,
            config=config, masters=masters,
        )
        plan_run.batches_prioritized = run_rule(
            trace, "rule3", rule3_tiebreak_process_time.run, plan_run.batches_sorted,
            config=config, masters=masters,
        )
        plan_run.schedule = run_rule(
            trace, "rule6", rule6_allocate.run, plan_run.batches_prioritized,
            config=config, masters=masters, machine_lost_min=machine_lost_min,
        )
    except RuleError:
        # The failing rule's entry already holds the error; mark the rest unreached.
        failed_at = max(
            (i for i, nm in enumerate(RULE_NAMES) if nm in trace and trace[nm]["error"]),
            default=0,
        )
        _mark_not_reached(trace, failed_at + 1)
        return trace

    _mark_not_reached(trace, len(RULE_NAMES))  # fill rule4/5/7/8 placeholders
    return trace
