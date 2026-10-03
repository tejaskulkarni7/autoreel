from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from autoreel.config import SECRET_MASK
from autoreel.db import Account, Clip, PublishJob, Task, Video, select, session_scope


@pytest.fixture
def client(data_dir):
    from autoreel.main import create_app

    with TestClient(create_app()) as c:
        yield c


def make_ready_clips(n=3, video_id=None):
    with session_scope() as s:
        v = Video(title="Pod", path="/x.mp4", duration=600, width=1920, height=1080, status="ready")
        s.add(v)
        s.commit()
        clips = []
        for i in range(n):
            c = Clip(video_id=v.id, rank=i + 1, start=i * 60, end=i * 60 + 30, title=f"C{i}", score=50 + i * 10,
                     status="ready", file_path=f"/tmp/c{i}.mp4")
            s.add(c)
            clips.append(c)
        s.commit()
        return v, clips


def add_account(platform="youtube", enabled=True):
    with session_scope() as s:
        a = Account(platform=platform, name=platform, enabled=enabled, credentials={})
        s.add(a)
        s.commit()
        return a


def test_upload_creates_video_and_task(client, sample_video):
    with sample_video.open("rb") as f:
        r = client.post("/api/videos", files={"file": ("my talk.mp4", f, "video/mp4")}, data={"title": ""})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["title"] == "my talk" and v["status"] == "queued" and abs(v["duration"] - 12) < 0.5
    assert v["width"] == 640 and v["task"]["kind"] == "process_video"
    assert client.get("/api/videos").json()[0]["id"] == v["id"]
    detail = client.get(f"/api/videos/{v['id']}").json()
    assert detail["clips"] == []
    # Second process request while queued -> conflict
    assert client.post(f"/api/videos/{v['id']}/process", json={"mode": "full"}).status_code == 409
    assert client.post(f"/api/videos/{v['id']}/cancel").json()["canceled"] == 1
    assert client.get(f"/api/videos/{v['id']}").json()["status"] == "canceled"
    assert client.delete(f"/api/videos/{v['id']}").json()["ok"]
    assert client.get("/api/videos").json() == []


