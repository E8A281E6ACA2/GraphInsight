package httpserver

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
)

type stubGraphService struct {
	nodeDetail      graph.NodeDetail
	nodeErr         error
	schema          graph.GraphSchemaResponse
	schemaErr       error
	counts          graph.GraphCounts
	countErr        error
	runtimeInfo     graph.RuntimeConnectionInfo
	docTotals       graph.DocumentGraphStats
	docTotErr       error
	docPreview      graph.DocumentGraphStats
	docPrevErr      error
	docDelete       graph.DocumentGraphStats
	docDelErr       error
	docClearPreview graph.DocumentGraphStats
	docClearPrevErr error
	docClear        graph.DocumentGraphStats
	docClearErr     error
	healthErr       error
	// M4-R1 负向测试：记录下游是否被调用，验证跨 KB 拒绝时不触达 Neo4j 服务。
	discoverSchemaCalls int
	getNodeDetailCalls  int
	expandNodeCalls     int
}

func (s *stubGraphService) CheckHealth(ctx context.Context) error {
	return s.healthErr
}

func (s *stubGraphService) RuntimeConnectionInfo() graph.RuntimeConnectionInfo {
	return s.runtimeInfo
}

func (s *stubGraphService) CountGraph(ctx context.Context) (graph.GraphCounts, error) {
	if s.countErr != nil {
		return graph.GraphCounts{}, s.countErr
	}
	return s.counts, nil
}

func (s *stubGraphService) GetDocumentGraphTotals(ctx context.Context) (graph.DocumentGraphStats, error) {
	if s.docTotErr != nil {
		return graph.DocumentGraphStats{}, s.docTotErr
	}
	return s.docTotals, nil
}

func (s *stubGraphService) GetDocumentGraphTotalsForKB(ctx context.Context, kbID string) (graph.DocumentGraphStats, error) {
	if s.docTotErr != nil {
		return graph.DocumentGraphStats{}, s.docTotErr
	}
	return s.docTotals, nil
}

func (s *stubGraphService) PreviewDeleteDocumentGraph(ctx context.Context, docID string, kbID string) (graph.DocumentGraphStats, error) {
	if s.docPrevErr != nil {
		return graph.DocumentGraphStats{}, s.docPrevErr
	}
	return s.docPreview, nil
}

func (s *stubGraphService) DeleteDocumentGraph(ctx context.Context, docID string, kbID string) (graph.DocumentGraphStats, error) {
	if s.docDelErr != nil {
		return graph.DocumentGraphStats{}, s.docDelErr
	}
	return s.docDelete, nil
}

func (s *stubGraphService) PreviewClearDocumentGraph(ctx context.Context, kbID string) (graph.DocumentGraphStats, error) {
	if s.docClearPrevErr != nil {
		return graph.DocumentGraphStats{}, s.docClearPrevErr
	}
	return s.docClearPreview, nil
}

func (s *stubGraphService) ClearDocumentGraph(ctx context.Context, kbID string) (graph.DocumentGraphStats, error) {
	if s.docClearErr != nil {
		return graph.DocumentGraphStats{}, s.docClearErr
	}
	return s.docClear, nil
}

func (s *stubGraphService) ExecuteQuery(ctx context.Context, cypher string, parameters map[string]interface{}) (graph.QueryResponse, error) {
	return graph.QueryResponse{}, nil
}

func (s *stubGraphService) DiscoverSchema(ctx context.Context, kbID string) (graph.GraphSchemaResponse, error) {
	s.discoverSchemaCalls++
	if s.schemaErr != nil {
		return graph.GraphSchemaResponse{}, s.schemaErr
	}
	return s.schema, nil
}

func (s *stubGraphService) ExpandNode(ctx context.Context, req graph.ExpandRequest) (graph.QueryResponse, error) {
	s.expandNodeCalls++
	return graph.QueryResponse{}, nil
}

func (s *stubGraphService) GetNodeDetail(ctx context.Context, nodeID string, kbID string) (graph.NodeDetail, error) {
	s.getNodeDetailCalls++
	if s.nodeErr != nil {
		return graph.NodeDetail{}, s.nodeErr
	}
	return s.nodeDetail, nil
}

