import { test as baseTest, expect, type Page } from '@playwright/test';

const test = baseTest.extend<{ browserAudit: void }>({
  browserAudit: [async ({ page }, use, testInfo) => {
    const errors: string[] = []; const fatal: string[] = []; const expectedStatuses = new Set<number>();
    page.on('response', (response) => { if (response.status() >= 400 && response.url().includes('/api/')) expectedStatuses.add(response.status()); });
    page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
    page.on('pageerror', (error) => fatal.push(error.message));
    await use();
    await testInfo.attach('console-errors', { body: JSON.stringify({ expectedStatuses: [...expectedStatuses], errors, pageerrors: fatal }), contentType: 'application/json' });
    expect(fatal, 'Uncaught pageerror is always fatal').toEqual([]);
    expect(errors.filter((text) => ![...expectedStatuses].some((status) => text.includes(String(status))) && !(expectedStatuses.size > 0 && /API 错误处理:|Axios 错误:|标准化业务错误:/.test(text))), 'Unexpected console error').toEqual([]);
  }, { auto: true }],
});

const root = '/api/v1/admin/knowledge-bases';
const envelope = (data: unknown) => ({ code: 200, message: 'ok', data, timestamp: '2026-10-09', trace_id: 'fixture-trace' });
const TENANT = 'tenant-test';
const PROJECT = 'project-test';
const scopeKey = (tenant: string, project: string) => `${tenant}/${project}`;
type Row = { id: string; name: string; status: string; tenant_id: string; project_id: string };
const initialRows = new Map<string, Row[]>([
  [scopeKey(TENANT, PROJECT), [{ id: 'kb-a', name: '测试知识库 A', status: 'archived', tenant_id: TENANT, project_id: PROJECT }]],
  [scopeKey('tenant-other', 'project-other'), [{ id: 'kb-b', name: '测试知识库 B', status: 'active', tenant_id: 'tenant-other', project_id: 'project-other' }]],
]);

