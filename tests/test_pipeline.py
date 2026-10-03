"""End-to-end pipeline run with a fake transcriber and LLM (real ffmpeg rendering)."""

import subprocess

import numpy as np

from autoreel.config import VideoConfig
from autoreel.db import Clip, Task, Video, load_config, save_config, select, session_scope
from autoreel.pipeline.media import escape_filter_value, probe
from autoreel.pipeline.reframe import _pick_target, _smooth, crop_size

from .conftest import FakeLLM, fake_transcript


def test_crop_size_and_targeting():
    assert crop_size(1920, 1080, 1080, 1920) == (608, 1080)
    assert crop_size(1080, 1920, 1080, 1920) == (1080, 1920)
    assert crop_size(1000, 1000, 1080, 1920) == (562, 1000)
    # two faces close together -> frame both
    faces = [(800, 300, 100, 100, 0.9), (950, 300, 100, 100, 0.9)]
    assert _pick_target(faces, None, 608, 1920) == 925
    # far apart -> stick with previous subject
    faces = [(100, 300, 120, 120, 0.9), (1500, 300, 110, 110, 0.9)]
    assert _pick_target(faces, 1555, 608, 1920) == 1555
    assert _pick_target(faces, None, 608, 1920) == 160
    assert _pick_target([], None, 608, 1920) is None
    xs = np.array([0.0] * 10 + [100.0] * 10)
    sm = _smooth(xs, 5)
    assert len(sm) == 20 and 0 < sm[10] < 100 and sm[0] == 0 and sm[-1] == 100


def test_ffmpeg_filter_escaping_with_odd_paths(tmp_path, sample_video):
    """The subtitles filter must accept paths with spaces, quotes, commas and colons."""
    from autoreel.config import SubtitleConfig
    from autoreel.pipeline.subtitles import build_ass

    weird = tmp_path / "dir with 'quote', comma: colon"
    weird.mkdir()
    ass = weird / "subs.ass"
    ass.write_text(build_ass([{"start": 0, "end": 1, "word": "hi"}], 2, SubtitleConfig(), 640, 360))
    out = tmp_path / "o.mp4"
    proc = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(sample_video), "-t", "1",
                           "-vf", f"ass=filename={escape_filter_value(str(ass))}", str(out)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _setup_video(sample_video):
    info = probe(sample_video)
    with session_scope() as s:
        v = Video(title="Sample", path=str(sample_video), duration=info.duration, width=info.width,
                  height=info.height, fps=info.fps)
        s.add(v)
        s.commit()
        t = Task(kind="process_video", video_id=v.id, options={"retranscribe": True})
        s.add(t)
        s.commit()
        return v, t


def test_process_video_end_to_end(monkeypatch, sample_video, data_dir):
    from autoreel.pipeline import runner

    cfg = load_config()
    cfg.clips.min_duration, cfg.clips.max_duration = 3, 8
    cfg.video = VideoConfig(layout="auto", width=360, height=640, preset="ultrafast")
    save_config(cfg)
    monkeypatch.setattr(runner, "transcribe", lambda audio, c, m, progress=None: fake_transcript(12.0))
    llm = FakeLLM([
        {"start": 0.5, "end": 5.0, "title": "Start now", "hook": "Nobody tells you this", "caption": "c",
         "hashtags": ["motivation"], "score": 91, "category": "insightful", "reason": "strong hook"},
        {"start": 6.0, "end": 11.5, "title": "Worst decision", "hook": "", "caption": "c2", "hashtags": [],
         "score": 75, "category": "story", "reason": "regret"},
    ])
    monkeypatch.setattr(runner, "get_llm", lambda c: llm)

    class NoFaces:
        def __init__(self, *a, **k):
            pass

        def detect(self, frame):
            return []

    monkeypatch.setattr(runner, "FaceDetector", NoFaces)
    v, t = _setup_video(sample_video)
    runner.run_task(t.id)

    with session_scope() as s:
        task = s.get(Task, t.id)
        video = s.get(Video, v.id)
        clips = s.exec(select(Clip).where(Clip.video_id == v.id).order_by(Clip.rank)).all()
    assert task.status == "done", task.error
    assert video.status == "ready" and video.progress == 1.0 and video.language == "en"
    assert [c.title for c in clips] == ["Start now", "Worst decision"]
    for c in clips:
        assert c.status == "ready" and c.layout == "blur"  # no faces -> auto picks blur
        info = probe(__import__("pathlib").Path(c.file_path))
        assert (info.width, info.height) == (360, 640) and info.has_audio
        assert 3 <= info.duration <= 9
    assert (data_dir / "videos" / v.id / "transcript.json").exists()

    # Re-render one clip after editing it (face layout with a fake face on the right)
    cfg.video.layout = "face"
    save_config(cfg)

    class RightFace(NoFaces):
        def detect(self, frame):
            h, w = frame.shape[:2]
            return [(w * 0.75, h * 0.3, w * 0.1, w * 0.1, 0.9)]

    monkeypatch.setattr(runner, "FaceDetector", RightFace)
    with session_scope() as s:
        rt = Task(kind="render_clip", video_id=v.id, clip_id=clips[1].id)
        s.add(rt)
        s.commit()
    runner.run_task(rt.id)
    with session_scope() as s:
        c = s.get(Clip, clips[1].id)
        assert s.get(Task, rt.id).status == "done"
    assert c.layout == "face" and c.render_version == 2


def test_process_video_reports_llm_failure(monkeypatch, sample_video):
    from autoreel.pipeline import runner
    from autoreel.pipeline.llm import LLMError

    monkeypatch.setattr(runner, "transcribe", lambda audio, c, m, progress=None: fake_transcript(12.0))

    class Broken:
        def complete_json(self, *a):
            raise LLMError("Anthropic authentication failed")

    monkeypatch.setattr(runner, "get_llm", lambda c: Broken())
    v, t = _setup_video(sample_video)
    runner.run_task(t.id)
    with session_scope() as s:
        assert s.get(Task, t.id).status == "failed"
        video = s.get(Video, v.id)
    assert video.status == "failed" and "authentication" in video.error
