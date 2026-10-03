"""Find the best moments in a transcript with an LLM, then clean the answer up.

The LLM proposes rough ``start``/``end`` times; we snap them to real word/sentence
boundaries from the transcript, enforce length limits, drop overlaps and rank by score.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ..config import ClipsConfig, LLMConfig
from .llm import LLMClient

log = logging.getLogger(__name__)

CATEGORIES = ["funny", "insightful", "emotional", "controversial", "story", "educational", "other"]

CLIP_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number", "description": "Clip start, seconds from video start"},
                    "end": {"type": "number", "description": "Clip end, seconds from video start"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "caption": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "score": {"type": "integer"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "reason": {"type": "string"},
                },
                "required": ["start", "end", "title", "hook", "caption", "hashtags", "score", "category", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior short-form video editor. You have cut hundreds of viral TikToks, \
Instagram Reels and YouTube Shorts out of podcasts, interviews, streams, talks and vlogs.

You will get the timestamped transcript of a long-form video. Pick the moments that will work best \
as standalone vertical clips for people scrolling a feed with no context.

What makes a great clip:
- A hook in the first 1-3 seconds: a bold claim, a question, a surprising fact, conflict, or the start \
of a joke. Never start on filler ("so", "um", "yeah", "anyway") or mid-sentence.
- Self-contained: understandable without the rest of the video. No dangling "as I said before".
- A payoff: a punchline, a reveal, a strong opinion, an emotional peak, a useful insight or the end \
of a story. End right after the payoff - never mid-thought.
- Energy: laughter, banter, passion, disagreement and vivid stories beat calm exposition.

Scoring (0-100): how likely the clip is to hold attention and get shared. Be honest and spread the \
scores out; 90+ is reserved for moments you'd bet on.

For each clip also write, in the language spoken in the video:
- title: a punchy title, max 70 characters, no hashtags, no clickbait lies.
- hook: 2-7 words of on-screen text for the first seconds that makes people stop scrolling.
- caption: a 1-2 sentence social caption that adds curiosity; no hashtags in it.
- hashtags: 3-6 relevant hashtags without the # sign (mix broad and niche).
- reason: one short sentence on why this moment works.

Clips must not overlap each other. Use the timestamps from the transcript: start at the beginning \
of the first sentence of the moment and end at the end of its last sentence."""


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Candidate:
    start: float
    end: float
    title: str = ""
    hook: str = ""
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    score: int = 0
    category: str = "other"
    reason: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


