import { expect, test as base, type Page, type Route, type Request } from '@playwright/test';

const TOKEN = 'synthetic-workspace-admin-token';
const A = 'kb-alpha';
const B = 'kb-beta';
const INPUT = '输入问题，Enter 发送，Shift+Enter 换行';

type Reply = { data?: unknown; status?: number; message?: string };
type Call = { path: string; method: string; kb: string; request: Request };
type Gate = { call: Call; release: (reply: Reply) => void; finished: Promise<void> };
type CatalogReply = { items: Array<{ kb_id: string; name: string; project_id: string; status: string }> };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

function envelope(data: unknown, status = 200, message = 'ok') {
  return { code: status, message, data, timestamp: '2026-01-01T00:00:00Z', trace_id: 'trace-synthetic-workspace' };
}

function documentItems(name: string) {
  return { items: [{ id: `${name}-id`, name, path: `/synthetic/${name}`, ext: '.txt', size: 1024, updated_at: 1 }] };
}

function qaAnswer(answer: string, kb = A) {
  return { answer, citations: [{ id: `${kb}-chunk`, title: `${kb}-citation.txt`, snippet: `${kb}-evidence`, entity_names: ['synthetic entity'], confidence: 0.9 }] };
}

const GRAPH = {
  nodes: [{ id: 'synthetic-node', labels: ['Entity'], properties: { name: 'synthetic entity' } }],
  edges: [],
  stats: { nodeCount: 1, edgeCount: 0, executionTime: 0.01 },
};
const SCHEMA = {
  labels: [{ label: 'Entity', count: 1 }], relationships: [], patterns: [], nodeProperties: [], relProperties: [],
  sampleQuery: 'MATCH (n) RETURN n LIMIT 1', stats: GRAPH.stats,
};
const UPLOAD = {
  uploaded: [{ id: 'synthetic-upload', doc_id: 'synthetic-upload', name: 'uploaded.txt', path: '/synthetic/uploaded.txt', ext: '.txt', size: 32 }],
  skipped: [],
};
const BUILD = { job_id: 42, status: 'queued', message: 'synthetic build queued' };

class Fixtures {
  readonly calls: Call[] = [];
  readonly violations: string[] = [];
  readonly consoleErrors: string[] = [];
  qaFailure = false;
  catalogReplies: CatalogReply[] = [{ items: [
    { kb_id: A, name: 'Alpha 知识库', project_id: 'synthetic-project-a', status: 'active' },
    { kb_id: B, name: 'Beta 知识库', project_id: 'synthetic-project-b', status: 'active' },
  ] }];
  private readonly heldPaths = new Set<string>();
  private readonly gates: Gate[] = [];
  private closing = false;

  constructor(private readonly page: Page, private readonly frontendOrigin: string) {}

  hold(path: string) { this.heldPaths.add(path); }

  count(path: string, kb?: string) {
    return this.calls.filter((call) => call.path === path && (kb === undefined || call.kb === kb)).length;
  }

  async waitFor(path: string, kb: string, ordinal = 1) {
    await expect.poll(() => this.gates.filter((gate) => gate.call.path === path && gate.call.kb === kb).length).toBeGreaterThanOrEqual(ordinal);
    return this.gates.filter((gate) => gate.call.path === path && gate.call.kb === kb)[ordinal - 1];
  }

  async release(gate: Gate, reply: Reply) {
    gate.release(reply);
    await gate.finished;
    await this.renderBarrier();
  }

  async renderBarrier() {
    await this.page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  }

