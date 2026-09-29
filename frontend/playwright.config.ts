import { defineConfig, devices } from '@playwright/test';

const configuredBaseURL = process.env.E2E_BASE_URL;
const resolvedBaseURL = configuredBaseURL || `http://127.0.0.1:${process.env.E2E_PORT || '4173'}`;
const parsedBaseURL = new URL(resolvedBaseURL);
const host = process.env.E2E_HOST || parsedBaseURL.hostname || '127.0.0.1';
const port = process.env.E2E_PORT || parsedBaseURL.port || (parsedBaseURL.protocol === 'https:' ? '443' : '80');
const baseURL = resolvedBaseURL;

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 120_000,
  workers: 1,
  expect: {
    timeout: 15_000,
  },
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  webServer: {
    command: `npm run dev -- --host ${host} --port ${port}`,
    url: `${baseURL}/admin/login`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // E2E_BROWSER_CHANNEL=chrome 时复用本机 Chrome，避免下载 playwright 自带浏览器。
        channel: process.env.E2E_BROWSER_CHANNEL || undefined,
      },
    },
  ],
});
