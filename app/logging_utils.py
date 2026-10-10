"""Logs estructurados en JSON"""
import json
import logging
import time
from contextlib import contextmanager

logger = logging.getLogger("centro_demo")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)      # evita ruido de cada petición HTTP


def log_event(event: str, **fields) -> None:
    # Nunca pasamos aquí la API key ni el contenido completo de los mensajes.
    logger.info(json.dumps({"ts": round(time.time(), 3), "event": event, **fields},
                           ensure_ascii=False, default=str))


@contextmanager
def timed(event: str, **fields):
    """Mide la duración de un bloque y registra su resultado (ok / error)."""
    start = time.perf_counter()
    outcome = "ok"
    try:
        yield
    except BaseException as exc:
        outcome = type(exc).__name__
        raise
    finally:
        log_event(event, duration_ms=round((time.perf_counter() - start) * 1000), outcome=outcome, **fields)