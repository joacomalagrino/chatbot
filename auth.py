"""Autenticación de endpoints internos (/leads, /ads)."""
import hmac

from fastapi import Header, HTTPException, status

from config import get_settings

settings = get_settings()


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
