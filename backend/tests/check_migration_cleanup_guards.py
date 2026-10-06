#!/usr/bin/env python3
"""Static guards for migration cleanup regressions."""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_nl2cypher_status_uses_current_config_service() -> None:
    source = _source("api/routes/nl2cypher.py")
    if "build_examples_response" in source or "get_nl2cypher_status" in source:
        raise AssertionError("retired Python nl2cypher examples/status helpers must not remain in shared route module")
    if "admin.config_service" in source or "ConfigService.get_" in source:
        raise AssertionError("nl2cypher shared capability module must not depend on removed admin.config_service")


def test_config_constants_do_not_restore_openai_category() -> None:
    source = _source("core/constants.py")
    if "OPENAI =" in source:
        raise AssertionError("ConfigCategory must not restore retired OPENAI category; use AI_SERVICE")


def test_public_compat_registry_uses_compat_route_package() -> None:
    source = _source("api/route_registry.py")
    if "api.compat_routes" in source:
        raise AssertionError("Python business public compatibility package should be removed from the route registry")
    if "legacy_debug_routes" in source:
        raise AssertionError("business route registry must not keep removed legacy debug route imports")


def test_public_admin_compat_registry_uses_compat_route_package() -> None:
    source = _source("admin/api/route_registry.py")
    if "admin.api.compat_routes" in source:
        raise AssertionError("Python admin public compatibility package should be removed from the route registry")
    if "legacy_debug_routes" in source:
        raise AssertionError("admin route registry must not keep removed legacy debug route imports")
    forbidden_endpoint_imports = (
        "auth as",
        "config as",
        "logs as",
        "monitor as",
        "profile as",
        "qa_traces as",
        "rbac as",
        "users as",
        "auth,",
        "config,",
        "logs,",
        "monitor,",
        "profile,",
        "qa_traces,",
        "rbac,",
        "users,",
    )
    for marker in forbidden_endpoint_imports:
        if marker in source:
            raise AssertionError(f"admin route registry must not import public admin endpoint module: {marker}")
    if "jobs_endpoints.internal_router" not in source:
        raise AssertionError("admin route registry should only mount the internal jobs wake router")
    if "jobs_endpoints.router" in source:
        raise AssertionError("admin route registry must not mount jobs public router")


def test_admin_auth_and_jobs_public_routes_removed_from_python() -> None:
    auth_source = _source("admin/api/endpoints/auth.py")
    jobs_source = _source("admin/api/endpoints/jobs.py")
    if "public_router = APIRouter" in auth_source:
        raise AssertionError("admin auth public routes should no longer be declared in endpoints/auth.py")
    if "public_router = APIRouter" in jobs_source:
        raise AssertionError("admin jobs public routes should no longer be declared in endpoints/jobs.py")
    if "compat_router = APIRouter" in auth_source or '"/authorize"' in auth_source:
        raise AssertionError("admin auth authorize compatibility route should no longer be declared in endpoints/auth.py")
    retired_auth_handlers = (
        "async def login",
        "async def logout",
        "async def get_profile",
        "async def register",
        "async def change_password",
    )
    for marker in retired_auth_handlers:
        if marker in auth_source:
            raise AssertionError(f"admin auth endpoint must not keep retired public handler: {marker}")
    retired_jobs_handlers = (
        "create_build_graph_job",
        "create_clear_kb_job",
        "create_reindex_job",
        "list_jobs",
        "get_job(",
        "get_job_logs",
        "retry_job",
        "cancel_job",
    )
    for marker in retired_jobs_handlers:
        if marker in jobs_source:
            raise AssertionError(f"admin jobs endpoint must not keep retired public handler: {marker}")


