"""Instagram Reels via the official Instagram Graph API (Content Publishing).

Needs an Instagram Professional (Business or Creator) account and an access token with
``instagram_business_content_publish`` (Instagram Login) or ``instagram_content_publish``
(Facebook Login, IG account linked to a Facebook Page).

Upload modes:
* ``resumable`` - uploads the local file to rupload.facebook.com. Works without exposing
  your machine; documented for Facebook Login tokens (graph.facebook.com).
* ``video_url`` - Instagram downloads the clip from a public URL. Requires
  ``AUTOREEL_PUBLIC_BASE_URL`` (e.g. a Cloudflare tunnel pointing at this app).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import timedelta

from ..config import get_server_settings
from ..db import Clip, parse_dt, utcnow
from .base import Publisher, PublishError, PublishResult, http_client

log = logging.getLogger(__name__)

TRANSIENT_CODES = {1, 2, 4, 17, 32, 341, 613}


def graph_host(login_type: str) -> str:
    return "graph.facebook.com" if login_type == "facebook" else "graph.instagram.com"


def _check(resp, what: str) -> dict:
    try:
        body = resp.json()
    except ValueError:
        raise PublishError(f"Instagram {what}: HTTP {resp.status_code} {resp.text[:300]}",
                           retryable=resp.status_code >= 500)
    err = body.get("error") if isinstance(body, dict) else None
    if err or resp.status_code >= 400:
        err = err or {}
        code = err.get("code")
        retry = bool(err.get("is_transient")) or code in TRANSIENT_CODES or resp.status_code >= 500
        msg = err.get("error_user_msg") or err.get("message") or str(body)
        raise PublishError(f"Instagram {what} failed (code {code}): {msg}", retryable=retry)
    return body


def resolve_account(access_token: str, login_type: str, version: str, ig_user_id: str = "") -> tuple[str, str]:
    """Return (ig_user_id, username) for a token."""
    base = f"https://{graph_host(login_type)}/{version}"
    with http_client(30) as c:
        if login_type == "instagram":
            body = _check(c.get(f"{base}/me", params={"fields": "user_id,username", "access_token": access_token}),
                          "account lookup")
            return str(body.get("user_id") or body.get("id")), body.get("username", "")
        if ig_user_id:
            body = _check(c.get(f"{base}/{ig_user_id}", params={"fields": "id,username", "access_token": access_token}),
                          "account lookup")
            return str(body["id"]), body.get("username", "")
        resp = c.get(f"{base}/me/accounts", params={
            "fields": "name,instagram_business_account{id,username}", "access_token": access_token})
        if resp.status_code < 400:
            for page in resp.json().get("data", []):
                ig = page.get("instagram_business_account")
                if ig:
                    return str(ig["id"]), ig.get("username", "")
        # Page access token instead of a user token
        body = _check(c.get(f"{base}/me", params={
            "fields": "instagram_business_account{id,username}", "access_token": access_token}), "account lookup")
        ig = body.get("instagram_business_account")
        if not ig:
            raise PublishError("No Instagram professional account is linked to this Facebook token/page")
        return str(ig["id"]), ig.get("username", "")


class InstagramPublisher(Publisher):
    platform = "instagram"
    poll_interval = 5.0
    poll_timeout = 900.0

    @property
    def login_type(self) -> str:
        return self.creds.get("login_type", "instagram")

    @property
    def base(self) -> str:
        return f"https://{graph_host(self.login_type)}/{self.cfg.publishing.instagram.graph_api_version}"

    def _token(self) -> str:
        token = self.creds.get("access_token")
        if not token:
            raise PublishError("Instagram account has no access token")
        # Long-lived Instagram-login tokens last 60 days; refreshing extends them.
        refreshed = self.creds.get("refreshed_at")
        if self.login_type == "instagram" and (
            not refreshed or parse_dt(refreshed) < utcnow() - timedelta(days=7)
        ):
            try:
                with http_client(30) as c:
                    resp = c.get("https://graph.instagram.com/refresh_access_token",
                                 params={"grant_type": "ig_refresh_token", "access_token": token})
                if resp.status_code < 400 and resp.json().get("access_token"):
                    token = resp.json()["access_token"]
                    self.save_credentials(access_token=token, refreshed_at=utcnow().isoformat())
            except Exception as e:  # not fatal: token may still be valid
                log.warning("Instagram token refresh failed: %s", e)
        return token

    def check(self) -> str:
        _, username = resolve_account(self._token(), self.login_type,
                                      self.cfg.publishing.instagram.graph_api_version, self.account.external_id or "")
        return username or self.account.name

    def _mode(self) -> str:
        mode = self.cfg.publishing.instagram.upload_mode
        if mode == "auto":
            return "resumable" if self.login_type == "facebook" else "video_url"
        return mode

    def publish(self, clip: Clip, test: bool = False) -> PublishResult:
        token = self._token()
        ig_id = self.account.external_id
        icfg = self.cfg.publishing.instagram
        params = {
            "media_type": "REELS",
            "caption": self.caption(clip),
            "share_to_feed": "true" if icfg.share_to_feed else "false",
            "access_token": token,
        }
        mode = self._mode()
        with http_client(600) as c:
            if mode == "video_url":
                public = get_server_settings().public_base_url.rstrip("/")
                if not public:
                    raise PublishError(
                        "Instagram-login tokens need a public video URL: set AUTOREEL_PUBLIC_BASE_URL (e.g. a "
                        "Cloudflare tunnel) or use a Facebook-login token for resumable uploads")
                params["video_url"] = f"{public}/public/clips/{clip.public_token}.mp4"
                container = _check(c.post(f"{self.base}/{ig_id}/media", data=params), "container create")
            else:
                params["upload_type"] = "resumable"
                container = _check(c.post(f"{self.base}/{ig_id}/media", data=params), "container create")
                upload_uri = container.get("uri") or (
                    f"https://rupload.facebook.com/ig-api-upload/{icfg.graph_api_version}/{container['id']}")
                size = os.path.getsize(clip.file_path)
                with open(clip.file_path, "rb") as f:
                    data = f.read()
                _check(c.post(upload_uri, content=data, headers={
                    "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
                    "Content-Type": "application/octet-stream",
                }), "video upload")
            container_id = container["id"]
            self._wait(c, container_id, token)
            if test:  # Instagram has no private posts: verify upload + processing, then stop
                return PublishResult(external_id=container_id,
                                     note="Test passed: Instagram accepted and processed the video (not published)")
            published = _check(c.post(f"{self.base}/{ig_id}/media_publish",
                                      data={"creation_id": container_id, "access_token": token}), "publish")
            media_id = published.get("id")
            url = None
            try:
                info = _check(c.get(f"{self.base}/{media_id}", params={"fields": "permalink", "access_token": token}),
                              "permalink")
                url = info.get("permalink")
            except PublishError:
                pass
        return PublishResult(external_id=media_id, url=url)

    def _wait(self, c, container_id: str, token: str) -> None:
        deadline = time.time() + self.poll_timeout
        status = {}
        while time.time() < deadline:
            status = _check(c.get(f"{self.base}/{container_id}",
                                  params={"fields": "status_code,status", "access_token": token}), "status")
            code = status.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram could not process the video: {status.get('status') or code}")
            time.sleep(self.poll_interval)
        raise PublishError(f"Timed out waiting for Instagram processing (last: {status.get('status_code')})",
                           retryable=True)
