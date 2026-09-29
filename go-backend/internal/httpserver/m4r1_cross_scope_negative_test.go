package httpserver

// M4-R1 修复批次的跨项目授权负向测试（审计 P0-2 / P0-3）。
//
// 场景：用户仅被授权 project-a，但请求指向 project-b 下的 kb-b。
// 期望：普通图谱只读接口与任务读取都返回 403 KB_ACCESS_DENIED，
// 且在鉴权失败时不触达下游 Neo4j 服务（不执行任何 Cypher）。
//
// 第一阶段 guard.wrap 用请求头作用域鉴权（请求只带 kb，不带 project，放行）；
// 第二阶段 authorizeGraphKBReadScope / authorizeJobKBReadScope 用 KB 行的权威
// {tenant, project, kb} 重新求交，命中未授权项目 -> 拒绝。

import (
	"context"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
)

// crossScopePermissionStore 模拟 RBAC：主体只被授予 project-a。
// 任何指向其它项目的资源作用域都返回 Allowed=false。
type crossScopePermissionStore struct {
	calls          int
	lastPermission string
	lastScope      map[string]string
}

func (s *crossScopePermissionStore) CheckPermission(_ context.Context, subject string, permission string, scope map[string]string) (authz.CheckResult, error) {
	s.calls++
	s.lastPermission = permission
	s.lastScope = scope
	projectID := strings.TrimSpace(scope["x-project-id"])
	if projectID != "" && projectID != "project-a" {
		return authz.CheckResult{Allowed: false, Reason: "cross-project", User: subject}, nil
	}
	return authz.CheckResult{Allowed: true, User: subject}, nil
}

// newForeignKBBStore 返回一个 KB 存储：kb-b 归属 tenant-b/project-b（用户未授权的项目）。
func newForeignKBBStore() *fakeUnifiedGraphBuildStore {
	return &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			kbRow: adminstore.KnowledgeBaseItem{
				ID: "kb-b", TenantID: "tenant-b", ProjectID: "project-b",
				Status: adminstore.KBStatusActive, StoragePrefix: "tenant-b/project-b/kb-b",
			},
		},
	}
}

func newCrossScopeGuard(t *testing.T, store *crossScopePermissionStore) (businessPermissionGuard, string) {
	t.Helper()
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: true,
		RBACAuthzMode:          "go_db",
		AdminSecretKey:         "test-secret",
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))
	return newBusinessPermissionGuard(cfg, logger, store), token
}

func assertForbiddenAccessDenied(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "KB_ACCESS_DENIED") {
		t.Fatalf("expected KB_ACCESS_DENIED, got body=%s", rec.Body.String())
	}
}

func TestGraphSchemaRouteCrossProjectDenied(t *testing.T) {
	t.Parallel()

	store := &crossScopePermissionStore{}
	guard, token := newCrossScopeGuard(t, store)
	graphSvc := &stubGraphService{schema: graph.GraphSchemaResponse{Labels: []graph.GraphLabelSummary{{Label: "Entity"}}}}

	mux := http.NewServeMux()
	registerNativeGraphRoutes(mux, slog.New(slog.NewTextHandler(io.Discard, nil)), graphSvc, nil, guard, &fakeAdminLogStore{}, newForeignKBBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/graph/schema", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("x-kb-id", "kb-b")
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if graphSvc.discoverSchemaCalls != 0 {
		t.Fatalf("DiscoverSchema must not be called when cross-project denied, got %d", graphSvc.discoverSchemaCalls)
	}
	if store.lastPermission != "graph:read" {
		t.Fatalf("expected graph:read permission check, got %q", store.lastPermission)
	}
}

func TestGraphNodeDetailRouteCrossProjectDenied(t *testing.T) {
	t.Parallel()

	store := &crossScopePermissionStore{}
	guard, token := newCrossScopeGuard(t, store)
	graphSvc := &stubGraphService{nodeDetail: graph.NodeDetail{ID: "42"}}

	mux := http.NewServeMux()
	registerNativeGraphRoutes(mux, slog.New(slog.NewTextHandler(io.Discard, nil)), graphSvc, nil, guard, &fakeAdminLogStore{}, newForeignKBBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/node/42", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("x-kb-id", "kb-b")
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if graphSvc.getNodeDetailCalls != 0 {
		t.Fatalf("GetNodeDetail must not be called when cross-project denied, got %d", graphSvc.getNodeDetailCalls)
	}
}

func TestGraphExpandRouteCrossProjectDenied(t *testing.T) {
	t.Parallel()

	store := &crossScopePermissionStore{}
	guard, token := newCrossScopeGuard(t, store)
	graphSvc := &stubGraphService{}

	mux := http.NewServeMux()
	registerNativeGraphRoutes(mux, slog.New(slog.NewTextHandler(io.Discard, nil)), graphSvc, nil, guard, &fakeAdminLogStore{}, newForeignKBBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/expand", strings.NewReader(`{"nodeId":"42","kb_id":"kb-b"}`))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if graphSvc.expandNodeCalls != 0 {
		t.Fatalf("ExpandNode must not be called when cross-project denied, got %d", graphSvc.expandNodeCalls)
	}
}

func TestAuthorizeGraphKBReadScopeMissingDenied(t *testing.T) {
	t.Parallel()

	// 缺少 kb 作用域时，普通图谱接口必须拒绝，而不是回退全局查询。
	store := &crossScopePermissionStore{}
	guard, _ := newCrossScopeGuard(t, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/graph/schema", nil)
	item, ok := authorizeGraphKBReadScope(rec, req, slog.New(slog.NewTextHandler(io.Discard, nil)), guard, newForeignKBBStore(), "")
	if ok {
		t.Fatalf("expected denial for missing kb scope, got item %+v", item)
	}
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 KB_SCOPE_REQUIRED, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "KB_SCOPE_REQUIRED") {
		t.Fatalf("expected KB_SCOPE_REQUIRED, got body=%s", rec.Body.String())
	}
}

func TestAuthorizeJobKBReadScopeCrossProjectDenied(t *testing.T) {
	t.Parallel()

	store := &crossScopePermissionStore{}
	guard, _ := newCrossScopeGuard(t, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/jobs?kb_id=kb-b", nil)
	// 模拟第一阶段 guard.wrap 已传播的认证主体。
	req.Header.Set("x-auth-user-name", "user-a")

	item, ok := authorizeJobKBReadScope(rec, req, slog.New(slog.NewTextHandler(io.Discard, nil)), guard, newForeignKBBStore(), "kb-b")
	if ok {
		t.Fatalf("expected denial for cross-project job read, got item %+v", item)
	}
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "KB_ACCESS_DENIED") {
		t.Fatalf("expected KB_ACCESS_DENIED, got body=%s", rec.Body.String())
	}
	if store.lastPermission != "job:read" {
		t.Fatalf("expected job:read permission check, got %q", store.lastPermission)
	}
}