def test_admin_endpoint_modules_are_marked_public_retired() -> None:
    endpoint_files = (
        "auth.py",
        "config.py",
        "jobs.py",
        "logs.py",
        "monitor.py",
        "profile.py",
        "qa_traces.py",
        "rbac.py",
        "users.py",
    )
    for filename in endpoint_files:
        source = _source(f"admin/api/endpoints/{filename}")
        if "PYTHON_PUBLIC_ADMIN_API_RETIRED = True" not in source:
            raise AssertionError(f"admin endpoint module must be marked public-retired: {filename}")
        if filename != "jobs.py" and "internal_router" in source:
            raise AssertionError(f"only jobs.py may expose a Python internal admin capability router: {filename}")
        if filename != "jobs.py":
            forbidden_markers = (
                "APIRouter(",
                "@router.",
                "async def ",
                "Depends(",
                "Query(",
            )
            for marker in forbidden_markers:
                if marker in source:
                    raise AssertionError(
                        f"retired admin endpoint module must stay marker-only: {filename} contains {marker}"
                    )
    qa_traces_source = _source("admin/api/endpoints/qa_traces.py")
    retired_qa_trace_handlers = (
        "async def list_qa_traces",
        "async def get_qa_cost_summary",
        "async def get_qa_trace",
    )
    for marker in retired_qa_trace_handlers:
        if marker in qa_traces_source:
            raise AssertionError(f"admin qa_traces endpoint must not keep retired public handler: {marker}")
    retired_module_handlers = {
        "config.py": (
            "async def get_config_list",
            "async def get_available_models",
            "async def get_openai_config",
            "async def get_nl2cypher_config",
            "async def get_neo4j_config",
            "async def get_ai_service_config",
            "async def get_config_detail",
            "async def create_config",
            "async def update_config",
            "async def batch_update_configs",
            "async def delete_config",
            "async def init_from_env",
            "async def test_connection",
            "async def get_latest_model_connection_test",
        ),
        "profile.py": (
            "async def get_profile",
            "async def update_profile",
            "async def change_password",
            "async def get_profile_stats",
        ),
        "rbac.py": (
            "async def list_roles",
            "async def list_permissions",
            "async def list_bindings",
            "async def create_binding",
            "async def delete_binding",
        ),
        "logs.py": (
            "async def get_log_list",
            "async def get_log_detail",
            "async def get_log_stats",
            "async def get_recent_logs",
            "async def clean_old_logs",
        ),
        "monitor.py": (
            "async def get_system_stats",
            "async def get_health_status",
            "async def get_unified_metrics",
            "async def get_performance_metrics",
            "async def get_qa_quality_metrics",
            "async def get_slo_snapshot",
            "async def get_log_severity_metrics",
            "async def check_alerts",
            "async def simple_health_check",
        ),
        "users.py": (
            "def _write_user_audit_log",
            "async def list_users",
            "async def export_users_csv",
            "async def create_user",
            "async def update_user",
            "async def toggle_user_status",
            "async def reset_user_password",
            "async def delete_user",
            "async def batch_reset_users_password",
            "async def batch_update_user_status",
            "async def batch_delete_users",
        ),
    }
    for filename, markers in retired_module_handlers.items():
        source = _source(f"admin/api/endpoints/{filename}")
        for marker in markers:
            if marker in source:
                raise AssertionError(f"admin endpoint must not keep retired public handler {marker} in {filename}")
    jobs_source = _source("admin/api/endpoints/jobs.py")
    if "internal_router = APIRouter" not in jobs_source:
        raise AssertionError("jobs.py must keep the internal wake router")
    if '@internal_router.post("/wake"' not in jobs_source:
        raise AssertionError('jobs.py must keep only the internal "/wake" route')
    forbidden_jobs_markers = (
        "\nrouter = APIRouter(",
        "@router.",
        "create_build_graph_job",
        "create_clear_kb_job",
        "create_reindex_job",
        "list_jobs",
        "get_job(",
        "get_job_logs",
        "retry_job",
        "cancel_job",
    )
    for marker in forbidden_jobs_markers:
        if marker in jobs_source:
            raise AssertionError(f"jobs.py must not regress to public/admin handler surface: {marker}")


def test_python_compatibility_helper_file_stays_removed() -> None:
    if (ROOT / "api/compatibility.py").exists():
        raise AssertionError("api/compatibility.py should stay removed after Python public compat retirement")


def test_legacy_root_debug_helpers_stay_removed() -> None:
    removed_files = (
        "admin/schemas.py",
        "check_logs_table.py",
        "check_node.py",
        "create_video_node.py",
        "debug_node.py",
        "init_admin_quick.py",
    )
    existing = [path for path in removed_files if (ROOT / path).exists()]
    if existing:
        raise AssertionError(f"legacy root debug/helper files should stay deleted, found: {existing}")


def test_old_admin_legacy_archive_removed() -> None:
    legacy_dir = ROOT / "admin" / "_legacy_routes"
    if not legacy_dir.exists():
        return
    leftover = sorted(path.name for path in legacy_dir.glob("*.py")) + sorted(path.name for path in legacy_dir.glob("*.md"))
    if leftover:
        raise AssertionError(f"old admin legacy archive should be removed, found: {leftover}")


