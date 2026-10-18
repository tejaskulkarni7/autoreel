from __future__ import annotations

import json
import logging
import secrets
import urllib.parse
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import get_server_settings
from ..db import Account, Clip, PublishJob, get_session, kv_get, kv_set, load_config, utcnow
from ..publishers import PLATFORMS, PublishError, get_publisher
from .common import account_out, get_or_404

log = logging.getLogger(__name__)
router = APIRouter(tags=["accounts"])

YT_CLIENT_KEY = "youtube_client_config"


def _base_url(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    return base.replace("://127.0.0.1", "://localhost")


def youtube_redirect_uri(request: Request) -> str:
    public = get_server_settings().public_base_url.rstrip("/")
    return f"{public or _base_url(request)}/api/oauth/youtube/callback"


def tiktok_redirect_uri() -> str:
    cfg = load_config()
    if cfg.publishing.tiktok.redirect_uri:
        return cfg.publishing.tiktok.redirect_uri
    public = get_server_settings().public_base_url.rstrip("/")
    return f"{public}/api/oauth/tiktok/callback" if public else ""


def _upsert(session: Session, platform: str, external_id: str | None, name: str, credentials: dict) -> Account:
    acct = None
    if external_id:
        acct = session.exec(select(Account).where(Account.platform == platform,
                                                  Account.external_id == external_id)).first()
    if acct is None:
        acct = Account(platform=platform, external_id=external_id)
    acct.name, acct.credentials, acct.last_error, acct.updated_at = name, credentials, None, utcnow()
    session.add(acct)
    session.commit()
    return acct


def _save_state(state: str, data: dict) -> None:
    kv_set(f"oauth_state:{state}", {**data, "created": utcnow().isoformat()})


def _pop_state(state: str) -> dict:
    data = kv_get(f"oauth_state:{state}")
    if not data:
        raise HTTPException(400, "Unknown or expired OAuth state - start the connection again")
    kv_set(f"oauth_state:{state}", None)
    return data


@router.get("/api/accounts")
def list_accounts(request: Request, session: Session = Depends(get_session)):
    cfg = load_config(session)
    accounts = session.exec(select(Account).order_by(Account.created_at)).all()
    try:
        import tiktok_uploader  # noqa: F401

        has_browser_uploader = True
    except ImportError:
        has_browser_uploader = False
    yt_client = kv_get(YT_CLIENT_KEY)
    return {
        "accounts": [account_out(a) for a in accounts],
        "platforms": PLATFORMS,
        "setup": {
            "youtube": {
                "client_configured": bool(yt_client),
                "client_type": next(iter(yt_client), None) if yt_client else None,
                "redirect_uri": youtube_redirect_uri(request),
            },
            "tiktok": {
                "client_configured": bool(cfg.publishing.tiktok.client_key and cfg.publishing.tiktok.client_secret),
                "redirect_uri": tiktok_redirect_uri(),
                "browser_uploader_installed": has_browser_uploader,
            },
            "instagram": {"public_base_url": get_server_settings().public_base_url},
        },
    }


class AccountPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = None


@router.patch("/api/accounts/{account_id}")
def patch_account(account_id: str, patch: AccountPatch, session: Session = Depends(get_session)):
    acct = get_or_404(session, Account, account_id)
    if patch.enabled is not None:
        acct.enabled = patch.enabled
    if patch.name:
        acct.name = patch.name.strip()
    session.add(acct)
    session.commit()
    return account_out(acct)


@router.delete("/api/accounts/{account_id}")
def delete_account(account_id: str, session: Session = Depends(get_session)):
    acct = get_or_404(session, Account, account_id)
    jobs = session.exec(select(PublishJob).where(PublishJob.account_id == account_id)).all()
    if any(j.status == "running" for j in jobs):
        raise HTTPException(409, "An upload to this account is running")
    for j in jobs:
        session.delete(j)
    session.flush()  # children first: no ORM relationships to order the deletes for us
    session.delete(acct)
    session.commit()
    if acct.platform == "tiktok_browser":
        from ..publishers.tiktok_browser import cookies_path

        cookies_path(account_id).unlink(missing_ok=True)
    return {"ok": True}


class TestPublishRequest(BaseModel):
    clip_id: str | None = None


@router.post("/api/accounts/{account_id}/test-publish")
def test_publish(account_id: str, req: TestPublishRequest, session: Session = Depends(get_session)):
    """Queue a private/test post of one clip to verify the account end to end."""
    acct = get_or_404(session, Account, account_id)
    if req.clip_id:
        clip = get_or_404(session, Clip, req.clip_id)
    else:
        clip = session.exec(select(Clip).where(Clip.status == "ready").order_by(Clip.created_at.desc())).first()
    if clip is None or clip.status != "ready" or not clip.file_path:
        raise HTTPException(400, "No rendered clip available yet - process a video first")
    job = PublishJob(batch_id="test", clip_id=clip.id, account_id=acct.id, platform=acct.platform,
                     scheduled_at=utcnow(), test=True)
    session.add(job)
    session.commit()
    return {"job_id": job.id, "clip_title": clip.title}


@router.post("/api/accounts/{account_id}/test")
def test_account(account_id: str, session: Session = Depends(get_session)):
    acct = get_or_404(session, Account, account_id)
    try:
        name = get_publisher(acct, load_config(session)).check()
    except PublishError as e:
        acct.last_error = str(e)[:500]
        session.add(acct)
        session.commit()
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"{e.__class__.__name__}: {e}")
    session.refresh(acct)
    acct.name, acct.last_error = name or acct.name, None
    session.add(acct)
    session.commit()
    return account_out(acct)


