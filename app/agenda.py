"""Reglas de negocio y persistencia de la agenda (SQLite).
"""

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Guayaquil")
SLOT_MINUTES = 30
OPEN_TIME = time(9, 0)
LAST_START = time(16, 30)          # la última cita empieza a las 16:30 y termina a las 17:00


class AgendaError(Exception):
    """Error de negocio con un código estable que el agente puede interpretar."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Appointment:
    id: str
    user_id: str
    start: datetime
    status: str

    def to_dict(self) -> dict:
        return {"id": self.id, "start": self.start.isoformat(), "status": self.status}


def now_local() -> datetime:
    return datetime.now(TZ)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS appointments (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id),
    start           TEXT NOT NULL,              -- ISO 8601 con zona America/Guayaquil
    status          TEXT NOT NULL DEFAULT 'active',
    idempotency_key TEXT UNIQUE,                -- evita reservas duplicadas por reintentos
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_slot
    ON appointments(start) WHERE status = 'active';
"""


class Agenda:
    def __init__(self, db_path: str, now_fn: Callable[[], datetime] = now_local):
        self.db_path = db_path
        self.now_fn = now_fn          # inyectable para tests reproducibles
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        """Una conexión por operación: simple y segura con hilos (asyncio.to_thread).
        Todo lo que se ejecuta dentro del bloque es UNA transacción: commit o rollback."""
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ reglas
    def validate_slot(self, start: datetime) -> datetime:
        """Lanza AgendaError si la hora no es reservable. Devuelve la hora normalizada."""
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)          # asumimos hora local del negocio
        start = start.astimezone(TZ)
        if start.weekday() >= 5:
            raise AgendaError("CLOSED_DAY", "Solo atendemos de lunes a viernes.")
        if start.minute not in (0, 30) or start.second or start.microsecond:
            raise AgendaError("NOT_ALIGNED", "Las citas empiezan en punto o a y media.")
        if not (OPEN_TIME <= start.time() <= LAST_START):
            raise AgendaError("OUTSIDE_HOURS", "Horario: 09:00 a 17:00; la última cita empieza a las 16:30.")
        if start <= self.now_fn():
            raise AgendaError("PAST", "No se pueden reservar citas en el pasado.")
        return start

    def _slots_of_day(self, day: date) -> list[datetime]:
        first = datetime.combine(day, OPEN_TIME, tzinfo=TZ)
        last = datetime.combine(day, LAST_START, tzinfo=TZ)
        slots, current = [], first
        while current <= last:
            slots.append(current)
            current += timedelta(minutes=SLOT_MINUTES)
        return slots

    # --------------------------------------------------------------- consultas
    def ensure_user(self, user_id: str, name: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO users(id, name) VALUES (?, ?)", (user_id, name))

    def user_exists(self, user_id: str) -> bool:
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is not None

    def available_slots(self, day: date) -> list[datetime]:
        if day.weekday() >= 5:
            return []
        with self._conn() as conn:
            taken = {row["start"] for row in conn.execute(
                "SELECT start FROM appointments WHERE status = 'active' AND substr(start, 1, 10) = ?",
                (day.isoformat(),))}
        now = self.now_fn()
        return [s for s in self._slots_of_day(day) if s > now and s.isoformat() not in taken]

    def is_free(self, start: datetime) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM appointments WHERE status = 'active' AND start = ?",
                               (start.isoformat(),)).fetchone()
        return row is None

    def list_appointments(self, user_id: str, include_past: bool = False) -> list[Appointment]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM appointments WHERE user_id = ? AND status = 'active' ORDER BY start",
                (user_id,)).fetchall()
        appts = [self._row(r) for r in rows]
        if not include_past:
            appts = [a for a in appts if a.start > self.now_fn()]
        return appts

    def get_own(self, user_id: str, appointment_id: str) -> Appointment:
        """Devuelve la cita SOLO si pertenece al usuario. Si no, 'no encontrada' (no filtramos datos ajenos)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM appointments WHERE id = ? AND user_id = ? AND status = 'active'",
                (appointment_id, user_id)).fetchone()
        if row is None:
            raise AgendaError("NOT_FOUND", "No existe una cita activa con ese identificador para este usuario.")
        return self._row(row)

    # --------------------------------------------------------------- escrituras
    def create(self, user_id: str, start: datetime, idempotency_key: str) -> Appointment:
        start = self.validate_slot(start)
        appt_id = "c_" + uuid.uuid4().hex[:8]
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO appointments(id, user_id, start, status, idempotency_key, created_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?)",
                    (appt_id, user_id, start.isoformat(), idempotency_key, self.now_fn().isoformat()))
        except sqlite3.IntegrityError:
            existing = self._by_key(idempotency_key)
            if existing is not None:          # misma operación repetida -> devolvemos la misma cita
                return existing
            raise AgendaError("SLOT_TAKEN", "Ese horario ya está ocupado.")
        return Appointment(appt_id, user_id, start, "active")

    def reschedule(self, user_id: str, appointment_id: str, new_start: datetime,
                   idempotency_key: str) -> Appointment:
        new_start = self.validate_slot(new_start)
        previous = self._by_key(idempotency_key)
        if previous is not None:
            return previous
        current = self.get_own(user_id, appointment_id)
        new_id = "c_" + uuid.uuid4().hex[:8]
        try:
            with self._conn() as conn:   # transacción: cancelar la antigua + crear la nueva, o nada
                conn.execute("UPDATE appointments SET status = 'rescheduled' WHERE id = ?", (current.id,))
                conn.execute(
                    "INSERT INTO appointments(id, user_id, start, status, idempotency_key, created_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?)",
                    (new_id, user_id, new_start.isoformat(), idempotency_key, self.now_fn().isoformat()))
        except sqlite3.IntegrityError:
            raise AgendaError("SLOT_TAKEN", "El nuevo horario ya está ocupado.")
        return Appointment(new_id, user_id, new_start, "active")

    def cancel(self, user_id: str, appointment_id: str) -> Appointment:
        appt = self.get_own(user_id, appointment_id)
        with self._conn() as conn:
            conn.execute("UPDATE appointments SET status = 'cancelled' WHERE id = ?", (appt.id,))
        appt.status = "cancelled"
        return appt

    # ----------------------------------------------------------------- helpers
    def _by_key(self, key: str) -> Appointment | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM appointments WHERE idempotency_key = ?", (key,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row: sqlite3.Row) -> Appointment:
        return Appointment(row["id"], row["user_id"], datetime.fromisoformat(row["start"]), row["status"])