def test_removed_legacy_shim_files_do_not_return() -> None:
    removed_files = (
        "api/legacy_debug_routes/__init__.py",
        "api/legacy_debug_routes/client_logs.py",
        "api/legacy_debug_routes/query.py",
        "api/legacy_debug_routes/node.py",
        "api/legacy_debug_routes/expand.py",
        "api/legacy_debug_routes/media.py",
        "api/compatibility.py",
        "api/compat_routes/__init__.py",
        "api/compat_routes/doc_qa.py",
        "api/compat_routes/documents.py",
        "api/compat_routes/graph_build.py",
        "api/compat_routes/nl2cypher.py",
        "api/compat_routes/client_logs.py",
        "api/compat_routes/query.py",
        "api/compat_routes/node.py",
        "api/compat_routes/expand.py",
        "api/compat_routes/media.py",
        "admin/api/legacy_debug_routes/__init__.py",
        "admin/api/legacy_debug_routes/config.py",
        "admin/api/legacy_debug_routes/logs.py",
        "admin/api/legacy_debug_routes/monitor.py",
        "admin/api/legacy_debug_routes/profile.py",
        "admin/api/legacy_debug_routes/qa_traces.py",
        "admin/api/legacy_debug_routes/rbac.py",
        "admin/api/legacy_debug_routes/users.py",
        "admin/api/compat_routes/__init__.py",
        "admin/api/compat_routes/auth.py",
        "admin/api/compat_routes/jobs.py",
        "admin/api/compat_routes/config.py",
        "admin/api/compat_routes/logs.py",
        "admin/api/compat_routes/monitor.py",
        "admin/api/compat_routes/profile.py",
        "admin/api/compat_routes/qa_traces.py",
        "admin/api/compat_routes/rbac.py",
        "admin/api/compat_routes/users.py",
        "api/routes/client_logs.py",
        "api/routes/query.py",
        "api/routes/node.py",
        "api/routes/expand.py",
        "api/routes/media.py",
        "api/routes/doc_qa_public.py",
        "api/routes/documents.py",
        "api/routes/documents_internal.py",
        "api/routes/documents_public.py",
        "api/routes/graph_build.py",
        "api/routes/graph_build_internal.py",
        "api/routes/graph_build_public.py",
        "api/routes/nl2cypher_public.py",
    )
    existing = [path for path in removed_files if (ROOT / path).exists()]
    if existing:
        raise AssertionError(f"removed legacy shim files should stay deleted, found: {existing}")


def test_go_business_route_registration_stays_explicit() -> None:
    source = _source("../go-backend/internal/httpserver/handlers.go")
    required_markers = (
        'mux.HandleFunc("/api/docqa", guard.wrap("qa:ask", buildNativeDocQAHandler(',
        'mux.HandleFunc("/api/docqa/deep-research", guard.wrap("qa:ask", buildNativeDeepResearchHandler(',
        'mux.HandleFunc("/api/docqa/health", guard.wrap("monitor:read", buildNativeDocQAHealthHandler(',
        'mux.HandleFunc("/api/nl2cypher", guard.wrap("nl2cypher:use", buildNativeNL2CypherGenerateHandler(',
    )
    for marker in required_markers:
        if marker not in source:
            raise AssertionError(f"expected explicit Go business route registration marker missing: {marker}")

    if 'buildOrchestratorHandler(' in source:
        raise AssertionError("handlers.go should not regress to generic orchestrator registration for public business routes")


def test_go_authz_does_not_call_python_authorize() -> None:
    authz_source = _source("../go-backend/internal/authz/client.go")
    middleware_source = _source("../go-backend/internal/httpserver/authz_middleware.go")
    server_source = _source("../go-backend/internal/httpserver/server.go")
    authz_client_forbidden = (
        "PythonBackendBaseURL",
        "type Client struct",
        "func New(cfg config.Config)",
        "CheckPermission(ctx context.Context, bearerToken",
        "/api/v1/admin/auth/authorize",
        "X-Go-Authz",
    )
    for marker in authz_client_forbidden:
        if marker in authz_source:
            raise AssertionError(f"go-backend/internal/authz/client.go must not restore Python authorize hop marker: {marker}")

    for marker in ("allowLegacyAuthzClientRequest", "authzClient", "authzInitErr"):
        if marker in middleware_source:
            raise AssertionError(f"go-backend/internal/httpserver/authz_middleware.go must not restore Python authorize hop marker: {marker}")

    for marker in ("authz.New", "authzClient", "authzInitErr"):
        if marker in server_source:
            raise AssertionError(f"go-backend/internal/httpserver/server.go must not restore Python authorize hop marker: {marker}")


