"""API HTTP (FastAPI). Arranque: uvicorn app.main:app"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from app.agenda import Agenda
from app.agent_agenda import PendingOperations, build_agenda_agent
from app.agent_main import ChatService, ConversationNotFound, build_main_agent
from app.config import Settings, get_settings
from app.llm import build_llm
from app.logging_utils import log_event, setup_logging
from app.rag import BusinessKB
from app.schemas import ChatRequest, ChatResponse, EventOut, TaskOut
from app.seed import seed
from app.tasks import TaskManager


def create_app(settings: Settings | None = None, llm_main=None, llm_agenda=None, now_fn=None) -> FastAPI:
    """Fábrica de la app. Los tests inyectan modelos falsos y un reloj fijo."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.log_level)
        agenda = Agenda(settings.db_path, now_fn) if now_fn else Agenda(settings.db_path)
        seed(agenda)
        tasks = TaskManager(timeout_s=settings.subagent_timeout_s)
        ops = PendingOperations()
        main_llm = llm_main or build_llm(settings)
        agenda_llm = llm_agenda or main_llm
        agenda_agent = build_agenda_agent(agenda_llm, agenda, ops, settings)
        main_agent = build_main_agent(main_llm, BusinessKB(settings.docs_dir), tasks, agenda,
                                      agenda_agent, settings)
        app.state.agenda, app.state.tasks = agenda, tasks
        app.state.chat = ChatService(main_agent, tasks)
        log_event("startup", model=settings.groq_model, db=settings.db_path)
        yield
        for task in tasks.running_for_all():
            tasks.cancel(task.id, reason="apagado del servidor")

    app = FastAPI(title="Centro Demo – agente con subagente de agenda", lifespan=lifespan)

    # ------------------------------------------------------------ identidad
    def current_user(request: Request, x_user_id: str = Header(..., description="Usuario simulado")) -> str:
        """Identidad simulada: la fija el cliente HTTP en una cabecera, NUNCA el texto del chat."""
        if not request.app.state.agenda.user_exists(x_user_id):
            raise HTTPException(401, "Usuario desconocido")
        return x_user_id

    # ----------------------------------------------------------- endpoints
    @app.get("/health")
    async def health(request: Request):
        return {"status": "ok", "model": settings.groq_model,
                "running_tasks": request.app.state.tasks.running_count()}

    @app.post("/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request, user_id: str = Depends(current_user)):
        try:
            conv_id, reply, started = await request.app.state.chat.chat(
                user_id, body.message, body.conversation_id)
        except ConversationNotFound:
            raise HTTPException(404, "Conversación no encontrada")
        except Exception as exc:                          # p. ej. el LLM no responde
            log_event("chat_error", user_id=user_id, error=type(exc).__name__)
            raise HTTPException(503, "El asistente no está disponible ahora mismo. Inténtalo de nuevo.")
        return ChatResponse(conversation_id=conv_id, reply=reply, started_tasks=started)

    @app.get("/conversations/{conversation_id}/events", response_model=list[EventOut])
    async def events(conversation_id: str, request: Request, after: int = 0,
                     user_id: str = Depends(current_user)):
        """Polling: el cliente pide los eventos con seq > after."""
        try:
            request.app.state.chat.get_conversation(conversation_id, user_id)
        except ConversationNotFound:
            raise HTTPException(404, "Conversación no encontrada")
        return [EventOut(**vars(e)) for e in request.app.state.tasks.events_after(conversation_id, after)]

    def _own_task(request: Request, task_id: str, user_id: str):
        task = request.app.state.tasks.tasks.get(task_id)
        if task is None or task.user_id != user_id:
            raise HTTPException(404, "Tarea no encontrada")
        return task

    @app.get("/tasks/{task_id}", response_model=TaskOut)
    async def get_task(task_id: str, request: Request, user_id: str = Depends(current_user)):
        return TaskOut(**vars(_own_task(request, task_id, user_id)))

    @app.delete("/tasks/{task_id}", response_model=TaskOut)
    async def cancel_task(task_id: str, request: Request, user_id: str = Depends(current_user)):
        task = _own_task(request, task_id, user_id)
        request.app.state.tasks.cancel(task_id)
        return TaskOut(**vars(task))

    @app.get("/appointments")
    async def my_appointments(request: Request, user_id: str = Depends(current_user)):
        """Estado real de la agenda del usuario (sin LLM). Útil para la demo y las pruebas."""
        return [a.to_dict() for a in request.app.state.agenda.list_appointments(user_id)]

    return app


app = create_app()