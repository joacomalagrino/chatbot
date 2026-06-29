import logging
import time

import anthropic

from config import get_settings
from observability import record_error

logger = logging.getLogger(__name__)
settings = get_settings()

# Cliente ASYNC: no bloquea el event loop de FastAPI. timeout y reintentos acotados.
client = anthropic.AsyncAnthropic(
    api_key=settings.anthropic_api_key, timeout=20.0, max_retries=2
)

FALLBACK = "Disculpá, estoy con un problema técnico en este momento. ¿Probás de nuevo en un ratito?"

SYSTEM_TEMPLATE = """\
{persona}

OBJETIVO: {goal}

INSTRUCCIONES:
- Respondé en el mismo idioma que el usuario. Si hablan en español, usá español rioplatense informal (vos, te, etc.).
- Sé conciso y natural. Máximo 2-3 oraciones por respuesta.
- Hacé UNA sola pregunta por turno. No bombardees con varias a la vez.
- Si el usuario da su nombre, usalo en las respuestas.
- Cuando tengas nombre + teléfono o email, avisale que alguien lo va a contactar pronto y preguntá si necesita algo más.
- No inventes precios ni información. Si no sabés algo, decí que lo consultás.
- No menciones que sos una IA a menos que te lo pregunten directamente.

PREGUNTAS CLAVE (hacelas de a una, en el momento natural de la conversación):
{questions}
"""


def _build_system_prompt(project_config: dict) -> str:
    return SYSTEM_TEMPLATE.format(
        persona=project_config["persona"],
        goal=project_config["goal"],
        questions="\n".join(f"- {q}" for q in project_config["questions"]),
    )


async def get_ai_response(project: str, project_config: dict, message: str, history: list) -> str:
    system_prompt = _build_system_prompt(project_config)

    messages = history[-20:] + [{"role": "user", "content": message}]

    started = time.monotonic()
    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system_prompt,
            messages=messages,
        )
    except anthropic.APIError as exc:
        # Latencia hasta el fallo: distingue un timeout (≈20s, el límite del cliente) de un
        # rechazo inmediato (rate limit / 5xx). Bajo carga, una racha de timeouts de Claude
        # es por qué los leads dejan de recibir respuesta; queda en /leads/errors además del log.
        latency_ms = round((time.monotonic() - started) * 1000)
        logger.warning(
            "Claude API falló (project=%s, latency_ms=%d): %s",
            project, latency_ms, type(exc).__name__,
        )
        record_error("claude_service.create", exc, project=project, latency_ms=latency_ms)
        return FALLBACK

    # Acceso defensivo: tomar el primer bloque de texto, no asumir content[0].
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    return text or FALLBACK


async def stream_ai_response(project: str, project_config: dict, message: str, history: list):
    """Versión streaming de get_ai_response: async generator que yieldea cada delta de texto.

    Reusa el mismo system prompt y armado de `messages` que get_ai_response. Ante un error
    de la API de Claude yieldea el FALLBACK una sola vez (no propaga), para que el consumidor
    cierre el stream limpio. NO reemplaza a get_ai_response (que sigue para el webhook y el
    fallback no-streaming de /chat)."""
    system_prompt = _build_system_prompt(project_config)

    messages = history[-20:] + [{"role": "user", "content": message}]

    started = time.monotonic()
    try:
        async with client.messages.stream(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system_prompt,
            messages=messages,
        ) as stream:
            async for delta in stream.text_stream:
                yield delta
    except anthropic.APIError as exc:
        latency_ms = round((time.monotonic() - started) * 1000)
        logger.warning(
            "Claude streaming falló (project=%s, latency_ms=%d): %s",
            project, latency_ms, type(exc).__name__,
        )
        record_error("claude_service.stream", exc, project=project, latency_ms=latency_ms)
        yield FALLBACK
