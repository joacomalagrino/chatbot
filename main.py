import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text

from auth import require_admin
from config import get_settings
from database import engine, init_db
from observability import configure_logging
from ratelimit import limiter
from services import meta_service
from routers.ads import router as ads_router
from routers.chat import router as chat_router
from routers.leads import router as leads_router
from routers.reengage import router as reengage_router
from routers.webhook import router as webhook_router

# Logging estructurado (timestamp/nivel/módulo, nivel por LOG_LEVEL) lo antes posible, para
# que cualquier log del arranque (incl. fallos de init_db) salga ya con el formato consistente.
configure_logging()

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        init_db()
    except Exception:
        logger.exception("DB init failed (app still starts)")
    yield
    await meta_service.close_client()


# En prod la doc interactiva queda cerrada; se abre solo en dev (CHATBOT_DEV=1).
app = FastAPI(
    title="Chatbot Service",
    lifespan=lifespan,
    docs_url="/docs" if settings.dev else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.dev else None,
)

# Rate limiting (slowapi) por IP.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Comprime respuestas (JSON de la API, HTML del panel, widget.js).
app.add_middleware(GZipMiddleware, minimum_size=512)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list(),
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


# CSP del panel admin: scripts y estilos SOLO del propio origen (sin inline -> bloquea
# XSS), nada de framing. Los anchos dinámicos del dashboard se setean por CSSOM
# (element.style.width), que la CSP no bloquea; no quedan atributos style="" inline.
_ADMIN_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
)


@app.middleware("http")
async def security_and_cache_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    path = request.url.path
    # El widget.js se carga en cada pageview de los sitios cliente -> cachear.
    if path.startswith("/widget"):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
    # CSP estricta solo para el panel (no global: rompería /docs).
    if path.startswith("/admin"):
        response.headers.setdefault("Content-Security-Policy", _ADMIN_CSP)
    return response

app.include_router(chat_router, prefix="/chat", tags=["chat"])
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])
# /leads y /ads son internos: requieren ADMIN_API_KEY (fail-closed).
app.include_router(
    leads_router, prefix="/leads", tags=["leads"], dependencies=[Depends(require_admin)]
)
app.include_router(
    ads_router, prefix="/ads", tags=["ads"], dependencies=[Depends(require_admin)]
)
# /reengage: trigger del re-engagement proactivo. Interno: misma auth admin (fail-closed).
app.include_router(
    reengage_router, prefix="/reengage", tags=["reengage"], dependencies=[Depends(require_admin)]
)

app.mount("/widget", StaticFiles(directory="widget"), name="widget")
# Panel de administración (HTML estático; la auth la hace cada llamada a la API con el token).
app.mount("/admin", StaticFiles(directory="admin", html=True), name="admin")


@app.get("/health")
def health():
    """Liveness: el proceso responde. Lo usa el healthcheck de Railway."""
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    """Readiness: además verifica que la DB responda (no apto para liveness:
    devuelve 503 si Postgres está caído)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(status_code=503, detail="DB no disponible")
    return {"status": "ready"}


if __name__ == "__main__":
    # reload solo para desarrollo local: CHATBOT_DEV=1
    import os
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=os.getenv("CHATBOT_DEV") == "1")
