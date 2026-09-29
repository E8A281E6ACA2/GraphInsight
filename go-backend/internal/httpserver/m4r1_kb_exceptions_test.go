package httpserver

// 复审整改项 1：KB 授权第二阶段必须无条件 fail-closed。
//
// 覆盖此前剩余的 4 类放行例外与专家点名的负向场景：
//   - 无主体（未认证、无法归因）→ 401 UNAUTHORIZED（含拒绝审计），不再随第一阶段
//     soft 放行获得任意 KB 范围（DocQA handler 级验证）；
//   - 授权服务未接入（adminStore 为 nil）→ 503 AUTHZ_UNAVAILABLE（soft 配置下验证）；
//   - store 不支持 KB 作用域解析（未实现 qaScopeAuthorizer）→ 503，enforce/soft 一律拒绝
//     （DocQA handler 级 + guard 直调验证）；
//   - AuthorizedKBIDs 解析失败 → 503；
//   - 交集为空（跨 KB）在 soft / local_jwt_soft 下依旧 403（DocQA / NL2Cypher handler 级）。
//
// 关键不变式：所有拒绝路径不得触达下游（Python orchestrator 上游绝不被调用）。

import (
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
	"graphinsight/go-backend/internal/orchestrator"
	"graphinsight/go-backend/internal/scope"
)

func newExceptionsLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func newExceptionsGuard(enforce bool, mode string, store adminPermissionStore) businessPermissionGuard {
	return newBusinessPermissionGuard(config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: enforce,
		RBACAuthzMode:          mode,
		AdminSecretKey:         "test-secret",
	}, newExceptionsLogger(), store)
}

func newQATargetForExceptions() *scope.SearchTarget {
	return &scope.SearchTarget{
		TenantID:  "tenant-a",
		ProjectID: "project-a",
		KBIDs:     []string{"kb-a"},
	}
}

// newPanicOrchestratorClient 构造一个拒绝路径绝不应触达的上游 client。
func newPanicOrchestratorClient(t *testing.T) *orchestrator.Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python orchestrator upstream must not be called on denied kb scope request, path=%s", r.URL.Path)
	}))
	t.Cleanup(srv.Close)
	client, err := orchestrator.New(config.Config{PythonBackendBaseURL: srv.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}
	return client
}

func newDocQAHandlerForExceptions(guard businessPermissionGuard, client *orchestrator.Client, logStore adminLogStore) http.HandlerFunc {
	logger := newExceptionsLogger()
	return guard.wrap("qa:ask", buildNativeDocQAHandler(logger, guard, client, nil, newOrchestratorMetrics(), logStore, &fakeAdminConfigStore{}, false))
}

func newNL2CypherHandlerForExceptions(guard businessPermissionGuard, client *orchestrator.Client, logStore adminLogStore) http.HandlerFunc {
	logger := newExceptionsLogger()
	return guard.wrap("nl2cypher:use", buildNativeNL2CypherGenerateHandler(logger, guard, client, nil, newOrchestratorMetrics(), logStore))
}

func newQAPostRequest(body string, withToken bool, token string) *http.Request {
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	if withToken {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	return req
}

func assertUnauthorizedScopeFailClosed(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 fail-closed, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "UNAUTHORIZED") {
		t.Fatalf("expected UNAUTHORIZED error code, got body=%s", rec.Body.String())
	}
}

func assertAuthzUnavailable(t *testing.T, rec *httptest.ResponseRecorder) {
	t.Helper()
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503 AUTHZ_UNAVAILABLE, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "AUTHZ_UNAVAILABLE") {
		t.Fatalf("expected AUTHZ_UNAVAILABLE error code, got body=%s", rec.Body.String())
	}
}

// --- authorizeQAEffectiveKBIDs：guard 级负向 ---