  private async fulfill(route: Route, reply: Reply) {
    const status = reply.status ?? 200;
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(envelope(reply.data ?? null, status, reply.message)) });
  }

  async install() {
    this.page.on('console', (message) => {
      if (message.type() === 'error') this.consoleErrors.push(message.text());
    });
    this.page.on('pageerror', (error) => {
      this.consoleErrors.push(`pageerror: ${error.message}`);
      this.violations.push(`Uncaught browser error: ${error.message}`);
    });
    await this.page.context().addInitScript(({ token, kb }) => {
      localStorage.clear();
      localStorage.setItem('admin_token', token);
      const activeKbId = new URLSearchParams(location.search).has('empty-kb') ? null : kb;
      localStorage.setItem('graph-insight-storage', JSON.stringify({ state: { activeKbId }, version: 0 }));
    }, { token: TOKEN, kb: A });
    await this.page.context().route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const method = request.method();
      if (url.hostname === 'fonts.googleapis.com' && request.resourceType() === 'stylesheet') {
        await route.fulfill({ status: 200, contentType: 'text/css', body: '' });
        return;
      }
      if (url.hostname === 'fonts.gstatic.com' && request.resourceType() === 'font') {
        await route.abort('blockedbyclient');
        return;
      }
      const business = path.startsWith('/api/') || path === '/client-logs';
      if (!business) {
        const resource = request.resourceType();
        const frontendResource = url.origin === this.frontendOrigin && ['document', 'script', 'stylesheet', 'image', 'font', 'other'].includes(resource);
        if (frontendResource) {
          await route.continue();
        } else {
          this.violations.push(`Unexpected external/non-resource request: ${method} ${url.origin}${path}`);
          await route.abort('blockedbyclient');
        }
        return;
      }
      const call: Call = { path, method, kb: request.headers()['x-kb-id'] ?? '', request };
      this.calls.push(call);
      const known: Record<string, Reply> = {
        'GET /api/v1/admin/auth/me': { data: { id: 1, username: 'synthetic', email: 'synthetic@example.test', is_active: true, created_at: '2026-01-01', login_count: 1, preferred_home_path: '/workspace' } },
        'GET /api/knowledge-bases': { data: this.catalogReplies[0] ?? { items: [] } },
        'GET /api/documents': { data: documentItems(call.kb === B ? 'beta-report.txt' : 'alpha-report.txt') },
        'GET /api/graph/schema': { data: SCHEMA },
        'POST /api/docqa': this.qaFailure ? { status: 503, message: 'synthetic QA unavailable' } : { data: qaAnswer('synthetic answer', call.kb) },
        'POST /api/docqa/deep-research': { data: { question: 'synthetic research', report: 'synthetic research report', summary: 'synthetic summary', final_conclusion: 'synthetic conclusion', citations: qaAnswer('', call.kb).citations, sub_questions: [], confidence: { score: 0.9, level: 'high' }, evidence_stats: { sub_questions: 0, retrieved_chunks: 1, unique_citations: 1 } } },
        'POST /api/query': { data: GRAPH },
        'POST /api/documents/upload': { data: UPLOAD },
        'POST /api/graph/build': { data: BUILD },
        'POST /client-logs': { data: null },
        'POST /api/client-logs': { data: null },
      };
      const deletionFixture = method === 'DELETE' && /^\/api\/documents\/[^/]+$/.test(path)
        ? { data: { doc_id: path.split('/').pop(), dry_run: url.searchParams.get('dry_run') === 'true', candidate_file: { exists: true }, file_action: 'soft_deleted', graph: { documents: 1, chunks: 1, relations: 1, orphan_entities: 0 } } }
        : undefined;
      const reply = known[`${method} ${path}`] ?? deletionFixture;
      if (!reply) {
        this.violations.push(`Unhandled business request: ${method} ${path}`);
        await route.abort('blockedbyclient');
        return;
      }
      if (path.startsWith('/api/') && path !== '/api/v1/admin/auth/me' && path !== '/api/client-logs') {
        if (request.headers().authorization !== `Bearer ${TOKEN}`) this.violations.push(`Missing synthetic authorization: ${method} ${path}`);
      }
      if (this.heldPaths.has(path)) {
        const release = deferred<Reply>();
        const finished = deferred<void>();
        this.gates.push({ call, release: release.resolve, finished: finished.promise });
        try {
          const released = await release.promise;
          if (this.closing) await route.abort('blockedbyclient');
          else await this.fulfill(route, released);
        } catch (error) {
          if (!this.closing && !request.failure()) throw error;
        } finally {
          finished.resolve();
        }
      } else {
        await this.fulfill(route, reply);
      }
    });
  }

  async close() {
    this.closing = true;
    for (const gate of this.gates) gate.release({});
    await Promise.all(this.gates.map((gate) => gate.finished));
  }
}

