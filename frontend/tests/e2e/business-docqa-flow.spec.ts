import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const adminEmail = process.env.E2E_ADMIN_EMAIL || process.env.ADMIN_EMAIL || 'yh@qs.al';
const adminPassword = process.env.E2E_ADMIN_PASSWORD || process.env.ADMIN_PASSWORD || '';
const adminToken = process.env.E2E_ADMIN_TOKEN || process.env.ADMIN_TOKEN || '';
const backendApiBaseUrl = (
  process.env.E2E_API_BASE_URL ||
  process.env.ADMIN_BASE_URL ||
  'http://127.0.0.1:8081'
).replace(/\/+$/, '');
const browserApiBaseEnv = (process.env.VITE_API_BASE_URL || '').trim();

type LoginResponse = {
  code: number;
  data?: {
    token?: string;
  };
};

type TraceListResponse = {
  code: number;
  data?: {
    items?: Array<{
      id: number;
      trace_id?: string | null;
      qa_type?: string;
      status?: string;
      question?: string;
    }>;
  };
};

type TraceDetailResponse = {
  code: number;
  data?: {
    trace_id?: string | null;
    qa_type?: string;
    status?: string;
    question?: string;
    citation_count?: number;
    answer_preview?: string | null;
  };
};

type TraceLookupResult = {
  id: number;
  traceId: string;
};

type GraphBuildSubmitResponse = {
  code: number;
  data?: {
    job_id?: number;
    status?: string;
    message?: string;
  };
};

type JobDetailResponse = {
  code: number;
  data?: {
    id: number;
    status?: string;
    error_message?: string | null;
    trace_id?: string | null;
    result?: Record<string, unknown>;
  };
};

type DocumentListResponse = {
  code: number;
  data?: {
    items?: Array<{
      id: string;
      name: string;
    }>;
  };
};

type KnowledgeBaseCatalogResponse = {
  code: number;
  data?: {
    items?: Array<{
      kb_id: string;
      name: string;
      status: string;
    }>;
  };
};

type DeletedDocumentListResponse = {
  code: number;
  data?: {
    items?: Array<{
      doc_id: string;
      name: string;
    }>;
  };
};

type RestoreDocumentResponse = {
  code: number;
  data?: {
    doc_id?: string;
    restored_name?: string;
  };
};

async function getAdminBearer(request: APIRequestContext) {
  if (adminToken) {
    return adminToken;
  }

  test.skip(!adminPassword, '业务链路测试需要 ADMIN_TOKEN 或 ADMIN_PASSWORD');

  const response = await request.post(`${backendApiBaseUrl}/api/v1/admin/auth/login`, {
    data: {
      username: adminEmail,
      password: adminPassword,
    },
  });

  if (!response.ok()) {
    throw new Error(
      `admin login failed via request context: status=${response.status()} body=${await response.text()} baseUrl=${backendApiBaseUrl}`
    );
  }
  const body = (await response.json()) as LoginResponse;
  expect(body.code).toBe(200);
  expect(body.data?.token).toBeTruthy();
  return body.data!.token!;
}

// 首页偏好由服务端权威存储：客户端会话校验时会用 /auth/me 的 preferred_home_path
// 覆盖 localStorage，因此根路由跳转测试必须通过 profile API 改服务端值，
// 而不是只写 localStorage（后者会在冷加载时被同步冲掉）。
async function setPreferredHome(
  request: APIRequestContext,
  bearer: string,
  path: '/admin/dashboard' | '/workspace'
) {
  const response = await request.put(`${backendApiBaseUrl}/api/v1/admin/profile`, {
    headers: { Authorization: `Bearer ${bearer}` },
    data: { preferred_home_path: path },
  });
  expect(response.ok()).toBeTruthy();
}

async function expectGoOwnedBackend(request: APIRequestContext) {
  const response = await request.get(`${backendApiBaseUrl}/health`);
  expect(response.ok()).toBeTruthy();
  expect(response.headers()['x-graphinsight-route-owner']).toBe('go-native');
  const body = await response.json();
  expect(body.code).toBe(200);
  expect(body.data?.neo4j?.connected).toBe(true);
  expect(body.data?.python_backend?.connected).toBe(true);
  expect(body.data?.orchestrator?.connected).toBe(true);
}

