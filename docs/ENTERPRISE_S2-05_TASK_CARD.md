# S2-05任务卡：工作台展示真实文档处理状态

> 状态：DESIGNED / 待开发
>任务类型：前端状态消费与展示
>负责人：开发工程师
> 审核人：独立测试/项目审核角色
> 创建日期：2026-10-15
> 当前分支基线：`codex/m5-seed-v2-only`

##1.任务目标

在当前知识库工作台的文档面板中，基于现有文档列表 API 返回的 `status`、`graph_status`、`vector_status` 和 `version`，展示文档、图谱、向量三个层面的真实处理状态。

本任务要消除“上传成功即已入库”的误导。文件上传完成、文档解析完成、图谱投影完成、向量索引完成是不同状态，UI 必须分别表达，不能用单一绿色标签覆盖所有情况。

##2. 背景与现状证据

当前后端文档列表已返回状态字段：

- `go-backend/internal/httpserver/documents_native.go`：返回 `status`、`graph_status`、`vector_status`、`version`、`relative_path`。
- `backend/services/chunk_projection_state.py`：已有文档级图谱/向量状态聚合逻辑。
- `backend/services/chunk_projection_reindex.py`、`backend/services/chunk_revision_lifecycle.py`：已有投影状态与 revision 生命周期实现。
- `frontend/src/services/documents.ts`：`DocumentItem` 已声明部分状态字段，但状态枚举未收紧，列表函数返回值未显式建模分页合同。
- `frontend/src/components/Workspace/DocumentPanel.tsx`：当前文档卡片固定展示“已入库”，没有消费上述状态字段。
- `frontend/src/pages/Admin/KnowledgeBaseDetailPage.tsx`：已经有一套局部状态标签，可作为语义参考，但本卡不重做后台详情页。

现有作用域和异步切库保护必须保持：请求继续携带 `X-KB-ID`，A→B 切换时旧 A 响应不能覆盖 B。

##3.交付范围

###3.1 必须完成

1. 为文档状态建立前端安全映射，支持已知状态、未知状态、`null` 和字段缺失。
2. 文档卡片分别显示：
 - 文档处理状态 `status`
 - 图谱投影状态 `graph_status`
 - 向量投影状态 `vector_status`
3. 明确区分 `pending`、`stale`、`indexed`、`skipped`、`failed`。
4.失败、过期、未知状态不得显示为成功。
5. `skipped` 不得显示为“已索引”；应表达为“未启用”或等价文案。
6. 保持现有 loading、empty、error、success 状态。
7. 保持现有知识库作用域、删除行为和异步响应保护。
8. 增加纯函数测试和前端隔离回归测试。

###3.2 明确不做

- 不新增后端 API、数据库字段或迁移。
- 不修改 Python 状态机、解析器、worker、Neo4j、Milvus 或任务中心。
- 不自动重试、不自动触发重建、不新增任务轮询。
- 不实现 Chunk 编辑、rollback 或 QA trace版本展示。
- 不新增 KB详情统计，不重做 KB目录页。
- 不改变上传、删除、建图和问答请求协议。
- 不连接共享 dev、生产、真实模型或真实 Milvus/Neo4j 做本卡验收。

##4. 状态契约

###4.1 文档处理状态

后端合同中的已知值：

| 原始值 | UI 文案 | 建议语义颜色 |说明 |
| --- | --- | --- | --- |
| `uploaded` | 已上传，等待处理 | info | 文件已登记，尚未完成解析/处理 |
| `parsing` |解析中 | warning | 正在解析或处理 |
| `indexed` | 文档处理完成 | success | 文档级处理已完成；仍须单独查看图谱/向量 |
| `failed` |处理失败 | error | 文档级处理失败 |
| `archived` | 已归档 | default | 不再作为当前有效文档 |
| 缺失、`null`、未知值 |处理状态未知 | warning/error | 不得推断为成功 |

###4.2 图谱/向量投影状态

| 原始值 | UI 文案 | 建议语义颜色 |说明 |
| --- | --- | --- | --- |
| `pending` | 待索引 | warning | 尚未完成投影 |
| `stale` |需要重建 | warning | 当前投影落后于内容版本 |
| `indexed` | 已索引 | success | 当前投影完成 |
| `skipped` | 未启用 | default | 能力未配置；不等于成功 |
| `failed` | 索引失败 | error | 投影失败 |
| 缺失、`null`、未知值 | 状态未知 | warning/error | 不得推断为已索引 |

