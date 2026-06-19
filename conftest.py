"""Config de tests: raíz en sys.path + variables de entorno de prueba.

Usa setdefault para que CI pueda pisar los valores si hace falta. La DB de
test es SQLite (los modelos usan el tipo Uuid genérico, compatible)."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_chatbot.db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin")
os.environ.setdefault("META_APP_SECRET", "test-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify")
os.environ.setdefault("META_WHATSAPP_PHONE_ID", "PHONE123")
os.environ.setdefault("META_INSTAGRAM_ACCOUNT_ID", "IG123")
