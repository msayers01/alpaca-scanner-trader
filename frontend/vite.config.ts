import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the backend (127.0.0.1:8765) so the app always uses relative URLs.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: false },
      "/ws": { target: "ws://127.0.0.1:8765", ws: true, changeOrigin: false },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8765" },
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
  build: {
    target: "es2022",
    sourcemap: true,
    chunkSizeWarningLimit: 800,
  },
});
