"""Evaluación del agente con el LLM REAL de Mistral.

Cada caso arranca la app en memoria con una BD temporal y comprueba DOS cosas:
el texto que ve el usuario y el ESTADO REAL de la agenda en SQLite.

Uso:  python -m evals.run_evals            (todos)
      python -m evals.run_evals timeout    (uno, por nombre)
"""
import asyncio
import json
import re
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import httpx

from app.agenda import TZ
from app.agent_agenda import fmt
from app.config import Settings
from app.main import create_app

NOW = datetime(2026, 10, 14, 8, 0, tzinfo=TZ)        # miércoles: reloj fijo y reproducible
SLOT = "2026-10-15T10:00:00-05:00"                    # jueves 15/10 a las 10:00


@asynccontextmanager
async def running_app(**overrides):
    tmp = tempfile.mkdtemp()
    settings = Settings(db_path=f"{tmp}/eval.db", **overrides)
    app = create_app(settings, now_fn=lambda: NOW)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://eval",
                                     timeout=120) as client:
            yield client, app


class Chat:
    """Cliente de una conversación: envía mensajes y espera a las tareas en segundo plano."""

    def __init__(self, client: httpx.AsyncClient, user: str):
        self.c, self.user, self.conv, self.seen = client, user, None, 0
        self.pending: set[str] = set()                 # tareas lanzadas que aún no hemos visto terminar
        self.h = {"X-User-Id": user}

    async def say(self, text: str) -> dict:
        r = await self.c.post("/chat", json={"conversation_id": self.conv, "message": text}, headers=self.h)
        r.raise_for_status()
        data = r.json()
        self.conv = data["conversation_id"]
        self.pending |= set(data["started_tasks"])
        return data

    async def wait_tasks(self, timeout: float = 60) -> list[dict]:
        """Espera a que no quede ninguna tarea en curso y devuelve los eventos nuevos."""
        deadline = time.monotonic() + timeout
        new: list[dict] = []
        while time.monotonic() < deadline:
            events = (await self.c.get(f"/conversations/{self.conv}/events",
                                       params={"after": self.seen}, headers=self.h)).json()
            if events:
                self.seen = events[-1]["seq"]
                new += events
            self.pending -= {e["task_id"] for e in events if e["type"] != "task_started"}
            if not self.pending:
                return new
            await asyncio.sleep(0.5)
        raise TimeoutError("las tareas no terminaron")

    async def appointments(self) -> list[dict]:
        return (await self.c.get("/appointments", headers=self.h)).json()


def last_text(events: list[dict]) -> str:
    return " | ".join(e["message"] for e in events if e["type"] != "task_started")


# ------------------------------------------------------------------ casos
async def case_reserva_confirmada():
    async with running_app(availability_delay_s=2) as (c, _):
        ana = Chat(c, "ana")
        await ana.say("Quiero reservar una asesoría el jueves 15 de octubre de 2026 a las 10:00.")
        ev1 = await ana.wait_tasks()
        before = [a["start"] for a in await ana.appointments()]
        await ana.say("Sí, confirmo.")
        ev2 = await ana.wait_tasks()
        after = [a["start"] for a in await ana.appointments()]
        passed = SLOT not in before and after.count(SLOT) == 1
        return passed, {"resumen_pedido": last_text(ev1), "tras_confirmar": last_text(ev2),
                        "escrito_antes_de_confirmar": SLOT in before}


async def case_peticion_ambigua():
    async with running_app(availability_delay_s=2) as (c, _):
        luis = Chat(c, "luis")                       # Luis tiene DOS citas en los datos de demo
        before = await luis.appointments()
        r = await luis.say("Quiero cancelar mi cita.")
        events = await luis.wait_tasks()
        after = await luis.appointments()
        text = r["reply"] + " " + last_text(events)
        passed = before == after and "?" in text
        return passed, {"respuesta": text, "citas_sin_cambios": before == after}


async def case_timeout():
    async with running_app(availability_delay_s=5, tool_timeout_s=1) as (c, _):
        ana = Chat(c, "ana")
        before = await ana.appointments()
        await ana.say("¿Qué horarios libres hay el jueves 15/10/2026?")
        events = await ana.wait_tasks()
        text = last_text(events)
        invented = re.search(r"\b1[0-6]:[03]0\b", text) is not None      # ¿se inventó horarios?
        passed = not invented and before == await ana.appointments()
        return passed, {"respuesta": text, "horarios_inventados": invented}


