"""Retention cleanup: keeps the data disk from filling up with old sources and posted clips."""

from __future__ import annotations

import logging
import shutil
from datetime import timedelta
from pathlib import Path

from sqlmodel import select

from .config import get_server_settings
from .db import Clip, PublishJob, Task, Video, load_config, session_scope, utcnow

log = logging.getLogger(__name__)


def free_gb() -> float:
    return shutil.disk_usage(get_server_settings().data_dir).free / 1e9


def run_cleanup() -> dict:
    cfg = load_config().storage
    now = utcnow()
    removed_sources = removed_clips = 0
    freed = 0
    with session_scope() as s:
        if cfg.delete_source_after_days > 0:
            cutoff = now - timedelta(days=cfg.delete_source_after_days)
            busy = {t.video_id for t in s.exec(select(Task).where(Task.status.in_(["queued", "running"]))).all()}
            for v in s.exec(select(Video).where(Video.status == "ready", Video.source_deleted.is_(False))).all():
                if v.id in busy or v.updated_at > cutoff or not v.path:
                    continue
                path = Path(v.path)
                if path.exists():
                    freed += path.stat().st_size
                    path.unlink()
                v.source_deleted = True
                s.add(v)
                removed_sources += 1
        if cfg.delete_published_clips_after_days > 0:
            cutoff = now - timedelta(days=cfg.delete_published_clips_after_days)
            for c in s.exec(select(Clip).where(Clip.status == "ready")).all():
                jobs = [j for j in s.exec(select(PublishJob).where(PublishJob.clip_id == c.id)).all() if not j.test]
                if not jobs or any(j.status != "published" for j in jobs if j.status != "canceled"):
                    continue
                published = [j.published_at for j in jobs if j.published_at]
                if not published or max(published) > cutoff:
                    continue
                if c.file_path and Path(c.file_path).exists():
                    freed += Path(c.file_path).stat().st_size
                    Path(c.file_path).unlink()
                c.file_path, c.status = None, "archived"  # thumbnail is kept for the UI
                s.add(c)
                removed_clips += 1
        s.commit()
    if removed_sources or removed_clips:
        log.info("Cleanup removed %s source videos and %s published clips (%.1f GB)",
                 removed_sources, removed_clips, freed / 1e9)
    return {"sources": removed_sources, "clips": removed_clips, "freed_gb": round(freed / 1e9, 2)}