const test = base.extend<{ fixtures: Fixtures }>({
  fixtures: async ({ page, baseURL }, runFixture, testInfo) => {
    expect(baseURL, 'Playwright baseURL is required').toBeTruthy();
    const fixtures = new Fixtures(page, new URL(baseURL!).origin);
    await fixtures.install();
    try {
      await runFixture(fixtures);
    } finally {
      await fixtures.close();
      if (fixtures.consoleErrors.length > 0) {
        await testInfo.attach('browser-console-errors', { body: fixtures.consoleErrors.join('\n'), contentType: 'text/plain' });
      }
      expect(fixtures.violations, 'All non-frontend traffic must be explicitly mocked or aborted').toEqual([]);
    }
  },
});

const selector = (page: Page) => page.getByTestId('knowledge-base-selector').getByRole('combobox');

async function chooseKb(page: Page, kb: string) {
  await selector(page).click();
  await page.getByTestId(`kb-option-${kb}`).click();
  await expect(selector(page)).toContainText(kb === A ? 'Alpha' : 'Beta');
}

async function openWorkspace(page: Page) {
  await page.goto('/workspace');
  await expect(selector(page)).toBeVisible();
}

async function ask(page: Page, question: string) {
  const input = page.getByPlaceholder(INPUT);
  await input.fill(question);
  await input.press('Enter');
}

async function upload(page: Page) {
  await page.locator('input[type="file"]').setInputFiles({ name: 'uploaded.txt', mimeType: 'text/plain', buffer: Buffer.from('synthetic document') });
}

test('R1：目录新成功结果胜过旧成功结果', async ({ page, fixtures }) => {
  fixtures.hold('/api/knowledge-bases');
  await page.goto('/workspace');
  const first = await fixtures.waitFor('/api/knowledge-bases', A);
  const initialSecond = await fixtures.waitFor('/api/knowledge-bases', A, 2);
  const initialReply = { data: { items: [{ kb_id: A, name: 'Alpha 初始目录', project_id: 'synthetic-project-a', status: 'active' }] } };
  await fixtures.release(first, initialReply);
  await fixtures.release(initialSecond, initialReply);
  await expect(page.getByTestId('kb-selector-refresh')).toBeVisible();
  const beforeRefresh = fixtures.count('/api/knowledge-bases', A);
  await page.getByTestId('kb-selector-refresh').click();
  const oldRefresh = await fixtures.waitFor('/api/knowledge-bases', A, beforeRefresh + 1);
  await page.getByTestId('kb-selector-refresh').click();
  const latest = await fixtures.waitFor('/api/knowledge-bases', A, beforeRefresh + 2);
  expect(latest).not.toBe(oldRefresh);
  await fixtures.release(latest, { data: { items: [{ kb_id: A, name: 'Alpha 当前目录', project_id: 'synthetic-project-a', status: 'active' }, { kb_id: B, name: 'Beta 最新目录', project_id: 'synthetic-project-b', status: 'active' }] } });
  await expect(page.getByTestId('knowledge-base-selector')).toHaveCount(1);
  await page.getByTestId('knowledge-base-selector').getByRole('combobox').click();
  await expect(page.getByTestId('kb-option-kb-beta')).toContainText('Beta 最新目录');
  await page.getByTestId('kb-option-kb-beta').click();
  await expect(selector(page)).toContainText('Beta 最新目录');
  await fixtures.release(oldRefresh, { data: { items: [{ kb_id: A, name: 'Alpha 旧目录', project_id: 'synthetic-project-a', status: 'active' }] } });
  await expect(selector(page)).toContainText('Beta 最新目录');
  await expect(page.getByTestId('knowledge-base-selector')).toHaveCount(1);
  await page.getByTestId('knowledge-base-selector').getByRole('combobox').click();
  await expect(page.getByTestId('kb-option-kb-beta')).toHaveCount(1);
  await expect(page.getByTestId('kb-option-kb-beta')).toContainText('Beta 最新目录');
  await page.keyboard.press('Escape');
});

