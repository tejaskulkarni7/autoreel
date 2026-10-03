from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from ..config import get_server_settings
from ..db import Clip, PublishJob, Task, Video, get_session, utcnow
from ..pipeline.media import MediaError, probe
from ..pipeline.transcribe import load_transcript
from .common import active_task, clip_out, enqueue, get_or_404, iso, latest_task, video_out

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/videos", tags=["videos"])

ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".flv", ".wmv", ".ts"}


def publish_summaries(session: Session, clip_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {cid: {} for cid in clip_ids}
    if not clip_ids:
        return out
    for j in session.exec(select(PublishJob).where(PublishJob.clip_id.in_(clip_ids))).all():
        cur = out[j.clip_id].get(j.platform)
        # Show the most meaningful status per platform.
        rank = {"published": 4, "running": 3, "scheduled": 2, "failed": 1, "canceled": 0}
        if cur is None or rank.get(j.status, 0) >= rank.get(cur["status"], 0):
            out[j.clip_id][j.platform] = {"status": j.status, "url": j.url, "scheduled_at": iso(j.scheduled_at),
                                          "error": j.error}
    return out


@router.get("")
def list_videos(session: Session = Depends(get_session)):
    videos = session.exec(select(Video).order_by(Video.created_at.desc())).all()
    counts = dict(session.exec(select(Clip.video_id, func.count(Clip.id)).group_by(Clip.video_id)).all())
    ready = dict(session.exec(select(Clip.video_id, func.count(Clip.id)).where(Clip.status == "ready")
                              .group_by(Clip.video_id)).all())
    return [video_out(v, counts.get(v.id, 0), ready.get(v.id, 0), latest_task(session, v.id)) for v in videos]


def _new_video_dir(video_id: str) -> Path:
    d = get_server_settings().videos_dir / video_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _apply_probe(v: Video, path: Path) -> None:
    info = probe(path)
    v.duration, v.width, v.height, v.fps = info.duration, info.width, info.height, info.fps
    v.size_bytes = path.stat().st_size


@router.post("")
def upload_video(
    file: UploadFile = File(...),
    title: str = Form(""),
    auto_process: bool = Form(True),
    session: Session = Depends(get_session),
):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}")
    video = Video(title=title.strip() or Path(file.filename).stem, filename=file.filename, source_type="upload")
    vdir = _new_video_dir(video.id)
    dest = vdir / f"source{ext}"
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out, 4 * 1024 * 1024)
    video.path = str(dest)
    try:
        _apply_probe(video, dest)
    except MediaError as e:
        shutil.rmtree(vdir, ignore_errors=True)
        raise HTTPException(400, f"Could not read video: {e}")
    session.add(video)
    session.commit()
    if auto_process:
        enqueue(session, "process_video", video_id=video.id, retranscribe=True)
    session.refresh(video)
    return video_out(video, task=latest_task(session, video.id))


class ImportRequest(BaseModel):
    url: str
    title: str = ""
    auto_process: bool = True


@router.post("/import")
def import_video(req: ImportRequest, session: Session = Depends(get_session)):
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http(s)://")
    video = Video(title=req.title.strip() or req.url, source_type="url", source_url=req.url, status="queued")
    session.add(video)
    session.commit()
    enqueue(session, "download", video_id=video.id, auto_process=req.auto_process, title=req.title.strip())
    return video_out(video, task=latest_task(session, video.id))


@router.get("/{video_id}")
def get_video(video_id: str, session: Session = Depends(get_session)):
    v = get_or_404(session, Video, video_id)
    clips = session.exec(select(Clip).where(Clip.video_id == video_id).order_by(Clip.rank)).all()
    pubs = publish_summaries(session, [c.id for c in clips])
    out = video_out(v, len(clips), sum(1 for c in clips if c.status == "ready"), latest_task(session, video_id))
    out["clips"] = [clip_out(c, pubs.get(c.id)) for c in clips]
    out["clip_tasks"] = {
        t.clip_id: t.status
        for t in session.exec(select(Task).where(Task.video_id == video_id, Task.kind == "render_clip",
                                                 Task.status.in_(["queued", "running"]))).all()
    }
    return out


class VideoPatch(BaseModel):
    title: str | None = None


@router.patch("/{video_id}")
def patch_video(video_id: str, patch: VideoPatch, session: Session = Depends(get_session)):
    v = get_or_404(session, Video, video_id)
    if patch.title is not None:
        v.title = patch.title.strip() or v.title
    v.updated_at = utcnow()
    session.add(v)
    session.commit()
    return video_out(v)


class ProcessRequest(BaseModel):
    # full: transcribe + detect + render; redetect: reuse transcript; rerender: keep clips, re-render all
    mode: Literal["full", "redetect", "rerender"] = "redetect"


@router.post("/{video_id}/process")
def process_video(video_id: str, req: ProcessRequest, session: Session = Depends(get_session)):
    v = get_or_404(session, Video, video_id)
    if not v.path:
        raise HTTPException(400, "Video has no file yet")
    if active_task(session, video_id):
        raise HTTPException(409, "This video already has a task queued or running")
    has_transcript = (get_server_settings().videos_dir / video_id / "transcript.json").exists()
    enqueue(session, "process_video", video_id=video_id,
            retranscribe=req.mode == "full" or not has_transcript, rerender_only=req.mode == "rerender")
    return {"ok": True}


@router.post("/{video_id}/cancel")
def cancel_video(video_id: str, session: Session = Depends(get_session)):
    tasks = session.exec(select(Task).where(Task.video_id == video_id, Task.status.in_(["queued", "running"]))).all()
    for t in tasks:
        if t.status == "queued":
            t.status, t.finished_at, t.message = "canceled", utcnow(), "Canceled"
            if t.kind in ("process_video", "download"):
                v = session.get(Video, video_id)
                if v:
                    v.status, v.stage = ("canceled" if v.path else "failed"), None
                    session.add(v)
        else:
            t.cancel_requested = True
        session.add(t)
    session.commit()
    return {"canceled": len(tasks)}


@router.delete("/{video_id}")
def delete_video(video_id: str, session: Session = Depends(get_session)):
    from ..pipeline.runner import remove_video_files

    v = get_or_404(session, Video, video_id)
    for t in session.exec(select(Task).where(Task.video_id == video_id)).all():
        if t.status == "running":
            raise HTTPException(409, "Cancel the running task before deleting this video")
        session.delete(t)
    clip_ids = [c.id for c in session.exec(select(Clip).where(Clip.video_id == video_id)).all()]
    if clip_ids:
        if session.exec(select(PublishJob).where(PublishJob.clip_id.in_(clip_ids),
                                                 PublishJob.status == "running")).first():
            raise HTTPException(409, "A clip of this video is being uploaded right now - try again in a minute")
        for j in session.exec(select(PublishJob).where(PublishJob.clip_id.in_(clip_ids))).all():
            session.delete(j)
        session.flush()  # children first: no ORM relationships to order the deletes for us
        for c in session.exec(select(Clip).where(Clip.video_id == video_id)).all():
            session.delete(c)
        session.flush()
    session.delete(v)
    session.commit()
    remove_video_files(video_id)
    return {"ok": True}


@router.get("/{video_id}/transcript")
def get_transcript(video_id: str, session: Session = Depends(get_session)):
    get_or_404(session, Video, video_id)
    path = get_server_settings().videos_dir / video_id / "transcript.json"
    if not path.exists():
        raise HTTPException(404, "No transcript yet")
    t = load_transcript(path)
    return {"language": t.get("language"), "duration": t.get("duration"),
            "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in t["segments"]]}
