import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["tests/api/**/*.test.ts", "tests/unit/**/*.test.ts"],
    testTimeout: 30000,
    hookTimeout: 60000,
    globalSetup: ["tests/api/globalSetup.ts"],
    setupFiles: ["tests/api/setup.ts"],
    fileParallelism: false,
  },
});