test('R2：目录新成功结果胜过旧错误结果，不清除新 active KB', async ({ page, fixtures }) => {
  fixtures.hold('/api/knowledge-bases');
  await page.goto('/workspace');
  const first = await fixtures.waitFor('/api/knowledge-bases', A);
  const initialSecond = await fixtures.waitFor('/api/knowledge-bases', A, 2);
  const initialReply = { data: { items: [{ kb_id: A, name: 'Alpha 初始目录', project_id: 'synthetic-project-a', status: 'active' }] } };
  await fixtures.release(first, initialReply);
  await fixtures.release(initialSecond, initialReply);
  await expect(page.getByTestId('kb-selector-refresh')).toBeVisible();
  const beforeRefresh = fixtures.count('/api/knowledge-bases', A);
  await page.getByTestId('kb-selector-refresh').click();
  const oldRefresh = await fixtures.waitFor('/api/knowledge-bases', A, beforeRefresh + 1);
  await page.getByTestId('kb-selector-refresh').click();
  const latest = await fixtures.waitFor('/api/knowledge-bases', A, beforeRefresh + 2);
  expect(latest).not.toBe(oldRefresh);
  await fixtures.release(latest, { data: { items: [{ kb_id: A, name: 'Alpha 当前目录', project_id: 'synthetic-project-a', status: 'active' }, { kb_id: B, name: 'Beta 成功目录', project_id: 'synthetic-project-b', status: 'active' }] } });
  await expect(page.getByTestId('knowledge-base-selector')).toHaveCount(1);
  await page.getByTestId('knowledge-base-selector').getByRole('combobox').click();
  await expect(page.getByTestId('kb-option-kb-beta')).toContainText('Beta 成功目录');
  await page.getByTestId('kb-option-kb-beta').click();
  await expect(selector(page)).toContainText('Beta 成功目录');
  await fixtures.release(oldRefresh, { status: 503, message: 'stale catalog failure' });
  await expect(selector(page)).toContainText('Beta 成功目录');
  await expect(page.getByTestId('knowledge-base-selector')).toHaveCount(1);
  await page.getByTestId('knowledge-base-selector').getByRole('combobox').click();
  await expect(page.getByTestId('kb-option-kb-beta')).toHaveCount(1);
  await expect(page.getByTestId('kb-option-kb-beta')).toContainText('Beta 成功目录');
  await expect(page.getByTestId('kb-selector-error')).toHaveCount(0);
  await page.keyboard.press('Escape');
});

test('A→B：A 文档迟到不得覆盖 B 已加载文档', async ({ page, fixtures }) => {
  fixtures.hold('/api/documents');
  await openWorkspace(page);
  await chooseKb(page, A);
  const oldA = await fixtures.waitFor('/api/documents', A);
  await chooseKb(page, B);
  const currentB = await fixtures.waitFor('/api/documents', B);
  await fixtures.release(currentB, { data: documentItems('beta-current.txt') });
  await expect(page.getByText('beta-current.txt', { exact: true })).toBeVisible();
  await fixtures.release(oldA, { data: documentItems('alpha-stale.txt') });
  await expect(page.getByText('beta-current.txt', { exact: true })).toBeVisible();
  await expect(page.getByText('alpha-stale.txt', { exact: true })).toHaveCount(0);
});

test('A 列表旧 error/finally 不得覆盖 B 错误或清除 B loading', async ({ page, fixtures }) => {
  fixtures.hold('/api/documents');
  await openWorkspace(page);
  const oldA = await fixtures.waitFor('/api/documents', A);
  await chooseKb(page, B);
  const currentB = await fixtures.waitFor('/api/documents', B);
  await expect(page.getByText('正在加载文档', { exact: true })).toBeVisible();
  await fixtures.release(oldA, { status: 503, message: 'stale alpha document failure' });
  await expect(page.getByText('stale alpha document failure [trace_id: trace-synthetic-workspace]', { exact: true })).toHaveCount(0);
  await expect(page.getByText('正在加载文档', { exact: true })).toBeVisible();
  await fixtures.release(currentB, { data: documentItems('beta-after-old-error.txt') });
  await expect(page.getByText('beta-after-old-error.txt', { exact: true })).toBeVisible();
  await expect(page.getByText('正在加载文档', { exact: true })).toHaveCount(0);
});

