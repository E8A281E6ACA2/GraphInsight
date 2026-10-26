# GraphInsight Backend

多模态知识图谱可视化平台后端服务

## 当前角色

当前执行口径下，`backend/` 是 GraphInsight 的 Python 能力层，而不是默认公共 API 前门。

它主要负责：

1. 文档解析与切分
2. 实体/关系抽取
3. `docqa`、`deep-research`、`nl2cypher` 等 AI 能力
4. 被 Go 网关编排调用的上游服务

本地默认入口：

1. Go 外部网关：`http://127.0.0.1:8081`
2. Python 能力层：`http://127.0.0.1:8001`

说明：

1. `8081` 是 Go 默认端口。
2. 如果该端口已被非 GraphInsight 服务占用，统一启动脚本会自动切换到其他可用端口。
3. 当前这次启动的真实入口地址以仓库根目录下的 `logs/dev/runtime.env` 为准。

前端与外部调用默认应优先经过 Go，而不是直接打 Python。

## 环境模式

当前后端联调建议区分两种模式：

1. 本机混合模式：Neo4j 用 Desktop 或 Docker，Python / Go 跑宿主机
2. Docker 联调模式：Neo4j 用 Docker，Python / Go 仍跑宿主机

详细说明见：

1. [DEVELOPMENT_ENVIRONMENT_MODES.md](../docs/DEVELOPMENT_ENVIRONMENT_MODES.md)
2. [NEO4J_RUNTIME_SWITCHING.md](../docs/NEO4J_RUNTIME_SWITCHING.md)
3. [DELIVERY_RUNTIME_STRATEGY.md](../docs/DELIVERY_RUNTIME_STRATEGY.md)

## 技术栈

- **FastAPI**: 现代 Python Web 框架
- **Neo4j**: 图数据库
- **Pydantic**: 数据验证
- **Uvicorn**: ASGI 服务器

## 快速开始

### 1. 安装 Neo4j

使用 Docker 启动 Neo4j（推荐）：

```bash
docker run -d \
  --name neo4j \
  -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/password \
  -e NEO4J_PLUGINS='["apoc"]' \
  neo4j:5.26.26
```

或者下载并安装 Neo4j Desktop：https://neo4j.com/download/

### 2. 安装 Python 依赖

```bash
# Linux 开发环境固定使用 backend/.venv
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows 的 venv 布局是 `.venv\Scripts\python.exe`（没有 `bin/`）：

```bat
:: 建议用 3.11，requirements.txt 的 pin（SQLAlchemy 2.0.23 等）在 3.13 上不兼容
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python 边界门禁 `tests/run_unified_boundary_guards.py` 的解释器探测顺序（2026-10-24 起）：

1. `GUARD_PYTHON` 显式覆盖（路径不存在即报错，不静默降级）；
2. `.venv/bin/python`（POSIX）→ `.venv/Scripts/python.exe`（Windows），只有存在才使用；
3. 两者都不存在才回退当前解释器并打警告。

```bat
:: Windows 推荐运行方式（直接指定 venv 解释器，避免宿主 Python 造成虚假失败）
.venv\Scripts\python.exe tests\run_unified_boundary_guards.py

:: 或指定仓库外的独立 venv
set GUARD_PYTHON=C:\path\to\venv\Scripts\python.exe
python tests\run_unified_boundary_guards.py
```

探测只判断路径存在性；若 `.venv` 的解释器与已装包不一致（如 3.13 解释器配 cp311 扩展），
会以该 venv 运行并失败，应重建 venv 而不是升级依赖。探测逻辑的离线契约：

```bat
python tests\check_guard_python_resolution_unit.py
```

### 3. 配置环境变量

```bash
# 复制环境变量配置
cp .env.example .env

# 编辑 .env 文件，配置 Neo4j 连接信息
# 默认配置：
# NEO4J_URI=bolt://localhost:7687
# NEO4J_USER=neo4j
# NEO4J_PASSWORD=<your-local-dev-password>
```

### 4. 初始化测试数据

```bash
python init_test_data.py
```

这将创建以下测试数据：
- 4 个作物节点（水稻、小麦、玉米、番茄）
- 3 个病害节点（稻瘟病、小麦锈病、番茄晚疫病）
- 2 个虫害节点（玉米螟、蚜虫）
- 2 个技术节点（综合防治技术、滴灌技术）
- 2 个肥料节点（复合肥、有机肥）
- 以及它们之间的关系

### 5. 启动后端服务

推荐从仓库根目录使用统一启动脚本：

