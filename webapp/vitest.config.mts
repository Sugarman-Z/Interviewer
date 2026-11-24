import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

export default defineConfig({
  // tsconfig 的 jsx: preserve 是给 Next 编译链的；测试侧用 esbuild 直接按
  // automatic 运行时转换（react/jsx-runtime），无需 @vitejs/plugin-react。
  esbuild: { jsx: "automatic" },
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