test('A→B→A：同一 KB 的旧世代响应仍必须作废', async ({ page, fixtures }) => {
  fixtures.hold('/api/documents');
  await openWorkspace(page);
  await chooseKb(page, A);
  const firstA = await fixtures.waitFor('/api/documents', A);
  const initialACount = fixtures.count('/api/documents', A);
  await chooseKb(page, B);
  const oldB = await fixtures.waitFor('/api/documents', B);
  await chooseKb(page, A);
  const currentA = await fixtures.waitFor('/api/documents', A, initialACount + 1);
  await fixtures.release(currentA, { data: documentItems('alpha-current-generation.txt') });
  await expect(page.getByText('alpha-current-generation.txt', { exact: true })).toBeVisible();
  await fixtures.release(oldB, { data: documentItems('beta-stale.txt') });
  await fixtures.release(firstA, { data: documentItems('alpha-stale-generation.txt') });
  await expect(page.getByText('alpha-current-generation.txt', { exact: true })).toBeVisible();
  await expect(page.getByText(/beta-stale|alpha-stale-generation/)).toHaveCount(0);
});

test('旧 QA 成功：切到 B 后不得恢复 A 回答、引用或触发 B 图谱查询', async ({ page, fixtures }) => {
  fixtures.hold('/api/docqa');
  await openWorkspace(page);
  await chooseKb(page, A);
  await ask(page, 'old alpha question');
  const oldQa = await fixtures.waitFor('/api/docqa', A);
  expect(oldQa.call.request.postDataJSON()).toMatchObject({ kb_id: A, question: 'old alpha question' });
  await chooseKb(page, B);
  await expect(page.getByText('old alpha question', { exact: true })).toHaveCount(0);
  await fixtures.release(oldQa, { data: qaAnswer('stale alpha answer') });
  await expect(page.getByText('stale alpha answer', { exact: true })).toHaveCount(0);
  await expect(page.getByText(`${A}-evidence`, { exact: true })).toHaveCount(0);
  expect(fixtures.count('/api/query')).toBe(0);
  await expect(page.getByRole('tab', { name: '引用证据' })).toHaveAttribute('aria-selected', 'true');
});

test('切到 B 的新问答不携带 A 的会话历史', async ({ page, fixtures }) => {
  await openWorkspace(page);
  await chooseKb(page, A);
  await ask(page, 'alpha history question');
  await expect(page.getByText('synthetic answer', { exact: true })).toBeVisible();
  await chooseKb(page, B);
  await ask(page, 'beta isolated question');
  await expect.poll(() => fixtures.count('/api/docqa', B)).toBe(1);
  const betaQa = fixtures.calls.find((call) => call.path === '/api/docqa' && call.kb === B);
  expect(betaQa?.request.postDataJSON()).toMatchObject({ kb_id: B, question: 'beta isolated question', conversation_history: [] });
  expect(JSON.stringify(betaQa?.request.postDataJSON())).not.toContain('alpha history question');
  await expect(page.getByText('synthetic answer', { exact: true })).toHaveCount(1);
});

test('旧 QA 失败与 finally：不得污染 B 或提前解除 B 的 typing', async ({ page, fixtures }) => {
  fixtures.hold('/api/docqa');
  await openWorkspace(page);
  await chooseKb(page, A);
  await ask(page, 'old failing question');
  const oldQa = await fixtures.waitFor('/api/docqa', A);
  await chooseKb(page, B);
  await ask(page, 'current beta question');
  const currentQa = await fixtures.waitFor('/api/docqa', B);
  await fixtures.release(oldQa, { status: 503, message: 'stale alpha failure' });
  await expect(page.getByText('stale alpha failure', { exact: true })).toHaveCount(0);
  await page.getByPlaceholder(INPUT).fill('must remain blocked');
  await expect(page.getByRole('button', { name: '深度调研', exact: true })).toBeDisabled();
  await page.getByPlaceholder(INPUT).press('Enter');
  await fixtures.renderBarrier();
  expect(fixtures.count('/api/docqa', B)).toBe(1);
  await fixtures.release(currentQa, { data: qaAnswer('current beta answer', B) });
  await expect(page.getByText('current beta answer', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '深度调研', exact: true })).toBeEnabled();
});