// newGraphRouteKBStore 构造满足普通图谱只读路由二阶段鉴权的 KB 存储（M4-R1 FIX #3）。
// kb-a 为 active，tenant-a/project-a，与测试请求携带的 kb_id=kb-a 一致。
func newGraphRouteKBStore() *fakeUnifiedGraphBuildStore {
	return &fakeUnifiedGraphBuildStore{
		fakeAdminUserStore:   &fakeAdminUserStore{},
		fakeAdminConfigStore: &fakeAdminConfigStore{},
		fakeAdminLogStore:    &fakeAdminLogStore{},
		fakeAdminJobStore: &fakeAdminJobStore{
			kbRow: adminstore.KnowledgeBaseItem{
				ID: "kb-a", TenantID: "tenant-a", ProjectID: "project-a",
				Status: adminstore.KBStatusActive, StoragePrefix: "tenant-a/project-a/kb-a",
			},
		},
	}
}

func newTestLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestNodeDetailContractSuccess(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	graphSvc := &stubGraphService{nodeDetail: graph.NodeDetail{
		ID:     "42",
		Labels: []string{"Entity"},
		Properties: map[string]interface{}{
			"name": "Fhb1",
		},
		Media: map[string][]graph.MediaResource{
			"images": []graph.MediaResource{
				{Filename: "a.png", URL: "/api/media/a.png", Thumbnail: "/api/media/a.png"},
			},
			"videos": []graph.MediaResource{},
			"audios": []graph.MediaResource{},
		},
	}}

	registerRoutes(mux, cfg, newTestLogger(), graphSvc, nil, nil, nil, nil, nil, newGraphRouteKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/node/42?kb_id=kb-a", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var body map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body failed: %v", err)
	}
	data, ok := body["data"].(map[string]interface{})
	if !ok {
		t.Fatalf("response missing data envelope: %v", body)
	}
	if data["id"] != "42" {
		t.Fatalf("unexpected id: %v", data["id"])
	}
	if _, ok := data["media"]; !ok {
		t.Fatalf("response missing media field")
	}
	if body["trace_id"] == "" {
		t.Fatalf("response missing trace_id")
	}
}

func TestNodeDetailContractNotFound(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	graphSvc := &stubGraphService{nodeErr: graph.ErrNodeNotFound}

	registerRoutes(mux, cfg, newTestLogger(), graphSvc, nil, nil, nil, nil, nil, newGraphRouteKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/node/not-exists?kb_id=kb-a", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}

	var body map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body failed: %v", err)
	}
	if body["code"] != float64(http.StatusNotFound) {
		t.Fatalf("unexpected status code: %v", body["code"])
	}
	data, ok := body["data"].(map[string]interface{})
	if !ok {
		t.Fatalf("response missing data envelope: %v", body)
	}
	if data["error_code"] != "NODE_NOT_FOUND" {
		t.Fatalf("unexpected error code: %v", data["error_code"])
	}
	if body["trace_id"] == "" {
		t.Fatalf("response missing trace_id")
	}
}

func TestNodeDetailContractInternalError(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	graphSvc := &stubGraphService{nodeErr: errors.New("boom")}

	registerRoutes(mux, cfg, newTestLogger(), graphSvc, nil, nil, nil, nil, nil, newGraphRouteKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/node/42?kb_id=kb-a", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("expected 500, got %d", rec.Code)
	}
}

func TestGraphSchemaContractSuccess(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	graphSvc := &stubGraphService{schema: graph.GraphSchemaResponse{
		Labels: []graph.GraphLabelSummary{
			{Label: "Section", Count: 12},
		},
		Relationships: []graph.GraphRelationshipSummary{
			{Type: "HAS_SUBSECTION", Count: 11},
		},
		Patterns: []graph.GraphPatternSummary{
			{SourceLabels: []string{"Section"}, Relationship: "HAS_SUBSECTION", TargetLabels: []string{"Section"}, Count: 11},
		},
		SampleQuery: "MATCH (n)-[r]->(m) RETURN n,r,m LIMIT 80",
		Stats: graph.GraphSchemaStats{
			NodeCount: 12,
			EdgeCount: 11,
		},
	}}

	registerRoutes(mux, cfg, newTestLogger(), graphSvc, nil, nil, nil, nil, nil, newGraphRouteKBStore())

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/graph/schema?kb_id=kb-a", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var body map[string]interface{}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body failed: %v", err)
	}
	data, ok := body["data"].(map[string]interface{})
	if !ok {
		t.Fatalf("response missing data envelope: %v", body)
	}
	if data["sampleQuery"] == "" {
		t.Fatalf("response missing sampleQuery")
	}
	if _, ok := data["labels"]; !ok {
		t.Fatalf("response missing labels")
	}
	if _, ok := data["patterns"]; !ok {
		t.Fatalf("response missing patterns")
	}
}