async function install(page: Page) {
  const unexpected: string[] = [];
  const writes: Array<{ method: string; body: Record<string, unknown> }> = [];
  const queries: Array<{ tenant: string | null; project: string | null; status: string | null }> = [];
  const pendingWrites: Array<{ release: (fail?: boolean) => void }> = [];
  const controls = { holdWrites: false, forbidden: false, failWrites: false, holdLists: false };
  const pendingLists: Array<{ page: number; release: () => void }> = [];
  const rowsByScope = new Map([...initialRows].map(([key, rows]) => [key, rows.map((row) => ({ ...row }))]));
  const findRow = (id: string) => [...rowsByScope.values()].flat().find((row) => row.id === id);
  await page.addInitScript(() => localStorage.setItem('admin_token', 'synthetic-token'));
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' });
    if (url.pathname === '/api/v1/admin/auth/me') return route.fulfill({ json: envelope({ id: 1, username: 'synthetic', is_active: true }) });
    if (url.pathname === root && request.method() === 'GET') {
      const status = url.searchParams.get('status'); const tenant = url.searchParams.get('tenant_id'); const project = url.searchParams.get('project_id');
      queries.push({ tenant, project, status });
      const headers = request.headers();
      const conflict = (headers['x-tenant-id'] && headers['x-tenant-id'] !== tenant) || (headers['x-project-id'] && headers['x-project-id'] !== project);
      if (!tenant || !project || conflict) return route.fulfill({ status: 400, json: { ...envelope(null), code: 400, message: conflict ? 'KB_CROSS_SCOPE' : 'KB_SCOPE_REQUIRED' } });
      if (controls.forbidden) return route.fulfill({ status: 403, json: { ...envelope(null), code: 403, message: 'KB_ACCESS_DENIED' } });
      const rows = rowsByScope.get(scopeKey(tenant, project)) ?? [];
      const filtered = rows.filter((row) => !status || row.status === status);
      const requestedPage = Number(url.searchParams.get('page') ?? 1);
      const data = { items: filtered.slice((requestedPage - 1) * 20, requestedPage * 20), total: filtered.length, page: requestedPage, page_size: 20, total_pages: Math.ceil(filtered.length / 20) };
      if (controls.holdLists) await new Promise<void>((resolve) => pendingLists.push({ page: requestedPage, release: resolve }));
      return route.fulfill({ json: envelope(data) });
    }
    if (url.pathname === root && request.method() === 'POST') {
      const body = request.postDataJSON();
      if (!body.tenant_id || !body.project_id || (request.headers()['x-tenant-id'] && request.headers()['x-tenant-id'] !== body.tenant_id) || (request.headers()['x-project-id'] && request.headers()['x-project-id'] !== body.project_id)) return route.fulfill({ status: 400, json: { ...envelope(null), code: 400, message: 'KB_SCOPE_REQUIRED_OR_CONFLICT' } });
      writes.push({ method: 'POST', body });
      if (controls.holdWrites) { const failed = await new Promise<boolean>((resolve) => pendingWrites.push({ release: (fail = false) => resolve(fail) })); if (failed || controls.failWrites) return route.fulfill({ status: 403, json: { ...envelope(null), code: 403, message: 'stale operation denied' } }); }
      if (controls.failWrites) return route.fulfill({ status: 500, json: { ...envelope(null), code: 500, message: 'write failed trace-write' } });
      const key = scopeKey(body.tenant_id, body.project_id); const row = { id: `kb-${Date.now()}`, name: body.name, status: 'active', tenant_id: body.tenant_id, project_id: body.project_id } as Row;
      rowsByScope.set(key, [...(rowsByScope.get(key) ?? []), row]);
      return route.fulfill({ status: 201, json: envelope(row) });
    }
    const idMatch = url.pathname.match(new RegExp(`^${root}/([^/]+)(/archive)?$`));
    if (idMatch && (request.method() === 'PATCH' || request.method() === 'POST')) {
      const id = decodeURIComponent(idMatch[1]); const row = findRow(id); const isArchive = Boolean(idMatch[2]);
      if (!row) return route.fulfill({ status: 404, json: { ...envelope(null), code: 404, message: 'KB_NOT_FOUND' } });
      const body = isArchive ? {} : request.postDataJSON(); if (!isArchive && !body.status && !body.name) return route.fulfill({ status: 400, json: { ...envelope(null), code: 400, message: 'invalid update' } });
      if (row.status === 'deleting') return route.fulfill({ status: 409, json: { ...envelope({ error_code: 'KB_INVALID_STATE' }), code: 409, message: '知识库当前状态不支持该操作' } });
      if (!isArchive && row.status === 'archived' && body.status !== 'active') return route.fulfill({ status: 409, json: { ...envelope({ error_code: 'KB_ARCHIVED' }), code: 409, message: '知识库已归档，禁止写入' } });
      writes.push({ method: isArchive ? 'ARCHIVE' : 'PATCH', body });
      if (controls.holdWrites) { const failed = await new Promise<boolean>((resolve) => pendingWrites.push({ release: (fail = false) => resolve(fail) })); if (failed || controls.failWrites) return route.fulfill({ status: 403, json: { ...envelope(null), code: 403, message: 'stale operation denied' } }); }
      if (controls.failWrites) return route.fulfill({ status: 500, json: { ...envelope(null), code: 500, message: 'write failed trace-write' } });
      Object.assign(row, isArchive ? { status: 'archived' } : body);
      return route.fulfill({ json: envelope(row) });
    }
    if (url.pathname.startsWith('/api/') || (url.origin !== 'http://127.0.0.1:1234')) {
      unexpected.push(`${request.method()} ${url.pathname}`); return route.abort();
    }
    return route.continue();
  });
  return { writes, queries, unexpected, controls, pendingWrites, pendingLists, rowsByScope };
}

async function applyScope(page: Page, tenant = TENANT, project = PROJECT) {
  await page.getByLabel('Tenant ID', { exact: true }).fill(tenant);
  await page.getByLabel('Project ID', { exact: true }).fill(project);
  await page.getByRole('button', { name: '应用作用域', exact: true }).click();
}

