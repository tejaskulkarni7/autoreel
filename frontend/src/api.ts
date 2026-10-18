// Typed client for the AutoReel REST API.

export type TaskStatus = "queued" | "running" | "done" | "failed" | "canceled";

export interface Task {
  id: string;
  kind: "process_video" | "render_clip" | "download";
  video_id: string | null;
  clip_id: string | null;
  status: TaskStatus;
  stage: string | null;
  progress: number;
  message: string | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface PublishState {
  status: JobStatus;
  url: string | null;
  scheduled_at: string;
  error: string | null;
}

export interface Clip {
  id: string;
  video_id: string;
  rank: number;
  start: number;
  end: number;
  duration: number;
  title: string;
  hook: string;
  caption: string;
  hashtags: string[];
  score: number;
  reason: string;
  category: string;
  layout: string | null;
  status: "pending" | "rendering" | "ready" | "failed" | "archived";
  error: string | null;
  approved: boolean;
  video_url: string | null;
  thumb_url: string | null;
  render_version: number;
  publish: Record<string, PublishState>;
  created_at: string;
  updated_at: string;
}

export type VideoStatus = "uploaded" | "queued" | "processing" | "ready" | "failed" | "canceled";

export interface Video {
  id: string;
  title: string;
  source_type: "upload" | "url";
  source_url: string | null;
  filename: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  size_bytes: number | null;
  language: string | null;
  status: VideoStatus;
  stage: string | null;
  progress: number;
  error: string | null;
  source_deleted: boolean;
  clip_count: number;
  ready_clip_count: number;
  source_url_media: string | null;
  thumb_url: string | null;
  task: Task | null;
  created_at: string;
  updated_at: string;
  clips?: Clip[];
  clip_tasks?: Record<string, TaskStatus>;
}

export type JobStatus = "scheduled" | "running" | "published" | "failed" | "canceled";

export interface PublishJob {
  id: string;
  batch_id: string;
  clip_id: string;
  account_id: string;
  platform: Platform;
  scheduled_at: string;
  status: JobStatus;
  attempts: number;
  external_id: string | null;
  url: string | null;
  error: string | null;
  created_at: string;
  published_at: string | null;
  test: boolean;
  clip: { title: string; thumb_url: string | null; video_id: string; score: number } | null;
  account: { name: string; platform: Platform } | null;
}

export type Platform = "youtube" | "tiktok" | "tiktok_browser" | "instagram";

export interface Account {
  id: string;
  platform: Platform;
  name: string;
  external_id: string | null;
  enabled: boolean;
  last_error: string | null;
  details: Record<string, string>;
  created_at: string;
}

export interface AccountsResponse {
  accounts: Account[];
  platforms: Record<Platform, string>;
  setup: {
    youtube: { client_configured: boolean; client_type: string | null; redirect_uri: string };
    tiktok: { client_configured: boolean; redirect_uri: string; browser_uploader_installed: boolean };
    instagram: { public_base_url: string };
  };
}

export interface SystemStatus {
  python: string;
  platform: string;
  cpu_count: number;
  ffmpeg: string | null;
  cuda: boolean;
  whisper_models_cached: string[];
  data_dir: string;
  disk_free_gb: number;
  anthropic_key_in_env: boolean;
  openai_key_in_env: boolean;
  tiktok_browser_uploader: boolean;
  public_base_url: string;
  auth_enabled: boolean;
  workers_running: boolean;
  current_task: Task | null;
  queued_tasks: number;
}

export interface AppConfig {
  transcription: {
    model: string;
    device: "auto" | "cpu" | "cuda";
    compute_type: string;
    language: string;
    beam_size: number;
    vad_filter: boolean;
  };
  llm: {
    provider: "anthropic" | "ollama" | "openai_compatible";
    anthropic_model: string;
    anthropic_api_key: string;
    anthropic_effort: "low" | "medium" | "high" | "xhigh" | "max";
    ollama_base_url: string;
    ollama_model: string;
    ollama_num_ctx: number;
    openai_base_url: string;
    openai_model: string;
    openai_api_key: string;
    openai_context_tokens: number;
    temperature: number;
    chunk_minutes: number;
    rerank: boolean;
    custom_instructions: string;
  };
  clips: {
    max_clips: number;
    min_duration: number;
    max_duration: number;
    min_score: number;
    pad_start: number;
    pad_end: number;
  };
  video: {
    layout: "auto" | "face" | "center" | "blur";
    width: number;
    height: number;
    fps: number;
    crf: number;
    preset: string;
    audio_bitrate: string;
    detect_every_n_frames: number;
    static_threshold: number;
    smoothing_seconds: number;
    face_coverage_for_auto: number;
    normalize_audio: boolean;
  };
  subtitles: SubtitleConfig;
  publishing: {
    interval_minutes: number;
    order: "score" | "chronological";
    caption_template: string;
    max_hashtags: number;
    youtube: { privacy: "public" | "unlisted" | "private"; category_id: string; add_shorts_tag: boolean; made_for_kids: boolean };
    tiktok: {
      mode: "direct" | "inbox";
      privacy_level: "PUBLIC_TO_EVERYONE" | "MUTUAL_FOLLOW_FRIENDS" | "FOLLOWER_OF_CREATOR" | "SELF_ONLY";
      disable_comment: boolean;
      disable_duet: boolean;
      disable_stitch: boolean;
      client_key: string;
      client_secret: string;
      redirect_uri: string;
      browser: string;
      headless: boolean;
    };
    instagram: { share_to_feed: boolean; graph_api_version: string; upload_mode: "auto" | "resumable" | "video_url" };
  };
  storage: {
    max_video_minutes: number;
    min_free_gb: number;
    delete_source_after_days: number;
    delete_published_clips_after_days: number;
  };
}

export interface SubtitleConfig {
  enabled: boolean;
  style: "karaoke" | "word" | "phrase";
  font: string;
  font_size: number;
  uppercase: boolean;
  primary_color: string;
  highlight_color: string;
  outline_color: string;
  outline_width: number;
  shadow: number;
  max_words: number;
  position_pct: number;
  pop_animation: boolean;
  hook_enabled: boolean;
  hook_seconds: number;
  hook_font_size: number;
  hook_text_color: string;
  hook_box_color: string;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function errorMessage(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail))
      return detail
        .map((d) => (d && typeof d === "object" && "msg" in d ? `${(d.loc ?? []).slice(-2).join(".")}: ${d.msg}` : String(d)))
        .join("; ");
  }
  return fallback;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const { json, headers, ...rest } = init;
  const res = await fetch(path, {
    ...rest,
    headers: json !== undefined ? { "Content-Type": "application/json", ...headers } : headers,
    body: json !== undefined ? JSON.stringify(json) : rest.body,
  });
  const text = await res.text();
  let body: unknown = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) throw new ApiError(res.status, errorMessage(body, `${res.status} ${res.statusText}`));
  return body as T;
}

export function uploadVideo(
  file: File,
  opts: { title: string; autoProcess: boolean },
  onProgress: (fraction: number) => void,
): Promise<Video> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);
    form.append("title", opts.title);
    form.append("auto_process", String(opts.autoProcess));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/videos");
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* ignore */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as Video);
      else reject(new ApiError(xhr.status, errorMessage(body, `Upload failed (${xhr.status})`)));
    };
    xhr.onerror = () => reject(new ApiError(0, "Network error during upload"));
    xhr.send(form);
  });
}

export const PLATFORM_LABEL: Record<Platform, string> = {
  youtube: "YouTube Shorts",
  tiktok: "TikTok",
  tiktok_browser: "TikTok (browser)",
  instagram: "Instagram Reels",
};
