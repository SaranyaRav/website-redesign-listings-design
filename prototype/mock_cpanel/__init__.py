"""A small FastAPI app that mimics the subset of cPanel UAPI used by deploy.py."""

from .app import create_app

__all__ = ["create_app"]