>具体组件使用项目现有 MUI语义色和主题，不要在组件内散落颜色字面量。若项目已有统一 Badge/Chip 封装，优先复用。

###4.3 总体展示状态

本卡不新增后端 `overall_status` 字段。若需要辅助摘要，只能在前端由纯函数派生，且不能掩盖三个原始状态。

推荐优先级：

1. 文档状态或任一投影状态为 `failed` → “处理失败”
2. 任一投影状态为 `stale` → “需要重建”
3. 文档状态为 `parsing` 或任一投影状态为 `pending` → “处理中”
4. 任一值未知 → “状态未知”
5. 所有已执行能力均为 `indexed`，且文档状态为 `indexed` → “已完成”
6.只有 `skipped` 的能力不得使总体状态变成“已完成”；应保留“未启用/部分完成”等准确表达

如果总体摘要会造成语义歧义，可以只展示三个独立状态，不强制增加总体 Chip。

##5. 推荐实现设计

###5.1 新增纯函数模块

建议新增：

- `frontend/src/utils/documentStatus.ts`
- `frontend/src/utils/documentStatus.test.ts`（按现有测试工具和项目配置选择测试位置；若项目不支持单元测试 runner，则将等价断言放入 Playwright/纯 TS 可执行测试）

建议导出小而明确的函数：

```ts
export type DocumentProcessingStatus = 'uploaded' | 'parsing' | 'indexed' | 'failed' | 'archived';
export type ProjectionStatus = 'pending' | 'stale' | 'indexed' | 'skipped' | 'failed';

export function getDocumentStatusView(value: unknown): StatusView;
export function getProjectionStatusView(value: unknown): StatusView;
export function getDocumentOverallStatus(input: DocumentStatusInput): StatusView;
```

`StatusView` 至少包含：稳定 key、用户可读 label、MUI severity、是否 unknown。函数必须对 `undefined`、`null`、空字符串和未来未知值 fail-safe。

不要把 API 请求、React state、主题对象或副作用放进该模块。

###5.2 修改文档模型

在 `frontend/src/services/documents.ts`：

- 保留后端兼容性，允许未知值进入映射函数。
- 如收紧类型，使用已知值联合类型与 `string`/`unknown` 边界，不得通过类型断言把未知服务器值伪装成成功。
- 不改变请求路径和 `X-KB-ID`发送方式。

###5.3 修改工作台文档卡片

在 `frontend/src/components/Workspace/DocumentPanel.tsx`：

- 删除固定的“已入库”成功 Chip。
- 使用状态映射函数渲染文档、图谱、向量状态。
- 保持文件名、扩展名、大小、删除按钮和引用高亮行为。
- 窄屏下标签必须换行，不能造成页面水平溢出。
- 标签文字应可读且不依赖颜色 alone传达状态。
- 不重构切库 store 或请求生命周期。

可选 UI 文案结构：

```text
文件名.pdf
PDF ·120 KB
文档：解析中 图谱：待索引 向量：待索引
```

##6. 验收标准（AC）

### AC-01 正常完成

输入：`status=indexed`、`graph_status=indexed`、`vector_status=indexed`。

预期：三个状态分别显示“文档处理完成”“已索引”“已索引”，使用成功语义样式。

### AC-02解析中

输入：`status=parsing`、`graph_status=pending`、`vector_status=pending`。

预期：显示“解析中”“待索引”“待索引”；不得出现“已入库”或整体成功标签。

### AC-03 部分完成

输入：`status=indexed`、`graph_status=indexed`、`vector_status=pending`。

预期：文档和图谱成功，向量仍为“待索引”；不得显示全部完成。

### AC-04失败优先

输入：`status=failed`、`graph_status=failed`、`vector_status=indexed`。

预期：文档和图谱显示失败；向量保持已索引；不得用绿色总体成功掩盖失败。

### AC-05 stale

输入：`status=indexed`、`graph_status=stale`、`vector_status=indexed`。

预期：图谱显示“需要重建”，不能显示“已索引”。

### AC-06 skipped

输入：`status=indexed`、`graph_status=skipped`、`vector_status=skipped`。

预期：图谱和向量显示“未启用”；不能把 skipped 当成 indexed。

### AC-07 未知值和旧响应

输入：未知字符串、`null` 或字段缺失。

