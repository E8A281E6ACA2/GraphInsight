package httpserver

// 业务面 KB 目录（GET /api/knowledge-bases）单测（M4-R1 步骤 3）。
// 门控口径：合法 JWT + AuthorizedKBIDs(subject, "graph:read") 已解析集合；
// 端点自身不做授权判断，也不允许调用方传入作用域过滤参数。

import (
	"context"
	"encoding/json"
	"errors"
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

type fakeWorkspaceKBStore struct {
	authorizedKBIDs []string
	allKBs          bool
	authorizedErr   error
	listErr         error

	authorizedCalls        int
	lastAuthorizedSubject  string
	lastAuthorizedPerms    []string
	listCalls              int
	lastListKBIDs          []string
	lastListAllKBs         bool
	lastListAllKBsCaptured bool

	items []adminstore.KnowledgeBaseItem
}

func (s *fakeWorkspaceKBStore) AuthorizedKBIDs(_ context.Context, subject string, permissions ...string) ([]string, bool, error) {
	s.authorizedCalls++
	s.lastAuthorizedSubject = subject
	s.lastAuthorizedPerms = permissions
	if s.authorizedErr != nil {
		return nil, false, s.authorizedErr
	}
	return s.authorizedKBIDs, s.allKBs, nil
}

func (s *fakeWorkspaceKBStore) ListAuthorizedKnowledgeBases(_ context.Context, kbIDs []string, allKBs bool) ([]adminstore.KnowledgeBaseItem, error) {
	s.listCalls++
	s.lastListKBIDs = kbIDs
	s.lastListAllKBs = allKBs
	s.lastListAllKBsCaptured = true
	if s.listErr != nil {
		return nil, s.listErr
	}
	return s.items, nil
}

func newWorkspaceKBCatalogHandler(store workspaceKBStore) (http.HandlerFunc, businessPermissionGuard) {
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: true,
		RBACAuthzMode:          "go_db",
		AdminSecretKey:         "test-secret",
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(cfg, logger, &crossScopePermissionStore{})
	return buildWorkspaceKnowledgeBasesHandler(logger, guard, store), guard
}

func doWorkspaceKBCatalogRequest(t *testing.T, handler http.HandlerFunc, authHeader string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, workspaceKnowledgeBasesRoute, nil)
	if authHeader != "" {
		req.Header.Set("Authorization", authHeader)
	}
	handler.ServeHTTP(rec, req)
	return rec
}

