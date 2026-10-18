"""Tests for the production-hardening features: chunking, rerank, overlap trimming, Ollama,
Anthropic retries, DB migration, retention cleanup, test posts and guards."""

from datetime import timedelta

import pytest
import respx

from autoreel.config import AppConfig, ClipsConfig, LLMConfig
from autoreel.db import (
    Account,
    Clip,
    PublishJob,
    Video,
    load_config,
    save_config,
    select,
    session_scope,
    utcnow,
)
from autoreel.pipeline.highlights import (
    RERANK_PROMPT,
    SYSTEM_PROMPT,
    Word,
    detect_highlights,
    plan_windows,
    postprocess,
    token_windows,
)
from autoreel.pipeline.llm import LLMError, OllamaLLM, estimate_tokens
from autoreel.pipeline.transcribe import flatten_words

from .conftest import fake_transcript


def long_transcript(minutes: int = 60) -> dict:
    """~150 words per minute, one sentence segment every ~4 seconds."""
    segs = []
    t = 0.0
    i = 0
    while t < minutes * 60:
        text = f"This is sentence number {i} and it talks about something quite interesting today."
        words, wt = [], t
        for w in text.split():
            words.append({"start": round(wt, 2), "end": round(wt + 0.3, 2), "word": " " + w})
            wt += 0.32
        segs.append({"start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words})
        t = wt + 0.2
        i += 1
    return {"language": "en", "duration": float(minutes * 60), "segments": segs}


class SmallCtxLLM:
    """Fake local model with a 16K window that answers clip and rerank prompts."""

    context_tokens = 16384
    output_reserve = 3000

    def __init__(self):
        self.clip_calls = []
        self.rerank_calls = []

    def complete_json(self, system, user, schema):
        assert estimate_tokens(system + user) < self.context_tokens - self.output_reserve
        if system == RERANK_PROMPT:
            self.rerank_calls.append(user)
            n = user.count("<candidate ")
            # reverse the order: last candidate becomes the best
            return {"ranking": [{"id": i, "score": 10 + i * 5} for i in range(n)]}
        self.clip_calls.append(user)
        import re

        a, b = (float(x) for x in re.search(r"covering (\d+)s to (\d+)s", user).groups())
        mid = (a + b) / 2
        return {"clips": [
            {"start": mid, "end": mid + 40, "title": f"Clip at {mid:.0f}", "hook": "h", "caption": "c",
             "hashtags": ["x"], "score": 80, "category": "story", "reason": "r"},
        ]}


def test_token_windows_fit_budget_and_overlap():
    t = long_transcript(60)
    budget = 4000
    windows = token_windows(t["segments"], budget, overlap=60)
    assert len(windows) > 3
    assert windows[0][0] == 0.0 and abs(windows[-1][1] - t["segments"][-1]["end"]) < 1e-6
    for (_a1, b1), (a2, _) in zip(windows, windows[1:], strict=False):
        assert a2 < b1 and b1 - a2 >= 50  # ~60s of overlap between neighbours
    for a, b in windows:
        text = "\n".join(f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text']}" for s in t["segments"]
                         if s["end"] >= a and s["start"] <= b)
        assert estimate_tokens(text) <= budget * 1.05


def test_plan_windows_depends_on_context_size():
    t = long_transcript(60)

    class Claude:
        context_tokens = 150_000
        output_reserve = 0

    assert len(plan_windows(t["segments"], 3600, Claude(), LLMConfig())) == 1
    assert len(plan_windows(t["segments"], 3600, SmallCtxLLM(), LLMConfig())) >= 2
    # forcing 10-minute chunks still respects the token budget
    assert len(plan_windows(t["segments"], 3600, Claude(), LLMConfig(chunk_minutes=10))) == 6


def test_one_hour_video_chunked_with_rerank():
    t = long_transcript(60)
    llm = SmallCtxLLM()
    out = detect_highlights(t, llm, LLMConfig(), ClipsConfig(max_clips=3, min_duration=20, max_duration=60))
    assert len(llm.clip_calls) >= 2 and len(llm.rerank_calls) == 1
    assert len(out) == 3
    # rerank flipped the order: the candidate listed last got the highest score
    assert out[0].score > out[1].score > out[2].score
    assert all(20 <= c.duration <= 61 for c in out)


def test_rerank_failure_keeps_first_pass():
    t = long_transcript(60)

    class FlakyRerank(SmallCtxLLM):
        def complete_json(self, system, user, schema):
            if system == RERANK_PROMPT:
                raise LLMError("boom")
            return super().complete_json(system, user, schema)

    out = detect_highlights(t, FlakyRerank(), LLMConfig(), ClipsConfig(max_clips=2, min_duration=20, max_duration=60))
    assert len(out) == 2 and all(c.score == 80 for c in out)


def _words(t):
    return [Word(w["start"], w["end"], w["word"]) for w in flatten_words(t)]


def test_partial_overlap_is_trimmed_at_sentence_boundary():
    t = long_transcript(5)
    words = _words(t)
    cfg = ClipsConfig(min_duration=15, max_duration=60, max_clips=5, pad_start=0, pad_end=0)
    raw = [
        {"start": 60, "end": 100, "score": 90},
        {"start": 85, "end": 140, "score": 70},  # overlaps 85-100 with the first clip
    ]
    out = postprocess(raw, words, 300, cfg)
    assert len(out) == 2
    first, second = sorted(out, key=lambda c: c.start)
    assert second.start >= first.end - 1e-6
    assert any(abs(second.start - s["start"]) < 1e-6 for s in t["segments"])  # starts on a sentence


def test_mostly_overlapping_clip_is_dropped():
    t = long_transcript(5)
    cfg = ClipsConfig(min_duration=15, max_duration=60, max_clips=5)
    out = postprocess([{"start": 60, "end": 100, "score": 90}, {"start": 62, "end": 98, "score": 70}],
                      _words(t), 300, cfg)
    assert len(out) == 1 and out[0].score == 90


def test_trim_that_leaves_too_little_drops_clip():
    t = long_transcript(5)
    cfg = ClipsConfig(min_duration=30, max_duration=60, max_clips=5)
    out = postprocess([{"start": 60, "end": 100, "score": 90}, {"start": 70, "end": 115, "score": 70}],
                      _words(t), 300, cfg)
    assert len(out) == 1


@respx.mock
def test_ollama_sends_context_window_and_schema():
    route = respx.post("http://ollama.test:11434/api/chat").respond(json={
        "message": {"content": '{"clips": []}'}, "prompt_eval_count": 100})
    llm = OllamaLLM(LLMConfig(provider="ollama", ollama_base_url="http://ollama.test:11434/v1",
                              ollama_model="qwen2.5:14b", ollama_num_ctx=32768))
    assert llm.context_tokens == 32768
    assert llm.complete_json("sys", "user", {"type": "object"}) == {"clips": []}
    import json

    body = json.loads(route.calls[0].request.content)
    assert body["options"]["num_ctx"] == 32768 and body["format"] == {"type": "object"} and body["stream"] is False


@respx.mock
def test_ollama_missing_model_message():
    respx.post("http://localhost:11434/api/chat").respond(404, json={"error": "model not found"})
    with pytest.raises(LLMError, match="ollama pull qwen2.5:14b"):
        OllamaLLM(LLMConfig(provider="ollama")).complete_json("s", "u", {})


def test_anthropic_retries_without_rejected_features(monkeypatch):
    import anthropic
    import httpx2

    from autoreel.pipeline.llm import AnthropicLLM

    llm = AnthropicLLM(LLMConfig(anthropic_api_key="sk-test"))
    calls = []

    class Msg:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": '{"clips": [1]}'})()]

    def fake_call(params, use_fallbacks):
        calls.append((use_fallbacks, dict(params["output_config"])))
        resp = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
        if use_fallbacks:
            raise anthropic.BadRequestError("fallbacks: not enabled for this organization", response=resp, body=None)
        if "format" in params["output_config"]:
            raise anthropic.BadRequestError("output_config.format: schema not supported", response=resp, body=None)
        return Msg()

    monkeypatch.setattr(llm, "_call", fake_call)
    assert llm.complete_json("s", "u", {"type": "object"}) == {"clips": [1]}
    assert calls[0][0] is True and calls[1][0] is False and "format" not in calls[2][1]


def test_db_adds_missing_columns(tmp_path, monkeypatch):
    import sqlite3

    from autoreel import config, db

    data = tmp_path / "olddata"
    data.mkdir()
    con = sqlite3.connect(data / "autoreel.db")
    con.execute("CREATE TABLE publishjob (id VARCHAR PRIMARY KEY, batch_id VARCHAR, clip_id VARCHAR, "
                "account_id VARCHAR, platform VARCHAR, scheduled_at DATETIME, status VARCHAR, attempts INTEGER, "
                "external_id VARCHAR, url VARCHAR, error VARCHAR, created_at DATETIME, published_at DATETIME)")
    con.execute("INSERT INTO publishjob (id, status, attempts) VALUES ('j1', 'published', 1)")
    con.commit()
    con.close()
    monkeypatch.setenv("AUTOREEL_DATA_DIR", str(data))
    config.get_server_settings.cache_clear()
    db.reset_engine()
    db.get_engine()
    con = sqlite3.connect(data / "autoreel.db")
    cols = {r[1] for r in con.execute("PRAGMA table_info(publishjob)")}
    assert "test" in cols
    assert con.execute("SELECT test FROM publishjob WHERE id='j1'").fetchone() == (0,)


def _video_with_clip(tmp_path, days_old: int, published_days_ago: int | None):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"x" * 1000)
    clipf = tmp_path / "clip.mp4"
    clipf.write_bytes(b"y" * 100)
    old = utcnow() - timedelta(days=days_old)
    with session_scope() as s:
        v = Video(title="v", path=str(src), status="ready", updated_at=old)
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, start=0, end=10, status="ready", file_path=str(clipf))
        a = Account(platform="youtube", name="yt")
        s.add(c)
        s.add(a)
        s.commit()
        if published_days_ago is not None:
            s.add(PublishJob(batch_id="b", clip_id=c.id, account_id=a.id, platform="youtube", status="published",
                             scheduled_at=old, published_at=utcnow() - timedelta(days=published_days_ago)))
            s.add(PublishJob(batch_id="t", clip_id=c.id, account_id=a.id, platform="youtube", status="failed",
                             scheduled_at=old, test=True))  # test jobs don't block cleanup
            s.commit()
        return v, c, src, clipf


