from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import Account, Clip, PublishJob, get_session, kv_get, kv_set, load_config, new_id, utcnow
from ..worker import PUBLISH_PAUSED_KEY
from .common import get_or_404, iso, job_out

router = APIRouter(prefix="/api/publish", tags=["publish"])


class PublishRequest(BaseModel):
    video_id: str | None = None
    clip_ids: list[str] | None = None
    account_ids: list[str] | None = None  # default: every enabled account
    interval_minutes: float | None = None  # default: settings
    start_at: datetime | None = None  # default: now
    order: str | None = None  # score | chronological | given
    include_already_posted: bool = False


def _to_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def plan_schedule(clips: list[Clip], accounts: list[Account], start: datetime, interval_minutes: float,
                  existing: set[tuple[str, str]], include_already_posted: bool) -> list[PublishJob]:
    """One slot per clip, ``interval_minutes`` apart; each slot posts to every account."""
    batch = new_id()
    jobs: list[PublishJob] = []
    slot = 0
    for clip in clips:
        targets = [a for a in accounts if include_already_posted or (clip.id, a.id) not in existing]
        if not targets:
            continue
        at = start + timedelta(minutes=interval_minutes * slot)
        for a in targets:
            jobs.append(PublishJob(batch_id=batch, clip_id=clip.id, account_id=a.id, platform=a.platform,
                                   scheduled_at=at))
        slot += 1
    return jobs


@router.post("")
def schedule(req: PublishRequest, session: Session = Depends(get_session)):
    cfg = load_config(session)
    if req.clip_ids:
        clips = [c for c in (session.get(Clip, cid) for cid in req.clip_ids) if c is not None]
    elif req.video_id:
        clips = session.exec(select(Clip).where(Clip.video_id == req.video_id)).all()
    else:
        clips = session.exec(select(Clip)).all()
    clips = [c for c in clips if c.status == "ready" and c.approved and c.file_path]
    if not clips:
        raise HTTPException(400, "No approved, rendered clips to publish")
    order = req.order or cfg.publishing.order
    if order == "score":
        clips.sort(key=lambda c: (-c.score, c.rank))
    elif order == "chronological":
        clips.sort(key=lambda c: (c.video_id, c.start))

    if req.account_ids is not None:
        accounts = [a for a in (session.get(Account, aid) for aid in req.account_ids) if a is not None]
    else:
        accounts = session.exec(select(Account).where(Account.enabled.is_(True))).all()
    if not accounts:
        raise HTTPException(400, "No connected accounts selected - connect one on the Accounts page")

    existing = {
        (j.clip_id, j.account_id)
        for j in session.exec(select(PublishJob).where(
            PublishJob.clip_id.in_([c.id for c in clips]),
            PublishJob.status.in_(["scheduled", "running", "published"]))).all()
    }
    interval = cfg.publishing.interval_minutes if req.interval_minutes is None else max(0.0, req.interval_minutes)
    start = _to_utc(req.start_at) if req.start_at else utcnow()
    jobs = plan_schedule(clips, accounts, start, interval, existing, req.include_already_posted)
    if not jobs:
        raise HTTPException(400, "Everything selected is already scheduled or published on those accounts")
    for j in jobs:
        session.add(j)
    session.commit()
    times = sorted(j.scheduled_at for j in jobs)
    return {"batch_id": jobs[0].batch_id, "jobs": len(jobs), "clips": len({j.clip_id for j in jobs}),
            "first_at": iso(times[0]), "last_at": iso(times[-1])}


@router.get("/jobs")
def list_jobs(status: str | None = None, video_id: str | None = None, limit: int = 300,
              session: Session = Depends(get_session)):
    q = select(PublishJob)
    if status:
        q = q.where(PublishJob.status.in_(status.split(",")))
    if video_id:
        q = q.where(PublishJob.clip_id.in_(select(Clip.id).where(Clip.video_id == video_id)))
    jobs = session.exec(q.order_by(PublishJob.scheduled_at.desc()).limit(limit)).all()
    clips = {c.id: c for c in session.exec(select(Clip).where(Clip.id.in_({j.clip_id for j in jobs}))).all()}
    accounts = {a.id: a for a in session.exec(select(Account)).all()}
    return [job_out(j, clips.get(j.clip_id), accounts.get(j.account_id)) for j in jobs]


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, session: Session = Depends(get_session)):
    job = get_or_404(session, PublishJob, job_id)
    if job.status != "scheduled":
        raise HTTPException(400, f"Can't cancel a job that is {job.status}")
    job.status = "canceled"
    session.add(job)
    session.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, session: Session = Depends(get_session)):
    job = get_or_404(session, PublishJob, job_id)
    if job.status not in ("failed", "canceled"):
        raise HTTPException(400, f"Can't retry a job that is {job.status}")
    job.status, job.error, job.scheduled_at, job.attempts = "scheduled", None, utcnow(), 0
    session.add(job)
    session.commit()
    return {"ok": True}


@router.post("/jobs/{job_id}/run-now")
def run_now(job_id: str, session: Session = Depends(get_session)):
    job = get_or_404(session, PublishJob, job_id)
    if job.status != "scheduled":
        raise HTTPException(400, f"Job is {job.status}")
    job.scheduled_at = utcnow()
    session.add(job)
    session.commit()
    return {"ok": True}


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, session: Session = Depends(get_session)):
    job = get_or_404(session, PublishJob, job_id)
    if job.status == "running":
        raise HTTPException(400, "Job is uploading right now")
    session.delete(job)
    session.commit()
    return {"ok": True}


@router.post("/batches/{batch_id}/cancel")
def cancel_batch(batch_id: str, session: Session = Depends(get_session)):
    jobs = session.exec(select(PublishJob).where(PublishJob.batch_id == batch_id,
                                                 PublishJob.status == "scheduled")).all()
    for j in jobs:
        j.status = "canceled"
        session.add(j)
    session.commit()
    return {"canceled": len(jobs)}


class PauseRequest(BaseModel):
    paused: bool


@router.get("/state")
def get_state():
    return {"paused": bool(kv_get(PUBLISH_PAUSED_KEY, False))}


@router.post("/state")
def set_state(req: PauseRequest, session: Session = Depends(get_session)):
    was_paused = bool(kv_get(PUBLISH_PAUSED_KEY, False))
    if was_paused and not req.paused:
        # Shift the remaining schedule so overdue posts keep their spacing instead of all firing at once.
        pending = session.exec(select(PublishJob).where(PublishJob.status == "scheduled")).all()
        now = utcnow()
        earliest = min((j.scheduled_at for j in pending), default=now)
        delay = now - earliest
        if delay > timedelta(0):
            for j in pending:
                j.scheduled_at = j.scheduled_at + delay
                session.add(j)
            session.commit()
    kv_set(PUBLISH_PAUSED_KEY, req.paused)
    return {"paused": req.paused}
