import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Local dev runs against the live Pi4 sidecar so the dashboard shows real
// data; override with PI4_NOC_API when targeting a different host.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    proxy: {
      "/api": {
        target: process.env.PI4_NOC_API || "http://192.168.0.101",
        changeOrigin: true,
      },
    },
  },
});
