"""Configuración centralizada"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM (Mistral) ---
    mistral_api_key: str = ""                   # nunca se registra en los logs
    mistral_model: str = "ministral-14b-latest"
    mistral_fallback_model: str = "ministral-8b-latest"   # si el principal falla (p. ej. 429); "" lo desactiva
    llm_temperature: float = 0.0
    llm_timeout_s: float = 30.0                 # timeout de cada llamada al modelo
    llm_max_retries: int = 2                    # reintentar el LLM es seguro 

    # --- Límites del agente ---
    max_model_calls_per_run: int = 8
    max_tool_calls_per_run: int = 8

    # --- Agenda ---
    db_path: str = "data/agenda.db"
    availability_delay_s: float = 15.0          # demora simulada exigida por la prueba
    tool_timeout_s: float = 25.0                # timeout de la herramienta de disponibilidad
    subagent_timeout_s: float = 90.0            # timeout global de una tarea del subagente

    # --- RAG ---
    docs_dir: str = "docs"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()