func TestAuthorizeQAEffectiveKBIDsEmptySubjectDeniedBothModes(t *testing.T) {
	t.Parallel()

	for _, tc := range []struct {
		name    string
		enforce bool
		mode    string
	}{
		{name: "soft_go_db", enforce: false, mode: "go_db"},
		{name: "enforce_go_db", enforce: true, mode: "go_db"},
		{name: "local_jwt_soft", enforce: false, mode: "local_jwt_soft"},
	} {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			store := &fakeQAScopeStore{
				fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
				allKBs:                   true,
			}
			guard := newExceptionsGuard(tc.enforce, tc.mode, store)
			logStore := &fakeAdminLogStore{}

			rec := httptest.NewRecorder()
			// 请求不带 x-auth-user-name：无法归因主体。
			req := httptest.NewRequest(http.MethodPost, "/api/docqa", nil)
			kbIDs, ok := guard.authorizeQAEffectiveKBIDs(rec, req, logStore, "qa:ask", newQATargetForExceptions())
			if ok || kbIDs != nil {
				t.Fatalf("expected denial for empty subject, got kbIDs=%v ok=%v", kbIDs, ok)
			}
			assertUnauthorizedScopeFailClosed(t, rec)
			if store.calledWithSubject != "" {
				t.Fatalf("AuthorizedKBIDs must not be called without subject, got %q", store.calledWithSubject)
			}
			if logStore.businessAuditReq.Status != "denied" || logStore.businessAuditReq.Action != "authz_denied" {
				t.Fatalf("expected denial audit, got %#v", logStore.businessAuditReq)
			}
		})
	}
}

func TestAuthorizeQAEffectiveKBIDsNilStoreDenied(t *testing.T) {
	t.Parallel()

	// soft 配置下 adminStore 为 nil（授权服务未接入）也必须 503，不得降级放行。
	guard := newExceptionsGuard(false, "go_db", nil)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", nil)
	req.Header.Set("x-auth-user-name", "user-a")

	kbIDs, ok := guard.authorizeQAEffectiveKBIDs(rec, req, &fakeAdminLogStore{}, "qa:ask", newQATargetForExceptions())
	if ok || kbIDs != nil {
		t.Fatalf("expected denial with nil store, got kbIDs=%v ok=%v", kbIDs, ok)
	}
	assertAuthzUnavailable(t, rec)
}

func TestAuthorizeQAEffectiveKBIDsUnsupportedInterfaceDenied(t *testing.T) {
	t.Parallel()

	// crossScopePermissionStore 只实现 CheckPermission，未实现 qaScopeAuthorizer。
	for _, enforce := range []bool{false, true} {
		enforce := enforce
		t.Run(map[bool]string{true: "enforce", false: "soft"}[enforce], func(t *testing.T) {
			t.Parallel()
			guard := newExceptionsGuard(enforce, "go_db", &crossScopePermissionStore{})
			rec := httptest.NewRecorder()
			req := httptest.NewRequest(http.MethodPost, "/api/docqa", nil)
			req.Header.Set("x-auth-user-name", "user-a")

			kbIDs, ok := guard.authorizeQAEffectiveKBIDs(rec, req, &fakeAdminLogStore{}, "qa:ask", newQATargetForExceptions())
			if ok || kbIDs != nil {
				t.Fatalf("expected denial for store without qaScopeAuthorizer (enforce=%v), got kbIDs=%v ok=%v", enforce, kbIDs, ok)
			}
			assertAuthzUnavailable(t, rec)
		})
	}
}

func TestAuthorizeQAEffectiveKBIDsResolveErrorDenied(t *testing.T) {
	t.Parallel()

	store := &fakeQAScopeStore{
		fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
		err:                      errors.New("db down"),
	}
	guard := newExceptionsGuard(false, "go_db", store)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", nil)
	req.Header.Set("x-auth-user-name", "admin@example.com")

	kbIDs, ok := guard.authorizeQAEffectiveKBIDs(rec, req, &fakeAdminLogStore{}, "qa:ask", newQATargetForExceptions())
	if ok || kbIDs != nil {
		t.Fatalf("expected denial when AuthorizedKBIDs errors, got kbIDs=%v ok=%v", kbIDs, ok)
	}
	assertAuthzUnavailable(t, rec)
}