def test_upload_rejects_bad_files(client, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    with bad.open("rb") as f:
        assert client.post("/api/videos", files={"file": ("notes.txt", f)}).status_code == 400
    fake = tmp_path / "broken.mp4"
    fake.write_bytes(b"not a video")
    with fake.open("rb") as f:
        assert client.post("/api/videos", files={"file": ("broken.mp4", f)}).status_code == 400


def test_settings_roundtrip_masks_secrets(client):
    cfg = client.get("/api/settings").json()
    assert cfg["llm"]["anthropic_api_key"] == ""
    cfg["llm"]["anthropic_api_key"] = "sk-ant-secret"
    cfg["clips"]["max_clips"] = 4
    out = client.put("/api/settings", json=cfg).json()
    assert out["llm"]["anthropic_api_key"] == SECRET_MASK and out["clips"]["max_clips"] == 4
    # Sending the mask back keeps the stored key
    out["clips"]["max_clips"] = 5
    client.put("/api/settings", json=out)
    from autoreel.db import load_config

    stored = load_config()
    assert stored.llm.anthropic_api_key == "sk-ant-secret" and stored.clips.max_clips == 5
    bad = client.get("/api/settings").json()
    bad["video"]["layout"] = "sideways"
    assert client.put("/api/settings", json=bad).status_code == 422
    reset = client.post("/api/settings/reset").json()
    assert reset["clips"]["max_clips"] == 8 and reset["llm"]["anthropic_api_key"] == SECRET_MASK


def test_publish_schedules_clips_at_intervals(client):
    v, clips = make_ready_clips(3)
    yt, ig = add_account("youtube"), add_account("instagram")
    add_account("tiktok", enabled=False)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
    r = client.post("/api/publish", json={"video_id": v.id, "interval_minutes": 5, "start_at": start.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["jobs"] == 6 and body["clips"] == 3
    jobs = client.get("/api/publish/jobs").json()
    by_clip = {}
    for j in jobs:
        by_clip.setdefault(j["clip"]["title"], set()).add((j["platform"], j["scheduled_at"]))
    # highest score first (C2), then C1, C0; each slot hits both enabled accounts
    assert {p for p, _ in by_clip["C2"]} == {"youtube", "instagram"}
    times = {t for _, t in by_clip["C2"]} | {t for _, t in by_clip["C1"]} | {t for _, t in by_clip["C0"]}
    parsed = sorted(datetime.fromisoformat(t) for t in times)
    assert parsed == [start, start + timedelta(minutes=5), start + timedelta(minutes=10)]
    assert datetime.fromisoformat(next(iter(by_clip["C2"]))[1]) == start
    # Re-publishing the same clips is a no-op unless forced
    assert client.post("/api/publish", json={"video_id": v.id}).status_code == 400
    forced = client.post("/api/publish", json={"clip_ids": [clips[0].id], "account_ids": [yt.id],
                                               "include_already_posted": True}).json()
    assert forced["jobs"] == 1
    # Cancel a whole batch
    assert client.post(f"/api/publish/batches/{body['batch_id']}/cancel").json()["canceled"] == 6
    _ = ig


def test_publish_skips_unapproved_and_requires_accounts(client):
    v, clips = make_ready_clips(2)
    assert client.post("/api/publish", json={"video_id": v.id}).status_code == 400  # no accounts
    add_account("youtube")
    client.patch(f"/api/clips/{clips[0].id}", json={"approved": False})
    body = client.post("/api/publish", json={"video_id": v.id}).json()
    assert body["clips"] == 1


def test_job_cancel_retry_and_pause_shift(client):
    v, clips = make_ready_clips(2)
    add_account("youtube")
    past = datetime.now(timezone.utc) - timedelta(minutes=30)
    client.post("/api/publish", json={"video_id": v.id, "interval_minutes": 3, "start_at": past.isoformat()})
    jobs = sorted(client.get("/api/publish/jobs").json(), key=lambda j: j["scheduled_at"])
    assert client.post("/api/publish/state", json={"paused": True}).json()["paused"] is True
    assert client.post("/api/publish/state", json={"paused": False}).json()["paused"] is False
    shifted = sorted(client.get("/api/publish/jobs").json(), key=lambda j: j["scheduled_at"])
    t0, t1 = (datetime.fromisoformat(j["scheduled_at"]) for j in shifted)
    assert t1 - t0 == timedelta(minutes=3)
    assert abs((t0 - datetime.now(timezone.utc)).total_seconds()) < 5
    jid = jobs[0]["id"]
    assert client.post(f"/api/publish/jobs/{jid}/cancel").json()["ok"]
    assert client.post(f"/api/publish/jobs/{jid}/cancel").status_code == 400
    assert client.post(f"/api/publish/jobs/{jid}/retry").json()["ok"]
    assert client.delete(f"/api/publish/jobs/{jid}").json()["ok"]


def test_clip_patch_queues_rerender_on_timing_change(client):
    v, clips = make_ready_clips(1)
    c = clips[0]
    r = client.patch(f"/api/clips/{c.id}", json={"title": "New", "caption": "Cap", "hashtags": ["#a", "b"]})
    assert r.json()["title"] == "New" and r.json()["hashtags"] == ["a", "b"]
    with session_scope() as s:
        assert s.exec(select(Task).where(Task.kind == "render_clip")).first() is None
    client.patch(f"/api/clips/{c.id}", json={"start": 5, "end": 40})
    with session_scope() as s:
        tasks = s.exec(select(Task).where(Task.kind == "render_clip")).all()
    assert len(tasks) == 1
    client.patch(f"/api/clips/{c.id}", json={"hook": "New hook"})  # already queued -> no duplicate
    with session_scope() as s:
        assert len(s.exec(select(Task).where(Task.kind == "render_clip")).all()) == 1
    assert client.patch(f"/api/clips/{c.id}", json={"start": 10, "end": 10.5}).status_code == 400
    new = client.post("/api/clips", json={"video_id": v.id, "start": 100, "end": 130, "title": "Manual"}).json()
    assert new["rank"] == 2 and new["status"] == "pending"
    assert client.delete(f"/api/clips/{new['id']}").json()["ok"]


def test_accounts_listing_and_delete(client):
    a = add_account("instagram")
    with session_scope() as s:
        v = Video(title="v")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, start=0, end=10)
        s.add(c)
        s.commit()
        s.add(PublishJob(batch_id="b", clip_id=c.id, account_id=a.id, platform="instagram",
                         scheduled_at=datetime.now(timezone.utc)))
        s.commit()
    data = client.get("/api/accounts").json()
    assert data["accounts"][0]["platform"] == "instagram"
    assert data["setup"]["youtube"]["redirect_uri"].endswith("/api/oauth/youtube/callback")
    assert client.patch(f"/api/accounts/{a.id}", json={"enabled": False}).json()["enabled"] is False
    assert client.delete(f"/api/accounts/{a.id}").json()["ok"]
    with session_scope() as s:
        assert s.exec(select(PublishJob)).all() == []


def test_youtube_client_config_and_tiktok_guard(client):
    r = client.post("/api/accounts/youtube/client-config", files={"file": ("c.json", b'{"nope": 1}')})
    assert r.status_code == 400
    cfg = b'{"web": {"client_id": "id", "client_secret": "s", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token"}}'
    assert client.post("/api/accounts/youtube/client-config", files={"file": ("c.json", cfg)}).json()["ok"]
    url = client.get("/api/accounts/youtube/authorize").json()["url"]
    assert url.startswith("https://accounts.google.com/") and "code_challenge" in url
    assert client.get("/api/accounts/tiktok/authorize").status_code == 400


def test_public_clip_url_and_health(client, sample_video):
    with session_scope() as s:
        v = Video(title="v")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, start=0, end=10, file_path=str(sample_video), status="ready")
        s.add(c)
        s.commit()
    assert client.get("/api/health").json() == {"ok": True}
    r = client.get(f"/public/clips/{c.public_token}.mp4")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert client.get("/public/clips/wrong.mp4").status_code == 404


def test_basic_auth(monkeypatch, data_dir):
    from autoreel import config

    monkeypatch.setenv("AUTOREEL_ADMIN_PASSWORD", "hunter2")
    config.get_server_settings.cache_clear()
    from autoreel.main import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/videos").status_code == 401
        assert c.get("/api/videos", auth=("admin", "nope")).status_code == 401
        assert c.get("/api/videos", auth=("admin", "hunter2")).status_code == 200
        assert c.get("/api/health").status_code == 200
