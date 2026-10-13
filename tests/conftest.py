"""Dobles de prueba: un modelo de chat 'guionizado' que decide por reglas, sin red ni API key."""
import re
import uuid
from datetime import datetime
from typing import Any, Callable

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agenda import TZ, Agenda

# Miércoles 14/10/2026 08:00 en Guayaquil: reloj fijo -> pruebas reproducibles.
FIXED_NOW = datetime(2026, 10, 14, 8, 0, tzinfo=TZ)


class ScriptedChatModel(BaseChatModel):
    """Modelo falso: `rules(messages)` devuelve el AIMessage que 'respondería' el LLM."""
    rules: Any

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self                                   # las herramientas ya están en las reglas

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.rules(messages))])


def call(name: str, **args) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": "call_" + uuid.uuid4().hex[:6]}])


def main_rules(messages: list[BaseMessage]) -> AIMessage:
    """Agente principal falso: horario -> RAG; todo lo de agenda -> delegar."""
    last = messages[-1]
    if isinstance(last, HumanMessage):
        text = last.content.lower()
        if "horario" in text:
            return call("buscar_info_negocio", consulta=last.content)
        if text.startswith("cancela la tarea"):
            return call("cancelar_tarea_agenda", task_id=last.content.split()[-1])
        return call("delegar_agenda", instruccion=last.content)
    if isinstance(last, ToolMessage):
        if last.name == "buscar_info_negocio":
            return AIMessage("Atendemos de lunes a viernes de 09:00 a 17:00.")
        return AIMessage("Lo estoy consultando; el resultado aparecerá en la conversación.")
    return AIMessage("¿En qué puedo ayudarte?")


def agenda_rules(messages: list[BaseMessage]) -> AIMessage:
    """Subagente falso. Instrucciones: 'disponibilidad YYYY-MM-DD', 'reservar ISO',
    'confirma op_x', y 'impaciente ISO' (un LLM que intenta ejecutar sin confirmación)."""
    instruction = next(m for m in messages if isinstance(m, HumanMessage)).content
    last = messages[-1]
    if isinstance(last, HumanMessage):
        verb, arg = instruction.split()[0], instruction.split()[-1]
        if verb == "disponibilidad":
            return call("consultar_disponibilidad", fecha=arg)
        if verb in ("reservar", "impaciente"):
            return call("preparar_reserva", inicio=arg)
        if verb == "confirma":
            return call("ejecutar_operacion", operation_id=arg)
    if isinstance(last, ToolMessage):
        op = re.search(r"op_[0-9a-f]{8}", str(last.content))
        if instruction.startswith("impaciente") and last.name == "preparar_reserva" and op:
            return call("ejecutar_operacion", operation_id=op.group())
        return AIMessage(f"Resultado: {last.content}")
    return AIMessage("No entendí la instrucción.")


@pytest.fixture
def agenda(tmp_path) -> Agenda:
    a = Agenda(str(tmp_path / "test.db"), now_fn=lambda: FIXED_NOW)
    a.ensure_user("ana", "Ana")
    a.ensure_user("luis", "Luis")
    return a


def make_rules_model(rules: Callable) -> ScriptedChatModel:
    return ScriptedChatModel(rules=rules)