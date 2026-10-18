"""TikTok via the official Content Posting API (Direct Post or upload-to-inbox).

Requirements (developers.tiktok.com): an app with Login Kit + Content Posting API, scopes
``video.publish`` (direct) and/or ``video.upload`` (inbox), and an HTTPS redirect URI.
Until TikTok audits the app, direct posts are forced to SELF_ONLY (private) visibility.
"""

from __future__ import annotations

import logging
import math
import os
import time
import urllib.parse
from datetime import timedelta

from ..db import Clip, parse_dt, utcnow
from .base import Publisher, PublishError, PublishResult, http_client, raise_for_http

log = logging.getLogger(__name__)

API = "https://open.tiktokapis.com/v2"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = f"{API}/oauth/token/"
SCOPES = "user.info.basic,video.publish,video.upload"
MAX_SINGLE_CHUNK = 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


def authorize_url(client_key: str, redirect_uri: str, state: str, scopes: str = SCOPES) -> str:
    q = urllib.parse.urlencode({
        "client_key": client_key, "scope": scopes, "response_type": "code",
        "redirect_uri": redirect_uri, "state": state,
    })
    return f"{AUTH_URL}?{q}"


def _token_request(data: dict) -> dict:
    with http_client(60) as c:
        resp = c.post(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                      "Cache-Control": "no-cache"})
    try:
        body = resp.json()
    except ValueError:
        raise PublishError(f"TikTok token endpoint returned HTTP {resp.status_code}", retryable=True)
    if "access_token" not in body:
        raise PublishError(f"TikTok token error: {body.get('error_description') or body.get('error') or body}")
    return body


def _token_fields(body: dict) -> dict:
    now = utcnow()
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token"),
        "open_id": body.get("open_id"),
        "scope": body.get("scope"),
        "expires_at": (now + timedelta(seconds=int(body.get("expires_in", 86400)))).isoformat(),
        "refresh_expires_at": (now + timedelta(seconds=int(body.get("refresh_expires_in", 31536000)))).isoformat(),
    }


