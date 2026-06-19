import logging

import anthropic

from config import get_settings
from services.text_utils import parse_model_json

logger = logging.getLogger(__name__)
settings = get_settings()

# Cliente SYNC a propósito: el endpoint /ads/generate es `def` (corre en threadpool),
# así que no bloquea el event loop. Si se pasa el endpoint a `async def`, migrar a AsyncAnthropic.
client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=30.0, max_retries=2)

AD_SYSTEM = """\
Sos un copywriter publicitario experto en Meta Ads (Facebook e Instagram) para el mercado argentino.
Escribís anuncios que convierten: ganchos fuertes, beneficios claros, llamado a la acción directo.
Usás español rioplatense natural, sin sonar robótico ni exagerado.

Contexto del negocio:
{persona}
Objetivo del negocio: {goal}

Generás SIEMPRE 3 variantes de anuncio para testear (A/B/C).
Respondés ÚNICAMENTE con un JSON válido, sin texto adicional, con esta forma exacta:
{{
  "variantes": [
    {{
      "titular": "máx 40 caracteres",
      "texto_principal": "máx 125 caracteres, el cuerpo del anuncio",
      "descripcion": "máx 30 caracteres, debajo del titular",
      "cta": "uno de: Más información | Enviar mensaje | Comprar | Registrarte | Cómo llegar",
      "concepto_visual": "descripción breve de la imagen/video ideal para esta variante"
    }}
  ],
  "publico_sugerido": {{
    "edad": "rango ej. 25-55",
    "intereses": ["interés 1", "interés 2"],
    "ubicacion": "ej. Argentina, AMBA"
  }},
  "presupuesto_sugerido_ars_dia": "monto sugerido por día en pesos"
}}
"""


def generate_ad(project: str, project_config: dict, brief: str, channel: str = "ambos") -> dict:
    """Genera 3 variantes de anuncio + sugerencia de público para un brief.

    brief: texto libre describiendo qué promocionar.
    channel: facebook | instagram | ambos
    """
    system = AD_SYSTEM.format(
        persona=project_config["persona"],
        goal=project_config["goal"],
    )

    user_prompt = (
        f"Generá anuncios para esta campaña.\n"
        f"Plataforma: {channel}\n"
        f"Qué promocionar: {brief}\n\n"
        f"Recordá: solo el JSON, nada más."
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=system,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except anthropic.APIError as e:
        logger.exception("Error llamando a la API de Claude en generate_ad")
        return {"error": f"Error del modelo ({e.__class__.__name__})"}

    if getattr(response, "stop_reason", None) == "max_tokens":
        return {"error": "La respuesta se truncó (max_tokens). Probá con un brief más corto."}

    raw = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    return parse_model_json(raw)