def test_core_delivery_docs_do_not_regress_to_old_workspace_or_smoke_baseline() -> None:
    docs_to_check = (
        "docs/GO_PYTHON_MIGRATION_STATUS.md",
        "docs/GO_DEFAULT_ENTRY_EXECUTION_PLAN.md",
        "docs/GO_PYTHON_DELIVERY_CLOSURE_CHECKLIST.md",
        "docs/ENTERPRISE_PRE_RELEASE_SMOKE_CHECKLIST.md",
        "docs/ENTERPRISE_IMPLEMENTATION_BACKLOG.md",
        "docs/ENTERPRISE_ROADMAP_CHECKLIST.md",
        "docs/ENTERPRISE_GO_LIVE_ACCEPTANCE_CHECKLIST.md",
        "docs/ENTERPRISE_OPERATIONS_RUNBOOK.md",
        "docs/DELIVERY_RUNTIME_STRATEGY.md",
        "docs/FRONTEND_E2E_RUNTIME_GUIDE.md",
        "docs/DEVELOPMENT_ENVIRONMENT_MODES.md",
    )
    forbidden_markers = (
        "/mnt/c/Users/AxTlz/projects/GraphInsight",
        "SUMMARY total=10 failed=0",
        "共 18 个 case",
        "共 19 个 case",
        "total=19",
        "Go 入口 + Python 上游实现",
        "权限校验仍有部分依赖 Python 上游",
    )
    for rel_path in docs_to_check:
        doc_path = ROOT / ".." / rel_path
        if not doc_path.exists():
            # 该交付文档当前不在仓库（未提交/已下线）：不存在即无可回归内容，
            # 跳过；不为通过守卫而伪造文档（M4-R1 审计前置文件处理）。
            continue
        source = doc_path.read_text(encoding="utf-8")
        for marker in forbidden_markers:
            if marker in source:
                raise AssertionError(f"{rel_path} should not regress to stale marker: {marker}")


def test_linux_backend_tooling_uses_dot_venv_only() -> None:
    linux_entrypoints = (
        "tests/run_unified_boundary_guards.py",
        "tests/run_backend_smoke_suite.py",
        "tests/run_perf_soak.py",
    )
    forbidden_markers = (
        "Scripts",
        "python.exe",
        "backend/venv",
        'ROOT / "venv"',
        "PYTHON_CANDIDATES",
    )
    required_marker = 'ROOT / ".venv" / "bin" / "python"'
    for rel_path in linux_entrypoints:
        source = _source(rel_path)
        if required_marker not in source:
            raise AssertionError(f"{rel_path} must resolve Python from backend/.venv/bin/python")
        for marker in forbidden_markers:
            if marker in source:
                raise AssertionError(f"{rel_path} must not fall back to Windows/system Python marker: {marker}")


def test_unified_dev_defaults_do_not_regress_to_remote_or_python_public() -> None:
    files_to_check = (
        "../backend/.env.example",
        "../go-backend/.env.example",
        "../scripts/dev-backend.sh",
        "../AGENTS.md",
    )
    forbidden_markers = (
        "182.92.111.65",
        "localhost:5432/graphinsight_admin",
        "PUBLIC_BUSINESS_ROUTES_ENABLED=true",
        "PUBLIC_ADMIN_ROUTES_ENABLED=true",
        "RBAC_AUTHZ_MODE=python",
        "backend/venv/Scripts/python.exe",
    )
    for rel_path in files_to_check:
        source = _source(rel_path)
        for marker in forbidden_markers:
            if marker in source:
                raise AssertionError(f"{rel_path} should not regress to stale unified default: {marker}")

    backend_env = _source("../backend/.env.example")
    go_env = _source("../go-backend/.env.example")
    for rel_path, source in (
        ("../backend/.env.example", backend_env),
        ("../go-backend/.env.example", go_env),
    ):
        if "127.0.0.1:5434/graphinsight_admin" not in source:
            raise AssertionError(f"{rel_path} must default to local Docker admin PostgreSQL")
        if "RBAC_AUTHZ_MODE=go_db" not in source:
            raise AssertionError(f"{rel_path} must default authz to go_db")


