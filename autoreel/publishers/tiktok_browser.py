"""TikTok via browser automation (the open-source ``tiktok-uploader`` package, Playwright).

Use this when you don't have an audited TikTok API app: it drives a real browser logged in
with *your* cookies and can post publicly. It is unofficial - TikTok UI changes can break
it, and automated posting may violate TikTok's terms. Install with:

    uv pip install -e ".[tiktok-browser]" && playwright install chromium
"""

from __future__ import annotations

from pathlib import Path

from ..config import get_server_settings
from ..db import Clip
from .base import Publisher, PublishError, PublishResult

VISIBILITY = {"PUBLIC_TO_EVERYONE": "everyone", "MUTUAL_FOLLOW_FRIENDS": "friends",
              "FOLLOWER_OF_CREATOR": "friends", "SELF_ONLY": "only_you"}


def cookies_path(account_id: str) -> Path:
    return get_server_settings().secrets_dir / f"tiktok_cookies_{account_id}.txt"


class TikTokBrowserPublisher(Publisher):
    platform = "tiktok_browser"

    def check(self) -> str:
        if not cookies_path(self.account.id).exists():
            raise PublishError("cookies.txt missing - re-add the account")
        try:
            import tiktok_uploader  # noqa: F401
        except ImportError as e:
            raise PublishError('tiktok-uploader not installed: uv pip install -e ".[tiktok-browser]"') from e
        return self.account.name

    def publish(self, clip: Clip, test: bool = False) -> PublishResult:
        try:
            from tiktok_uploader.upload import upload_video
        except ImportError as e:
            raise PublishError('tiktok-uploader not installed: uv pip install -e ".[tiktok-browser]" '
                               "&& playwright install chromium") from e
        cookies = cookies_path(self.account.id)
        if not cookies.exists():
            raise PublishError("cookies.txt missing - re-add the TikTok (browser) account")
        tc = self.cfg.publishing.tiktok
        try:
            failed = upload_video(
                str(clip.file_path),
                description=self.caption(clip),
                cookies=str(cookies),
                visibility="only_you" if test else VISIBILITY.get(tc.privacy_level, "everyone"),
                browser=tc.browser,
                headless=tc.headless,
            )
        except Exception as e:
            raise PublishError(f"Browser upload crashed: {e}", retryable=True) from e
        if failed:
            raise PublishError("tiktok-uploader reported the upload failed (cookies expired or TikTok UI changed)",
                               retryable=True)
        return PublishResult(note="Uploaded via browser automation")
