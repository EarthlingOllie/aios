/// <reference types="vitest/config" />
import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, proxy the backend routes so the SPA and API share an origin (and the session cookie).
const backend = process.env.BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  test: {
    include: ["src/**/*.test.ts"],
  },
  server: {
    proxy: {
      "/api": backend,
      "/login": backend,
      "/logout": backend,
      "/auth": backend,
    },
  },
});
