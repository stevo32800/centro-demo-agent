"""Agente principal + servicio de chat (memoria, turnos y delegación en segundo plano)."""
import asyncio
import uuid
from dataclasses import dataclass, field

from langchain.agents import create_agent
from langchain.agents.middleware import (ModelCallLimitMiddleware, ModelRequest,
                                         ToolCallLimitMiddleware, dynamic_prompt)
from langchain.tools import ToolRuntime, tool
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.agenda import Agenda
from app.agent_agenda import fmt
from app.config import Settings
from app.logging_utils import log_event, timed
from app.rag import BusinessKB
from app.schemas import AppContext, BuscarInfoInput, CancelarTareaInput, DelegarAgendaInput
from app.tasks import TaskManager

SYSTEM_PROMPT = """Eres el asistente de Centro Demo, un centro de asesorías de 30 minutos en Guayaquil.
Responde en español, de forma breve y amable.

- Para preguntas sobre el negocio (horarios, servicios, precios, políticas, equipo) usa
  buscar_info_negocio y responde SOLO con lo que digan los documentos.
- Todo lo relacionado con citas (disponibilidad, reservar, ver, cambiar o cancelar) se delega con
  delegar_agenda. Escribe una instrucción autocontenida: fechas absolutas YYYY-MM-DD, horas HH:MM e
  identificadores (c_..., op_...) si los conoces. Usa el historial para resolver "mañana", "esa", etc.
- Si falta un dato imprescindible (por ejemplo el día), pregunta antes de delegar.
- Tras delegar, di que lo estás consultando y que el resultado aparecerá en la conversación.
  NUNCA inventes el resultado de la agenda.
- Si el usuario confirma una operación pendiente, delega: "El usuario CONFIRMA explícitamente la
  operación op_...". Si la rechaza, delega que la descarte.
- Si el usuario cambia de idea mientras hay una consulta en curso, delega la nueva petición: la
  anterior se cancela automáticamente. Si pide parar la consulta, usa cancelar_tarea_agenda.
- Los mensajes que empiezan por "[Resultado de la tarea" ya se mostraron al usuario: úsalos como contexto."""


def build_main_agent(llm: BaseChatModel, kb: BusinessKB, tasks: TaskManager, agenda: Agenda,
                     agenda_agent, settings: Settings):

    @dynamic_prompt
    def prompt_with_date(request: ModelRequest) -> str:
        now = agenda.now_fn()
        return f"{SYSTEM_PROMPT}\n\nAhora es {fmt(now)} ({now:%Y-%m-%d}), hora de Guayaquil."

    @tool(args_schema=BuscarInfoInput)
    def buscar_info_negocio(consulta: str) -> dict:
        """Busca información del negocio en los documentos de Centro Demo."""
        with timed("tool", tool="buscar_info_negocio"):
            chunks = kb.search(consulta, k=3)
        return {"ok": True, "fragmentos": [{"fuente": c.source, "seccion": c.title, "texto": c.text}
                                           for c in chunks]}

    @tool(args_schema=DelegarAgendaInput)
    async def delegar_agenda(instruccion: str, runtime: ToolRuntime[AppContext]) -> dict:
        """Delega una tarea de agenda al subagente. Vuelve al instante; el resultado llega después."""
        ctx = runtime.context
        sub_ctx = AppContext(user_id=ctx.user_id, conversation_id=ctx.conversation_id, turn=ctx.turn)

        async def run_subagent() -> str:
            result = await agenda_agent.ainvoke(
                {"messages": [HumanMessage(instruccion)]},
                context=sub_ctx,                                   # misma identidad, fijada por el servidor
                config={"recursion_limit": 25},
            )
            return result["messages"][-1].content

        task, superseded = tasks.start(ctx.conversation_id, ctx.user_id, instruccion, run_subagent)
        sub_ctx.task_id = task.id            # antes de que el subagente arranque: aparece en sus logs
        ctx.started_tasks.append(task.id)
        return {"ok": True, "task_id": task.id, "estado": "en_curso", "tareas_reemplazadas": superseded}

    @tool(args_schema=CancelarTareaInput)
    def cancelar_tarea_agenda(task_id: str, runtime: ToolRuntime[AppContext]) -> dict:
        """Cancela una consulta de agenda en curso de esta conversación."""
        task = tasks.tasks.get(task_id)
        if task is None or task.conversation_id != runtime.context.conversation_id:
            return {"ok": False, "error": "NOT_FOUND", "message": "No existe esa tarea en esta conversación."}
        if not tasks.cancel(task_id):
            return {"ok": False, "error": "NOT_RUNNING", "message": f"La tarea ya terminó ({task.status})."}
        return {"ok": True, "cancelada": task_id}

    return create_agent(
        model=llm,
        tools=[buscar_info_negocio, delegar_agenda, cancelar_tarea_agenda],
        context_schema=AppContext,
        checkpointer=InMemorySaver(),             # memoria por conversación (thread_id)
        middleware=[
            prompt_with_date,
            ModelCallLimitMiddleware(run_limit=settings.max_model_calls_per_run, exit_behavior="end"),
            ToolCallLimitMiddleware(run_limit=settings.max_tool_calls_per_run),
        ],
        name="agente_principal",
    )


@dataclass
class Conversation:
    id: str
    user_id: str
    turn: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationNotFound(Exception):
    pass


class ChatService:
    def __init__(self, main_agent, tasks: TaskManager):
        self.agent = main_agent
        self.tasks = tasks
        self.conversations: dict[str, Conversation] = {}

    def get_conversation(self, conversation_id: str, user_id: str) -> Conversation:
        conv = self.conversations.get(conversation_id)
        if conv is None or conv.user_id != user_id:     # una conversación ajena se trata como inexistente
            raise ConversationNotFound(conversation_id)
        return conv

    async def chat(self, user_id: str, message: str, conversation_id: str | None) -> tuple[str, str, list[str]]:
        if conversation_id is None:
            conversation_id = "conv_" + uuid.uuid4().hex[:8]
            self.conversations[conversation_id] = Conversation(conversation_id, user_id)
        conv = self.get_conversation(conversation_id, user_id)

        # El lock solo serializa los turnos de ESTA conversación (dura lo que tarda el LLM,
        # no los 15 s del subagente). Otras conversaciones avanzan en paralelo.
        async with conv.lock:
            conv.turn += 1
            ctx = AppContext(user_id=user_id, conversation_id=conv.id, turn=conv.turn)
            # Resultados de tareas que terminaron desde el último turno -> contexto del agente.
            notes = [AIMessage(n) for n in self.tasks.pop_notes(conv.id)]
            with timed("agent_turn", conversation_id=conv.id, turn=conv.turn, msg_chars=len(message)):
                result = await self.agent.ainvoke(
                    {"messages": [*notes, HumanMessage(message)]},
                    config={"configurable": {"thread_id": conv.id}, "recursion_limit": 25},
                    context=ctx,
                )
        reply = result["messages"][-1].content or "¿Puedes reformular tu mensaje?"
        log_event("agent_reply", conversation_id=conv.id, turn=conv.turn, started_tasks=ctx.started_tasks)
        return conv.id, reply, ctx.started_tasks