// 解析一个当前用户可访问的 active KB（业务面 GET /api/knowledge-bases）。
// M4-R1 步骤 3：业务面调用（文档列表/上传/建图/问答）都必须带 kb 作用域，
// E2E 进入 workspace 前先确定测试 kb_id，同时用于浏览器端注入与 request 上下文头。
async function resolveTestKbId(request: APIRequestContext, bearer: string): Promise<string> {
  const response = await request.get(`${backendApiBaseUrl}/api/knowledge-bases`, {
    headers: { Authorization: `Bearer ${bearer}` },
  });
  expect(response.ok()).toBeTruthy();
  expect(response.headers()['x-graphinsight-route-owner']).toBe('go-native');
  const body = (await response.json()) as KnowledgeBaseCatalogResponse;
  expect(body.code).toBe(200);
  const firstActive = (body.data?.items || []).find((item) => item.status === 'active');
  expect(
    firstActive?.kb_id,
    '业务面 KB 目录未返回可访问的 active 知识库，无法进行带作用域的 E2E 流程'
  ).toBeTruthy();
  return firstActive!.kb_id;
}

function kbScopedHeaders(bearer: string, kbId: string): Record<string, string> {
  return {
    Authorization: `Bearer ${bearer}`,
    'X-KB-ID': kbId,
  };
}

async function findDocumentIdByName(
  request: APIRequestContext,
  bearer: string,
  kbId: string,
  fileName: string
) {
  const response = await request.get(`${backendApiBaseUrl}/api/documents`, {
    headers: kbScopedHeaders(bearer, kbId),
  });
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as DocumentListResponse;
  expect(body.code).toBe(200);
  const matched = (body.data?.items || []).find((item) => item.name === fileName);
  return matched?.id || null;
}

async function deleteActiveDocumentIfPresent(
  request: APIRequestContext,
  bearer: string,
  kbId: string,
  fileName: string,
  options?: { softDelete?: boolean }
) {
  const docId = await findDocumentIdByName(request, bearer, kbId, fileName);
  if (!docId) return false;

  const response = await request.delete(`${backendApiBaseUrl}/api/documents/${encodeURIComponent(docId)}`, {
    headers: kbScopedHeaders(bearer, kbId),
    params: {
      purge_graph: 'true',
      soft_delete: String(options?.softDelete ?? false),
      dry_run: 'false',
      verify_after: 'true',
    },
  });
  expect(response.ok()).toBeTruthy();
  return true;
}

async function findDeletedDocumentIdByName(
  request: APIRequestContext,
  bearer: string,
  kbId: string,
  fileName: string
) {
  const response = await request.get(`${backendApiBaseUrl}/api/documents/deleted`, {
    headers: kbScopedHeaders(bearer, kbId),
  });
  expect(response.ok()).toBeTruthy();
  const body = (await response.json()) as DeletedDocumentListResponse;
  expect(body.code).toBe(200);
  const matched = (body.data?.items || []).find((item) => item.name === fileName);
  return matched?.doc_id || null;
}

async function cleanupSoftDeletedDocument(
  request: APIRequestContext,
  bearer: string,
  kbId: string,
  fileName: string
) {
  const deletedDocId = await findDeletedDocumentIdByName(request, bearer, kbId, fileName);
  if (!deletedDocId) return false;

  const restoreResponse = await request.post(`${backendApiBaseUrl}/api/documents/${encodeURIComponent(deletedDocId)}/restore`, {
    headers: kbScopedHeaders(bearer, kbId),
    params: {
      verify_after: 'false',
    },
  });
  expect(restoreResponse.ok()).toBeTruthy();
  const restored = (await restoreResponse.json()) as RestoreDocumentResponse;
  expect(restored.code).toBe(200);
  const restoredName = restored.data?.restored_name || fileName;
  await deleteActiveDocumentIfPresent(request, bearer, kbId, restoredName, { softDelete: false });
  return true;
}