def test_kb_scope_strict_mode_has_no_compat_toggle() -> None:
    """M4 关闭后 KB 作用域 strict 是唯一形态（契约 §2.11 D4）。

    固定三件事，防止后续把 strict 悄悄削弱：
    1. 不得新增可关闭/降级 KB 作用域强制的配置开关或 default KB 兜底；
    2. KB 作用域第二阶段授权必须保持 fail-closed 锚点；
    3. 跨 KB 负向测试与前端 kb_id 显式透传必须在位。
    """
    forbidden_markers = (
        "KB_SCOPE_ENFORCE",
        "KB_DEFAULT_ID",
        "DefaultKBID",
        "defaultKBID",
        "KB_SCOPE_COMPAT",
    )
    scope_surfaces = (
        "../go-backend/internal/config/config.go",
        "../go-backend/internal/scope/scope.go",
        "../go-backend/internal/httpserver/qa_scope.go",
        "../go-backend/internal/httpserver/authz_middleware.go",
    )
    for rel_path in scope_surfaces:
        source = _source(rel_path)
        for marker in forbidden_markers:
            if marker in source:
                raise AssertionError(
                    f"{rel_path} must not add a KB scope toggle/default-KB fallback ({marker}); "
                    "KB scope strict is unconditional"
                )

    scope_source = _source("../go-backend/internal/scope/scope.go")
    if "ErrScopeRequired()" not in scope_source:
        raise AssertionError("scope resolver must keep rejecting requests without kb_id/kb_ids")

    authz_source = _source("../go-backend/internal/httpserver/authz_middleware.go")
    if "scoped permission check failed, fail closed" not in authz_source:
        raise AssertionError("checkPermissionWithScope must keep fail-closed on scoped permission errors")
    qa_scope_source = _source("../go-backend/internal/httpserver/qa_scope.go")
    if "resolve authorized kb ids failed, fail closed" not in qa_scope_source:
        raise AssertionError("authorizeQAEffectiveKBIDs must keep fail-closed on authorization errors")

    negative_tests = (
        "../go-backend/internal/httpserver/m4r1_kb_failclosed_soft_test.go",
        "../go-backend/internal/httpserver/m4r1_cross_scope_negative_test.go",
        "../go-backend/internal/httpserver/m4r1_qa_scope_negative_test.go",
    )
    for rel_path in negative_tests:
        path = ROOT / rel_path
        if not path.exists():
            raise AssertionError(f"{rel_path} must stay present as cross-KB denial regression evidence")
        if "must not be called" not in path.read_text(encoding="utf-8"):
            raise AssertionError(f"{rel_path} must assert downstream services are not reached on denial")

    api_source = _source("../frontend/src/services/api.ts")
    if "X-KB-ID" not in api_source:
        raise AssertionError("frontend api client must keep injecting X-KB-ID for business calls")
    for rel_path in ("../frontend/src/services/docQa.ts", "../frontend/src/services/graphService.ts"):
        if "requireActiveKbId" not in _source(rel_path):
            raise AssertionError(f"{rel_path} must keep local interception when no KB is selected")


def _tracked_backend_tests(pattern: str) -> list:
    """返回 git 追踪的 `backend/tests/<pattern>` 文件 [(绝对路径, 源码)]。

    只走 `git ls-files`，不用目录 glob：未追踪的嵌套 worktree/venv 里的同名脚本会污染
    扫描面，产出"本地判红、CI 不复现"的假红（既有铁律）。git 退出码非 0、追踪文件在
    磁盘缺失、或扫描面为空，一律 raise——扫描面塌成 0 个文件绝不能读成"全部干净"。
    """
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", f"backend/tests/{pattern}"],
        cwd=str(ROOT.parent),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git ls-files 失败（{pattern}, exit={proc.returncode}）: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    files = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8")
        path = ROOT.parent / rel
        if not path.exists():
            raise AssertionError(f"追踪文件在磁盘缺失，扫描面不可信：{rel}")
        files.append((path, path.read_text(encoding="utf-8")))
    if not files:
        raise AssertionError(f"扫描面为空：backend/tests/{pattern} 没有任何追踪文件")
    return sorted(files, key=lambda item: item[0].name)


BLANK_ENV_OVERRIDE_MARKERS = (
    'GRAPHINSIGHT_BACKEND_ENV_FILE"] = ""',
    'GRAPHINSIGHT_BACKEND_ENV_FILE", "")',
    "GRAPHINSIGHT_BACKEND_ENV_FILE'] = ''",
)
ENV_FILE_PIN_MARKERS = (
    'GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(',
    "GRAPHINSIGHT_BACKEND_ENV_FILE'] = str(",
    'GRAPHINSIGHT_BACKEND_ENV_FILE"] = f"',
)