def exchange_code(client_key: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    body = _token_request({
        "client_key": client_key, "client_secret": client_secret, "code": code,
        "grant_type": "authorization_code", "redirect_uri": redirect_uri,
    })
    return _token_fields(body)


def fetch_user(access_token: str) -> dict:
    with http_client(30) as c:
        resp = c.get(f"{API}/user/info/", params={"fields": "open_id,display_name,avatar_url"},
                     headers={"Authorization": f"Bearer {access_token}"})
    raise_for_http(resp, "TikTok")
    return (resp.json().get("data") or {}).get("user") or {}


def _api_error(body: dict) -> str | None:
    err = body.get("error") or {}
    if isinstance(err, dict) and err.get("code") not in (None, "ok"):
        return f"{err.get('code')}: {err.get('message')}"
    return None


class TikTokPublisher(Publisher):
    platform = "tiktok"
    poll_interval = 5.0
    poll_timeout = 900.0

    def _token(self) -> str:
        c = self.creds
        expires_at = c.get("expires_at")
        if not c.get("access_token"):
            raise PublishError("TikTok account is not connected")

        if expires_at and parse_dt(expires_at) - timedelta(minutes=10) < utcnow():
            tc = self.cfg.publishing.tiktok
            client_key = c.get("client_key") or tc.client_key
            client_secret = c.get("client_secret") or tc.client_secret
            if not (client_key and client_secret and c.get("refresh_token")):
                raise PublishError("TikTok token expired and cannot be refreshed - reconnect the account")
            body = _token_request({
                "client_key": client_key, "client_secret": client_secret,
                "grant_type": "refresh_token", "refresh_token": c["refresh_token"],
            })
            self.save_credentials(**_token_fields(body))
        return self.creds["access_token"]

    def _post(self, client, path: str, json_body: dict) -> dict:
        resp = client.post(f"{API}{path}", json=json_body, headers={
            "Authorization": f"Bearer {self._token()}", "Content-Type": "application/json; charset=UTF-8"})
        try:
            body = resp.json()
        except ValueError:
            raise_for_http(resp, "TikTok")
            raise PublishError("TikTok returned a non-JSON response", retryable=True)
        err = _api_error(body)
        if err or resp.status_code >= 400:
            retry = resp.status_code >= 500 or "rate_limit" in (err or "")
            raise PublishError(f"TikTok {path} failed: {err or body}", retryable=retry)
        return body.get("data") or {}

    def check(self) -> str:
        user = fetch_user(self._token())
        return user.get("display_name") or self.account.name

    def publish(self, clip: Clip, test: bool = False) -> PublishResult:
        tc = self.cfg.publishing.tiktok
        if test:  # private post: never public, even for audited apps
            tc = tc.model_copy(update={"privacy_level": "SELF_ONLY"})
        size = os.path.getsize(clip.file_path)
        chunk_size, chunks = chunk_plan(size)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk_size, "total_chunk_count": chunks}
        note = None
        with http_client(300) as c:
            if tc.mode == "inbox":
                data = self._post(c, "/post/publish/inbox/video/init/", {"source_info": source})
            else:
                info = self._post(c, "/post/publish/creator_info/query/", {})
                privacy = tc.privacy_level
                options = info.get("privacy_level_options") or []
                if options and privacy not in options:
                    note = f"Privacy {privacy} not allowed for this app/account; used {options[0]}"
                    privacy = "SELF_ONLY" if "SELF_ONLY" in options else options[0]
                max_dur = info.get("max_video_post_duration_sec")
                if max_dur and clip.duration > max_dur:
                    raise PublishError(f"Clip is {clip.duration:.0f}s; this account allows max {max_dur}s")
                post_info = {
                    "title": self.caption(clip),
                    "privacy_level": privacy,
                    "disable_comment": tc.disable_comment or bool(info.get("comment_disabled")),
                    "disable_duet": tc.disable_duet or bool(info.get("duet_disabled")),
                    "disable_stitch": tc.disable_stitch or bool(info.get("stitch_disabled")),
                    "video_cover_timestamp_ms": 1000,
                }
                data = self._post(c, "/post/publish/video/init/", {"post_info": post_info, "source_info": source})
            publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
            if not publish_id or not upload_url:
                raise PublishError(f"TikTok init returned no upload URL: {data}", retryable=True)
            self._upload(c, upload_url, clip.file_path, size, chunk_size, chunks)
            status = self._wait(c, publish_id, inbox=tc.mode == "inbox")
        post_ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
        post_id = str(post_ids[0]) if post_ids else None
        username = self.creds.get("username")
        url = f"https://www.tiktok.com/@{username}/video/{post_id}" if (post_id and username) else None
        if tc.mode == "inbox":
            note = (note + "; " if note else "") + "Sent to TikTok inbox - finish posting in the TikTok app"
        elif test:
            note = (note + "; " if note else "") + "Test post (Only me) - delete it in the TikTok app when done"
        return PublishResult(external_id=post_id or publish_id, url=url, note=note)

    def _upload(self, c, url: str, path: str, size: int, chunk_size: int, chunks: int) -> None:
        with open(path, "rb") as f:
            for i in range(chunks):
                first = i * chunk_size
                last = size - 1 if i == chunks - 1 else first + chunk_size - 1
                f.seek(first)
                data = f.read(last - first + 1)
                resp = c.put(url, content=data, headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {first}-{last}/{size}",
                })
                if resp.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok chunk upload failed: HTTP {resp.status_code} {resp.text[:300]}",
                                       retryable=resp.status_code >= 500)

    def _wait(self, c, publish_id: str, inbox: bool) -> dict:
        deadline = time.time() + self.poll_timeout
        done = {"SEND_TO_USER_INBOX"} if inbox else {"PUBLISH_COMPLETE"}
        last: dict = {}
        while time.time() < deadline:
            last = self._post(c, "/post/publish/status/fetch/", {"publish_id": publish_id})
            st = last.get("status")
            if st in done or (inbox and st == "PUBLISH_COMPLETE"):
                return last
            if st == "FAILED":
                raise PublishError(f"TikTok processing failed: {last.get('fail_reason')}")
            time.sleep(self.poll_interval)
        raise PublishError(f"Timed out waiting for TikTok to process the video (last status {last.get('status')})")


def chunk_plan(size: int) -> tuple[int, int]:
    """Exposed for tests: (chunk_size, total_chunk_count) following TikTok's rules."""
    if size <= MAX_SINGLE_CHUNK:
        return size, 1
    return CHUNK, max(1, math.floor(size / CHUNK))
