"""SQLite persistence (SQLModel)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import event
from sqlmodel import JSON, Column, Field, Session, SQLModel, create_engine, select

from .config import AppConfig, get_server_settings


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (SQLModel stores aware datetimes and returns them in UTC)."""
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Video(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    title: str
    source_type: str = "upload"  # upload | url
    source_url: str | None = None
    filename: str | None = None
    path: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    size_bytes: int | None = None
    language: str | None = None
    # uploaded | queued | processing | ready | failed | canceled
    status: str = "uploaded"
    stage: str | None = None
    progress: float = 0.0
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Clip(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    video_id: str = Field(index=True, foreign_key="video.id")
    rank: int = 0
    start: float
    end: float
    title: str = ""
    hook: str = ""
    caption: str = ""
    hashtags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    score: int = 0
    reason: str = ""
    category: str = ""
    layout: str | None = None
    # pending | rendering | ready | failed
    status: str = "pending"
    error: str | None = None
    file_path: str | None = None
    thumb_path: str | None = None
    approved: bool = True
    render_version: int = 0
    # Unguessable token for the public media URL (Instagram video_url uploads).
    public_token: str = Field(default_factory=lambda: secrets.token_urlsafe(24))
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


class Task(SQLModel, table=True):
    """Background work item executed by the worker process (FIFO)."""

    id: str = Field(default_factory=new_id, primary_key=True)
    # process_video | render_clip
    kind: str
    video_id: str | None = Field(default=None, index=True)
    clip_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # queued | running | done | failed | canceled
    status: str = "queued"
    stage: str | None = None
    progress: float = 0.0
    message: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class Account(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    # youtube | tiktok | tiktok_browser | instagram
    platform: str = Field(index=True)
    name: str = ""
    external_id: str | None = None
    credentials: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    enabled: bool = True
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class PublishJob(SQLModel, table=True):
    id: str = Field(default_factory=new_id, primary_key=True)
    batch_id: str = Field(index=True)
    clip_id: str = Field(index=True, foreign_key="clip.id")
    account_id: str = Field(foreign_key="account.id")
    platform: str
    scheduled_at: datetime = Field(index=True)
    # scheduled | running | published | failed | canceled
    status: str = Field(default="scheduled", index=True)
    attempts: int = 0
    external_id: str | None = None
    url: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    published_at: datetime | None = None


class KV(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_server_settings()
        _engine = create_engine(
            f"sqlite:///{settings.db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        SQLModel.metadata.create_all(_engine)
    return _engine


def reset_engine() -> None:
    """Used by tests and by forked/spawned worker processes."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine(), expire_on_commit=False) as session:
        yield session


# ---- app config ---------------------------------------------------------------------

CONFIG_KEY = "app_config"


def load_config(session: Session | None = None) -> AppConfig:
    def _load(s: Session) -> AppConfig:
        row = s.get(KV, CONFIG_KEY)
        return AppConfig.model_validate(row.value) if row else AppConfig()

    if session is not None:
        return _load(session)
    with session_scope() as s:
        return _load(s)


def save_config(cfg: AppConfig, session: Session | None = None) -> None:
    def _save(s: Session) -> None:
        row = s.get(KV, CONFIG_KEY)
        if row is None:
            row = KV(key=CONFIG_KEY, value=cfg.model_dump())
        else:
            row.value = cfg.model_dump()
        s.add(row)
        s.commit()

    if session is not None:
        _save(session)
    else:
        with session_scope() as s:
            _save(s)


def kv_get(key: str, default: Any = None) -> Any:
    with session_scope() as s:
        row = s.get(KV, key)
        return row.value.get("v", default) if row else default


def kv_set(key: str, value: Any) -> None:
    with session_scope() as s:
        row = s.get(KV, key) or KV(key=key)
        row.value = {"v": value}
        s.add(row)
        s.commit()


def update_row(model, row_id: str, **fields) -> None:
    """Small helper for workers: update a few columns of one row and commit."""
    with session_scope() as s:
        row = s.get(model, row_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        if hasattr(row, "updated_at"):
            row.updated_at = utcnow()
        s.add(row)
        s.commit()


__all__ = [
    "KV",
    "Account",
    "Clip",
    "PublishJob",
    "Session",
    "Task",
    "Video",
    "get_engine",
    "get_session",
    "kv_get",
    "kv_set",
    "load_config",
    "new_id",
    "reset_engine",
    "save_config",
    "select",
    "session_scope",
    "update_row",
    "utcnow",
]