def test_sqlite_isolated_tests_use_env_file_not_blank_override() -> None:
    """DB 隔离铁律（任务 #55）：注入 sqlite 测试库的文件禁止把 env 覆盖变量置空。

    `GRAPHINSIGHT_BACKEND_ENV_FILE=""` 是伪隔离：admin/database.py 只在该变量指向
    存在的文件时才走隔离分支，空串落到 else 分支执行 load_dotenv(find_dotenv(),
    override=True)，沿子进程脚本 __file__ 命中 backend/.env，用其中的 PostgreSQL
    地址覆盖注入的 sqlite 地址——迁移测试的 rollback（DROP TABLE/COLUMN）就会打到
    开发库。唯一合法写法是把该变量指向真实存在的临时 env 文件。
    """
    offenders = []
    self_name = Path(__file__).name
    for path, source in _tracked_backend_tests("check_*.py"):
        if path.name == self_name:
            # 本守卫自身源码里就带着被禁字面量（用于构造匹配规则），不参与扫描
            continue
        if "sqlite:///" not in source:
            continue
        for marker in BLANK_ENV_OVERRIDE_MARKERS:
            if marker in source:
                offenders.append(f"{path.name} 置空 env 覆盖变量（伪隔离）: {marker}")
        if not any(marker in source for marker in ENV_FILE_PIN_MARKERS):
            offenders.append(f"{path.name} 未把 env 覆盖变量指向真实 env 文件")
    if offenders:
        raise AssertionError("sqlite-isolated tests must point GRAPHINSIGHT_BACKEND_ENV_FILE at an existing env file: "
                             + "; ".join(offenders))


ENGINE_BUILD_MARKERS = ("create_all(", "create_engine(")
DRIVER_SUFFIX = "_driver.py"
# `ADMIN_DATABASE_URL` 里的 DATABASE_URL 不算裸写：lookbehind 只放行真正的前缀边界。
BARE_DATABASE_URL_RE = re.compile(r"(?<!ADMIN_)(?<![_A-Za-z0-9])DATABASE_URL\b")


def _dialect_gate_present(source: str) -> bool:
    """方言闸门两种合法形态：子进程内 `!= "sqlite"` 硬退出，或父进程探针回读 DIALECT。"""
    if "dialect.name" not in source:
        return False
    return any(
        token in source
        for token in ('!= "sqlite"', "!= 'sqlite'", '"DIALECT"', "'DIALECT'")
    )


def _db_isolation_findings(files: list) -> list:
    """对 [(path, source)] 执行共享开发库隔离判据，返回 findings（空 == 干净）。

    判据只作用于"真的建引擎/建表"的脚本（含 create_all/create_engine），其余脚本不掺和，
    避免把纯单元脚本读成假红。子进程 driver 的 env 由父进程钉好后再 spawn，因此只对它
    要求方言闸门与"禁止裸 DATABASE_URL"。
    """
    findings = []
    self_name = Path(__file__).name
    for path, source in files:
        if path.name == self_name:
            # 本守卫源码里带着被禁字面量（构造匹配规则用），不参与扫描
            continue
        if not any(marker in source for marker in ENGINE_BUILD_MARKERS):
            continue
        if BARE_DATABASE_URL_RE.search(source):
            findings.append(
                f"{path.name} 写了裸 DATABASE_URL（admin/database.py 只认 ADMIN_DATABASE_URL，"
                "未知变量被静默忽略 → 回落 backend/.env 的共享开发 PostgreSQL）"
            )
        if not _dialect_gate_present(source):
            findings.append(
                f"{path.name} 缺方言 fail-closed 闸门（必须校验 engine.dialect.name，"
                '非 sqlite 时退出或断言，如 `!= "sqlite"` 或探针回读 `DIALECT`）'
            )
        if path.name.endswith(DRIVER_SUFFIX):
            continue
        if "GRAPHINSIGHT_BACKEND_ENV_FILE" not in source:
            findings.append(f"{path.name} 未钉 GRAPHINSIGHT_BACKEND_ENV_FILE（env 覆盖入口）")
        if "ADMIN_DATABASE_URL" not in source:
            findings.append(f"{path.name} 未注入 ADMIN_DATABASE_URL（建引擎却用默认配置 = 连开发库）")
        if "sqlite:///" not in source:
            findings.append(f"{path.name} 未把测试库指向 sqlite:/// 临时文件")
    return findings


