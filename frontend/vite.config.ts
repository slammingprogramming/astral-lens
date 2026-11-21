import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API serves the built UI under /ui/ (backend/src/astral/api.py). In
// development `npm run dev` proxies the API paths to a locally running server.
const API = "http://127.0.0.1:8000";
const paths = ["/cases", "/plugins", "/sources", "/secrets", "/preflight", "/audit", "/health"];

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(paths.map((p) => [p, { target: API, changeOrigin: false }])),
  },
  build: { outDir: "dist", sourcemap: false },
});
