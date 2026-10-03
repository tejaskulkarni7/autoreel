from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError
from sqlmodel import Session, select

from ..config import AppConfig, LLMConfig, SubtitleConfig, get_server_settings, mask_secrets, merge_secrets
from ..db import Clip, Task, Video, get_session, load_config, save_config
from ..pipeline.media import FONTS_DIR, escape_filter_value, ffmpeg_available, ffmpeg_bin
from ..pipeline.subtitles import build_ass
from .common import task_out

router = APIRouter(tags=["settings"])


@router.get("/api/settings")
def get_settings(session: Session = Depends(get_session)):
    return mask_secrets(load_config(session))


@router.put("/api/settings")
def put_settings(payload: dict, session: Session = Depends(get_session)):
    try:
        cfg = merge_secrets(payload, load_config(session))
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False))
    save_config(cfg, session)
    return mask_secrets(cfg)


@router.post("/api/settings/reset")
def reset_settings(session: Session = Depends(get_session)):
    old = load_config(session)
    cfg = AppConfig()
    # Keep credentials when resetting the knobs.
    cfg.llm.anthropic_api_key = old.llm.anthropic_api_key
    cfg.llm.openai_api_key = old.llm.openai_api_key
    cfg.publishing.tiktok.client_key = old.publishing.tiktok.client_key
    cfg.publishing.tiktok.client_secret = old.publishing.tiktok.client_secret
    cfg.publishing.tiktok.redirect_uri = old.publishing.tiktok.redirect_uri
    save_config(cfg, session)
    return mask_secrets(cfg)


@router.post("/api/settings/test-llm")
def test_llm(payload: dict | None = None, session: Session = Depends(get_session)):
    from ..pipeline.llm import LLMError, get_llm

    current = load_config(session)
    llm_cfg = current.llm
    if payload:
        merged = merge_secrets({**current.model_dump(), "llm": payload}, current)
        llm_cfg = merged.llm
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "model": {"type": "string"}},
              "required": ["ok", "model"], "additionalProperties": False}
    try:
        out = get_llm(LLMConfig.model_validate(llm_cfg.model_dump())).complete_json(
            "You are a health check.", 'Reply with {"ok": true, "model": "<your model name>"}.', schema)
    except LLMError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"{e.__class__.__name__}: {e}")
    return {"ok": bool(out.get("ok")), "reply": out}


@router.post("/api/settings/preview-subtitles")
def preview_subtitles(payload: dict, session: Session = Depends(get_session)):
    """Render one 540x960 PNG showing the given subtitle style over a frame of the latest video."""
    try:
        sub = SubtitleConfig.model_validate(payload)
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False))
    W, H = 1080, 1920
    words = [{"start": 0.0, "end": 0.4, "word": "This"}, {"start": 0.4, "end": 0.9, "word": "changed"},
             {"start": 0.9, "end": 1.5, "word": "everything"}]
    ass = build_ass(words, 3.0, sub, W, H, hook="The one habit nobody talks about")
    video = session.exec(select(Video).where(Video.path.is_not(None)).order_by(Video.created_at.desc())).first()
    clip = session.exec(select(Clip).where(Clip.video_id == video.id).order_by(Clip.rank)).first() if video else None
    with tempfile.TemporaryDirectory() as tmp:
        ass_path = Path(tmp) / "preview.ass"
        ass_path.write_text(ass, encoding="utf-8")
        out = Path(tmp) / "preview.png"
        subs = f"ass=filename={escape_filter_value(str(ass_path))}:fontsdir={escape_filter_value(str(FONTS_DIR))}"
        if video and video.path and Path(video.path).exists():
            at = (clip.start + 2) if clip else min(30.0, (video.duration or 0) / 3)
            vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,{subs},"
                  f"scale=540:960")
            # Shift timestamps so the frame is rendered at subtitle time 0.6s (after fade-in/pop).
            cmd = [ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", video.path, "-frames:v", "1",
                   "-vf", f"setpts=PTS-STARTPTS+0.6/TB,{vf}", str(out)]
        else:
            cmd = [ffmpeg_bin(), "-y", "-v", "error", "-f", "lavfi", "-i",
                   f"gradients=s={W}x{H}:c0=0x1e293b:c1=0x7c3aed:duration=1", "-ss", "0.6", "-frames:v", "1",
                   "-vf", f"{subs},scale=540:960", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            raise HTTPException(500, f"Preview failed: {proc.stderr[-500:]}")
        return Response(out.read_bytes(), media_type="image/png")


@router.get("/api/system")
def system_status(request: Request, session: Session = Depends(get_session)):
    from ..pipeline.transcribe import cuda_available

    settings = get_server_settings()
    ffmpeg_version = None
    if ffmpeg_available():
        try:
            ffmpeg_version = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True,
                                            timeout=10).stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            pass
    models = sorted(p.name.replace("models--Systran--faster-whisper-", "").replace("models--", "")
                    for p in settings.models_dir.glob("models--*"))
    usage = shutil.disk_usage(settings.data_dir)
    running = session.exec(select(Task).where(Task.status == "running")).first()
    queued = session.exec(select(Task).where(Task.status == "queued")).all()
    workers = getattr(request.app.state, "workers", None)
    try:
        import tiktok_uploader  # noqa: F401

        tiktok_browser = True
    except ImportError:
        tiktok_browser = False
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "ffmpeg": ffmpeg_version,
        "cuda": cuda_available(),
        "whisper_models_cached": models,
        "data_dir": str(settings.data_dir),
        "disk_free_gb": round(usage.free / 1e9, 1),
        "anthropic_key_in_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_key_in_env": bool(os.environ.get("OPENAI_API_KEY")),
        "tiktok_browser_uploader": tiktok_browser,
        "public_base_url": settings.public_base_url,
        "auth_enabled": bool(settings.admin_password),
        "workers_running": bool(workers and workers.tasks.is_alive() and workers.publisher.is_alive()),
        "current_task": task_out(running),
        "queued_tasks": len(queued),
    }