func decodeWorkspaceKBCatalogData(t *testing.T, rec *httptest.ResponseRecorder) workspaceKnowledgeBasesData {
	t.Helper()
	var envelope struct {
		Code int                         `json:"code"`
		Data workspaceKnowledgeBasesData `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode envelope: %v body=%s", err, rec.Body.String())
	}
	if envelope.Code != http.StatusOK {
		t.Fatalf("expected envelope code 200, got %d body=%s", envelope.Code, rec.Body.String())
	}
	return envelope.Data
}

// global 授权（allKBs 哨兵）→ 以 allKBs=true 查全部 active，返回集合。
func TestWorkspaceKBCatalogGlobalAuthorization(t *testing.T) {
	t.Parallel()

	store := &fakeWorkspaceKBStore{
		allKBs: true,
		items: []adminstore.KnowledgeBaseItem{
			{ID: "kb-1", TenantID: "tenant-a", ProjectID: "project-a", Name: "KB One", Status: adminstore.KBStatusActive},
			{ID: "kb-2", TenantID: "tenant-b", ProjectID: "project-b", Name: "KB Two", Status: adminstore.KBStatusActive},
		},
	}
	handler, _ := newWorkspaceKBCatalogHandler(store)
	token := issueTestAdminJWT(t, "global@example.com", "test-secret", time.Now().Add(time.Hour))

	rec := doWorkspaceKBCatalogRequest(t, handler, "Bearer "+token)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if store.authorizedCalls != 1 || store.lastAuthorizedSubject != "global@example.com" {
		t.Fatalf("expected one AuthorizedKBIDs call with jwt subject, got calls=%d subject=%q", store.authorizedCalls, store.lastAuthorizedSubject)
	}
	if len(store.lastAuthorizedPerms) != 1 || store.lastAuthorizedPerms[0] != "graph:read" {
		t.Fatalf("expected graph:read permission, got %v", store.lastAuthorizedPerms)
	}
	if !store.lastListAllKBs {
		t.Fatalf("expected allKBs=true passed to store, got %+v", store)
	}
	data := decodeWorkspaceKBCatalogData(t, rec)
	if len(data.Items) != 2 || data.Items[0].KBID != "kb-1" || data.Items[1].KBID != "kb-2" {
		t.Fatalf("unexpected items: %+v", data.Items)
	}
	if data.Items[0].TenantID != "tenant-a" || data.Items[0].ProjectID != "project-a" || data.Items[0].Status != "active" {
		t.Fatalf("unexpected item fields: %+v", data.Items[0])
	}
}

// 仅 project-a 显式授权集合 → 按集合取行，且只含 active（archived/deleting 由 store 过滤）。
func TestWorkspaceKBCatalogScopedAuthorization(t *testing.T) {
	t.Parallel()

	store := &fakeWorkspaceKBStore{
		authorizedKBIDs: []string{"kb-a1"},
		items: []adminstore.KnowledgeBaseItem{
			{ID: "kb-a1", TenantID: "tenant-a", ProjectID: "project-a", Name: "KB A1", Status: adminstore.KBStatusActive},
		},
	}
	handler, _ := newWorkspaceKBCatalogHandler(store)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	rec := doWorkspaceKBCatalogRequest(t, handler, "Bearer "+token)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if store.lastListAllKBs {
		t.Fatalf("expected allKBs=false for explicit kb set")
	}
	if len(store.lastListKBIDs) != 1 || store.lastListKBIDs[0] != "kb-a1" {
		t.Fatalf("expected kbIDs=[kb-a1], got %v", store.lastListKBIDs)
	}
	data := decodeWorkspaceKBCatalogData(t, rec)
	if len(data.Items) != 1 || data.Items[0].KBID != "kb-a1" {
		t.Fatalf("unexpected items: %+v", data.Items)
	}
}

// 无 graph:read 授权 → AuthorizedKBIDs 返回空集合 → items 为空列表（不放大授权）。
func TestWorkspaceKBCatalogNoPermissionEmptyItems(t *testing.T) {
	t.Parallel()

	store := &fakeWorkspaceKBStore{authorizedKBIDs: []string{}, allKBs: false}
	handler, _ := newWorkspaceKBCatalogHandler(store)
	token := issueTestAdminJWT(t, "user-no-graph", "test-secret", time.Now().Add(time.Hour))

	rec := doWorkspaceKBCatalogRequest(t, handler, "Bearer "+token)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 with empty catalog, got %d body=%s", rec.Code, rec.Body.String())
	}
	data := decodeWorkspaceKBCatalogData(t, rec)
	if len(data.Items) != 0 {
		t.Fatalf("expected empty items, got %+v", data.Items)
	}
	if data.Items == nil {
		t.Fatalf("items must serialize as [] not null")
	}
}

// 未认证 / 非法 token → 401 且不调用 store。
func TestWorkspaceKBCatalogUnauthenticated(t *testing.T) {
	t.Parallel()

	store := &fakeWorkspaceKBStore{allKBs: true}
	handler, _ := newWorkspaceKBCatalogHandler(store)

	rec := doWorkspaceKBCatalogRequest(t, handler, "")
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d body=%s", rec.Code, rec.Body.String())
	}
	rec = doWorkspaceKBCatalogRequest(t, handler, "Bearer not-a-jwt")
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 with invalid token, got %d body=%s", rec.Code, rec.Body.String())
	}
	if store.authorizedCalls != 0 || store.listCalls != 0 {
		t.Fatalf("store must not be called when unauthenticated, authorized=%d list=%d", store.authorizedCalls, store.listCalls)
	}
}

// 过期 token → 401 TOKEN_EXPIRED。
func TestWorkspaceKBCatalogExpiredToken(t *testing.T) {
	t.Parallel()

	store := &fakeWorkspaceKBStore{allKBs: true}
	handler, _ := newWorkspaceKBCatalogHandler(store)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(-time.Hour))

	rec := doWorkspaceKBCatalogRequest(t, handler, "Bearer "+token)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "TOKEN_EXPIRED") {
		t.Fatalf("expected TOKEN_EXPIRED, got body=%s", rec.Body.String())
	}
	if store.authorizedCalls != 0 {
		t.Fatalf("store must not be called for expired token")
	}
}

// 授权服务错误 → 503 AUTHZ_UNAVAILABLE，不降级为全量。
func TestWorkspaceKBCatalogAuthzUnavailable(t *testing.T) {
	t.Parallel()

	store := &fakeWorkspaceKBStore{authorizedErr: errors.New("db down")}
	handler, _ := newWorkspaceKBCatalogHandler(store)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	rec := doWorkspaceKBCatalogRequest(t, handler, "Bearer "+token)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected 503, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "AUTHZ_UNAVAILABLE") {
		t.Fatalf("expected AUTHZ_UNAVAILABLE, got body=%s", rec.Body.String())
	}
	if store.listCalls != 0 {
		t.Fatalf("list must not be called when authorization fails")
	}
}

// 非 GET 方法 → 405。
func TestWorkspaceKBCatalogMethodNotAllowed(t *testing.T) {
	t.Parallel()

	store := &fakeWorkspaceKBStore{}
	handler, _ := newWorkspaceKBCatalogHandler(store)
	token := issueTestAdminJWT(t, "user-a", "test-secret", time.Now().Add(time.Hour))

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, workspaceKnowledgeBasesRoute, nil)
	req.Header.Set("Authorization", "Bearer "+token)
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d body=%s", rec.Code, rec.Body.String())
	}
	if store.authorizedCalls != 0 {
		t.Fatalf("store must not be called for disallowed method")
	}
}
