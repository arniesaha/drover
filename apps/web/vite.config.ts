import { defineConfig } from "vite";
import preact from "@preact/preset-vite";

export default defineConfig({
  base: "/ui/",
  plugins: [preact()],
  build: {
    emptyOutDir: true,
    outDir: "../../src/drover/server/web/static/client",
    sourcemap: false,
    target: "es2022"
  }
});
