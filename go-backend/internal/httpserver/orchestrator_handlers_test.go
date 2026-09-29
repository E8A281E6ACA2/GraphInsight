package httpserver

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"mime/multipart"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
	"graphinsight/go-backend/internal/orchestrator"
)

type fakeUnifiedGraphBuildStore struct {
	*fakeAdminUserStore
	*fakeAdminConfigStore
	*fakeAdminJobStore
	*fakeAdminLogStore
	*fakeAdminDocumentStore
	createCalls int32
}

// AuthorizedKBIDs 让该 fake 满足 qaScopeAuthorizer（allKBs 哨兵）：
// KB 第二阶段授权 fail-closed 后，携带合法主体的契约类正向测试可解析授权范围。
// 交集/跨 KB 的负向语义由 fakeQAScopeStore 与 crossScopePermissionStore 覆盖。
func (s *fakeUnifiedGraphBuildStore) AuthorizedKBIDs(_ context.Context, _ string, _ ...string) ([]string, bool, error) {
	return nil, true, nil
}

func (s *fakeUnifiedGraphBuildStore) CheckPermission(_ context.Context, subject string, permission string, scope map[string]string) (authz.CheckResult, error) {
	return authz.CheckResult{Allowed: true, User: subject, Reason: "fake_allow", Scope: scope}, nil
}

// attachAllowedTestAuth 给业务路由测试请求附加合法主体：
// KB-scoped 路由第二阶段无条件 fail-closed 后，正向测试必须携带可归因主体。
func attachAllowedTestAuth(t *testing.T, req *http.Request) *http.Request {
	t.Helper()
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour)))
	return req
}

// fakeAdminDocumentStore 满足 adminDocumentStore（M3 文档注册表路由使用）。
// 所有方法 nil-receiver 安全：嵌入指针未初始化时返回零值，避免误触发路由时 panic。
type fakeAdminDocumentStore struct {
	listResult     adminstore.DocumentListResult
	getRow         adminstore.DocumentRegistryItem
	getErr         error
	insertErr      error
	insertRequests []adminstore.DocumentInsertRequest
	deletedRowIDs  []string
}

func (s *fakeAdminDocumentStore) InsertDocument(_ context.Context, req adminstore.DocumentInsertRequest) (adminstore.DocumentRegistryItem, error) {
	if s == nil || s.insertErr != nil {
		return adminstore.DocumentRegistryItem{}, adminstore.ErrDocumentNotFound
	}
	s.insertRequests = append(s.insertRequests, req)
	return adminstore.DocumentRegistryItem{
		DocID: req.DocID, KBID: req.KBID, TenantID: req.TenantID, ProjectID: req.ProjectID,
		Name: req.Name, RelativePath: req.RelativePath, Status: req.Status, CreatedAt: time.Now(),
	}, nil
}

func (s *fakeAdminDocumentStore) GetDocument(_ context.Context, docID string) (adminstore.DocumentRegistryItem, error) {
	if s == nil || s.getErr != nil {
		return adminstore.DocumentRegistryItem{}, adminstore.ErrDocumentNotFound
	}
	if s.getRow.DocID == "" {
		return adminstore.DocumentRegistryItem{}, adminstore.ErrDocumentNotFound
	}
	return s.getRow, nil
}

func (s *fakeAdminDocumentStore) ListDocumentsByKB(_ context.Context, _ adminstore.DocumentListQuery) (adminstore.DocumentListResult, error) {
	if s == nil {
		return adminstore.DocumentListResult{}, nil
	}
	return s.listResult, nil
}

func (s *fakeAdminDocumentStore) MarkDocumentStatus(_ context.Context, req adminstore.DocumentStatusUpdate) (adminstore.DocumentRegistryItem, error) {
	if s == nil {
		return adminstore.DocumentRegistryItem{}, adminstore.ErrDocumentNotFound
	}
	return s.getRow, nil
}

func (s *fakeAdminDocumentStore) DeleteDocumentRow(_ context.Context, docID string, _ string) error {
	if s == nil {
		return nil
	}
	s.deletedRowIDs = append(s.deletedRowIDs, docID)
	return nil
}

func (s *fakeAdminDocumentStore) DeleteDocumentRowsByKB(_ context.Context, _ string) (int64, error) {
	return 0, nil
}

func (s *fakeUnifiedGraphBuildStore) CreateJob(ctx context.Context, req adminstore.JobCreateRequest) (adminstore.JobItem, error) {
	atomic.AddInt32(&s.createCalls, 1)
	return s.fakeAdminJobStore.CreateJob(ctx, req)
}

