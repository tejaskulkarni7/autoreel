from autoreel.config import ClipsConfig, LLMConfig
from autoreel.pipeline.highlights import (
    Word,
    _windows,
    clean_hashtags,
    detect_highlights,
    format_transcript,
    postprocess,
    sentence_flags,
)
from autoreel.pipeline.llm import LLMError, extract_json
from autoreel.pipeline.transcribe import flatten_words

from .conftest import FakeLLM, fake_transcript


def words_from(text_with_times):
    return [Word(s, e, t) for s, e, t in text_with_times]


def test_sentence_flags():
    words = words_from([(0, 0.4, "Hello"), (0.5, 0.9, "world."), (1.0, 1.4, "Next"), (3.0, 3.4, "gap")])
    starts, ends = sentence_flags(words)
    assert starts == [True, False, True, True]
    assert ends == [False, True, True, True]


def test_postprocess_snaps_to_sentence_boundaries():
    t = fake_transcript(60.0)
    words = [Word(w["start"], w["end"], w["word"]) for w in flatten_words(t)]
    cfg = ClipsConfig(min_duration=5, max_duration=45, pad_start=0, pad_end=0)
    # LLM gives sloppy times that land mid-sentence
    second_sentence_start = t["segments"][1]["start"]
    fourth_sentence_end = t["segments"][3]["end"]
    raw = [{"start": second_sentence_start + 0.6, "end": fourth_sentence_end - 0.4, "title": "T", "score": 80,
            "hashtags": ["#Startup", "startup", "life lessons"], "category": "story"}]
    out = postprocess(raw, words, 60.0, cfg)
    assert len(out) == 1
    c = out[0]
    assert abs(c.start - second_sentence_start) < 1e-6
    assert abs(c.end - fourth_sentence_end) < 1e-6
    assert c.hashtags == ["Startup", "lifelessons"]
    assert c.category == "story"


def test_postprocess_enforces_max_and_dedupes_and_ranks():
    t = fake_transcript(60.0)
    words = [Word(w["start"], w["end"], w["word"]) for w in flatten_words(t)]
    cfg = ClipsConfig(min_duration=5, max_duration=20, max_clips=5)
    raw = [
        {"start": 0, "end": 59, "score": 50},  # too long -> trimmed to <= 20s at a sentence end
        {"start": 1, "end": 15, "score": 90},  # overlaps the first one heavily
        {"start": 40, "end": 55, "score": 70},
        {"start": 30, "end": 20, "score": 99},  # invalid
        {"start": "x", "end": 3, "score": 99},  # invalid
    ]
    out = postprocess(raw, words, 60.0, cfg)
    assert [c.score for c in out] == [90, 70]
    for c in out:
        assert c.duration <= 20 + cfg.pad_end + cfg.pad_start + 1e-6


def test_postprocess_extends_short_clips():
    t = fake_transcript(60.0)
    words = [Word(w["start"], w["end"], w["word"]) for w in flatten_words(t)]
    cfg = ClipsConfig(min_duration=15, max_duration=40)
    out = postprocess([{"start": 0, "end": 3, "score": 10}], words, 60.0, cfg)
    assert len(out) == 1 and out[0].duration >= 15


def test_min_score_and_max_clips():
    t = fake_transcript(60.0)
    words = [Word(w["start"], w["end"], w["word"]) for w in flatten_words(t)]
    cfg = ClipsConfig(min_duration=3, max_duration=30, max_clips=1, min_score=60)
    raw = [{"start": 0, "end": 10, "score": 55}, {"start": 30, "end": 40, "score": 65},
           {"start": 45, "end": 55, "score": 95}]
    out = postprocess(raw, words, 60.0, cfg)
    assert [c.score for c in out] == [95]


def test_clean_hashtags_limit():
    assert clean_hashtags(["#a", "b c", "A", "d", None, ""], 2) == ["a", "bc"]


def test_windows():
    assert _windows(600, 0) == [(0.0, 600)]
    assert _windows(600, 10) == [(0.0, 600)]  # within 20% slack
    w = _windows(3600, 15)
    assert w[0] == (0.0, 900) and w[1][0] == 840 and w[-1][1] == 3600


def test_detect_highlights_with_fake_llm_and_chunking():
    t = fake_transcript(3600.0)
    llm = FakeLLM([{"start": 100, "end": 160, "title": "A", "hook": "Wait for it", "caption": "c",
                    "hashtags": ["x"], "score": 88, "category": "funny", "reason": "r"}])
    out = detect_highlights(t, llm, LLMConfig(chunk_minutes=20), ClipsConfig(min_duration=20, max_duration=90),
                            title="My podcast")
    assert len(llm.calls) == 3
    assert "part 1 of 3" in llm.calls[0] and "My podcast" in llm.calls[0]
    assert len(out) == 1 and out[0].hook == "Wait for it"


def test_format_transcript_window():
    t = fake_transcript(60.0)
    text = format_transcript(t["segments"], 20, 35)
    assert text.startswith("[") and "s - " in text
    assert "nobody tells you" not in text


def test_extract_json_variants():
    assert extract_json('{"clips": []}') == {"clips": []}
    assert extract_json('Sure!\n```json\n{"clips": [1]}\n```') == {"clips": [1]}
    assert extract_json('blah {"clips": [2]} trailing') == {"clips": [2]}
    assert extract_json("[1, 2]") == {"clips": [1, 2]}
    try:
        extract_json("no json here")
    except LLMError:
        pass
    else:
        raise AssertionError("expected LLMError")
