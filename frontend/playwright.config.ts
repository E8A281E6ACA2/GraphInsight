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
    // CI 产物是公开可下载的：trace 会记录 addInitScript 参数与登录请求体（含 Bearer token），
    // HTML 报告还会把 trace/video 复制进 playwright-report/data，光删 test-results 下的原件挡不住。
    // 因此在 CI 里直接不生成，本地排障仍保留。截图保留（密码框是掩码输入，不含明文）。
    trace: process.env.CI ? 'off' : 'retain-on-failure',
    video: process.env.CI ? 'off' : 'retain-on-failure',
    screenshot: 'only-on-failure',
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