```bash
scripts/dev-backend.sh up
```

该脚本会按顺序处理：

1. 检查并通过 `docker-compose.dev.yml` 拉起开发 PostgreSQL 与 Neo4j。
2. 确保 `backend/.venv` 存在。
3. 启动 Python 能力层，监听 `0.0.0.0:8001`，本机访问地址默认写为 `http://localhost:8001`。
4. 启动 Go 外部网关，默认监听 `0.0.0.0:8081`，本机访问地址默认写为 `http://localhost:8081`，必要时自动回退到其他端口。

脚本会为 Linux 开发期写入本地容器 PostgreSQL 和 Neo4j 配置到 `logs/dev/backend.env`，并通过 `GRAPHINSIGHT_BACKEND_ENV_FILE` 传给 Python，不会覆盖你的 `backend/.env`。同时会把当前真实访问地址写入 `logs/dev/runtime.env`。
后端统一改造计划见 `backend/UNIFIED_BACKEND_PLAN.md`，当前后端职责冻结清单见 [BACKEND_BOUNDARY_FINAL.md](../docs/BACKEND_BOUNDARY_FINAL.md)。

统一模式下，启动脚本会默认写入：

1. `RBAC_AUTHZ_MODE=go_db`
2. `NEO4J_CONFIG_SOURCE=auto`，开发期优先读取后台 `admin_configs` 的 Neo4j 配置，缺失时回退到本地环境变量
3. Python 不再挂载任何公开 `/api/*` 业务兼容路由，仅保留 `/api/internal/*` 给 Go 编排调用
4. 直接访问这些已移除的 Python 公开业务路由会返回 `404`
5. Python 不再挂载公开 `/api/v1/admin/*` 管理兼容路由；只保留 `POST /api/internal/jobs/wake`
6. `POST /api/internal/jobs/wake` 保留给 Go 唤醒 Python 任务执行器
7. Python 路由注册层已按职责拆分：`api/route_registry.py` 只负责内部业务 capability 挂载，`admin/api/route_registry.py` 只负责内部 admin capability 挂载
8. `api/routes/*` 与 `admin/api/endpoints/*` 只保留共享实现和内部能力路由；旧的 `api/compat_routes/*` / `admin/api/compat_routes/*` 已删除
9. 可运行 `backend/.venv/bin/python backend/tests/check_dev_runtime_defaults.py`，同时校验 `logs/dev/backend.env`、`logs/dev/runtime.env` 与当前 Go/Python `/health` 是否仍符合 unified 默认值

如果只需要单独启动 Python 能力层：

```bash
cd backend
.venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8001
```

服务启动后，Python 能力层可用于能力诊断：
- API 根路径: http://127.0.0.1:8001
- Swagger UI: http://127.0.0.1:8001/docs
- ReDoc: http://127.0.0.1:8001/redoc

诊断脚本约定：

1. 默认诊断模式走 Go 正式入口。
2. Python `/api/internal/*` 只保留当前仍在 unified runtime 默认挂载的 capability surface。
3. 已退役的 `/api/internal/documents*` 与 `/api/internal/graph/build` 不再作为诊断入口保留。
4. `backend/tests/diagnose_docqa.py` 如需直连 Python，只允许显式使用 `GRAPHINSIGHT_DIAG_MODE=python-internal-docqa` 诊断仍存活的 DocQA capability，不表示 Python 重新暴露公开业务入口。

如果同时联调前端，建议顺序仍为：

1. 启动 Python `8001`
2. 启动 Go `8081`
3. 启动前端 `5173`

对外访问和前端联调应优先使用 Go `8081`。Python `8001` 主要用于 Go 上游调用和能力诊断。
如果脚本因端口占用把 Go 切换到了其他端口，则以 `logs/dev/runtime.env` 里的 `GO_BASE_URL` 为准，而不是继续假设 `8081`。
如果是浏览器联调或手工页面验收，前端建议设置 `VITE_API_BASE_URL=same-origin`，由浏览器走当前页面同源地址访问 Go；Node 侧 smoke / Playwright 预检如需直连后端，则单独使用 `ADMIN_BASE_URL` 或 `E2E_API_BASE_URL`。

### 6. 测试 API

```bash
python test_api.py
```

### 7. 发布前后端校验

Windows PowerShell:

```powershell
$env:ADMIN_EMAIL="yh@qs.al"
$env:ADMIN_PASSWORD="***"
powershell -ExecutionPolicy Bypass -File backend/tests/run_backend_preflight.ps1
```

