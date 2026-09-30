# soak / capacity 观察记录（2026-09-30，Release Gate R1 项4）

## §1 阈值与参数声明（执行前写定，事后不调整）

声明时间：2026-09-30 10:52（在跑第一轮探针之前落档；若结果不达标，按"不达标"记录，不回改本节的阈值）。

| 项 | 取值 | 口径来源 |
|---|---|---|
| preset | `release` | 开工令 `perf_probe_preset=release` |
| 用例集合 | `health`、`query`、`docqa-health`、`nl2cypher-status`、`docqa`、`graph-build` | `run_perf_probe.py` 的 `RELEASE_CASES` |
| rounds | 3 | 本轮 soak 定义（同一参数重复 3 轮看趋势与稳定性） |
| requests / 轮 / 用例 | 20 | 开工令 `perf_probe_requests=20` |
| concurrency | 4 | 开工令 `perf_probe_concurrency=4` |
| `max_error_rate` | **0.0** | 与发布验收同一口径，**不放宽** |
| `max_p95_ms` | **0（关闭）** | 沿用发布验收既有口径：本轮**不引入新的延迟门禁**，p95 只作趋势观测与记录 |

明确两点，避免被读成"阈值放水"或"阈值加码"：

1. 错误率阈值保持 0 容忍，与 `run_release_acceptance.sh` 默认一致。
2. p95 未设硬阈值是**既有事实**（`--max-p95-ms 0` 即"0 disables"），本轮只声明沿用它；
   若要给 p95 上门禁，必须另立口径并说明依据，不能在一次 soak 结果出来后反填。
3. `graph-build` 是写副作用用例：探针提交任务后立即 `:cancel`（`run_perf_probe.py` 已有该收口），
   避免污染 worker 队列。

## §2 被测栈与前置核验

| 项 | 值 |
|---|---|
| 入口 | `http://127.0.0.1:18090`（容器 `gi-freshgw-e2e`，Cmd=`/src/bin/api-linux-66e1d18`） |
| Go 代码等价性 | `git diff 66e1d18..HEAD -- go-backend` 为空 → 该二进制即当前 HEAD 的 Go 面 |
| Python 能力层 | 宿主 `127.0.0.1:8001`，`/health` = 200 |
| 元数据库 | 容器 `gi-freshpg-e2e`（独立空库初始化，11 表） |
| 认证 | 管理账号真实登录取 token（口令只经环境变量传入，不写 argv/文档/日志） |
| KB 作用域 | 探针自行调用 `GET /api/knowledge-bases` 发现 active KB 并注入 `x-kb-id`，无手工 SQL |

## §3 结果（执行后回填）

执行命令（口令只经 `ADMIN_PASSWORD` 环境变量传入，未落 argv 之外的任何记录）：

```bash
ADMIN_PASSWORD=*** python -X utf8 backend/tests/run_perf_soak.py \
  --base-url http://127.0.0.1:18090 --admin-email <admin> \
  --preset release --rounds 3 --requests 20 --concurrency 4 \
  --max-error-rate 0.0 --max-p95-ms 0 --sleep-seconds 5 \
  --output-dir artifacts/perf-soak/2026-09-30-release
```

产物：`artifacts/perf-soak/2026-09-30-release/round-{1,2,3}.{json,md}` 与 `summary.json`（该目录已被
`.gitignore` 忽略，属可再生运行态）。探针自行发现 active KB 并注入 `x-kb-id`（`source=discover`），
未使用手工 SQL。

上表数值已逐个与 `summary.json` 复核一致（非按记忆誊写）：`requests_per_case=20`、`concurrency=4`、
`thresholds={max_error_rate: 0.0, max_p95_ms: 0.0}`、`route_owner_check=true`（即路由归属断言在跑，
没有被跳过）、总请求 360 / 失败 0。

`SOAK_SUMMARY rounds=3 failed_rounds=0`，逐轮逐用例（每格 total=20、failed=0、error_rate=0.00%）：

| 用例 | 路由 Owner | 轮1 p50 / p95 / max | 轮2 p50 / p95 / max | 轮3 p50 / p95 / max |
|---|---|---|---|---|
| `health` | go-native | 4.2 / 31.3 / 31.7 ms | 14.9 / 27.1 / 28.8 ms | 4.3 / 26.8 / 26.9 ms |
| `query` | go-native | 19.8 / 42.1 / 47.0 ms | 9.7 / 28.2 / 30.7 ms | 10.7 / 29.1 / 34.3 ms |
| `docqa-health` | go-orchestrator | 349.7 / 400.5 / 401.9 ms | 270.5 / 275.5 / 275.7 ms | 277.4 / 292.0 / 292.1 ms |
| `nl2cypher-status` | go-native | 7.1 / 20.0 / 26.3 ms | 6.5 / 29.1 / 29.9 ms | 5.8 / 29.4 / 29.5 ms |
| `docqa` | go-orchestrator | 85.2 / 310.1 / 334.2 ms | 69.4 / 84.6 / 103.2 ms | 68.4 / 90.0 / 92.4 ms |
| `graph-build` | go-native | 45.4 / 65.1 / 77.5 ms | 36.0 / 61.0 / 61.1 ms | 44.6 / 61.7 / 62.3 ms |

