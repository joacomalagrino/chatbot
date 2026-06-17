import json
import anthropic
from config import get_settings

settings = get_settings()
client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

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
    """Generate 3 ad variants + targeting suggestion for a given brief.

    brief: free text describing what to promote, e.g.
      "Financiación 0% en autos 0km" or "Prueba gratis de Mesa para PyMEs".
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

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # Tolerate accidental markdown fencing
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "No se pudo parsear la respuesta del modelo", "raw": raw}
