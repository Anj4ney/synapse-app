import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import models  # noqa: F401 (ensures models are registered before create_all)
from .database import Base, engine, ensure_schema_upgrades
from .routers import auth, courses, leaderboard

# Vercel's docs specifically recommend basing file paths on the working
# directory (the project root) rather than __file__ for the Python runtime.
FRONTEND_INDEX = os.path.join(os.getcwd(), "index.html")

# Upgrades pre-existing databases with any newly added columns (additive
# ALTERs only) before create_all() — which by itself only creates missing
# tables, not missing columns.
ensure_schema_upgrades()
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Synapse API")

_allowed = os.environ.get("ALLOWED_ORIGINS", "*")
origins = ["*"] if _allowed == "*" else [o.strip() for o in _allowed.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(courses.router)
app.include_router(courses.notes_router)   # /api/notes/... (personal notes, Feature 7)
app.include_router(courses.shared_router)  # /api/shared/... (public read-only view, Feature 12)
app.include_router(leaderboard.router)     # /api/leaderboard (Feature 13)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/share/{share_id}")
def serve_shared_frontend(share_id: str):
    """Serves the single-page frontend for public /share/{id} links when the
    app itself hosts the frontend (local dev / non-Vercel deploys). On Vercel
    the vercel.json rewrite maps /share/* to the static index.html instead."""
    if not os.path.isfile(FRONTEND_INDEX):
        return {
            "error": "index.html not found",
            "looked_in": FRONTEND_INDEX,
            "cwd": os.getcwd(),
            "cwd_contents": os.listdir(os.getcwd()),
        }
    return FileResponse(FRONTEND_INDEX)


@app.get("/")
def serve_frontend():
    if not os.path.isfile(FRONTEND_INDEX):
        return {
            "error": "index.html not found",
            "looked_in": FRONTEND_INDEX,
            "cwd": os.getcwd(),
            "cwd_contents": os.listdir(os.getcwd()),
        }
    return FileResponse(FRONTEND_INDEX)
