import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: `npm run dev` serves on :5173 and proxies /api -> FastAPI on :8000.
// Build: `npm run build` -> dist/ (copied into backend/static by the Dockerfile).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": "http://localhost:8000" },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
