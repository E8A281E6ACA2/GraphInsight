package httpserver

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/scope"
)

// TestResolveKBParentScopeSources 是 FIX #1 的负向纯函数测试（契约 §3.2）：
// body 父作用域与授权用的 header 作用域不一致必须返回 KB_CROSS_SCOPE，
// 缺失返回 KB_SCOPE_REQUIRED；项目越权（authorized tenant-a/project-a
// vs request tenant-b/project-b）在进入 store 之前就被解析层拒绝。
func TestResolveKBParentScopeSources(t *testing.T) {
	t.Parallel()

	tests := []struct {
		name          string
		headerTenant  string
		headerProject string
		queryTenant   string
		queryProject  string
		bodyTenant    string
		bodyProject   string
		wantTenant    string
		wantProject   string
		wantCode      string
	}{
		{
			name:        "missing everywhere is scope required",
			wantCode:    scope.CodeScopeRequired,
			wantTenant:  "",
			wantProject: "",
		},
		{
			name:        "body only scope resolves to body values",
			bodyTenant:  "tenant-b",
			bodyProject: "project-b",
			wantTenant:  "tenant-b",
			wantProject: "project-b",
		},
		{
			name:         "consistent sources merge",
			headerTenant: "tenant-a",
			queryProject: "project-a",
			bodyTenant:   "tenant-a",
			bodyProject:  "project-a",
			wantTenant:   "tenant-a",
			wantProject:  "project-a",
		},
		{
			name:          "body tenant differs from header tenant is cross scope",
			headerTenant:  "tenant-a",
			headerProject: "project-a",
			bodyTenant:    "tenant-b",
			bodyProject:   "project-a",
			wantCode:      scope.CodeCrossScope,
		},
		{
			// 项目越权：调用方被授权 tenant-a/project-a，body 却指向 tenant-b/project-b。
			name:          "project escalation body differs from header is cross scope",
			headerTenant:  "tenant-a",
			headerProject: "project-a",
			bodyTenant:    "tenant-b",
			bodyProject:   "project-b",
			wantCode:      scope.CodeCrossScope,
		},
		{
			name:         "query project differs from body project is cross scope",
			headerTenant: "tenant-a",
			queryProject: "project-a",
			bodyTenant:   "tenant-a",
			bodyProject:  "project-b",
			wantCode:     scope.CodeCrossScope,
		},
	}

	for _, tc := range tests {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tenantID, projectID, err := resolveKBParentScopeSources(
				tc.headerTenant, tc.headerProject,
				tc.queryTenant, tc.queryProject,
				tc.bodyTenant, tc.bodyProject,
			)
			if tc.wantCode != "" {
				if err == nil || err.Code != tc.wantCode {
					t.Fatalf("expected error %s, got %v (tenant=%q project=%q)", tc.wantCode, err, tenantID, projectID)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if tenantID != tc.wantTenant || projectID != tc.wantProject {
				t.Fatalf("expected (%q,%q), got (%q,%q)", tc.wantTenant, tc.wantProject, tenantID, projectID)
			}
		})
	}
}

// recordingKBStore 记录 store 调用，用于断言“拒绝发生在任何 store 调用之前”。
type recordingKBStore struct {
	listCalled   bool
	createCalled bool
	createReq    adminstore.KBCreateRequest
}

func (s *recordingKBStore) ListKnowledgeBases(ctx context.Context, query adminstore.KBListQuery) (adminstore.KBListResult, error) {
	s.listCalled = true
	return adminstore.KBListResult{}, nil
}

func (s *recordingKBStore) GetKnowledgeBase(ctx context.Context, kbID string) (adminstore.KnowledgeBaseItem, error) {
	return adminstore.KnowledgeBaseItem{}, adminstore.ErrKBNotFound
}

func (s *recordingKBStore) CreateKnowledgeBase(ctx context.Context, req adminstore.KBCreateRequest) (adminstore.KnowledgeBaseItem, error) {
	s.createCalled = true
	s.createReq = req
	return adminstore.KnowledgeBaseItem{
		ID: req.ID, TenantID: req.TenantID, ProjectID: req.ProjectID,
		Name: req.Name, Status: adminstore.KBStatusActive, StoragePrefix: req.StoragePrefix,
	}, nil
}