func TestGraphBuildRouteCreatesNativeJob(t *testing.T) {
	t.Parallel()

	wakeCalled := false
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/internal/jobs/wake" {
			t.Fatalf("unexpected wake path: %s", r.URL.Path)
		}
		if r.Method != http.MethodPost {
			t.Fatalf("unexpected wake method: %s", r.Method)
		}
		if r.Header.Get("X-Go-Proxy") != "graphinsight-go" {
			t.Fatalf("expected go control header, got %q", r.Header.Get("X-Go-Proxy"))
		}
		wakeCalled = true
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"accepted":true}}`))
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	createdAt := time.Date(2026, 6, 7, 9, 0, 0, 0, time.UTC)
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore: &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{
			values: map[string]map[string]string{
				"ai_service": {
					"graph_extract_reasoning_profile":         "fast",
					"graph_extract_complex_reasoning_profile": "balanced",
				},
			},
		},
		fakeAdminLogStore: &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			createResult: adminstore.JobItem{
				ID:         31,
				JobType:    "build_graph",
				Status:     "pending",
				Payload:    map[string]interface{}{"source": "documents", "force": false, "doc_ids": []interface{}{"doc-1"}},
				RetryCount: 0,
				MaxRetries: 3,
				CreatedAt:  createdAt,
			},
		},
	}
	registerRoutes(mux, cfg, logger, nil, nil, pythonWakeClient, nil, nil, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/graph/build?tenant_id=tenant-a&project_id=project-a&kb_id=kb-a", strings.NewReader(`{"source":"documents","force":false,"doc_ids":["doc-1"],"note":"smoke"}`))
	req.Header.Set("x-auth-user-id", "12")
	req.Header.Set(traceHeader, "trace-build-job")
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.fakeAdminJobStore.createReq.JobType != "build_graph" {
		t.Fatalf("unexpected job type: %#v", store.fakeAdminJobStore.createReq)
	}
	if store.fakeAdminJobStore.createReq.RequestedBy != nil {
		t.Fatalf("unexpected untrusted requested_by: %#v", store.fakeAdminJobStore.createReq.RequestedBy)
	}
	if store.fakeAdminJobStore.createReq.TraceID == nil || *store.fakeAdminJobStore.createReq.TraceID != "trace-build-job" {
		t.Fatalf("unexpected trace id: %#v", store.fakeAdminJobStore.createReq.TraceID)
	}
	if store.fakeAdminJobStore.createReq.TenantID == nil || *store.fakeAdminJobStore.createReq.TenantID != "tenant-a" {
		t.Fatalf("unexpected tenant id: %#v", store.fakeAdminJobStore.createReq.TenantID)
	}
	if store.fakeAdminJobStore.createReq.ProjectID == nil || *store.fakeAdminJobStore.createReq.ProjectID != "project-a" {
		t.Fatalf("unexpected project id: %#v", store.fakeAdminJobStore.createReq.ProjectID)
	}
	if store.fakeAdminJobStore.createReq.KBID == nil || *store.fakeAdminJobStore.createReq.KBID != "kb-a" {
		t.Fatalf("unexpected kb id: %#v", store.fakeAdminJobStore.createReq.KBID)
	}
	if store.fakeAdminJobStore.createReq.Payload["source"] != "documents" {
		t.Fatalf("unexpected payload source: %#v", store.fakeAdminJobStore.createReq.Payload)
	}
	if store.fakeAdminJobStore.createReq.Payload["reasoning_profile"] != "fast" {
		t.Fatalf("expected default graph_extract reasoning profile, got %#v", store.fakeAdminJobStore.createReq.Payload["reasoning_profile"])
	}
	docIDs, ok := store.fakeAdminJobStore.createReq.Payload["doc_ids"].([]string)
	if !ok || len(docIDs) != 1 || docIDs[0] != "doc-1" {
		t.Fatalf("unexpected payload doc_ids: %#v", store.fakeAdminJobStore.createReq.Payload["doc_ids"])
	}
	if !wakeCalled {
		t.Fatalf("expected python wake to be triggered")
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected response data object, got %#v", resp.Data)
	}
	if data["status"] != "queued" {
		t.Fatalf("unexpected status: %#v", data["status"])
	}
	if int(data["job_id"].(float64)) != 31 {
		t.Fatalf("unexpected job_id: %#v", data["job_id"])
	}
}

func TestGraphBuildRouteInjectsComplexScenarioReasoningProfileFromConfig(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"accepted":true}}`))
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore: &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{
			values: map[string]map[string]string{
				"ai_service": {
					"graph_extract_reasoning_profile":         "fast",
					"graph_extract_complex_reasoning_profile": "balanced",
				},
			},
		},
		fakeAdminLogStore: &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			createResult: adminstore.JobItem{ID: 52, JobType: "build_graph", Status: "pending", Payload: map[string]interface{}{"force": false}, RetryCount: 0, MaxRetries: 3, CreatedAt: time.Date(2026, 6, 7, 11, 0, 0, 0, time.UTC)},
		},
	}
	registerRoutes(mux, cfg, logger, nil, nil, pythonWakeClient, nil, nil, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/graph/build?kb_id=kb-a", strings.NewReader(`{"force":false,"complex_extraction":true}`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if store.fakeAdminJobStore.createReq.Payload["reasoning_profile"] != "balanced" {
		t.Fatalf("expected complex graph_extract reasoning profile=balanced, got %#v", store.fakeAdminJobStore.createReq.Payload["reasoning_profile"])
	}
	if store.fakeAdminJobStore.createReq.Payload["complex_extraction"] != true {
		t.Fatalf("expected complex_extraction=true, got %#v", store.fakeAdminJobStore.createReq.Payload["complex_extraction"])
	}
}

