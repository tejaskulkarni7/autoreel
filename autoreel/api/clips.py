from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import Clip, PublishJob, Task, Video, get_session, utcnow
from ..pipeline.highlights import clean_hashtags
from ..pipeline.runner import _delete_clip_files
from .common import clip_out, enqueue, get_or_404

router = APIRouter(prefix="/api/clips", tags=["clips"])


def _queue_render(session: Session, clip: Clip) -> None:
    pending = session.exec(select(Task).where(Task.clip_id == clip.id, Task.kind == "render_clip",
                                              Task.status == "queued")).first()
    if not pending:
        enqueue(session, "render_clip", video_id=clip.video_id, clip_id=clip.id)


class ClipPatch(BaseModel):
    title: str | None = None
    hook: str | None = None
    caption: str | None = None
    hashtags: list[str] | None = None
    approved: bool | None = None
    start: float | None = None
    end: float | None = None


@router.patch("/{clip_id}")
def patch_clip(clip_id: str, patch: ClipPatch, session: Session = Depends(get_session)):
    clip = get_or_404(session, Clip, clip_id)
    needs_render = False
    if patch.title is not None:
        clip.title = patch.title.strip()[:100]
    if patch.caption is not None:
        clip.caption = patch.caption.strip()
    if patch.hashtags is not None:
        clip.hashtags = clean_hashtags(patch.hashtags, 30)
    if patch.approved is not None:
        clip.approved = patch.approved
    if patch.hook is not None and patch.hook.strip() != clip.hook:
        clip.hook = patch.hook.strip()[:80]
        needs_render = True
    if patch.start is not None or patch.end is not None:
        video = session.get(Video, clip.video_id)
        start = clip.start if patch.start is None else max(0.0, patch.start)
        end = clip.end if patch.end is None else min(video.duration or patch.end, patch.end)
        if end - start < 1.0:
            raise HTTPException(400, "Clip must be at least 1 second long")
        if end - start > 180:
            raise HTTPException(400, "Shorts/Reels/TikToks from this app are capped at 180 seconds")
        if (start, end) != (clip.start, clip.end):
            clip.start, clip.end = round(start, 3), round(end, 3)
            needs_render = True
    clip.updated_at = utcnow()
    session.add(clip)
    session.commit()
    if needs_render:
        _queue_render(session, clip)
    return clip_out(clip)


@router.post("/{clip_id}/render")
def render_clip(clip_id: str, session: Session = Depends(get_session)):
    clip = get_or_404(session, Clip, clip_id)
    _queue_render(session, clip)
    return {"ok": True}


class NewClip(BaseModel):
    video_id: str
    start: float
    end: float
    title: str = ""
    hook: str = ""
    caption: str = ""
    hashtags: list[str] = []


@router.post("")
def create_clip(req: NewClip, session: Session = Depends(get_session)):
    video = get_or_404(session, Video, req.video_id)
    if not 1.0 <= req.end - req.start <= 180:
        raise HTTPException(400, "Clip length must be between 1 and 180 seconds")
    if req.start < 0 or (video.duration and req.end > video.duration + 0.5):
        raise HTTPException(400, "Clip is outside the video")
    ranks = [c.rank for c in session.exec(select(Clip).where(Clip.video_id == video.id)).all()]
    clip = Clip(video_id=video.id, rank=max(ranks, default=0) + 1, start=req.start, end=req.end,
                title=req.title.strip() or "Custom clip", hook=req.hook.strip(), caption=req.caption.strip(),
                hashtags=clean_hashtags(req.hashtags, 30), score=0, category="other", reason="Added manually")
    session.add(clip)
    session.commit()
    _queue_render(session, clip)
    return clip_out(clip)


@router.delete("/{clip_id}")
def delete_clip(clip_id: str, session: Session = Depends(get_session)):
    clip = get_or_404(session, Clip, clip_id)
    jobs = session.exec(select(PublishJob).where(PublishJob.clip_id == clip_id)).all()
    if any(j.status == "running" for j in jobs):
        raise HTTPException(409, "This clip is being uploaded right now")
    for j in jobs:
        session.delete(j)
    session.flush()  # children first: no ORM relationships to order the deletes for us
    for t in session.exec(select(Task).where(Task.clip_id == clip_id, Task.status == "queued")).all():
        session.delete(t)
    _delete_clip_files(clip)
    session.delete(clip)
    session.commit()
    return {"ok": True}
