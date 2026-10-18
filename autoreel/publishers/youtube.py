"""YouTube Shorts via the official YouTube Data API v3 (videos.insert).

A Short is simply a vertical video of up to 3 minutes. Notes:
* Each upload costs 1,600 quota units; the default quota is 10,000/day (~6 uploads).
* Projects that haven't passed Google's API audit can only upload *private* videos.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..db import Clip
from .base import Publisher, PublishError, PublishResult

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

# Google sends back extra previously-granted scopes; allow plain-http localhost redirects.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def _allow_local_http(redirect_uri: str) -> None:
    if redirect_uri.startswith("http://localhost") or redirect_uri.startswith("http://127.0.0.1"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def start_oauth(client_config: dict, redirect_uri: str) -> tuple[str, str, str | None]:
    from google_auth_oauthlib.flow import Flow

    _allow_local_http(redirect_uri)
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return url, state, flow.code_verifier


def finish_oauth(client_config: dict, redirect_uri: str, state: str, code_verifier: str | None,
                 authorization_response: str) -> tuple[dict, str, str]:
    """Exchange the code; return (credentials dict, channel id, channel title)."""
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build

    _allow_local_http(redirect_uri)
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri, state=state,
                                   code_verifier=code_verifier, autogenerate_code_verifier=False)
    flow.fetch_token(authorization_response=authorization_response)
    creds = flow.credentials
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    resp = yt.channels().list(part="snippet", mine=True).execute()
    items = resp.get("items") or []
    channel_id = items[0]["id"] if items else ""
    title = items[0]["snippet"]["title"] if items else "YouTube channel"
    return _creds_to_dict(creds), channel_id, title


def _creds_to_dict(creds) -> dict:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or SCOPES),
    }


def _shorts_title(title: str, add_tag: bool) -> str:
    title = (title or "New short").replace("<", "").replace(">", "").strip()
    if add_tag and "#shorts" not in title.lower():
        if len(title) + 8 <= 100:
            return f"{title} #Shorts"
    return title[:100]


class YouTubePublisher(Publisher):
    platform = "youtube"

    def _service(self):
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        c = self.creds
        if not c.get("refresh_token"):
            raise PublishError("YouTube account has no refresh token - reconnect it")
        creds = Credentials(
            token=c.get("token"), refresh_token=c["refresh_token"], token_uri=c.get("token_uri"),
            client_id=c.get("client_id"), client_secret=c.get("client_secret"), scopes=c.get("scopes") or SCOPES,
        )
        if not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                raise PublishError(f"YouTube token refresh failed - reconnect the account ({e})") from e
            self.save_credentials(**_creds_to_dict(creds))
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    def check(self) -> str:
        from googleapiclient.errors import HttpError

        try:
            resp = self._service().channels().list(part="snippet", mine=True).execute()
        except HttpError as e:
            raise PublishError(f"YouTube API error: {e}") from e
        items = resp.get("items") or []
        return items[0]["snippet"]["title"] if items else self.account.name

    def publish(self, clip: Clip, test: bool = False) -> PublishResult:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        yc = self.cfg.publishing.youtube
        privacy = "private" if test else yc.privacy
        title = _shorts_title(clip.title, yc.add_shorts_tag)
        if test:
            title = ("[AutoReel test] " + title)[:100]
        body = {
            "snippet": {
                "title": title,
                "description": self.caption(clip)[:5000],
                "tags": (clip.hashtags or [])[:15],
                "categoryId": yc.category_id,
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": yc.made_for_kids},
        }
        media = MediaFileUpload(str(Path(clip.file_path)), mimetype="video/mp4", chunksize=8 * 1024 * 1024,
                                resumable=True)
        try:
            request = self._service().videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                _, response = request.next_chunk(num_retries=3)
        except HttpError as e:
            status = getattr(e.resp, "status", 0)
            reason = str(e)
            if "quotaExceeded" in reason or "uploadLimitExceeded" in reason:
                raise PublishError("YouTube daily quota/upload limit exceeded - retry tomorrow") from e
            raise PublishError(f"YouTube upload failed ({status}): {reason}", retryable=status >= 500) from e
        vid = response.get("id")
        note = None
        if response.get("status", {}).get("privacyStatus") != privacy:
            note = f"YouTube set privacy to {response.get('status', {}).get('privacyStatus')}"
        if test:
            note = "Test upload (private) - delete it in YouTube Studio when done"
        return PublishResult(external_id=vid, url=f"https://www.youtube.com/shorts/{vid}", note=note)
