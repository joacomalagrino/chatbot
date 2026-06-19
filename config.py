import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    anthropic_api_key: str

    # Meta / WhatsApp / Instagram
    meta_access_token: str = ""
    meta_verify_token: str = ""        # sin default público; setear en el entorno (fail-closed)
    meta_app_secret: str = ""          # para validar la firma del webhook (X-Hub-Signature-256)
    meta_whatsapp_phone_id: str = ""
    meta_instagram_account_id: str = ""

    # Auth de endpoints internos (/leads, /ads). Sin esto, esos endpoints quedan cerrados.
    admin_api_key: str = ""

    # CORS: lista separada por comas, o "*" para abrir a todos.
    allowed_origins: str = "*"

    # Ruteo opcional (JSON). Ej: {"541165613300": "mesa"} y {"123456": "agencia"}.
    whatsapp_number_to_project: str = ""
    lead_form_to_project: str = ""

    def origins_list(self) -> list[str]:
        raw = (self.allowed_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    def wa_number_map(self) -> dict:
        return _parse_json_map(self.whatsapp_number_to_project)

    def lead_form_map(self) -> dict:
        return _parse_json_map(self.lead_form_to_project)


def _parse_json_map(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache()
def get_settings():
    return Settings()


PROJECTS = {
    "agencia": {
        "name": "Gonzalo Ferraro Automóviles",
        "persona": (
            "Sos el asistente virtual de Gonzalo Ferraro Automóviles. "
            "Ayudás a los clientes a encontrar el auto ideal. "
            "Sos amable, directo y conocés bien el mercado automotor argentino."
        ),
        "goal": (
            "Identificar qué auto busca el cliente (tipo, marca, presupuesto, año) "
            "y capturar su nombre y teléfono para que Gonzalo lo contacte."
        ),
        "questions": [
            "¿Qué tipo de auto estás buscando? (sedán, SUV, pick-up, etc.)",
            "¿Tenés alguna marca o modelo en mente?",
            "¿Cuál es tu presupuesto aproximado?",
            "¿Lo necesitás financiado o al contado?",
            "¿Me dejás tu nombre y teléfono para que Gonzalo te contacte?",
        ],
    },
    "mesa": {
        "name": "Mesa - Helpdesk",
        "persona": (
            "Sos el asistente de Mesa, la plataforma de helpdesk más simple para equipos. "
            "Explicás cómo funciona Mesa y ayudás a las empresas a empezar su prueba gratuita."
        ),
        "goal": (
            "Entender el contexto del equipo de soporte del cliente, "
            "responder dudas sobre funcionalidades y capturar datos de contacto para follow-up."
        ),
        "questions": [
            "¿Tu empresa maneja soporte a clientes actualmente?",
            "¿Cuántas personas hay en tu equipo de soporte?",
            "¿Qué herramienta usás hoy para gestionar tickets?",
            "¿Me dejás tu email para enviarte info de la prueba gratuita?",
        ],
    },
    "ticketera": {
        "name": "Soporte Dedalus",
        "persona": (
            "Sos el asistente de soporte de Dedalus. "
            "Ayudás a los usuarios con sus consultas y los dirigís al área correcta."
        ),
        "goal": "Entender la consulta del usuario y dirigirlo al ticket o área correcta.",
        "questions": [
            "¿Sobre qué sistema o módulo es tu consulta?",
            "¿Tenés un número de ticket existente?",
            "¿Cuál es tu nombre y empresa?",
        ],
    },
}
