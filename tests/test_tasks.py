"""Ciclo de vida de las tareas en segundo plano (sin LLM)."""
import asyncio

import pytest

from app.tasks import TaskManager

pytestmark = pytest.mark.asyncio


async def slow(result: str, seconds: float):
    await asyncio.sleep(seconds)
    return result


async def test_start_returns_immediately_and_publishes_result():
    tm = TaskManager(timeout_s=5)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    task, _ = tm.start("c1", "ana", "x", lambda: slow("listo", 0.3))
    assert loop.time() - t0 < 0.05 and task.status == "running"
    await asyncio.sleep(0.5)
    assert task.status == "completed"
    assert [e.type for e in tm.events_after("c1")] == ["task_started", "task_completed"]
    assert "listo" in tm.pop_notes("c1")[0]


async def test_timeout():
    tm = TaskManager(timeout_s=0.1)
    task, _ = tm.start("c1", "ana", "x", lambda: slow("tarde", 1))
    await asyncio.sleep(0.3)
    assert task.status == "timeout"
    assert tm.events_after("c1")[-1].type == "task_failed"


async def test_failure_is_controlled():
    async def boom():
        raise ValueError("fallo simulado")
    tm = TaskManager(timeout_s=1)
    task, _ = tm.start("c1", "ana", "x", boom)
    await asyncio.sleep(0.05)
    assert task.status == "failed" and "fallo simulado" in task.error


async def test_cancel_discards_late_result():
    tm = TaskManager(timeout_s=5)
    task, _ = tm.start("c1", "ana", "x", lambda: slow("no debe verse", 0.2))
    assert tm.cancel(task.id)
    await asyncio.sleep(0.4)
    assert task.status == "cancelled" and task.result is None
    assert "task_completed" not in [e.type for e in tm.events_after("c1")]


async def test_new_request_supersedes_previous():
    tm = TaskManager(timeout_s=5)
    old, _ = tm.start("c1", "ana", "martes", lambda: slow("martes", 0.2))
    new, superseded = tm.start("c1", "ana", "jueves", lambda: slow("jueves", 0.2))
    assert superseded == [old.id]
    await asyncio.sleep(0.4)
    assert old.status == "cancelled" and new.status == "completed"
    completed = [e for e in tm.events_after("c1") if e.type == "task_completed"]
    assert [e.message for e in completed] == ["jueves"]


async def test_other_conversations_are_not_affected():
    tm = TaskManager(timeout_s=5)
    a, _ = tm.start("c1", "ana", "x", lambda: slow("a", 0.1))
    b, superseded = tm.start("c2", "luis", "y", lambda: slow("b", 0.1))
    assert superseded == []
    await asyncio.sleep(0.3)
    assert a.status == b.status == "completed"