import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Build output goes into ../static so FastAPI's StaticFiles can serve it directly
// when the user runs `checkpoint serve`. During local development, run
// `npm run dev` for the Vite dev server (port 5173) which proxies /api to the
// FastAPI backend on 4001.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  build: {
    outDir: path.resolve(__dirname, "..", "static"),
    emptyOutDir: true,
    sourcemap: false,
    target: "es2020",
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          query: ["@tanstack/react-query"],
        },
      },
    },
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": "http://127.0.0.1:4001",
      "/healthz": "http://127.0.0.1:4001",
      "/metrics": "http://127.0.0.1:4001",
    },
  },
});
