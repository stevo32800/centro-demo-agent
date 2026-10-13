"""Flujo de extremo a extremo por la API, con modelos FALSOS (dobles de prueba) y demora corta."""
import asyncio
import re
import time
from contextlib import asynccontextmanager

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from tests.conftest import FIXED_NOW, agenda_rules, main_rules, make_rules_model

pytestmark = pytest.mark.asyncio
DELAY = 1.0                                   # 15 s en producción; 1 s en tests


@asynccontextmanager
async def client(tmp_path, **overrides):
    settings = Settings(db_path=str(tmp_path / "api.db"), availability_delay_s=DELAY,
                        groq_api_key="", **overrides)
    app = create_app(settings, llm_main=make_rules_model(main_rules),
                     llm_agenda=make_rules_model(agenda_rules), now_fn=lambda: FIXED_NOW)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def say(c, user, message, conv=None):
    r = await c.post("/chat", json={"conversation_id": conv, "message": message},
                     headers={"X-User-Id": user})
    assert r.status_code == 200, r.text
    return r.json()


async def wait_events(c, user, conv, until="task_completed", timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = (await c.get(f"/conversations/{conv}/events", headers={"X-User-Id": user})).json()
        if any(e["type"] == until for e in events):
            return events
        await asyncio.sleep(0.1)
    raise AssertionError(f"no llegó {until}")


async def test_health(tmp_path):
    async with client(tmp_path) as c:
        assert (await c.get("/health")).json()["status"] == "ok"


async def test_conversation_stays_available_during_slow_lookup(tmp_path):
    """La secuencia de la sección 4 del enunciado."""
    async with client(tmp_path) as c:
        r1 = await say(c, "ana", "disponibilidad 2026-10-15")
        conv, task_id = r1["conversation_id"], r1["started_tasks"][0]

        t0 = time.monotonic()
        r2 = await say(c, "ana", "¿Cuál es el horario?", conv)
        assert time.monotonic() - t0 < DELAY / 2                  # no esperó a la agenda
        assert "09:00" in r2["reply"]
        task = (await c.get(f"/tasks/{task_id}", headers={"X-User-Id": "ana"})).json()
        assert task["status"] == "running"

        events = await wait_events(c, "ana", conv)                 # llega solo, sin volver a pedirlo
        assert "16:30" in events[-1]["message"]


async def test_confirmation_is_enforced_in_code(tmp_path):
    async with client(tmp_path) as c:
        # Un LLM 'impaciente' intenta ejecutar en el mismo turno -> el código lo impide.
        r = await say(c, "ana", "impaciente 2026-10-15T10:00")
        events = await wait_events(c, "ana", r["conversation_id"])
        assert "CONFIRMATION_REQUIRED" in events[-1]["message"]
        starts = [a["start"] for a in (await c.get("/appointments", headers={"X-User-Id": "ana"})).json()]
        assert "2026-10-15T10:00:00-05:00" not in starts


async def test_booking_with_confirmation_and_repeated_confirmation(tmp_path):
    async with client(tmp_path) as c:
        r = await say(c, "ana", "reservar 2026-10-15T10:00")
        conv = r["conversation_id"]
        events = await wait_events(c, "ana", conv)
        op_id = re.search(r"op_[0-9a-f]{8}", events[-1]["message"]).group()

        for _ in range(2):                                         # confirmación repetida
            r = await say(c, "ana", f"confirma {op_id}", conv)
            await wait_events(c, "ana", conv)
            await asyncio.sleep(0.2)
        starts = [a["start"] for a in (await c.get("/appointments", headers={"X-User-Id": "ana"})).json()]
        assert starts.count("2026-10-15T10:00:00-05:00") == 1      # estado REAL de la agenda


async def test_identity_and_isolation(tmp_path):
    async with client(tmp_path) as c:
        assert (await c.post("/chat", json={"message": "hola"}, headers={"X-User-Id": "mallory"})).status_code == 401
        r = await say(c, "ana", "disponibilidad 2026-10-15")
        conv, task_id = r["conversation_id"], r["started_tasks"][0]
        assert (await c.get(f"/conversations/{conv}/events", headers={"X-User-Id": "luis"})).status_code == 404
        assert (await c.get(f"/tasks/{task_id}", headers={"X-User-Id": "luis"})).status_code == 404
        assert (await c.post("/chat", json={"conversation_id": conv, "message": "hola"},
                             headers={"X-User-Id": "luis"})).status_code == 404


async def test_two_conversations_progress_in_parallel(tmp_path):
    async with client(tmp_path) as c:
        t0 = time.monotonic()
        ra, rl = await asyncio.gather(say(c, "ana", "disponibilidad 2026-10-15"),
                                      say(c, "luis", "disponibilidad 2026-10-16"))
        await asyncio.gather(wait_events(c, "ana", ra["conversation_id"]),
                             wait_events(c, "luis", rl["conversation_id"]))
        assert time.monotonic() - t0 < DELAY * 1.8                 # en paralelo, no en serie


async def test_tool_timeout_is_reported(tmp_path):
    async with client(tmp_path, tool_timeout_s=0.2) as c:
        r = await say(c, "ana", "disponibilidad 2026-10-15")
        events = await wait_events(c, "ana", r["conversation_id"])
        assert "TIMEOUT" in events[-1]["message"]


async def test_change_date_while_searching(tmp_path):
    """Extra: la búsqueda vieja se cancela y su resultado nunca se presenta."""
    async with client(tmp_path) as c:
        r1 = await say(c, "ana", "disponibilidad 2026-10-15")
        conv = r1["conversation_id"]
        r2 = await say(c, "ana", "disponibilidad 2026-10-16", conv)
        events = await wait_events(c, "ana", conv)
        await asyncio.sleep(DELAY)
        events = (await c.get(f"/conversations/{conv}/events", headers={"X-User-Id": "ana"})).json()
        completed = [e for e in events if e["type"] == "task_completed"]
        assert [e["task_id"] for e in completed] == r2["started_tasks"]
        assert any(e["type"] == "task_cancelled" and e["task_id"] == r1["started_tasks"][0] for e in events)


async def test_explicit_cancel(tmp_path):
    async with client(tmp_path) as c:
        r = await say(c, "ana", "disponibilidad 2026-10-15")
        conv, task_id = r["conversation_id"], r["started_tasks"][0]
        assert (await c.delete(f"/tasks/{task_id}", headers={"X-User-Id": "ana"})).json()["status"] == "cancelled"
        await asyncio.sleep(DELAY + 0.3)
        types = [e["type"] for e in (await c.get(f"/conversations/{conv}/events",
                                                   headers={"X-User-Id": "ana"})).json()]
        assert types == ["task_started", "task_cancelled"]