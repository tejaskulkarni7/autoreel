"""ffmpeg / ffprobe helpers."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


class MediaError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_bin() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def run(cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise MediaError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc


def _parse_rate(rate: str | None) -> float:
    if not rate or rate in ("0/0", "0"):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        return float(num) / float(den) if float(den) else 0.0
    return float(rate)


def probe(path: Path) -> ProbeResult:
    proc = run(
        [
            ffprobe_bin(), "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
    )
    info = json.loads(proc.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("No video stream found in file")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    width, height = int(video.get("width", 0)), int(video.get("height", 0))

    # Phone footage often stores rotation as metadata; ffmpeg auto-rotates on decode.
    rotation = 0
    for sd in video.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = int(float(sd["rotation"]))
    rotation = int(video.get("tags", {}).get("rotate", rotation) or rotation)
    if abs(rotation) % 180 == 90:
        width, height = height, width

    fps = _parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")) or 30.0
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0.0)
    return ProbeResult(duration=duration, width=width, height=height, fps=fps, has_audio=has_audio)


def extract_audio(src: Path, dst: Path) -> Path:
    """16 kHz mono WAV - what Whisper wants."""
    run([ffmpeg_bin(), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dst)])
    return dst


def make_thumbnail(video: Path, dst: Path, at: float = 1.0, width: int = 360) -> Path:
    run([ffmpeg_bin(), "-y", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(video),
         "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(dst)])
    return dst


def escape_filter_value(value: str) -> str:
    """Escape a string used as an option value inside an ffmpeg filtergraph.

    Two levels: first for the filter's option parser (``\\ ' :``), then for the
    filtergraph parser (``\\ ' [ ] , ;``).
    """
    v = str(value).replace("\\", "/")
    level1 = re.sub(r"([\\':])", r"\\\1", v)
    return re.sub(r"([\\'\[\],;])", r"\\\1", level1)