def format_transcript(segments: list[dict], t0: float = 0.0, t1: float = math.inf) -> str:
    lines = []
    for seg in segments:
        if seg["end"] < t0 or seg["start"] > t1:
            continue
        text = seg["text"].strip()
        if text:
            lines.append(f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {text}")
    return "\n".join(lines)


def build_user_prompt(
    transcript_text: str,
    duration: float,
    clips_cfg: ClipsConfig,
    wanted: int,
    custom_instructions: str = "",
    part: tuple[int, int, float, float] | None = None,
    title: str = "",
) -> str:
    parts = []
    if title:
        parts.append(f"Video title: {title}")
    parts.append(f"Video length: {duration:.0f} seconds.")
    if part:
        idx, total, a, b = part
        parts.append(f"This is part {idx} of {total} of the transcript, covering {a:.0f}s to {b:.0f}s. "
                     "Only pick moments inside this part.")
    parts.append(
        f"Find up to {wanted} clips. Each clip must be between {clips_cfg.min_duration:.0f} and "
        f"{clips_cfg.max_duration:.0f} seconds long. Fewer, better clips beat filler - skip weak moments."
    )
    if custom_instructions.strip():
        parts.append(f"Extra instructions from the channel owner: {custom_instructions.strip()}")
    parts.append("Transcript:\n<transcript>\n" + transcript_text + "\n</transcript>")
    return "\n\n".join(parts)


def _windows(duration: float, chunk_minutes: int, overlap: float = 60.0) -> list[tuple[float, float]]:
    if chunk_minutes <= 0 or duration <= chunk_minutes * 60 * 1.2:
        return [(0.0, duration)]
    size = chunk_minutes * 60.0
    out, t = [], 0.0
    while t < duration:
        out.append((max(0.0, t - overlap if t else 0.0), min(duration, t + size)))
        t += size
    return out


def detect_highlights(
    transcript: dict,
    llm: LLMClient,
    llm_cfg: LLMConfig,
    clips_cfg: ClipsConfig,
    max_hashtags: int = 6,
    title: str = "",
    progress: Callable[[float], None] | None = None,
) -> list[Candidate]:
    segments = transcript["segments"]
    duration = float(transcript.get("duration") or (segments[-1]["end"] if segments else 0.0))
    if not segments:
        return []
    windows = _windows(duration, llm_cfg.chunk_minutes)
    # Ask for extra candidates so dedupe/filtering still leaves enough good ones.
    total_wanted = max(1, math.ceil(clips_cfg.max_clips * 1.5))
    raw: list[dict] = []
    for i, (a, b) in enumerate(windows):
        wanted = total_wanted if len(windows) == 1 else max(2, math.ceil(total_wanted * (b - a) / duration) + 1)
        text = format_transcript(segments, a, b)
        if not text:
            continue
        prompt = build_user_prompt(
            text, duration, clips_cfg, wanted, llm_cfg.custom_instructions,
            part=(i + 1, len(windows), a, b) if len(windows) > 1 else None, title=title,
        )
        result = llm.complete_json(SYSTEM_PROMPT, prompt, CLIP_SCHEMA)
        raw.extend(result.get("clips") or [])
        if progress:
            progress((i + 1) / len(windows))
    words = [Word(w["start"], w["end"], w["word"]) for w in _flatten(transcript)]
    return postprocess(raw, words, duration, clips_cfg, max_hashtags)


def _flatten(transcript: dict) -> list[dict]:
    from .transcribe import flatten_words

    return flatten_words(transcript)


# ------------------------------------------------------------------------------------
# Post-processing (pure functions - unit tested)
# ------------------------------------------------------------------------------------

_SENT_END = re.compile(r"[.!?…]['\")\]]*$")
_GAP = 0.8


def sentence_flags(words: list[Word]) -> tuple[list[bool], list[bool]]:
    n = len(words)
    starts, ends = [False] * n, [False] * n
    for i, w in enumerate(words):
        is_end = bool(_SENT_END.search(w.text)) or i == n - 1 or (words[i + 1].start - w.end) > _GAP
        ends[i] = is_end
        if i + 1 < n and is_end:
            starts[i + 1] = True
    if n:
        starts[0] = True
    return starts, ends


def _closest(indices: list[int], key: Callable[[int], float], target: float) -> int | None:
    return min(indices, key=lambda i: abs(key(i) - target)) if indices else None


def snap_start(t: float, words: list[Word], starts: list[bool], before: float = 3.0, after: float = 1.5) -> int:
    window = [i for i, w in enumerate(words) if t - before <= w.start <= t + after]
    best = _closest([i for i in window if starts[i]], lambda i: words[i].start, t)
    if best is None:
        best = _closest(window, lambda i: words[i].start, t)
    if best is None:  # nothing nearby: first word after t
        best = next((i for i, w in enumerate(words) if w.start >= t), len(words) - 1)
    return best


def snap_end(t: float, words: list[Word], ends: list[bool], before: float = 1.5, after: float = 3.0) -> int:
    window = [i for i, w in enumerate(words) if t - before <= w.end <= t + after]
    best = _closest([i for i in window if ends[i]], lambda i: words[i].end, t)
    if best is None:
        best = _closest(window, lambda i: words[i].end, t)
    if best is None:
        best = max((i for i, w in enumerate(words) if w.end <= t), default=0)
    return best


def fit_length(si: int, ei: int, words: list[Word], ends: list[bool], cfg: ClipsConfig) -> int:
    """Move the end index so the clip respects min/max duration (preferring sentence ends)."""
    start = words[si].start
    dur = words[ei].end - start
    if dur > cfg.max_duration:
        options = [i for i in range(si, ei + 1) if words[i].end - start <= cfg.max_duration]
        sentence_opts = [i for i in options if ends[i] and words[i].end - start >= cfg.min_duration]
        if sentence_opts:
            return max(sentence_opts)
        return max(options) if options else si
    if dur < cfg.min_duration:
        later = [i for i in range(ei, len(words)) if words[i].end - start <= cfg.max_duration]
        sentence_opts = [i for i in later if ends[i] and words[i].end - start >= cfg.min_duration]
        if sentence_opts:
            return min(sentence_opts)
        long_enough = [i for i in later if words[i].end - start >= cfg.min_duration]
        if long_enough:
            return min(long_enough)
        return max(later) if later else ei
    return ei


def clean_hashtags(tags, limit: int) -> list[str]:
    out: list[str] = []
    for t in tags or []:
        t = re.sub(r"[^\w]", "", str(t).lstrip("#"), flags=re.UNICODE)
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:limit]


def _overlap(a: Candidate, b: Candidate) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.duration, b.duration))


def postprocess(
    raw: list[dict], words: list[Word], duration: float, cfg: ClipsConfig, max_hashtags: int = 6
) -> list[Candidate]:
    if not words:
        return []
    starts, ends = sentence_flags(words)
    cands: list[Candidate] = []
    for r in raw:
        try:
            s, e = float(r.get("start", 0)), float(r.get("end", 0))
        except (TypeError, ValueError):
            continue
        s, e = max(0.0, min(s, duration)), max(0.0, min(e, duration))
        if e <= s:
            continue
        si = snap_start(s, words, starts)
        ei = snap_end(e, words, ends)
        ei = max(ei, si)
        ei = fit_length(si, ei, words, ends, cfg)
        clip_start = max(0.0, words[si].start - cfg.pad_start)
        if si > 0:
            clip_start = max(clip_start, min(words[si - 1].end, words[si].start))
        clip_end = min(duration, words[ei].end + cfg.pad_end)
        if ei + 1 < len(words):
            clip_end = min(clip_end, max(words[ei + 1].start, words[ei].end))
        if clip_end - clip_start < min(cfg.min_duration * 0.6, cfg.min_duration - 1):
            log.info("Dropping too-short clip %.1f-%.1f", clip_start, clip_end)
            continue
        try:
            score = int(round(float(r.get("score", 0))))
        except (TypeError, ValueError):
            score = 0
        category = str(r.get("category") or "other").lower()
        cands.append(
            Candidate(
                start=round(clip_start, 3),
                end=round(clip_end, 3),
                title=str(r.get("title") or "").strip()[:100],
                hook=str(r.get("hook") or "").strip()[:80],
                caption=str(r.get("caption") or "").strip(),
                hashtags=clean_hashtags(r.get("hashtags"), max_hashtags),
                score=max(0, min(100, score)),
                category=category if category in CATEGORIES else "other",
                reason=str(r.get("reason") or "").strip(),
            )
        )
    cands.sort(key=lambda c: c.score, reverse=True)
    kept: list[Candidate] = []
    for c in cands:
        if c.score < cfg.min_score:
            continue
        if any(_overlap(c, k) > 0.5 for k in kept):
            continue
        kept.append(c)
        if len(kept) >= cfg.max_clips:
            break
    return kept
