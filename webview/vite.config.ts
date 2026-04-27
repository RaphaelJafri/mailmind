import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Tauri expects a fixed dev server port and clearScreen off so its logs survive.
  clearScreen: false,
  server: {
    host: "127.0.0.1",
  },
  build: {
    outDir: "dist",
    target: "esnext",
  },
});