func (s *recordingKBStore) UpdateKnowledgeBase(ctx context.Context, req adminstore.KBUpdateRequest) (adminstore.KnowledgeBaseItem, error) {
	return adminstore.KnowledgeBaseItem{}, adminstore.ErrKBNotFound
}

func (s *recordingKBStore) ArchiveKnowledgeBase(ctx context.Context, req adminstore.KBArchiveRequest) (adminstore.KnowledgeBaseItem, error) {
	return adminstore.KnowledgeBaseItem{}, adminstore.ErrKBNotFound
}

func (s *recordingKBStore) DeleteKnowledgeBase(ctx context.Context, req adminstore.KBDeleteRequest) (adminstore.KnowledgeBaseItem, error) {
	return adminstore.KnowledgeBaseItem{}, adminstore.ErrKBNotFound
}

func newKBAuthzTestGuard(t *testing.T, store *fakeAdminPermissionStore) businessPermissionGuard {
	t.Helper()
	return newBusinessPermissionGuard(config.Config{
		RBACEnforceBusinessAPI: true,
		RBACAuthzMode:          "go_db",
		AdminSecretKey:         "test-secret",
	}, newDiscardLogger(), store)
}

// newKBCreateRequest 构造 POST /api/v1/admin/knowledge-bases 请求并执行 handler
// （header 作用域与 body JSON 由调用方指定）。
func newKBCreateRequest(t *testing.T, guard businessPermissionGuard, kbStore adminKBStore, headerTenant, headerProject, body string) *httptest.ResponseRecorder {
	t.Helper()
	handler := buildAdminKBCreateNativeHandler(newDiscardLogger(), guard, kbStore, nil)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/knowledge-bases", strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour)))
	if headerTenant != "" {
		req.Header.Set("X-Tenant-Id", headerTenant)
	}
	if headerProject != "" {
		req.Header.Set("X-Project-Id", headerProject)
	}
	rec := httptest.NewRecorder()
	handler(rec, req)
	return rec
}