test('创建按钮打开表单并调用真实create客户端POST', async ({ page }) => {
  const fixture = await install(page);
  await page.goto('/admin/knowledge-bases');
  await applyScope(page);
  await page.getByRole('button', { name: '创建知识库', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await dialog.getByLabel('名称', { exact: false }).fill('新建知识库');
  await dialog.getByLabel('描述').fill('确定性创建');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('新建知识库', { exact: true })).toBeVisible();
  expect(fixture.writes).toEqual([{ method: 'POST', body: { tenant_id: TENANT, project_id: PROJECT, name: '新建知识库', description: '确定性创建' } }]);
  expect(fixture.unexpected).toEqual([]);
});

test('归档行恢复调用PATCH active并回到启用列表', async ({ page }) => {
  const fixture = await install(page);
  await page.goto('/admin/knowledge-bases');
  await applyScope(page);
  await page.getByLabel('状态', { exact: true }).selectOption('archived');
  await page.getByRole('button', { name: '恢复', exact: true }).click();
  await expect(page.getByText('暂无匹配知识库')).toBeVisible();
  await page.getByLabel('状态', { exact: true }).selectOption('active');
  await expect(page.getByText('测试知识库 A', { exact: true })).toBeVisible();
  expect(fixture.writes).toEqual([{ method: 'PATCH', body: { status: 'active' } }]);
  expect(fixture.unexpected).toEqual([]);
  expect(fixture.queries[0]).toMatchObject({ tenant: TENANT, project: PROJECT });
});

test('缺少父作用域本地门控且403可见', async ({ page }) => {
  const fixture = await install(page);
  await page.goto('/admin/knowledge-bases');
  await expect(page.getByText('请选择管理父作用域')).toBeVisible();
  await expect(page.getByRole('button', { name: '创建知识库', exact: true })).toBeDisabled();
  expect(fixture.queries).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('管理403显示错误', async ({ page }) => {
  const fixture = await install(page);
  await page.goto('/admin/knowledge-bases');
  fixture.controls.forbidden = true;
  await applyScope(page);
  await expect(page.getByText(/知识库目录加载失败|KB_ACCESS_DENIED/)).toBeVisible();
  expect(fixture.unexpected).toEqual([]);
});

test('父作用域切换通过真实GET参数传输', async ({ page }) => {
  const fixture = await install(page);
  await page.goto('/admin/knowledge-bases');
  await applyScope(page);
  await expect.poll(() => fixture.queries.length).toBeGreaterThan(0);
  await applyScope(page, 'tenant-other', 'project-other');
  await expect.poll(() => fixture.queries.at(-1)).toMatchObject({ tenant: 'tenant-other', project: 'project-other', status: 'active' });
  expect(fixture.unexpected).toEqual([]);
});

test('fixture合同拒绝缺失与不一致的实际scope', async ({ page }) => {
  const fixture = await install(page);
  await page.goto('/admin/knowledge-bases');
  const results = await page.evaluate(async (root) => {
    const missing = await fetch(root);
    const conflict = await fetch(`${root}?tenant_id=tenant-test&project_id=project-test`, { headers: { 'X-Tenant-ID': 'tenant-other' } });
    const create = await fetch(root, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: '缺scope' }) });
    return [missing.status, conflict.status, create.status];
  }, root);
  expect(results).toEqual([400, 400, 400]);
  expect(fixture.writes).toEqual([]);
  expect(fixture.unexpected).toEqual([]);
});

test('恢复迟到：archived切active后仅刷新当前active筛选', async ({ page }) => {
  const fixture = await install(page);
  await page.goto('/admin/knowledge-bases');
  await applyScope(page);
  await page.getByLabel('状态', { exact: true }).selectOption('archived');
  fixture.controls.holdWrites = true;
  await page.getByRole('button', { name: '恢复', exact: true }).click();
  await expect.poll(() => fixture.pendingWrites.length).toBe(1);
  await page.getByLabel('状态', { exact: true }).selectOption('active');
  await expect(page.getByText('暂无匹配知识库')).toBeVisible();
  const boundary = fixture.queries.length;
  const done = page.waitForResponse((response) => response.request().method() === 'PATCH');
  fixture.pendingWrites[0].release();
  await done;
  await expect(page.getByText('测试知识库 A', { exact: true })).toBeVisible();
  await expect(page.getByLabel('状态', { exact: true })).toHaveValue('active');
  expect(fixture.queries.slice(boundary).every((query) => query.status === 'active' && query.tenant === TENANT && query.project === PROJECT)).toBe(true);
  expect(fixture.queries.length).toBeGreaterThan(boundary);
  expect(fixture.unexpected).toEqual([]);
});

async function controlledLists(page: Page) {
  await page.evaluate(async () => {
    const module = await import('/src/services/adminService.ts');
    const pending: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> = [];
    Object.assign(window, { directoryRequests: pending });
    module.knowledgeBasesApi.list = () => new Promise((resolve, reject) => pending.push({ resolve, reject }));
  });
}
async function release(page: Page, index: number, name: string, failure = false) {
  await page.evaluate(({ index, name, failure }) => {
    const requests = (window as unknown as { directoryRequests: Array<{ resolve: (value: unknown) => void; reject: (error: unknown) => void }> }).directoryRequests;
    if (failure) requests[index].reject(new Error(name));
    else requests[index].resolve({ items: [{ id: name, name, status: 'active', tenant_id: 'tenant-test', project_id: 'project-test' }], total: 1, page: 1, page_size: 200, total_pages: 1 });
  }, { index, name, failure });
}
async function waitRequests(page: Page, count: number) {
  await expect.poll(() => page.evaluate(() => (window as unknown as { directoryRequests: unknown[] }).directoryRequests.length)).toBe(count);
}