func TestGraphBuildRouteIdempotencyReplay(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"accepted":true}}`))
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	createdAt := time.Date(2026, 6, 7, 10, 0, 0, 0, time.UTC)
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			createResult: adminstore.JobItem{ID: 45, JobType: "build_graph", Status: "pending", Payload: map[string]interface{}{"force": false}, RetryCount: 0, MaxRetries: 3, CreatedAt: createdAt},
		},
	}
	registerRoutes(mux, cfg, logger, nil, nil, pythonWakeClient, nil, nil, nil, store)

	rec1 := httptest.NewRecorder()
	req1 := httptest.NewRequest(http.MethodPost, "/api/graph/build?kb_id=kb-a", strings.NewReader(`{"force":false}`))
	req1.Header.Set("Idempotency-Key", "build-456")
	req1.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec1, req1)
	if rec1.Code != http.StatusOK {
		t.Fatalf("first request expected 200, got %d", rec1.Code)
	}

	rec2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodPost, "/api/graph/build", strings.NewReader(`{"force":false}`))
	req2.Header.Set("Idempotency-Key", "build-456")
	req2.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec2, req2)

	if rec2.Code != http.StatusOK {
		t.Fatalf("expected 200 replay, got %d", rec2.Code)
	}
	if rec1.Body.String() != rec2.Body.String() {
		t.Fatalf("expected replayed response to match, got %s vs %s", rec1.Body.String(), rec2.Body.String())
	}
	if atomic.LoadInt32(&store.createCalls) != 1 {
		t.Fatalf("expected create job called once, got %d", atomic.LoadInt32(&store.createCalls))
	}
	if rec2.Header().Get("X-Idempotency-Key") != "build-456" {
		t.Fatalf("expected X-Idempotency-Key response header")
	}
}

func TestGraphBuildRouteIdempotencyConflict(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"accepted":true}}`))
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			createResult: adminstore.JobItem{ID: 46, JobType: "build_graph", Status: "pending", Payload: map[string]interface{}{"force": false}, RetryCount: 0, MaxRetries: 3, CreatedAt: time.Date(2026, 6, 7, 10, 5, 0, 0, time.UTC)},
		},
	}
	registerRoutes(mux, cfg, logger, nil, nil, pythonWakeClient, nil, nil, nil, store)

	rec1 := httptest.NewRecorder()
	req1 := httptest.NewRequest(http.MethodPost, "/api/graph/build?kb_id=kb-a", strings.NewReader(`{"force":false}`))
	req1.Header.Set("Idempotency-Key", "build-789")
	req1.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec1, req1)
	if rec1.Code != http.StatusOK {
		t.Fatalf("first request expected 200, got %d", rec1.Code)
	}

	rec2 := httptest.NewRecorder()
	req2 := httptest.NewRequest(http.MethodPost, "/api/graph/build?kb_id=kb-a", strings.NewReader(`{"force":true}`))
	req2.Header.Set("Idempotency-Key", "build-789")
	req2.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec2, req2)

	if rec2.Code != http.StatusConflict {
		t.Fatalf("expected 409, got %d", rec2.Code)
	}
	if atomic.LoadInt32(&store.createCalls) != 1 {
		t.Fatalf("expected create job called once, got %d", atomic.LoadInt32(&store.createCalls))
	}
}

