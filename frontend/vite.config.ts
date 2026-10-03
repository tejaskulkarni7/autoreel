import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// During development the API runs on :8000 (autoreel serve) and Vite proxies to it.
const backend = process.env.AUTOREEL_BACKEND ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/media": backend,
      "/public": backend,
    },
  },
});
