import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import path from "path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: [
      { find: "@", replacement: path.resolve(__dirname, "./src") },
      // Monaco 安装全局 AMD loader 后，Decimal 的 UMD 入口会跳过 CommonJS 导出。
      { find: /^decimal\.js-light$/, replacement: path.resolve(__dirname, "./node_modules/decimal.js-light/decimal.mjs") },
    ],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8001",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (id.includes("@monaco-editor/react") || id.includes("monaco-editor")) {
            return "vendor-monaco";
          }
          if (id.includes("lightweight-charts")) {
            return "vendor-lightweight-charts";
          }
          if (
            id.includes("recharts")
            || id.includes("d3-")
            || id.includes("victory-vendor")
          ) {
            return "vendor-recharts";
          }
          if (id.includes("motion") || id.includes("lucide-react")) {
            return "vendor-ui";
          }
          if (id.includes("react") || id.includes("scheduler")) {
            return "vendor-react";
          }
          return "vendor-misc";
        },
      },
    },
  },
});
