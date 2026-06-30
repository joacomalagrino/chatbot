"""Autenticación de endpoints internos (/leads, /ads) y del panel /admin."""
import base64
import binascii
import hmac

from fastapi import Header, HTTPException, status

from config import get_settings

settings = get_settings()


def check_basic_admin(authorization: str) -> bool:
    """Valida un header HTTP Basic contra ADMIN_API_KEY (la contraseña; user ignorado).

    Sirve para gatear el panel estático /admin server-side: el navegador puede
    mandar credenciales Basic en el GET inicial del HTML, cosa que un Bearer en
    JS no puede hacer. Fail-closed: sin ADMIN_API_KEY configurada, deniega todo.
    """
    expected = settings.admin_api_key
    if not expected:
        return False
    if not authorization.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:].strip()).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    _, _, password = decoded.partition(":")
    return bool(password) and hmac.compare_digest(password, expected)


def require_admin(authorization: str = Header(default="")):
    """Exige un Bearer token que coincida con ADMIN_API_KEY.

    Fail-closed: si ADMIN_API_KEY no está configurada, deniega TODO (es más
    seguro que exponer PII de clientes por defecto). Configurá ADMIN_API_KEY
    en las variables de entorno para habilitar estos endpoints.
    """
    expected = settings.admin_api_key
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth no configurada (falta ADMIN_API_KEY)",
        )

    token = ""
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No autorizado",
        )
