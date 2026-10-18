"""Pipeline orchestration for one task (runs inside the worker subprocess)."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlmodel import select

from ..config import get_server_settings
from ..db import Clip, PublishJob, Task, Video, load_config, session_scope, update_row, utcnow
from .highlights import detect_highlights
from .llm import get_llm
from .media import extract_audio, probe
from .reframe import FaceDetector
from .render import render_clip
from .transcribe import flatten_words, load_transcript, save_transcript, transcribe

log = logging.getLogger(__name__)

# Share of the overall progress bar per stage.
STAGES = {"download": (0.0, 1.0), "audio": (0.0, 0.05), "transcribe": (0.05, 0.55), "detect": (0.55, 0.62), "render": (0.62, 1.0)}


class Cancelled(Exception):
    pass


def video_dir(video_id: str) -> Path:
    return get_server_settings().videos_dir / video_id


class Reporter:
    def __init__(self, task_id: str, video_id: str | None):
        self.task_id = task_id
        self.video_id = video_id
        self._last = -1.0

    def __call__(self, stage: str, frac: float, message: str | None = None, force: bool = False) -> None:
        lo, hi = STAGES.get(stage, (0.0, 1.0))
        total = round(lo + (hi - lo) * max(0.0, min(1.0, frac)), 4)
        if not force and abs(total - self._last) < 0.01:
            return
        self._last = total
        fields = {"stage": stage, "progress": total}
        if message is not None:
            fields["message"] = message
        update_row(Task, self.task_id, **fields)
        if self.video_id:
            update_row(Video, self.video_id, stage=stage, progress=total)


def _clip_dir(video_id: str) -> Path:
    return video_dir(video_id) / "clips"


def _delete_clip_files(clip: Clip) -> None:
    for p in (clip.file_path, clip.thumb_path):
        if p:
            Path(p).unlink(missing_ok=True)
    (_clip_dir(clip.video_id) / f"{clip.id}.ass").unlink(missing_ok=True)


def _render_one(clip: Clip, video: Video, words: list[dict], detector: FaceDetector, progress) -> None:
    cfg = load_config()
    settings = get_server_settings()
    update_row(Clip, clip.id, status="rendering", error=None)
    try:
        has_audio = probe(Path(video.path)).has_audio
        result = render_clip(
            Path(video.path), _clip_dir(video.id), clip.id, clip.start, clip.end, video.width, video.height,
            has_audio, words, clip.hook, cfg.video, cfg.subtitles, settings.models_dir, detector, progress,
        )
    except Exception as e:
        update_row(Clip, clip.id, status="failed", error=str(e)[:2000])
        raise
    update_row(Clip, clip.id, status="ready", file_path=str(result.path), thumb_path=str(result.thumb),
               layout=result.layout, render_version=clip.render_version + 1)


def process_video(task: Task, report: Reporter) -> None:
    settings = get_server_settings()
    cfg = load_config()
    with session_scope() as s:
        video = s.get(Video, task.video_id)
    if video is None or not video.path:
        raise RuntimeError("Video not found")
    if video.source_deleted or not Path(video.path).exists():
        raise RuntimeError("The source video was deleted by the storage cleanup - upload it again to regenerate")
    from ..cleanup import free_gb

    if free_gb() < cfg.storage.min_free_gb:
        raise RuntimeError(f"Less than {cfg.storage.min_free_gb:g} GB free on the data disk")
    update_row(Video, video.id, status="processing", error=None)
    vdir = video_dir(video.id)
    vdir.mkdir(parents=True, exist_ok=True)
    opts = task.options or {}

    # 1. transcript (cached unless asked to redo it)
    transcript_path = vdir / "transcript.json"
    if opts.get("retranscribe") or not transcript_path.exists():
        report("audio", 0.0, "Extracting audio", force=True)
        audio = extract_audio(Path(video.path), vdir / "audio.wav")
        report("transcribe", 0.0, f"Transcribing with Whisper '{cfg.transcription.model}'", force=True)
        transcript = transcribe(audio, cfg.transcription, settings.models_dir,
                                progress=lambda f: report("transcribe", f))
        save_transcript(transcript, transcript_path)
        audio.unlink(missing_ok=True)
        update_row(Video, video.id, language=transcript.get("language"))
    else:
        transcript = load_transcript(transcript_path)
    words = flatten_words(transcript)
    if not words:
        raise RuntimeError("No speech found in the video - nothing to clip")

    # 2. highlights (skip when only re-rendering)
    with session_scope() as s:
        existing = s.exec(select(Clip).where(Clip.video_id == video.id)).all()
    if opts.get("rerender_only") and existing:
        clips = sorted(existing, key=lambda c: c.rank)
    else:
        report("detect", 0.0, "Finding the best moments with the LLM", force=True)
        llm = get_llm(cfg.llm)
        cands = detect_highlights(transcript, llm, cfg.llm, cfg.clips, cfg.publishing.max_hashtags,
                                  title=video.title, progress=lambda f: report("detect", f))
        if not cands:
            raise RuntimeError("The LLM did not find any clip-worthy moments")
        with session_scope() as s:
            # Replace old clips, but never ones that already have publish history.
            for old in s.exec(select(Clip).where(Clip.video_id == video.id)).all():
                has_jobs = s.exec(select(PublishJob).where(PublishJob.clip_id == old.id)).first()
                if not has_jobs:
                    _delete_clip_files(old)
                    s.delete(old)
                    s.flush()
            s.commit()
            clips = []
            for i, c in enumerate(cands):
                clip = Clip(video_id=video.id, rank=i + 1, start=c.start, end=c.end, title=c.title, hook=c.hook,
                            caption=c.caption, hashtags=c.hashtags, score=c.score, reason=c.reason,
                            category=c.category)
                s.add(clip)
                clips.append(clip)
            s.commit()

    # 3. render
    detector = FaceDetector(settings.models_dir)
    n = len(clips)
    failures = 0
    for i, clip in enumerate(clips):
        report("render", i / n, f"Rendering clip {i + 1}/{n}", force=True)
        try:
            _render_one(clip, video, words, detector, lambda f, i=i: report("render", (i + f) / n))
        except Exception:
            log.exception("Clip %s failed", clip.id)
            failures += 1
    if failures == n:
        raise RuntimeError("All clips failed to render - see clip errors")
    report("render", 1.0, f"Done: {n - failures} clips ready" + (f", {failures} failed" if failures else ""),
           force=True)
    update_row(Video, video.id, status="ready", stage="done", progress=1.0)


def render_single_clip(task: Task, report: Reporter) -> None:
    settings = get_server_settings()
    with session_scope() as s:
        clip = s.get(Clip, task.clip_id)
        video = s.get(Video, clip.video_id) if clip else None
    if clip is None or video is None:
        raise RuntimeError("Clip not found")
    if video.source_deleted or not video.path or not Path(video.path).exists():
        update_row(Clip, clip.id, status="ready" if clip.file_path else "failed")
        raise RuntimeError("The source video was deleted by the storage cleanup - can't re-render")
    transcript = load_transcript(video_dir(video.id) / "transcript.json")
    words = flatten_words(transcript)
    report("render", 0.0, "Re-rendering clip", force=True)
    _render_one(clip, video, words, FaceDetector(settings.models_dir), lambda f: report("render", f))
    report("render", 1.0, "Clip ready", force=True)


def download_video(task: Task, report: Reporter) -> None:
    """Fetch a video from a URL with yt-dlp (YouTube, Vimeo, X, Twitch VODs, direct links...)."""
    import yt_dlp

    with session_scope() as s:
        video = s.get(Video, task.video_id)
    vdir = video_dir(video.id)
    vdir.mkdir(parents=True, exist_ok=True)
    update_row(Video, video.id, status="processing", stage="download", error=None)

    def hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                report("download", d.get("downloaded_bytes", 0) / total)

    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(vdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    report("download", 0.0, "Downloading video", force=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video.source_url, download=True)
    files = sorted(vdir.glob("source.*"), key=lambda p: p.stat().st_size, reverse=True)
    files = [f for f in files if f.suffix not in (".part", ".ytdl")]
    if not files:
        raise RuntimeError("Download finished but no video file was found")
    path = files[0]
    meta = probe(path)
    max_minutes = load_config().storage.max_video_minutes
    if meta.duration > max_minutes * 60:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"Video is {meta.duration / 60:.0f} min long; the limit is {max_minutes} min")
    title = (task.options or {}).get("title") or info.get("title") or video.title
    update_row(Video, video.id, path=str(path), filename=path.name, title=title, duration=meta.duration,
               width=meta.width, height=meta.height, fps=meta.fps, size_bytes=path.stat().st_size,
               status="uploaded", stage=None, progress=0.0)
    if (task.options or {}).get("auto_process", True):
        with session_scope() as s:
            s.add(Task(kind="process_video", video_id=video.id, options={"retranscribe": True}))
            v = s.get(Video, video.id)
            v.status = "queued"
            s.add(v)
            s.commit()


def run_task(task_id: str) -> None:
    with session_scope() as s:
        task = s.get(Task, task_id)
    if task is None:
        return
    report = Reporter(task.id, task.video_id if task.kind in ("process_video", "download") else None)
    update_row(Task, task.id, status="running", started_at=utcnow(), error=None)
    try:
        if task.kind == "process_video":
            process_video(task, report)
        elif task.kind == "render_clip":
            render_single_clip(task, report)
        elif task.kind == "download":
            download_video(task, report)
        else:
            raise RuntimeError(f"Unknown task kind {task.kind}")
    except Exception as e:
        log.exception("Task %s failed", task.id)
        msg = str(e)[:4000] or e.__class__.__name__
        update_row(Task, task.id, status="failed", error=msg, finished_at=utcnow())
        if task.kind in ("process_video", "download"):
            update_row(Video, task.video_id, status="failed", error=msg)
        return
    update_row(Task, task.id, status="done", progress=1.0, finished_at=utcnow())


def remove_video_files(video_id: str) -> None:
    shutil.rmtree(video_dir(video_id), ignore_errors=True)
