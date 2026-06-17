import anthropic
from config import get_settings

settings = get_settings()
client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

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

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system_prompt,
        messages=messages,
    )

    return response.content[0].text
