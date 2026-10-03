import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, type AccountsResponse, type AppConfig, type Clip } from "../api";
import { fmtDateTime, toLocalInput } from "../format";
import { PlatformIcon } from "./PlatformIcon";
import { Alert, Button, Field, Modal, NumberInput, Select, Spinner, TextInput, cx } from "./ui";

export interface PublishPayload {
  account_ids: string[];
  interval_minutes: number;
  start_at?: string;
  order: "score" | "chronological";
}

export function PublishDialog({
  open,
  onClose,
  clips,
  onSubmit,
  submitting,
}: {
  open: boolean;
  onClose: () => void;
  clips: Clip[];
  onSubmit: (p: PublishPayload) => void;
  submitting: boolean;
}) {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: () => api<AccountsResponse>("/api/accounts"), enabled: open });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api<AppConfig>("/api/settings"), enabled: open });
  const [selected, setSelected] = useState<string[]>([]);
  const [interval, setIntervalMin] = useState(3);
  const [order, setOrder] = useState<"score" | "chronological">("score");
  const [when, setWhen] = useState<"now" | "later">("now");
  const [startAt, setStartAt] = useState(toLocalInput(new Date(Date.now() + 3600_000)));

  useEffect(() => {
    if (open && accounts.data) setSelected(accounts.data.accounts.filter((a) => a.enabled).map((a) => a.id));
  }, [open, accounts.data]);
  useEffect(() => {
    if (open && settings.data) {
      setIntervalMin(settings.data.publishing.interval_minutes);
      setOrder(settings.data.publishing.order);
    }
  }, [open, settings.data]);

  const selectedPlatforms = [...new Set((accounts.data?.accounts ?? []).filter((a) => selected.includes(a.id)).map((a) => a.platform))];
  const alreadyQueued = (c: Clip) =>
    selectedPlatforms.length > 0 && selectedPlatforms.every((p) => ["scheduled", "running", "published"].includes(c.publish?.[p]?.status ?? ""));
  const ready = clips.filter((c) => c.approved && c.status === "ready");
  const eligible = ready.filter((c) => !alreadyQueued(c));
  const skippedQueued = ready.length - eligible.length;
  const ordered = useMemo(
    () => [...eligible].sort((a, b) => (order === "score" ? b.score - a.score || a.rank - b.rank : a.start - b.start)),
    [eligible, order],
  );
  const start = when === "now" ? new Date() : new Date(startAt);
  const schedule = ordered.map((c, i) => ({ clip: c, at: new Date(start.getTime() + i * interval * 60_000) }));
  const youtubeSelected = accounts.data?.accounts.some((a) => selected.includes(a.id) && a.platform === "youtube");

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Publish clips"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={submitting}
            disabled={!selected.length || !eligible.length}
            onClick={() =>
              onSubmit({
                account_ids: selected,
                interval_minutes: interval,
                order,
                ...(when === "later" ? { start_at: new Date(startAt).toISOString() } : {}),
              })
            }
          >
            Schedule {eligible.length} clip{eligible.length === 1 ? "" : "s"}
          </Button>
        </>
      }
    >
      {accounts.isLoading ? (
        <div className="flex justify-center py-6">
          <Spinner />
        </div>
      ) : !accounts.data?.accounts.length ? (
        <Alert kind="warn">
          No accounts connected yet. <Link to="/accounts" className="underline">Connect YouTube, TikTok or Instagram</Link> first.
        </Alert>
      ) : (
        <div className="space-y-5">
          <Field label="Post to">
            <div className="space-y-1.5">
              {accounts.data.accounts.map((a) => (
                <label
                  key={a.id}
                  className={cx(
                    "flex cursor-pointer items-center gap-2.5 rounded-lg border px-3 py-2 text-sm",
                    selected.includes(a.id) ? "border-fuchsia-600/60 bg-fuchsia-600/10" : "border-zinc-800",
                  )}
                >
                  <input
                    type="checkbox"
                    className="accent-fuchsia-600"
                    checked={selected.includes(a.id)}
                    onChange={(e) => setSelected((s) => (e.target.checked ? [...s, a.id] : s.filter((x) => x !== a.id)))}
                  />
                  <PlatformIcon platform={a.platform} />
                  <span className="text-zinc-100">{a.name}</span>
                  <span className="text-xs text-zinc-500">{accounts.data.platforms[a.platform]}</span>
                  {!a.enabled && <span className="ml-auto text-xs text-zinc-500">disabled</span>}
                </label>
              ))}
            </div>
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="Minutes between clips">
              <NumberInput value={interval} min={0} step={0.5} onChange={setIntervalMin} />
            </Field>
            <Field label="Order">
              <Select
                value={order}
                onChange={setOrder}
                options={[
                  { value: "score", label: "Best score first" },
                  { value: "chronological", label: "Chronological" },
                ]}
              />
            </Field>
          </div>
          <Field label="Start">
            <div className="flex gap-2">
              <Select
                value={when}
                onChange={setWhen}
                className="w-32"
                options={[
                  { value: "now", label: "Now" },
                  { value: "later", label: "At…" },
                ]}
              />
              {when === "later" && <TextInput type="datetime-local" value={startAt} onChange={(e) => setStartAt(e.target.value)} />}
            </div>
          </Field>
          {ready.length < clips.length && (
            <p className="text-xs text-zinc-500">{clips.length - ready.length} clip(s) skipped (not approved or not rendered).</p>
          )}
          {skippedQueued > 0 && (
            <p className="text-xs text-zinc-500">{skippedQueued} clip(s) already scheduled or published on the selected accounts - skipped.</p>
          )}
          {youtubeSelected && eligible.length > 6 && (
            <Alert kind="warn">
              YouTube's default API quota allows ~6 uploads per day (1,600 units each). Extra uploads will fail with a quota error - consider a longer
              interval or requesting more quota.
            </Alert>
          )}
          <div>
            <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-zinc-400">Schedule preview</p>
            <ol className="max-h-48 space-y-1 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950 p-2 text-sm">
              {schedule.map(({ clip, at }) => (
                <li key={clip.id} className="flex gap-3">
                  <span className="w-28 shrink-0 font-mono text-xs leading-5 text-fuchsia-300">{fmtDateTime(at.toISOString())}</span>
                  <span className="truncate text-zinc-300">{clip.title}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      )}
    </Modal>
  );
}
