"""Tareas en segundo plano (asyncio) y eventos por conversación.

Todo vive en memoria: si el proceso se reinicia, se pierden las tareas en curso,
los eventos y las notas pendientes (las citas sí persisten en SQLite).
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

from app.logging_utils import log_event


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Task:
    id: str
    conversation_id: str
    user_id: str
    instruction: str
    status: str = "running"           # running | completed | failed | timeout | cancelled
    result: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None


@dataclass
class Event:
    seq: int
    conversation_id: str
    type: str                         # task_started | task_completed | task_failed | task_cancelled
    task_id: str
    message: str
    created_at: datetime = field(default_factory=_now)


class TaskManager:
    def __init__(self, timeout_s: float):
        self.timeout_s = timeout_s
        self.tasks: dict[str, Task] = {}
        self._handles: dict[str, asyncio.Task] = {}
        self._events: dict[str, list[Event]] = {}
        self._notes: dict[str, list[str]] = {}       # resultados para el contexto del agente principal
        self._seq = 0

    # ------------------------------------------------------------ ciclo de vida
    def start(self, conversation_id: str, user_id: str, instruction: str,
              runner: Callable[[], Awaitable[str]]) -> tuple[Task, list[str]]:
        """Lanza la tarea y vuelve INMEDIATAMENTE. Devuelve (tarea, ids de tareas reemplazadas)."""
        # Regla: una sola tarea de agenda activa por conversación. Una petición nueva
        # (p. ej. "mejor el jueves") reemplaza a la anterior, cuyo resultado ya no es vigente.
        superseded = [t.id for t in self.running_for(conversation_id)]
        for task_id in superseded:
            self.cancel(task_id, reason="reemplazada por una petición más reciente")

        task = Task(id="t_" + uuid.uuid4().hex[:8], conversation_id=conversation_id,
                    user_id=user_id, instruction=instruction)
        self.tasks[task.id] = task
        self._handles[task.id] = asyncio.create_task(self._run(task, runner))   # <- no se espera
        self._emit(task, "task_started", "Consultando la agenda…")
        log_event("task_started", conversation_id=conversation_id, task_id=task.id)
        return task, superseded

    async def _run(self, task: Task, runner: Callable[[], Awaitable[str]]) -> None:
        start = asyncio.get_running_loop().time()
        try:
            result = await asyncio.wait_for(runner(), timeout=self.timeout_s)
        except asyncio.CancelledError:
            return                                    # cancel() ya registró el estado y el evento
        except TimeoutError:
            self._finish(task, "timeout", error="La tarea superó el tiempo máximo.")
        except Exception as exc:                      # error controlado: nunca tumba el servidor
            self._finish(task, "failed", error=f"{type(exc).__name__}: {exc}")
        else:
            if task.status != "running":              # resultado tardío de una tarea cancelada
                log_event("late_result_discarded", task_id=task.id)
                return
            self._finish(task, "completed", result=result)
        finally:
            self._handles.pop(task.id, None)
            log_event("task_finished", conversation_id=task.conversation_id, task_id=task.id,
                      status=task.status,
                      duration_ms=round((asyncio.get_running_loop().time() - start) * 1000))

    def cancel(self, task_id: str, reason: str = "cancelada por el usuario") -> bool:
        task = self.tasks.get(task_id)
        if task is None or task.status != "running":
            return False
        task.status, task.error, task.finished_at = "cancelled", reason, _now()
        handle = self._handles.get(task_id)
        if handle:
            handle.cancel()
        self._emit(task, "task_cancelled", f"Tarea cancelada: {reason}.")
        self._note(task, f"La tarea {task.id} fue cancelada ({reason}); su resultado no es vigente.")
        return True

    def _finish(self, task: Task, status: str, result: str | None = None, error: str | None = None):
        task.status, task.result, task.error, task.finished_at = status, result, error, _now()
        if status == "completed":
            self._emit(task, "task_completed", result or "")
            self._note(task, result or "")
        else:
            msg = "No pude completar la consulta de agenda. Por favor, inténtalo de nuevo."
            self._emit(task, "task_failed", msg)
            self._note(task, f"{msg} (detalle: {error})")

    # ------------------------------------------------------------- eventos
    def _emit(self, task: Task, type_: str, message: str) -> None:
        self._seq += 1
        self._events.setdefault(task.conversation_id, []).append(
            Event(self._seq, task.conversation_id, type_, task.id, message))

    def _note(self, task: Task, text: str) -> None:
        self._notes.setdefault(task.conversation_id, []).append(f"[Resultado de la tarea {task.id}] {text}")

    def events_after(self, conversation_id: str, after: int = 0) -> list[Event]:
        return [e for e in self._events.get(conversation_id, []) if e.seq > after]

    def pop_notes(self, conversation_id: str) -> list[str]:
        return self._notes.pop(conversation_id, [])

    def running_for(self, conversation_id: str) -> list[Task]:
        return [t for t in self.tasks.values()
                if t.conversation_id == conversation_id and t.status == "running"]

    def running_for_all(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == "running"]

    def running_count(self) -> int:
        return len(self.running_for_all())