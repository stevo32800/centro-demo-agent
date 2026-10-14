"""Subagente de agenda: un agente LangChain con sus propias instrucciones y herramientas.

Es SIN ESTADO: cada tarea recibe una instrucción autocontenida y devuelve un texto.
La memoria de la conversación la tiene el agente principal; lo único que persiste
entre tareas son las operaciones pendientes de confirmación (en código, no en el LLM).
"""
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from langchain.agents import create_agent
from langchain.agents.middleware import (ModelCallLimitMiddleware, ModelFallbackMiddleware,
                                         ModelRequest, ToolCallLimitMiddleware, dynamic_prompt)
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel

from app.agenda import TZ, Agenda, AgendaError
from app.config import Settings
from app.logging_utils import timed
from app.schemas import (AppContext, CitaIdInput, FechaInput, OperacionInput,
                         PrepararReprogramacionInput, PrepararReservaInput)

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fmt(dt: datetime) -> str:
    dt = dt.astimezone(TZ)
    return f"{DIAS[dt.weekday()]} {dt:%d/%m/%Y} a las {dt:%H:%M}"


def ok(**data) -> dict:
    return {"ok": True, **data}


def fail(code: str, message: str) -> dict:
    return {"ok": False, "error": code, "message": message}


# ------------------------------------------------ operaciones pendientes
@dataclass
class PendingOperation:
    id: str
    user_id: str
    conversation_id: str
    kind: str                      # crear | reprogramar | cancelar
    params: dict
    summary: str
    turn: int                      # turno del usuario en que se preparó
    status: str = "pending"        # pending | executed | discarded
    result: dict = field(default_factory=dict)


class PendingOperations:
    def __init__(self):
        self._ops: dict[str, PendingOperation] = {}

    def add(self, ctx: AppContext, kind: str, params: dict, summary: str) -> PendingOperation:
        op = PendingOperation("op_" + uuid.uuid4().hex[:8], ctx.user_id, ctx.conversation_id,
                              kind, params, summary, ctx.turn)
        self._ops[op.id] = op
        return op

    def get(self, ctx: AppContext, op_id: str) -> PendingOperation | None:
        op = self._ops.get(op_id)
        # Aislamiento: solo se ven las operaciones del mismo usuario Y la misma conversación.
        if op and op.user_id == ctx.user_id and op.conversation_id == ctx.conversation_id:
            return op
        return None

    def pending_for(self, ctx: AppContext) -> list[PendingOperation]:
        return [o for o in self._ops.values() if o.status == "pending"
                and o.user_id == ctx.user_id and o.conversation_id == ctx.conversation_id]


SYSTEM_PROMPT = """Eres el subagente de agenda de Centro Demo (asesorías de 30 minutos).
Horario: lunes a viernes, 09:00-17:00 (America/Guayaquil); la última cita empieza a las 16:30.

Reglas:
1. Usa SIEMPRE las herramientas; nunca inventes disponibilidad, citas ni identificadores.
2. Para crear, reprogramar o cancelar: llama primero a preparar_reserva / preparar_reprogramacion /
   preparar_cancelacion, muestra el resumen y pide confirmación explícita al usuario.
3. Llama a ejecutar_operacion SOLO si la instrucción dice que el usuario CONFIRMÓ una operación
   pendiente. Si el usuario la rechaza, llama a descartar_operacion.
4. Si faltan datos (día, hora, qué cita) o hay varias citas posibles, NO supongas: pide aclaración
   y enumera las opciones.
5. Comunica éxito solo si la herramienta devuelve ok=true. Si devuelve ok=false, explica el motivo.
6. Tu respuesta final va dirigida al usuario: en español, breve, con fechas legibles.
   Incluye los identificadores de cita (c_...) y de operación (op_...) entre paréntesis."""