# ---- YouTube --------------------------------------------------------------------------


@router.post("/api/accounts/youtube/client-config")
async def set_youtube_client(file: UploadFile = File(...)):
    try:
        data = json.loads((await file.read()).decode())
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Not a JSON file")
    if not ("web" in data or "installed" in data):
        raise HTTPException(400, "Expected a Google OAuth client JSON ('web' or 'installed' client)")
    kv_set(YT_CLIENT_KEY, data)
    return {"ok": True, "client_type": next(iter(data))}


@router.get("/api/accounts/youtube/authorize")
def youtube_authorize(request: Request):
    from ..publishers.youtube import start_oauth

    client = kv_get(YT_CLIENT_KEY)
    if not client:
        raise HTTPException(400, "Upload your Google OAuth client JSON first")
    redirect = youtube_redirect_uri(request)
    url, state, verifier = start_oauth(client, redirect)
    _save_state(state, {"platform": "youtube", "verifier": verifier, "redirect": redirect})
    return {"url": url}


@router.get("/api/oauth/youtube/callback")
def youtube_callback(request: Request, state: str = "", error: str = "", session: Session = Depends(get_session)):
    from ..publishers.youtube import finish_oauth

    if error:
        return RedirectResponse(f"/accounts?error={urllib.parse.quote(error)}")
    data = _pop_state(state)
    try:
        creds, channel_id, title = finish_oauth(kv_get(YT_CLIENT_KEY), data["redirect"], state, data.get("verifier"),
                                                str(request.url))
    except Exception as e:
        log.exception("YouTube OAuth failed")
        return RedirectResponse(f"/accounts?error={urllib.parse.quote(str(e)[:300])}")
    _upsert(session, "youtube", channel_id, title, creds)
    return RedirectResponse("/accounts?connected=youtube")


# ---- TikTok (official API) ------------------------------------------------------------