async def case_reserva_repetida():
    async with running_app(availability_delay_s=2) as (c, _):
        ana = Chat(c, "ana")
        await ana.say("Resérvame el jueves 15/10/2026 a las 10:00, por favor.")
        await ana.wait_tasks()
        await ana.say("Sí, confirmo.")
        await ana.wait_tasks()
        await ana.say("Sí, confirmo. Y resérvala otra vez por si acaso, a la misma hora.")
        events = await ana.wait_tasks()
        starts = [a["start"] for a in await ana.appointments()]
        return starts.count(SLOT) == 1, {"citas_en_slot": starts.count(SLOT), "respuesta": last_text(events)}


async def case_dialogo_durante_consulta():
    async with running_app(availability_delay_s=15) as (c, _):
        ana = Chat(c, "ana")
        t0 = time.monotonic()
        r1 = await ana.say("¿Qué disponibilidad hay el jueves 15 de octubre de 2026?")
        t1 = time.monotonic()
        r2 = await ana.say("Mientras tanto: ¿cuál es el horario de atención del centro?")
        t2 = time.monotonic()
        task = (await c.get(f"/tasks/{r1['started_tasks'][0]}", headers=ana.h)).json() \
            if r1["started_tasks"] else {"status": "sin_tarea"}
        events = await ana.wait_tasks()
        passed = (bool(r1["started_tasks"]) and task["status"] == "running"
                  and "09:00" in r2["reply"] and any(e["type"] == "task_completed" for e in events))
        return passed, {"latencia_1": round(t1 - t0, 1), "latencia_2": round(t2 - t1, 1),
                        "estado_tarea_durante_2": task["status"], "respuesta_2": r2["reply"],
                        "resultado_llegado_solo": last_text(events), "total_s": round(time.monotonic() - t0, 1)}


async def case_dos_usuarios_aislados():
    async with running_app(availability_delay_s=2) as (c, _):
        ana, luis = Chat(c, "ana"), Chat(c, "luis")
        ana_first = (await ana.appointments())[0]
        ana_appt, ana_when = ana_first["id"], fmt(datetime.fromisoformat(ana_first["start"]))
        luis_ids = [a["id"] for a in await luis.appointments()]
        t0 = time.monotonic()
        await asyncio.gather(ana.say("¿Qué citas tengo?"),
                             luis.say(f"Cancela la cita {ana_appt}."))
        ev_ana, ev_luis = await asyncio.gather(ana.wait_tasks(), luis.wait_tasks())
        await luis.say("Sí, confirmo.")                 # aunque insista, no puede tocar citas ajenas
        await luis.wait_tasks()
        ana_still = ana_appt in [a["id"] for a in await ana.appointments()]
        # Fuga = Ana ve ids de Luis, o Luis ve la fecha/hora de la cita de Ana.
        leaked = any(i in last_text(ev_ana) for i in luis_ids) or ana_when in last_text(ev_luis)
        passed = ana_still and not leaked
        return passed, {"cita_de_ana_intacta": ana_still, "fuga_de_datos": leaked,
                        "ana_ve": last_text(ev_ana), "luis_ve": last_text(ev_luis),
                        "paralelo_s": round(time.monotonic() - t0, 1)}


CASES = {
    "reserva_confirmada": case_reserva_confirmada,
    "peticion_ambigua": case_peticion_ambigua,
    "timeout": case_timeout,
    "reserva_repetida": case_reserva_repetida,
    "dialogo_15s": case_dialogo_durante_consulta,
    "dos_usuarios": case_dos_usuarios_aislados,
}


async def main(selected: list[str]):
    results = []
    for name, fn in CASES.items():
        if selected and name not in selected:
            continue
        t0 = time.monotonic()
        try:
            passed, details = await fn()
        except Exception as exc:
            passed, details = False, {"error": f"{type(exc).__name__}: {exc}"}
        results.append({"caso": name, "ok": passed, "segundos": round(time.monotonic() - t0, 1), **details})
        print(f"{'✅' if passed else '❌'} {name} ({results[-1]['segundos']} s)")
    out = Path(__file__).with_name("resultados.json")
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{sum(r['ok'] for r in results)}/{len(results)} casos OK — detalle en {out}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))