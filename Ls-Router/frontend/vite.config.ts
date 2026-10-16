import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// envDir "..": read VITE_* values from the repo-root .env (other keys are not exposed to the browser).
// The /api proxy forwards to the FastAPI backend, so no CORS setup is needed in development.
export default defineConfig({
  plugins: [react()],
  envDir: "..",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
