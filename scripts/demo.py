"""Demostración reproducible de la sección 4: la conversación sigue disponible
mientras el subagente consulta la agenda (15 s).

Requiere el servidor en marcha (uvicorn o Docker).  Uso:
    python scripts/demo.py [--url http://127.0.0.1:8000] [--user ana]
"""
import argparse
import time
from datetime import date, timedelta

import httpx

T0 = time.monotonic()


def log(who: str, text: str) -> None:
    print(f"[{time.monotonic() - T0:5.1f}s] {who:<9} {text}")


def next_business_day(today: date) -> date:
    d = today + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--user", default="ana")
    args = parser.parse_args()
    headers = {"X-User-Id": args.user}
    day = next_business_day(date.today())

    with httpx.Client(base_url=args.url, headers=headers, timeout=60) as client:
        # 1. El usuario pide disponibilidad para una fecha futura concreta.
        msg = f"¿Qué horarios tenéis libres el {day:%d/%m/%Y}?"
        log("usuario", msg)
        r = client.post("/chat", json={"message": msg}).json()
        conv, tasks = r["conversation_id"], r["started_tasks"]
        # 2. El agente principal lanza la tarea y comunica que está consultando.
        log("asistente", f"{r['reply']}   (tareas: {tasks})")

        # 3. ANTES de que termine, el usuario pregunta otra cosa y recibe respuesta sin esperar.
        msg = "Mientras tanto, ¿cuál es el horario de atención?"
        log("usuario", msg)
        r = client.post("/chat", json={"conversation_id": conv, "message": msg}).json()
        log("asistente", r["reply"])
        for task_id in tasks:
            log("sistema", f"estado de {task_id}: {client.get(f'/tasks/{task_id}').json()['status']}")

        # 4. El resultado llega solo: polling automático de eventos, sin volver a pedirlo.
        seen, pending = 0, set(tasks)
        while pending:
            for event in client.get(f"/conversations/{conv}/events", params={"after": seen}).json():
                seen = event["seq"]
                if event["type"] != "task_started":
                    pending.discard(event["task_id"])
                    log("evento", f"{event['type']}: {event['message']}")
            time.sleep(1)
    log("fin", "La conversación nunca se bloqueó durante la consulta.")


if __name__ == "__main__":
    main()