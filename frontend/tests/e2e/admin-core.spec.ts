import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const adminEmail = process.env.E2E_ADMIN_EMAIL || process.env.ADMIN_EMAIL || 'yh@qs.al';
const adminPassword = process.env.E2E_ADMIN_PASSWORD || process.env.ADMIN_PASSWORD || '';
const adminToken = process.env.E2E_ADMIN_TOKEN || process.env.ADMIN_TOKEN || '';
const checkUiLogin = ['1', 'true', 'yes'].includes(
  (process.env.E2E_CHECK_UI_LOGIN || '').trim().toLowerCase()
);

function loginFromUI() {
  test.skip(!checkUiLogin, '默认跳过 UI 登录页验证；如需启用，请设置 E2E_CHECK_UI_LOGIN=1');
  test.skip(!adminPassword, 'UI 登录测试需要 ADMIN_PASSWORD / E2E_ADMIN_PASSWORD');
}

async function issueAdminToken(request: APIRequestContext): Promise<string> {
  const response = await request.post('/api/v1/admin/auth/login', {
    data: {
      username: adminEmail,
      password: adminPassword,
    },
  });
  expect(response.ok()).toBeTruthy();
  const payload = await response.json();
  return payload.data.token as string;
}

async function setPreferredHome(
  request: APIRequestContext,
  path: '/admin/dashboard' | '/workspace'
) {
  const token = await issueAdminToken(request);
  const response = await request.put('/api/v1/admin/profile', {
    headers: {
      Authorization: `Bearer ${token}`,
    },
    data: {
      preferred_home_path: path,
    },
  });
  expect(response.ok()).toBeTruthy();
}

async function authenticate(page: Page) {
  if (adminToken) {
    await page.addInitScript((token: string) => {
      window.localStorage.setItem('admin_token', token);
    }, adminToken);
    return;
  }

  test.skip(!adminPassword, '页面导航测试需要 ADMIN_TOKEN 或 ADMIN_PASSWORD');
  await page.goto('/admin/login');
  await page.getByLabel('邮箱').fill(adminEmail);
  await page.getByLabel('密码').fill(adminPassword);
  const loginResponsePromise = page.waitForResponse((response) =>
    response.url().includes('/api/v1/admin/auth/login')
  );
  await page.getByRole('button', { name: '登录控制台' }).click();
  const loginResponse = await loginResponsePromise;
  if (!loginResponse.ok()) {
    throw new Error(`admin login failed: status=${loginResponse.status()} body=${await loginResponse.text()}`);
  }
  await expect(page).toHaveURL(/\/admin\/dashboard$/);
}

async function installFullscreenLoadingObserver(page: Page) {
  await page.evaluate(() => {
    const marker = 'GraphInsight 正在加载';
    const globalWindow = window as Window & {
      __graphInsightFullscreenLoadingSeen?: boolean;
      __graphInsightLoadingObserver?: MutationObserver;
    };
    globalWindow.__graphInsightFullscreenLoadingSeen = false;
    const check = () => {
      if (document.body?.innerText.includes(marker)) {
        globalWindow.__graphInsightFullscreenLoadingSeen = true;
      }
    };
    check();
    globalWindow.__graphInsightLoadingObserver?.disconnect();
    globalWindow.__graphInsightLoadingObserver = new MutationObserver(check);
    globalWindow.__graphInsightLoadingObserver.observe(document.body, {
      childList: true,
      subtree: true,
      characterData: true,
    });
  });
}

async function expectAdminShellStable(page: Page) {
  await expect(page.getByText('GraphInsight 正在加载')).toBeHidden();
  // AdminLayout 侧栏品牌文案现为「Control Plane」（原「Enterprise Admin」已改版）。
  await expect(page.getByText('Control Plane')).toBeVisible();
  const seen = await page.evaluate(() => {
    const globalWindow = window as Window & { __graphInsightFullscreenLoadingSeen?: boolean };
    return Boolean(globalWindow.__graphInsightFullscreenLoadingSeen);
  });
  expect(seen).toBe(false);
}

