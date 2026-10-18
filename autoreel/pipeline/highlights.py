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
from .llm import LLMClient, estimate_tokens

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
    # word indices of the first/last spoken word (used to trim overlaps on sentence boundaries)
    si: int = field(default=-1, repr=False)
    ei: int = field(default=-1, repr=False)

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


def token_windows(segments: list[dict], budget: int, overlap: float = 60.0) -> list[tuple[float, float]]:
    """Split the transcript into time windows whose formatted text fits ``budget`` tokens."""
    if not segments:
        return []
    out: list[tuple[float, float]] = []
    start_i, used = 0, 0
    for i, seg in enumerate(segments):
        cost = estimate_tokens(f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}\n")
        if used + cost > budget and i > start_i:
            out.append((segments[start_i]["start"], segments[i - 1]["end"]))
            # next window starts ~overlap seconds earlier so moments on the seam aren't lost
            back = i
            while back > start_i + 1 and segments[i - 1]["end"] - segments[back - 1]["start"] < overlap:
                back -= 1
            start_i = back if back < i else i
            used = sum(estimate_tokens(f"[{x['start']:.1f}s - {x['end']:.1f}s] {x['text']}\n")
                       for x in segments[start_i:i])
        used += cost
    out.append((segments[start_i]["start"], segments[-1]["end"]))
    return out


def plan_windows(segments: list[dict], duration: float, llm: LLMClient, llm_cfg: LLMConfig) -> list[tuple[float, float]]:
    budget = int(getattr(llm, "context_tokens", 100_000)) - int(getattr(llm, "output_reserve", 0)) \
        - estimate_tokens(SYSTEM_PROMPT) - 600
    budget = max(1000, budget)
    out: list[tuple[float, float]] = []
    for a, b in _windows(duration, llm_cfg.chunk_minutes):
        inside = [seg for seg in segments if seg["end"] >= a and seg["start"] <= b]
        out.extend(token_windows(inside, budget) or [(a, b)])
    return out


RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranking": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "score": {"type": "integer"}},
                "required": ["id", "score"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranking"],
    "additionalProperties": False,
}

RERANK_PROMPT = """You are a senior short-form video editor doing the final selection. Below are candidate \
clips cut from one long video, each with its exact transcript. Compare them against each other and \
score every one 0-100 for how well it would perform as a standalone TikTok / Reel / Short: strong \
hook in the first seconds, self-contained, clear payoff, energy, shareability. Use the full range \
and make the scores comparable across candidates. Return one entry per candidate id."""


def clip_text(segments: list[dict], start: float, end: float, max_chars: int = 1500) -> str:
    text = " ".join(s["text"].strip() for s in segments if s["end"] > start and s["start"] < end)
    return text[:max_chars]