// --- DocQA handler 级负向（拒绝路径不得触达 Python）---

func TestDocQAHandlerSoftSpoofedSubjectHeaderFailClosed(t *testing.T) {
	t.Parallel()

	guard := newExceptionsGuard(false, "go_db", &fakeQAScopeStore{
		fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
		allKBs:                   true,
	})
	req := newQAPostRequest(`{"question":"你好","kb_id":"kb-a"}`, false, "")
	req.Header.Set("x-auth-user-name", "admin@example.com")
	rec := httptest.NewRecorder()
	newDocQAHandlerForExceptions(guard, newPanicOrchestratorClient(t), &fakeAdminLogStore{}).ServeHTTP(rec, req)

	assertUnauthorizedScopeFailClosed(t, rec)
}

func TestDocQAHandlerSoftNoTokenFailClosed(t *testing.T) {
	t.Parallel()

	// RBACEnforceBusinessAPI=false 且无 token：第一阶段按既有语义 soft 放行，
	// 第二阶段因无法归因主体必须 401，且绝不调用 Python。
	guard := newExceptionsGuard(false, "go_db", &fakeQAScopeStore{
		fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
		allKBs:                   true,
	})
	logStore := &fakeAdminLogStore{}
	rec := httptest.NewRecorder()
	newDocQAHandlerForExceptions(guard, newPanicOrchestratorClient(t), logStore).
		ServeHTTP(rec, newQAPostRequest(`{"question":"你好","kb_id":"kb-a"}`, false, ""))

	assertUnauthorizedScopeFailClosed(t, rec)
	if logStore.businessAuditReq.Status != "denied" {
		t.Fatalf("expected denial audit on no-token fail-closed, got %#v", logStore.businessAuditReq)
	}
}

func TestDocQAHandlerSoftUnsupportedInterfaceDenied(t *testing.T) {
	t.Parallel()

	// store 不支持 KB 作用域解析：即使带合法 token，也必须 503 且不触达 Python。
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))
	guard := newExceptionsGuard(false, "go_db", &crossScopePermissionStore{})

	rec := httptest.NewRecorder()
	newDocQAHandlerForExceptions(guard, newPanicOrchestratorClient(t), &fakeAdminLogStore{}).
		ServeHTTP(rec, newQAPostRequest(`{"question":"你好","kb_id":"kb-a"}`, true, token))

	assertAuthzUnavailable(t, rec)
}

func TestDocQAHandlerLocalJWTSoftCrossKBDenied(t *testing.T) {
	t.Parallel()

	// local_jwt_soft：第一阶段软放行但主体已归因；第二阶段交集为空 → 403。
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))
	store := &fakeQAScopeStore{
		fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, User: "user-a"}},
		kbIDs:                    []string{"kb-b"},
	}
	guard := newExceptionsGuard(false, "local_jwt_soft", store)
	logStore := &fakeAdminLogStore{}

	rec := httptest.NewRecorder()
	newDocQAHandlerForExceptions(guard, newPanicOrchestratorClient(t), logStore).
		ServeHTTP(rec, newQAPostRequest(`{"question":"你好","kb_id":"kb-a"}`, true, token))

	assertForbiddenAccessDenied(t, rec)
	if store.calledWithPermission != "qa:ask" {
		t.Fatalf("expected qa:ask authorization lookup, got %q", store.calledWithPermission)
	}
	if logStore.businessAuditReq.Status != "denied" {
		t.Fatalf("expected denial audit, got %#v", logStore.businessAuditReq)
	}
}

// --- NL2Cypher handler 级负向 ---

