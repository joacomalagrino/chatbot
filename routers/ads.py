from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from services.ads_service import generate_ad
from config import PROJECTS

router = APIRouter()


class AdRequest(BaseModel):
    project: str
    brief: str
    channel: str = "ambos"   # facebook | instagram | ambos


@router.post("/generate")
def generate(payload: AdRequest):
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
