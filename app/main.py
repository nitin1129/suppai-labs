from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware

from app.db import ensure_indexes, get_db
from app.routers import admin, applications, auth, lab, member
from app.security import verify_signature
from app.services.storage import get_storage


@asynccontextmanager
async def lifespan(_: FastAPI):
    await ensure_indexes(get_db())
    yield


app = FastAPI(title="SuppAI Labs API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://suppai.in", "https://www.suppai.in", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for r in (applications.router, admin.router, auth.router, lab.router, member.router):
    app.include_router(r)


@app.get("/health", tags=["Meta"])
async def health():
    return {"ok": True}


@app.get("/files/{key:path}", include_in_schema=False)
async def local_file(key: str, e: int = Query(...), s: str = Query(...)):
    """Serves stored files, only through a valid, unexpired signed link."""
    if not verify_signature(key, e, s):
        raise HTTPException(403, "Link is invalid or has expired")
    try:
        f = await get_storage().read(key)
    except FileNotFoundError:
        raise HTTPException(404, "File not found")
    return Response(
        f.data,
        media_type=f.content_type,
        headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="{f.filename}"'},
    )