func TestOrchestratorDocumentsRoutes(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	docDir := t.TempDir()
	// M3：文档物理路径为 DocumentStoragePath / storage_prefix / relative_path。
	docPrefix := filepath.Join("tenant-a", "project-a", "kb-a")
	if err := os.MkdirAll(filepath.Join(docDir, docPrefix), 0o755); err != nil {
		t.Fatalf("mkdir kb storage prefix: %v", err)
	}
	if err := os.WriteFile(filepath.Join(docDir, docPrefix, "alpha.txt"), []byte("alpha"), 0o644); err != nil {
		t.Fatalf("write temp document: %v", err)
	}
	graphSvc := &stubGraphService{
		docTotals:       graph.DocumentGraphStats{Documents: 1, Chunks: 2, Relations: 3, Entities: 4},
		docClearPreview: graph.DocumentGraphStats{Documents: 1, Chunks: 2, Relations: 3},
		docClear:        graph.DocumentGraphStats{Documents: 1, Chunks: 2, Relations: 3, OrphanEntities: 1},
	}
	cfg := config.Config{
		AppName:                     "GraphInsight Go API",
		Version:                     "test",
		RBACEnforceBusinessAPI:      false,
		AdminSecretKey:              "test-secret",
		DocumentStoragePath:         docDir,
		DocumentStorageFallbackPath: docDir,
	}
	alphaRow := adminstore.DocumentRegistryItem{
		DocID: "doc-alpha", KBID: "kb-a", TenantID: "tenant-a", ProjectID: "project-a",
		Name: "alpha.txt", RelativePath: "alpha.txt", Status: adminstore.DocumentStatusUploaded, CreatedAt: time.Now(),
	}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			kbRow: adminstore.KnowledgeBaseItem{
				ID: "kb-a", TenantID: "tenant-a", ProjectID: "project-a",
				Status: adminstore.KBStatusActive, StoragePrefix: "tenant-a/project-a/kb-a",
			},
		},
		fakeAdminDocumentStore: &fakeAdminDocumentStore{
			listResult: adminstore.DocumentListResult{Items: []adminstore.DocumentRegistryItem{alphaRow}, Total: 1},
			getRow:     alphaRow,
		},
	}
	registerRoutes(mux, cfg, logger, graphSvc, nil, nil, nil, nil, nil, store)

	getCases := []struct {
		method string
		path   string
		owner  string
	}{
		{method: http.MethodGet, path: "/api/documents?kb_id=kb-a", owner: "go-native"},
		{method: http.MethodGet, path: "/api/documents/deleted?kb_id=kb-a", owner: "go-native"},
	}
	for _, tc := range getCases {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(tc.method, tc.path, nil)
		attachAllowedTestAuth(t, req)
		mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("request %s %s expected 200, got %d", tc.method, tc.path, rec.Code)
		}
		if rec.Header().Get(routeOwnerHeader) != tc.owner {
			t.Fatalf("request %s %s unexpected route owner: %s", tc.method, tc.path, rec.Header().Get(routeOwnerHeader))
		}
	}

	var listBody struct {
		Code int `json:"code"`
		Data struct {
			Items []map[string]interface{} `json:"items"`
		} `json:"data"`
	}
	recList := httptest.NewRecorder()
	reqList := httptest.NewRequest(http.MethodGet, "/api/documents?kb_id=kb-a", nil)
	attachAllowedTestAuth(t, reqList)
	mux.ServeHTTP(recList, reqList)
	if err := json.Unmarshal(recList.Body.Bytes(), &listBody); err != nil {
		t.Fatalf("unmarshal list response: %v", err)
	}
	if len(listBody.Data.Items) != 1 || listBody.Data.Items[0]["name"] != "alpha.txt" {
		t.Fatalf("unexpected document list payload: %s", recList.Body.String())
	}

	recClear := httptest.NewRecorder()
	docID, _ := listBody.Data.Items[0]["id"].(string)
	if strings.TrimSpace(docID) == "" {
		t.Fatalf("expected alpha doc id in list payload: %s", recList.Body.String())
	}

	recDelete := httptest.NewRecorder()
	reqDelete := httptest.NewRequest(http.MethodDelete, "/api/documents/"+docID+"?purge_graph=false&soft_delete=true&dry_run=false&verify_after=true&kb_id=kb-a", nil)
	attachAllowedTestAuth(t, reqDelete)
	mux.ServeHTTP(recDelete, reqDelete)
	if recDelete.Code != http.StatusOK {
		t.Fatalf("request DELETE /api/documents/{id} expected 200, got %d body=%s", recDelete.Code, recDelete.Body.String())
	}
	if recDelete.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("request DELETE /api/documents/{id} unexpected route owner: %s", recDelete.Header().Get(routeOwnerHeader))
	}

	// M4 FIX #4：删除成功后 best-effort 向量清理——orchestrator 客户端不可用时
	// 尝试仍会被记录（attempted=true, success=false），且删除不受影响。
	var deleteBody struct {
		Data struct {
			VectorCleanup struct {
				Attempted bool `json:"attempted"`
				Success   bool `json:"success"`
			} `json:"vector_cleanup"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recDelete.Body.Bytes(), &deleteBody); err != nil {
		t.Fatalf("decode delete response: %v body=%s", err, recDelete.Body.String())
	}
	if !deleteBody.Data.VectorCleanup.Attempted || deleteBody.Data.VectorCleanup.Success {
		t.Fatalf("unexpected vector_cleanup result: %#v", deleteBody.Data.VectorCleanup)
	}

	recRestore := httptest.NewRecorder()
	reqRestore := httptest.NewRequest(http.MethodPost, "/api/documents/"+docID+"/restore?kb_id=kb-a", nil)
	attachAllowedTestAuth(t, reqRestore)
	mux.ServeHTTP(recRestore, reqRestore)
	if recRestore.Code != http.StatusOK {
		t.Fatalf("request POST /api/documents/{id}/restore expected 200, got %d body=%s", recRestore.Code, recRestore.Body.String())
	}
	if recRestore.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("request POST /api/documents/{id}/restore unexpected route owner: %s", recRestore.Header().Get(routeOwnerHeader))
	}

	reqClear := httptest.NewRequest(http.MethodDelete, "/api/documents?purge_graph=true&kb_id=kb-a", nil)
	attachAllowedTestAuth(t, reqClear)
	mux.ServeHTTP(recClear, reqClear)
	if recClear.Code != http.StatusOK {
		t.Fatalf("request DELETE /api/documents expected 200, got %d body=%s", recClear.Code, recClear.Body.String())
	}
	if recClear.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("request DELETE /api/documents unexpected route owner: %s", recClear.Header().Get(routeOwnerHeader))
	}

	if _, err := os.Stat(filepath.Join(docDir, docPrefix, "alpha.txt")); !os.IsNotExist(err) {
		t.Fatalf("expected document to be removed from active directory, err=%v", err)
	}
	trashDir := filepath.Join(docDir, documentsSoftDeleteDirName)
	metaFiles, err := filepath.Glob(filepath.Join(trashDir, "*"+documentsSoftDeleteMetaExt))
	if err != nil {
		t.Fatalf("glob trash meta files: %v", err)
	}
	if len(metaFiles) != 1 {
		t.Fatalf("expected one trash metadata file after clear, got %d", len(metaFiles))
	}
}

func TestOrchestratorUploadRoute(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	docDir := t.TempDir()
	cfg := config.Config{
		AppName:                     "GraphInsight Go API",
		Version:                     "test",
		RBACEnforceBusinessAPI:      false,
		AdminSecretKey:              "test-secret",
		DocumentStoragePath:         docDir,
		DocumentStorageFallbackPath: docDir,
	}
	uploadStore := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			kbRow: adminstore.KnowledgeBaseItem{
				ID: "kb-a", TenantID: "tenant-a", ProjectID: "project-a",
				Status: adminstore.KBStatusActive, StoragePrefix: "tenant-a/project-a/kb-a",
			},
		},
		fakeAdminDocumentStore: &fakeAdminDocumentStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, nil, nil, uploadStore)

	var b strings.Builder
	writer := multipart.NewWriter(&b)
	part, _ := writer.CreateFormFile("files", "a.txt")
	_, _ = part.Write([]byte("abc"))
	_ = writer.Close()

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/documents/upload?kb_id=kb-a", strings.NewReader(b.String()))
	req.Header.Set("Content-Type", writer.FormDataContentType())
	attachAllowedTestAuth(t, req)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var payload struct {
		Code int `json:"code"`
		Data struct {
			Uploaded []map[string]interface{} `json:"uploaded"`
			Skipped  []map[string]interface{} `json:"skipped"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal upload response: %v", err)
	}
	if payload.Code != http.StatusOK {
		t.Fatalf("unexpected response code in body: %d", payload.Code)
	}
	if len(payload.Data.Uploaded) != 1 {
		t.Fatalf("expected 1 uploaded item, got %d body=%s", len(payload.Data.Uploaded), rec.Body.String())
	}
	if len(payload.Data.Skipped) != 0 {
		t.Fatalf("expected 0 skipped items, got %d body=%s", len(payload.Data.Skipped), rec.Body.String())
	}
	if payload.Data.Uploaded[0]["name"] != "a.txt" {
		t.Fatalf("unexpected uploaded name: %v", payload.Data.Uploaded[0]["name"])
	}
	if _, err := os.Stat(filepath.Join(docDir, "tenant-a", "project-a", "kb-a", "a.txt")); err != nil {
		t.Fatalf("expected uploaded file written to disk under kb storage prefix: %v", err)
	}
}

