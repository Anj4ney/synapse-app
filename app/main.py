import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import models  # noqa: F401 (ensures models are registered before create_all)
from .database import Base, engine
from .routers import auth, courses
# index.html lives one directory up from this file (project root: app/../index.html)
FRONTEND_INDEX = os.path.join(os.path.dirname(__file__), "..", "index.html")

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
    return FileResponse(FRONTEND_INDEX)
