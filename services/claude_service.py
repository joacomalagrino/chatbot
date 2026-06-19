import logging

import anthropic

from config import get_settings

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


async def get_ai_response(project: str, project_config: dict, message: str, history: list) -> str:
    system_prompt = SYSTEM_TEMPLATE.format(
        persona=project_config["persona"],
        goal=project_config["goal"],
        questions="\n".join(f"- {q}" for q in project_config["questions"]),
    )

    messages = history[-20:] + [{"role": "user", "content": message}]

    try:
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system_prompt,
            messages=messages,
        )
    except anthropic.APIError:
        logger.exception("Error llamando a la API de Claude")
        return FALLBACK

    # Acceso defensivo: tomar el primer bloque de texto, no asumir content[0].
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    return text or FALLBACK
