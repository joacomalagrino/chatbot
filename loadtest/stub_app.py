#!/usr/bin/env python3
"""Levanta la app REAL para load testing, con Claude y Meta MOCKEADOS.

Por qué mockear: el load test mide la concurrencia del SERVICIO (event loop,
pool de DB, manejo de requests), no la latencia de la API de Anthropic ni de
Graph. Pegarle a las APIs reales además cuesta plata y las rate-limita ELLAS,
contaminando la medición. Acá:

  - get_ai_response / stream_ai_response devuelven texto fijo tras un pequeño
    sleep async (simula el tiempo de "pensar" del modelo SIN red real).
  - los envíos a Meta (WhatsApp/Instagram/template) son no-ops async.
  - la firma del webhook se desactiva (ALLOW_UNSIGNED_WEBHOOKS=1) para poder
    mandar payloads sin HMAC desde el harness.
  - DB = SQLite local (loadtest.db), se recrea limpia en cada arranque.

El AI_DELAY_MS simula la latencia del modelo: subilo para ver cómo el servicio
acumula requests en vuelo (más realista) o bajalo a 0 para medir el techo puro
de la DB/event loop.

USO
  python loadtest/stub_app.py                 # puerto 8000, AI_DELAY_MS=150
  AI_DELAY_MS=0 python loadtest/stub_app.py    # sin latencia simulada de IA
  PORT=8001 python loadtest/stub_app.py

OJO: SQLite serializa escrituras (un solo writer). Para un load test fiel a
prod (Postgres + pool real) apuntá DATABASE_URL a un Postgres de staging ANTES
de importar la app. Igual, SQLite ya revela el efecto del I/O sync bloqueante
en el event loop, que es el hallazgo principal.
"""
import asyncio
import os
import sys

# --- Config de entorno ANTES de importar la app -------------------------------
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DATABASE_URL", "sqlite:///./loadtest.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "loadtest-key")
os.environ.setdefault("ADMIN_API_KEY", "loadtest-admin")
# Webhook SIN firma (solo para este harness local): permite mandar payloads sin HMAC.
os.environ.pop("META_APP_SECRET", None)
os.environ["ALLOW_UNSIGNED_WEBHOOKS"] = "1"
os.environ.setdefault("META_VERIFY_TOKEN", "loadtest-verify")
os.environ.setdefault("META_WHATSAPP_PHONE_ID", "PHONE_LOAD")
os.environ.setdefault("META_INSTAGRAM_ACCOUNT_ID", "IG_LOAD")
os.environ.setdefault("LOG_LEVEL", "WARNING")  # menos ruido bajo carga

AI_DELAY_S = float(os.getenv("AI_DELAY_MS", "150")) / 1000.0
PORT = int(os.getenv("PORT", "8000"))

# Borrar la DB de carga previa para arrancar limpio.
_db_path = "./loadtest.db"
if os.path.exists(_db_path):
    os.remove(_db_path)


def _patch_external_calls() -> None:
    """Reemplaza Claude y Meta por stubs async rápidos (sin red real)."""
    import services.claude_service as claude
    import services.conversation_service as convsvc
    import services.meta_service as meta
    import routers.webhook as webhook

    _REPLY = "Listo, te ayudo con eso. Dejame tu nombre y telefono asi te contactan."

    async def fake_get_ai_response(project, project_config, message, history):
        if AI_DELAY_S:
            await asyncio.sleep(AI_DELAY_S)
        return _REPLY

    async def fake_stream_ai_response(project, project_config, message, history):
        for chunk in _REPLY.split(" "):
            if AI_DELAY_S:
                await asyncio.sleep(AI_DELAY_S / 20)
            yield chunk + " "

    async def fake_send(*args, **kwargs):
        return {"messages": [{"id": "stub"}]}

    # Parchear en TODOS los namespaces que ya importaron los símbolos.
    claude.get_ai_response = fake_get_ai_response
    claude.stream_ai_response = fake_stream_ai_response
    convsvc.get_ai_response = fake_get_ai_response
    convsvc.stream_ai_response = fake_stream_ai_response

    meta.send_whatsapp_message = fake_send
    meta.send_whatsapp_template = fake_send
    meta.send_whatsapp_reply = fake_send
    meta.send_instagram_message = fake_send
    webhook.send_whatsapp_reply = fake_send
    webhook.send_instagram_message = fake_send


def main() -> None:
    _patch_external_calls()
    import uvicorn

    print(f"# stub app en http://127.0.0.1:{PORT}  (AI_DELAY_MS={int(AI_DELAY_S * 1000)}, DB={os.environ['DATABASE_URL']})")
    print("# Claude y Meta MOCKEADOS. Firma de webhook DESACTIVADA. Solo para load testing local.")
    uvicorn.run("main:app", host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
