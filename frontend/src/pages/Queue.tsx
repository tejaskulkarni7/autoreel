import { useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, ExternalLink, Pause, Play, RotateCcw, Trash2, X, Zap } from "lucide-react";
import { api, type JobStatus, type PublishJob } from "../api";
import { PlatformIcon } from "../components/PlatformIcon";
import { Alert, Badge, Button, Card, CardHeader, EmptyState, Spinner, StatusBadge, cx, errMsg, useToast } from "../components/ui";
import { fmtDateTime, fmtRelative } from "../format";

const FILTERS: { key: string; label: string; statuses?: JobStatus[] }[] = [
  { key: "upcoming", label: "Upcoming", statuses: ["scheduled", "running"] },
  { key: "published", label: "Published", statuses: ["published"] },
  { key: "failed", label: "Failed", statuses: ["failed"] },
  { key: "all", label: "All" },
];

export default function QueuePage() {
  const toast = useToast();
  const qc = useQueryClient();
  const [filter, setFilter] = useState("upcoming");
  const statuses = FILTERS.find((f) => f.key === filter)?.statuses;
  const jobs = useQuery({
    queryKey: ["jobs", filter],
    queryFn: () => api<PublishJob[]>(`/api/publish/jobs${statuses ? `?status=${statuses.join(",")}` : ""}`),
    refetchInterval: 4000,
  });
  const state = useQuery({ queryKey: ["publish-state"], queryFn: () => api<{ paused: boolean }>("/api/publish/state") });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["publish-state"] });
  };
  const setPaused = useMutation({
    mutationFn: (paused: boolean) => api("/api/publish/state", { method: "POST", json: { paused } }),
    onSuccess: refresh,
    onError: (e) => toast("error", errMsg(e)),
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "cancel" | "retry" | "run-now" | "delete" }) =>
      action === "delete" ? api(`/api/publish/jobs/${id}`, { method: "DELETE" }) : api(`/api/publish/jobs/${id}/${action}`, { method: "POST" }),
    onSuccess: refresh,
    onError: (e) => toast("error", errMsg(e)),
  });
  const cancelBatch = useMutation({
    mutationFn: (batch: string) => api<{ canceled: number }>(`/api/publish/batches/${batch}/cancel`, { method: "POST" }),
    onSuccess: (r) => {
      toast("success", `Canceled ${r.canceled} post(s)`);
      refresh();
    },
  });

  const list = [...(jobs.data ?? [])].sort((a, b) =>
    filter === "upcoming" ? a.scheduled_at.localeCompare(b.scheduled_at) : b.scheduled_at.localeCompare(a.scheduled_at),
  );
  const batches = filter === "upcoming" ? [...new Set(list.filter((j) => j.status === "scheduled").map((j) => j.batch_id))] : [];
  const paused = state.data?.paused;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-white">Publishing queue</h1>
          <p className="text-sm text-zinc-400">Clips go out one slot at a time; each slot posts to every selected account.</p>
        </div>
        <Button variant={paused ? "primary" : "secondary"} onClick={() => setPaused.mutate(!paused)} loading={setPaused.isPending}>
          {paused ? <Play className="size-4" /> : <Pause className="size-4" />}
          {paused ? "Resume publishing" : "Pause publishing"}
        </Button>
      </div>
      {paused && <Alert kind="warn">Publishing is paused. When you resume, the remaining schedule shifts so posts keep their spacing.</Alert>}

      <Card>
        <CardHeader
          title={
            <div className="flex gap-1">
              {FILTERS.map((f) => (
                <button
                  key={f.key}
                  onClick={() => setFilter(f.key)}
                  className={cx("rounded-md px-3 py-1 text-sm", filter === f.key ? "bg-zinc-800 text-white" : "text-zinc-400 hover:text-white")}
                >
                  {f.label}
                </button>
              ))}
            </div>
          }
          actions={batches.map((b) => (
            <Button key={b} size="sm" variant="ghost" onClick={() => confirm("Cancel all remaining posts in this batch?") && cancelBatch.mutate(b)}>
              <X className="size-3.5" /> Cancel batch {b.slice(0, 4)}
            </Button>
          ))}
        />
        {jobs.isLoading ? (
          <div className="flex justify-center p-10">
            <Spinner />
          </div>
        ) : !list.length ? (
          <EmptyState icon={<CalendarClock className="size-6" />} title="Nothing here">
            Open a video and hit <b>Publish</b> to schedule its clips.
          </EmptyState>
        ) : (
          <div className="divide-y divide-zinc-800">
            {list.map((j) => (
              <div key={j.id} className="grid grid-cols-[auto_1fr] items-center gap-4 px-5 py-3 md:grid-cols-[150px_auto_1fr_auto]">
                <div className="hidden md:block">
                  <p className="text-sm text-zinc-200">{fmtDateTime(j.published_at ?? j.scheduled_at)}</p>
                  <p className="text-xs text-zinc-500">{fmtRelative(j.published_at ?? j.scheduled_at)}</p>
                </div>
                <div className="relative h-16 w-9 overflow-hidden rounded bg-zinc-800">
                  {j.clip?.thumb_url && <img src={j.clip.thumb_url} alt="" className="size-full object-cover" />}
                </div>
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <PlatformIcon platform={j.platform} />
                    <span className="text-sm text-zinc-400">{j.account?.name}</span>
                    <StatusBadge status={j.status} />
                    {j.test && <Badge color="blue">test</Badge>}
                    {j.attempts > 1 && <span className="text-xs text-zinc-500">attempt {j.attempts}</span>}
                  </div>
                  {j.clip ? (
                    <Link to={`/videos/${j.clip.video_id}`} className="mt-0.5 block truncate text-sm font-medium text-white hover:underline">
                      {j.clip.title}
                    </Link>
                  ) : (
                    <p className="text-sm text-zinc-500">Clip deleted</p>
                  )}
                  <p className="text-xs text-zinc-500 md:hidden">
                    {fmtDateTime(j.scheduled_at)} · {fmtRelative(j.scheduled_at)}
                  </p>
                  {j.error && <p className={cx("mt-0.5 line-clamp-2 text-xs", j.status === "published" ? "text-amber-400" : "text-red-400")} title={j.error}>{j.error}</p>}
                </div>
                <div className="col-span-2 flex justify-end gap-1 md:col-span-1">
                  {j.url && (
                    <a href={j.url} target="_blank" rel="noreferrer">
                      <Button size="sm" variant="secondary">
                        <ExternalLink className="size-3.5" /> View
                      </Button>
                    </a>
                  )}
                  {j.status === "scheduled" && (
                    <>
                      <Button size="sm" variant="ghost" onClick={() => act.mutate({ id: j.id, action: "run-now" })} title="Post now">
                        <Zap className="size-3.5" />
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => act.mutate({ id: j.id, action: "cancel" })} title="Cancel">
                        <X className="size-3.5" />
                      </Button>
                    </>
                  )}
                  {(j.status === "failed" || j.status === "canceled") && (
                    <Button size="sm" variant="ghost" onClick={() => act.mutate({ id: j.id, action: "retry" })} title="Retry now">
                      <RotateCcw className="size-3.5" />
                    </Button>
                  )}
                  {j.status !== "running" && j.status !== "scheduled" && (
                    <Button size="sm" variant="ghost" onClick={() => act.mutate({ id: j.id, action: "delete" })} title="Remove from list">
                      <Trash2 className="size-3.5" />
                    </Button>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
