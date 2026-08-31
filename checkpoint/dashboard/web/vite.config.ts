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
        // Split the big vendors into their own chunks. Vite 8's rolldown
        // bundler types manualChunks as a function (the object-map form was
        // dropped), so we match on module id.
        manualChunks(id: string) {
          if (
            id.includes("node_modules/react-router-dom") ||
            id.includes("node_modules/react-dom") ||
            id.includes("node_modules/react/")
          ) {
            return "react";
          }
          if (id.includes("node_modules/@tanstack/react-query")) {
            return "query";
          }
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
