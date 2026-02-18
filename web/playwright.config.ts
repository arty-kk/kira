import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:3005'
  },
  webServer: {
    command: 'npm run dev -- --port 3005',
    port: 3005,
    reuseExistingServer: true,
    timeout: 120_000
  }
});
