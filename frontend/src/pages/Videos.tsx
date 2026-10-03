import { useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clapperboard, Film, Globe, UploadCloud } from "lucide-react";
import { api, uploadVideo, type Video } from "../api";
import { Alert, Button, Card, CardHeader, EmptyState, ProgressBar, Spinner, StatusBadge, TextInput, Toggle, cx, errMsg, useToast } from "../components/ui";
import { STAGE_LABEL, fmtBytes, fmtDuration, fmtRelative } from "../format";

function UploadCard() {
  const toast = useToast();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [title, setTitle] = useState("");
  const [autoProcess, setAutoProcess] = useState(true);
  const [progress, setProgress] = useState<number | null>(null);
  const [url, setUrl] = useState("");

  const doUpload = async (file: File) => {
    setProgress(0);
    try {
      const v = await uploadVideo(file, { title, autoProcess }, setProgress);
      toast("success", `Uploaded "${v.title}"${autoProcess ? " - processing started" : ""}`);
      setTitle("");
      qc.invalidateQueries({ queryKey: ["videos"] });
      navigate(`/videos/${v.id}`);
    } catch (e) {
      toast("error", errMsg(e));
    } finally {
      setProgress(null);
    }
  };

  const importUrl = useMutation({
    mutationFn: () => api<Video>("/api/videos/import", { method: "POST", json: { url, title, auto_process: autoProcess } }),
    onSuccess: (v) => {
      toast("success", "Download queued");
      setUrl("");
      setTitle("");
      qc.invalidateQueries({ queryKey: ["videos"] });
      navigate(`/videos/${v.id}`);
    },
    onError: (e) => toast("error", errMsg(e)),
  });

  return (
    <Card>
      <CardHeader title="Add a long-form video" subtitle="Podcasts, interviews, streams, talks - AutoReel finds the best moments and cuts vertical clips." />
      <div className="grid gap-5 p-5 lg:grid-cols-[1fr_320px]">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            const f = e.dataTransfer.files?.[0];
            if (f) doUpload(f);
          }}
          onClick={() => progress === null && inputRef.current?.click()}
          className={cx(
            "flex min-h-44 cursor-pointer flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-8 text-center transition-colors",
            drag ? "border-fuchsia-500 bg-fuchsia-500/5" : "border-zinc-700 hover:border-zinc-500 hover:bg-zinc-800/30",
          )}
        >
          <input
            ref={inputRef}
            type="file"
            accept="video/*,.mkv,.mov,.mp4,.webm"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) doUpload(f);
              e.target.value = "";
            }}
          />
          {progress === null ? (
            <>
              <UploadCloud className="mb-3 size-9 text-fuchsia-400" />
              <p className="font-medium text-zinc-100">Drop a video here or click to browse</p>
              <p className="mt-1 text-sm text-zinc-500">MP4, MOV, MKV, WebM… any length</p>
            </>
          ) : (
            <div className="w-full max-w-sm">
              <p className="mb-2 text-sm text-zinc-300">Uploading… {Math.round(progress * 100)}%</p>
              <ProgressBar value={progress} />
            </div>
          )}
        </div>
        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-400">Title (optional)</label>
            <TextInput value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Defaults to the file name" />
          </div>
          <Toggle checked={autoProcess} onChange={setAutoProcess} label="Generate clips right away" hint="Transcribe → find highlights → render" />
          <div className="border-t border-zinc-800 pt-4">
            <label className="mb-1 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-zinc-400">
              <Globe className="size-3.5" /> Or import from a URL
            </label>
            <div className="flex gap-2">
              <TextInput value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://youtube.com/watch?v=…" />
              <Button onClick={() => importUrl.mutate()} loading={importUrl.isPending} disabled={!url.trim()}>
                Import
              </Button>
            </div>
            <p className="mt-1 text-xs text-zinc-500">Downloaded with yt-dlp. Only use content you have rights to.</p>
          </div>
        </div>
      </div>
    </Card>
  );
}

function VideoRow({ v }: { v: Video }) {
  const busy = v.status === "processing" || v.status === "queued";
  return (
    <Link to={`/videos/${v.id}`} className="grid grid-cols-[auto_1fr] items-center gap-4 px-5 py-4 transition-colors hover:bg-zinc-800/40 sm:grid-cols-[auto_1fr_auto]">
      {v.thumb_url ? (
        <img src={v.thumb_url} alt="" className="h-16 w-9 rounded-md object-cover ring-1 ring-zinc-700" />
      ) : (
        <div className="flex h-16 w-9 items-center justify-center rounded-md bg-zinc-800 text-zinc-400">
          <Film className="size-4" />
        </div>
      )}
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <p className="truncate font-medium text-white">{v.title}</p>
          <StatusBadge status={v.status} />
        </div>
        <p className="mt-0.5 text-sm text-zinc-500">
          {fmtDuration(v.duration)} · {v.width && v.height ? `${v.width}×${v.height}` : "–"} · {fmtBytes(v.size_bytes)} · added {fmtRelative(v.created_at)}
        </p>
        {busy && (
          <div className="mt-2 max-w-md">
            <ProgressBar value={v.progress} />
            <p className="mt-1 text-xs text-zinc-400">
              {v.status === "queued" ? "Waiting in queue" : `${STAGE_LABEL[v.stage ?? ""] ?? "Working"}${v.task?.message ? ` - ${v.task.message}` : ""}`}
            </p>
          </div>
        )}
        {v.status === "failed" && v.error && <p className="mt-1 line-clamp-1 text-xs text-red-400">{v.error}</p>}
      </div>
      <div className="col-span-2 text-sm text-zinc-400 sm:col-span-1 sm:text-right">
        <span className="font-semibold text-zinc-100">{v.ready_clip_count}</span> clip{v.ready_clip_count === 1 ? "" : "s"} ready
      </div>
    </Link>
  );
}

export default function VideosPage() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["videos"],
    queryFn: () => api<Video[]>("/api/videos"),
    refetchInterval: (q) => (q.state.data?.some((v) => v.status === "processing" || v.status === "queued") ? 2500 : 15000),
  });
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-white">Videos</h1>
        <p className="text-sm text-zinc-400">Upload long-form content, review the AI-picked clips, then publish them on a schedule.</p>
      </div>
      <UploadCard />
      <Card>
        <CardHeader title="Library" subtitle={data ? `${data.length} video${data.length === 1 ? "" : "s"}` : undefined} />
        {isLoading ? (
          <div className="flex justify-center p-10">
            <Spinner />
          </div>
        ) : error ? (
          <div className="p-5">
            <Alert kind="error">{errMsg(error)}</Alert>
          </div>
        ) : !data?.length ? (
          <EmptyState icon={<Clapperboard className="size-6" />} title="No videos yet">
            Upload your first long-form video above.
          </EmptyState>
        ) : (
          <div className="divide-y divide-zinc-800">
            {data.map((v) => (
              <VideoRow key={v.id} v={v} />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
