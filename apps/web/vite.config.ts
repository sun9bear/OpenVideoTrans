/// <reference types="vitest/config" />
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vite";

// apps/web (T2.6) — minimal Svelte+Vite SPA for the Tier 1 anonymous upload/translate flow. The API
// base URL is injected at build/deploy via VITE_API_BASE (defaults to same-origin "" so a Worker
// serving the SPA + /api on one origin needs no config). Tests run under jsdom.
export default defineConfig({
  plugins: [svelte()],
  // Under Vitest, resolve Svelte's BROWSER (client) export so component mount() works in jsdom — the
  // default Node condition picks the server build, where mount() throws. Guarded to test runs only so
  // the production `vite build` is unaffected.
  resolve: process.env.VITEST ? { conditions: ["browser"] } : {},
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts"],
  },
});
