"""Configuration.

Two layers:

* ``ServerSettings`` - process-level settings read from environment variables / ``.env``
  (where data lives, host/port, optional admin password). Changing these needs a restart.
* ``AppConfig`` - everything the admin tunes from the web UI (AI provider, clip lengths,
  subtitle style, publishing defaults...). Stored as JSON in the database and read fresh
  by every job, so edits apply to the next video/clip without a restart.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_MASK = "••••••••"


class ServerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOREEL_", env_file=".env", extra="ignore")

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000
    # If set, the whole UI/API sits behind HTTP basic auth (user "admin").
    # Strongly recommended if you expose the app through a tunnel for OAuth callbacks.
    admin_password: str = ""
    # Public URL where this app is reachable (e.g. a Cloudflare tunnel). Only needed for
    # TikTok's HTTPS OAuth redirect and Instagram "video_url" uploads.
    public_base_url: str = ""
    # Reverse proxies whose X-Forwarded-* headers are trusted ("*" inside docker-compose behind Caddy).
    forwarded_allow_ips: str = "127.0.0.1"
    # Escape hatch to listen on a public interface without a password (not recommended).
    allow_no_auth: bool = False
    # Where the built React app lives.
    frontend_dist: Path = Path(__file__).resolve().parent.parent / "frontend" / "dist"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autoreel.db"

    @property
    def videos_dir(self) -> Path:
        return self.data_dir / "videos"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def secrets_dir(self) -> Path:
        return self.data_dir / "secrets"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.videos_dir, self.models_dir, self.secrets_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_server_settings() -> ServerSettings:
    s = ServerSettings()
    s.data_dir = s.data_dir.resolve()
    s.ensure_dirs()
    return s


# --------------------------------------------------------------------------------------
# Admin-editable configuration
# --------------------------------------------------------------------------------------


class TranscriptionConfig(BaseModel):
    # tiny | base | small | medium | large-v3 | large-v3-turbo | distil-large-v3 ...
    model: str = "small"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    # auto -> float16 on CUDA, int8 on CPU
    compute_type: str = "auto"
    # ISO code like "en", or "" to auto-detect
    language: str = ""
    beam_size: int = 5
    vad_filter: bool = True


class LLMConfig(BaseModel):
    provider: Literal["anthropic", "ollama", "openai_compatible"] = "anthropic"
    # Anthropic
    anthropic_model: str = "claude-opus-5-5"
    anthropic_api_key: str = ""  # empty -> ANTHROPIC_API_KEY env / `ant auth login` profile
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Ollama (native API, so we can set the context window - its default is tiny)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b"
    ollama_num_ctx: int = 16384
    # OpenAI-compatible server: LM Studio, vLLM, llama.cpp, OpenAI, OpenRouter, Groq...
    openai_base_url: str = "http://localhost:1234/v1"
    openai_model: str = "qwen2.5-14b-instruct"
    openai_api_key: str = ""  # OPENAI_API_KEY env is used when empty
    # Context window of the OpenAI-compatible model (make sure the server is configured with it).
    openai_context_tokens: int = 16384
    temperature: float = 0.4
    # Force windows of this many minutes (0 = automatic: split only when the transcript doesn't
    # fit the model's context window).
    chunk_minutes: int = 0
    # When the transcript was split, run a final pass that compares the best candidates
    # side by side and re-scores them (makes small local models much more consistent).
    rerank: bool = True
    # Extra guidance appended to the prompt, e.g. "Prefer the funniest moments".
    custom_instructions: str = ""


class ClipsConfig(BaseModel):
    max_clips: int = 8
    min_duration: float = 20.0
    max_duration: float = 60.0
    min_score: int = 0
    # Seconds of breathing room added around the snapped sentence boundaries.
    pad_start: float = 0.15
    pad_end: float = 0.35


class VideoConfig(BaseModel):
    # face: track the speaker's face; center: static center crop; blur: whole frame on a
    # blurred background; auto: face if faces are found, otherwise blur.
    layout: Literal["auto", "face", "center", "blur"] = "auto"
    width: int = 1080
    height: int = 1920
    fps: float = 30.0
    crf: int = 20
    preset: str = "veryfast"
    audio_bitrate: str = "160k"
    # Face tracking
    detect_every_n_frames: int = 3
    # Shots whose subject moves less than this fraction of the crop width get a static crop.
    static_threshold: float = 0.25
    smoothing_seconds: float = 0.8
    face_coverage_for_auto: float = 0.3
    normalize_audio: bool = True


class SubtitleConfig(BaseModel):
    enabled: bool = True
    # karaoke: phrase on screen, active word highlighted; word: one word at a time;
    # phrase: plain phrases, no highlight
    style: Literal["karaoke", "word", "phrase"] = "karaoke"
    font: str = "Montserrat ExtraBold"
    font_size: int = 84
    uppercase: bool = True
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFE600"
    outline_color: str = "#000000"
    outline_width: int = 6
    shadow: int = 2
    max_words: int = 3
    # Vertical position as % of the frame height measured from the bottom.
    position_pct: int = 28
    pop_animation: bool = True
    # Hook/title card shown at the top for the first seconds of the clip.
    hook_enabled: bool = True
    hook_seconds: float = 3.0
    hook_font_size: int = 72
    hook_text_color: str = "#000000"
    hook_box_color: str = "#FFFFFF"


class YouTubeConfig(BaseModel):
    privacy: Literal["public", "unlisted", "private"] = "public"
    category_id: str = "22"  # People & Blogs
    add_shorts_tag: bool = True
    made_for_kids: bool = False


class TikTokConfig(BaseModel):
    # direct: publish straight to the profile; inbox: send to the TikTok app's drafts/inbox
    mode: Literal["direct", "inbox"] = "direct"
    # Unaudited TikTok apps may only post SELF_ONLY.
    privacy_level: Literal["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"] = (
        "SELF_ONLY"
    )
    disable_comment: bool = False
    disable_duet: bool = False
    disable_stitch: bool = False
    client_key: str = ""
    client_secret: str = ""
    # Must be an HTTPS URL registered in the TikTok app (localhost is rejected by TikTok).
    redirect_uri: str = ""
    # Browser-automation uploader (tiktok-uploader) options
    browser: str = "chrome"
    headless: bool = True


class InstagramConfig(BaseModel):
    share_to_feed: bool = True
    graph_api_version: str = "v23.0"
    # auto: resumable upload for Facebook-login tokens, video_url for Instagram-login tokens
    upload_mode: Literal["auto", "resumable", "video_url"] = "auto"


class PublishingConfig(BaseModel):
    interval_minutes: float = 3.0
    order: Literal["score", "chronological"] = "score"
    caption_template: str = "{caption}\n\n{hashtags}"
    max_hashtags: int = 5
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    tiktok: TikTokConfig = Field(default_factory=TikTokConfig)
    instagram: InstagramConfig = Field(default_factory=InstagramConfig)


class StorageConfig(BaseModel):
    # Reject uploads/imports longer than this (minutes).
    max_video_minutes: int = 75
    # Refuse new uploads/processing when the data disk has less free space than this (GB).
    min_free_gb: float = 3.0
    # Delete the (large) source video this many days after its clips were generated; 0 = keep forever.
    # Re-detecting or re-rendering clips needs the source, so those actions disappear afterwards.
    delete_source_after_days: int = 14
    # Delete rendered clip files this many days after they were published everywhere; 0 = keep.
    delete_published_clips_after_days: int = 30


class AppConfig(BaseModel):
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    clips: ClipsConfig = Field(default_factory=ClipsConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    subtitles: SubtitleConfig = Field(default_factory=SubtitleConfig)
    publishing: PublishingConfig = Field(default_factory=PublishingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)


# Dotted paths of fields that hold secrets: masked in API responses.
SECRET_FIELDS = (
    ("llm", "anthropic_api_key"),
    ("llm", "openai_api_key"),
    ("publishing", "tiktok", "client_secret"),
)


def _get_path(d: dict, path: tuple[str, ...]):
    for p in path[:-1]:
        d = d.get(p, {})
    return d.get(path[-1])


def _set_path(d: dict, path: tuple[str, ...], value) -> None:
    for p in path[:-1]:
        d = d.setdefault(p, {})
    d[path[-1]] = value


def mask_secrets(cfg: AppConfig) -> dict:
    data = cfg.model_dump()
    for path in SECRET_FIELDS:
        if _get_path(data, path):
            _set_path(data, path, SECRET_MASK)
    return data


def merge_secrets(incoming: dict, current: AppConfig) -> AppConfig:
    """Keep stored secrets when the UI sends back the mask placeholder."""
    cur = current.model_dump()
    for path in SECRET_FIELDS:
        if _get_path(incoming, path) == SECRET_MASK:
            _set_path(incoming, path, _get_path(cur, path))
    return AppConfig.model_validate(incoming)
