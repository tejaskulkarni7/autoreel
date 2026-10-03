from __future__ import annotations

from ..config import AppConfig
from ..db import Account
from .base import Publisher, PublishError, PublishResult, render_caption

PLATFORMS = {
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
    "tiktok_browser": "TikTok (browser)",
    "instagram": "Instagram Reels",
}


def get_publisher(account: Account, cfg: AppConfig) -> Publisher:
    if account.platform == "youtube":
        from .youtube import YouTubePublisher

        return YouTubePublisher(account, cfg)
    if account.platform == "tiktok":
        from .tiktok import TikTokPublisher

        return TikTokPublisher(account, cfg)
    if account.platform == "tiktok_browser":
        from .tiktok_browser import TikTokBrowserPublisher

        return TikTokBrowserPublisher(account, cfg)
    if account.platform == "instagram":
        from .instagram import InstagramPublisher

        return InstagramPublisher(account, cfg)
    raise PublishError(f"Unknown platform {account.platform}")


__all__ = ["PLATFORMS", "PublishError", "PublishResult", "Publisher", "get_publisher", "render_caption"]