@router.get("/api/accounts/tiktok/authorize")
def tiktok_authorize():
    from ..publishers.tiktok import authorize_url

    tc = load_config().publishing.tiktok
    redirect = tiktok_redirect_uri()
    if not (tc.client_key and tc.client_secret):
        raise HTTPException(400, "Set your TikTok client key and secret in Settings -> Publishing first")
    if not redirect:
        raise HTTPException(400, "Set a TikTok redirect URI (HTTPS) in Settings -> Publishing")
    state = secrets.token_urlsafe(16)
    _save_state(state, {"platform": "tiktok", "redirect": redirect})
    return {"url": authorize_url(tc.client_key, redirect, state), "redirect_uri": redirect}


def _finish_tiktok(session: Session, code: str, state: str) -> Account:
    from ..publishers.tiktok import exchange_code, fetch_user

    data = _pop_state(state)
    tc = load_config(session).publishing.tiktok
    creds = exchange_code(tc.client_key, tc.client_secret, code, data["redirect"])
    creds.update(client_key=tc.client_key, client_secret=tc.client_secret)
    try:
        user = fetch_user(creds["access_token"])
    except PublishError:
        user = {}
    return _upsert(session, "tiktok", creds.get("open_id"), user.get("display_name") or "TikTok account", creds)


@router.get("/api/oauth/tiktok/callback")
def tiktok_callback(code: str = "", state: str = "", error: str = "", error_description: str = "",
                    session: Session = Depends(get_session)):
    if error:
        return RedirectResponse(f"/accounts?error={urllib.parse.quote(error_description or error)}")
    try:
        _finish_tiktok(session, code, state)
    except (PublishError, HTTPException) as e:
        msg = e.detail if isinstance(e, HTTPException) else str(e)
        return RedirectResponse(f"/accounts?error={urllib.parse.quote(str(msg)[:300])}")
    return RedirectResponse("/accounts?connected=tiktok")


class TikTokComplete(BaseModel):
    redirect_url: str


@router.post("/api/accounts/tiktok/complete")
def tiktok_complete(req: TikTokComplete, session: Session = Depends(get_session)):
    """For redirect URIs that don't point at this app: paste the URL TikTok redirected to."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(req.redirect_url.strip()).query)
    code, state = (q.get("code") or [""])[0], (q.get("state") or [""])[0]
    if not code or not state:
        raise HTTPException(400, "That URL has no ?code=...&state=... - copy the full address after authorizing")
    try:
        acct = _finish_tiktok(session, code, state)
    except PublishError as e:
        raise HTTPException(400, str(e))
    return account_out(acct)


# ---- TikTok (browser automation) ------------------------------------------------------


@router.post("/api/accounts/tiktok-browser")
async def add_tiktok_browser(name: str = Form(...), cookies: UploadFile = File(...),
                             session: Session = Depends(get_session)):
    from ..publishers.tiktok_browser import cookies_path

    content = await cookies.read()
    if b"tiktok.com" not in content:
        raise HTTPException(400, "This doesn't look like a cookies.txt exported from tiktok.com")
    acct = Account(platform="tiktok_browser", name=name.strip() or "TikTok (browser)", credentials={})
    session.add(acct)
    session.commit()
    path = cookies_path(acct.id)
    path.write_bytes(content)
    path.chmod(0o600)
    return account_out(acct)


# ---- Instagram ------------------------------------------------------------------------


class InstagramConnect(BaseModel):
    access_token: str
    login_type: Literal["instagram", "facebook"] = "instagram"
    ig_user_id: str = ""


@router.post("/api/accounts/instagram")
def add_instagram(req: InstagramConnect, session: Session = Depends(get_session)):
    from ..publishers.instagram import resolve_account

    version = load_config(session).publishing.instagram.graph_api_version
    try:
        ig_id, username = resolve_account(req.access_token.strip(), req.login_type, version, req.ig_user_id.strip())
    except PublishError as e:
        raise HTTPException(400, str(e))
    acct = _upsert(session, "instagram", ig_id, username or "Instagram account", {
        "access_token": req.access_token.strip(), "login_type": req.login_type,
        "refreshed_at": utcnow().isoformat(),
    })
    return account_out(acct)
