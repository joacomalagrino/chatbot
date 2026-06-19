from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from config import PROJECTS
from ratelimit import limiter
from services.ads_service import generate_ad

router = APIRouter()


class AdRequest(BaseModel):
    project: str = Field(min_length=1, max_length=50)
    brief: str = Field(min_length=1, max_length=1000)
    channel: Literal["facebook", "instagram", "ambos"] = "ambos"


@router.post("/generate")
@limiter.limit("10/minute")
def generate(request: Request, payload: AdRequest):
    if payload.project not in PROJECTS:
        raise HTTPException(status_code=400, detail=f"Proyecto inválido: {payload.project}")

    result = generate_ad(
        project=payload.project,
        project_config=PROJECTS[payload.project],
        brief=payload.brief,
        channel=payload.channel,
    )
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])
    return result
