import type { Platform } from "../api";
import { cx } from "./ui";

// Simple brand-colored glyphs (no external icon fonts needed).
export function PlatformIcon({ platform, className }: { platform: Platform | string; className?: string }) {
  const base = cx("inline-flex size-5 shrink-0 items-center justify-center rounded-md text-[10px] font-bold text-white", className);
  if (platform === "youtube")
    return (
      <span className={cx(base, "bg-red-600")} title="YouTube Shorts">
        <svg viewBox="0 0 24 24" className="size-3 fill-white">
          <path d="M8 5v14l11-7z" />
        </svg>
      </span>
    );
  if (platform === "instagram")
    return (
      <span className={cx(base, "bg-gradient-to-tr from-amber-400 via-pink-500 to-purple-600")} title="Instagram Reels">
        <svg viewBox="0 0 24 24" className="size-3 fill-none stroke-white" strokeWidth={2.5}>
          <rect x="4" y="4" width="16" height="16" rx="5" />
          <circle cx="12" cy="12" r="3.5" />
        </svg>
      </span>
    );
  return (
    <span className={cx(base, "bg-zinc-950 ring-1 ring-zinc-600")} title={platform === "tiktok_browser" ? "TikTok (browser)" : "TikTok"}>
      <svg viewBox="0 0 24 24" className="size-3 fill-white">
        <path d="M16.5 3c.4 2.2 1.8 3.7 4 4v3c-1.5 0-2.9-.4-4-1.2V15a6 6 0 1 1-6-6v3.2a2.8 2.8 0 1 0 2.8 2.8V3h3.2z" />
      </svg>
    </span>
  );
}
