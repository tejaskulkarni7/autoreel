import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Minus, Plus } from "lucide-react";
import { api, type Clip } from "../api";
import { fmtTimestamp } from "../format";
import { Alert, Button, Field, Modal, NumberInput, TextArea, TextInput } from "./ui";

interface Transcript {
  segments: { start: number; end: number; text: string }[];
}

function Nudge({ label, value, onChange, onSeek }: { label: string; value: number; onChange: (v: number) => void; onSeek: () => void }) {
  return (
    <Field label={label}>
      <div className="flex items-center gap-1">
        <Button size="sm" variant="ghost" onClick={() => onChange(Math.max(0, +(value - 0.5).toFixed(2)))} title="-0.5s">
          <Minus className="size-3.5" />
        </Button>
        <NumberInput value={value} step={0.1} min={0} onChange={onChange} className="text-center" />
        <Button size="sm" variant="ghost" onClick={() => onChange(+(value + 0.5).toFixed(2))} title="+0.5s">
          <Plus className="size-3.5" />
        </Button>
        <Button size="sm" variant="secondary" onClick={onSeek}>
          Preview
        </Button>
      </div>
    </Field>
  );
}

export function ClipEditor({
  clip,
  sourceUrl,
  open,
  onClose,
  onSave,
  saving,
}: {
  clip: Clip | null;
  sourceUrl: string | null;
  open: boolean;
  onClose: () => void;
  onSave: (patch: Partial<Clip>) => void;
  saving: boolean;
}) {
  const [title, setTitle] = useState("");
  const [hook, setHook] = useState("");
  const [caption, setCaption] = useState("");
  const [tags, setTags] = useState("");
  const [start, setStart] = useState(0);
  const [end, setEnd] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);

  useEffect(() => {
    if (!clip) return;
    setTitle(clip.title);
    setHook(clip.hook);
    setCaption(clip.caption);
    setTags(clip.hashtags.map((t) => `#${t}`).join(" "));
    setStart(clip.start);
    setEnd(clip.end);
  }, [clip]);

  const transcript = useQuery({
    queryKey: ["transcript", clip?.video_id],
    queryFn: () => api<Transcript>(`/api/videos/${clip!.video_id}/transcript`),
    enabled: open && !!clip,
    staleTime: 60_000,
  });

  const excerpt = useMemo(
    () => (transcript.data?.segments ?? []).filter((s) => s.end > start - 15 && s.start < end + 15),
    [transcript.data, start, end],
  );

  if (!clip) return null;
  const timingChanged = Math.abs(start - clip.start) > 0.001 || Math.abs(end - clip.end) > 0.001;
  const hookChanged = hook.trim() !== clip.hook;
  const seek = (t: number) => {
    const v = videoRef.current;
    if (v) {
      v.currentTime = t;
      v.play().catch(() => {});
    }
  };
  const save = () =>
    onSave({
      title,
      hook,
      caption,
      hashtags: tags.split(/[\s,]+/).map((t) => t.replace(/^#/, "")).filter(Boolean),
      ...(timingChanged ? { start, end } : {}),
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={`Edit clip #${clip.rank}`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={save} loading={saving}>
            {timingChanged || hookChanged ? "Save & re-render" : "Save"}
          </Button>
        </>
      }
    >
      <div className="grid gap-5 md:grid-cols-[1fr_280px]">
        <div className="space-y-4">
          <Field label="Title" hint={`${title.length}/100 - used as the YouTube title`}>
            <TextInput value={title} maxLength={100} onChange={(e) => setTitle(e.target.value)} />
          </Field>
          <Field label="On-screen hook" hint="Shown at the top for the first seconds. Changing it re-renders the clip.">
            <TextInput value={hook} maxLength={80} onChange={(e) => setHook(e.target.value)} />
          </Field>
          <Field label="Caption" hint="Posted as the description/caption (hashtags are appended per the template in Settings).">
            <TextArea value={caption} onChange={(e) => setCaption(e.target.value)} rows={3} />
          </Field>
          <Field label="Hashtags">
            <TextInput value={tags} onChange={(e) => setTags(e.target.value)} placeholder="#podcast #startup" />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Nudge label="Start (s)" value={start} onChange={setStart} onSeek={() => seek(start)} />
            <Nudge label="End (s)" value={end} onChange={setEnd} onSeek={() => seek(Math.max(0, end - 3))} />
          </div>
          <p className="text-xs text-zinc-500">
            Length: {(end - start).toFixed(1)}s {end - start > 180 && <span className="text-red-400">(max 180s)</span>}
          </p>
          {timingChanged && <Alert kind="info">Timing changed - the clip will be re-rendered with fresh subtitles.</Alert>}
        </div>
        <div className="space-y-3">
          {sourceUrl && <video ref={videoRef} src={sourceUrl} controls preload="metadata" className="aspect-video w-full rounded-lg bg-black" />}
          <div className="max-h-72 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950 p-3 text-xs leading-relaxed">
            {transcript.isLoading && <p className="text-zinc-500">Loading transcript…</p>}
            {excerpt.map((s) => {
              const inside = s.end > start && s.start < end;
              return (
                <button
                  key={s.start}
                  className={`block w-full rounded px-1 py-0.5 text-left hover:bg-zinc-800 ${inside ? "text-zinc-100" : "text-zinc-500"}`}
                  onClick={() => seek(s.start)}
                  title="Click to preview · shift-click to set start · alt-click to set end"
                  onMouseDown={(e) => {
                    if (e.shiftKey) setStart(+s.start.toFixed(2));
                    if (e.altKey) setEnd(+s.end.toFixed(2));
                  }}
                >
                  <span className="mr-1 font-mono text-fuchsia-400/80">{fmtTimestamp(s.start)}</span>
                  {s.text}
                </button>
              );
            })}
          </div>
          <p className="text-[11px] text-zinc-500">Tip: shift-click a line to set the start, alt-click to set the end.</p>
        </div>
      </div>
    </Modal>
  );
}
