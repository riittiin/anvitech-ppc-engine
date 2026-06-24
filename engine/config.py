"""Tunable parameters for the PPC engine.

Every configurable knob from RULES.md lives here, with validation. The pipeline
validates the config once at run start (Design spec §7). Rules receive this
object and never read global state.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional


# Overlap modes for Rule 5.
OVERLAP_SEQUENTIAL = "sequential"   # next process starts only after previous fully done
OVERLAP_PERCENT = "overlap"         # next process starts after overlap_percent of previous

# Rule 3 priority metrics (operations-management dispatching rules).
PRIORITY_SLACK = "slack"            # least slack first (time-to-due minus work-needed)
PRIORITY_CRITICAL_RATIO = "critical_ratio"  # lowest critical ratio first (time / work)
PRIORITY_PROCESS_TIME = "process_time"      # legacy: more process time first (exact-date ties)
PRIORITY_METRICS = (PRIORITY_SLACK, PRIORITY_CRITICAL_RATIO, PRIORITY_PROCESS_TIME)


@dataclass
class Config:
    """All tunable params. Defaults match RULES.md "Configurable parameters"."""

    # Rule 1 — consolidation window (days). SO lines for the same item whose
    # delivery dates fall within this many days are merged into one batch.
    consolidation_window_days: int = 10

    # Rule 4 — setup time added to every process's machine occupancy (minutes).
    setup_time_min: int = 90

    # Rule 5 — operation overlap.
    overlap_mode: str = OVERLAP_SEQUENTIAL
    overlap_percent: int = 50  # only used when overlap_mode == OVERLAP_PERCENT

    # Rule 3 — smart priority. The metric folds due date + workload into urgency.
    #   slack          = (working time until SO delivery date) − (work needed)
    #   critical_ratio = (time until due) / (work needed)
    #   process_time   = legacy: more process time first
    # priority_window_days bounds how far apart two SO delivery dates may be for
    # the metric to reorder them; None = no limit (pure metric sort), 0 = only
    # exact-same-date ties (legacy behaviour).
    priority_metric: str = PRIORITY_SLACK
    priority_window_days: Optional[int] = None

    # Rule 6 — where the schedule clock starts. The first process of the
    # highest-priority batch can begin no earlier than this date (08:00, first
    # shift). Kept configurable + explicit so runs are reproducible.
    plan_start_date: date = date(2025, 3, 1)

    # Shift windows (24h clock). 1st shift 08:00-19:00, 2nd 19:00-05:00 (next day).
    first_shift_start_hour: int = 8
    second_shift_end_hour: int = 5  # on the following calendar day

    def validate(self) -> None:
        """Fail loud on a bad config (Design spec §8 — config validation)."""
        errs = []
        if self.consolidation_window_days < 0:
            errs.append("consolidation_window_days must be >= 0")
        if self.setup_time_min < 0:
            errs.append("setup_time_min must be >= 0")
        if self.overlap_mode not in (OVERLAP_SEQUENTIAL, OVERLAP_PERCENT):
            errs.append(
                f"overlap_mode must be '{OVERLAP_SEQUENTIAL}' or '{OVERLAP_PERCENT}'"
            )
        if not (0 <= self.overlap_percent <= 100):
            errs.append("overlap_percent must be within 0..100")
        if self.priority_metric not in PRIORITY_METRICS:
            errs.append(f"priority_metric must be one of {PRIORITY_METRICS}")
        if self.priority_window_days is not None and self.priority_window_days < 0:
            errs.append("priority_window_days must be >= 0 or null (no limit)")
        if not (0 <= self.first_shift_start_hour <= 23):
            errs.append("first_shift_start_hour must be within 0..23")
        if not (0 <= self.second_shift_end_hour <= 23):
            errs.append("second_shift_end_hour must be within 0..23")
        if errs:
            raise ValueError("Invalid config: " + "; ".join(errs))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["plan_start_date"] = self.plan_start_date.isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> "Config":
        """Build a Config from a (possibly partial) dict, e.g. API query params."""
        cfg = cls()
        if not data:
            return cfg
        for key, value in data.items():
            if not hasattr(cfg, key):
                continue
            if key == "plan_start_date" and isinstance(value, str):
                value = date.fromisoformat(value)
            if key == "priority_window_days":
                # "" / "none" / null from the UI means "no limit".
                if value in (None, "", "none", "null"):
                    value = None
                else:
                    value = int(value)
            setattr(cfg, key, value)
        return cfg