func decodeKBError(t *testing.T, rec *httptest.ResponseRecorder) string {
	t.Helper()
	var body struct {
		Data struct {
			ErrorCode string `json:"error_code"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode response failed: %v", err)
	}
	return body.Data.ErrorCode
}

// TestKBCreateBodyScopeSmugglingRejected 证明：body 父作用域与 header（授权来源）
// 不一致时，请求在进入任何 store 调用之前被 KB_CROSS_SCOPE 拒绝（FIX #1）。
func TestKBCreateBodyScopeSmugglingRejected(t *testing.T) {
	t.Parallel()

	store := &fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, Reason: "allowed", UserID: 1, User: "admin"}}
	guard := newKBAuthzTestGuard(t, store)
	kbStore := &recordingKBStore{}

	rec := newKBCreateRequest(t, guard, kbStore, "tenant-a", "project-a",
		`{"name":"smuggled","tenant_id":"tenant-b","project_id":"project-b"}`)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if code := decodeKBError(t, rec); code != scope.CodeCrossScope {
		t.Fatalf("expected error_code %s, got %q", scope.CodeCrossScope, code)
	}
	if kbStore.createCalled {
		t.Fatalf("CreateKnowledgeBase must not be called when body scope differs from authorized header scope")
	}
}

// TestKBCreateAuthorizationReceivesBodyScope 证明：授权（CheckPermission）收到的
// scope 是合并 body 后的有效父作用域——body-only 请求以 body 作用域被鉴权，
// 项目越权请求在 store 调用前被拒（FIX #1 ordering）。
func TestKBCreateAuthorizationReceivesBodyScope(t *testing.T) {
	t.Run("body-only scope is used for authorization", func(t *testing.T) {
		t.Parallel()
		store := &fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, Reason: "allowed", UserID: 1, User: "admin"}}
		guard := newKBAuthzTestGuard(t, store)
		kbStore := &recordingKBStore{}

		rec := newKBCreateRequest(t, guard, kbStore, "", "",
			`{"name":"kb","tenant_id":"tenant-b","project_id":"project-b"}`)

		if rec.Code != http.StatusCreated {
			t.Fatalf("expected 201, got %d body=%s", rec.Code, rec.Body.String())
		}
		if got := store.scope["x-tenant-id"]; got != "tenant-b" {
			t.Fatalf("expected CheckPermission to receive body tenant scope tenant-b, got %q", got)
		}
		if got := store.scope["x-project-id"]; got != "project-b" {
			t.Fatalf("expected CheckPermission to receive body project scope project-b, got %q", got)
		}
		if !kbStore.createCalled || kbStore.createReq.TenantID != "tenant-b" || kbStore.createReq.ProjectID != "project-b" {
			t.Fatalf("expected create to target body scope, got %+v called=%v", kbStore.createReq, kbStore.createCalled)
		}
	})

	t.Run("project escalation denied before store call", func(t *testing.T) {
		t.Parallel()
		// 调用方仅被授权 tenant-a/project-a（模拟：CheckPermission 对 project-a 拒绝），
		// 请求作用域一致地指向 tenant-a/project-a → 授权在该作用域上执行并拒绝。
		store := &fakeAdminPermissionStore{result: authz.CheckResult{Allowed: false, Reason: "scope_mismatch"}}
		guard := newKBAuthzTestGuard(t, store)
		kbStore := &recordingKBStore{}

		rec := newKBCreateRequest(t, guard, kbStore, "tenant-a", "project-a",
			`{"name":"esc","tenant_id":"tenant-a","project_id":"project-a"}`)

		if rec.Code != http.StatusForbidden {
			t.Fatalf("expected 403, got %d body=%s", rec.Code, rec.Body.String())
		}
		if got := store.scope["x-project-id"]; got != "project-a" {
			t.Fatalf("expected authorization evaluated against effective project scope project-a, got %q", got)
		}
		if kbStore.createCalled {
			t.Fatalf("CreateKnowledgeBase must not be called when authorization denies the effective parent scope")
		}
	})

	t.Run("project escalation via body never reaches authorization or store", func(t *testing.T) {
		t.Parallel()
		// body project 与 header 不一致：作用域解析层在授权之前就拒绝（KB_CROSS_SCOPE），
		// CheckPermission 不得被调用，store 更不会被触碰（FIX #1 ordering）。
		store := &fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, Reason: "allowed", UserID: 1, User: "admin"}}
		guard := newKBAuthzTestGuard(t, store)
		kbStore := &recordingKBStore{}

		rec := newKBCreateRequest(t, guard, kbStore, "tenant-a", "project-a",
			`{"name":"esc","tenant_id":"tenant-a","project_id":"project-b"}`)

		if rec.Code != http.StatusBadRequest {
			t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
		}
		if code := decodeKBError(t, rec); code != scope.CodeCrossScope {
			t.Fatalf("expected error_code %s, got %q", scope.CodeCrossScope, code)
		}
		if store.subject != "" {
			t.Fatalf("CheckPermission must not run when scope resolution fails, got subject %q", store.subject)
		}
		if kbStore.createCalled {
			t.Fatalf("CreateKnowledgeBase must not be called on cross-scope rejection")
		}
	})
}

// TestKBListAuthorizationUsesResolvedParentScope 证明：列表授权同样以解析后的
// 父作用域执行（query 提供的父作用域进入 CheckPermission）。
func TestKBListAuthorizationUsesResolvedParentScope(t *testing.T) {
	t.Parallel()

	store := &fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, Reason: "allowed", UserID: 1, User: "admin"}}
	guard := newKBAuthzTestGuard(t, store)
	kbStore := &recordingKBStore{}

	handler := buildAdminKBListNativeHandler(newDiscardLogger(), guard, kbStore)
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/knowledge-bases?tenant_id=tenant-a&project_id=project-a", nil)
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour)))
	rec := httptest.NewRecorder()
	handler(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if got := store.scope["x-tenant-id"]; got != "tenant-a" {
		t.Fatalf("expected CheckPermission to receive tenant-a, got %q", got)
	}
	if got := store.scope["x-project-id"]; got != "project-a" {
		t.Fatalf("expected CheckPermission to receive project-a, got %q", got)
	}
	if !kbStore.listCalled {
		t.Fatalf("expected store list to be called")
	}
}
