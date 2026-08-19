import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import models  # noqa: F401 (ensures models are registered before create_all)
from .database import Base, engine
from .routers import auth, courses

# Vercel's docs specifically recommend basing file paths on the working
# directory (the project root) rather than __file__ for the Python runtime.
FRONTEND_INDEX = os.path.join(os.getcwd(), "index.html")

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


@app.get("/api/health")
def health():
    return {"status": "ok"}


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
