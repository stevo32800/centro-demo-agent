"""Reglas de negocio de la agenda: código puro, sin LLM."""
import threading
from datetime import date, datetime

import pytest

from app.agenda import TZ, AgendaError


def dt(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


@pytest.mark.parametrize("start, code", [
    (dt(17, 10), "CLOSED_DAY"),          # sábado
    (dt(15, 8, 30), "OUTSIDE_HOURS"),    # antes de abrir
    (dt(15, 17, 0), "OUTSIDE_HOURS"),    # 17:00 ya no es un inicio válido
    (dt(15, 10, 15), "NOT_ALIGNED"),
    (dt(13, 10), "PAST"),                # ayer
])
def test_rejects_invalid_slots(agenda, start, code):
    with pytest.raises(AgendaError) as exc:
        agenda.validate_slot(start)
    assert exc.value.code == code


def test_last_slot_is_1630(agenda):
    assert agenda.validate_slot(dt(15, 16, 30)) == dt(15, 16, 30)


def test_naive_datetime_is_business_time(agenda):
    assert agenda.validate_slot(datetime(2026, 10, 15, 9, 0)).tzinfo is not None


def test_available_slots_excludes_taken_and_past(agenda):
    assert len(agenda.available_slots(date(2026, 10, 15))) == 16       # 09:00 … 16:30
    agenda.create("ana", dt(15, 9), "k1")
    slots = agenda.available_slots(date(2026, 10, 15))
    assert dt(15, 9) not in slots and len(slots) == 15
    assert agenda.available_slots(date(2026, 10, 17)) == []             # sábado


def test_no_double_booking(agenda):
    agenda.create("ana", dt(15, 10), "k1")
    with pytest.raises(AgendaError) as exc:
        agenda.create("luis", dt(15, 10), "k2")
    assert exc.value.code == "SLOT_TAKEN"


def test_idempotent_create(agenda):
    first = agenda.create("ana", dt(15, 10), "op_same")
    again = agenda.create("ana", dt(15, 10), "op_same")                  # reintento
    assert first.id == again.id
    assert len(agenda.list_appointments("ana")) == 1


def test_users_only_touch_their_own(agenda):
    appt = agenda.create("ana", dt(15, 10), "k1")
    assert agenda.list_appointments("luis") == []
    for action in (lambda: agenda.get_own("luis", appt.id), lambda: agenda.cancel("luis", appt.id)):
        with pytest.raises(AgendaError) as exc:
            action()
        assert exc.value.code == "NOT_FOUND"
    assert agenda.list_appointments("ana")[0].status == "active"


def test_reschedule_frees_old_slot(agenda):
    appt = agenda.create("ana", dt(15, 10), "k1")
    new = agenda.reschedule("ana", appt.id, dt(15, 11), "k2")
    assert [a.start for a in agenda.list_appointments("ana")] == [new.start]
    assert agenda.is_free(dt(15, 10))


def test_cancel_frees_slot(agenda):
    appt = agenda.create("ana", dt(15, 10), "k1")
    agenda.cancel("ana", appt.id)
    assert agenda.is_free(dt(15, 10))
    agenda.create("luis", dt(15, 10), "k2")                              # se puede volver a reservar


def test_concurrent_booking_same_slot(agenda):
    """Extra: dos usuarios reservan a la vez el mismo horario -> solo uno lo consigue."""
    results, barrier = [], threading.Barrier(2)

    def book(user):
        barrier.wait()
        try:
            agenda.create(user, dt(16, 9), f"k-{user}")
            results.append("ok")
        except AgendaError as e:
            results.append(e.code)

    threads = [threading.Thread(target=book, args=(u,)) for u in ("ana", "luis")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["SLOT_TAKEN", "ok"]