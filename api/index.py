import os
import sys

# Allow importing the `app` package that lives one directory up (project root).
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: E402  (Vercel's Python runtime looks for this ASGI `app`)
