from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import httpx

from ..config import AppConfig
from ..db import Account, Clip, update_row


class PublishError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class PublishResult:
    external_id: str | None = None
    url: str | None = None
    note: str | None = None  # non-fatal warning shown in the queue


def hashtag_string(clip: Clip, limit: int) -> str:
    return " ".join(f"#{t}" for t in (clip.hashtags or [])[:limit])


def render_caption(template: str, clip: Clip, limit: int, max_len: int = 2200) -> str:
    values = defaultdict(str, caption=clip.caption or "", hashtags=hashtag_string(clip, limit),
                         title=clip.title or "", hook=clip.hook or "")
    text = template.format_map(values).strip()
    return text[:max_len]


class Publisher:
    platform = ""

    def __init__(self, account: Account, cfg: AppConfig):
        self.account = account
        self.cfg = cfg

    @property
    def creds(self) -> dict:
        return self.account.credentials or {}

    def save_credentials(self, **updates) -> None:
        creds = dict(self.creds)
        creds.update(updates)
        self.account.credentials = creds
        update_row(Account, self.account.id, credentials=creds)

    def caption(self, clip: Clip) -> str:
        p = self.cfg.publishing
        return render_caption(p.caption_template, clip, p.max_hashtags)

    def publish(self, clip: Clip, test: bool = False) -> PublishResult:  # pragma: no cover - interface
        """Post the clip. ``test=True`` posts privately (or uploads without publishing) to check the setup."""
        raise NotImplementedError

    def check(self) -> str:
        """Validate the connection; return a display name. Raise PublishError if broken."""
        return self.account.name


def raise_for_http(resp: httpx.Response, platform: str) -> None:
    if resp.status_code < 400:
        return
    retryable = resp.status_code >= 500 or resp.status_code == 429
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:500]
    raise PublishError(f"{platform} HTTP {resp.status_code}: {body}", retryable=retryable)


def http_client(timeout: float = 120.0) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=20.0), follow_redirects=True)
