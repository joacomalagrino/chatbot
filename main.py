import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from auth import require_admin
from config import get_settings
from database import create_tables
from ratelimit import limiter
from routers.ads import router as ads_router
from routers.chat import router as chat_router
from routers.leads import router as leads_router
from routers.webhook import router as webhook_router

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        create_tables()
    except Exception:
        logger.exception("DB init failed (app still starts)")
    yield


app = FastAPI(title="Chatbot Service", lifespan=lifespan)

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


@app.middleware("http")
async def security_and_cache_headers(request: Request, call_next):
    response = await call_next(request)
    for k, v in _SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    # El widget.js se carga en cada pageview de los sitios cliente -> cachear.
    if request.url.path.startswith("/widget"):
        response.headers.setdefault("Cache-Control", "public, max-age=3600")
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

app.mount("/widget", StaticFiles(directory="widget"), name="widget")
# Panel de administración (HTML estático; la auth la hace cada llamada a la API con el token).
app.mount("/admin", StaticFiles(directory="admin", html=True), name="admin")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
