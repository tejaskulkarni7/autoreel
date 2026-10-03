"""Render a vertical clip with ffmpeg: reframe + burned-in subtitles + loudness normalization."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..config import SubtitleConfig, VideoConfig
from .media import FONTS_DIR, MediaError, escape_filter_value, ffmpeg_bin, make_thumbnail
from .reframe import CropPlan, FaceDetector, crop_size, iter_frames, plan_crop
from .subtitles import build_ass, clip_words

log = logging.getLogger(__name__)


@dataclass
class RenderResult:
    path: Path
    thumb: Path
    layout: str


def _subs_filter(ass_path: Path | None) -> str:
    if ass_path is None:
        return ""
    return f",ass=filename={escape_filter_value(str(ass_path))}:fontsdir={escape_filter_value(str(FONTS_DIR))}"


def _encode_args(cfg: VideoConfig, has_audio: bool) -> list[str]:
    args = ["-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf), "-pix_fmt", "yuv420p",
            "-r", f"{cfg.fps}"]
    if has_audio:
        args += ["-c:a", "aac", "-b:a", cfg.audio_bitrate, "-ar", "48000"]
        if cfg.normalize_audio:
            args += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    return args + ["-movflags", "+faststart"]


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise MediaError(f"ffmpeg render failed:\n{tail}")


def render_static(src: Path, out: Path, start: float, duration: float, src_w: int, src_h: int, layout: str,
                  cfg: VideoConfig, ass_path: Path | None, has_audio: bool) -> None:
    W, H = cfg.width, cfg.height
    subs = _subs_filter(ass_path)
    if layout == "blur":
        bw, bh = W // 4, H // 4
        graph = (
            f"[0:v]fps={cfg.fps},split=2[a][b];"
            f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=10:2,"
            f"scale={W}:{H},setsar=1[bg];"
            f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2{subs}[v]"
        )
    else:  # center
        cw, ch = crop_size(src_w, src_h, W, H)
        graph = (
            f"[0:v]fps={cfg.fps},crop={cw}:{ch}:(iw-{cw})/2:(ih-{ch})/2,scale={W}:{H}:flags=lanczos,setsar=1"
            f"{subs}[v]"
        )
    cmd = [ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
           "-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "0:a:0"]
    cmd += _encode_args(cfg, has_audio) + [str(out)]
    _run_ffmpeg(cmd)


def render_tracked(src: Path, out: Path, start: float, duration: float, src_w: int, src_h: int,
                   plan: CropPlan, cfg: VideoConfig, ass_path: Path | None, has_audio: bool,
                   progress: Callable[[float], None] | None = None) -> None:
    W, H = cfg.width, cfg.height
    cw, ch = plan.crop_w, plan.crop_h
    vf = f"scale={W}:{H}:flags=lanczos,setsar=1{_subs_filter(ass_path)}"
    cmd = [ffmpeg_bin(), "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{cw}x{ch}", "-r", f"{cfg.fps}", "-i", "-"]
    if has_audio:
        cmd += ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(src)]
    cmd += ["-map", "0:v"]
    if has_audio:
        cmd += ["-map", "1:a:0"]
    cmd += ["-vf", vf] + _encode_args(cfg, has_audio) + ["-shortest", str(out)]

    with tempfile.TemporaryFile() as errlog:
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errlog)
        total = max(1, len(plan.xs))
        i = 0
        try:
            for frame in iter_frames(src, start, duration, cfg.fps, src_w, src_h):
                x = plan.xs[min(i, total - 1)]
                crop = frame[plan.y : plan.y + ch, x : x + cw]
                enc.stdin.write(np.ascontiguousarray(crop).tobytes())
                i += 1
                if progress and i % 30 == 0:
                    progress(min(1.0, i / total))
        except BrokenPipeError:
            pass
        finally:
            try:
                enc.stdin.close()
            except BrokenPipeError:
                pass
            enc.wait()
        if enc.returncode != 0:
            errlog.seek(0)
            tail = "\n".join(errlog.read().decode(errors="replace").strip().splitlines()[-15:])
            raise MediaError(f"ffmpeg render failed:\n{tail}")
        if i == 0:
            raise MediaError("No frames decoded for clip")


def render_clip(
    src: Path,
    out_dir: Path,
    clip_id: str,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    has_audio: bool,
    words: list[dict],
    hook: str,
    video_cfg: VideoConfig,
    sub_cfg: SubtitleConfig,
    models_dir: Path,
    detector: FaceDetector | None = None,
    progress: Callable[[float], None] | None = None,
) -> RenderResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    duration = end - start
    out = out_dir / f"{clip_id}.mp4"
    tmp_out = out_dir / f"{clip_id}.rendering.mp4"
    ass_path = out_dir / f"{clip_id}.ass"
    ass_path.write_text(
        build_ass(clip_words(words, start, end), duration, sub_cfg, video_cfg.width, video_cfg.height, hook=hook),
        encoding="utf-8",
    )
    use_ass = ass_path if (sub_cfg.enabled or (sub_cfg.hook_enabled and hook)) else None

    layout = video_cfg.layout
    already_vertical = src_w / src_h <= video_cfg.width / video_cfg.height + 0.01
    plan = None
    if already_vertical:
        layout = "center"
    elif layout in ("face", "auto"):
        plan = plan_crop(src, start, duration, src_w, src_h, video_cfg, models_dir, detector)
        if progress:
            progress(0.3)
        if layout == "auto" and plan.face_coverage < video_cfg.face_coverage_for_auto:
            layout = "blur"
        else:
            layout = "face"

    if layout == "face" and plan is not None:
        def sub_progress(p: float) -> None:
            if progress:
                progress(0.3 + 0.65 * p)

        render_tracked(src, tmp_out, start, duration, src_w, src_h, plan, video_cfg, use_ass, has_audio,
                       sub_progress)
    else:
        render_static(src, tmp_out, start, duration, src_w, src_h, layout, video_cfg, use_ass, has_audio)
    os.replace(tmp_out, out)
    thumb = make_thumbnail(out, out_dir / f"{clip_id}.jpg", at=min(1.0, duration / 2))
    if progress:
        progress(1.0)
    return RenderResult(path=out, thumb=thumb, layout=layout)
