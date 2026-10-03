import { NavLink, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CalendarClock, Clapperboard, Cog, Link2, Loader2, Menu, PauseCircle } from "lucide-react";
import { useState } from "react";
import { api, type SystemStatus } from "./api";
import { cx } from "./components/ui";
import { STAGE_LABEL } from "./format";
import VideosPage from "./pages/Videos";
import VideoDetailPage from "./pages/VideoDetail";
import QueuePage from "./pages/Queue";
import AccountsPage from "./pages/Accounts";
import SettingsPage from "./pages/Settings";

const NAV = [
  { to: "/", label: "Videos", icon: Clapperboard, end: true },
  { to: "/queue", label: "Publishing", icon: CalendarClock },
  { to: "/accounts", label: "Accounts", icon: Link2 },
  { to: "/settings", label: "Settings", icon: Cog },
];

function WorkerStatus() {
  const { data } = useQuery({ queryKey: ["system"], queryFn: () => api<SystemStatus>("/api/system"), refetchInterval: 4000 });
  const paused = useQuery({ queryKey: ["publish-state"], queryFn: () => api<{ paused: boolean }>("/api/publish/state"), refetchInterval: 10000 });
  const task = data?.current_task;
  return (
    <div className="space-y-2 rounded-lg border border-zinc-800 bg-zinc-900/70 p-3 text-xs">
      <div className="flex items-center gap-2 text-zinc-300">
        {task ? <Loader2 className="size-3.5 animate-spin text-fuchsia-400" /> : <span className="size-2 rounded-full bg-emerald-500" />}
        <span className="font-medium">{task ? STAGE_LABEL[task.stage ?? ""] ?? "Working" : "Worker idle"}</span>
      </div>
      {task && (
        <p className="truncate text-zinc-500" title={task.message ?? ""}>
          {task.message} · {Math.round(task.progress * 100)}%
        </p>
      )}
      {!!data?.queued_tasks && <p className="text-zinc-500">{data.queued_tasks} task(s) waiting</p>}
      {data && !data.workers_running && <p className="text-red-400">Background workers are not running</p>}
      {paused.data?.paused && (
        <p className="flex items-center gap-1 text-amber-400">
          <PauseCircle className="size-3.5" /> Publishing paused
        </p>
      )}
    </div>
  );
}

export default function App() {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex min-h-screen">
      <aside
        className={cx(
          "fixed inset-y-0 left-0 z-40 flex w-60 flex-col gap-6 border-r border-zinc-800 bg-zinc-950 p-4 transition-transform md:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex items-center gap-2.5 px-2 pt-1">
          <img src="/favicon.svg" alt="" className="size-8" />
          <div>
            <p className="font-semibold leading-tight text-white">AutoReel</p>
            <p className="text-xs text-zinc-500">long video → shorts</p>
          </div>
        </div>
        <nav className="flex flex-col gap-1">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setOpen(false)}
              className={({ isActive }) =>
                cx(
                  "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors",
                  isActive ? "bg-zinc-800 text-white" : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100",
                )
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto">
          <WorkerStatus />
        </div>
      </aside>
      {open && <div className="fixed inset-0 z-30 bg-black/60 md:hidden" onClick={() => setOpen(false)} />}
      <div className="flex min-w-0 flex-1 flex-col md:pl-60">
        <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-zinc-800 bg-zinc-950/90 px-4 py-3 backdrop-blur md:hidden">
          <button onClick={() => setOpen(true)} className="rounded-md p-1.5 text-zinc-300 hover:bg-zinc-800" aria-label="Menu">
            <Menu className="size-5" />
          </button>
          <span className="font-semibold">AutoReel</span>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <Routes>
            <Route path="/" element={<VideosPage />} />
            <Route path="/videos/:id" element={<VideoDetailPage />} />
            <Route path="/queue" element={<QueuePage />} />
            <Route path="/accounts" element={<AccountsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<p className="text-zinc-400">Page not found.</p>} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
