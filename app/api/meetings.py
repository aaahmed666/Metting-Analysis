from fastapi import APIRouter

router = APIRouter(prefix="/meetings", tags=["Meetings"])

@router.get("")
async def get_meetings():
    return {"meetings": []}
