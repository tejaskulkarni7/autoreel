import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Copy, ExternalLink, FileJson, PlugZap, Trash2, Upload } from "lucide-react";
import { api, type Account, type AccountsResponse, type Platform } from "../api";
import { PlatformIcon } from "../components/PlatformIcon";
import { Alert, Badge, Button, Card, CardHeader, Field, Select, Spinner, TextArea, TextInput, Toggle, errMsg, useToast } from "../components/ui";
import { fmtDateTime } from "../format";

function Copyable({ text }: { text: string }) {
  const toast = useToast();
  return (
    <span className="inline-flex max-w-full items-center gap-1 rounded-md bg-zinc-950 px-2 py-1 font-mono text-xs text-zinc-300 ring-1 ring-zinc-800">
      <span className="truncate">{text}</span>
      <button
        onClick={() => {
          navigator.clipboard?.writeText(text);
          toast("success", "Copied");
        }}
        className="shrink-0 text-zinc-500 hover:text-white"
        title="Copy"
      >
        <Copy className="size-3" />
      </button>
    </span>
  );
}

function AccountRow({ a, onChange }: { a: Account; onChange: () => void }) {
  const toast = useToast();
  const test = useMutation({
    mutationFn: () => api<Account>(`/api/accounts/${a.id}/test`, { method: "POST" }),
    onSuccess: (r) => {
      toast("success", `Connection OK: ${r.name}`);
      onChange();
    },
    onError: (e) => {
      toast("error", errMsg(e));
      onChange();
    },
  });
  const patch = useMutation({
    mutationFn: (enabled: boolean) => api(`/api/accounts/${a.id}`, { method: "PATCH", json: { enabled } }),
    onSuccess: onChange,
  });
  const del = useMutation({
    mutationFn: () => api(`/api/accounts/${a.id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast("success", "Account removed");
      onChange();
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const expires = a.details.expires_at || a.details.refresh_expires_at;
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-2.5">
      <PlatformIcon platform={a.platform} className="size-6" />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-white">{a.name}</p>
        <p className="text-xs text-zinc-500">
          connected {fmtDateTime(a.created_at)}
          {a.details.login_type && ` · ${a.details.login_type} login`}
          {expires && ` · token refresh until ${fmtDateTime(a.details.refresh_expires_at ?? expires)}`}
        </p>
        {a.last_error && <p className="mt-0.5 line-clamp-2 text-xs text-red-400">{a.last_error}</p>}
      </div>
      <Toggle checked={a.enabled} onChange={(v) => patch.mutate(v)} label={<span className="text-xs">{a.enabled ? "Posting" : "Off"}</span>} />
      <Button size="sm" variant="secondary" onClick={() => test.mutate()} loading={test.isPending}>
        <PlugZap className="size-3.5" /> Test
      </Button>
      <Button size="sm" variant="ghost" onClick={() => confirm(`Remove ${a.name}? Scheduled posts to it are deleted.`) && del.mutate()}>
        <Trash2 className="size-3.5" />
      </Button>
    </div>
  );
}

function PlatformCard({
  platform,
  title,
  subtitle,
  accounts,
  onChange,
  children,
}: {
  platform: Platform;
  title: string;
  subtitle: ReactNode;
  accounts: Account[];
  onChange: () => void;
  children: ReactNode;
}) {
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <PlatformIcon platform={platform} className="size-6" /> {title}
            {accounts.length > 0 && (
              <Badge color="green">
                <CheckCircle2 className="size-3" /> {accounts.length} connected
              </Badge>
            )}
          </span>
        }
        subtitle={subtitle}
      />
      <div className="space-y-4 p-5">
        {accounts.length > 0 && (
          <div className="space-y-2">
            {accounts.map((a) => (
              <AccountRow key={a.id} a={a} onChange={onChange} />
            ))}
          </div>
        )}
        {children}
      </div>
    </Card>
  );
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <div className="flex gap-3">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-zinc-800 text-xs font-semibold text-zinc-300">{n}</span>
      <div className="min-w-0 flex-1 space-y-2">
        <p className="text-sm font-medium text-zinc-100">{title}</p>
        {children}
      </div>
    </div>
  );
}

export default function AccountsPage() {
  const toast = useToast();
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const { data, isLoading } = useQuery({ queryKey: ["accounts"], queryFn: () => api<AccountsResponse>("/api/accounts") });
  const refresh = () => qc.invalidateQueries({ queryKey: ["accounts"] });
  const ytFile = useRef<HTMLInputElement>(null);
  const cookiesFile = useRef<HTMLInputElement>(null);
  const [ttPaste, setTtPaste] = useState("");
  const [ttBrowserName, setTtBrowserName] = useState("");
  const [igToken, setIgToken] = useState("");
  const [igLogin, setIgLogin] = useState<"instagram" | "facebook">("facebook");
  const [igUserId, setIgUserId] = useState("");

  useEffect(() => {
    const connected = params.get("connected");
    const error = params.get("error");
    if (connected) toast("success", `${connected} account connected`);
    if (error) toast("error", `Connection failed: ${error}`);
    if (connected || error) setParams({}, { replace: true });
  }, [params, setParams, toast]);

  const uploadYtClient = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api("/api/accounts/youtube/client-config", { method: "POST", body: form });
    },
    onSuccess: () => {
      toast("success", "Google OAuth client saved");
      refresh();
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const connectYt = useMutation({
    mutationFn: () => api<{ url: string }>("/api/accounts/youtube/authorize"),
    onSuccess: (r) => (window.location.href = r.url),
    onError: (e) => toast("error", errMsg(e)),
  });
  const connectTt = useMutation({
    mutationFn: () => api<{ url: string }>("/api/accounts/tiktok/authorize"),
    onSuccess: (r) => window.open(r.url, "_blank"),
    onError: (e) => toast("error", errMsg(e)),
  });
  const completeTt = useMutation({
    mutationFn: () => api<Account>("/api/accounts/tiktok/complete", { method: "POST", json: { redirect_url: ttPaste } }),
    onSuccess: (a) => {
      toast("success", `Connected ${a.name}`);
      setTtPaste("");
      refresh();
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const addTtBrowser = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("name", ttBrowserName || "TikTok (browser)");
      form.append("cookies", file);
      return api<Account>("/api/accounts/tiktok-browser", { method: "POST", body: form });
    },
    onSuccess: () => {
      toast("success", "TikTok browser account added");
      setTtBrowserName("");
      refresh();
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const addIg = useMutation({
    mutationFn: () => api<Account>("/api/accounts/instagram", { method: "POST", json: { access_token: igToken, login_type: igLogin, ig_user_id: igUserId } }),
    onSuccess: (a) => {
      toast("success", `Connected @${a.name}`);
      setIgToken("");
      refresh();
    },
    onError: (e) => toast("error", errMsg(e)),
  });

  if (isLoading || !data)
    return (
      <div className="flex justify-center p-16">
        <Spinner />
      </div>
    );
  const of = (p: Platform) => data.accounts.filter((a) => a.platform === p);
  const { setup } = data;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-white">Accounts</h1>
        <p className="text-sm text-zinc-400">Connect the channels clips get posted to. Everything stays on this machine.</p>
      </div>

      <PlatformCard
        platform="youtube"
        title="YouTube Shorts"
        subtitle="Official YouTube Data API. Vertical videos ≤ 3 min become Shorts automatically."
        accounts={of("youtube")}
        onChange={refresh}
      >
        <Step n={1} title="Create a Google OAuth client">
          <p className="text-sm text-zinc-400">
            In{" "}
            <a className="text-fuchsia-300 underline" href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer">
              Google Cloud Console
            </a>{" "}
            enable <b>YouTube Data API v3</b>, configure the OAuth consent screen (add yourself as a test user), then create an OAuth client of type{" "}
            <b>Web application</b> with this authorized redirect URI:
          </p>
          <Copyable text={setup.youtube.redirect_uri} />
          <div className="flex flex-wrap items-center gap-2">
            <input ref={ytFile} type="file" accept=".json,application/json" className="hidden" onChange={(e) => e.target.files?.[0] && uploadYtClient.mutate(e.target.files[0])} />
            <Button size="sm" onClick={() => ytFile.current?.click()} loading={uploadYtClient.isPending}>
              <FileJson className="size-3.5" /> {setup.youtube.client_configured ? "Replace client JSON" : "Upload client JSON"}
            </Button>
            {setup.youtube.client_configured && <Badge color="green">client saved ({setup.youtube.client_type})</Badge>}
          </div>
        </Step>
        <Step n={2} title="Sign in with the channel you want to post to">
          <Button variant="primary" size="sm" onClick={() => connectYt.mutate()} disabled={!setup.youtube.client_configured} loading={connectYt.isPending}>
            Connect YouTube channel
          </Button>
          <p className="text-xs text-zinc-500">
            Unverified Google projects can only upload <b>private</b> videos until they pass Google's API audit, and the default quota allows ~6 uploads/day.
          </p>
        </Step>
      </PlatformCard>

      <PlatformCard
        platform="tiktok"
        title="TikTok"
        subtitle="Official Content Posting API (Direct Post or send to your TikTok inbox as a draft)."
        accounts={of("tiktok")}
        onChange={refresh}
      >
        <Step n={1} title="Create a TikTok developer app">
          <p className="text-sm text-zinc-400">
            On{" "}
            <a className="text-fuchsia-300 underline" href="https://developers.tiktok.com/apps" target="_blank" rel="noreferrer">
              developers.tiktok.com
            </a>{" "}
            add <b>Login Kit</b> + <b>Content Posting API</b> (enable Direct Post), scopes <code>video.publish</code>, <code>video.upload</code>. TikTok only accepts an{" "}
            <b>HTTPS</b> redirect URI (no localhost) - use a tunnel to this app or any HTTPS page you control. Then enter the client key/secret and redirect URI in{" "}
            <Link to="/settings#publishing" className="text-fuchsia-300 underline">
              Settings → Publishing
            </Link>
            .
          </p>
          {setup.tiktok.client_configured ? <Badge color="green">client key saved</Badge> : <Badge color="yellow">client key not set</Badge>}
          {setup.tiktok.redirect_uri && (
            <div>
              <span className="mr-2 text-xs text-zinc-500">Redirect URI:</span>
              <Copyable text={setup.tiktok.redirect_uri} />
            </div>
          )}
        </Step>
        <Step n={2} title="Authorize your TikTok account">
          <Button variant="primary" size="sm" onClick={() => connectTt.mutate()} disabled={!setup.tiktok.client_configured} loading={connectTt.isPending}>
            Open TikTok authorization <ExternalLink className="size-3.5" />
          </Button>
          <p className="text-xs text-zinc-500">If the redirect doesn't land back on this app, paste the full URL you were redirected to:</p>
          <div className="flex gap-2">
            <TextInput value={ttPaste} onChange={(e) => setTtPaste(e.target.value)} placeholder="https://your-redirect/...?code=...&state=..." />
            <Button onClick={() => completeTt.mutate()} disabled={!ttPaste.trim()} loading={completeTt.isPending}>
              Finish
            </Button>
          </div>
          <Alert kind="warn">Until TikTok audits your app, API posts are private (SELF_ONLY) and the account must be private. Use the browser uploader below to post publicly.</Alert>
        </Step>
      </PlatformCard>

      <PlatformCard
        platform="tiktok_browser"
        title="TikTok (browser automation)"
        subtitle="Unofficial: drives a real browser with your cookies via the open-source tiktok-uploader. No API audit needed."
        accounts={of("tiktok_browser")}
        onChange={refresh}
      >
        {!setup.tiktok.browser_uploader_installed && (
          <Alert kind="info">
            Install the optional extra first: <code>uv pip install -e ".[tiktok-browser]"</code> then <code>playwright install chromium</code>.
          </Alert>
        )}
        <p className="text-sm text-zinc-400">
          Log into tiktok.com in your browser, export cookies with a "Get cookies.txt" extension (Netscape format), and upload the file. Automated posting can break when TikTok
          changes its site and may conflict with TikTok's terms - use at your own risk.
        </p>
        <div className="flex flex-wrap gap-2">
          <TextInput className="max-w-60" value={ttBrowserName} onChange={(e) => setTtBrowserName(e.target.value)} placeholder="Account label (e.g. @myhandle)" />
          <input ref={cookiesFile} type="file" accept=".txt" className="hidden" onChange={(e) => e.target.files?.[0] && addTtBrowser.mutate(e.target.files[0])} />
          <Button onClick={() => cookiesFile.current?.click()} loading={addTtBrowser.isPending}>
            <Upload className="size-3.5" /> Upload cookies.txt
          </Button>
        </div>
      </PlatformCard>

      <PlatformCard
        platform="instagram"
        title="Instagram Reels"
        subtitle="Official Instagram Graph API. Requires a Professional (Business/Creator) account."
        accounts={of("instagram")}
        onChange={refresh}
      >
        <p className="text-sm text-zinc-400">
          Create a Meta app at{" "}
          <a className="text-fuchsia-300 underline" href="https://developers.facebook.com/apps" target="_blank" rel="noreferrer">
            developers.facebook.com
          </a>{" "}
          and generate a long-lived access token for your account (you're an app admin, so no app review is needed for your own account).
        </p>
        <div className="grid gap-3 md:grid-cols-[220px_1fr]">
          <Field
            label="Token type"
            hint={igLogin === "facebook" ? "Uploads the file directly (resumable) - recommended." : "Instagram downloads clips from AUTOREEL_PUBLIC_BASE_URL."}
          >
            <Select
              value={igLogin}
              onChange={setIgLogin}
              options={[
                { value: "facebook", label: "Facebook Login (Page-linked)" },
                { value: "instagram", label: "Instagram Login" },
              ]}
            />
          </Field>
          <Field label="Access token">
            <TextArea value={igToken} onChange={(e) => setIgToken(e.target.value)} rows={2} placeholder="EAAG… or IGAA…" className="font-mono text-xs" />
          </Field>
        </div>
        {igLogin === "facebook" && (
          <Field label="Instagram user ID (optional)" hint="Leave empty to auto-detect the IG account linked to your Page.">
            <TextInput value={igUserId} onChange={(e) => setIgUserId(e.target.value)} className="max-w-xs" />
          </Field>
        )}
        {igLogin === "instagram" && !setup.instagram.public_base_url && (
          <Alert kind="warn">Instagram-login tokens need AUTOREEL_PUBLIC_BASE_URL (e.g. a Cloudflare tunnel) so Instagram can fetch the video.</Alert>
        )}
        <Button variant="primary" size="sm" onClick={() => addIg.mutate()} disabled={!igToken.trim()} loading={addIg.isPending}>
          Connect Instagram
        </Button>
      </PlatformCard>
    </div>
  );
}