def test_cleanup_removes_old_sources_and_posted_clips(tmp_path):
    from autoreel.cleanup import run_cleanup

    v, c, src, clipf = _video_with_clip(tmp_path, days_old=20, published_days_ago=40)
    result = run_cleanup()
    assert result["sources"] == 1 and result["clips"] == 1
    assert not src.exists() and not clipf.exists()
    with session_scope() as s:
        assert s.get(Video, v.id).source_deleted
        clip = s.get(Clip, c.id)
        assert clip.status == "archived" and clip.file_path is None


def test_cleanup_keeps_recent_and_unposted(tmp_path):
    from autoreel.cleanup import run_cleanup

    _, _, src, clipf = _video_with_clip(tmp_path, days_old=3, published_days_ago=None)
    assert run_cleanup() == {"sources": 0, "clips": 0, "freed_gb": 0.0}
    assert src.exists() and clipf.exists()
    cfg = load_config()
    cfg.storage.delete_source_after_days = 0
    save_config(cfg)
    assert run_cleanup()["sources"] == 0


@pytest.fixture
def client(data_dir):
    from fastapi.testclient import TestClient

    from autoreel.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_test_publish_endpoint_and_exclusion_from_schedule(client, tmp_path):
    f = tmp_path / "c.mp4"
    f.write_bytes(b"0")
    with session_scope() as s:
        v = Video(title="v", path=str(f), status="ready")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, start=0, end=30, status="ready", file_path=str(f), title="Best clip")
        a = Account(platform="instagram", name="ig")
        s.add(c)
        s.add(a)
        s.commit()
    r = client.post(f"/api/accounts/{a.id}/test-publish", json={}).json()
    assert r["clip_title"] == "Best clip"
    jobs = client.get("/api/publish/jobs").json()
    assert jobs[0]["test"] is True
    # A test post doesn't count as "already posted": real publishing still schedules the clip
    assert client.post("/api/publish", json={"video_id": v.id}).json()["jobs"] == 1
    detail = client.get(f"/api/videos/{v.id}").json()
    assert detail["clips"][0]["publish"]["instagram"]["status"] == "scheduled"


