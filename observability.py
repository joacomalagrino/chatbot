"""Observabilidad liviana: logging estructurado + registro en memoria de errores recientes.

En un captador de leads, un error no visto = lead perdido. Las tareas en background del
webhook (FastAPI no reporta sus excepciones) y los fetches a Graph son justo los puntos
donde un fallo se vuelve silencioso. Acá centralizamos dos cosas, sin dependencias nuevas:

1. `configure_logging()` — formato consistente (timestamp/nivel/módulo) y nivel por env
   (LOG_LEVEL, default INFO).
2. `record_error()` / `recent_errors()` — un anillo en memoria de los últimos N errores
   para poder ver "qué falló últimamente" desde el panel sin entrar a los logs de Railway.
"""
import logging
import os
import threading
from collections import deque
from datetime import datetime, timezone

# Cuántos errores recientes retenemos en memoria. Es un anillo (deque acotado): el más viejo
# se descarta al entrar uno nuevo, así que el consumo de memoria está acotado por diseño.
MAX_RECENT_ERRORS = 50

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_logging_configured = False
_logging_lock = threading.Lock()

# El registro de errores se toca desde BackgroundTasks (event loop) y desde el endpoint del
# panel (request). El deque de stdlib es thread-safe para append/iteración, pero protegemos
# igual la lectura del snapshot con un lock para no exponer estados intermedios.
_errors: "deque[dict]" = deque(maxlen=MAX_RECENT_ERRORS)
_errors_lock = threading.Lock()


def configure_logging() -> None:
    """Configura el root logger una sola vez (idempotente).

    Nivel desde LOG_LEVEL (default INFO). Si LOG_LEVEL trae un valor inválido, cae a INFO
    en vez de romper el arranque del app.
    """
    global _logging_configured
    with _logging_lock:
        if _logging_configured:
            return
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, None)
        if not isinstance(level, int):
            level = logging.INFO
        logging.basicConfig(level=level, format=_LOG_FORMAT)
        # basicConfig no reconfigura si ya hay handlers (p.ej. uvicorn): forzamos el nivel y
        # el formato igual, para que nuestro formato gane.
        root = logging.getLogger()
        root.setLevel(level)
        for handler in root.handlers:
            handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        _logging_configured = True


def record_error(context: str, exc: BaseException | None = None, **details) -> None:
    """Registra un error en el anillo en memoria para verlo después en el panel.

    `context` es una etiqueta corta de dónde pasó (ej. "webhook._process_event").
    `exc` es la excepción capturada (opcional). `details` son claves de contexto del
    negocio (qué evento, qué lead) — NO meter PII cruda acá; usar ids/etiquetas.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "context": context,
        "error": repr(exc) if exc is not None else None,
        "error_type": type(exc).__name__ if exc is not None else None,
        "details": details or None,
    }
    with _errors_lock:
        _errors.append(entry)


def recent_errors() -> list[dict]:
    """Snapshot de los errores recientes, del más nuevo al más viejo."""
    with _errors_lock:
        return list(reversed(_errors))


def error_count() -> int:
    """Cuántos errores hay retenidos ahora mismo en el anillo."""
    with _errors_lock:
        return len(_errors)


def clear_errors() -> None:
    """Vacía el anillo (sobre todo para aislamiento entre tests)."""
    with _errors_lock:
        _errors.clear()
