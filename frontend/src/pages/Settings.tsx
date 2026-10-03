import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Captions, CheckCircle2, Cpu, Eye, Film, Mic, RotateCcw, Save, Send, Scissors, XCircle } from "lucide-react";
import { api, type AppConfig, type SystemStatus } from "../api";
import { Alert, Button, Card, CardHeader, ColorInput, Field, NumberInput, Select, Spinner, TextArea, TextInput, Toggle, errMsg, useToast } from "../components/ui";

type Path = (string | number)[];

function setIn<T>(obj: T, path: Path, value: unknown): T {
  if (!path.length) return value as T;
  const [head, ...rest] = path;
  const clone: any = Array.isArray(obj) ? [...(obj as unknown[])] : { ...(obj as object) };
  clone[head] = setIn((obj as any)[head], rest, value);
  return clone;
}

function Section({ id, icon, title, subtitle, children }: { id: string; icon: ReactNode; title: string; subtitle?: string; children: ReactNode }) {
  return (
    <Card>
      <div id={id} className="scroll-mt-6" />
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <span className="text-fuchsia-400">{icon}</span>
            {title}
          </span>
        }
        subtitle={subtitle}
      />
      <div className="grid gap-4 p-5 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
    </Card>
  );
}

function SubHeading({ children }: { children: ReactNode }) {
  return <p className="col-span-full -mb-1 border-t border-zinc-800 pt-3 text-xs font-semibold uppercase tracking-wider text-zinc-500">{children}</p>;
}

