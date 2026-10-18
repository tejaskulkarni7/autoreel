# Deploying AutoReel

There are two ways to run AutoReel:

- **A cloud server** (recommended if your computer is slow or you want posting to keep running when
  your laptop is off).
- **Your own computer**, with or without Docker.

Either way, you still have to sort out two things yourself: **compute** (where it runs) and **API
credentials** (Claude or Ollama, plus YouTube, TikTok and Instagram). Everything else is set up
below.

---

## Option A: cloud server with Docker and automatic HTTPS (~$10-50/month)

### 1. Pick a server

| Videos per month | Server | Example |
|---|---|---|
| A few hours | 4 vCPU / 8 GB RAM / 80+ GB disk | Hetzner CPX31 or CX42, DigitalOcean 4 vCPU/8 GB |
| 20+ hours | 8 vCPU / 16 GB RAM / 160 GB disk | Hetzner CPX51 or CCX33 |

Choose **Ubuntu 24.04**. No GPU is needed. With the default Whisper `small` model, transcribing a
1-hour video takes roughly 20-40 minutes on 4-8 vCPUs, and rendering 8 clips adds another 5-10
minutes. All of this runs in the background.

### 2. Point a domain at it

Create a DNS **A record**, such as `clips.yourdomain.com`, pointing to the server's IP address.
HTTPS is required for TikTok's login redirect and is strongly recommended in general.

### 3. Install and start

```bash
ssh root@YOUR_SERVER_IP
curl -fsSL https://get.docker.com | sh          # installs Docker + compose
git clone https://github.com/tejaskulkarni7/autoreel && cd autoreel
cp .env.example .env
nano .env    # set DOMAIN, AUTOREEL_ADMIN_PASSWORD (long random string), ANTHROPIC_API_KEY
docker compose up -d --build
```

After a minute or two, open `https://clips.yourdomain.com` and log in as **admin** with your
password. Caddy obtains the HTTPS certificate automatically, and the firewall needs ports 80 and 443
open.

- Firewall: `ufw allow 22,80,443/tcp && ufw enable`. The app port (8000) is never exposed directly.
- Updates: `git pull && docker compose up -d --build`
- Logs: `docker compose logs -f app`
- Data: videos, clips, the database, downloaded models and account tokens all live in the Docker
  volume `autoreel_autoreel-data`. Back it up with:
  `docker run --rm -v autoreel_autoreel-data:/d -v $PWD:/b ubuntu tar czf /b/autoreel-backup.tgz -C /d .`

### 4. Optional extras

- **Fully local AI (no Claude bill):** run `docker compose --profile ollama up -d`, then
  `docker compose exec ollama ollama pull qwen2.5:14b`. In **Settings → AI**, choose *Ollama* with
  URL `http://ollama:11434`. A 14B model needs about 12 GB of RAM on top of the app, so use a 16-32
  GB server. CPU-only inference of a 1-hour transcript takes several minutes per chunk.
- **Browser-automation TikTok uploader:** set `TIKTOK_BROWSER=true` in `.env` and rebuild. This adds
  Chromium (about 400 MB).

---

## Option B: your own computer

**With Docker** (only reachable from your computer, no password needed):

```bash
docker compose -f docker-compose.local.yml up -d --build    # → http://localhost:8000
```

**Without Docker**, using the steps in the README: `uv pip install -e .`, build the frontend, then
`autoreel serve`. This is the way to use an **NVIDIA GPU** for Whisper. Install the CUDA 12 and
cuDNN 9 runtime, and AutoReel detects the GPU automatically.

On your own computer, connecting **TikTok** (which needs an HTTPS redirect) or Instagram with an
Instagram-login token (which needs a public video URL) requires a tunnel, for example:

```bash
cloudflared tunnel --url http://localhost:8000
# then: AUTOREEL_PUBLIC_BASE_URL=https://<random>.trycloudflare.com AUTOREEL_ADMIN_PASSWORD=... autoreel serve
```

---

## After it's running: connect credentials (≈1 hour, one time)

Do these in the app's **Settings** and **Accounts** pages. The README has the details for each
platform.

1. **AI:** paste your Anthropic API key in Settings → AI (or set `ANTHROPIC_API_KEY`), or choose
   Ollama. Click **Test connection**.
2. **YouTube:** create a Google Cloud OAuth client (Web application) with the redirect URI shown on
   the Accounts page. That URI is `https://YOUR_DOMAIN/api/oauth/youtube/callback` on a server.
   Upload the client JSON, then click Connect.
3. **TikTok:** create a TikTok developer app and use `https://YOUR_DOMAIN/api/oauth/tiktok/callback`
   as its redirect URI. Enter the client key and secret in Settings → Publishing, then click Connect
   on the Accounts page.
4. **Instagram:** paste a long-lived token. A Facebook-login token is recommended.
5. On each account, click **Test post**. YouTube uploads a *private* video, TikTok posts an *Only me*
   video, and Instagram uploads and processes the video *without publishing*. Check the result under
   Publishing → All, then delete the private test posts.

Once every account passes its test post, you're live. Upload a video, review the clips and hit
**Publish**.

## Platform reviews (outside AutoReel)

- **YouTube:** uploads stay private until your Google project passes the YouTube API audit. Apply
  early, because it can take a few weeks. The default quota allows about 6 uploads per day.
- **TikTok:** posts are private until TikTok audits your app. Until then, use *inbox* mode (drafts
  you publish from the app) or the browser uploader.
- **Instagram:** no review is needed to post to your own Professional account.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Transcription fails with "Could not load Whisper model" | The server can't reach huggingface.co. Check outbound internet access or DNS. |
| "Less than N GB free" | Delete old videos, or lower the retention days in Settings → Storage |
| YouTube "quotaExceeded" | It resets daily. Space out posts or request a higher quota. |
| URL import fails from a cloud server | YouTube often blocks data-center IP addresses. Upload the file instead. |
| Local model picks are poor | Use a bigger model, raise the context window, and keep "Final ranking pass" on |