func TestHealthReportsNeo4jProbeFailure(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	graphSvc := &stubGraphService{
		healthErr: errors.New("connection refused"),
		runtimeInfo: graph.RuntimeConnectionInfo{
			URI:             "bolt://runtime-db:7687",
			Database:        "runtime-graph",
			ConfigMode:      "auto",
			ConfigSource:    "admin",
			ResolutionError: "",
		},
	}

	registerRoutes(mux, cfg, newTestLogger(), graphSvc, nil, nil, nil, nil, nil, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var body struct {
		Data struct {
			Status string `json:"status"`
			Neo4j  struct {
				Connected       bool   `json:"connected"`
				Error           string `json:"error"`
				URI             string `json:"uri"`
				Database        string `json:"database"`
				ConfigMode      string `json:"config_mode"`
				ConfigSource    string `json:"config_source"`
				ResolutionError string `json:"resolution_error"`
			} `json:"neo4j"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body failed: %v", err)
	}
	if body.Data.Status != "degraded" {
		t.Fatalf("expected degraded status, got %q", body.Data.Status)
	}
	if body.Data.Neo4j.Connected {
		t.Fatalf("expected neo4j connected=false")
	}
	if body.Data.Neo4j.Error == "" {
		t.Fatalf("expected neo4j error")
	}
	if body.Data.Neo4j.URI != "bolt://runtime-db:7687" || body.Data.Neo4j.Database != "runtime-graph" {
		t.Fatalf("expected runtime neo4j info, got %#v", body.Data.Neo4j)
	}
	if body.Data.Neo4j.ConfigMode != "auto" || body.Data.Neo4j.ConfigSource != "admin" {
		t.Fatalf("expected runtime neo4j config mode/source, got %#v", body.Data.Neo4j)
	}
}

func TestHealthReportsGoDBAuthzModeTruthfully(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: true,
		RBACAuthzMode:          "go_db",
	}

	registerRoutes(mux, cfg, newTestLogger(), &stubGraphService{}, nil, nil, nil, nil, nil, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var body struct {
		Data struct {
			Authz struct {
				Connected                  bool   `json:"connected"`
				Mode                       string `json:"mode"`
				PermissionCheckViaUpstream bool   `json:"permission_check_via_upstream"`
				PermissionCheckViaLocal    bool   `json:"permission_check_via_local"`
				EnforceBusinessAPI         bool   `json:"enforce_business_api"`
				Error                      string `json:"error"`
			} `json:"authz"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body failed: %v", err)
	}
	if body.Data.Authz.Mode != "go_db" {
		t.Fatalf("expected authz mode go_db, got %q", body.Data.Authz.Mode)
	}
	if !body.Data.Authz.Connected {
		t.Fatalf("expected go_db authz connected=true")
	}
	if body.Data.Authz.PermissionCheckViaUpstream {
		t.Fatalf("expected go_db authz upstream flag=false")
	}
	if !body.Data.Authz.PermissionCheckViaLocal {
		t.Fatalf("expected go_db authz local flag=true")
	}
	if !body.Data.Authz.EnforceBusinessAPI {
		t.Fatalf("expected enforce_business_api=true")
	}
	if body.Data.Authz.Error != "" {
		t.Fatalf("expected no authz error, got %q", body.Data.Authz.Error)
	}
}

func TestHealthFallsBackToLocalAuthzForUnknownMode(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		RBACEnforceBusinessAPI: true,
		RBACAuthzMode:          "legacy_unknown_mode",
	}

	registerRoutes(mux, cfg, newTestLogger(), &stubGraphService{}, nil, nil, nil, nil, nil, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/health", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}

	var body struct {
		Data struct {
			Authz struct {
				Connected                  bool   `json:"connected"`
				Mode                       string `json:"mode"`
				PermissionCheckViaUpstream bool   `json:"permission_check_via_upstream"`
				PermissionCheckViaLocal    bool   `json:"permission_check_via_local"`
				Error                      string `json:"error"`
			} `json:"authz"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode body failed: %v", err)
	}
	if body.Data.Authz.Mode != "legacy_unknown_mode" {
		t.Fatalf("expected authz mode passthrough, got %q", body.Data.Authz.Mode)
	}
	if !body.Data.Authz.Connected {
		t.Fatalf("expected local authz fallback connected=true for unknown mode")
	}
	if body.Data.Authz.PermissionCheckViaUpstream {
		t.Fatalf("expected no upstream authz dependency for unknown mode fallback")
	}
	if !body.Data.Authz.PermissionCheckViaLocal {
		t.Fatalf("expected local authz flag=true")
	}
}