function Check({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <div className="flex items-start gap-2 text-sm">
      {ok ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-400" /> : <XCircle className="mt-0.5 size-4 shrink-0 text-amber-400" />}
      <span className="text-zinc-300">{children}</span>
    </div>
  );
}

function SystemCard() {
  const { data } = useQuery({ queryKey: ["system"], queryFn: () => api<SystemStatus>("/api/system") });
  if (!data) return null;
  return (
    <Card>
      <CardHeader title={<span className="flex items-center gap-2"><Cpu className="size-4 text-fuchsia-400" /> System check</span>} subtitle={data.platform} />
      <div className="grid gap-2.5 p-5 sm:grid-cols-2">
        <Check ok={!!data.ffmpeg}>{data.ffmpeg ? data.ffmpeg.replace(/ Copyright.*/, "") : "ffmpeg not found - install it"}</Check>
        <Check ok={data.cuda}>{data.cuda ? "NVIDIA GPU detected - Whisper runs on CUDA" : `No CUDA GPU - Whisper runs on CPU (${data.cpu_count} cores)`}</Check>
        <Check ok={data.whisper_models_cached.length > 0}>
          {data.whisper_models_cached.length ? `Whisper models cached: ${data.whisper_models_cached.join(", ")}` : "No Whisper model downloaded yet (first run downloads it)"}
        </Check>
        <Check ok={data.anthropic_key_in_env || data.openai_key_in_env}>
          {data.anthropic_key_in_env ? "ANTHROPIC_API_KEY found in environment" : data.openai_key_in_env ? "OPENAI_API_KEY found in environment" : "No LLM key in environment (set one below)"}
        </Check>
        <Check ok={data.disk_free_gb > 10}>
          {data.disk_free_gb} GB free in {data.data_dir}
        </Check>
        <Check ok={data.workers_running}>{data.workers_running ? "Background workers running" : "Background workers stopped"}</Check>
      </div>
    </Card>
  );
}

const WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3"].map((m) => ({ value: m, label: m }));
const FONTS = ["Montserrat ExtraBold", "Montserrat Black", "Montserrat Bold", "Arial", "Impact", "Helvetica", "DejaVu Sans"];

export default function SettingsPage() {
  const toast = useToast();
  const qc = useQueryClient();
  const remote = useQuery({ queryKey: ["settings"], queryFn: () => api<AppConfig>("/api/settings") });
  const [cfg, setCfg] = useState<AppConfig | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  useEffect(() => {
    if (remote.data) setCfg(remote.data);
  }, [remote.data]);

  const save = useMutation({
    mutationFn: (c: AppConfig) => api<AppConfig>("/api/settings", { method: "PUT", json: c }),
    onSuccess: (c) => {
      qc.setQueryData(["settings"], c);
      toast("success", "Settings saved - they apply to the next video/clip you process");
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const reset = useMutation({
    mutationFn: () => api<AppConfig>("/api/settings/reset", { method: "POST" }),
    onSuccess: (c) => {
      qc.setQueryData(["settings"], c);
      toast("success", "Defaults restored (API keys kept)");
    },
  });
  const testLlm = useMutation({
    mutationFn: () => api<{ ok: boolean; reply: Record<string, unknown> }>("/api/settings/test-llm", { method: "POST", json: cfg!.llm }),
    onSuccess: (r) => toast("success", `LLM replied: ${JSON.stringify(r.reply)}`),
    onError: (e) => toast("error", errMsg(e)),
  });
  const previewSubs = useMutation({
    mutationFn: async () => {
      const res = await fetch("/api/settings/preview-subtitles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg!.subtitles),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? "Preview failed");
      return URL.createObjectURL(await res.blob());
    },
    onSuccess: (url) => {
      if (preview) URL.revokeObjectURL(preview);
      setPreview(url);
    },
    onError: (e) => toast("error", errMsg(e)),
  });

  if (!cfg)
    return (
      <div className="flex justify-center p-16">
        <Spinner />
      </div>
    );
  const set = (path: Path) => (value: unknown) => setCfg((c) => (c ? setIn(c, path, value) : c));
  const dirty = JSON.stringify(cfg) !== JSON.stringify(remote.data);
  const { llm, transcription: tr, clips, video, subtitles: sub, publishing: pub } = cfg;

  return (
    <div className="space-y-6 pb-20">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-white">Settings</h1>
          <p className="text-sm text-zinc-400">Tune the AI, the look of your clips and how they get posted.</p>
        </div>
        <Button variant="ghost" onClick={() => confirm("Reset all settings to defaults? API keys are kept.") && reset.mutate()}>
          <RotateCcw className="size-4" /> Reset to defaults
        </Button>
      </div>

      <SystemCard />

      <Section id="ai" icon={<Bot className="size-4" />} title="AI highlight detection" subtitle="The LLM reads the transcript and picks the most viral, funny or insightful moments.">
        <Field label="Provider">
          <Select
            value={llm.provider}
            onChange={set(["llm", "provider"])}
            options={[
              { value: "anthropic", label: "Claude (Anthropic API)" },
              { value: "openai_compatible", label: "OpenAI-compatible / local (Ollama, LM Studio…)" },
            ]}
          />
        </Field>
        {llm.provider === "anthropic" ? (
          <>
            <Field label="Model" hint="claude-opus-5-5 is the most capable; sonnet/haiku are cheaper.">
              <TextInput list="claude-models" value={llm.anthropic_model} onChange={(e) => set(["llm", "anthropic_model"])(e.target.value)} />
              <datalist id="claude-models">
                <option value="claude-opus-5-5" />
                <option value="claude-sonnet-5-5" />
                <option value="claude-haiku-4-5" />
              </datalist>
            </Field>
            <Field label="API key" hint="Leave empty to use ANTHROPIC_API_KEY from the environment.">
              <TextInput type="password" value={llm.anthropic_api_key} onChange={(e) => set(["llm", "anthropic_api_key"])(e.target.value)} placeholder="sk-ant-…" />
            </Field>
            <Field label="Effort" hint="Higher = more thinking, better picks, slower.">
              <Select
                value={llm.anthropic_effort}
                onChange={set(["llm", "anthropic_effort"])}
                options={["low", "medium", "high", "xhigh", "max"].map((v) => ({ value: v as AppConfig["llm"]["anthropic_effort"], label: v }))}
              />
            </Field>
          </>
        ) : (
          <>
            <Field label="Base URL" hint="Ollama: http://localhost:11434/v1 · LM Studio: http://localhost:1234/v1">
              <TextInput value={llm.openai_base_url} onChange={(e) => set(["llm", "openai_base_url"])(e.target.value)} />
            </Field>
            <Field label="Model" hint="e.g. qwen2.5:14b-instruct, llama3.1:8b, gpt-4.1-mini">
              <TextInput value={llm.openai_model} onChange={(e) => set(["llm", "openai_model"])(e.target.value)} />
            </Field>
            <Field label="API key" hint="Not needed for Ollama.">
              <TextInput type="password" value={llm.openai_api_key} onChange={(e) => set(["llm", "openai_api_key"])(e.target.value)} />
            </Field>
            <Field label="Temperature">
              <NumberInput value={llm.temperature} step={0.1} min={0} max={2} onChange={set(["llm", "temperature"])} />
            </Field>
          </>
        )}
        <Field label="Chunk transcript (minutes)" hint="0 = send the whole transcript. Use 10-20 for small local models.">
          <NumberInput value={llm.chunk_minutes} min={0} onChange={set(["llm", "chunk_minutes"])} />
        </Field>
        <div className="flex items-end">
          <Button onClick={() => testLlm.mutate()} loading={testLlm.isPending}>
            Test connection
          </Button>
        </div>
        <Field label="Extra instructions for the AI" className="col-span-full" hint='e.g. "Prefer the funniest moments", "Skip sponsor reads", "Our audience is founders".'>
          <TextArea value={llm.custom_instructions} onChange={(e) => set(["llm", "custom_instructions"])(e.target.value)} rows={2} />
        </Field>
      </Section>

      <Section id="clips" icon={<Scissors className="size-4" />} title="Clips" subtitle="How many clips to make and how long they should be.">
        <Field label="Max clips per video">
          <NumberInput value={clips.max_clips} min={1} max={50} onChange={set(["clips", "max_clips"])} />
        </Field>
        <Field label="Min length (s)">
          <NumberInput value={clips.min_duration} min={3} onChange={set(["clips", "min_duration"])} />
        </Field>
        <Field label="Max length (s)" hint="Shorts/Reels/TikTok all accept up to 180s; 30-60s performs best.">
          <NumberInput value={clips.max_duration} min={5} max={180} onChange={set(["clips", "max_duration"])} />
        </Field>
        <Field label="Minimum score" hint="Drop clips the AI scored lower than this (0-100).">
          <NumberInput value={clips.min_score} min={0} max={100} onChange={set(["clips", "min_score"])} />
        </Field>
        <Field label="Padding before (s)">
          <NumberInput value={clips.pad_start} step={0.05} min={0} onChange={set(["clips", "pad_start"])} />
        </Field>
        <Field label="Padding after (s)">
          <NumberInput value={clips.pad_end} step={0.05} min={0} onChange={set(["clips", "pad_end"])} />
        </Field>
      </Section>

      <Section id="transcription" icon={<Mic className="size-4" />} title="Transcription (Whisper)" subtitle="Runs locally with faster-whisper. Bigger models are more accurate but slower.">
        <Field label="Model" hint="CPU: small or base. NVIDIA GPU: large-v3-turbo. Or a local model folder path.">
          <TextInput list="whisper-models" value={tr.model} onChange={(e) => set(["transcription", "model"])(e.target.value)} />
          <datalist id="whisper-models">
            {WHISPER_MODELS.map((m) => (
              <option key={m.value} value={m.value} />
            ))}
          </datalist>
        </Field>
        <Field label="Device">
          <Select
            value={tr.device}
            onChange={set(["transcription", "device"])}
            options={[
              { value: "auto", label: "Auto" },
              { value: "cpu", label: "CPU" },
              { value: "cuda", label: "NVIDIA GPU (CUDA)" },
            ]}
          />
        </Field>
        <Field label="Compute type" hint="auto = int8 on CPU, float16 on GPU">
          <Select
            value={tr.compute_type}
            onChange={set(["transcription", "compute_type"])}
            options={["auto", "int8", "int8_float16", "float16", "float32"].map((v) => ({ value: v, label: v }))}
          />
        </Field>
        <Field label="Language" hint="ISO code like en, es, de - empty = auto-detect">
          <TextInput value={tr.language} onChange={(e) => set(["transcription", "language"])(e.target.value)} placeholder="auto" />
        </Field>
        <Field label="Beam size">
          <NumberInput value={tr.beam_size} min={1} max={10} onChange={set(["transcription", "beam_size"])} />
        </Field>
        <div className="flex items-end pb-2">
          <Toggle checked={tr.vad_filter} onChange={set(["transcription", "vad_filter"])} label="Skip silence (VAD)" />
        </div>
      </Section>

      <Section id="video" icon={<Film className="size-4" />} title="Video & reframing" subtitle="Turning 16:9 into 9:16.">
        <Field label="Layout" hint="auto: face tracking when faces are visible, otherwise blurred background.">
          <Select
            value={video.layout}
            onChange={set(["video", "layout"])}
            options={[
              { value: "auto", label: "Auto" },
              { value: "face", label: "Face tracking crop" },
              { value: "center", label: "Center crop" },
              { value: "blur", label: "Fit on blurred background" },
            ]}
          />
        </Field>
        <Field label="Resolution">
          <Select
            value={`${video.width}x${video.height}`}
            onChange={(v) => {
              const [w, h] = v.split("x").map(Number);
              setCfg((c) => (c ? setIn(setIn(c, ["video", "width"], w), ["video", "height"], h) : c));
            }}
            options={[
              { value: "1080x1920", label: "1080×1920 (Full HD)" },
              { value: "720x1280", label: "720×1280 (faster)" },
            ]}
          />
        </Field>
        <Field label="Frame rate">
          <NumberInput value={video.fps} min={15} max={60} onChange={set(["video", "fps"])} />
        </Field>
        <Field label="Quality (CRF)" hint="Lower = better quality, bigger files. 18-23 is typical.">
          <NumberInput value={video.crf} min={14} max={32} onChange={set(["video", "crf"])} />
        </Field>
        <Field label="Encoder preset">
          <Select
            value={video.preset}
            onChange={set(["video", "preset"])}
            options={["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"].map((v) => ({ value: v, label: v }))}
          />
        </Field>
        <div className="flex items-end pb-2">
          <Toggle checked={video.normalize_audio} onChange={set(["video", "normalize_audio"])} label="Normalize loudness (-14 LUFS)" />
        </div>
        <SubHeading>Face tracking</SubHeading>
        <Field label="Detect faces every N frames">
          <NumberInput value={video.detect_every_n_frames} min={1} max={15} onChange={set(["video", "detect_every_n_frames"])} />
        </Field>
        <Field label="Static-crop threshold" hint="Fraction of crop width the subject may move before the camera pans.">
          <NumberInput value={video.static_threshold} step={0.05} min={0} max={1} onChange={set(["video", "static_threshold"])} />
        </Field>
        <Field label="Pan smoothing (s)">
          <NumberInput value={video.smoothing_seconds} step={0.1} min={0} onChange={set(["video", "smoothing_seconds"])} />
        </Field>
      </Section>

      <Section id="subtitles" icon={<Captions className="size-4" />} title="Subtitles & hook" subtitle="Animated captions burned into the video.">
        <div className="col-span-full grid gap-5 lg:grid-cols-[1fr_270px]">
          <div className="grid content-start gap-4 sm:grid-cols-2">
            <div className="col-span-full flex flex-wrap gap-6">
              <Toggle checked={sub.enabled} onChange={set(["subtitles", "enabled"])} label="Show subtitles" />
              <Toggle checked={sub.uppercase} onChange={set(["subtitles", "uppercase"])} label="UPPERCASE" />
              <Toggle checked={sub.pop_animation} onChange={set(["subtitles", "pop_animation"])} label="Pop-in animation" />
            </div>
            <Field label="Style">
              <Select
                value={sub.style}
                onChange={set(["subtitles", "style"])}
                options={[
                  { value: "karaoke", label: "Karaoke - highlight the spoken word" },
                  { value: "word", label: "One word at a time" },
                  { value: "phrase", label: "Plain phrases" },
                ]}
              />
            </Field>
            <Field label="Font" hint="Montserrat ships with AutoReel; others must be installed.">
              <TextInput list="fonts" value={sub.font} onChange={(e) => set(["subtitles", "font"])(e.target.value)} />
              <datalist id="fonts">
                {FONTS.map((f) => (
                  <option key={f} value={f} />
                ))}
              </datalist>
            </Field>
            <Field label="Font size">
              <NumberInput value={sub.font_size} min={20} max={200} onChange={set(["subtitles", "font_size"])} />
            </Field>
            <Field label="Words per line">
              <NumberInput value={sub.max_words} min={1} max={8} onChange={set(["subtitles", "max_words"])} />
            </Field>
            <Field label="Text color">
              <ColorInput value={sub.primary_color} onChange={set(["subtitles", "primary_color"])} />
            </Field>
            <Field label="Highlight color">
              <ColorInput value={sub.highlight_color} onChange={set(["subtitles", "highlight_color"])} />
            </Field>
            <Field label="Outline color">
              <ColorInput value={sub.outline_color} onChange={set(["subtitles", "outline_color"])} />
            </Field>
            <Field label="Outline width">
              <NumberInput value={sub.outline_width} min={0} max={20} onChange={set(["subtitles", "outline_width"])} />
            </Field>
            <Field label="Shadow">
              <NumberInput value={sub.shadow} min={0} max={10} onChange={set(["subtitles", "shadow"])} />
            </Field>
            <Field label="Height from bottom (%)">
              <NumberInput value={sub.position_pct} min={2} max={90} onChange={set(["subtitles", "position_pct"])} />
            </Field>
            <SubHeading>Hook title card</SubHeading>
            <div className="col-span-full">
              <Toggle checked={sub.hook_enabled} onChange={set(["subtitles", "hook_enabled"])} label="Show the AI hook at the top for the first seconds" />
            </div>
            <Field label="Hook duration (s)">
              <NumberInput value={sub.hook_seconds} step={0.5} min={0.5} onChange={set(["subtitles", "hook_seconds"])} />
            </Field>
            <Field label="Hook font size">
              <NumberInput value={sub.hook_font_size} min={20} max={160} onChange={set(["subtitles", "hook_font_size"])} />
            </Field>
            <Field label="Hook text color">
              <ColorInput value={sub.hook_text_color} onChange={set(["subtitles", "hook_text_color"])} />
            </Field>
            <Field label="Hook box color">
              <ColorInput value={sub.hook_box_color} onChange={set(["subtitles", "hook_box_color"])} />
            </Field>
          </div>
          <div className="space-y-2">
            <div className="flex aspect-[9/16] items-center justify-center overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
              {preview ? <img src={preview} alt="Subtitle preview" className="size-full object-cover" /> : <p className="px-6 text-center text-sm text-zinc-500">Preview how captions look on your latest video</p>}
            </div>
            <Button className="w-full" onClick={() => previewSubs.mutate()} loading={previewSubs.isPending}>
              <Eye className="size-4" /> Preview style
            </Button>
          </div>
        </div>
      </Section>

      <Section id="publishing" icon={<Send className="size-4" />} title="Publishing" subtitle="Defaults for scheduling and per-platform options.">
        <Field label="Minutes between clips">
          <NumberInput value={pub.interval_minutes} step={0.5} min={0} onChange={set(["publishing", "interval_minutes"])} />
        </Field>
        <Field label="Default order">
          <Select
            value={pub.order}
            onChange={set(["publishing", "order"])}
            options={[
              { value: "score", label: "Best score first" },
              { value: "chronological", label: "Chronological" },
            ]}
          />
        </Field>
        <Field label="Max hashtags">
          <NumberInput value={pub.max_hashtags} min={0} max={30} onChange={set(["publishing", "max_hashtags"])} />
        </Field>
        <Field label="Caption template" className="col-span-full" hint="Placeholders: {caption} {hashtags} {title} {hook}">
          <TextArea value={pub.caption_template} onChange={(e) => set(["publishing", "caption_template"])(e.target.value)} rows={3} className="font-mono text-xs" />
        </Field>

        <SubHeading>YouTube</SubHeading>
        <Field label="Privacy">
          <Select
            value={pub.youtube.privacy}
            onChange={set(["publishing", "youtube", "privacy"])}
            options={[
              { value: "public", label: "Public" },
              { value: "unlisted", label: "Unlisted" },
              { value: "private", label: "Private" },
            ]}
          />
        </Field>
        <Field label="Category ID" hint="22 = People & Blogs, 23 = Comedy, 24 = Entertainment, 27 = Education">
          <TextInput value={pub.youtube.category_id} onChange={(e) => set(["publishing", "youtube", "category_id"])(e.target.value)} />
        </Field>
        <div className="flex flex-col justify-end gap-3 pb-1">
          <Toggle checked={pub.youtube.add_shorts_tag} onChange={set(["publishing", "youtube", "add_shorts_tag"])} label='Add "#Shorts" to titles' />
          <Toggle checked={pub.youtube.made_for_kids} onChange={set(["publishing", "youtube", "made_for_kids"])} label="Made for kids" />
        </div>

        <SubHeading>TikTok</SubHeading>
        <Field label="Client key">
          <TextInput value={pub.tiktok.client_key} onChange={(e) => set(["publishing", "tiktok", "client_key"])(e.target.value)} />
        </Field>
        <Field label="Client secret">
          <TextInput type="password" value={pub.tiktok.client_secret} onChange={(e) => set(["publishing", "tiktok", "client_secret"])(e.target.value)} />
        </Field>
        <Field label="Redirect URI (HTTPS)" hint="Must match the TikTok app. Pointing it at <tunnel>/api/oauth/tiktok/callback finishes automatically.">
          <TextInput value={pub.tiktok.redirect_uri} onChange={(e) => set(["publishing", "tiktok", "redirect_uri"])(e.target.value)} placeholder="https://…/api/oauth/tiktok/callback" />
        </Field>
        <Field label="Mode">
          <Select
            value={pub.tiktok.mode}
            onChange={set(["publishing", "tiktok", "mode"])}
            options={[
              { value: "direct", label: "Direct post" },
              { value: "inbox", label: "Send to TikTok inbox (draft)" },
            ]}
          />
        </Field>
        <Field label="Privacy" hint="Unaudited apps are forced to Only me.">
          <Select
            value={pub.tiktok.privacy_level}
            onChange={set(["publishing", "tiktok", "privacy_level"])}
            options={[
              { value: "PUBLIC_TO_EVERYONE", label: "Everyone" },
              { value: "MUTUAL_FOLLOW_FRIENDS", label: "Friends" },
              { value: "FOLLOWER_OF_CREATOR", label: "Followers" },
              { value: "SELF_ONLY", label: "Only me" },
            ]}
          />
        </Field>
        <div className="flex flex-col justify-end gap-2 pb-1">
          <Toggle checked={pub.tiktok.disable_comment} onChange={set(["publishing", "tiktok", "disable_comment"])} label="Disable comments" />
          <Toggle checked={pub.tiktok.disable_duet} onChange={set(["publishing", "tiktok", "disable_duet"])} label="Disable duets" />
          <Toggle checked={pub.tiktok.disable_stitch} onChange={set(["publishing", "tiktok", "disable_stitch"])} label="Disable stitches" />
        </div>
        <Field label="Browser uploader: browser">
          <Select
            value={pub.tiktok.browser}
            onChange={set(["publishing", "tiktok", "browser"])}
            options={["chrome", "chromium", "firefox", "edge", "safari"].map((v) => ({ value: v, label: v }))}
          />
        </Field>
        <div className="flex items-end pb-2">
          <Toggle checked={pub.tiktok.headless} onChange={set(["publishing", "tiktok", "headless"])} label="Run browser headless" />
        </div>

        <SubHeading>Instagram</SubHeading>
        <Field label="Upload mode" hint="auto: resumable for Facebook-login tokens, public URL for Instagram-login tokens">
          <Select
            value={pub.instagram.upload_mode}
            onChange={set(["publishing", "instagram", "upload_mode"])}
            options={[
              { value: "auto", label: "Auto" },
              { value: "resumable", label: "Resumable upload" },
              { value: "video_url", label: "Public video URL" },
            ]}
          />
        </Field>
        <Field label="Graph API version">
          <TextInput value={pub.instagram.graph_api_version} onChange={(e) => set(["publishing", "instagram", "graph_api_version"])(e.target.value)} />
        </Field>
        <div className="flex items-end pb-2">
          <Toggle checked={pub.instagram.share_to_feed} onChange={set(["publishing", "instagram", "share_to_feed"])} label="Also show Reels in the main feed" />
        </div>
      </Section>

      {remote.error && <Alert kind="error">{errMsg(remote.error)}</Alert>}

      <div className="fixed right-6 bottom-6 z-30 flex items-center gap-3 rounded-xl border border-zinc-700 bg-zinc-900/95 px-4 py-3 shadow-2xl backdrop-blur">
        <span className="text-sm text-zinc-400">{dirty ? "Unsaved changes" : "All changes saved"}</span>
        <Button variant="primary" onClick={() => save.mutate(cfg)} loading={save.isPending} disabled={!dirty}>
          <Save className="size-4" /> Save
        </Button>
      </div>
    </div>
  );
}
