import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: 'tests/web',
  timeout: 45000,
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: process.env.WEB_TEST_URL ?? 'http://127.0.0.1:8088',
    headless: true,
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE },
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
  },
  reporter: 'list',
});