def build_agenda_agent(llm: BaseChatModel, agenda: Agenda, ops: PendingOperations,
                       settings: Settings, fallback: BaseChatModel | None = None):

    @dynamic_prompt
    def prompt_with_context(request: ModelRequest) -> str:
        ctx: AppContext = request.runtime.context
        now = agenda.now_fn()
        pending = "\n".join(f"- {o.id}: {o.summary}" for o in ops.pending_for(ctx)) or "- (ninguna)"
        return (f"{SYSTEM_PROMPT}\n\nAhora es {fmt(now)} ({now:%Y-%m-%d}).\n"
                f"Operaciones pendientes de confirmación en esta conversación:\n{pending}")

    # ---------------------------------------------------------- lectura
    @tool(args_schema=FechaInput)
    async def consultar_disponibilidad(fecha: date, runtime: ToolRuntime[AppContext]) -> dict:
        """Consulta los horarios libres de un día. Es LENTA (~15 s)."""
        ctx = runtime.context

        async def slow_backend() -> list[datetime]:
            await asyncio.sleep(settings.availability_delay_s)          # demora simulada (no bloquea)
            return await asyncio.to_thread(agenda.available_slots, fecha)

        with timed("tool", tool="consultar_disponibilidad", conversation_id=ctx.conversation_id,
                   task_id=ctx.task_id):
            try:
                slots = await asyncio.wait_for(slow_backend(), timeout=settings.tool_timeout_s)
            except TimeoutError:
                return fail("TIMEOUT", "El sistema de agenda no respondió a tiempo. No se reservó nada.")
        if not slots:
            return ok(fecha=fecha.isoformat(), libres=[], nota="No hay horarios libres ese día.")
        return ok(fecha=fecha.isoformat(), libres=[s.strftime("%H:%M") for s in slots])

    @tool
    async def listar_mis_citas(runtime: ToolRuntime[AppContext]) -> dict:
        """Lista las citas futuras activas del usuario actual."""
        ctx = runtime.context
        with timed("tool", tool="listar_mis_citas", conversation_id=ctx.conversation_id,
                   task_id=ctx.task_id):
            appts = await asyncio.to_thread(agenda.list_appointments, ctx.user_id)
        return ok(citas=[{"cita_id": a.id, "inicio": fmt(a.start)} for a in appts])

    # ------------------------------------------- preparar (sin escribir)
    @tool(args_schema=PrepararReservaInput)
    async def preparar_reserva(inicio: datetime, runtime: ToolRuntime[AppContext]) -> dict:
        """Valida una reserva y la deja PENDIENTE de confirmación. No escribe en la agenda."""
        ctx = runtime.context
        try:
            start = agenda.validate_slot(inicio)
            if not await asyncio.to_thread(agenda.is_free, start):
                return fail("SLOT_TAKEN", f"El {fmt(start)} ya está ocupado.")
        except AgendaError as e:
            return fail(e.code, e.message)
        op = ops.add(ctx, "crear", {"inicio": start.isoformat()},
                     f"Crear una asesoría de 30 min el {fmt(start)}")
        return ok(operation_id=op.id, resumen=op.summary, requiere_confirmacion=True)

    @tool(args_schema=PrepararReprogramacionInput)
    async def preparar_reprogramacion(cita_id: str, nuevo_inicio: datetime,
                                      runtime: ToolRuntime[AppContext]) -> dict:
        """Valida un cambio de horario y lo deja PENDIENTE de confirmación."""
        ctx = runtime.context
        try:
            current = await asyncio.to_thread(agenda.get_own, ctx.user_id, cita_id)
            start = agenda.validate_slot(nuevo_inicio)
            if not await asyncio.to_thread(agenda.is_free, start):
                return fail("SLOT_TAKEN", f"El {fmt(start)} ya está ocupado.")
        except AgendaError as e:
            return fail(e.code, e.message)
        op = ops.add(ctx, "reprogramar", {"cita_id": cita_id, "inicio": start.isoformat()},
                     f"Mover la cita {cita_id} del {fmt(current.start)} al {fmt(start)}")
        return ok(operation_id=op.id, resumen=op.summary, requiere_confirmacion=True)

    @tool(args_schema=CitaIdInput)
    async def preparar_cancelacion(cita_id: str, runtime: ToolRuntime[AppContext]) -> dict:
        """Deja PENDIENTE de confirmación la cancelación de una cita del usuario."""
        ctx = runtime.context
        try:
            current = await asyncio.to_thread(agenda.get_own, ctx.user_id, cita_id)
        except AgendaError as e:
            return fail(e.code, e.message)
        op = ops.add(ctx, "cancelar", {"cita_id": cita_id},
                     f"Cancelar la cita {cita_id} del {fmt(current.start)}")
        return ok(operation_id=op.id, resumen=op.summary, requiere_confirmacion=True)

    # ------------------------------------------------ ejecutar (escribe)
    @tool(args_schema=OperacionInput)
    async def ejecutar_operacion(operation_id: str, runtime: ToolRuntime[AppContext]) -> dict:
        """Ejecuta una operación pendiente que el usuario ya confirmó explícitamente."""
        ctx = runtime.context
        op = ops.get(ctx, operation_id)
        if op is None:
            return fail("NOT_FOUND", "No existe esa operación pendiente en esta conversación.")
        if op.status == "executed":                       # repetición: misma respuesta, sin escribir
            return ok(ya_ejecutada=True, **op.result)
        if op.status == "discarded":
            return fail("DISCARDED", "Esa operación fue descartada.")
        if ctx.turn <= op.turn:
            # Garantía en código: la confirmación tiene que llegar en un mensaje POSTERIOR del usuario.
            return fail("CONFIRMATION_REQUIRED", "Falta la confirmación explícita del usuario.")
        with timed("tool", tool="ejecutar_operacion", kind=op.kind, conversation_id=ctx.conversation_id,
                   task_id=ctx.task_id):
            try:
                if op.kind == "crear":
                    appt = await asyncio.to_thread(agenda.create, ctx.user_id,
                                                   datetime.fromisoformat(op.params["inicio"]), op.id)
                elif op.kind == "reprogramar":
                    appt = await asyncio.to_thread(agenda.reschedule, ctx.user_id, op.params["cita_id"],
                                                   datetime.fromisoformat(op.params["inicio"]), op.id)
                else:
                    appt = await asyncio.to_thread(agenda.cancel, ctx.user_id, op.params["cita_id"])
            except AgendaError as e:
                op.status = "discarded"
                return fail(e.code, e.message)
        op.status = "executed"
        op.result = {"cita_id": appt.id, "inicio": fmt(appt.start), "estado": appt.status}
        return ok(**op.result)

    @tool(args_schema=OperacionInput)
    async def descartar_operacion(operation_id: str, runtime: ToolRuntime[AppContext]) -> dict:
        """Descarta una operación pendiente que el usuario rechazó."""
        op = ops.get(runtime.context, operation_id)
        if op is None or op.status != "pending":
            return fail("NOT_FOUND", "No hay una operación pendiente con ese identificador.")
        op.status = "discarded"
        return ok(descartada=operation_id)

    return create_agent(
        model=llm,
        tools=[consultar_disponibilidad, listar_mis_citas, preparar_reserva, preparar_reprogramacion,
               preparar_cancelacion, ejecutar_operacion, descartar_operacion],
        context_schema=AppContext,
        middleware=[
            prompt_with_context,
            ModelCallLimitMiddleware(run_limit=settings.max_model_calls_per_run, exit_behavior="end"),
            ToolCallLimitMiddleware(run_limit=settings.max_tool_calls_per_run),
            *([ModelFallbackMiddleware(fallback)] if fallback else []),
        ],
        name="subagente_agenda",
    )