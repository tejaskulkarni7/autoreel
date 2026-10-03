"""Generate animated "TikTok style" subtitles as an ASS file (burned in by ffmpeg/libass)."""

from __future__ import annotations

import re
import textwrap

from ..config import SubtitleConfig

_SENT_END = re.compile(r"[.!?…,;:]['\")\]]*$")


def ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS uses BGR and inverted alpha)."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        h = "FFFFFF"
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_tag_color(hex_color: str) -> str:
    """Color for inline override tags (\\c): '&HBBGGRR&' (no alpha byte)."""
    return "&H" + ass_color(hex_color)[4:] + "&"


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def clip_words(words: list[dict], start: float, end: float) -> list[dict]:
    """Words inside [start, end], re-timed relative to the clip start."""
    out = []
    for w in words:
        if w["end"] <= start or w["start"] >= end:
            continue
        out.append({"start": max(0.0, w["start"] - start), "end": min(end, w["end"]) - start, "word": w["word"]})
    return out


def group_words(words: list[dict], max_words: int, max_chars: int = 22, max_gap: float = 0.6) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            text_len = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            gap = w["start"] - cur[-1]["end"]
            if len(cur) >= max_words or text_len > max_chars or gap > max_gap or _SENT_END.search(cur[-1]["word"]):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def _fmt_word(word: str, cfg: SubtitleConfig) -> str:
    word = ass_escape(word)
    return word.upper() if cfg.uppercase else word


def build_ass(words: list[dict], clip_duration: float, cfg: SubtitleConfig, width: int, height: int,
              hook: str = "") -> str:
    primary = ass_color(cfg.primary_color)
    highlight = ass_color(cfg.highlight_color)
    primary_tag = ass_tag_color(cfg.primary_color)
    highlight_tag = ass_tag_color(cfg.highlight_color)
    outline = ass_color(cfg.outline_color)
    shadow = ass_color("#000000", alpha=0x60)
    margin_v = int(height * cfg.position_pct / 100)
    scale = width / 1080.0
    font_size = int(cfg.font_size * scale)
    hook_size = int(cfg.hook_font_size * scale)
    hook_box = ass_color(cfg.hook_box_color)
    hook_text = ass_color(cfg.hook_text_color)

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, \
MarginV, Encoding
Style: Caption,{cfg.font},{font_size},{primary},{highlight},{outline},{shadow},0,0,0,0,100,100,0,0,1,\
{int(cfg.outline_width * scale)},{cfg.shadow},2,{int(60 * scale)},{int(60 * scale)},{margin_v},1
Style: Hook,{cfg.font},{hook_size},{hook_text},{hook_text},{hook_box},{hook_box},0,0,0,0,100,100,0,0,3,\
{int(18 * scale)},0,8,{int(80 * scale)},{int(80 * scale)},{int(height * 0.12)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    def add(start: float, end: float, text: str, style: str = "Caption", layer: int = 0) -> None:
        if end - start >= 0.01:
            events.append(f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style},,0,0,0,,{text}")

    pop = r"{\fscx80\fscy80\t(0,90,\fscx100\fscy100)}" if cfg.pop_animation else ""

    if cfg.enabled and words:
        if cfg.style == "phrase":
            groups = group_words(words, max(cfg.max_words * 2, 4), max_chars=34)
        elif cfg.style == "word":
            groups = [[w] for w in words]
        else:
            groups = group_words(words, max(1, cfg.max_words))

        for gi, group in enumerate(groups):
            next_start = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else clip_duration
            # Keep the phrase up through short pauses so captions don't flicker.
            gap = next_start - group[-1]["end"]
            g_end = next_start if gap < 0.5 else group[-1]["end"] + 0.15
            g_end = min(g_end, clip_duration)
            g_start = group[0]["start"]

            if cfg.style == "karaoke":
                for wi, w in enumerate(group):
                    w_start = g_start if wi == 0 else w["start"]
                    w_end = group[wi + 1]["start"] if wi + 1 < len(group) else g_end
                    parts = []
                    for wj, other in enumerate(group):
                        token = _fmt_word(other["word"], cfg)
                        if wj == wi:
                            token = f"{{\\c{highlight_tag}}}{token}{{\\c{primary_tag}}}"
                        parts.append(token)
                    add(w_start, w_end, (pop if wi == 0 else "") + " ".join(parts))
            else:
                text = " ".join(_fmt_word(w["word"], cfg) for w in group)
                add(g_start, g_end, pop + text)

    if cfg.hook_enabled and hook.strip():
        wrapped = textwrap.wrap(ass_escape(hook.strip()), width=22)[:3]
        add(0.0, min(cfg.hook_seconds, clip_duration), r"{\fad(120,250)}" + r"\N".join(wrapped), style="Hook", layer=1)

    return header + "\n".join(events) + "\n"
