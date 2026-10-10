"""Datos de demostración reproducibles: dos usuarios y algunas citas futuras."""
from datetime import date, datetime, time, timedelta

from app.agenda import TZ, Agenda

USERS = {"ana": "Ana Pérez", "luis": "Luis Gómez"}


def next_weekday(today: date, weekday: int) -> date:
    """Próximo lunes(0)…viernes(4) estrictamente posterior a hoy."""
    days = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=days)


def seed(agenda: Agenda) -> None:
    for user_id, name in USERS.items():
        agenda.ensure_user(user_id, name)
    if agenda.list_appointments("ana") or agenda.list_appointments("luis"):
        return                                         # ya sembrado: no duplicamos
    today = agenda.now_fn().date()
    monday, tuesday = next_weekday(today, 0), next_weekday(today, 1)
    demo = [("ana", monday, time(10, 0)), ("luis", monday, time(11, 0)), ("luis", tuesday, time(9, 0))]
    for user_id, day, hour in demo:
        start = datetime.combine(day, hour, tzinfo=TZ)
        agenda.create(user_id, start, idempotency_key=f"seed-{user_id}-{start.isoformat()}")