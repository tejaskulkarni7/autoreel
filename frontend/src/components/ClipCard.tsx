import { AlertTriangle, CheckCircle2, ExternalLink, Pencil, RefreshCw, Trash2 } from "lucide-react";
import type { Clip, Platform, TaskStatus } from "../api";
import { fmtDateTime, fmtDuration, fmtTimestamp } from "../format";
import { PlatformIcon } from "./PlatformIcon";
import { Badge, Button, Spinner, cx } from "./ui";

const CATEGORY_COLOR: Record<string, "purple" | "yellow" | "blue" | "red" | "green" | "gray"> = {
  funny: "yellow",
  insightful: "blue",
  emotional: "purple",
  controversial: "red",
  story: "green",
  educational: "blue",
};

function scoreColor(score: number) {
  if (score >= 85) return "from-emerald-500 to-lime-400";
  if (score >= 70) return "from-fuchsia-500 to-violet-500";
  if (score >= 50) return "from-amber-500 to-orange-400";
  return "from-zinc-500 to-zinc-400";
}

export function ClipCard({
  clip,
  task,
  onEdit,
  onToggleApprove,
  onRerender,
  onDelete,
}: {
  clip: Clip;
  task?: TaskStatus;
  onEdit: () => void;
  onToggleApprove: () => void;
  onRerender: () => void;
  onDelete: () => void;
}) {
  const busy = task === "queued" || task === "running" || clip.status === "rendering" || clip.status === "pending";
  const publishEntries = Object.entries(clip.publish ?? {});
  return (
    <div className={cx("flex flex-col overflow-hidden rounded-xl border bg-zinc-900/60 transition-colors", clip.approved ? "border-zinc-800" : "border-zinc-800/60 opacity-60")}>
      <div className="relative aspect-[9/16] bg-black">
        {clip.video_url ? (
          <video key={clip.video_url} src={clip.video_url} poster={clip.thumb_url ?? undefined} controls preload="none" playsInline className="size-full object-contain" />
        ) : (
          <div className="flex size-full items-center justify-center text-sm text-zinc-500">{clip.status === "failed" ? "Render failed" : "Not rendered yet"}</div>
        )}
        {busy && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-black/70 text-sm text-zinc-200">
            <Spinner className="text-fuchsia-400" />
            {task === "queued" || clip.status === "pending" ? "Waiting to render…" : "Rendering…"}
          </div>
        )}
        <div className="pointer-events-none absolute top-2 left-2 flex items-center gap-1.5">
          <span className={cx("rounded-md bg-gradient-to-r px-1.5 py-0.5 text-xs font-bold text-white shadow", scoreColor(clip.score))} title="Virality score">
            {clip.score}
          </span>
          <span className="rounded-md bg-black/70 px-1.5 py-0.5 text-xs font-medium text-zinc-200">#{clip.rank}</span>
        </div>
        <span className="pointer-events-none absolute top-2 right-2 rounded-md bg-black/70 px-1.5 py-0.5 text-xs font-medium text-zinc-200">
          {fmtDuration(clip.duration)}
        </span>
      </div>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <div className="flex flex-wrap items-center gap-1.5">
          {clip.category && <Badge color={CATEGORY_COLOR[clip.category] ?? "gray"}>{clip.category}</Badge>}
          {clip.layout && <Badge>{clip.layout}</Badge>}
          <span className="text-xs text-zinc-500">
            {fmtTimestamp(clip.start)} → {fmtTimestamp(clip.end)}
          </span>
        </div>
        <p className="line-clamp-2 font-medium leading-snug text-white" title={clip.title}>
          {clip.title || "Untitled clip"}
        </p>
        {clip.reason && <p className="line-clamp-2 text-xs text-zinc-400" title={clip.reason}>{clip.reason}</p>}
        {clip.status === "failed" && clip.error && (
          <p className="line-clamp-3 flex gap-1 text-xs text-red-400" title={clip.error}>
            <AlertTriangle className="mt-0.5 size-3 shrink-0" /> {clip.error}
          </p>
        )}
        {publishEntries.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {publishEntries.map(([platform, p]) => (
              <a
                key={platform}
                href={p.url ?? undefined}
                target="_blank"
                rel="noreferrer"
                title={p.status === "scheduled" ? `Scheduled ${fmtDateTime(p.scheduled_at)}` : p.error ?? p.status}
                className={cx(
                  "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs",
                  p.status === "published" ? "border-emerald-800 text-emerald-300" : p.status === "failed" ? "border-red-800 text-red-300" : "border-zinc-700 text-zinc-300",
                )}
              >
                <PlatformIcon platform={platform as Platform} className="size-4" />
                {p.status}
                {p.url && <ExternalLink className="size-3" />}
              </a>
            ))}
          </div>
        )}
        <div className="mt-auto flex items-center gap-1 pt-1">
          <Button size="sm" variant={clip.approved ? "secondary" : "ghost"} onClick={onToggleApprove} title="Only approved clips get published">
            <CheckCircle2 className={cx("size-3.5", clip.approved && "text-emerald-400")} />
            {clip.approved ? "Approved" : "Skipped"}
          </Button>
          <div className="ml-auto flex gap-0.5">
            <Button size="sm" variant="ghost" onClick={onEdit} title="Edit title, caption, hook, timing">
              <Pencil className="size-3.5" />
            </Button>
            <Button size="sm" variant="ghost" onClick={onRerender} disabled={busy} title="Re-render with current settings">
              <RefreshCw className="size-3.5" />
            </Button>
            <Button size="sm" variant="ghost" onClick={onDelete} title="Delete clip">
              <Trash2 className="size-3.5" />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