func TestOrchestratorMetricsRoute(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/internal/docqa/health" {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"code":200,"message":"ok"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"code":503,"message":"busy"}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{
		PythonBackendBaseURL:        upstream.URL,
		PythonBackendTimeoutSeconds: 2,
		OrchestratorRetryMax:        0,
	})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore: &fakeAdminUserStore{
			permissionResult: authz.CheckResult{Allowed: true, User: "admin@example.com"},
		},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	recCall := httptest.NewRecorder()
	reqCall := httptest.NewRequest(http.MethodGet, "/api/docqa/health", nil)
	mux.ServeHTTP(recCall, reqCall)
	if recCall.Code != http.StatusOK {
		t.Fatalf("expected health call 200, got %d", recCall.Code)
	}

	recCall2 := httptest.NewRecorder()
	reqCall2 := httptest.NewRequest(http.MethodPost, "/api/docqa/deep-research", strings.NewReader(`{"question":"x","kb_id":"kb-a"}`))
	attachAllowedTestAuth(t, reqCall2)
	reqCall2.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(recCall2, reqCall2)
	if recCall2.Code != http.StatusServiceUnavailable {
		t.Fatalf("expected deep-research 503 passthrough, got %d", recCall2.Code)
	}

	recMetrics := httptest.NewRecorder()
	reqMetrics := httptest.NewRequest(http.MethodGet, "/api/monitor/orchestrator", nil)
	mux.ServeHTTP(recMetrics, reqMetrics)
	if recMetrics.Code != http.StatusOK {
		t.Fatalf("expected metrics 200, got %d", recMetrics.Code)
	}

	var payload struct {
		Code int `json:"code"`
		Data struct {
			TotalRequests int `json:"total_requests"`
			Failed        int `json:"failed"`
			Routes        []struct {
				Route         string         `json:"route"`
				Method        string         `json:"method"`
				ErrorTaxonomy map[string]int `json:"error_taxonomy"`
			} `json:"routes"`
		} `json:"data"`
	}
	if err := json.Unmarshal(recMetrics.Body.Bytes(), &payload); err != nil {
		t.Fatalf("unmarshal metrics response: %v", err)
	}
	if payload.Code != http.StatusOK {
		t.Fatalf("expected response code 200 in body, got %d", payload.Code)
	}
	if payload.Data.TotalRequests < 2 {
		t.Fatalf("expected at least 2 orchestrator requests, got %d", payload.Data.TotalRequests)
	}
	if payload.Data.Failed < 1 {
		t.Fatalf("expected failed request count >=1, got %d", payload.Data.Failed)
	}
	foundSuccess := false
	found5xx := false
	for _, route := range payload.Data.Routes {
		if route.Route == "/api/internal/docqa/health" && route.Method == http.MethodGet && route.ErrorTaxonomy["success"] >= 1 {
			foundSuccess = true
		}
		if route.Route == "/api/internal/docqa/deep-research" && route.Method == http.MethodPost && route.ErrorTaxonomy["upstream_5xx"] >= 1 {
			found5xx = true
		}
	}
	if !foundSuccess {
		t.Fatalf("expected success taxonomy for /api/internal/docqa/health")
	}
	if !found5xx {
		t.Fatalf("expected upstream_5xx taxonomy for /api/internal/docqa/deep-research")
	}
}

