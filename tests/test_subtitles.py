from autoreel.config import SubtitleConfig
from autoreel.pipeline.subtitles import ass_color, ass_tag_color, ass_time, build_ass, clip_words, group_words


def test_ass_color_and_time():
    assert ass_color("#FFE600") == "&H0000E6FF"
    assert ass_color("#fff", alpha=0x80) == "&H80FFFFFF"
    assert ass_tag_color("#FFE600") == "&H00E6FF&"
    assert ass_time(3725.456) == "1:02:05.46"
    assert ass_time(-1) == "0:00:00.00"


def test_clip_words_retimes():
    words = [{"start": 9.0, "end": 9.5, "word": "a"}, {"start": 10.2, "end": 10.6, "word": "b"},
             {"start": 20.5, "end": 21.0, "word": "c"}]
    out = clip_words(words, 10.0, 20.0)
    assert out == [{"start": 0.2, "end": 0.6, "word": "b"}] or abs(out[0]["start"] - 0.2) < 1e-9


def test_group_words_breaks_on_punctuation_and_gaps():
    words = [{"start": i * 0.3, "end": i * 0.3 + 0.25, "word": w} for i, w in enumerate(
        ["one", "two.", "three", "four", "five", "six"])]
    words[5]["start"] = 10.0
    words[5]["end"] = 10.3
    groups = group_words(words, max_words=3)
    assert [[w["word"] for w in g] for g in groups] == [["one", "two."], ["three", "four", "five"], ["six"]]


def _dialogues(ass: str):
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def test_karaoke_one_event_per_word_plus_hook():
    words = [{"start": 0.0, "end": 0.4, "word": "hello"}, {"start": 0.5, "end": 0.9, "word": "big"},
             {"start": 1.0, "end": 1.5, "word": "world"}]
    ass = build_ass(words, 5.0, SubtitleConfig(max_words=3), 1080, 1920, hook="Stop scrolling")
    d = _dialogues(ass)
    assert len(d) == 4
    assert "{\\c&H00E6FF&}HELLO{\\c&HFFFFFF&}" in d[0]
    assert "{\\c&H00E6FF&}BIG" in d[1]
    assert "Hook" in d[3] and "Stop scrolling" in d[3]
    assert "PlayResX: 1080" in ass


def test_disabled_subtitles_keep_hook_only():
    words = [{"start": 0.0, "end": 0.4, "word": "x"}]
    ass = build_ass(words, 2.0, SubtitleConfig(enabled=False), 1080, 1920, hook="Hi")
    d = _dialogues(ass)
    assert len(d) == 1 and ",Hook," in d[0]


def test_word_and_phrase_styles_escape_braces():
    words = [{"start": 0.0, "end": 0.4, "word": "{evil}"}, {"start": 0.5, "end": 0.9, "word": "b\\c"}]
    for style in ("word", "phrase"):
        ass = build_ass(words, 2.0, SubtitleConfig(style=style, uppercase=False, hook_enabled=False), 1080, 1920)
        body = "\n".join(_dialogues(ass))
        assert "{evil}" not in body and "(evil)" in body and "b/c" in body