test.describe('Admin Console Core Flow', () => {
  test.beforeEach(async ({ request }, testInfo) => {
    if (!adminPassword) {
      return;
    }
    void testInfo;
    await setPreferredHome(request, '/admin/dashboard');
  });

  test('anonymous user is redirected to login before entering console', async ({ page }) => {
    await page.goto('/');
    await expect(page).toHaveURL(/\/admin\/login$/);

    await page.goto('/workspace');
    await expect(page).toHaveURL(/\/admin\/login$/);
  });

  test('admin can login from UI', async ({ page }) => {
    await loginFromUI();
    await page.goto('/admin/login');
    await page.getByLabel('邮箱').fill(adminEmail);
    await page.getByLabel('密码').fill(adminPassword);
    const loginResponsePromise = page.waitForResponse((response) =>
      response.url().includes('/api/v1/admin/auth/login')
    );
    await page.getByRole('button', { name: '登录控制台' }).click();
    const loginResponse = await loginResponsePromise;
    if (!loginResponse.ok()) {
      throw new Error(`admin login failed: status=${loginResponse.status()} body=${await loginResponse.text()}`);
    }
    await expect(page).toHaveURL(/\/admin\/dashboard$/);
    await expect(page.getByRole('heading', { name: '运营总览' })).toBeVisible();
    await expect(page.getByText('系统健康状态')).toBeVisible();
  });

  test('invalid password stays on login page with an error message', async ({ page }) => {
    await loginFromUI();
    await page.goto('/admin/login');
    await page.getByLabel('邮箱').fill(adminEmail);
    await page.getByLabel('密码').fill('DefinitelyWrongPassword123!');
    const loginResponsePromise = page.waitForResponse((response) =>
      response.url().includes('/api/v1/admin/auth/login')
    );
    await page.getByRole('button', { name: '登录控制台' }).click();
    const loginResponse = await loginResponsePromise;
    expect(loginResponse.status()).toBeGreaterThanOrEqual(400);
    await expect(page).toHaveURL(/\/admin\/login$/);
    await expect(page.getByRole('alert')).toBeVisible();
  });

  test('admin preferred home redirects to workspace after re-login', async ({ page }) => {
    test.skip(!adminPassword, '默认首页偏好测试需要 ADMIN_PASSWORD / E2E_ADMIN_PASSWORD');

    await page.goto('/admin/login');
    await page.getByLabel('邮箱').fill(adminEmail);
    await page.getByLabel('密码').fill(adminPassword);
    await page.getByRole('button', { name: '登录控制台' }).click();
    await expect(page).toHaveURL(/\/admin\/dashboard$/);

    await page.getByRole('button', { name: '个人设置' }).click();
    await expect(page).toHaveURL(/\/admin\/profile$/);
    const preferredHomeGroup = page.getByRole('heading', { name: '进入偏好' }).locator('..').locator('..');
    await preferredHomeGroup.getByRole('button', { name: '图谱工作台' }).click();
    await page.getByRole('button', { name: '保存修改' }).click();
    await expect(page.getByText('保存成功')).toBeVisible();

    await page.getByRole('button', { name: '退出登录' }).click();
    await expect(page).toHaveURL(/\/admin\/login$/);

    await page.getByLabel('邮箱').fill(adminEmail);
    await page.getByLabel('密码').fill(adminPassword);
    await page.getByRole('button', { name: '登录控制台' }).click();
    await expect(page).toHaveURL(/\/workspace$/);

    await page.goto('/admin/profile');
    const resetPreferredHomeGroup = page.getByRole('heading', { name: '进入偏好' }).locator('..').locator('..');
    await resetPreferredHomeGroup.getByRole('button', { name: '系统仪表板' }).click();
    await page.getByRole('button', { name: '保存修改' }).click();
    await expect(page.getByText('保存成功')).toBeVisible();
  });

  test('authenticated admin can browse core admin pages', async ({ page }) => {
    await authenticate(page);

    await page.goto('/admin/dashboard');
    // DashboardPage 实际渲染标题为「运营总览」（AdminLayout title），与侧栏菜单名「仪表盘」不同。
    await expect(page.getByRole('heading', { name: '运营总览' })).toBeVisible();
    await installFullscreenLoadingObserver(page);
    await expectAdminShellStable(page);

    const sidebar = page.getByRole('list').first();

    await sidebar.getByRole('button', { name: '配置中心', exact: true }).click();
    await expectAdminShellStable(page);
    await expect(page).toHaveURL(/\/admin\/config$/);
    await expect(page.getByRole('heading', { name: '配置中心', exact: true })).toBeVisible();
    await expectAdminShellStable(page);
    await page.getByRole('tab', { name: 'AI / 模型 / 检索' }).click();
    await expect(page.getByRole('button', { name: /测试当前模型/ })).toBeVisible();

    await sidebar.getByRole('button', { name: '任务中心', exact: true }).click();
    await expectAdminShellStable(page);
    await expect(page).toHaveURL(/\/admin\/jobs$/);
    await expect(page.getByRole('heading', { name: '任务中心', exact: true })).toBeVisible();
    await expectAdminShellStable(page);
    await expect(page.getByRole('button', { name: '新建建图任务' })).toBeVisible();

    await sidebar.getByRole('button', { name: '问答追踪', exact: true }).click();
    await expectAdminShellStable(page);
    await expect(page).toHaveURL(/\/admin\/qa-traces$/);
    await expect(page.getByRole('heading', { name: '问答追踪', exact: true })).toBeVisible();
    await expectAdminShellStable(page);
    // M4-R1 审计 P1：问答追踪页按知识库 fail-closed 隔离，未选库不加载、不渲染表格。
    // 真实活栈必须先选择一个知识库，才能看到按 kb_id 作用域加载的结果表。
    const kbSelect = page.getByLabel('知识库');
    await expect(kbSelect).toBeVisible();
    await kbSelect.click();
    const firstKbOption = page
      .locator('li[role="option"]:not([aria-disabled="true"])')
      .first();
    await expect(firstKbOption).toBeVisible();
    await firstKbOption.click();
    await expect(page.getByRole('table')).toBeVisible();
  });
});
