"""Which shifts a station runs is decided by its master hours, never by its KIND.

Owner correction, 2026-09-05: "all the non-machining activities shouldn't happen at
night" was never Anvitech's rule. Deburring, drilling, the centre lathe and packing DO
run at night whenever somebody qualified is rostered on that shift. The shift is gated
by STAFFING (``staffing.candidate_operator`` returns None when nobody qualified is on),
not by the kind of work.

The old rule was ``runs_second_shift = (kind == MACHINING)``. It silently erased anyone
rostered on nights for non-machining work: on the live book one such operator (13
stations, second shift) was given ZERO work, the three stations he covers starved for
393 hours, and 57 late-days were attributable to this rule alone.
"""
import pytest

from engine.models import Machine as AppMachine
from ppc_engine.domain.resources import Machine, MachineKind

KINDS = (MachineKind.MACHINING, MachineKind.MANUAL, MachineKind.INSPECTION)


def _m(kind, hrs):
    return Machine(id="X", type_text="t", kind=kind, available_hrs_per_day=hrs)


@pytest.mark.parametrize("kind", KINDS)
def test_a_station_with_two_shift_hours_runs_at_night_whatever_its_kind(kind):
    """THE FIX. A manual/inspection station on 19.5 h/day must run the night shift —
    under the old rule only MACHINING ever could."""
    assert _m(kind, 19.5).runs_second_shift is True


@pytest.mark.parametrize("kind", KINDS)
def test_a_station_with_single_shift_hours_does_not(kind):
    assert _m(kind, 9.5).runs_second_shift is False


def test_kind_does_not_influence_the_shift_decision_at_all():
    """The regression guard: the answer must depend ONLY on the hours. If someone
    reintroduces a kind-based rule, these two sets stop being equal."""
    for hrs in (19.5, 12.0, 11.9, 9.5, 0.0, None):
        answers = {_m(k, hrs).runs_second_shift for k in KINDS}
        assert len(answers) == 1, f"kind changed the answer at {hrs} h/day"


@pytest.mark.parametrize("hrs", [19.5, 12.0, 11.9, 9.5, 0.0, None])
@pytest.mark.parametrize("kind", KINDS)
def test_engine_and_app_agree_on_every_case(kind, hrs):
    """The engine schedules with ppc_engine's rule; Analytics and the delay
    justification report measure capacity with the app's. Before 2026-09-05 those were
    two DIFFERENT rules that happened to give the same answer for the current master —
    exactly the divergence class that cost 158 hours of mis-reported working time in
    2026-08-07. They must now agree by construction, not by coincidence."""
    app = AppMachine(machine_no="X", display_name="X", machine_type="t",
                     available_hrs_per_day=hrs)
    assert _m(kind, hrs).runs_second_shift is app.is_two_shift(12.0)


def test_the_two_thresholds_are_the_same_number():
    """ppc's threshold is duplicated (a dataclass property cannot read Config), so it
    must stay equal to the app default it mirrors."""
    import inspect
    sig = inspect.signature(AppMachine.is_two_shift)
    assert Machine._TWO_SHIFT_THRESHOLD_HRS == sig.parameters["threshold"].default


def test_setup_is_still_a_machining_only_concept():
    """Guard against over-correcting: the 90-min setup IS kind-based and must stay so.
    Only the SHIFT rule was wrong."""
    assert _m(MachineKind.MACHINING, 9.5).needs_setup is True
    assert _m(MachineKind.MANUAL, 19.5).needs_setup is False
    assert _m(MachineKind.INSPECTION, 19.5).needs_setup is False
