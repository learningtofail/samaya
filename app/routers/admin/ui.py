from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def admin_home():
    with open("/app/static/admin.html") as f:
        return HTMLResponse(f.read())
