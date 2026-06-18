from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from database import create_tables
from routers.chat import router as chat_router
from routers.webhook import router as webhook_router
from routers.leads import router as leads_router
from routers.ads import router as ads_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        create_tables()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("DB init failed (app still starts): %s", e)
    yield


app = FastAPI(title="Chatbot Service", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["*"],
)

app.include_router(chat_router, prefix="/chat", tags=["chat"])
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])
app.include_router(leads_router, prefix="/leads", tags=["leads"])
app.include_router(ads_router, prefix="/ads", tags=["ads"])

app.mount("/widget", StaticFiles(directory="widget"), name="widget")


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