@respx.mock
def test_instagram_test_mode_does_not_publish(tmp_path):
    from autoreel.publishers.instagram import InstagramPublisher

    f = tmp_path / "c.mp4"
    f.write_bytes(b"0" * 10)
    with session_scope() as s:
        v = Video(title="v")
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, start=0, end=30, status="ready", file_path=str(f))
        a = Account(platform="instagram", name="ig", external_id="17", credentials={"access_token": "T",
                                                                                   "login_type": "facebook"})
        s.add(c)
        s.add(a)
        s.commit()
    base = "https://graph.facebook.com/v23.0"
    respx.post(f"{base}/17/media").respond(json={"id": "C1", "uri": "https://rupload.facebook.com/x/C1"})
    respx.post("https://rupload.facebook.com/x/C1").respond(json={"success": True})
    respx.get(f"{base}/C1").respond(json={"status_code": "FINISHED"})
    publish = respx.post(f"{base}/17/media_publish").respond(json={"id": "M"})
    pub = InstagramPublisher(a, AppConfig())
    res = pub.publish(c, test=True)
    assert not publish.called and "not published" in res.note


def test_youtube_test_mode_uploads_private(monkeypatch, tmp_path):
    from autoreel.publishers.youtube import YouTubePublisher

    f = tmp_path / "c.mp4"
    f.write_bytes(b"0" * 10)
    clip = Clip(video_id="v", start=0, end=30, title="My clip", file_path=str(f))
    acct = Account(platform="youtube", name="yt", credentials={"refresh_token": "r"})
    captured = {}

    class Req:
        def next_chunk(self, num_retries=0):
            return None, {"id": "vid1", "status": {"privacyStatus": "private"}}

    class Videos:
        def insert(self, part, body, media_body):
            captured["body"] = body
            return Req()

    class Service:
        def videos(self):
            return Videos()

    pub = YouTubePublisher(acct, AppConfig())
    monkeypatch.setattr(pub, "_service", lambda: Service())
    res = pub.publish(clip, test=True)
    assert captured["body"]["status"]["privacyStatus"] == "private"
    assert captured["body"]["snippet"]["title"].startswith("[AutoReel test]")
    assert res.url == "https://www.youtube.com/shorts/vid1"