预期：显示“状态未知”；页面不崩溃；不能默认显示成功。

### AC-08 loading / empty / error 不回归

预期：文档列表加载中、空列表和 API 错误仍显示原有正确状态；状态映射异常不得吞掉列表错误。

### AC-09作用域隔离

在 KB-A → KB-B 切换期间延迟释放 A 的文档响应：

- B只显示 B 的文档和状态；
- A 的文档、状态、错误和 loading 不覆盖 B；
- 请求仍带正确的 `X-KB-ID`；
-既有 A→B→A 和同库乱序保护继续通过。

### AC-10 可访问性和响应式

- 状态不能只靠颜色表达，文本必须包含状态含义；
- 键盘可访问；
-约400px 宽度下无页面水平滚动；
- 长文件名和长状态文案不破坏卡片布局。

##7. 测试计划

###7.1 开发者本地验证

```bash
cd frontend
npm run lint
npm run build
```

如项目已有单元测试入口，执行对应定向测试；否则对纯函数增加可执行的 Playwright/Node 测试，不引入新的测试框架依赖。

###7.2 独立前端隔离测试

沿用 `frontend/tests/e2e/workspace-kb-async.spec.ts` 的请求夹具和网络边界，不连接真实 Go/Python/Neo4j/Milvus。

至少增加/覆盖：

- indexed / indexed / indexed
- parsing / pending / pending
- indexed / indexed / pending
- failed / failed / indexed
- indexed / stale / indexed
- indexed / skipped / skipped
- unknown / null / 缺失字段
- A→B 延迟响应不串状态
- loading / empty / error

定向命令示例：

```bash
cd frontend
npm run e2e -- workspace-kb-async.spec.ts
```

实际运行应使用项目要求的 Node `22.22.2`，并记录浏览器、通过数、失败数、跳过数和 exit code。

###7.3证据口径

测试记录必须明确：

-这是前端隔离验证；
- API 响应来自受控 fixture；
- 没有连接真实 Go/Python/Neo4j/Milvus/模型；
- 没有写共享 dev 或生产；
-该测试不能证明真实索引链路或生产问答可用。

##8. 文件范围与禁止扩散

预期改动范围：

- `frontend/src/components/Workspace/DocumentPanel.tsx`
- `frontend/src/services/documents.ts`
- `frontend/src/utils/documentStatus.ts`
- 对应测试文件
- 本任务验收记录和必要的 Backlog 状态行

未经重新拆卡和审核，不得修改：

- 后端 API、数据库迁移、Python 状态聚合
- Neo4j/Milvus 写入与检索
-任务 worker、重试和轮询
- `graphStore` 切库逻辑
- QA trace、Chunk revision、M5-B1
- CI、共享配置和部署脚本

##9. 完成定义（DoD）

- [ ] 每个 AC 都有可复核测试证据。
- [ ] 未知、缺失、`failed`、`stale`、`skipped` 均不被误报为成功。
- [ ] 文档、图谱、向量状态分别展示。
- [ ] loading / empty / error / success 状态不回归。
- [ ] A→B 切库和迟到响应保护不回归。
- [ ] Node `22.22.2` 下 lint、build、定向 E2E通过。
- [ ] 有开发记录、独立测试记录和审核结论。
- [ ] Backlog 与 P0 缺口文档更新为事实状态。
- [ ] 未修改后端/数据库/共享环境，未宣称生产闭环。
- [ ] 使用单一意图 Conventional Commit；不 push，除非另行授权。

##10.交付给开发工程师时的执行指令

请只执行 `S2-05`，先阅读：

1. `AGENTS.md`
2. 本任务卡
3. `frontend/src/components/Workspace/DocumentPanel.tsx`
4. `frontend/src/services/documents.ts`
5. `frontend/tests/e2e/workspace-kb-async.spec.ts`
6. `docs/KNOWLEDGE_BASE_P0_CONTRACT_AND_GAP_AUDIT.md` 中 S2-01/S2-02 状态合同

执行顺序：

1.先确认现有状态字段和组件测试夹具；
2.只实现前端状态映射与展示；
3.运行 lint、build 和定向隔离 E2E；
4. 保存原始命令输出和 exit code；
5. 创建开发记录，列出实际修改文件；
6. 不自行扩大到任务轮询、重试、后端状态机或 M5-B1；
7. 完成后停止，不宣布生产验收，由独立测试和项目审核角色继续。
