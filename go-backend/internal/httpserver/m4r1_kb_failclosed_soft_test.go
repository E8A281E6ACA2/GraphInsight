package httpserver

// M4-R1 审计阻断项 P0（soft 放行收紧）：KB-scoped 路由必须独立 fail-closed。
//
// 即使 RBACEnforceBusinessAPI=false 或 authz mode=local_jwt_soft，
// 已认证主体访问其未授权项目下的 KB 也必须被拒绝，且拒绝路径不得触达下游：
//   - 检索诊断不得调用 Python；
//   - QA trace 列表/详情不得调用 trace store；
//   - 图谱读取不得调用 Graph 服务；
//   - 任务读取不得越过 KB 归属校验。

import (
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
)

// newSoftGoDBCrossScopeGuard 构造 go_db 模式但未启用强制（soft）的守卫。
func newSoftGoDBCrossScopeGuard(store *crossScopePermissionStore) businessPermissionGuard {
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: false,
		RBACAuthzMode:          "go_db",
		AdminSecretKey:         "test-secret",
	}
	return newBusinessPermissionGuard(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)), store)
}

// newLocalJWTSoftCrossScopeGuard 构造 local_jwt_soft 模式的守卫。
func newLocalJWTSoftCrossScopeGuard(store *crossScopePermissionStore) businessPermissionGuard {
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: false,
		RBACAuthzMode:          "local_jwt_soft",
		AdminSecretKey:         "test-secret",
	}
	return newBusinessPermissionGuard(cfg, slog.New(slog.NewTextHandler(io.Discard, nil)), store)
}

func TestSoftGoDBRetrievalDiagnosticsCrossKBDenied(t *testing.T) {
	t.Parallel()

	permStore := &crossScopePermissionStore{}
	guard := newSoftGoDBCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called when cross-kb diagnostics denied under soft config")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, newCrossScopeKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/admin/qa/retrieval-diagnostics",
		strings.NewReader(`{"question":"cross scope under soft?","kb_id":"kb-b"}`),
	)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if permStore.lastPermission != "qa:ask" {
		t.Fatalf("expected qa:ask permission check under soft, got %q", permStore.lastPermission)
	}
}

func TestLocalJWTSoftRetrievalDiagnosticsCrossKBDenied(t *testing.T) {
	t.Parallel()

	permStore := &crossScopePermissionStore{}
	guard := newLocalJWTSoftCrossScopeGuard(permStore)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client must not be called when cross-kb diagnostics denied under local_jwt_soft")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, newCrossScopeKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/admin/qa/retrieval-diagnostics",
		strings.NewReader(`{"question":"cross scope under local_jwt_soft?","kb_id":"kb-b"}`),
	)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if permStore.lastPermission != "qa:ask" {
		t.Fatalf("expected qa:ask permission check under local_jwt_soft, got %q", permStore.lastPermission)
	}
}

func TestSoftGoDBQATracesListCrossKBDenied(t *testing.T) {
	t.Parallel()

	permStore := &crossScopePermissionStore{}
	guard := newSoftGoDBCrossScopeGuard(permStore)
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
	// 拒绝必须先于 store 查询：ListQATraces 未被调用。
	if traceStore.listQuery.KBID != "" {
		t.Fatalf("ListQATraces must not be invoked on cross-kb denial under soft, got query %+v", traceStore.listQuery)
	}
	if permStore.lastPermission != "monitor:read" {
		t.Fatalf("expected monitor:read permission check, got %q", permStore.lastPermission)
	}
}

func TestLocalJWTSoftQATracesDetailCrossKBDenied(t *testing.T) {
	t.Parallel()

	permStore := &crossScopePermissionStore{}
	guard := newLocalJWTSoftCrossScopeGuard(permStore)
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
		t.Fatalf("GetQATrace must not be invoked on cross-kb denial under local_jwt_soft, got key %q", traceStore.detailKey)
	}
}

func TestSoftGoDBGraphSchemaCrossKBDenied(t *testing.T) {
	t.Parallel()

	store := &crossScopePermissionStore{}
	guard := newSoftGoDBCrossScopeGuard(store)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))
	graphSvc := &stubGraphService{schema: graph.GraphSchemaResponse{Labels: []graph.GraphLabelSummary{{Label: "Entity"}}}}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerNativeGraphRoutes(mux, logger, graphSvc, nil, guard, &fakeAdminLogStore{}, newForeignKBBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/graph/schema", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("x-kb-id", "kb-b")
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if graphSvc.discoverSchemaCalls != 0 {
		t.Fatalf("DiscoverSchema must not be called when cross-kb denied under soft, got %d", graphSvc.discoverSchemaCalls)
	}
}

func TestSoftGoDBAuthorizeJobKBReadScopeCrossKBDenied(t *testing.T) {
	t.Parallel()

	store := &crossScopePermissionStore{}
	guard := newSoftGoDBCrossScopeGuard(store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/jobs?kb_id=kb-b", nil)
	req.Header.Set("x-auth-user-name", "user-a")

	item, ok := authorizeJobKBReadScope(rec, req, slog.New(slog.NewTextHandler(io.Discard, nil)), guard, newForeignKBBStore(), "kb-b")
	if ok {
		t.Fatalf("expected denial for cross-kb job read under soft, got item %+v", item)
	}
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403 under soft, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "KB_ACCESS_DENIED") {
		t.Fatalf("expected KB_ACCESS_DENIED, got body=%s", rec.Body.String())
	}
}
