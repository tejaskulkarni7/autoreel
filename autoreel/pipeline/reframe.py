"""Speaker-aware 9:16 reframing.

1. Decode the clip at low resolution and detect hard cuts (shot boundaries).
2. Detect faces every N frames with OpenCV's YuNet DNN detector (Haar cascade fallback).
3. Per shot, choose the subject (largest face, sticky across frames, or a two-shot if both
   faces fit) and build a horizontal crop path: static when the subject barely moves
   (looks like a real camera operator), smoothed panning otherwise, instant jump on cuts.
"""

from __future__ import annotations

import logging
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..config import VideoConfig
from .media import ffmpeg_bin

log = logging.getLogger(__name__)

YUNET_URL = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
ANALYSIS_WIDTH = 640
CUT_THRESHOLD = 27.0  # mean HSV delta, same scale as PySceneDetect's ContentDetector


def even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


@dataclass
class CropPlan:
    crop_w: int
    crop_h: int
    xs: list[int]  # left edge of the crop for every output frame
    y: int
    face_coverage: float  # share of sampled frames with at least one face
    shots: list[tuple[int, int]]


class FaceDetector:
    def __init__(self, models_dir: Path):
        self.kind = "haar"
        self.yunet = None
        model = models_dir / "face_detection_yunet_2023mar.onnx"
        try:
            if not model.exists() or model.stat().st_size < 10_000:
                log.info("Downloading YuNet face detector...")
                tmp = model.with_suffix(".part")
                urllib.request.urlretrieve(YUNET_URL, tmp)
                tmp.rename(model)
            self.yunet = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.7, 0.3, 50)
            self.kind = "yunet"
        except Exception as e:  # offline etc.
            log.warning("YuNet unavailable (%s); falling back to Haar cascade", e)
        if self.yunet is None:
            self.haar = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    def detect(self, frame: np.ndarray) -> list[tuple[float, float, float, float, float]]:
        """Return faces as (x, y, w, h, score) in frame pixel coordinates."""
        h, w = frame.shape[:2]
        if self.yunet is not None:
            self.yunet.setInputSize((w, h))
            _, faces = self.yunet.detect(frame)
            if faces is None:
                return []
            return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[14])) for f in faces]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = self.haar.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(w // 20, w // 20))
        return [(float(x), float(y), float(fw), float(fh), 1.0) for (x, y, fw, fh) in found]


def crop_size(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int]:
    target = out_w / out_h
    if src_w / src_h > target:
        return min(src_w, even(src_h * target)), src_h
    return src_w, min(src_h, even(src_w / target))


def iter_frames(path: Path, start: float, duration: float, fps: float, width: int, height: int,
                scale_width: int | None = None):
    """Yield BGR frames for [start, start+duration] at ``fps`` (optionally downscaled)."""
    vf = f"fps={fps}"
    out_w, out_h = width, height
    if scale_width and scale_width < width:
        out_w = even(scale_width)
        out_h = even(height * out_w / width)
        vf += f",scale={out_w}:{out_h}"
    cmd = [
        ffmpeg_bin(), "-v", "error", "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{duration:.3f}",
        "-map", "0:v:0", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]
    frame_bytes = out_w * out_h * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=frame_bytes * 2)
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, np.uint8).reshape(out_h, out_w, 3)
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def _pick_target(faces, prev_center: float | None, crop_w: float, frame_w: float) -> float | None:
    if not faces:
        return None
    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
    largest_area = faces[0][2] * faces[0][3]
    big = [f for f in faces if f[2] * f[3] >= 0.4 * largest_area]
    # Two-shot: if all significant faces fit in the crop, frame them together.
    if len(big) > 1:
        left = min(f[0] for f in big)
        right = max(f[0] + f[2] for f in big)
        if right - left <= crop_w * 0.9:
            return (left + right) / 2
    if prev_center is not None:
        for f in big:  # stick with the current subject to avoid ping-ponging
            c = f[0] + f[2] / 2
            if abs(c - prev_center) < 0.15 * frame_w:
                return c
    f = faces[0]
    return f[0] + f[2] / 2


def _smooth(xs: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(xs) < 3:
        return xs
    window = min(window | 1, (len(xs) // 2) * 2 + 1)
    pad = window // 2
    padded = np.pad(xs, pad, mode="edge")
    kernel = np.ones(window) / window
    return np.convolve(padded, kernel, mode="valid")


def plan_crop(
    path: Path, start: float, duration: float, src_w: int, src_h: int, cfg: VideoConfig, models_dir: Path,
    detector: FaceDetector | None = None,
) -> CropPlan:
    crop_w, crop_h = crop_size(src_w, src_h, cfg.width, cfg.height)
    y = (src_h - crop_h) // 2
    detector = detector or FaceDetector(models_dir)
    scale = 1.0
    prev_hsv = None
    cuts = [0]
    raw_targets: list[float | None] = []
    sampled = with_face = 0
    n = 0
    every = max(1, cfg.detect_every_n_frames)
    for frame in iter_frames(path, start, duration, cfg.fps, src_w, src_h, scale_width=ANALYSIS_WIDTH):
        scale = src_w / frame.shape[1]
        small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.int16)
        if prev_hsv is not None and n - cuts[-1] >= int(cfg.fps * 0.5):
            if float(np.mean(np.abs(hsv - prev_hsv))) > CUT_THRESHOLD:
                cuts.append(n)
        prev_hsv = hsv
        if n % every == 0:
            faces = detector.detect(frame)
            faces = [(x * scale, yy * scale, w * scale, h * scale, s) for (x, yy, w, h, s) in faces]
            sampled += 1
            if faces:
                with_face += 1
            prev = next((t for t in reversed(raw_targets) if t is not None), None)
            if cuts[-1] == n:
                prev = None
            raw_targets.append(_pick_target(faces, prev, crop_w, src_w))
        else:
            raw_targets.append(None)
        n += 1

    total = max(1, n)
    shots = [(cuts[i], cuts[i + 1] if i + 1 < len(cuts) else total) for i in range(len(cuts))]
    center_default = src_w / 2
    xs = np.full(total, center_default, dtype=np.float64)
    smooth_window = int(cfg.fps * cfg.smoothing_seconds)
    for a, b in shots:
        idx = [i for i in range(a, min(b, len(raw_targets))) if raw_targets[i] is not None]
        if not idx:
            continue  # no face in this shot: keep it centered
        vals = np.array([raw_targets[i] for i in idx])
        seg = np.interp(np.arange(a, b), idx, vals)  # holds the edge values outside detections
        if seg.max() - seg.min() < cfg.static_threshold * crop_w:
            seg = np.full_like(seg, float(np.median(vals)))
        else:
            seg = _smooth(seg, smooth_window)
        xs[a:b] = seg
    lefts = np.clip(xs - crop_w / 2, 0, src_w - crop_w)
    return CropPlan(
        crop_w=crop_w,
        crop_h=crop_h,
        xs=[int(v) for v in lefts],
        y=int(y),
        face_coverage=(with_face / sampled) if sampled else 0.0,
        shots=shots,
    )
