import { test as baseTest, expect, type Page } from '@playwright/test';

const test = baseTest.extend<{ audit: void }>({ audit: [async ({ page }, use, info) => {
  const errors: string[] = []; const fatal: string[] = []; const statuses = new Set<number>();
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => fatal.push(e.message));
  page.on('response', (r) => { if (r.status() >= 400 && r.url().includes('/api/')) statuses.add(r.status()); });
  await use(); await info.attach('console-errors', { body: JSON.stringify({ expectedStatuses: [...statuses], errors, pageerrors: fatal }), contentType: 'application/json' });
  expect(fatal).toEqual([]);
  expect(errors.filter((text) => ![...statuses].some((s) => text.includes(String(s))) && !(statuses.size > 0 && /API 错误处理:|Axios 错误:|标准化业务错误:/.test(text)))).toEqual([]);
}, { auto: true }] });

const root = '/api/v1/admin/knowledge-bases';
const A = { id: 'kb-a', name: '知识库 A', tenant: 'tenant-a', project: 'project-a' };
const B = { id: 'kb-b', name: '知识库 B', tenant: 'tenant-b', project: 'project-b' };
const env = (data: unknown, code = 200, message = 'ok') => ({ code, message, data, timestamp: '2026-10-09', trace_id: `trace-${code}` });

type Flags = { detail: number; documents: number; jobs: number; workspace: number };
async function install(page: Page, flags: Partial<Flags> = {}) {
  const calls: Array<{ path: string; kb?: string; query: string }> = [];
  const unexpected: string[] = []; const errors: string[] = []; const pageErrors: string[] = [];
  const status = { detail: flags.detail ?? 200, documents: flags.documents ?? 200, jobs: flags.jobs ?? 200, workspace: flags.workspace ?? 200 };
  await page.addInitScript(() => localStorage.setItem('admin_token', 'synthetic-token'));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => pageErrors.push(e.message));
  await page.route('**/*', async (route) => {
    const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' });
    if (path === '/api/v1/admin/auth/me') return route.fulfill({ json: env({ id: 1, username: 'synthetic', is_active: true }) });
    const detail = path.match(new RegExp(`^${root}/(kb-a|kb-b)$`));
    if (detail && request.method() === 'GET') {
      const id = detail[1]; calls.push({ path, kb: id, query: url.search });
      if (status.detail !== 200) return route.fulfill({ status: status.detail, json: env(null, status.detail, status.detail === 403 ? 'KB_ACCESS_DENIED' : 'KB_DETAIL_FAILURE') });
      const item = id === A.id ? { id: A.id, name: A.name, status: 'active', tenant_id: A.tenant, project_id: A.project } : { id: B.id, name: B.name, status: 'archived', tenant_id: B.tenant, project_id: B.project };
      return route.fulfill({ json: env(item) });
    }
    if (path === '/api/documents' && request.method() === 'GET') {
      const kb = request.headers()['x-kb-id']; calls.push({ path, kb, query: url.search });
      if (status.documents !== 200) return route.fulfill({ status: status.documents, json: env(null, status.documents, 'DOCUMENTS_FAILURE') });
      return route.fulfill({ json: env({ items: kb === A.id ? [{ id: 'doc-a', name: '文档 A', status: 'ready', graph_status: 'ready', vector_status: 'ready' }] : [] }) });
    }
    if (path === '/api/v1/admin/jobs' && request.method() === 'GET') {
      const kb = url.searchParams.get('kb_id'); calls.push({ path, kb: kb ?? undefined, query: url.search });
      if (status.jobs !== 200) return route.fulfill({ status: status.jobs, json: env(null, status.jobs, 'JOBS_FAILURE') });
      return route.fulfill({ json: env({ items: kb === A.id ? [{ id: 1, job_type: 'reindex', status: 'completed' }] : [], total: 0, page: 1, page_size: 10, total_pages: 1 }) });
    }
    if (path === '/api/knowledge-bases' && request.method() === 'GET') {
      calls.push({ path, query: url.search });
      if (status.workspace !== 200) return route.fulfill({ status: status.workspace, json: env(null, status.workspace, 'WORKSPACE_DENIED') });
      return route.fulfill({ json: env({ items: [{ kb_id: A.id, name: A.name, project_id: A.project }] }) });
    }
    if (path.startsWith('/api/') || url.origin !== 'http://127.0.0.1:1234') { unexpected.push(`${request.method()} ${path}`); return route.abort(); }
    return route.continue();
  });
  return { calls, unexpected, errors, pageErrors };
}