def test_engine_building_scripts_pin_env_url_and_dialect() -> None:
    """共享开发库隔离铁律（Wave 4-3）：建引擎/建表的脚本必须同时满足钉 env、钉 ADMIN_DATABASE_URL、带方言闸门。

    本轮事故取证：一次性探针脚本把注入变量写成 `DATABASE_URL`。`admin/database.py` 的隔离
    分支只认 `ADMIN_DATABASE_URL`，未知变量被静默忽略，引擎于是回落到 `backend/.env` 里的
    共享开发 PostgreSQL，并在该连接上发起 DDL 尝试。已检查范围内未观察到持久化变化；因无
    事前全库快照，保留不可完全判定窗口。写错一个变量名 = 直连开发库，所以这条判据必须静态
    常驻，不能只靠人记住。
    """
    offenders = list(_db_isolation_findings(_tracked_backend_tests("*.py")))

    # 负向自证：四类缺陷各造一个合成样本，证明规则真在拦截而不是空转。
    compliant_check = (
        'os.environ["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(env_path)\n'
        'ADMIN_DATABASE_URL=sqlite:///tmp.db\n'
        'if engine.dialect.name != "sqlite":\n    raise SystemExit(9)\n'
        "Base.metadata.create_all(engine)\n"
    )
    cases = [
        ("check_missing_env_pin.py", 'ADMIN_DATABASE_URL=sqlite:///t.db\n'
                                     'if engine.dialect.name != "sqlite":\n    raise SystemExit(9)\n'
                                     "Base.metadata.create_all(engine)\n",
         "未钉 GRAPHINSIGHT_BACKEND_ENV_FILE"),
        ("check_wrong_var_name.py", 'os.environ["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(p)\n'
                                    'os.environ["DATABASE_URL"] = "sqlite:///t.db"\n'
                                    'if engine.dialect.name != "sqlite":\n    raise SystemExit(9)\n'
                                    "conn = create_engine(url)\n",
         "裸 DATABASE_URL"),
        ("check_no_dialect_gate.py", 'os.environ["GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(p)\n'
                                     "ADMIN_DATABASE_URL=sqlite:///t.db\n"
                                     "Base.metadata.create_all(engine)\n",
         "缺方言 fail-closed 闸门"),
        ("check_plain_unit.py", 'os.environ["DATABASE_URL"] = "postgres://x/y"\n'
                                "assert compute(1) == 1\n",
         None),  # 不建引擎 → 不在扫描面内
    ]
    for name, source, expect in cases:
        found = _db_isolation_findings([(Path(name), source)])
        if expect is None:
            if found:
                offenders.append(f"规则过宽：不建引擎的 {name} 被判违规 {found}")
            continue
        if not any(expect in item for item in found):
            offenders.append(f"负向自证失败：{name} 应命中「{expect}」，实际 {found}")

    for name, source in (("check_ok.py", compliant_check),
                         ("m5_ok_driver.py", 'if engine.dialect.name != "sqlite":\n    raise SystemExit(9)\n'
                                             "Base.metadata.create_all(engine)\n")):
        found = _db_isolation_findings([(Path(name), source)])
        if found:
            offenders.append(f"合规样本被误报：{name} -> {found}")

    # 真文件红证：把本轮事故形态（`ADMIN_DATABASE_URL` 写成 `DATABASE_URL`）注入真实脚本
    # 源码（仅内存，不落盘），必须判红——证明规则对着真内容也在拦，而不是只对夹具生效。
    engine_files = [
        (path, source)
        for path, source in _tracked_backend_tests("*.py")
        if any(marker in source for marker in ENGINE_BUILD_MARKERS) and "ADMIN_DATABASE_URL" in source
    ]
    if not engine_files:
        offenders.append("扫描面里没有任何『建引擎且钉库』的脚本，隔离守卫失去作用对象")
    else:
        probe_path, probe_source = engine_files[0]
        incident = probe_source.replace("ADMIN_DATABASE_URL", "DATABASE_URL")
        incident_findings = _db_isolation_findings([(probe_path, incident)])
        if not any("裸 DATABASE_URL" in item for item in incident_findings):
            offenders.append(
                f"事故形态未被判红：{probe_path.name} 去掉 ADMIN_ 前缀后仍放行 {incident_findings}"
            )

    # fail-closed：扫描面塌成 0 个文件必须报错，不能读成"全部干净"。
    try:
        _tracked_backend_tests("no_such_pattern_*.py")
    except AssertionError:
        pass
    else:
        offenders.append("扫描面为空时 _tracked_backend_tests 未报错（假绿灯风险）")

    if offenders:
        raise AssertionError("engine-building scripts must pin env file + ADMIN_DATABASE_URL + dialect gate: "
                             + "; ".join(offenders))


