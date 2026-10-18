from datetime import timedelta

import httpx
import pytest
import respx

from autoreel.config import AppConfig
from autoreel.db import Account, Clip, PublishJob, Video, session_scope, utcnow
from autoreel.publishers import PublishError, PublishResult, render_caption
from autoreel.publishers.tiktok import API as TT_API
from autoreel.publishers.tiktok import TikTokPublisher, chunk_plan
from autoreel.publishers.youtube import _shorts_title
from autoreel.worker import PublishScheduler


def _clip(tmp_path, **kw) -> Clip:
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"0" * 1024)
    with session_scope() as s:
        v = Video(title="v")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, start=0, end=30, title="Title", caption="Great moment", hashtags=["a", "b", "c"],
                 status="ready", file_path=str(f), **kw)
        s.add(c)
        s.commit()
        return c


def _account(platform, creds=None, external_id=None) -> Account:
    with session_scope() as s:
        a = Account(platform=platform, name=platform, credentials=creds or {}, external_id=external_id)
        s.add(a)
        s.commit()
        return a


def test_caption_and_titles(tmp_path):
    c = _clip(tmp_path)
    assert render_caption("{caption}\n\n{hashtags}", c, 2) == "Great moment\n\n#a #b"
    assert render_caption("{title} {unknown}", c, 2) == "Title"
    assert _shorts_title("Hello <world>", True) == "Hello world #Shorts"
    assert _shorts_title("x" * 99, True) == "x" * 99
    assert _shorts_title("Ends #shorts", True) == "Ends #shorts"