func TestOrchestratorNL2CypherRoutes(t *testing.T) {
	t.Parallel()

	var seen []string
	var permissions []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Method+" "+r.URL.Path)
		permissions = append(permissions, r.Header.Get("x-authz-permission"))
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok"}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret", AIModel: "gpt-4o-mini", AIAPIKey: "sk-test"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore: &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{
			values: map[string]map[string]string{
				"nl2cypher": {
					"enabled":   "true",
					"max_limit": "88",
				},
			},
		},
		fakeAdminLogStore: &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	postRec := httptest.NewRecorder()
	postReq := httptest.NewRequest(http.MethodPost, "/api/nl2cypher", strings.NewReader(`{"natural_language":"查找小麦","kb_id":"kb-a"}`))
	postReq.Header.Set("Content-Type", "application/json")
	postReq.Header.Set("x-authz-permission", "nl2cypher:use")
	attachAllowedTestAuth(t, postReq)
	mux.ServeHTTP(postRec, postReq)
	if postRec.Code != http.StatusOK {
		t.Fatalf("request POST /api/nl2cypher expected 200, got %d", postRec.Code)
	}
	if postRec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("request POST /api/nl2cypher unexpected route owner: %s", postRec.Header().Get(routeOwnerHeader))
	}

	examplesRec := httptest.NewRecorder()
	examplesReq := httptest.NewRequest(http.MethodGet, "/api/nl2cypher/examples", nil)
	mux.ServeHTTP(examplesRec, examplesReq)
	if examplesRec.Code != http.StatusOK {
		t.Fatalf("request GET /api/nl2cypher/examples expected 200, got %d", examplesRec.Code)
	}
	if examplesRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("request GET /api/nl2cypher/examples unexpected route owner: %s", examplesRec.Header().Get(routeOwnerHeader))
	}

	var examplesBody map[string]interface{}
	if err := json.Unmarshal(examplesRec.Body.Bytes(), &examplesBody); err != nil {
		t.Fatalf("decode examples response: %v", err)
	}
	if examplesBody["success"] != true {
		t.Fatalf("expected examples success=true, got body=%s", examplesRec.Body.String())
	}

	statusRec := httptest.NewRecorder()
	statusReq := httptest.NewRequest(http.MethodGet, "/api/nl2cypher/status", nil)
	statusReq.Header.Set("x-authz-permission", "config:read")
	mux.ServeHTTP(statusRec, statusReq)
	if statusRec.Code != http.StatusOK {
		t.Fatalf("request GET /api/nl2cypher/status expected 200, got %d", statusRec.Code)
	}
	if statusRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("request GET /api/nl2cypher/status unexpected route owner: %s", statusRec.Header().Get(routeOwnerHeader))
	}

	var statusBody map[string]interface{}
	if err := json.Unmarshal(statusRec.Body.Bytes(), &statusBody); err != nil {
		t.Fatalf("decode status response: %v", err)
	}
	if statusBody["config_source"] != "go-native" {
		t.Fatalf("expected go-native config_source, got body=%s", statusRec.Body.String())
	}
	if statusBody["max_limit"] != float64(88) {
		t.Fatalf("expected max_limit=88, got body=%s", statusRec.Body.String())
	}

	wantSeen := []string{
		"POST /api/internal/nl2cypher",
	}
	for i, want := range wantSeen {
		if seen[i] != want {
			t.Fatalf("request[%d] expected %s, got %s", i, want, seen[i])
		}
	}
	if len(seen) != 1 {
		t.Fatalf("expected only POST path to hit python upstream, got %v", seen)
	}
	if permissions[0] != "nl2cypher:use" {
		t.Fatalf("expected nl2cypher permission, got %s", permissions[0])
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "nl2cypher_generate" {
		t.Fatalf("expected nl2cypher business audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "success" {
		t.Fatalf("expected successful nl2cypher audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestNL2CypherRouteRejectsInvalidJSONBeforeUpstream(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/nl2cypher", strings.NewReader(`{"natural_language":`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamCalled {
		t.Fatalf("expected invalid JSON to be rejected before upstream call")
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "nl2cypher_generate" {
		t.Fatalf("expected invalid request audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "failed" {
		t.Fatalf("expected failed audit status, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestNL2CypherRouteRejectsBlankNaturalLanguageBeforeUpstream(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/nl2cypher", strings.NewReader(`{"natural_language":"   "}`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamCalled {
		t.Fatalf("expected blank natural_language to be rejected before upstream call")
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "nl2cypher_generate" {
		t.Fatalf("expected blank request audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "failed" {
		t.Fatalf("expected failed audit status, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestDocQARouteWritesBusinessAudit(t *testing.T) {
	t.Parallel()

	var seen []string
	forwardedBody := map[string]interface{}{}
	var forwardedKBIDs string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Method+" "+r.URL.Path)
		if r.URL.Path != "/api/internal/docqa" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		forwardedKBIDs = r.Header.Get("x-kb-ids")
		rawBody, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(rawBody, &forwardedBody); err != nil {
			t.Fatalf("decode forwarded body: %v body=%s", err, string(rawBody))
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"answer":"ok","citations":[]}}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", strings.NewReader(`{"question":"他们的工作单位呢","kb_id":"kb-a","top_k":3,"require_citation":true,"conversation_history":[{"role":"user","content":"郑雪梅和兰香瑚是谁？"},{"role":"assistant","content":"他们是论文作者。"}]}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-authz-permission", "qa:ask")
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour)))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if len(seen) != 1 || seen[0] != "POST /api/internal/docqa" {
		t.Fatalf("unexpected upstream calls: %v", seen)
	}
	if forwardedKBIDs != "kb-a" {
		t.Fatalf("expected x-kb-ids effective scope forwarded to python, got %q", forwardedKBIDs)
	}
	history, ok := forwardedBody["conversation_history"].([]interface{})
	if !ok || len(history) != 2 {
		t.Fatalf("expected conversation_history to be forwarded, got %#v", forwardedBody["conversation_history"])
	}
	if forwardedBody["reasoning_profile"] != "balanced" {
		t.Fatalf("expected default reasoning_profile to be added, got %#v", forwardedBody["reasoning_profile"])
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "docqa_ask" {
		t.Fatalf("expected docqa business audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "success" {
		t.Fatalf("expected successful docqa audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestDocQARouteRejectsInvalidJSONBeforeUpstream(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", strings.NewReader(`{"question":`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamCalled {
		t.Fatalf("expected invalid JSON to be rejected before upstream call")
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "docqa_ask" {
		t.Fatalf("expected invalid request audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "failed" {
		t.Fatalf("expected failed audit status, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestDocQARouteRejectsBlankQuestionBeforeUpstream(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", strings.NewReader(`{"question":"   "}`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamCalled {
		t.Fatalf("expected blank question to be rejected before upstream call")
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "docqa_ask" {
		t.Fatalf("expected blank request audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "failed" {
		t.Fatalf("expected failed audit status, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestDeepResearchRouteWritesBusinessAudit(t *testing.T) {
	t.Parallel()

	var seen []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Method+" "+r.URL.Path)
		if r.URL.Path != "/api/internal/docqa/deep-research" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"question":"x","summary":"ok","final_conclusion":"ok","report":"ok","sub_questions":[],"citations":[],"confidence":{},"evidence_stats":{}}}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa/deep-research", strings.NewReader(`{"question":"你好","kb_id":"kb-a","top_k":8,"max_sub_questions":4}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("x-authz-permission", "qa:ask")
	attachAllowedTestAuth(t, req)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if len(seen) != 1 || seen[0] != "POST /api/internal/docqa/deep-research" {
		t.Fatalf("unexpected upstream calls: %v", seen)
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "docqa_deep_research" {
		t.Fatalf("expected deep research business audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "success" {
		t.Fatalf("expected successful deep research audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestDocQARouteInjectsScenarioReasoningProfileFromConfig(t *testing.T) {
	t.Parallel()

	var bodyBytes []byte
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var err error
		bodyBytes, err = io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("read body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"answer":"ok","citations":[]}}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore: &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{
			values: map[string]map[string]string{
				"ai_service": {
					"docqa_reasoning_profile": "fast",
				},
			},
		},
		fakeAdminLogStore: &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", strings.NewReader(`{"question":"你好","kb_id":"kb-a"}`))
	attachAllowedTestAuth(t, req)
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(string(bodyBytes), `"reasoning_profile":"fast"`) {
		t.Fatalf("expected forwarded body to include configured reasoning profile, got %s", string(bodyBytes))
	}
}

func TestDeepResearchRouteInjectsScenarioReasoningProfileFromConfig(t *testing.T) {
	t.Parallel()

	var bodyBytes []byte
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var err error
		bodyBytes, err = io.ReadAll(r.Body)
		if err != nil {
			t.Fatalf("read body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"question":"x","summary":"ok","final_conclusion":"ok","report":"ok","sub_questions":[],"citations":[],"confidence":{},"evidence_stats":{}}}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore: &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{
			values: map[string]map[string]string{
				"ai_service": {
					"deep_research_reasoning_profile": "balanced",
				},
			},
		},
		fakeAdminLogStore: &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa/deep-research", strings.NewReader(`{"question":"你好","kb_id":"kb-a"}`))
	attachAllowedTestAuth(t, req)
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(string(bodyBytes), `"reasoning_profile":"balanced"`) {
		t.Fatalf("expected forwarded body to include configured reasoning profile, got %s", string(bodyBytes))
	}
}

func TestDeepResearchRouteRejectsInvalidJSONBeforeUpstream(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa/deep-research", strings.NewReader(`{"question":`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamCalled {
		t.Fatalf("expected invalid JSON to be rejected before upstream call")
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "docqa_deep_research" {
		t.Fatalf("expected invalid request audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "failed" {
		t.Fatalf("expected failed audit status, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestDeepResearchRouteRejectsBlankQuestionBeforeUpstream(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"success":true}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa/deep-research", strings.NewReader(`{"question":"   "}`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamCalled {
		t.Fatalf("expected blank question to be rejected before upstream call")
	}
	if store.fakeAdminLogStore.businessAuditReq.Action != "docqa_deep_research" {
		t.Fatalf("expected blank request audit, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
	if store.fakeAdminLogStore.businessAuditReq.Status != "failed" {
		t.Fatalf("expected failed audit status, got %#v", store.fakeAdminLogStore.businessAuditReq)
	}
}

func TestDocQAHealthRejectsInvalidProbeLLMBeforeUpstream(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok"}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false, AdminSecretKey: "test-secret"}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/docqa/health?probe_llm=not-bool", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-orchestrator" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamCalled {
		t.Fatalf("expected invalid probe_llm to be rejected before upstream call")
	}
}

// fakeQAScopeStore 同时满足 adminPermissionStore 与 qaScopeAuthorizer，
// 用于验证 M4 问答范围授权的 请求 ∩ 授权 语义。
type fakeQAScopeStore struct {
	fakeAdminPermissionStore
	kbIDs                []string
	allKBs               bool
	err                  error
	calledWithSubject    string
	calledWithPermission string
}

func (s *fakeQAScopeStore) AuthorizedKBIDs(_ context.Context, subject string, permissions ...string) ([]string, bool, error) {
	s.calledWithSubject = subject
	if len(permissions) > 0 {
		s.calledWithPermission = permissions[0]
	}
	return s.kbIDs, s.allKBs, s.err
}

func TestDocQAScopeAuthorizationIntersectsAuthorizedKBs(t *testing.T) {
	t.Parallel()

	upstreamCalled := false
	var upstreamKBIDs string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		upstreamCalled = true
		upstreamKBIDs = r.Header.Get("x-kb-ids")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"answer":"ok","citations":[]}}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{PythonBackendBaseURL: upstream.URL, PythonBackendTimeoutSeconds: 2})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	token := issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour))

	buildHandler := func(store *fakeQAScopeStore, logStore *fakeAdminLogStore) http.HandlerFunc {
		guard := newBusinessPermissionGuard(config.Config{
			RBACEnforceBusinessAPI: true,
			RBACAuthzMode:          "go_db",
			AdminSecretKey:         "test-secret",
		}, logger, store)
		// 与路由注册一致：guard.wrap 负责第一轮鉴权并传播 x-auth-user-name。
		return guard.wrap("qa:ask", buildNativeDocQAHandler(logger, guard, orc, nil, newOrchestratorMetrics(), logStore, &fakeAdminConfigStore{}, false))
	}

	newRequest := func(body string) *http.Request {
		req := httptest.NewRequest(http.MethodPost, "/api/docqa", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+token)
		return req
	}

	t.Run("empty intersection denied with audit before upstream", func(t *testing.T) {
		logStore := &fakeAdminLogStore{}
		store := &fakeQAScopeStore{
			fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
			kbIDs:                    []string{"kb-b"},
		}
		rec := httptest.NewRecorder()
		buildHandler(store, logStore).ServeHTTP(rec, newRequest(`{"question":"你好","kb_id":"kb-a"}`))

		if rec.Code != http.StatusForbidden {
			t.Fatalf("expected 403, got %d body=%s", rec.Code, rec.Body.String())
		}
		if !strings.Contains(rec.Body.String(), "KB_ACCESS_DENIED") {
			t.Fatalf("expected KB_ACCESS_DENIED error code, got body=%s", rec.Body.String())
		}
		if upstreamCalled {
			t.Fatalf("upstream must not be called when the intersection is empty")
		}
		if store.calledWithSubject != "admin@example.com" || store.calledWithPermission != "qa:ask" {
			t.Fatalf("unexpected authorized lookup: subject=%q permission=%q", store.calledWithSubject, store.calledWithPermission)
		}
		if logStore.businessAuditReq.Status != "denied" || logStore.businessAuditReq.Action != "authz_denied" {
			t.Fatalf("expected denial audit, got %#v", logStore.businessAuditReq)
		}
	})

	t.Run("intersection forwarded as effective scope", func(t *testing.T) {
		store := &fakeQAScopeStore{
			fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
			kbIDs:                    []string{"kb-a", "kb-b"},
		}
		rec := httptest.NewRecorder()
		buildHandler(store, &fakeAdminLogStore{}).ServeHTTP(rec, newRequest(`{"question":"你好","kb_id":"kb-a"}`))

		if rec.Code != http.StatusOK {
			t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
		}
		if !upstreamCalled || upstreamKBIDs != "kb-a" {
			t.Fatalf("expected forwarded x-kb-ids=kb-a, got %q (called=%v)", upstreamKBIDs, upstreamCalled)
		}
	})

	t.Run("global binding short circuits to request set", func(t *testing.T) {
		store := &fakeQAScopeStore{
			fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
			allKBs:                   true,
		}
		rec := httptest.NewRecorder()
		buildHandler(store, &fakeAdminLogStore{}).ServeHTTP(rec, newRequest(`{"question":"你好","kb_ids":["kb-a","kb-c"]}`))

		if rec.Code != http.StatusOK {
			t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
		}
		if !upstreamCalled || upstreamKBIDs != "kb-a,kb-c" {
			t.Fatalf("expected forwarded x-kb-ids=kb-a,kb-c, got %q (called=%v)", upstreamKBIDs, upstreamCalled)
		}
	})

	t.Run("missing kb scope rejected before upstream", func(t *testing.T) {
		store := &fakeQAScopeStore{
			fakeAdminPermissionStore: fakeAdminPermissionStore{result: authz.CheckResult{Allowed: true, UserID: 1, User: "admin@example.com"}},
			allKBs:                   true,
		}
		rec := httptest.NewRecorder()
		buildHandler(store, &fakeAdminLogStore{}).ServeHTTP(rec, newRequest(`{"question":"你好"}`))

		if rec.Code != http.StatusBadRequest {
			t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
		}
		if !strings.Contains(rec.Body.String(), "KB_SCOPE_REQUIRED") {
			t.Fatalf("expected KB_SCOPE_REQUIRED error code, got body=%s", rec.Body.String())
		}
		if store.calledWithSubject != "" {
			t.Fatalf("AuthorizedKBIDs must not be called when scope resolution fails, got subject %q", store.calledWithSubject)
		}
	})
}

func TestOrchestratorDocQAOptionalSafeRetry(t *testing.T) {
	t.Parallel()

	var attempts int32
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		current := atomic.AddInt32(&attempts, 1)
		if r.URL.Path != "/api/internal/docqa" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if current == 1 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"code":503,"message":"busy"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"code":200,"message":"ok"}`))
	}))
	defer upstream.Close()

	orc, err := orchestrator.New(config.Config{
		PythonBackendBaseURL:          upstream.URL,
		PythonBackendTimeoutSeconds:   2,
		OrchestratorRetryMax:          2,
		OrchestratorRetryBackoffMS:    1,
		OrchestratorRetryMaxBackoffMS: 2,
	})
	if err != nil {
		t.Fatalf("new orchestrator client: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{
		AppName:                    "GraphInsight Go API",
		Version:                    "test",
		RBACEnforceBusinessAPI:     false,
		AdminSecretKey:             "test-secret",
		OrchestratorSafeRetryDocQA: true,
	}
	store := &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore: &fakeAdminUserStore{
			permissionResult: authz.CheckResult{Allowed: true, User: "admin@example.com"},
		},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
	}
	registerRoutes(mux, cfg, logger, nil, nil, nil, nil, orc, nil, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/docqa", strings.NewReader(`{"question":"你好","kb_id":"kb-a"}`))
	attachAllowedTestAuth(t, req)
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if atomic.LoadInt32(&attempts) != 2 {
		t.Fatalf("expected 2 attempts with safe retry enabled, got %d", attempts)
	}
}
