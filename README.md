# AutoReel

AutoReel is a local app that turns long videos (podcasts, interviews, streams, talks) into vertical
shorts with burned-in subtitles. It then posts them to **YouTube Shorts**, **TikTok** and
**Instagram Reels** on a schedule you set. Everything runs on your machine. A web admin lets you
upload videos, review and edit the clips, change settings and hit **Publish**. AutoReel then posts
the clips a few minutes apart.

```
long video ─► Whisper transcript ─► LLM picks moments ─► 9:16 face-tracked crop ─► animated subtitles + hook
                                                                                      │
                     YouTube Shorts ◄─┬─ scheduler (one clip every N minutes) ◄───────┘
                     TikTok         ◄─┤
                     Instagram      ◄─┘
```

## Features

- **Upload or import**: drag in a file, or paste a URL that `yt-dlp` can download.
- **Local transcription**: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) with word-level
  timestamps. Runs on CPU (int8) or an NVIDIA GPU.
- **AI highlight detection**: Claude (Anthropic API) or any **OpenAI-compatible** endpoint. For a
  fully offline setup, use Ollama, LM Studio, vLLM or llama.cpp. The model scores each moment for
  hook, payoff, humor and shareability, and writes a title, an on-screen hook, a caption and
  hashtags. Clip edges are then snapped to real sentence boundaries, length limits are enforced and
  overlapping clips are removed.
- **Smart 9:16 reframing**: OpenCV's YuNet face detector (with a Haar fallback) and shot-cut
  detection. The crop stays still while the speaker barely moves (like a real camera operator), pans
  smoothly when they do, and jumps on hard cuts. Two-person shots are framed together when both fit.
  Shots with no faces use a blurred-background layout.
- **TikTok-style captions**: karaoke highlighting of the spoken word, one word at a time, or plain
  phrases. You can set the font, colors, outline, position, pop-in animation and a hook title card
  for the first seconds. Montserrat is bundled. Settings has a live preview.
- **Loudness normalization** to -14 LUFS, which is the level the platforms use.
- **Review and edit**: approve or skip clips, change the title, hook, caption or hashtags, nudge the
  start and end times using the transcript, add clips by hand, and re-render.
- **Staggered publishing**: "Publish" gives each clip its own time slot (for example every 3
  minutes), and each slot posts to every account you selected. You can pause and resume (the
  remaining schedule shifts to keep its spacing), post a clip now, cancel, retry or cancel a whole
  batch. Network errors are retried automatically.
- **Posting**: official APIs for YouTube, TikTok and Instagram, plus an optional browser-automation
  TikTok uploader (see the TikTok section for why you might need it).

## How it's built (and what it reuses)

AutoReel combines well-tested open-source parts instead of writing everything from scratch:

| Concern | Library / approach |
|---|---|
| Transcription | `faster-whisper` (CTranslate2 Whisper) |
| Highlights | Anthropic SDK (Claude, JSON-schema structured output) / `openai` SDK for local or other providers |
| Face detection | OpenCV `FaceDetectorYN` (YuNet from opencv_zoo) |
| Rendering | `ffmpeg` + libass (ASS subtitles) |
| URL import | `yt-dlp` |
| YouTube | `google-api-python-client` + `google-auth-oauthlib` |
| TikTok / Instagram | Official REST APIs via `httpx`; optional [`tiktok-uploader`](https://github.com/wkaisertexas/tiktok-uploader) |
| App | FastAPI + SQLite (SQLModel) · React + Vite + Tailwind + TanStack Query |

The pipeline is modeled on proven open-source clippers such as
[OpenShorts](https://github.com/mutonby/openshorts),
[ClipsAI](https://github.com/ClipsAI/clipsai) and
[AI-Youtube-Shorts-Generator](https://github.com/SamurAIGPT/AI-Youtube-Shorts-Generator). Those
projects post through paid aggregators (Upload-Post) or don't post at all. AutoReel posts directly to
your own accounts, with no third-party service in between.

## Requirements

- Python 3.10+ (with [uv](https://docs.astral.sh/uv/) recommended)
- Node.js 20+ (only needed to build the web UI)
- `ffmpeg` built with libass (`brew install ffmpeg` or `sudo apt install ffmpeg`)
- An LLM: an Anthropic API key, **or** a local model server such as [Ollama](https://ollama.com)

### Will my machine handle it?

| Step | CPU only | NVIDIA GPU |
|---|---|---|
| Transcription (Whisper `small`, int8) | about 0.2-0.5× real time on a modern 8-core CPU (a 1-hour podcast takes ~15-30 min) | `large-v3-turbo`: about 1-2 min per hour of audio |
| Highlights via Claude | seconds to ~1 min (cloud). A 1-hour transcript is ~15k tokens, a few cents per video | same |
| Highlights via local LLM | a 7-8B model works on 16 GB RAM but is slow; 14B+ gives better picks | fast |
| Rendering (1080×1920, x264 `veryfast`) | ~0.5-1× real time per clip | same (CPU encode) |

If your machine struggles, pick Whisper `base`, render at 720×1280, and use Claude for highlight
detection. On Apple Silicon, faster-whisper runs on the CPU, which is fast enough with `small`.

## Quick start

```bash
git clone https://github.com/tejaskulkarni7/autoreel && cd autoreel

# backend
uv venv && source .venv/bin/activate
uv pip install -e .

# web UI
(cd frontend && npm install && npm run build)

# optional: LLM key (you can also paste it in Settings)
export ANTHROPIC_API_KEY=sk-ant-...

autoreel doctor        # checks ffmpeg, GPU, UI build
autoreel serve         # → http://localhost:8000
```

Then open <http://localhost:8000>, go to **Settings** to check the AI provider and clip length, drop
a video on **Videos**, and watch the progress bar. The first run downloads the Whisper model (about
500 MB for `small`) and the face detector.

**Fully local (no cloud AI):** install Ollama, run `ollama pull qwen2.5:14b-instruct` (or a smaller
model), then in **Settings → AI** choose *OpenAI-compatible*, base URL `http://localhost:11434/v1`
and chunk minutes `15`.

**Development:** run `autoreel serve --reload` together with `cd frontend && npm run dev` (Vite on
:5173 proxies the API).

**Tests:** `uv pip install -e ".[dev]" && pytest`

## Connecting accounts

All connections live on the **Accounts** page. Credentials are stored in the local SQLite database
under `data/`, so keep that folder private.

### YouTube Shorts (official API)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable **YouTube
   Data API v3**, and configure the OAuth consent screen. Add yourself as a test user.
2. Create an OAuth client of type **Web application** with the redirect URI shown on the Accounts
   page (`http://localhost:8000/api/oauth/youtube/callback`).
3. Upload the downloaded client JSON, then click **Connect YouTube channel**.

> **Limits.** Each upload costs 1,600 quota units and the default quota is 10,000 per day, so about
> **6 uploads per day**. Videos uploaded from an unverified API project are locked to **private**
> until the project passes Google's free
> [API audit](https://support.google.com/youtube/contact/yt_api_form). Vertical videos up to 3
> minutes are classified as Shorts automatically.

### TikTok (official Content Posting API)

1. Create an app at [developers.tiktok.com](https://developers.tiktok.com/). Add **Login Kit** and
   **Content Posting API** (enable *Direct Post*), with scopes `user.info.basic`, `video.publish`
   and `video.upload`.
2. TikTok requires an **HTTPS redirect URI and rejects localhost**. You have two options:
   - Point it at this app through a tunnel, for example
     `cloudflared tunnel --url http://localhost:8000`. Then set `AUTOREEL_PUBLIC_BASE_URL` and use
     `https://<tunnel>/api/oauth/tiktok/callback`. Set `AUTOREEL_ADMIN_PASSWORD` when you do this.
   - Use any HTTPS page you control. After you authorize, copy the URL you were redirected to and
     paste it into **Finish** on the Accounts page.
3. Enter the client key, client secret and redirect URI under **Settings → Publishing → TikTok**,
   then click **Open TikTok authorization**.

> **Limits.** Until TikTok audits your app, every API post is **private (SELF_ONLY)**, the account
> must be private, and at most 5 users can post per day. *Inbox* mode instead sends the clip to your
> TikTok drafts so you can publish it from the app.

### TikTok (browser automation, optional)

If TikTok won't audit a personal app, you can post publicly with the open-source
[`tiktok-uploader`](https://github.com/wkaisertexas/tiktok-uploader). It drives a real browser
logged in with your cookies.

```bash
uv pip install -e ".[tiktok-browser]" && playwright install chromium
```

Export your tiktok.com cookies in Netscape `cookies.txt` format (for example with a "Get
cookies.txt" browser extension) and upload the file on the Accounts page. This is unofficial: it can
break when TikTok changes its site, and automated posting may conflict with TikTok's terms.

### Instagram Reels (official Graph API)

You need an Instagram **Professional** (Business or Creator) account and a Meta app from
[developers.facebook.com](https://developers.facebook.com/). For your own account, where you are the
app admin, no app review is needed.

- **Facebook Login (recommended):** link the IG account to a Facebook Page and generate a long-lived
  token with `instagram_basic`, `instagram_content_publish` and `pages_show_list`. AutoReel uploads
  the clip file directly with Meta's resumable upload, so no public URL is needed.
- **Instagram Login:** a token with `instagram_business_content_publish`. Instagram downloads the
  video from a public URL, so set `AUTOREEL_PUBLIC_BASE_URL` to a tunnel. Clips are served at
  unguessable `/public/clips/<token>.mp4` URLs. AutoReel refreshes these 60-day tokens weekly.

> **Limits.** 50 API-published posts per 24 hours per account. Reels must be 3-90 s to show in the
> Reels tab; longer videos are posted but may not get Reels distribution.

## Configuration

Most settings are edited in the UI and stored in the database. Process-level settings come from
environment variables or `.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `AUTOREEL_DATA_DIR` | `data` | Videos, clips, models, database |
| `AUTOREEL_HOST` / `AUTOREEL_PORT` | `127.0.0.1` / `8000` | Where the app listens |
| `AUTOREEL_ADMIN_PASSWORD` | – | HTTP basic auth (user `admin`) for the whole UI/API |
| `AUTOREEL_PUBLIC_BASE_URL` | – | Public HTTPS URL (tunnel) for the TikTok OAuth redirect and Instagram `video_url` uploads |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | – | LLM credentials (alternatively set in Settings) |

## Project layout

```
autoreel/
  pipeline/      transcribe · highlights (+llm) · reframe · subtitles · render · runner
  publishers/    youtube · tiktok · tiktok_browser · instagram
  api/           REST endpoints (videos, clips, publish, accounts, settings)
  worker.py      task worker (one subprocess per job) + publish scheduler
  main.py        FastAPI app, serves the built UI
frontend/        React admin (Vite + Tailwind)
tests/           pytest suite (pipeline, API, scheduler, publishers with mocked HTTP)
```

## Notes and caveats

- Only clip and post content you own or have the rights to.
- The worker handles one heavy job at a time, which keeps a laptop usable. Jobs survive restarts. If
  the app restarts mid-upload, that post is marked failed instead of retried blindly, so you never
  get a double post.
- Platform rules change often. If an upload fails, the queue shows the exact API error and you can
  retry it.
