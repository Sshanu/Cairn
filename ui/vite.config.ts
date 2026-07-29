import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Build straight into the Python package so `tt serve` can host it.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: { outDir: "../cairn/static", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});