for (const operation of ['restore', 'archive', 'save', 'save-error']) {
  test(`HTTP写操作迟到服从当前父作用域和筛选：${operation}`, async ({ page }) => {
    const fixture = await install(page);
    await page.goto('/admin/knowledge-bases');
    await applyScope(page);
    fixture.controls.holdWrites = true;
    if (operation === 'restore' || operation === 'archive') {
      await page.getByLabel('状态', { exact: true }).selectOption('archived');
      await expect(page.getByRole('button', { name: '恢复', exact: true })).toBeVisible();
      if (operation === 'archive') {
        fixture.controls.holdWrites = false;
        await page.getByRole('button', { name: '恢复', exact: true }).click();
        await expect(page.getByText('暂无匹配知识库')).toBeVisible();
        await page.getByLabel('状态', { exact: true }).selectOption('active');
        fixture.controls.holdWrites = true;
        page.once('dialog', (dialog) => dialog.accept());
        await page.getByRole('button', { name: '归档', exact: true }).click();
      } else await page.getByRole('button', { name: '恢复', exact: true }).click();
    } else {
      await page.getByRole('button', { name: '创建知识库', exact: true }).click();
      await page.getByRole('dialog').getByLabel('名称', { exact: false }).fill('旧作用域创建');
      await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click();
    }
    await expect.poll(() => fixture.pendingWrites.length).toBe(1);
    await page.getByLabel('状态', { exact: true }).selectOption(operation === 'archive' ? 'archived' : 'active');
    await applyScope(page, 'tenant-next', 'project-next');
    await expect.poll(() => fixture.queries.some((q) => q.tenant === 'tenant-next')).toBe(true);
    const beforeCompletion = fixture.queries.length;
    const responseDone = page.waitForResponse((response) => response.request().method() !== 'GET' && response.url().includes(root));
    fixture.pendingWrites[0].release(operation === 'save-error');
    await responseDone;
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    if (operation !== 'save-error') {
      await expect.poll(() => fixture.queries.length).toBeGreaterThan(beforeCompletion);
      for (const request of fixture.queries.slice(beforeCompletion)) expect(request).toEqual({ tenant: 'tenant-next', project: 'project-next', status: operation === 'archive' ? 'archived' : 'active' });
    }
    await expect(page.getByLabel('状态', { exact: true })).toHaveValue(operation === 'archive' ? 'archived' : 'active');
    await expect(page.getByText(/stale operation denied/)).toHaveCount(0);
    expect(fixture.queries.every((q) => q.tenant && q.project)).toBe(true);
    expect(fixture.writes.at(-1)?.body).toMatchObject(operation.startsWith('save') ? { tenant_id: TENANT, project_id: PROJECT } : operation === 'restore' ? { status: 'active' } : {});
    expect(fixture.unexpected).toEqual([]);
  });
}

for (const scenario of ['active-archived', 'refresh-refresh', 'old-error', 'old-finally']) {
  test(`目录sequence组件验证：${scenario}`, async ({ page }) => {
    const fixture = await install(page);
    await page.goto('/admin/knowledge-bases');
  await applyScope(page);
    await expect(page.getByRole('button', { name: '刷新', exact: true })).toBeVisible();
    await controlledLists(page);
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await waitRequests(page, 1);
    if (scenario === 'active-archived') await page.getByLabel('状态', { exact: true }).selectOption('archived');
    else await page.getByRole('button', { name: '刷新', exact: true }).click();
    await waitRequests(page, 2);
    if (scenario === 'old-finally') {
      await release(page, 0, '旧错误', true);
      await expect(page.getByText('加载知识库目录...', { exact: true })).toBeVisible();
      await expect(page.getByText('旧错误', { exact: true })).toHaveCount(0);
      await release(page, 1, '新目录');
    } else {
      await release(page, 1, '新目录');
      await expect(page.getByText('新目录', { exact: true })).toBeVisible();
      await release(page, 0, '旧目录', scenario === 'old-error');
    }
    await expect(page.getByText('新目录', { exact: true })).toBeVisible();
    await expect(page.getByText('旧目录', { exact: true })).toHaveCount(0);
    expect(fixture.unexpected).toEqual([]);
  });
}