def test_upload_guards(client, sample_video, monkeypatch):
    cfg = load_config()
    cfg.storage.max_video_minutes = 0  # 12s sample > 0 min
    save_config(cfg)
    with sample_video.open("rb") as fh:
        r = client.post("/api/videos", files={"file": ("a.mp4", fh, "video/mp4")})
    assert r.status_code == 400 and "limit" in r.json()["detail"]
    monkeypatch.setattr("autoreel.api.videos.free_gb", lambda: 0.5)
    with sample_video.open("rb") as fh:
        r = client.post("/api/videos", files={"file": ("a.mp4", fh, "video/mp4")})
    assert r.status_code == 507
    with session_scope() as s:
        assert s.exec(select(Video)).all() == []


def test_source_deleted_blocks_rerender(client, tmp_path):
    with session_scope() as s:
        v = Video(title="v", path=str(tmp_path / "gone.mp4"), status="ready", source_deleted=True, duration=100)
        s.add(v)
        s.commit()
        c = Clip(video_id=v.id, start=0, end=30, status="ready")
        s.add(c)
        s.commit()
    assert client.post(f"/api/videos/{v.id}/process", json={"mode": "redetect"}).status_code == 400
    assert client.post(f"/api/clips/{c.id}/render").status_code == 400
    assert client.patch(f"/api/clips/{c.id}", json={"start": 1, "end": 20}).status_code == 400
    assert client.patch(f"/api/clips/{c.id}", json={"title": "ok"}).status_code == 200


def test_cli_refuses_public_bind_without_password(monkeypatch, data_dir):
    from autoreel import cli, config

    monkeypatch.setenv("AUTOREEL_HOST", "0.0.0.0")
    config.get_server_settings.cache_clear()
    assert cli.main(["serve"]) == 2


def test_youtube_redirect_uses_public_url(monkeypatch, data_dir):
    from fastapi.testclient import TestClient

    from autoreel import config
    from autoreel.main import create_app

    monkeypatch.setenv("AUTOREEL_PUBLIC_BASE_URL", "https://clips.example.com/")
    config.get_server_settings.cache_clear()
    with TestClient(create_app()) as c:
        uri = c.get("/api/accounts").json()["setup"]["youtube"]["redirect_uri"]
    assert uri == "https://clips.example.com/api/oauth/youtube/callback"


def test_short_transcript_single_window_has_no_rerank():
    llm = SmallCtxLLM()
    llm.complete_json = lambda system, user, schema: (
        {"clips": [{"start": 0, "end": 10, "score": 50}]} if system == SYSTEM_PROMPT else pytest.fail("rerank"))
    out = detect_highlights(fake_transcript(60), llm, LLMConfig(), ClipsConfig(min_duration=5, max_duration=30))
    assert len(out) == 1
