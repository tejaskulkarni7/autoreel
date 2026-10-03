"""Speech-to-text with word timestamps via faster-whisper (CTranslate2 Whisper).

Runs on CPU (int8) or NVIDIA GPU (float16). Models are downloaded from Hugging Face on
first use into ``data/models``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from ..config import TranscriptionConfig

log = logging.getLogger(__name__)


def cuda_available() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:  # pragma: no cover - depends on host
        return False


def resolve_device(cfg: TranscriptionConfig) -> tuple[str, str]:
    device = cfg.device
    if device == "auto":
        device = "cuda" if cuda_available() else "cpu"
    compute = cfg.compute_type
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def transcribe(
    audio_path: Path,
    cfg: TranscriptionConfig,
    models_dir: Path,
    progress: Callable[[float], None] | None = None,
) -> dict:
    """Return ``{"language", "duration", "segments": [{start, end, text, words: [...]}]}``."""
    from faster_whisper import WhisperModel

    device, compute = resolve_device(cfg)
    log.info("Loading whisper model %s on %s (%s)", cfg.model, device, compute)
    model = WhisperModel(cfg.model, device=device, compute_type=compute, download_root=str(models_dir))
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=cfg.language or None,
        beam_size=cfg.beam_size,
        word_timestamps=True,
        vad_filter=cfg.vad_filter,
    )
    total = float(info.duration or 0.0)
    segments = []
    for seg in segments_iter:
        words = [
            {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word, "p": round(w.probability, 3)}
            for w in (seg.words or [])
        ]
        segments.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": seg.text.strip(),
                         "words": words})
        if progress and total:
            progress(min(1.0, seg.end / total))
    return {"language": info.language, "duration": total, "segments": segments}


def save_transcript(transcript: dict, path: Path) -> None:
    path.write_text(json.dumps(transcript, ensure_ascii=False))


def load_transcript(path: Path) -> dict:
    return json.loads(path.read_text())


def flatten_words(transcript: dict) -> list[dict]:
    """All words in order, with whitespace stripped and empty tokens removed."""
    out = []
    for seg in transcript.get("segments", []):
        seg_words = seg.get("words") or []
        if not seg_words and seg.get("text"):
            # No word timings (shouldn't happen with word_timestamps=True): spread evenly.
            tokens = seg["text"].split()
            span = (seg["end"] - seg["start"]) / max(1, len(tokens))
            seg_words = [
                {"start": seg["start"] + i * span, "end": seg["start"] + (i + 1) * span, "word": t}
                for i, t in enumerate(tokens)
            ]
        for w in seg_words:
            text = w["word"].strip()
            if text:
                out.append({"start": float(w["start"]), "end": float(w["end"]), "word": text})
    return out