test('目录两页互斥、搜索仅当前页、状态筛选重置首页', async ({ page }) => {
  const f = await install(page);
  f.rowsByScope.set(scopeKey(TENANT, PROJECT), Array.from({ length: 21 }, (_, i) => ({ id: `page-kb-${i}`, name: `分页知识库-${i}`, status: 'active', tenant_id: TENANT, project_id: PROJECT })));
  await page.goto('/admin/knowledge-bases'); await applyScope(page);
  await expect(page.getByText('分页知识库-0', { exact: true })).toBeVisible();
  await expect(page.getByText('分页知识库-20', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect(page.getByText('分页知识库-20', { exact: true })).toBeVisible();
  await expect(page.getByText('分页知识库-0', { exact: true })).toHaveCount(0);
  await page.getByLabel('搜索当前页', { exact: true }).fill('分页知识库-0');
  await expect(page.getByText('暂无匹配知识库')).toBeVisible();
  await page.getByLabel('搜索当前页', { exact: true }).fill('');
  await page.getByLabel('状态', { exact: true }).selectOption('');
  await expect(page.getByText('分页知识库-0', { exact: true })).toBeVisible();
  await expect(page.getByText('分页知识库-20', { exact: true })).toHaveCount(0);
  expect(f.unexpected).toEqual([]);
});

test('迟到第二页不能覆盖已切换父作用域 B', async ({ page }) => {
  const f = await install(page);
  f.rowsByScope.set(scopeKey(TENANT, PROJECT), Array.from({ length: 21 }, (_, i) => ({ id: `page-kb-${i}`, name: `分页知识库-${i}`, status: 'active', tenant_id: TENANT, project_id: PROJECT })));
  await page.goto('/admin/knowledge-bases'); await applyScope(page);
  await expect(page.getByText('分页知识库-0', { exact: true })).toBeVisible();
  f.controls.holdLists = true; await page.getByRole('button', { name: '下一页', exact: true }).click();
  await expect.poll(() => f.pendingLists.length).toBe(1);
  f.controls.holdLists = false; await applyScope(page, 'tenant-other', 'project-other');
  await expect(page.getByText('测试知识库 B', { exact: true })).toBeVisible();
  const response = page.waitForResponse((r) => r.url().includes('page=2') && r.url().includes(root));
  f.pendingLists[0].release(); await response;
  await expect(page.getByText('测试知识库 B', { exact: true })).toBeVisible();
  await expect(page.getByText('分页知识库-20', { exact: true })).toHaveCount(0);
  expect(f.unexpected).toEqual([]);
});

for (const operation of ['create', 'edit', 'restore', 'archive'] as const) {
  for (const failure of [false, true]) {
    test(`HTTP ${operation} ${failure ? '失败保留输入/状态和 trace' : '成功仅更新目标 ID'}`, async ({ page }) => {
      const f = await install(page); await page.goto('/admin/knowledge-bases'); await applyScope(page);
      await page.getByLabel('状态', { exact: true }).selectOption('archived');
      await expect(page.getByText('测试知识库 A', { exact: true })).toBeVisible();
      if (operation === 'archive' || operation === 'edit') {
        await page.getByRole('button', { name: '恢复', exact: true }).click();
        await expect(page.getByText('暂无匹配知识库')).toBeVisible();
        await page.getByLabel('状态', { exact: true }).selectOption('active');
      }
      f.controls.failWrites = failure;
      if (operation === 'create' || operation === 'edit') {
        await page.getByRole('button', { name: operation === 'create' ? '创建知识库' : '编辑', exact: true }).click();
        const dialog = page.getByRole('dialog'); await dialog.getByLabel('名称', { exact: false }).fill('更新名称');
        await dialog.getByRole('button', { name: '保存', exact: true }).click();
        if (failure) {
          await expect(dialog).toBeVisible(); await expect(dialog.getByLabel('名称', { exact: false })).toHaveValue('更新名称');
          await expect(dialog.getByText(/write failed.*trace-write|fixture-trace/)).toBeVisible();
          await dialog.getByRole('button', { name: '取消', exact: true }).click(); await expect(dialog).toHaveCount(0);
        } else { await expect(dialog).toHaveCount(0); if (operation === 'create') await page.getByLabel('状态', { exact: true }).selectOption('active'); await expect(page.getByText('更新名称', { exact: true })).toBeVisible(); }
      } else {
        if (operation === 'archive') page.once('dialog', (dialog) => dialog.accept());
        await page.getByRole('button', { name: operation === 'archive' ? '归档' : '恢复', exact: true }).click();
        if (failure) { await expect(page.getByText(/write failed.*trace-write/)).toBeVisible(); await expect(page.getByText('测试知识库 A', { exact: true })).toBeVisible(); }
        else await expect(page.getByText('暂无匹配知识库')).toBeVisible();
      }
      expect(f.rowsByScope.get(scopeKey('tenant-other', 'project-other'))).toEqual(initialRows.get(scopeKey('tenant-other', 'project-other')));
      expect(f.unexpected).toEqual([]);
    });
  }
}

for (const operation of ['archive', 'restore'] as const) {
  test(`第2页唯一KB${operation}移出筛选后回退并请求第1页`, async ({ page }) => {
    const f = await install(page);
    const rowStatus = operation === 'archive' ? 'active' : 'archived';
    f.rowsByScope.set(scopeKey(TENANT, PROJECT), Array.from({ length: 21 }, (_, i) => ({ id: `last-page-${i}`, name: `末页测试-${i}`, status: rowStatus, tenant_id: TENANT, project_id: PROJECT })));
    await page.goto('/admin/knowledge-bases'); await applyScope(page);
    if (operation === 'restore') await page.getByLabel('状态', { exact: true }).selectOption('archived');
    await expect(page.getByText('末页测试-0', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: '下一页', exact: true }).click();
    await expect(page.getByText('末页测试-20', { exact: true })).toBeVisible();
    const requestedPages: number[] = [];
    page.on('request', (request) => { const url = new URL(request.url()); if (url.pathname === root && request.method() === 'GET') requestedPages.push(Number(url.searchParams.get('page'))); });
    if (operation === 'archive') page.once('dialog', (dialog) => dialog.accept());
    await page.getByRole('button', { name: operation === 'archive' ? '归档' : '恢复', exact: true }).click();
    await expect(page.getByText('末页测试-0', { exact: true })).toBeVisible();
    await expect(page.getByText('末页测试-20', { exact: true })).toHaveCount(0);
    expect(requestedPages).toEqual([2, 1]);
    await expect(page.getByRole('button', { name: '下一页', exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await expect.poll(() => requestedPages.at(-1)).toBe(1);
    expect(f.unexpected).toEqual([]);
  });
}

test('归档KB编辑按钮禁用，fixture普通PATCH对归档态返回409', async ({ page }) => {
  const f = await install(page); await page.goto('/admin/knowledge-bases'); await applyScope(page);
  await page.getByLabel('状态', { exact: true }).selectOption('archived');
  await expect(page.getByText('测试知识库 A', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '编辑', exact: true })).toBeDisabled();
  const response = await page.evaluate(async (root) => fetch(`${root}/kb-a`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: '非法编辑' }) }).then((r) => r.status), root);
  expect(response).toBe(409); expect(f.writes).toEqual([]); expect(f.unexpected).toEqual([]);
});

test('保存期间防重提交且取消禁用，取消未提交表单不写接口', async ({ page }) => {
  const f = await install(page); await page.goto('/admin/knowledge-bases'); await applyScope(page);
  await page.getByRole('button', { name: '创建知识库', exact: true }).click();
  let dialog = page.getByRole('dialog'); await dialog.getByLabel('名称', { exact: false }).fill('取消草稿');
  await dialog.getByRole('button', { name: '取消', exact: true }).click(); expect(f.writes).toEqual([]);
  await page.getByRole('button', { name: '创建知识库', exact: true }).click(); dialog = page.getByRole('dialog');
  await expect(dialog.getByLabel('名称', { exact: false })).toHaveValue(''); await dialog.getByLabel('名称', { exact: false }).fill('只提交一次');
  f.controls.holdWrites = true; await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await expect.poll(() => f.pendingWrites.length).toBe(1); await expect(dialog.getByRole('button').filter({ hasText: '保存中...' })).toBeDisabled();
  await expect(dialog.getByRole('button', { name: '取消', exact: true })).toBeDisabled(); expect(f.writes).toHaveLength(1);
  f.pendingWrites[0].release(); await expect(dialog).toHaveCount(0); expect(f.unexpected).toEqual([]);
});