def _subprocess_calls_without_env(source: str) -> list:
    """返回 `subprocess.run/check_output/call/check_call(...)` 里没带 `env=` 的行号。"""
    offenders = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        names = []
        if isinstance(func, ast.Attribute):
            names.append(func.attr)
            if isinstance(func.value, ast.Name):
                names.append(func.value.id)
        elif isinstance(func, ast.Name):
            names.append(func.id)
        if not ({a for a in names} & {"run", "check_output", "call", "check_call"}) or "subprocess" not in names:
            continue
        if not any(keyword.arg == "env" for keyword in node.keywords):
            offenders.append(node.lineno)
    return offenders


def test_windows_utf8_acceptance_chain_is_self_enforced() -> None:
    """Windows UTF-8 验收链铁律（2026-10-02 裁定第 1 项）：取证脚本必须自带 UTF-8 契约。

    背景：`-X utf8` 曾被当成"运行命令前提"写进验收文档。父进程不强制 UTF-8 时，
    打印 `✓`/中文会 UnicodeEncodeError 崩掉整条链；子进程不传 `PYTHONUTF8` 时，
    被检 CLI 自己崩溃会把真实退出码顶掉——本轮实测出现过"子进程 exit 1、
    断言仍判通过"的假绿灯。因此这条契约必须由脚本自身承担，并有静态守卫防回归。
    """
    chain_files = (
        "tests/check_m5a_live_stack_readonly.py",
        "tests/check_m5a_live_execution.py",
        "tests/check_kb_migrations_smoke.py",
        "tests/run_unified_boundary_guards.py",
    )
    offenders = []
    for rel in chain_files:
        path = ROOT / rel
        source = path.read_text(encoding="utf-8")
        if 'sys.stdout.reconfigure(encoding="utf-8"' not in source:
            offenders.append(f"{rel} 缺少父进程 stdout 强制 UTF-8")
        if 'sys.stderr.reconfigure(encoding="utf-8"' not in source:
            offenders.append(f"{rel} 缺少父进程 stderr 强制 UTF-8")
        for lineno in _subprocess_calls_without_env(source):
            offenders.append(f"{rel}:{lineno} 子进程调用未传 env=（丢 PYTHONUTF8）")
        if "PYTHONUTF8" not in source and "subprocess.run(" in source:
            offenders.append(f"{rel} 起子进程但未设置 PYTHONUTF8")

    # 已登记 KB 的 CLI 检查必须看真实退出码（本轮假绿灯的直接回归位）
    readonly_src = (ROOT / "tests" / "check_m5a_live_stack_readonly.py").read_text(encoding="utf-8")
    if "proc.returncode == 0" not in readonly_src:
        offenders.append("tests/check_m5a_live_stack_readonly.py 的已登记 KB CLI 检查未断言 returncode == 0")

    # 守卫有效性自证：喂去势样本，规则必须变红（否则这是一条空规则）
    neutered_child = (
        "import subprocess, sys\n"
        'proc = subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, text=True)\n'
        "print(proc.returncode)\n"
    )
    if len(_subprocess_calls_without_env(neutered_child)) != 1:
        offenders.append("UTF-8 守卫未能识别无 env= 的子进程调用（规则是摆设）")
    if _subprocess_calls_without_env(
        "import subprocess\nsubprocess.run([1], env={'PYTHONUTF8': '1'})\n"
    ):
        offenders.append("UTF-8 守卫误报：带 env= 的子进程调用被判违规")

    if offenders:
        raise AssertionError("windows UTF-8 acceptance chain must be self-enforcing: " + "; ".join(offenders))


def main() -> int:
    test_nl2cypher_status_uses_current_config_service()
    test_config_constants_do_not_restore_openai_category()
    test_public_compat_registry_uses_compat_route_package()
    test_public_admin_compat_registry_uses_compat_route_package()
    test_admin_auth_and_jobs_public_routes_removed_from_python()
    test_admin_endpoint_modules_are_marked_public_retired()
    test_python_compatibility_helper_file_stays_removed()
    test_legacy_root_debug_helpers_stay_removed()
    test_old_admin_legacy_archive_removed()
    test_removed_legacy_shim_files_do_not_return()
    test_go_business_route_registration_stays_explicit()
    test_go_authz_does_not_call_python_authorize()
    test_core_delivery_docs_do_not_regress_to_old_workspace_or_smoke_baseline()
    test_linux_backend_tooling_uses_dot_venv_only()
    test_unified_dev_defaults_do_not_regress_to_remote_or_python_public()
    test_kb_scope_strict_mode_has_no_compat_toggle()
    test_sqlite_isolated_tests_use_env_file_not_blank_override()
    test_engine_building_scripts_pin_env_url_and_dialect()
    test_windows_utf8_acceptance_chain_is_self_enforced()
    print("MIGRATION_CLEANUP_GUARDS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
