"""Fábrica del modelo de chat (Groq vía LangChain)."""
from langchain_core.language_models import BaseChatModel
from langchain_groq import ChatGroq

from app.config import Settings


def build_llm(settings: Settings) -> BaseChatModel:
    if not settings.groq_api_key:
        raise RuntimeError("Falta GROQ_API_KEY")
    return ChatGroq(
        model=settings.groq_model,
        api_key=settings.groq_api_key,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,   # solo afecta a la llamada al LLM
    )