import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list(),
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

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