async function waitForTraceByKeyword(
  request: APIRequestContext,
  bearer: string,
  kbId: string,
  keyword: string
): Promise<TraceLookupResult> {
  const deadline = Date.now() + 120_000;
  let lastBody: TraceListResponse | null = null;

  while (Date.now() < deadline) {
    const response = await request.get(`${backendApiBaseUrl}/api/v1/admin/qa-traces`, {
      headers: {
        Authorization: `Bearer ${bearer}`,
      },
      params: {
        // M4-R1 审计 P1：qa-trace list 按 KB 作用域 fail-closed，必须带 kb_id。
        kb_id: kbId,
        keyword,
        page: '1',
        page_size: '10',
      },
    });
    expect(response.ok()).toBeTruthy();
    lastBody = (await response.json()) as TraceListResponse;
    const items = lastBody.data?.items || [];
    const matched = items.find((item) => item.question?.includes(keyword));
    if (matched?.trace_id) {
      return {
        id: matched.id,
        traceId: matched.trace_id,
      };
    }
    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }

  throw new Error(`QA trace not found by keyword=${keyword}; lastBody=${JSON.stringify(lastBody)}`);
}

async function waitForJobTerminalState(request: APIRequestContext, bearer: string, kbId: string, jobId: number) {
  const deadline = Date.now() + 240_000;
  let lastBody: JobDetailResponse | null = null;

  while (Date.now() < deadline) {
    const response = await request.get(`${backendApiBaseUrl}/api/v1/admin/jobs/${jobId}`, {
      headers: {
        Authorization: `Bearer ${bearer}`,
      },
      // M4-R1 审计 P1：job detail 按 KB 作用域 fail-closed，必须带 kb_id。
      params: { kb_id: kbId },
    });
    expect(response.ok()).toBeTruthy();
    lastBody = (await response.json()) as JobDetailResponse;
    expect(lastBody.code).toBe(200);

    const status = lastBody.data?.status;
    if (status === 'succeeded' || status === 'failed' || status === 'cancelled') {
      return lastBody.data;
    }

    await new Promise((resolve) => setTimeout(resolve, 2_000));
  }

  throw new Error(`job ${jobId} did not reach terminal state; lastBody=${JSON.stringify(lastBody)}`);
}

async function openHome(page: Page) {
  // 工作台新版布局：主区 tab 为「引用证据/关系图谱」（MainWorkspace），
  // 上传与一键建图在问答面板底部（DocChatPanel），回收站已移到后台治理页。
  await page.goto('/');
  await expect(page.getByRole('tab', { name: '引用证据' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '关系图谱' })).toBeVisible();
  await expect(page.getByRole('heading', { name: '引用证据' })).toBeVisible();
  await expect(page.getByRole('button', { name: /^(上传|上传中)$/ })).toBeVisible();
}

async function resolveBrowserApiBaseUrl(page: Page) {
  const normalizedEnv = browserApiBaseEnv.replace(/\/+$/, '');
  if (normalizedEnv && normalizedEnv !== 'auto' && normalizedEnv !== 'same-origin') {
    return normalizedEnv.endsWith('/api') ? normalizedEnv.slice(0, -4) : normalizedEnv;
  }

  return new URL(page.url()).origin.replace(/\/+$/, '');
}

function documentActionCard(page: Page, fileName: string, actionName: '删除' | '恢复') {
  // 注意：XPath ancestor 轴是「由近到远」文档序，[1] 会选中最远的 html 根节点，
  // 必须用 [last()] 取最近一层包含动作按钮的祖先（即文档卡片容器）。
  return page
    .getByRole('heading', { name: fileName })
    .first()
    .locator(`xpath=ancestor::*[button[normalize-space()="${actionName}"]][last()]`);
}