test('旧图谱查询：切库后迟到的图与引用不得恢复', async ({ page, fixtures }) => {
  fixtures.hold('/api/query');
  await openWorkspace(page);
  await chooseKb(page, A);
  await ask(page, 'alpha graph question');
  const oldGraph = await fixtures.waitFor('/api/query', A);
  await expect(page.getByText('synthetic answer', { exact: true })).toBeVisible();
  await chooseKb(page, B);
  await fixtures.release(oldGraph, { data: GRAPH });
  await expect(page.getByRole('tab', { name: '引用证据' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText(`${A}-evidence`, { exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: '关系图谱' }).click();
  await expect(page.getByText('执行 Cypher 查询以显示图谱', { exact: true })).toBeVisible();
});

test('旧上传成功：不得在 B 留最近上传或触发 B 文档刷新', async ({ page, fixtures }) => {
  fixtures.hold('/api/documents/upload');
  await openWorkspace(page);
  await chooseKb(page, A);
  await upload(page);
  const oldUpload = await fixtures.waitFor('/api/documents/upload', A);
  await chooseKb(page, B);
  await expect(page.getByText('beta-report.txt', { exact: true })).toBeVisible();
  const before = fixtures.count('/api/documents', B);
  await fixtures.release(oldUpload, { data: UPLOAD });
  await expect(page.getByText(/上传成功 1/)).toHaveCount(0);
  await expect(page.getByText('最近上传 1', { exact: true })).toHaveCount(0);
  expect(fixtures.count('/api/documents', B)).toBe(before);
});

test('旧建图提交成功：不得显示在 B 或清空 B 新上传范围', async ({ page, fixtures }) => {
  fixtures.hold('/api/graph/build');
  await openWorkspace(page);
  await chooseKb(page, A);
  await upload(page);
  await expect(page.getByText(/上传成功 1/)).toBeVisible();
  await page.getByRole('button', { name: '一键建图', exact: true }).click();
  const oldBuild = await fixtures.waitFor('/api/graph/build', A);
  expect(oldBuild.call.request.postDataJSON()).toMatchObject({ source: 'selected_documents', doc_ids: ['synthetic-upload'] });
  await chooseKb(page, B);
  await upload(page);
  await expect(page.getByText(/上传成功 1/)).toBeVisible();
  await fixtures.release(oldBuild, { data: BUILD });
  await expect(page.getByText('建图任务 #42 已提交', { exact: true })).toHaveCount(0);
  await expect(page.getByText('最近上传 1', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('button', { name: '一键建图', exact: true })).toBeEnabled();
});

test('同 KB 重复选择：保留会话、草稿、图谱且不额外刷新文档', async ({ page, fixtures }) => {
  await openWorkspace(page);
  await chooseKb(page, A);
  await ask(page, 'same kb question');
  await expect(page.getByText('synthetic answer', { exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: '关系图谱' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByText('执行 Cypher 查询以显示图谱', { exact: true })).toHaveCount(0);
  const before = fixtures.count('/api/documents', A);
  await page.getByPlaceholder(INPUT).fill('same kb draft');
  await chooseKb(page, A);
  await fixtures.renderBarrier();
  await expect(page.getByPlaceholder(INPUT)).toHaveValue('same kb draft');
  await expect(page.getByText('synthetic answer', { exact: true })).toBeVisible();
  await expect(page.getByText('执行 Cypher 查询以显示图谱', { exact: true })).toHaveCount(0);
  expect(fixtures.count('/api/documents', A)).toBe(before);
});

test('当前 KB 正常路径：QA 引用图谱、上传刷新与建图范围保持', async ({ page, fixtures }) => {
  await openWorkspace(page);
  await chooseKb(page, A);
  await expect(page.getByText('alpha-report.txt', { exact: true })).toBeVisible();
  await ask(page, 'normal scoped question');
  await expect(page.getByText('synthetic answer', { exact: true })).toBeVisible();
  await expect(page.getByRole('tab', { name: '关系图谱' })).toHaveAttribute('aria-selected', 'true');
  await page.getByRole('tab', { name: '引用证据' }).click();
  await expect(page.getByText(`${A}-evidence`, { exact: true })).toBeVisible();
  await upload(page);
  await expect(page.getByText(/上传成功 1/)).toBeVisible();
  await expect.poll(() => fixtures.count('/api/documents', A)).toBeGreaterThanOrEqual(2);
  await page.getByRole('button', { name: '一键建图', exact: true }).click();
  await expect(page.getByText('建图任务 #42 已提交', { exact: true })).toBeVisible();
  const build = fixtures.calls.find((call) => call.path === '/api/graph/build');
  expect(build?.kb).toBe(A);
  expect(build?.request.postDataJSON()).toMatchObject({ source: 'selected_documents', doc_ids: ['synthetic-upload'] });
  await expect(page.getByText('最近上传 1', { exact: true })).toHaveCount(0);
});

test('当前 KB QA 失败：finally 后可以再次发送', async ({ page, fixtures }) => {
  fixtures.qaFailure = true;
  await openWorkspace(page);
  await chooseKb(page, A);
  await ask(page, 'normal failure');
  await expect(page.getByText('synthetic QA unavailable [trace_id: trace-synthetic-workspace]', { exact: true })).toBeVisible();
  await page.getByPlaceholder(INPUT).fill('retry question');
  await expect(page.getByRole('button', { name: '深度调研', exact: true })).toBeEnabled();
  fixtures.qaFailure = false;
  await page.getByPlaceholder(INPUT).press('Enter');
  await expect(page.getByText('synthetic answer', { exact: true })).toBeVisible();
  expect(fixtures.count('/api/docqa', A)).toBe(2);
});

test('未选择 KB：文档、QA、调研、上传、建图均不发业务请求', async ({ page, fixtures }) => {
  await page.goto('/workspace?empty-kb=1');
  await expect(selector(page)).toContainText('请选择知识库');
  await ask(page, 'unscoped question');
  await page.getByPlaceholder(INPUT).fill('unscoped research');
  const research = page.getByRole('button', { name: '深度调研', exact: true });
  if (await research.isEnabled()) await research.click();
  const build = page.getByRole('button', { name: '一键建图', exact: true });
  if (await build.isEnabled()) await build.click();
  await upload(page);
  await fixtures.renderBarrier();
  for (const path of ['/api/documents', '/api/docqa', '/api/docqa/deep-research', '/api/documents/upload', '/api/graph/build', '/api/query', '/api/graph/schema']) {
    expect(fixtures.count(path), `unscoped request to ${path}`).toBe(0);
  }
});

test('同 KB 上传刷新乱序：最新列表必须胜出', async ({ page, fixtures }) => {
  fixtures.hold('/api/documents');
  await openWorkspace(page);
  await fixtures.waitFor('/api/documents', A);
  await fixtures.renderBarrier();
  const initialCount = fixtures.count('/api/documents', A);
  const initial = await fixtures.waitFor('/api/documents', A, initialCount);
  await fixtures.release(initial, { data: documentItems('initial.txt') });
  await expect(page.getByText('initial.txt', { exact: true })).toBeVisible();
  await upload(page);
  const refreshOne = await fixtures.waitFor('/api/documents', A, initialCount + 1);
  await expect(page.getByRole('button', { name: '上传', exact: true })).toBeEnabled();
  await upload(page);
  const refreshTwo = await fixtures.waitFor('/api/documents', A, initialCount + 2);
  await fixtures.release(refreshTwo, { data: documentItems('refresh-latest.txt') });
  await expect(page.getByText('refresh-latest.txt', { exact: true })).toBeVisible();
  await fixtures.release(refreshOne, { data: documentItems('refresh-stale.txt') });
  await expect(page.getByText('refresh-latest.txt', { exact: true })).toBeVisible();
  await expect(page.getByText('refresh-stale.txt', { exact: true })).toHaveCount(0);
});

test('卸载 workspace 后迟到 QA 不再写状态或发图谱请求', async ({ page, fixtures }) => {
  fixtures.hold('/api/docqa');
  await openWorkspace(page);
  await ask(page, 'question before unmount');
  const oldQa = await fixtures.waitFor('/api/docqa', A);
  await page.evaluate(() => {
    localStorage.removeItem('admin_token');
    window.dispatchEvent(new CustomEvent('graphinsight:admin-session-change', { detail: { authenticated: false } }));
    history.pushState({}, '', '/login');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page.getByPlaceholder(INPUT)).toHaveCount(0);
  await fixtures.release(oldQa, { data: qaAnswer('answer after unmount') });
  expect(fixtures.count('/api/query')).toBe(0);
  await expect(page.getByText('answer after unmount', { exact: true })).toHaveCount(0);
});

test('删除 dry-run 迟到：切库后禁止确认与执行删除', async ({ page, fixtures }) => {
  await openWorkspace(page);
  await expect(page.getByText('alpha-report.txt', { exact: true })).toBeVisible();
  const path = '/api/documents/alpha-report.txt-id';
  fixtures.hold(path);
  let confirmations = 0;
  page.on('dialog', async (dialog) => {
    confirmations += 1;
    await dialog.accept();
  });
  await page.getByRole('button', { name: '删除', exact: true }).click();
  const preview = await fixtures.waitFor(path, A);
  expect(new URL(preview.call.request.url()).searchParams.get('dry_run')).toBe('true');
  await chooseKb(page, B);
  await fixtures.release(preview, { data: { doc_id: 'alpha-report.txt-id', dry_run: true, candidate_file: { exists: true } } });
  expect(confirmations).toBe(0);
  expect(fixtures.count(path)).toBe(1);
  await expect(page.getByText('beta-report.txt', { exact: true })).toBeVisible();
});

test('旧深度调研：切库后迟到报告不得恢复引用或图谱', async ({ page, fixtures }) => {
  fixtures.hold('/api/docqa/deep-research');
  await openWorkspace(page);
  await page.getByPlaceholder(INPUT).fill('alpha research');
  await page.getByRole('button', { name: '深度调研', exact: true }).click();
  const research = await fixtures.waitFor('/api/docqa/deep-research', A);
  expect(research.call.request.postDataJSON()).toMatchObject({ kb_id: A, question: 'alpha research' });
  await chooseKb(page, B);
  await fixtures.release(research, { data: { report: 'stale research report', summary: 'stale research summary', final_conclusion: 'stale conclusion', citations: qaAnswer('').citations, sub_questions: [] } });
  await expect(page.getByText('stale research report', { exact: true })).toHaveCount(0);
  expect(fixtures.count('/api/query')).toBe(0);
  await expect(page.getByRole('tab', { name: '引用证据' })).toHaveAttribute('aria-selected', 'true');
});

test('过期空图结果：不得继续 overview fallback 或以新 KB 发查询', async ({ page, fixtures }) => {
  fixtures.hold('/api/query');
  await openWorkspace(page);
  await ask(page, 'alpha empty graph');
  const oldGraph = await fixtures.waitFor('/api/query', A);
  const before = fixtures.count('/api/query');
  await chooseKb(page, B);
  await fixtures.release(oldGraph, { data: { nodes: [], edges: [], stats: { nodeCount: 0, edgeCount: 0, executionTime: 0 } } });
  expect(fixtures.count('/api/query')).toBe(before);
  expect(fixtures.count('/api/query', B)).toBe(0);
  await expect(page.getByRole('tab', { name: '引用证据' })).toHaveAttribute('aria-selected', 'true');
});
