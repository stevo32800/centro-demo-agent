"""Fábrica del modelo de chat (Mistral vía LangChain)."""
from langchain_core.language_models import BaseChatModel
from langchain_mistralai import ChatMistralAI

from app.config import Settings


def build_llm(settings: Settings, model: str | None = None) -> BaseChatModel:
    if not settings.mistral_api_key:
        raise RuntimeError("Falta MISTRAL_API_KEY")
    return ChatMistralAI(
        model=model or settings.mistral_model,
        api_key=settings.mistral_api_key,
        temperature=settings.llm_temperature,
        timeout=int(settings.llm_timeout_s),
        max_retries=settings.llm_max_retries,   # solo afecta a la llamada al LLM
    )
