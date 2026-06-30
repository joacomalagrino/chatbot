import json
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    anthropic_api_key: str

    # Modo desarrollo (CHATBOT_DEV=1): expone /docs y /openapi.json. En prod queda cerrado.
    dev: bool = Field(default=False, validation_alias="CHATBOT_DEV")

    # Meta / WhatsApp / Instagram
    meta_access_token: str = ""
    meta_verify_token: str = ""        # sin default público; setear en el entorno (fail-closed)
    meta_app_secret: str = ""          # para validar la firma del webhook (X-Hub-Signature-256)
    allow_unsigned_webhooks: bool = False  # SOLO dev: aceptar webhooks sin firma si falta meta_app_secret
    meta_whatsapp_phone_id: str = ""
    meta_instagram_account_id: str = ""

    # Plantilla de re-engagement de WhatsApp: nombre EXACTO de una plantilla creada y
    # APROBADA en Meta (WhatsApp Manager). Se usa cuando la ventana de 24h ya cerró y
    # Graph rechaza el free-form. Vacío = no se reabre la conversación (se loguea y se
    # omite el envío en vez de mandar un free-form que Graph va a rechazar igual).
    whatsapp_reengage_template: str = ""
    # Código de idioma de la plantilla (debe coincidir con el aprobado en Meta).
    whatsapp_reengage_template_lang: str = "es_AR"

    # Re-engagement proactivo de leads fuera de la ventana de 24h (servicio reengage_service).
    # SCAFFOLD apagado por DEFAULT y con doble gate: hasta que NO esté prendido el flag Y
    # haya una plantilla aprobada cargada, el servicio es un NO-OP (no manda nada). Esto evita
    # mandar free-form que Graph rechazaría y, sobre todo, mandar algo sin querer.
    reengage_enabled: bool = Field(default=False, validation_alias="REENGAGE_ENABLED")
    # Nombre EXACTO de la plantilla aprobada en Meta para el re-engagement proactivo. Vacío
    # (default) => el servicio no manda nada. Si se deja vacío pero hay WHATSAPP_REENGAGE_TEMPLATE
    # configurada, se reusa esa (un solo nombre de plantilla sirve para ambos usos).
    reengage_template_name: str = Field(default="", validation_alias="REENGAGE_TEMPLATE_NAME")
    # Palabras de baja del re-engagement: si un inbound de WhatsApp matchea (exacto, tras
    # trim + case-insensitive) alguna de estas, se marca reengage_opt_out=True en esa
    # conversación y el selector deja de re-engancharla. Coma-separadas y ajustable por entorno.
    reengage_optout_keywords: str = Field(
        default="BAJA,STOP,CANCELAR", validation_alias="REENGAGE_OPTOUT_KEYWORDS"
    )

    # Auth de endpoints internos (/leads, /ads). Sin esto, esos endpoints quedan cerrados.
    admin_api_key: str = ""

    # Webhook opcional (Slack/Discord/Make/etc.) para avisar cuando entra un lead caliente.
    # Si está vacío, solo se loguea. Best-effort: nunca bloquea ni rompe el flujo.
    notify_webhook_url: str = ""

    # CORS: dominios donde se EMBEBE el widget (el Origin de los requests cross-origin),
    # separados por coma. Default = los sitios propios de los 3 proyectos (NO "*", para
    # no quedar abierto a cualquier sitio en prod). En modo dev (CHATBOT_DEV=1) se abre a
    # cualquier origen para poder probar el widget desde localhost. Poné "*" explícito
    # SOLO si necesitás abrirlo a todos en prod (no recomendado).
    allowed_origins: str = (
        "https://gonzaloferraroautomoviles.ar,"
        "https://www.gonzaloferraroautomoviles.ar,"
        "https://mesa-production-d6a9.up.railway.app,"
        "https://web-production-ddcb7.up.railway.app"
    )

    # Ruteo opcional (JSON). Ej: {"541165613300": "mesa"} y {"123456": "agencia"}.
    whatsapp_number_to_project: str = ""
    lead_form_to_project: str = ""

    def origins_list(self) -> list[str]:
        # En desarrollo abrimos CORS para probar el widget desde cualquier localhost/puerto.
        if self.dev:
            return ["*"]
        raw = (self.allowed_origins or "").strip()
        if raw == "*":
            return ["*"]
        # Sin orígenes válidos en prod → fail-closed (no se permite ningún cross-origin).
        return [o.strip() for o in raw.split(",") if o.strip()]

    def reengage_template(self) -> str:
        """Plantilla efectiva para el re-engagement proactivo.

        Prioriza REENGAGE_TEMPLATE_NAME; si está vacía, cae a WHATSAPP_REENGAGE_TEMPLATE
        (la misma plantilla aprobada sirve para los dos usos). Vacío => sin plantilla, el
        servicio no manda nada."""
        return (self.reengage_template_name or self.whatsapp_reengage_template or "").strip()

    def reengage_active(self) -> bool:
        """¿El re-engagement proactivo está habilitado de punta a punta?

        Doble gate: el flag prendido Y una plantilla aprobada cargada. Cualquiera de los
        dos en falso => NO-OP seguro (el servicio no manda nada)."""
        return bool(self.reengage_enabled) and bool(self.reengage_template())

    def optout_keywords(self) -> set[str]:
        """Palabras de baja normalizadas (trim + casefold) para comparar contra el inbound.

        Vacío => set vacío (no se detecta opt-out). El caller normaliza el mensaje igual
        antes de comparar, así el match es case-insensitive y tolera espacios."""
        return {
            kw.strip().casefold()
            for kw in (self.reengage_optout_keywords or "").split(",")
            if kw.strip()
        }

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
