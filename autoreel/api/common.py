from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import Session, select

from ..config import get_server_settings
from ..db import Account, Clip, PublishJob, Task, Video


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def media_url(path: str | None, version: int = 0) -> str | None:
    if not path:
        return None
    videos_dir = get_server_settings().videos_dir
    try:
        rel = Path(path).resolve().relative_to(videos_dir.resolve())
    except ValueError:
        return None
    return f"/media/videos/{rel.as_posix()}" + (f"?v={version}" if version else "")


def task_out(t: Task | None) -> dict | None:
    if t is None:
        return None
    return {
        "id": t.id, "kind": t.kind, "video_id": t.video_id, "clip_id": t.clip_id, "status": t.status,
        "stage": t.stage, "progress": t.progress, "message": t.message, "error": t.error,
        "created_at": iso(t.created_at), "started_at": iso(t.started_at), "finished_at": iso(t.finished_at),
    }


def clip_out(c: Clip, publish_summary: dict | None = None) -> dict:
    return {
        "id": c.id, "video_id": c.video_id, "rank": c.rank, "start": c.start, "end": c.end,
        "duration": round(c.end - c.start, 2), "title": c.title, "hook": c.hook, "caption": c.caption,
        "hashtags": c.hashtags or [], "score": c.score, "reason": c.reason, "category": c.category,
        "layout": c.layout, "status": c.status, "error": c.error, "approved": c.approved,
        "video_url": media_url(c.file_path, c.render_version), "thumb_url": media_url(c.thumb_path, c.render_version),
        "render_version": c.render_version, "publish": publish_summary or {},
        "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
    }


def video_out(v: Video, clip_count: int = 0, ready_count: int = 0, task: Task | None = None,
              thumb_url: str | None = None) -> dict:
    return {
        "id": v.id, "title": v.title, "source_type": v.source_type, "source_url": v.source_url,
        "filename": v.filename, "duration": v.duration, "width": v.width, "height": v.height, "fps": v.fps,
        "size_bytes": v.size_bytes, "language": v.language, "status": v.status, "stage": v.stage,
        "progress": v.progress, "error": v.error, "source_deleted": v.source_deleted,
        "clip_count": clip_count, "ready_clip_count": ready_count,
        "source_url_media": media_url(v.path), "task": task_out(task), "thumb_url": thumb_url,
        "created_at": iso(v.created_at), "updated_at": iso(v.updated_at),
    }


def account_out(a: Account) -> dict:
    c = a.credentials or {}
    summary = {k: c.get(k) for k in ("expires_at", "refresh_expires_at", "login_type", "scope", "refreshed_at")
               if c.get(k)}
    return {
        "id": a.id, "platform": a.platform, "name": a.name, "external_id": a.external_id, "enabled": a.enabled,
        "last_error": a.last_error, "details": summary, "created_at": iso(a.created_at),
    }


def job_out(j: PublishJob, clip: Clip | None = None, account: Account | None = None) -> dict:
    return {
        "id": j.id, "batch_id": j.batch_id, "clip_id": j.clip_id, "account_id": j.account_id,
        "platform": j.platform, "scheduled_at": iso(j.scheduled_at), "status": j.status, "attempts": j.attempts,
        "external_id": j.external_id, "url": j.url, "error": j.error, "created_at": iso(j.created_at),
        "test": j.test,
        "published_at": iso(j.published_at),
        "clip": {"title": clip.title, "thumb_url": media_url(clip.thumb_path, clip.render_version),
                 "video_id": clip.video_id, "score": clip.score} if clip else None,
        "account": {"name": account.name, "platform": account.platform} if account else None,
    }


def get_or_404(session: Session, model, row_id: str):
    row = session.get(model, row_id)
    if row is None:
        raise HTTPException(404, f"{model.__name__} not found")
    return row


def latest_task(session: Session, video_id: str) -> Task | None:
    return session.exec(
        select(Task).where(Task.video_id == video_id, Task.kind.in_(["process_video", "download"]))
        .order_by(Task.created_at.desc())
    ).first()


def enqueue(session: Session, kind: str, video_id: str | None = None, clip_id: str | None = None,
            **options) -> Task:
    task = Task(kind=kind, video_id=video_id, clip_id=clip_id, options=options)
    session.add(task)
    if kind == "process_video" and video_id:
        v = session.get(Video, video_id)
        if v:
            v.status, v.stage, v.progress, v.error = "queued", None, 0.0, None
            session.add(v)
    session.commit()
    return task


def active_task(session: Session, video_id: str) -> Task | None:
    return session.exec(
        select(Task).where(Task.video_id == video_id, Task.status.in_(["queued", "running"]))
    ).first()