def test_tiktok_chunk_plan():
    assert chunk_plan(3_000_000) == (3_000_000, 1)
    assert chunk_plan(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    size, n = chunk_plan(100 * 1024 * 1024)
    assert size == 10 * 1024 * 1024 and n == 10


@respx.mock
def test_tiktok_direct_post_flow(tmp_path):
    clip = _clip(tmp_path)
    future = (utcnow() + timedelta(hours=10)).isoformat()
    acct = _account("tiktok", {"access_token": "tok", "refresh_token": "r", "expires_at": future,
                               "username": "me"})
    cfg = AppConfig()
    cfg.publishing.tiktok.privacy_level = "PUBLIC_TO_EVERYONE"
    respx.post(f"{TT_API}/post/publish/creator_info/query/").respond(json={
        "data": {"privacy_level_options": ["SELF_ONLY"], "max_video_post_duration_sec": 600},
        "error": {"code": "ok"}})
    init = respx.post(f"{TT_API}/post/publish/video/init/").respond(json={
        "data": {"publish_id": "p1", "upload_url": "https://upload.tiktok.test/u"}, "error": {"code": "ok"}})
    put = respx.put("https://upload.tiktok.test/u").respond(201)
    statuses = iter(["PROCESSING_UPLOAD", "PUBLISH_COMPLETE"])
    respx.post(f"{TT_API}/post/publish/status/fetch/").mock(side_effect=lambda req: httpx.Response(200, json={
        "data": {"status": next(statuses), "publicaly_available_post_id": [123]}, "error": {"code": "ok"}}))
    pub = TikTokPublisher(acct, cfg)
    pub.poll_interval = 0
    res = pub.publish(clip)
    body = init.calls[0].request.content.decode()
    assert '"privacy_level":"SELF_ONLY"' in body.replace(" ", "")  # downgraded to what the app allows
    assert put.calls[0].request.headers["content-range"] == "bytes 0-1023/1024"
    assert res.url == "https://www.tiktok.com/@me/video/123"
    assert "not allowed" in res.note


@respx.mock
def test_tiktok_refreshes_expired_token(tmp_path):
    clip = _clip(tmp_path)
    past = (utcnow() - timedelta(hours=1)).isoformat()
    acct = _account("tiktok", {"access_token": "old", "refresh_token": "r", "expires_at": past,
                               "client_key": "ck", "client_secret": "cs"})
    respx.post(f"{TT_API}/oauth/token/").respond(json={"access_token": "new", "refresh_token": "r2",
                                                       "expires_in": 86400, "open_id": "o"})
    respx.post(f"{TT_API}/post/publish/inbox/video/init/").respond(json={
        "data": {"publish_id": "p1", "upload_url": "https://upload.tiktok.test/u"}, "error": {"code": "ok"}})
    respx.put("https://upload.tiktok.test/u").respond(201)
    respx.post(f"{TT_API}/post/publish/status/fetch/").respond(json={
        "data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
    cfg = AppConfig()
    cfg.publishing.tiktok.mode = "inbox"
    pub = TikTokPublisher(acct, cfg)
    res = pub.publish(clip)
    assert pub.creds["access_token"] == "new"
    with session_scope() as s:
        assert s.get(Account, acct.id).credentials["refresh_token"] == "r2"
    assert "inbox" in res.note


@respx.mock
def test_tiktok_api_error_is_reported(tmp_path):
    clip = _clip(tmp_path)
    acct = _account("tiktok", {"access_token": "tok", "expires_at": (utcnow() + timedelta(hours=5)).isoformat()})
    respx.post(f"{TT_API}/post/publish/creator_info/query/").respond(
        401, json={"data": {}, "error": {"code": "access_token_invalid", "message": "bad token"}})
    with pytest.raises(PublishError) as e:
        TikTokPublisher(acct, AppConfig()).publish(clip)
    assert "access_token_invalid" in str(e.value) and not e.value.retryable


@respx.mock
def test_instagram_resumable_flow(tmp_path):
    from autoreel.publishers.instagram import InstagramPublisher

    clip = _clip(tmp_path)
    acct = _account("instagram", {"access_token": "T", "login_type": "facebook"}, external_id="1789")
    base = "https://graph.facebook.com/v23.0"
    create = respx.post(f"{base}/1789/media").respond(json={"id": "C1", "uri": "https://rupload.facebook.com/x/C1"})
    upload = respx.post("https://rupload.facebook.com/x/C1").respond(json={"success": True})
    states = iter(["IN_PROGRESS", "FINISHED"])
    respx.get(f"{base}/C1").mock(side_effect=lambda r: httpx.Response(200, json={"status_code": next(states)}))
    respx.post(f"{base}/1789/media_publish").respond(json={"id": "M1"})
    respx.get(f"{base}/M1").respond(json={"permalink": "https://instagram.com/reel/abc"})
    pub = InstagramPublisher(acct, AppConfig())
    pub.poll_interval = 0
    res = pub.publish(clip)
    form = create.calls[0].request.content.decode()
    assert "upload_type=resumable" in form and "media_type=REELS" in form
    hdrs = upload.calls[0].request.headers
    assert hdrs["authorization"] == "OAuth T" and hdrs["offset"] == "0" and hdrs["file_size"] == "1024"
    assert res.url == "https://instagram.com/reel/abc" and res.external_id == "M1"


@respx.mock
def test_instagram_video_url_mode_requires_public_url(tmp_path, monkeypatch):
    from autoreel import config
    from autoreel.publishers.instagram import InstagramPublisher

    clip = _clip(tmp_path)
    acct = _account("instagram", {"access_token": "T", "login_type": "instagram",
                                  "refreshed_at": utcnow().isoformat()}, external_id="99")
    with pytest.raises(PublishError, match="AUTOREEL_PUBLIC_BASE_URL"):
        InstagramPublisher(acct, AppConfig()).publish(clip)
    monkeypatch.setenv("AUTOREEL_PUBLIC_BASE_URL", "https://tunnel.example.com")
    config.get_server_settings.cache_clear()
    base = "https://graph.instagram.com/v23.0"
    create = respx.post(f"{base}/99/media").respond(json={"id": "C2"})
    respx.get(f"{base}/C2").respond(json={"status_code": "ERROR", "status": "Error: video too short"})
    with pytest.raises(PublishError, match="too short"):
        InstagramPublisher(acct, AppConfig()).publish(clip)
    assert f"tunnel.example.com%2Fpublic%2Fclips%2F{clip.public_token}" in create.calls[0].request.content.decode()


def test_scheduler_runs_due_jobs_and_retries(tmp_path, monkeypatch):
    clip = _clip(tmp_path)
    good, flaky = _account("youtube"), _account("instagram")
    calls = []

    class FakePublisher:
        def __init__(self, account):
            self.account = account

        def publish(self, c, test=False):
            calls.append(self.account.platform)
            if self.account.platform == "instagram":
                raise PublishError("temporarily down", retryable=True)
            return PublishResult(external_id="v1", url="https://youtu.be/v1")

    monkeypatch.setattr("autoreel.publishers.get_publisher", lambda account, cfg: FakePublisher(account))
    now = utcnow()
    with session_scope() as s:
        for a in (good, flaky):
            s.add(PublishJob(batch_id="b", clip_id=clip.id, account_id=a.id, platform=a.platform,
                             scheduled_at=now - timedelta(seconds=1)))
        s.add(PublishJob(batch_id="b", clip_id=clip.id, account_id=good.id, platform="youtube",
                         scheduled_at=now + timedelta(hours=1)))
        s.commit()
    assert PublishScheduler().tick() == 2
    with session_scope() as s:
        from autoreel.db import select

        jobs = {(j.platform, j.status) for j in s.exec(select(PublishJob)).all()}
        flaky_job = s.exec(select(PublishJob).where(PublishJob.platform == "instagram")).first()
    assert ("youtube", "published") in jobs and ("youtube", "scheduled") in jobs
    assert flaky_job.status == "scheduled" and flaky_job.attempts == 1 and flaky_job.scheduled_at > now
    assert sorted(calls) == ["instagram", "youtube"]


def test_scheduler_respects_pause(tmp_path):
    from autoreel.db import kv_set
    from autoreel.worker import PUBLISH_PAUSED_KEY

    clip = _clip(tmp_path)
    a = _account("youtube")
    with session_scope() as s:
        s.add(PublishJob(batch_id="b", clip_id=clip.id, account_id=a.id, platform="youtube",
                         scheduled_at=utcnow() - timedelta(minutes=1)))
        s.commit()
    kv_set(PUBLISH_PAUSED_KEY, True)
    assert PublishScheduler().tick() == 0
