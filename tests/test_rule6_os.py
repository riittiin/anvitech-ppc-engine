"""Rule 6 — OS (outsourcing) steps reserve their cycle-time as a continuous block."""
from datetime import date, datetime, timedelta

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _P(seq, name, cyc, sug=None, allot=None):
    return Process(seq=seq, name=name, cycle_time=cyc, total_time=None,
                   suggested_machine=sug, allotted_machine=allot)


def test_is_os_detects_allotted_os():
    assert rule6_allocate._is_os(_P(1, "CNC OS", 7200, sug=None, allot="OS"))


def test_is_os_name_only_when_no_real_machine():
    # name has 'OS' and no machine -> OS
    assert rule6_allocate._is_os(_P(1, "BANDSAW OS", None, sug=None, allot=None))
    # name has 'OS' BUT a real machine is assigned -> NOT OS (the sample's 'CNC OS')
    assert not rule6_allocate._is_os(_P(1, "CNC OS", 5, sug="CNC1/CNC2", allot=None))
    # ordinary step -> NOT OS
    assert not rule6_allocate._is_os(_P(1, "BANDSAW", 3, sug="BS1", allot=None))