func TestNL2CypherHandlerSoftCrossKBDenied(t *testing.T) {
	t.Parallel()

	token := issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour))
	store := &fakeQAScopeStore{
		fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
		kbIDs:                    []string{"kb-b"},
	}
	guard := newExceptionsGuard(false, "go_db", store)
	logStore := &fakeAdminLogStore{}

	req := httptest.NewRequest(http.MethodPost, "/api/nl2cypher", strings.NewReader(`{"natural_language":"查一下实体","kb_id":"kb-a"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+token)

	rec := httptest.NewRecorder()
	newNL2CypherHandlerForExceptions(guard, newPanicOrchestratorClient(t), logStore).ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if store.calledWithPermission != "nl2cypher:use" {
		t.Fatalf("expected nl2cypher:use authorization lookup, got %q", store.calledWithPermission)
	}
	if logStore.businessAuditReq.Status != "denied" {
		t.Fatalf("expected denial audit, got %#v", logStore.businessAuditReq)
	}
}

func TestNL2CypherHandlerSoftNoTokenFailClosed(t *testing.T) {
	t.Parallel()

	guard := newExceptionsGuard(false, "go_db", &fakeQAScopeStore{
		fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
		allKBs:                   true,
	})
	logStore := &fakeAdminLogStore{}

	req := httptest.NewRequest(http.MethodPost, "/api/nl2cypher", strings.NewReader(`{"natural_language":"查一下实体","kb_id":"kb-a"}`))
	req.Header.Set("Content-Type", "application/json")

	rec := httptest.NewRecorder()
	newNL2CypherHandlerForExceptions(guard, newPanicOrchestratorClient(t), logStore).ServeHTTP(rec, req)

	assertUnauthorizedScopeFailClosed(t, rec)
}

// --- checkPermissionWithScope：无主体 / 无 store 一律拒绝 ---

func TestCheckPermissionWithScopeEmptySubjectDenied(t *testing.T) {
	t.Parallel()

	for _, enforce := range []bool{false, true} {
		enforce := enforce
		t.Run(map[bool]string{true: "enforce", false: "soft"}[enforce], func(t *testing.T) {
			t.Parallel()
			guard := newExceptionsGuard(enforce, "go_db", &crossScopePermissionStore{})
			req := httptest.NewRequest(http.MethodGet, "/api/graph/schema", nil)
			if guard.checkPermissionWithScope(req, "graph:read", map[string]string{"x-kb-id": "kb-b"}) {
				t.Fatalf("expected denial for empty subject under enforce=%v", enforce)
			}
		})
	}
}

func TestCheckPermissionWithScopeNilStoreDenied(t *testing.T) {
	t.Parallel()

	// 纯本地无库 soft 语义不再豁免 KB 边界。
	guard := newExceptionsGuard(false, "go_db", nil)
	req := httptest.NewRequest(http.MethodGet, "/api/graph/schema", nil)
	req.Header.Set("x-auth-user-name", "user-a")
	if guard.checkPermissionWithScope(req, "graph:read", map[string]string{"x-kb-id": "kb-b"}) {
		t.Fatalf("expected denial with nil store under soft")
	}
}

func TestGraphSchemaRouteSoftNoTokenFailClosed(t *testing.T) {
	t.Parallel()

	// soft 配置、无 token 访问 KB-scoped 图谱接口：不得回退为放行读取。
	graphSvc := &stubGraphService{schema: graph.GraphSchemaResponse{Labels: []graph.GraphLabelSummary{{Label: "Entity"}}}}
	guard := newExceptionsGuard(false, "go_db", &crossScopePermissionStore{})

	mux := http.NewServeMux()
	registerNativeGraphRoutes(mux, newExceptionsLogger(), graphSvc, nil, guard, &fakeAdminLogStore{}, newForeignKBBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/graph/schema", nil)
	req.Header.Set("x-kb-id", "kb-b")
	mux.ServeHTTP(rec, req)

	assertForbiddenAccessDenied(t, rec)
	if graphSvc.discoverSchemaCalls != 0 {
		t.Fatalf("DiscoverSchema must not be called when subject cannot be attributed, got %d", graphSvc.discoverSchemaCalls)
	}
}
