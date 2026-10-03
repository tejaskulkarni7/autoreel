"""FastAPI application: REST API + media files + the built React admin UI."""

from __future__ import annotations

import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from .api import accounts, clips, publish, videos
from .api import settings as settings_api
from .config import get_server_settings
from .db import Clip, get_engine, get_session
from .logging_setup import setup_logging

log = logging.getLogger(__name__)

# Reachable without the admin password: OAuth providers redirect here, Instagram fetches clips here.
PUBLIC_PREFIXES = ("/api/oauth/", "/public/", "/api/health")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    get_engine()
    workers = None
    if os.environ.get("AUTOREEL_DISABLE_WORKERS") != "1":
        from .worker import Workers

        workers = Workers()
        workers.start()
    app.state.workers = workers
    yield
    if workers:
        workers.stop()


def create_app() -> FastAPI:
    settings = get_server_settings()
    app = FastAPI(title="AutoReel", version="0.1.0", lifespan=lifespan)

    if settings.admin_password:

        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path.startswith(PUBLIC_PREFIXES):
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if header.lower().startswith("basic "):
                try:
                    _user, _, pwd = base64.b64decode(header[6:]).decode().partition(":")
                except (ValueError, UnicodeDecodeError):
                    pwd = ""
                if secrets.compare_digest(pwd, settings.admin_password):
                    return await call_next(request)
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="AutoReel"'})

    for r in (videos.router, clips.router, publish.router, accounts.router, settings_api.router):
        app.include_router(r)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/public/clips/{token}.mp4")
    def public_clip(token: str, session: Session = Depends(get_session)):
        clip = session.exec(select(Clip).where(Clip.public_token == token)).first()
        if clip is None or not clip.file_path or not Path(clip.file_path).exists():
            raise HTTPException(404)
        return FileResponse(clip.file_path, media_type="video/mp4")

    app.mount("/media/videos", StaticFiles(directory=settings.videos_dir), name="media")

    dist = settings.frontend_dist
    if (dist / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str):
            if full_path.startswith(("api/", "media/", "public/")):
                raise HTTPException(404)
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        def no_frontend():
            return JSONResponse({
                "message": "API is running, but the web UI is not built yet. Run: cd frontend && npm install && "
                           "npm run build  (or use `npm run dev` on port 5173 during development)",
                "docs": "/docs",
            })

    return app


app = create_app()
