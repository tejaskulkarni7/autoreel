import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ban, ChevronDown, Plus, Rocket, Scissors, Sparkles, Trash2, Wand2 } from "lucide-react";
import { api, type Clip, type Video } from "../api";
import { ClipCard } from "../components/ClipCard";
import { ClipEditor } from "../components/ClipEditor";
import { PublishDialog, type PublishPayload } from "../components/PublishDialog";
import { Alert, Button, Card, EmptyState, Field, Modal, NumberInput, ProgressBar, Spinner, StatusBadge, TextInput, errMsg, useToast } from "../components/ui";
import { STAGE_LABEL, fmtDuration } from "../format";

function AddClipModal({ open, onClose, video }: { open: boolean; onClose: () => void; video: Video }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(30);
  const [title, setTitle] = useState("");
  const add = useMutation({
    mutationFn: () => api<Clip>("/api/clips", { method: "POST", json: { video_id: video.id, start, end, title } }),
    onSuccess: () => {
      toast("success", "Clip added - rendering");
      qc.invalidateQueries({ queryKey: ["video", video.id] });
      onClose();
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Add a clip manually"
      footer={
        <Button variant="primary" onClick={() => add.mutate()} loading={add.isPending}>
          Add & render
        </Button>
      }
    >
      <div className="space-y-4">
        <Field label="Title">
          <TextInput value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Custom clip" />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Start (seconds)">
            <NumberInput value={start} min={0} step={0.5} onChange={setStart} />
          </Field>
          <Field label="End (seconds)">
            <NumberInput value={end} min={0} step={0.5} onChange={setEnd} />
          </Field>
        </div>
        <p className="text-xs text-zinc-500">Video length: {fmtDuration(video.duration)}</p>
      </div>
    </Modal>
  );
}

export default function VideoDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const qc = useQueryClient();
  const [editing, setEditing] = useState<Clip | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [titleDraft, setTitleDraft] = useState<string | null>(null);

  const { data: video, isLoading, error } = useQuery({
    queryKey: ["video", id],
    queryFn: () => api<Video>(`/api/videos/${id}`),
    refetchInterval: (q) => {
      const v = q.state.data;
      const busy = v && (["queued", "processing"].includes(v.status) || Object.keys(v.clip_tasks ?? {}).length > 0);
      return busy ? 2000 : 10000;
    },
  });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["video", id] });
    qc.invalidateQueries({ queryKey: ["videos"] });
  };

  const process = useMutation({
    mutationFn: (mode: "full" | "redetect" | "rerender") => api(`/api/videos/${id}/process`, { method: "POST", json: { mode } }),
    onSuccess: () => {
      toast("success", "Queued");
      invalidate();
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const cancel = useMutation({
    mutationFn: () => api(`/api/videos/${id}/cancel`, { method: "POST" }),
    onSuccess: invalidate,
    onError: (e) => toast("error", errMsg(e)),
  });
  const remove = useMutation({
    mutationFn: () => api(`/api/videos/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      toast("success", "Video deleted");
      qc.invalidateQueries({ queryKey: ["videos"] });
      navigate("/");
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const rename = useMutation({
    mutationFn: (title: string) => api(`/api/videos/${id}`, { method: "PATCH", json: { title } }),
    onSuccess: invalidate,
  });
  const patchClip = useMutation({
    mutationFn: ({ clipId, patch }: { clipId: string; patch: Partial<Clip> }) => api<Clip>(`/api/clips/${clipId}`, { method: "PATCH", json: patch }),
    onSuccess: () => {
      setEditing(null);
      invalidate();
    },
    onError: (e) => toast("error", errMsg(e)),
  });
  const clipAction = useMutation({
    mutationFn: ({ clipId, action }: { clipId: string; action: "render" | "delete" }) =>
      action === "render" ? api(`/api/clips/${clipId}/render`, { method: "POST" }) : api(`/api/clips/${clipId}`, { method: "DELETE" }),
    onSuccess: invalidate,
    onError: (e) => toast("error", errMsg(e)),
  });
  const publish = useMutation({
    mutationFn: (p: PublishPayload) =>
      api<{ jobs: number; clips: number; first_at: string; last_at: string }>("/api/publish", { method: "POST", json: { video_id: id, ...p } }),
    onSuccess: (r) => {
      toast("success", `Scheduled ${r.clips} clip(s) → ${r.jobs} post(s)`);
      setPublishOpen(false);
      invalidate();
      qc.invalidateQueries({ queryKey: ["jobs"] });
      navigate("/queue");
    },
    onError: (e) => toast("error", errMsg(e)),
  });

  if (isLoading)
    return (
      <div className="flex justify-center p-16">
        <Spinner />
      </div>
    );
  if (error || !video) return <Alert kind="error">{errMsg(error ?? "Not found")}</Alert>;

  const busy = video.status === "queued" || video.status === "processing";
  const clips = video.clips ?? [];
  const approvedReady = clips.filter((c) => c.approved && c.status === "ready").length;

  return (
    <div className="space-y-6">
      <div>
        <Link to="/" className="inline-flex items-center gap-1 text-sm text-zinc-400 hover:text-white">
          <ArrowLeft className="size-4" /> Videos
        </Link>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            {titleDraft === null ? (
              <h1 className="cursor-text truncate text-2xl font-semibold text-white" title="Click to rename" onClick={() => setTitleDraft(video.title)}>
                {video.title}
              </h1>
            ) : (
              <TextInput
                autoFocus
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={() => {
                  if (titleDraft.trim() && titleDraft !== video.title) rename.mutate(titleDraft);
                  setTitleDraft(null);
                }}
                onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
                className="text-lg"
              />
            )}
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-zinc-400">
              <StatusBadge status={video.status} />
              <span>{fmtDuration(video.duration)}</span>
              {video.width && <span>{video.width}×{video.height}</span>}
              {video.language && <span>language: {video.language}</span>}
              <span>{clips.length} clips</span>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {busy ? (
              <Button variant="secondary" onClick={() => cancel.mutate()} loading={cancel.isPending}>
                <Ban className="size-4" /> Cancel
              </Button>
            ) : (
              <div className="relative">
                <Button variant="secondary" onClick={() => setMenuOpen((o) => !o)} disabled={!video.source_url_media}>
                  <Wand2 className="size-4" /> Regenerate <ChevronDown className="size-3.5" />
                </Button>
                {menuOpen && (
                  <div className="absolute right-0 z-20 mt-1 w-72 rounded-lg border border-zinc-700 bg-zinc-900 p-1 shadow-xl" onMouseLeave={() => setMenuOpen(false)}>
                    {(
                      [
                        ["redetect", "Find new clips", "Re-run the AI on the existing transcript (uses current AI & clip settings)"],
                        ["rerender", "Re-render all clips", "Keep clips, apply current video & subtitle settings"],
                        ["full", "Start over", "Re-transcribe, re-detect and re-render everything"],
                      ] as const
                    ).map(([mode, label, hint]) => (
                      <button
                        key={mode}
                        onClick={() => {
                          setMenuOpen(false);
                          process.mutate(mode);
                        }}
                        className="block w-full rounded-md px-3 py-2 text-left hover:bg-zinc-800"
                      >
                        <span className="block text-sm text-zinc-100">{label}</span>
                        <span className="block text-xs text-zinc-500">{hint}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            )}
            <Button variant="ghost" onClick={() => confirm("Delete this video, its clips and their schedule?") && remove.mutate()} title="Delete video">
              <Trash2 className="size-4" />
            </Button>
            <Button variant="primary" onClick={() => setPublishOpen(true)} disabled={!approvedReady}>
              <Rocket className="size-4" /> Publish {approvedReady || ""} clip{approvedReady === 1 ? "" : "s"}
            </Button>
          </div>
        </div>
      </div>

      {busy && (
        <Card className="p-5">
          <div className="mb-2 flex items-center justify-between text-sm">
            <span className="font-medium text-zinc-100">
              {video.status === "queued" ? "Waiting in queue…" : STAGE_LABEL[video.stage ?? ""] ?? "Working…"}
            </span>
            <span className="text-zinc-400">{Math.round(video.progress * 100)}%</span>
          </div>
          <ProgressBar value={video.progress} />
          {video.task?.message && <p className="mt-2 text-xs text-zinc-500">{video.task.message}</p>}
          {video.stage === "transcribe" && (
            <p className="mt-2 text-xs text-zinc-500">First run downloads the Whisper model. On CPU, transcription takes roughly 0.2-1× the video length.</p>
          )}
        </Card>
      )}
      {video.status === "failed" && video.error && (
        <Alert kind="error">
          <p className="font-medium">Processing failed</p>
          <pre className="mt-1 whitespace-pre-wrap font-sans text-xs opacity-90">{video.error}</pre>
        </Alert>
      )}

      <div className="flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-lg font-semibold text-white">
          <Sparkles className="size-4 text-fuchsia-400" /> Clips
        </h2>
        <Button size="sm" variant="ghost" onClick={() => setAddOpen(true)} disabled={!video.source_url_media}>
          <Plus className="size-3.5" /> Add clip manually
        </Button>
      </div>

      {clips.length === 0 ? (
        <Card>
          <EmptyState icon={<Scissors className="size-6" />} title={busy ? "Clips will appear here" : "No clips yet"}>
            {busy ? "Sit tight - the AI is working through your video." : "Run “Regenerate → Find new clips” or add one manually."}
          </EmptyState>
        </Card>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {clips.map((c) => (
            <ClipCard
              key={c.id}
              clip={c}
              task={video.clip_tasks?.[c.id]}
              onEdit={() => setEditing(c)}
              onToggleApprove={() => patchClip.mutate({ clipId: c.id, patch: { approved: !c.approved } })}
              onRerender={() => clipAction.mutate({ clipId: c.id, action: "render" })}
              onDelete={() => confirm("Delete this clip?") && clipAction.mutate({ clipId: c.id, action: "delete" })}
            />
          ))}
        </div>
      )}

      <ClipEditor
        clip={editing}
        sourceUrl={video.source_url_media}
        open={!!editing}
        onClose={() => setEditing(null)}
        saving={patchClip.isPending}
        onSave={(patch) => editing && patchClip.mutate({ clipId: editing.id, patch })}
      />
      <PublishDialog open={publishOpen} onClose={() => setPublishOpen(false)} clips={clips} onSubmit={(p) => publish.mutate(p)} submitting={publish.isPending} />
      <AddClipModal open={addOpen} onClose={() => setAddOpen(false)} video={video} />
    </div>
  );
}