也可以改为提供已有 token：

```powershell
$env:ADMIN_TOKEN="***"
powershell -ExecutionPolicy Bypass -File backend/tests/run_backend_preflight.ps1
```

脚本会自动：

1. 检查 Python 能力层 `http://127.0.0.1:8001/health`
2. 检查当前 GraphInsight Go 外部网关健康状态，优先读取 `logs/dev/runtime.env` 中的真实地址
3. 如未启动则依次拉起 Python 与 Go
4. 先执行 `go-backend/scripts/smoke_orchestrated_routes.py`
5. 再执行 `backend/tests/run_backend_smoke_suite.py`
6. 自动停止本次脚本拉起的进程

说明：

1. 当前这个 preflight 已以 Go 作为默认外部验证入口。
2. Python 仍作为 Go 的内部上游能力层参与联调。

## Python 能力入口

当前 Python 侧只应按下面三类入口理解：

### 1. 公开基础入口

```bash
GET /health
GET /docs
```

### 2. 内部 capability 入口

这些入口给 Go 编排层调用，默认不对外开放：

```bash
POST /api/internal/docqa
POST /api/internal/docqa/deep-research
GET  /api/internal/docqa/health
POST /api/internal/nl2cypher
POST /api/internal/jobs/wake
```

### 3. 管理侧内部入口

Python admin 侧不再保留 public compat 集合；管理相关只保留：

1. `POST /api/internal/jobs/wake`

## 示例查询

### 查询所有作物

```cypher
MATCH (n:Crop) RETURN n LIMIT 10
```

### 查询作物及其病害

```cypher
MATCH (c:Crop)-[r:AFFECTED_BY]->(d:Disease)
RETURN c, r, d LIMIT 50
```

### 查询防治技术

```cypher
MATCH (t:Technology)-[r:PREVENTS]->(d)
RETURN t, r, d
```

### 查询完整的知识图谱

```cypher
MATCH (n)-[r]->(m)
RETURN n, r, m
LIMIT 100
```

## 项目结构

```
backend/
├── main.py                      # 应用入口
├── config.py                    # 配置管理
├── init_test_data.py           # 测试数据初始化脚本
├── test_api.py                 # API 测试脚本
├── requirements.txt            # Python 依赖
├── .env                        # 环境变量配置
├── api/
│   └── routes/                 # 共享实现与 internal capability 入口
├── admin/
│   └── api/
│       └── endpoints/          # 共享实现、internal capability、Go-only compat 例外
├── services/
│   ├── neo4j_service.py        # Neo4j 服务
│   └── media_service.py        # 能力层共享服务
├── models/
│   └── graph.py                # 数据模型
├── utils/
│   └── neo4j_parser.py         # Neo4j 解析器
└── media/                      # 多媒体文件存储
    └── README.md
```

## 开发指南

### 添加新的 API 端点

1. 先判断它是否应该属于 Go 外部入口还是 Python internal capability。
2. Python 不再新增任何对外 public compat 路由。
3. 共享实现放到 `api/routes/*` 或 `admin/api/endpoints/*`。
4. Python internal capability 放到 `*_internal.py` 或现有 internal router 中。
5. 需要 direct-debug 时，优先走 Go 正式入口；确实需要定位 Python 能力层时，直接使用 `/api/internal/*` 配合内部诊断脚本。

### 添加新的节点类型

1. 在 Neo4j 中创建新的节点标签
2. 更新前端的颜色映射配置
3. 更新数据模型（如需要）

## 故障排除

### Neo4j 连接失败

- 检查 Neo4j 是否正在运行
- 验证 `.env` 文件中的连接信息
- 检查防火墙设置

### CORS 错误

- 确保前端 URL 在 `main.py` 的 CORS 配置中
- 检查前端的 API 基础 URL 配置

### 媒体文件 404

- 确保媒体文件存在于 `backend/media/` 目录
- 检查文件名是否正确
- 检查文件权限

## 性能优化

- 使用连接池（已配置）
- 添加查询结果缓存
- 限制查询结果数量
- 使用索引优化查询

## 安全建议

- 使用只读用户执行查询
- 实现查询白名单
- 添加速率限制
- 使用 HTTPS（生产环境）
- 实现身份验证和授权

## 参考资料

- [FastAPI 文档](https://fastapi.tiangolo.com/)
- [Neo4j Python 驱动](https://neo4j.com/docs/python-manual/current/)
- [Cypher 查询语言](https://neo4j.com/docs/cypher-manual/current/)