def rerank(cands: list[Candidate], segments: list[dict], llm: LLMClient, custom_instructions: str = "") -> None:
    """Re-score candidates side by side (in place). Failures keep the original scores."""
    lines = []
    for i, c in enumerate(cands):
        lines.append(f"<candidate id=\"{i}\" length=\"{c.duration:.0f}s\">\nTitle: {c.title}\nTranscript: "
                     f"{clip_text(segments, c.start, c.end)}\n</candidate>")
    user = "\n\n".join(lines)
    if custom_instructions.strip():
        user += f"\n\nExtra instructions from the channel owner: {custom_instructions.strip()}"
    try:
        result = llm.complete_json(RERANK_PROMPT, user, RERANK_SCHEMA)
    except Exception as e:  # ranking is an optimization; never fail the video over it
        log.warning("Rerank pass failed, keeping first-pass scores: %s", e)
        return
    for item in result.get("ranking") or []:
        try:
            idx, score = int(item["id"]), int(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= idx < len(cands):
            cands[idx].score = max(0, min(100, score))


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
    windows = plan_windows(segments, duration, llm, llm_cfg)
    chunked = len(windows) > 1
    steps = len(windows) + (1 if chunked and llm_cfg.rerank else 0)
    # Ask for extra candidates so dedupe/filtering still leaves enough good ones.
    total_wanted = max(1, math.ceil(clips_cfg.max_clips * 1.5))
    raw: list[dict] = []
    for i, (a, b) in enumerate(windows):
        wanted = total_wanted if not chunked else max(2, math.ceil(total_wanted * (b - a) / duration) + 1)
        text = format_transcript(segments, a, b)
        if not text:
            continue
        prompt = build_user_prompt(
            text, duration, clips_cfg, wanted, llm_cfg.custom_instructions,
            part=(i + 1, len(windows), a, b) if chunked else None, title=title,
        )
        result = llm.complete_json(SYSTEM_PROMPT, prompt, CLIP_SCHEMA)
        raw.extend(result.get("clips") or [])
        if progress:
            progress((i + 1) / steps)
    words = [Word(w["start"], w["end"], w["word"]) for w in _flatten(transcript)]
    if chunked and llm_cfg.rerank:
        pool = postprocess(raw, words, duration, clips_cfg, max_hashtags,
                           keep_limit=max(clips_cfg.max_clips * 2, clips_cfg.max_clips + 4), apply_min_score=False)
        if len(pool) > 1:
            rerank(pool, segments, llm, llm_cfg.custom_instructions)
        if progress:
            progress(1.0)
        pool.sort(key=lambda c: c.score, reverse=True)
        return [c for c in pool if c.score >= clips_cfg.min_score][: clips_cfg.max_clips]
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


def _intersection(a: Candidate, b: Candidate) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def _bounds(si: int, ei: int, words: list[Word], duration: float, cfg: ClipsConfig) -> tuple[float, float]:
    """Clip start/end for a word range, with padding that never bleeds into neighbouring words."""
    clip_start = max(0.0, words[si].start - cfg.pad_start)
    if si > 0:
        clip_start = max(clip_start, min(words[si - 1].end, words[si].start))
    clip_end = min(duration, words[ei].end + cfg.pad_end)
    if ei + 1 < len(words):
        clip_end = min(clip_end, max(words[ei + 1].start, words[ei].end))
    return round(clip_start, 3), round(clip_end, 3)


def _trim_against(c: Candidate, k: Candidate, words: list[Word], starts: list[bool], ends: list[bool],
                  duration: float, cfg: ClipsConfig) -> bool:
    """Shrink ``c`` so it no longer overlaps kept clip ``k``. Returns False if it can't stay long enough."""
    min_len = cfg.min_duration * 0.8
    if c.start < k.start:  # overlap at c's tail -> end at a sentence end before k starts
        options = [i for i in range(c.si, c.ei + 1) if ends[i] and words[i].end <= k.start + 0.05]
        if not options:
            return False
        new_si, new_ei = c.si, max(options)
    else:  # overlap at c's head -> start at a sentence start after k ends
        options = [i for i in range(c.si, c.ei + 1) if starts[i] and words[i].start >= k.end - 0.05]
        if not options:
            return False
        new_si, new_ei = min(options), c.ei
    s, e = _bounds(new_si, new_ei, words, duration, cfg)
    if e - s < min_len:
        return False
    c.si, c.ei, c.start, c.end = new_si, new_ei, s, e
    return True


def postprocess(
    raw: list[dict], words: list[Word], duration: float, cfg: ClipsConfig, max_hashtags: int = 6,
    keep_limit: int | None = None, apply_min_score: bool = True,
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
        clip_start, clip_end = _bounds(si, ei, words, duration, cfg)
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
                start=clip_start,
                end=clip_end,
                title=str(r.get("title") or "").strip()[:100],
                hook=str(r.get("hook") or "").strip()[:80],
                caption=str(r.get("caption") or "").strip(),
                hashtags=clean_hashtags(r.get("hashtags"), max_hashtags),
                score=max(0, min(100, score)),
                category=category if category in CATEGORIES else "other",
                reason=str(r.get("reason") or "").strip(),
                si=si,
                ei=ei,
            )
        )
    cands.sort(key=lambda c: c.score, reverse=True)
    limit = keep_limit or cfg.max_clips
    kept: list[Candidate] = []
    for c in cands:
        if apply_min_score and c.score < cfg.min_score:
            continue
        ok = True
        for _ in range(3):  # trimming against one clip can create overlap with another
            clash = next((k for k in kept if _intersection(c, k) > 0.3), None)
            if clash is None:
                break
            # Mostly the same moment -> duplicate; otherwise trim at a sentence boundary.
            if _intersection(c, clash) / max(1e-6, min(c.duration, clash.duration)) > 0.5 or \
                    not _trim_against(c, clash, words, starts, ends, duration, cfg):
                ok = False
                break
        else:
            ok = not any(_intersection(c, k) > 0.3 for k in kept)
        if not ok:
            continue
        kept.append(c)
        if len(kept) >= limit:
            break
    return kept