## §4 解读与限制（不外推）

1. 360 个请求（6 用例 × 20 × 3 轮）零失败，`max_error_rate=0.0` 判据满足；并发 4 档位未出现排队抖动，
   轮间 p50 差异在个位数到十几毫秒级。
2. 明显的热身效应：`docqa` 轮1 p95=310ms、轮2/轮3 收敛到 85-90ms；`docqa-health` 稳定在 270-350ms，
   是当前 release 集合里最慢的一环（它串到 Python 能力层健康检查）。
3. **本轮容量结论不可外推到真实模型链路**：隔离栈未配置 embedding/LLM，`docqa` 返回 200 但检索为空
   （`citations=0`），因此 p95 只反映"网关 → 编排 → Python 检索空返回"的开销，不含模型生成时延。
   带模型的容量口径必须在配置了真实模型凭据的栈上另跑一轮，阈值同样先声明。
4. `graph-build` 走"提交即取消"，测量的是任务受理路径而非建图完成时延；建图完成时延属于
   worker 侧指标，需要按 job 终态统计，不在本探针覆盖范围。

## §5 状态

- [x] 阈值/参数在执行前声明（§1），执行后未回改
- [x] release preset 3 轮 × 20 请求 × 并发 4 全绿，`failed_rounds=0`（§3）
- [x] 限制条件已写明，不冒充模型链路容量（§4.3）
- [ ] 带真实 embedding/LLM 配置再跑一轮 release soak，并按当轮实测 p95 另立延迟门禁口径（需先声明）
- [ ] 更高并发 capacity 递增矩阵（4 → 8 → 16）以定位上限，当前只有并发 4 单点

## §6 临时性能阈值台账（唯一事实源）

开工令禁止"未说明就改验收阈值"，所以所有在用的阈值集中到这里，标明临时/长期与复审触发条件。

| 场景 | 阈值 | 值 | 性质 | 依据与复审触发 |
|---|---|---|---|---|
| 发布验收（CI `release-acceptance`） | `PERF_PROBE_MAX_ERROR_RATE` | `0.0` | **长期** | 错误零容忍，不放宽 |
| 发布验收（CI，`LLM_ENABLED=0`） | `PERF_PROBE_MAX_P95_MS` | `3000` | **临时** | 依据本文 §3 轮1 实测 p95（`health` 31.3 / `query` 42.1 / `docqa-health` 400.5 / `nl2cypher-status` 29.1 ms，20 请求 / 并发 4）取最大 400.5ms，再乘 9 倍余量覆盖冷共享 runner。**复审触发**：CI 累计 3 次成功发布验收后，用 CI 实测分布替换该余量系数；一旦配置真实模型即失效（见下一行） |
| 发布验收（CI，`LLM_ENABLED=1`） | `PERF_PROBE_MAX_P95_MS` | `20000` | **临时** | 该档位 `docqa` 由生成时延主导，3000ms 不成立；20000ms 是先验宽档。**复审触发**：真实模型凭据下跑满 3 轮后重定为分位数阈值，不得沿用先验值 |
| 本地 soak（§3） | `PERF_PROBE_MAX_P95_MS` | `0`（关闭） | 本轮口径 | `--max-p95-ms 0` 即 "0 disables"；soak 只观测趋势，避免把单机热身数据当 SLO |
| 回滚矩阵（`rollback-matrix` job / 执行器） | 延迟阈值 | 无 | 明确不适用 | 该矩阵判据是安全不变量与拒绝语义，不产出性能结论；其上 `docqa` 同样只算"检索/引用链路 smoke" |

两条红线：

1. 临时阈值是在 `ci.yml` 的 "Declare performance thresholds for this regime" 步骤里**执行前**写入
   `$GITHUB_ENV` 的，不在结果出来后反填；任何改动必须在同一提交里说明依据。
2. 以上所有场景都未配置 embedding / LLM，p95 只覆盖"网关 → 编排 → Python 空检索返回"，
   **不构成问答质量或模型容量结论**。无 LLM 口径统一表述为"检索/引用链路 smoke"，回滚探针逐腿打印
   `NOTE main_docqa_scope=link_smoke_only llm_configured=0`。
