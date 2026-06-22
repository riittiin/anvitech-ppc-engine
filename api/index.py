"""Vercel serverless entrypoint.

Vercel's @vercel/python builder serves the module-level ASGI ``app``. We just put
the project root on the path and re-export the FastAPI app from api.main, so the
exact same application (with login + all rules + Gantt) runs on Vercel and locally.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.main import app  # noqa: E402  (re-exported for Vercel)

__all__ = ["app"]