async function open(page: Page, id: string) { await page.goto(`/admin/knowledge-bases/${id}`); }

test('详情按 KB 作用域请求基础、文档、任务数据', async ({ page }) => {
  const f = await install(page); await open(page, A.id);
  await expect(page.getByTestId('kb-basic')).toBeVisible();
  await expect(page.getByTestId('kb-documents')).toBeVisible();
  await expect(page.getByTestId('kb-jobs')).toBeVisible();
  await expect(page.getByText(A.name, { exact: true })).toBeVisible();
  await expect(page.getByText('文档 A', { exact: true })).toBeVisible();
  expect(f.calls).toEqual(expect.arrayContaining([
    expect.objectContaining({ path: `${root}/${A.id}`, kb: A.id }),
    expect.objectContaining({ path: '/api/documents', kb: A.id }),
    expect.objectContaining({ path: '/api/v1/admin/jobs', kb: A.id, query: expect.stringContaining('kb_id=kb-a') }),
  ]));
  expect(f.unexpected).toEqual([]); expect(f.pageErrors).toEqual([]);
});

for (const area of ['detail', 'documents', 'jobs'] as const) {
  for (const code of [403, 500]) {
    test(`详情 ${area} 独立 ${code} 不吞其他区域`, async ({ page }) => {
      const f = await install(page, { [area]: code }); await open(page, A.id);
      const region = page.getByTestId(area === 'detail' ? 'kb-basic' : area === 'documents' ? 'kb-documents' : 'kb-jobs');
      await expect(region.getByRole('alert')).toBeVisible();
      if (area !== 'detail') await expect(page.getByTestId('kb-basic').getByText(A.name, { exact: true })).toBeVisible();
      if (area !== 'documents') await expect(page.getByTestId('kb-documents').getByText('文档 A', { exact: true })).toBeVisible();
      if (area !== 'jobs') await expect(page.getByTestId('kb-jobs').getByText('reindex', { exact: true })).toBeVisible();
      expect(f.unexpected).toEqual([]); expect(f.pageErrors).toEqual([]);
    });
  }
}

test('归档 KB 禁止进入工作台，未知状态和空统计可见', async ({ page }) => {
  const f = await install(page); await open(page, B.id);
  await expect(page.getByText(B.name, { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '进入工作台', exact: true })).toBeDisabled();
  await expect(page.getByText('暂无描述', { exact: true })).toBeVisible();
  await expect(page.getByText('暂无文档', { exact: true })).toBeVisible();
  await expect(page.getByText('暂无该 KB 任务', { exact: true })).toBeVisible();
  expect(f.unexpected).toEqual([]);
});

test('详情 A-B-A 乱序导航不保留旧内容', async ({ page }) => {
  const f = await install(page); await open(page, A.id); await expect(page.getByText(A.name, { exact: true })).toBeVisible();
  await page.evaluate(() => { history.pushState({}, '', '/admin/knowledge-bases/kb-b'); window.dispatchEvent(new PopStateEvent('popstate')); });
  await expect(page.getByText(B.name, { exact: true })).toBeVisible(); await expect(page.getByText('文档 A', { exact: true })).toHaveCount(0);
  await page.evaluate(() => { history.pushState({}, '', '/admin/knowledge-bases/kb-a'); window.dispatchEvent(new PopStateEvent('popstate')); });
  await expect(page.getByText(A.name, { exact: true })).toBeVisible(); await expect(page.getByText('文档 A', { exact: true })).toBeVisible();
  expect(f.unexpected).toEqual([]); expect(f.pageErrors).toEqual([]);
});