test.describe('Business DocQA Flow', () => {
  test('upload, build, ask, trace, delete', async ({ page, request }) => {
    // 慢模型（推理型 QA）下：上传~10s + 建图~210s + 问答~10s + 删除清理~30s，留余量到 15 分钟。
    test.setTimeout(900_000);

    const bearer = await getAdminBearer(request);
    await expectGoOwnedBackend(request);
    // M4-R1 步骤 3：业务面调用必须带 kb 作用域。先解析一个可访问的 active KB，
    // 同时用于浏览器端注入 activeKbId（供 api 拦截器/选择器）与 request 上下文头。
    const kbId = await resolveTestKbId(request, bearer);

    const uniqueId = `e2e-${Date.now()}`;
    const fileName = `codex-docqa-${uniqueId}.txt`;
    const traceKeyword = `TRACE-${uniqueId}`;
    const question = `这份文档主要用于验证什么？请忽略标记 ${traceKeyword}。`;
    let softDeletedByUi = false;

    try {
      await page.addInitScript(
        (init: { token: string; kbId: string }) => {
          window.localStorage.setItem('admin_token', init.token);
          // M4-R1 审计 P1-3：显式预置 admin_home_path=/workspace，使根路由 `/`
          // 的跳转确定性地进入 workspace，不依赖数据库里管理员既有的首页偏好，
          // 换干净环境也不会跳到 dashboard 导致选择器断言失败。
          window.localStorage.setItem('admin_home_path', '/workspace');
          // 预置 zustand persist store 的 activeKbId，使业务调用（上传/建图/问答）带作用域。
          window.localStorage.setItem(
            'graph-insight-storage',
            JSON.stringify({ state: { activeKbId: init.kbId }, version: 0 })
          );
        },
        { token: bearer, kbId }
      );

      // 首页偏好服务端权威：显式将偏好切到 /workspace，确保 `openHome` 的 goto('/') 进入工作台。
      await setPreferredHome(request, bearer, '/workspace');

      await openHome(page);

      const fileInput = page.locator('input[type="file"]').first();
      await fileInput.setInputFiles({
        name: fileName,
        mimeType: 'text/plain',
        buffer: Buffer.from(
          [
            'GraphInsight business flow smoke document.',
            // 正文必须每次唯一：后端按内容哈希去重（ErrDocumentDuplicate → skipped），
            // 固定文本在上一轮清理不完整时会让后续轮次被『相同内容已存在』跳过。
            `Run marker: ${uniqueId} / ${traceKeyword}.`,
            'Purpose: verify upload, build graph, docqa trace, and soft delete flow.',
            'This document is created by Playwright E2E for the local environment.',
            'Question expectation: the answer should mention verification or smoke flow.',
            'Current QA model target: qwen-flash.',
          ].join('\n'),
          'utf-8'
        ),
      });

      await expect(page.getByText(/上传成功\s+\d+\s+·\s+跳过\s+\d+/)).toBeVisible({ timeout: 120_000 });
      await expect(page.getByText(fileName)).toBeVisible({ timeout: 30_000 });
      expect(await findDocumentIdByName(request, bearer, kbId, fileName)).toBeTruthy();

      const browserApiBaseUrl = await resolveBrowserApiBaseUrl(page);
      const buildButton = page.getByRole('button', { name: '一键建图' });
      const buildResponsePromise = page.waitForResponse((response) => {
        return response.request().method() === 'POST' && response.url() === `${browserApiBaseUrl}/api/graph/build`;
      });
      await buildButton.click();
      const buildResponse = await buildResponsePromise;
      expect(buildResponse.ok()).toBeTruthy();
      const buildPayload = (await buildResponse.json()) as GraphBuildSubmitResponse;
      expect(buildPayload.code).toBe(200);
      const buildJobId = buildPayload.data?.job_id;
      expect(buildJobId).toBeTruthy();
      await expect(page.getByText(new RegExp(`建图任务\\s*#${buildJobId}\\s*已提交`))).toBeVisible({ timeout: 30_000 });
      const buildJob = await waitForJobTerminalState(request, bearer, kbId, buildJobId!);
      expect(buildJob?.status).toBe('succeeded');
      expect(buildJob?.error_message || '').toBe('');

      const qaInput = page.getByPlaceholder('输入问题，Enter 发送，Shift+Enter 换行');
      await qaInput.fill(question);
      await qaInput.press('Enter');

      await expect(page.getByText(question)).toBeVisible();
      // 新版答案下方是「引用 N 条 · 查看证据」按钮（DocChatPanel），不再是「引用摘要（N）」文本。
      await expect(page.getByRole('button', { name: /引用 \d+ 条 · 查看证据/ })).toBeVisible({ timeout: 180_000 });
      await expect(
        page.getByText(/验证|上传|建图|问答|链路|smoke/i).first()
      ).toBeVisible({ timeout: 180_000 });

      const trace = await waitForTraceByKeyword(request, bearer, kbId, traceKeyword);
      const traceDetailResponse = await request.get(`${backendApiBaseUrl}/api/v1/admin/qa-traces/${trace.id}`, {
        headers: {
          Authorization: `Bearer ${bearer}`,
        },
        // M4-R1 审计 P1：trace detail 按 KB 作用域 fail-closed，必须带 kb_id。
        params: { kb_id: kbId },
      });
      expect(traceDetailResponse.ok()).toBeTruthy();
      expect(traceDetailResponse.headers()['x-graphinsight-route-owner']).toBe('go-native');
      const traceDetail = (await traceDetailResponse.json()) as TraceDetailResponse;
      expect(traceDetail.code).toBe(200);
      expect(traceDetail.data?.trace_id).toBe(trace.traceId);
      expect(traceDetail.data?.qa_type).toBe('docqa');
      expect(traceDetail.data?.status).toBe('success');
      expect(traceDetail.data?.question).toContain(traceKeyword);
      expect((traceDetail.data?.citation_count || 0) >= 1).toBeTruthy();

      // 「查看证据」会把主区切到「关系图谱」tab（applyCitationSelection 联动），
      // 图谱画布盖住引用面板导致点击拦截，删除前必须先切回「引用证据」tab。
      await page.getByRole('tab', { name: '引用证据' }).click();
      // 删除入口在「引用证据」面板的文档卡片上（DocumentPanel），点击后 confirm 对话框。
      // 对话框监听必须在点击前常驻注册：dry-run 预览与真实删除各弹一次 confirm，
      // once() 只接第一次会把第二次 confirm 挂死在页面上拖满整个测试超时。
      page.on('dialog', (dialog) => dialog.accept());
      const activeCard = documentActionCard(page, fileName, '删除');
      const delButton = activeCard.getByRole('button', { name: '删除' });
      await delButton.click({ timeout: 30_000 });

      await expect(page.getByText(`已移入回收站 ${fileName}`, { exact: false })).toBeVisible({ timeout: 120_000 });
      softDeletedByUi = true;
      // 新版工作台不再内嵌回收站列表（恢复入口移到 /admin/knowledge-base），
      // 只断言活动列表已移除；恢复+硬清理走 finally 里的 API cleanup。
      await expect(documentActionCard(page, fileName, '删除')).toHaveCount(0);
      expect(await findDocumentIdByName(request, bearer, kbId, fileName)).toBeNull();
    } finally {
      if (!softDeletedByUi) {
        await deleteActiveDocumentIfPresent(request, bearer, kbId, fileName, { softDelete: false });
      } else {
        await cleanupSoftDeletedDocument(request, bearer, kbId, fileName);
      }
    }
  });

  // M4-R1 审计 P1-3：独立验证根路由 `/` 按服务端存储的首页偏好跳转。
  // 偏好由 /auth/me 权威下发并在会话校验时同步到 localStorage，故必须通过 profile API 切换。
  test('root route "/" redirects by stored admin home preference', async ({ page, request }) => {
    const bearer = await getAdminBearer(request);
    await page.addInitScript((token) => {
      window.localStorage.setItem('admin_token', token);
    }, bearer);

    // 服务端偏好为 dashboard 时，`/` 跳转到管理台 dashboard。
    await setPreferredHome(request, bearer, '/admin/dashboard');
    await page.goto('/');
    await expect(page).toHaveURL(/\/admin\/dashboard(\?|#|$)/);

    // 切换服务端偏好为 /workspace 后，冷加载 `/` 应跳转到图谱工作台。
    await setPreferredHome(request, bearer, '/workspace');
    await page.goto('/');
    await expect(page).toHaveURL(/\/workspace(\?|#|$)/);

    // 复原默认偏好，避免影响其它用例或真实使用。
    await setPreferredHome(request, bearer, '/admin/dashboard');
  });
});
