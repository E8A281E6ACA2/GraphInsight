#!/usr/bin/env python3
"""Static guards for migration cleanup regressions."""

from __future__ import annotations

import ast
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


def test_sqlite_isolated_tests_use_env_file_not_blank_override() -> None:
    """DB 隔离铁律（任务 #55）：注入 sqlite 测试库的文件禁止把 env 覆盖变量置空。

    `GRAPHINSIGHT_BACKEND_ENV_FILE=""` 是伪隔离：admin/database.py 只在该变量指向
    存在的文件时才走隔离分支，空串落到 else 分支执行 load_dotenv(find_dotenv(),
    override=True)，沿子进程脚本 __file__ 命中 backend/.env，用其中的 PostgreSQL
    地址覆盖注入的 sqlite 地址——迁移测试的 rollback（DROP TABLE/COLUMN）就会打到
    开发库。唯一合法写法是把该变量指向真实存在的临时 env 文件。
    """
    tests_dir = ROOT / "tests"
    blank_markers = (
        'GRAPHINSIGHT_BACKEND_ENV_FILE"] = ""',
        'GRAPHINSIGHT_BACKEND_ENV_FILE", "")',
        "GRAPHINSIGHT_BACKEND_ENV_FILE'] = ''",
    )
    isolation_markers = (
        'GRAPHINSIGHT_BACKEND_ENV_FILE"] = str(',
        "GRAPHINSIGHT_BACKEND_ENV_FILE'] = str(",
        'GRAPHINSIGHT_BACKEND_ENV_FILE"] = f"',
    )
    offenders = []
    self_name = Path(__file__).name
    for path in sorted(tests_dir.glob("check_*.py")):
        if path.name == self_name:
            # 本守卫自身源码里就带着被禁字面量（用于构造匹配规则），不参与扫描
            continue
        source = path.read_text(encoding="utf-8")
        if "sqlite:///" not in source:
            continue
        for marker in blank_markers:
            if marker in source:
                offenders.append(f"{path.name} 置空 env 覆盖变量（伪隔离）: {marker}")
        if not any(marker in source for marker in isolation_markers):
            offenders.append(f"{path.name} 未把 env 覆盖变量指向真实 env 文件")
    if offenders:
        raise AssertionError("sqlite-isolated tests must point GRAPHINSIGHT_BACKEND_ENV_FILE at an existing env file: "
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
    test_windows_utf8_acceptance_chain_is_self_enforced()
    print("MIGRATION_CLEANUP_GUARDS_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
