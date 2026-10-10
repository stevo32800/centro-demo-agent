"""Contratos: API HTTP, contexto de ejecución y entradas de las herramientas."""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


# ----------------------------------------------------------------- API HTTP
class ChatRequest(BaseModel):
    conversation_id: str | None = Field(None, description="Vacío para empezar una conversación nueva.")
    message: str = Field(..., min_length=1, max_length=2000)


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    started_tasks: list[str] = Field(default_factory=list,
                                     description="Tareas en segundo plano lanzadas en este turno.")


class EventOut(BaseModel):
    seq: int
    type: Literal["task_started", "task_completed", "task_failed", "task_cancelled"]
    task_id: str
    message: str
    created_at: datetime


class TaskOut(BaseModel):
    id: str
    conversation_id: str
    status: Literal["running", "completed", "failed", "timeout", "cancelled"]
    instruction: str
    result: str | None = None
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


# ------------------------------------------------- contexto de ejecución
@dataclass
class AppContext:
    """Lo fija el SERVIDOR en cada turno. El modelo nunca lo ve ni puede cambiarlo."""
    user_id: str
    conversation_id: str
    turn: int
    task_id: str | None = None          # solo dentro del subagente: para relacionar los logs
    started_tasks: list[str] = field(default_factory=list)


# --------------------------------------------- entradas de herramientas
class BuscarInfoInput(BaseModel):
    consulta: str = Field(..., description="Pregunta sobre el negocio: horarios, servicios, políticas...")


class DelegarAgendaInput(BaseModel):
    instruccion: str = Field(..., description=(
        "Instrucción autocontenida para el subagente de agenda, con fechas absolutas "
        "(YYYY-MM-DD), horas (HH:MM) e identificadores si se conocen."))


class CancelarTareaInput(BaseModel):
    task_id: str = Field(..., description="Identificador de la tarea a cancelar (t_...).")


class FechaInput(BaseModel):
    fecha: date = Field(..., description="Día a consultar, formato YYYY-MM-DD.")


class PrepararReservaInput(BaseModel):
    inicio: datetime = Field(..., description="Inicio de la cita, YYYY-MM-DDTHH:MM, hora de Guayaquil.")


class PrepararReprogramacionInput(BaseModel):
    cita_id: str = Field(..., description="Identificador de la cita actual (c_...).")
    nuevo_inicio: datetime = Field(..., description="Nuevo inicio, YYYY-MM-DDTHH:MM, hora de Guayaquil.")


class CitaIdInput(BaseModel):
    cita_id: str = Field(..., description="Identificador de la cita (c_...).")


class OperacionInput(BaseModel):
    operation_id: str = Field(..., description="Identificador de la operación pendiente (op_...).")