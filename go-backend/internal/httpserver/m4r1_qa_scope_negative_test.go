package httpserver

// M4-R1 审计阻断项 P0-1 / P0-2 的跨 KB 负向测试。
//
// 场景：主体仅被授予 project-a，却尝试访问 project-b 下的 kb-b。
//   - 第一阶段 guard.wrap 只看请求头作用域（只带 kb、不带 project）会放行；
//   - 第二阶段 authorizeQAKBScopedRequest 加载 kb-b 的权威行 {tenant-b, project-b, kb-b}，
//     以真实作用域执行权限求交，命中未授权项目 -> 403 KB_ACCESS_DENIED。
//
// 关键不变式：拒绝路径不得触达下游（Python 检索诊断、QA trace store 均不得被调用）。

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
)

// newCrossScopeKBStore 返回归属 tenant-b/project-b 的 kb-b（主体未授权的项目）。
func newCrossScopeKBStore() *fakeAdminQATraceStore {
	return &fakeAdminQATraceStore{
		kbRow: adminstore.KnowledgeBaseItem{
			ID: "kb-b", TenantID: "tenant-b", ProjectID: "project-b",
			Status: adminstore.KBStatusActive, StoragePrefix: "tenant-b/project-b/kb-b",
		},
	}
}

func newEnforceCrossScopeGuard(store *crossScopePermissionStore) businessPermissionGuard {
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: true,
		RBACAuthzMode:          "go_db",
		AdminSecretKey:         "test-secret",
	}
	return newBusinessPermissionGuard(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)), store)
}

func TestRetrievalDiagnosticsCrossProjectDenied(t *testing.T) {
	t.Parallel()

	permStore := &crossScopePermissionStore{}
	guard := newEnforceCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	// 拒绝路径绝不应触达 Python：任何上游调用直接失败。
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called when cross-project diagnostics denied")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	kbStore := newCrossScopeKBStore()
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, kbStore)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/admin/qa/retrieval-diagnostics",
		strings.NewReader(`{"question":"cross scope?","kb_id":"kb-b"}`),
	)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if permStore.lastPermission != "qa:ask" {
		t.Fatalf("expected qa:ask permission check, got %q", permStore.lastPermission)
	}
}

func TestRetrievalDiagnosticsDeclaredProjectMismatchDenied(t *testing.T) {
	t.Parallel()

	// 攻击路径：声明 x-project-id: project-a，却指向 project-b 下的 kb-b。
	// KB 权威行校验应先于权限求交命中 -> KB_CROSS_SCOPE。
	permStore := &crossScopePermissionStore{}
	guard := newEnforceCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called when declared scope mismatches kb row")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, newCrossScopeKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/admin/qa/retrieval-diagnostics",
		strings.NewReader(`{"question":"cross scope?","kb_id":"kb-b"}`),
	)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("x-project-id", "project-a")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 KB_CROSS_SCOPE, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "KB_CROSS_SCOPE") {
		t.Fatalf("expected KB_CROSS_SCOPE, got body=%s", rec.Body.String())
	}
}

func TestRetrievalDiagnosticsMissingScopeDenied(t *testing.T) {
	t.Parallel()

	// 缺 kb 作用域时必须拒绝，不得回退全局检索，也不得触达 Python。
	permStore := &crossScopePermissionStore{}
	guard := newEnforceCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called when kb scope missing")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, newCrossScopeKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/admin/qa/retrieval-diagnostics",
		strings.NewReader(`{"question":"no scope"}`),
	)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 KB_SCOPE_REQUIRED, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "KB_SCOPE_REQUIRED") {
		t.Fatalf("expected KB_SCOPE_REQUIRED, got body=%s", rec.Body.String())
	}
}

func TestQATracesListCrossProjectDenied(t *testing.T) {
	t.Parallel()

	permStore := &crossScopePermissionStore{}
	guard := newEnforceCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called for native qa traces route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	traceStore := newCrossScopeKBStore()
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, traceStore)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/qa-traces?kb_id=kb-b", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	// 拒绝发生在 store 查询之前：ListQATraces 未被调用（listQuery 保持零值）。
	if traceStore.listQuery.KBID != "" {
		t.Fatalf("ListQATraces must not be invoked on cross-project denial, got query %+v", traceStore.listQuery)
	}
	if permStore.lastPermission != "monitor:read" {
		t.Fatalf("expected monitor:read permission check, got %q", permStore.lastPermission)
	}
}

func TestQATracesDetailCrossProjectDenied(t *testing.T) {
	t.Parallel()

	permStore := &crossScopePermissionStore{}
	guard := newEnforceCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called for native qa traces route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	traceStore := newCrossScopeKBStore()
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, traceStore)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/qa-traces/trace-9?kb_id=kb-b", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if traceStore.detailKey != "" {
		t.Fatalf("GetQATrace must not be invoked on cross-project denial, got key %q", traceStore.detailKey)
	}
}

func TestQATracesListMissingScopeDenied(t *testing.T) {
	t.Parallel()

	// 缺 kb 作用域时 QA trace 列表必须拒绝，避免全局读取。
	permStore := &crossScopePermissionStore{}
	guard := newEnforceCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called for native qa traces route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	traceStore := newCrossScopeKBStore()
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, traceStore)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/qa-traces", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 KB_SCOPE_REQUIRED, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "KB_SCOPE_REQUIRED") {
		t.Fatalf("expected KB_SCOPE_REQUIRED, got body=%s", rec.Body.String())
	}
	if traceStore.listQuery.KBID != "" {
		t.Fatalf("ListQATraces must not be invoked when scope missing, got query %+v", traceStore.listQuery)
	}
}
