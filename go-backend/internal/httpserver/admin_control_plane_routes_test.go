package httpserver

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
	"graphinsight/go-backend/internal/proxy"
)

func TestPublicClientLogsRouteIsGoNative(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerPublicCompatibilityRoutes(mux, logger)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/client-logs", strings.NewReader(`{"level":"warn","message":"frontend warning","source":"ui","event":"render"}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set(traceHeader, "trace-client-log")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.Code != http.StatusOK {
		t.Fatalf("unexpected response code: %d", resp.Code)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok || data["received"] != true {
		t.Fatalf("unexpected response data: %#v", resp.Data)
	}
}

func TestPublicProxyMediaRouteStreamsRemoteContent(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "image/png")
		_, _ = w.Write([]byte("png-remote"))
	}))
	t.Cleanup(upstream.Close)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerPublicCompatibilityRoutes(mux, logger)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/proxy-media?url="+url.QueryEscape(upstream.URL+"/img.png"), nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if rec.Header().Get("Content-Type") != "image/png" {
		t.Fatalf("unexpected content-type: %s", rec.Header().Get("Content-Type"))
	}
	if rec.Body.String() != "png-remote" {
		t.Fatalf("unexpected body: %q", rec.Body.String())
	}
}

func TestPublicVideoThumbnailRouteReturnsProxyMarkerForReachableVideo(t *testing.T) {
	t.Parallel()

	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodHead {
			w.WriteHeader(http.StatusOK)
			return
		}
		t.Fatalf("unexpected method: %s", r.Method)
	}))
	t.Cleanup(upstream.Close)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerPublicCompatibilityRoutes(mux, logger)

	target := upstream.URL + "/demo.mp4"
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/video-thumbnail?url="+url.QueryEscape(target), nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if rec.Header().Get("Content-Type") != "text/plain; charset=utf-8" {
		t.Fatalf("unexpected content-type: %s", rec.Header().Get("Content-Type"))
	}
	if rec.Body.String() != "VIDEO_PROXY:"+target {
		t.Fatalf("unexpected body: %q", rec.Body.String())
	}
}

func newProxyClientForTest(t *testing.T, handler http.HandlerFunc) *proxy.Client {
	t.Helper()
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)

	client, err := proxy.New(config.Config{
		PythonBackendBaseURL:        srv.URL,
		PythonBackendTimeoutSeconds: 2,
		PythonBackendForwardAuth:    true,
	})
	if err != nil {
		t.Fatalf("new proxy client: %v", err)
	}
	return client
}

func newGoDBPermissionGuardForTest(permissionCapture *string, userID int) businessPermissionGuard {
	if userID <= 0 {
		userID = 1
	}
	store := &fakeAdminPermissionStoreForControlPlane{
		result: authz.CheckResult{
			Allowed: true,
			Reason:  "ok",
			UserID:  userID,
			User:    "test-admin",
			Email:   "admin@example.com",
		},
		permissionCapture: permissionCapture,
	}
	return newBusinessPermissionGuard(config.Config{
		RBACEnforceBusinessAPI: true,
		RBACAuthzMode:          "go_db",
		AdminSecretKey:         "test-secret",
	}, slog.New(slog.NewTextHandler(io.Discard, nil)), store)
}

func newAdminAuthRequestToken(t *testing.T) string {
	t.Helper()
	return issueTestAdminJWT(t, "admin@example.com", "test-secret", time.Now().Add(time.Hour))
}

type fakeAdminPermissionStoreForControlPlane struct {
	result            authz.CheckResult
	err               error
	permissionCapture *string
}

func (s *fakeAdminPermissionStoreForControlPlane) CheckPermission(_ context.Context, _ string, permission string, _ map[string]string) (authz.CheckResult, error) {
	if s.permissionCapture != nil {
		*s.permissionCapture = permission
	}
	return s.result, s.err
}

func TestAdminMonitorStatsNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor stats route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutes(mux, logger, pythonWakeClient, nil, guard)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/stats", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if resp.Code != http.StatusOK {
		t.Fatalf("unexpected response code: %d", resp.Code)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	for _, key := range []string{
		"cpu_percent",
		"memory_percent",
		"memory_used_mb",
		"memory_total_mb",
		"disk_percent",
		"disk_used_gb",
		"disk_total_gb",
		"uptime_seconds",
		"timestamp",
	} {
		if _, exists := data[key]; !exists {
			t.Fatalf("missing monitor stats key %s in %#v", key, data)
		}
	}
}

func TestAdminMonitorHealthNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor health route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{
		AppName:                "GraphInsight Go API",
		Version:                "test",
		Neo4jURI:               "bolt://neo4j:7687",
		Neo4jDatabase:          "neo4j",
		AIProvider:             "openai",
		AIModel:                "qwen-flash",
		AIAPIKey:               "sk-test",
		RBACEnforceBusinessAPI: false,
	}
	graphSvc := &stubGraphService{
		runtimeInfo: graph.RuntimeConnectionInfo{
			URI:          "bolt://runtime-neo4j:7687",
			Database:     "runtime-db",
			ConfigMode:   "auto",
			ConfigSource: "admin",
		},
		counts: graph.GraphCounts{
			NodeCount:         12,
			RelationshipCount: 34,
		},
	}
	registerRoutes(mux, cfg, logger, graphSvc, nil, pythonWakeClient, nil, nil, nil, nil)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/health", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["status"] == "" {
		t.Fatalf("expected health status, got %#v", data)
	}
	neo4j, ok := data["neo4j"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected neo4j object, got %#v", data["neo4j"])
	}
	if neo4j["connected"] != true {
		t.Fatalf("expected neo4j connected, got %#v", neo4j)
	}
	if neo4j["uri"] != "bolt://runtime-neo4j:7687" || neo4j["database"] != "runtime-db" {
		t.Fatalf("expected runtime neo4j endpoint info, got %#v", neo4j)
	}
	if neo4j["nodes_count"] != float64(12) || neo4j["relationships_count"] != float64(34) {
		t.Fatalf("unexpected graph counts: %#v", neo4j)
	}
	aiService, ok := data["ai_service"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected ai_service object, got %#v", data["ai_service"])
	}
	if aiService["api_key_configured"] != true {
		t.Fatalf("expected configured ai service, got %#v", aiService)
	}
}

func TestCollectAdminHealthStatusIncludesMilvusPythonAndStoredAIConfig(t *testing.T) {
	t.Parallel()

	pythonServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/health" {
			t.Fatalf("unexpected Python health path: %s", r.URL.Path)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"healthy"}`))
	}))
	t.Cleanup(pythonServer.Close)

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("start Milvus test listener: %v", err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		conn, acceptErr := listener.Accept()
		if acceptErr == nil {
			_ = conn.Close()
		}
	}()

	health := collectAdminHealthStatus(context.Background(), config.Config{
		PythonBackendBaseURL: pythonServer.URL,
	}, slog.New(slog.NewTextHandler(io.Discard, nil)), &stubGraphService{}, nil, &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"ai_service": {
				"provider": "openai",
				"model":    "stored-model",
				"api_key":  "vg-stored-key",
			},
			"vector_store": {
				"enabled": "true",
				"uri":     "http://" + listener.Addr().String(),
			},
		},
	})
	if !health.Milvus.Connected || !health.Milvus.Enabled {
		t.Fatalf("expected enabled and connected Milvus: %#v", health.Milvus)
	}
	if !health.PythonBackend.Connected {
		t.Fatalf("expected connected Python backend: %#v", health.PythonBackend)
	}
	if !health.AIService.APIKeyConfigured || health.AIService.Model != "stored-model" {
		t.Fatalf("expected stored AI config in health: %#v", health.AIService)
	}
}

func TestAdminMonitorPerformanceNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor performance route")
	})
	metrics := newAPIMetrics(10)
	metrics.Observe(http.MethodGet, "/health", http.StatusOK, 10)
	metrics.Observe(http.MethodGet, "/api/missing", http.StatusNotFound, 25)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	store := &fakeAdminMonitorStore{
		jobSLO: adminstore.JobSLOMetrics{
			WindowMinutes:     30,
			TotalJobs:         2,
			SucceededJobs:     1,
			FailedJobs:        1,
			TimeoutFailedJobs: 1,
			SuccessRate:       0.5,
			TimeoutRate:       0.5,
			P95DurationMS:     123,
			P99DurationMS:     123,
			Timestamp:         time.Now().UTC().Format(time.RFC3339),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, metrics, pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/performance?window_seconds=60", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["window_seconds"] != float64(60) {
		t.Fatalf("unexpected window_seconds: %#v", data["window_seconds"])
	}
	if data["total_requests"] != float64(2) {
		t.Fatalf("unexpected total_requests: %#v", data["total_requests"])
	}
	if data["failed_requests"] != float64(1) {
		t.Fatalf("unexpected failed_requests: %#v", data["failed_requests"])
	}
	if _, ok := data["top_paths"].([]interface{}); !ok {
		t.Fatalf("expected top_paths array, got %#v", data["top_paths"])
	}
}

func TestAdminMonitorSLONativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor slo route")
	})
	metrics := newAPIMetrics(10)
	metrics.Observe(http.MethodGet, "/health", http.StatusOK, 10)
	metrics.Observe(http.MethodGet, "/api/fail", http.StatusInternalServerError, 30)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	store := &fakeAdminMonitorStore{
		jobSLO: adminstore.JobSLOMetrics{
			WindowMinutes:     30,
			TotalJobs:         2,
			SucceededJobs:     1,
			FailedJobs:        1,
			TimeoutFailedJobs: 1,
			SuccessRate:       0.5,
			TimeoutRate:       0.5,
			P95DurationMS:     123,
			P99DurationMS:     123,
			Timestamp:         time.Now().UTC().Format(time.RFC3339),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, metrics, pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/slo?api_window_seconds=60&job_window_minutes=30", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	apiData, ok := data["api"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected api object, got %#v", data["api"])
	}
	if apiData["window_seconds"] != float64(60) {
		t.Fatalf("unexpected api window: %#v", apiData["window_seconds"])
	}
	jobs, ok := data["jobs"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected jobs object, got %#v", data["jobs"])
	}
	if jobs["window_minutes"] != float64(30) {
		t.Fatalf("unexpected job window: %#v", jobs["window_minutes"])
	}
	if jobs["total_jobs"] != float64(2) || jobs["timeout_rate"] != 0.5 {
		t.Fatalf("unexpected job slo data: %#v", jobs)
	}
	if store.jobWindowMinutes != 30 {
		t.Fatalf("expected job window 30, got %d", store.jobWindowMinutes)
	}
	slo, ok := data["slo"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected slo object, got %#v", data["slo"])
	}
	for _, key := range []string{"api_error_rate", "job_success_rate", "job_timeout_rate", "job_p95_duration_ms"} {
		if _, exists := slo[key]; !exists {
			t.Fatalf("missing slo key %s in %#v", key, slo)
		}
	}
}

func TestAdminMonitorUnifiedMetricsNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor unified metrics route")
	})
	metrics := newAPIMetrics(10)
	metrics.Observe(http.MethodGet, "/health", http.StatusOK, 10)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	store := &fakeAdminMonitorStore{
		qa: adminstore.QAQualitySnapshot{
			WindowSeconds: 120,
			TotalRequests: 4,
			SuccessRate:   0.75,
			CitationRate:  0.5,
			ByType:        []adminstore.QATypeMetric{},
			Timestamp:     time.Now().UTC().Format(time.RFC3339),
		},
		jobSLO: adminstore.JobSLOMetrics{
			WindowMinutes: 15,
			TotalJobs:     3,
			SucceededJobs: 2,
			SuccessRate:   0.666667,
			TimeoutRate:   0,
			Timestamp:     time.Now().UTC().Format(time.RFC3339),
		},
		logSeverity: adminstore.LogSeverityMetrics{
			WindowMinutes: 15,
			TotalLogs:     5,
			SeverityCount: map[string]int{"info": 4, "warn": 1, "error": 0},
			StatusCounts:  map[string]int{"success": 5},
			WarnRate:      0.2,
			TopActions:    []adminstore.LogTopItem{},
			TopResources:  []adminstore.LogTopItem{},
			AlertRoutes:   map[string]adminstore.LogAlertRoute{},
			RecentAlerts:  []adminstore.LogAlertItem{},
			Timestamp:     time.Now().UTC().Format(time.RFC3339),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, metrics, pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/metrics/unified?api_window_seconds=60&qa_window_seconds=120&job_window_minutes=15", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	for _, key := range []string{"summary", "api", "qa", "jobs", "logs", "timestamp"} {
		if _, exists := data[key]; !exists {
			t.Fatalf("missing unified metric key %s in %#v", key, data)
		}
	}
	summary, ok := data["summary"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected summary object, got %#v", data["summary"])
	}
	for _, key := range []string{"api_error_rate", "qa_success_rate", "job_success_rate", "log_warn_rate"} {
		if _, exists := summary[key]; !exists {
			t.Fatalf("missing summary key %s in %#v", key, summary)
		}
	}
	if store.qaWindowSeconds != 120 {
		t.Fatalf("expected qa window 120, got %d", store.qaWindowSeconds)
	}
	if store.jobWindowMinutes != 15 {
		t.Fatalf("expected job window 15, got %d", store.jobWindowMinutes)
	}
	if store.logWindowMinutes != 15 {
		t.Fatalf("expected log window 15, got %d", store.logWindowMinutes)
	}
}

func TestAdminMonitorQANativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor qa route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	store := &fakeAdminMonitorStore{
		qa: adminstore.QAQualitySnapshot{
			WindowSeconds: 60,
			ByType:        []adminstore.QATypeMetric{},
			Timestamp:     time.Now().UTC().Format(time.RFC3339),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/qa?window_seconds=60", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["window_seconds"] != float64(60) {
		t.Fatalf("unexpected window_seconds: %#v", data["window_seconds"])
	}
	if store.qaWindowSeconds != 60 {
		t.Fatalf("expected qa window 60, got %d", store.qaWindowSeconds)
	}
	for _, key := range []string{
		"total_requests",
		"failed_requests",
		"success_rate",
		"failure_rate",
		"citation_rate",
		"avg_citations",
		"avg_latency_ms",
		"p50_latency_ms",
		"p95_latency_ms",
		"p99_latency_ms",
		"by_type",
		"timestamp",
	} {
		if _, exists := data[key]; !exists {
			t.Fatalf("missing qa metric key %s in %#v", key, data)
		}
	}
	if _, ok := data["by_type"].([]interface{}); !ok {
		t.Fatalf("expected by_type array, got %#v", data["by_type"])
	}
}

func TestUnknownAdminRouteIsOwnedByGoNative404(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unknown admin route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutes(mux, logger, pythonWakeClient, nil, guard)
	registerMediaRoutes(mux, logger, config.Config{MediaStoragePath: t.TempDir()})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/unknown-module", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestUnknownNonAdminAPIV1RouteIsNotLegacyProxied(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerMediaRoutes(mux, logger, config.Config{MediaStoragePath: t.TempDir()})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/legacy/debug", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestMediaRouteServesLocalFileAsGoNative(t *testing.T) {
	t.Parallel()

	mediaDir := t.TempDir()
	target := filepath.Join(mediaDir, "image.png")
	if err := os.WriteFile(target, []byte("png-bytes"), 0o600); err != nil {
		t.Fatalf("write media file: %v", err)
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerMediaRoutes(mux, logger, config.Config{MediaStoragePath: mediaDir})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/media/image.png", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if body := rec.Body.String(); body != "png-bytes" {
		t.Fatalf("unexpected body: %q", body)
	}
}

func TestMediaRouteReturns404ForMissingFile(t *testing.T) {
	t.Parallel()

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	registerMediaRoutes(mux, logger, config.Config{MediaStoragePath: t.TempDir()})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/media/missing.png", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminMonitorAlertsCheckNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor alerts check route")
	})
	metrics := newAPIMetrics(10)
	metrics.Observe(http.MethodGet, "/health", http.StatusOK, 10)
	metrics.Observe(http.MethodGet, "/api/fail", http.StatusInternalServerError, 30)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	store := &fakeAdminMonitorStore{
		jobSLO: adminstore.JobSLOMetrics{
			WindowMinutes:     30,
			TotalJobs:         2,
			SucceededJobs:     1,
			FailedJobs:        1,
			TimeoutFailedJobs: 1,
			SuccessRate:       0.5,
			TimeoutRate:       0.5,
			Timestamp:         time.Now().UTC().Format(time.RFC3339),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, metrics, pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/monitor/alerts/check?api_window_seconds=60&job_window_minutes=30", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["alert_count"] != float64(2) {
		t.Fatalf("unexpected alert_count: %#v", data["alert_count"])
	}
	if data["sent"] != false {
		t.Fatalf("expected sent=false, got %#v", data["sent"])
	}
	if data["webhook_configured"] != false {
		t.Fatalf("expected webhook_configured=false, got %#v", data["webhook_configured"])
	}
	alerts, ok := data["alerts"].([]interface{})
	if !ok || len(alerts) != 2 {
		t.Fatalf("expected one alert, got %#v", data["alerts"])
	}
	firstAlert, ok := alerts[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected alert object, got %#v", alerts[0])
	}
	if firstAlert["type"] != "api_error_rate_high" {
		t.Fatalf("unexpected alert type: %#v", firstAlert)
	}
	secondAlert, ok := alerts[1].(map[string]interface{})
	if !ok {
		t.Fatalf("expected alert object, got %#v", alerts[1])
	}
	if secondAlert["type"] != "job_timeout_rate_high" {
		t.Fatalf("unexpected second alert type: %#v", secondAlert)
	}
	snapshot, ok := data["snapshot"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected snapshot object, got %#v", data["snapshot"])
	}
	if _, ok := snapshot["api"].(map[string]interface{}); !ok {
		t.Fatalf("expected api snapshot object, got %#v", snapshot["api"])
	}
	if jobs, ok := snapshot["jobs"].(map[string]interface{}); !ok || jobs["window_minutes"] != float64(30) {
		t.Fatalf("unexpected jobs snapshot: %#v", snapshot["jobs"])
	}
	if store.jobWindowMinutes != 30 {
		t.Fatalf("expected job window 30, got %d", store.jobWindowMinutes)
	}
}

func TestAdminMonitorAlertsCheckNativeRouteUsesReadPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor alerts check route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: true}
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminMonitorStore{
		jobSLO: adminstore.JobSLOMetrics{
			WindowMinutes: 60,
			Timestamp:     time.Now().UTC().Format(time.RFC3339),
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/monitor/alerts/check", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if gotPermission != "monitor:read" {
		t.Fatalf("expected monitor:read permission, got %s", gotPermission)
	}
}

func TestAdminMonitorLogSeverityNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor log severity route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	store := &fakeAdminMonitorStore{
		logSeverity: adminstore.LogSeverityMetrics{
			WindowMinutes: 30,
			SeverityCount: map[string]int{
				"info":  0,
				"warn":  0,
				"error": 0,
			},
			StatusCounts: map[string]int{},
			TopActions:   []adminstore.LogTopItem{},
			TopResources: []adminstore.LogTopItem{},
			AlertRoutes: map[string]adminstore.LogAlertRoute{
				"error": {
					Policy:       "page_or_webhook",
					ThresholdEnv: "ALERT_LOG_ERROR_RATE_THRESHOLD",
				},
				"warn": {
					Policy:       "webhook_or_digest",
					ThresholdEnv: "ALERT_LOG_WARN_RATE_THRESHOLD",
				},
			},
			RecentAlerts: []adminstore.LogAlertItem{},
			Timestamp:    time.Now().UTC().Format(time.RFC3339),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/log-severity?window_minutes=30", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["window_minutes"] != float64(30) {
		t.Fatalf("unexpected window_minutes: %#v", data["window_minutes"])
	}
	if store.logWindowMinutes != 30 {
		t.Fatalf("expected log window 30, got %d", store.logWindowMinutes)
	}
	for _, key := range []string{
		"total_logs",
		"failed_logs",
		"severity_counts",
		"status_counts",
		"error_rate",
		"warn_rate",
		"failed_rate",
		"top_actions",
		"top_resources",
		"alert_routes",
		"recent_alerts",
		"timestamp",
	} {
		if _, exists := data[key]; !exists {
			t.Fatalf("missing log severity key %s in %#v", key, data)
		}
	}
	severityCounts, ok := data["severity_counts"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected severity_counts object, got %#v", data["severity_counts"])
	}
	for _, key := range []string{"info", "warn", "error"} {
		if _, exists := severityCounts[key]; !exists {
			t.Fatalf("missing severity count %s in %#v", key, severityCounts)
		}
	}
	if _, ok := data["top_actions"].([]interface{}); !ok {
		t.Fatalf("expected top_actions array, got %#v", data["top_actions"])
	}
	if _, ok := data["recent_alerts"].([]interface{}); !ok {
		t.Fatalf("expected recent_alerts array, got %#v", data["recent_alerts"])
	}
}

func TestAdminMonitorLogSeverityNativeRouteUsesReadPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native monitor log severity route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AppName: "GraphInsight Go API", Version: "test", RBACEnforceBusinessAPI: true}
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminMonitorStore{
		logSeverity: adminstore.LogSeverityMetrics{
			WindowMinutes: 60,
			SeverityCount: map[string]int{
				"info":  0,
				"warn":  0,
				"error": 0,
			},
			StatusCounts: map[string]int{},
			TopActions:   []adminstore.LogTopItem{},
			TopResources: []adminstore.LogTopItem{},
			AlertRoutes:  map[string]adminstore.LogAlertRoute{},
			RecentAlerts: []adminstore.LogAlertItem{},
			Timestamp:    time.Now().UTC().Format(time.RFC3339),
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/log-severity", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if gotPermission != "monitor:read" {
		t.Fatalf("expected monitor:read permission, got %s", gotPermission)
	}
}

func TestAdminMonitorSimpleHealthSkipsPermission(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native simple health route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutes(mux, logger, pythonWakeClient, nil, guard)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/monitor/health/simple", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminJobsListNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native jobs list route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminJobStore{
		listResult: adminstore.JobListResult{
			Items: []adminstore.JobItem{
				{ID: 12, JobType: "build_graph", Status: "failed", Payload: map[string]interface{}{}, RetryCount: 1, MaxRetries: 3, CreatedAt: time.Date(2026, 6, 5, 11, 0, 0, 0, time.UTC)},
			},
			Total: 21,
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/jobs?page=2&page_size=10&status=failed&job_type=build_graph&tenant_id=tenant-1&project_id=project-1&kb_id=kb-1", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["page"] != float64(2) || data["page_size"] != float64(10) {
		t.Fatalf("unexpected pagination: %#v", data)
	}
	if data["total"] != float64(21) || data["total_pages"] != float64(3) {
		t.Fatalf("unexpected totals: %#v", data)
	}
	if _, ok := data["items"].([]interface{}); !ok {
		t.Fatalf("expected items array, got %#v", data["items"])
	}
	if store.listQuery.Page != 2 || store.listQuery.PageSize != 10 || store.listQuery.Status != "failed" || store.listQuery.JobType != "build_graph" {
		t.Fatalf("unexpected list query: %#v", store.listQuery)
	}
	if store.listQuery.TenantID != "tenant-1" || store.listQuery.ProjectID != "project-1" || store.listQuery.KBID != "kb-1" {
		t.Fatalf("unexpected list filters: %#v", store.listQuery)
	}
}

func TestAdminJobsListNativeRouteUsesReadPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native jobs list route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminJobStore{
		listResult: adminstore.JobListResult{Items: []adminstore.JobItem{}, Total: 0},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/jobs", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if gotPermission != "job:read" {
		t.Fatalf("expected job:read permission, got %s", gotPermission)
	}
}

func TestAdminJobsDetailAndLogsNativeRoutesSkipProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native jobs read route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminJobStore{
		detail: adminstore.JobItem{
			ID:         12,
			JobType:    "build_graph",
			Status:     "succeeded",
			Payload:    map[string]interface{}{"source": "test"},
			RetryCount: 0,
			MaxRetries: 3,
			CreatedAt:  time.Date(2026, 6, 5, 11, 0, 0, 0, time.UTC),
		},
		logs: adminstore.JobLogListResult{
			Items: []adminstore.JobLogItem{
				{ID: 30, Action: "job_started", Status: "success", CreatedAt: time.Date(2026, 6, 5, 11, 1, 0, 0, time.UTC), Details: map[string]interface{}{"job_type": "build_graph"}},
			},
			Total: 1,
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	detailRec := httptest.NewRecorder()
	detailReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/jobs/12", nil)
	mux.ServeHTTP(detailRec, detailReq)
	if detailRec.Code != http.StatusOK {
		t.Fatalf("expected detail 200, got %d", detailRec.Code)
	}
	if detailRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected detail route owner: %s", detailRec.Header().Get(routeOwnerHeader))
	}

	logsRec := httptest.NewRecorder()
	logsReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/jobs/12/logs?page=1&page_size=100", nil)
	mux.ServeHTTP(logsRec, logsReq)
	if logsRec.Code != http.StatusOK {
		t.Fatalf("expected logs 200, got %d", logsRec.Code)
	}
	if logsRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected logs route owner: %s", logsRec.Header().Get(routeOwnerHeader))
	}
	var resp APIResponse
	if err := json.NewDecoder(logsRec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode logs response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected logs object data, got %#v", resp.Data)
	}
	if data["page_size"] != float64(100) {
		t.Fatalf("unexpected logs page_size: %#v", data)
	}
	if data["total"] != float64(1) {
		t.Fatalf("unexpected logs total: %#v", data)
	}
	if _, ok := data["items"].([]interface{}); !ok {
		t.Fatalf("expected log items array, got %#v", data["items"])
	}
	if store.detailID != 12 || store.logsJobID != 12 || store.logsPage != 1 || store.logsPageSize != 100 {
		t.Fatalf("unexpected store calls: detail=%d logs=%d page=%d pageSize=%d", store.detailID, store.logsJobID, store.logsPage, store.logsPageSize)
	}
}

type fakeAdminJobStore struct {
	listQuery    adminstore.JobListQuery
	listResult   adminstore.JobListResult
	listErr      error
	detailID     int
	detail       adminstore.JobItem
	detailErr    error
	logsJobID    int
	logsPage     int
	logsPageSize int
	logs         adminstore.JobLogListResult
	logsErr      error
	createReq    adminstore.JobCreateRequest
	createResult adminstore.JobItem
	createErr    error
	retryReq     adminstore.JobRetryRequest
	retryResult  adminstore.JobItem
	retryErr     error
	cancelReq    adminstore.JobCancelRequest
	cancelResult adminstore.JobItem
	cancelErr    error
}

func (s *fakeAdminJobStore) ListJobs(_ context.Context, query adminstore.JobListQuery) (adminstore.JobListResult, error) {
	s.listQuery = query
	return s.listResult, s.listErr
}

func (s *fakeAdminJobStore) GetJob(_ context.Context, jobID int) (adminstore.JobItem, error) {
	s.detailID = jobID
	return s.detail, s.detailErr
}

func (s *fakeAdminJobStore) ListJobLogs(_ context.Context, jobID int, page int, pageSize int) (adminstore.JobLogListResult, error) {
	s.logsJobID = jobID
	s.logsPage = page
	s.logsPageSize = pageSize
	return s.logs, s.logsErr
}

func (s *fakeAdminJobStore) CreateJob(_ context.Context, req adminstore.JobCreateRequest) (adminstore.JobItem, error) {
	s.createReq = req
	return s.createResult, s.createErr
}

func (s *fakeAdminJobStore) RetryJob(_ context.Context, req adminstore.JobRetryRequest) (adminstore.JobItem, error) {
	s.retryReq = req
	return s.retryResult, s.retryErr
}

func (s *fakeAdminJobStore) CancelJob(_ context.Context, req adminstore.JobCancelRequest) (adminstore.JobItem, error) {
	s.cancelReq = req
	return s.cancelResult, s.cancelErr
}

func TestAdminJobsRetryNativeRouteUsesManagePermission(t *testing.T) {
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
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"accepted":true}}`))
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 7)
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	store := &fakeAdminJobStore{
		retryResult: adminstore.JobItem{ID: 12, JobType: "build_graph", Status: "pending", Payload: map[string]interface{}{}, RetryCount: 1, MaxRetries: 3, CreatedAt: createdAt},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/12:retry", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	req.Header.Set(traceHeader, "trace-retry")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.retryReq.JobID != 12 {
		t.Fatalf("unexpected retry request: %#v", store.retryReq)
	}
	if store.retryReq.OperatorID == nil || *store.retryReq.OperatorID != 7 {
		t.Fatalf("expected operator from authz response, got %#v", store.retryReq.OperatorID)
	}
	if store.retryReq.TraceID == nil || *store.retryReq.TraceID != "trace-retry" {
		t.Fatalf("expected trace id, got %#v", store.retryReq.TraceID)
	}
	if !wakeCalled {
		t.Fatalf("expected python wake to be triggered")
	}
}

func TestAdminJobsCreateNativeRouteUsesManagePermission(t *testing.T) {
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
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"accepted":true}}`))
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 10)
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	store := &fakeAdminJobStore{
		createResult: adminstore.JobItem{ID: 20, JobType: "build_graph", Status: "pending", Payload: map[string]interface{}{"force": true}, RetryCount: 0, MaxRetries: 3, CreatedAt: createdAt},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &struct {
		*fakeAdminJobStore
		*fakeAdminConfigStore
	}{
		fakeAdminJobStore: store,
		fakeAdminConfigStore: &fakeAdminConfigStore{
			values: map[string]map[string]string{
				"ai_service": {
					"graph_extract_reasoning_profile":         "fast",
					"graph_extract_complex_reasoning_profile": "balanced",
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/build-graph", strings.NewReader(`{"tenant_id":"tenant-a","payload":{"force":true},"max_retries":3}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	req.Header.Set(traceHeader, "trace-create-job")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.createReq.JobType != "build_graph" || store.createReq.MaxRetries != 3 {
		t.Fatalf("unexpected create request: %#v", store.createReq)
	}
	if store.createReq.TenantID == nil || *store.createReq.TenantID != "tenant-a" {
		t.Fatalf("unexpected tenant id: %#v", store.createReq.TenantID)
	}
	if store.createReq.RequestedBy == nil || *store.createReq.RequestedBy != 10 {
		t.Fatalf("expected requester from authz response, got %#v", store.createReq.RequestedBy)
	}
	if store.createReq.TraceID == nil || *store.createReq.TraceID != "trace-create-job" {
		t.Fatalf("expected trace id, got %#v", store.createReq.TraceID)
	}
	if force, ok := store.createReq.Payload["force"].(bool); !ok || !force {
		t.Fatalf("unexpected payload: %#v", store.createReq.Payload)
	}
	if store.createReq.Payload["reasoning_profile"] != "fast" {
		t.Fatalf("expected build_graph default reasoning profile, got %#v", store.createReq.Payload["reasoning_profile"])
	}
	if !wakeCalled {
		t.Fatalf("expected python wake to be triggered")
	}
}

func TestAdminBuildGraphJobCreateInjectsComplexScenarioReasoningProfile(t *testing.T) {
	t.Parallel()
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write([]byte(`{"code":200,"message":"ok","data":{"accepted":true}}`))
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	store := &fakeAdminJobStore{
		createResult: adminstore.JobItem{ID: 21, JobType: "build_graph", Status: "pending", Payload: map[string]interface{}{"force": false}, RetryCount: 0, MaxRetries: 3, CreatedAt: createdAt},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &struct {
		*fakeAdminJobStore
		*fakeAdminConfigStore
	}{
		fakeAdminJobStore: store,
		fakeAdminConfigStore: &fakeAdminConfigStore{
			values: map[string]map[string]string{
				"ai_service": {
					"graph_extract_reasoning_profile":         "fast",
					"graph_extract_complex_reasoning_profile": "balanced",
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/build-graph", strings.NewReader(`{"tenant_id":"tenant-a","payload":{"force":false,"complex_extraction":true},"max_retries":3}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	req.Header.Set(traceHeader, "trace-create-job-complex")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("expected 201, got %d body=%s", rec.Code, rec.Body.String())
	}
	if store.createReq.Payload["reasoning_profile"] != "balanced" {
		t.Fatalf("expected complex build_graph reasoning profile, got %#v", store.createReq.Payload["reasoning_profile"])
	}
	if store.createReq.Payload["complex_extraction"] != true {
		t.Fatalf("expected complex_extraction=true, got %#v", store.createReq.Payload["complex_extraction"])
	}
}

func TestAdminJobsRejectsUnknownSubpath(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unknown jobs subpath")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutes(mux, logger, pythonWakeClient, nil, guard)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodDelete, "/api/v1/admin/jobs/build-graph", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", rec.Code)
	}
}

func TestAdminQATracesNativeListRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native qa traces list route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminQATraceStore{
		listResult: adminstore.QATraceListResult{
			Items: []adminstore.QATraceItem{
				{ID: 20, QAType: "docqa", Status: "success", Question: "wheat?", RetrievalCount: 2, CitationCount: 1, ReasoningProfile: optionalStringPtr("balanced"), CreatedAt: time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)},
			},
			Total: 11,
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/qa-traces?page=2&page_size=10&keyword=wheat&qa_type=docqa&status=success&trace_id=trace-1&operator_id=3", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["page"] != float64(2) || data["page_size"] != float64(10) {
		t.Fatalf("unexpected pagination: %#v", data)
	}
	if data["total"] != float64(11) || data["total_pages"] != float64(2) {
		t.Fatalf("unexpected totals: %#v", data)
	}
	if _, ok := data["items"].([]interface{}); !ok {
		t.Fatalf("expected items array, got %#v", data["items"])
	}
	items := data["items"].([]interface{})
	firstItem, ok := items[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected first item object, got %#v", items[0])
	}
	if firstItem["reasoning_profile"] != "balanced" {
		t.Fatalf("unexpected reasoning profile: %#v", firstItem["reasoning_profile"])
	}
	if store.listQuery.Page != 2 || store.listQuery.PageSize != 10 || store.listQuery.Keyword != "wheat" {
		t.Fatalf("unexpected list query: %#v", store.listQuery)
	}
	if store.listQuery.QAType != "docqa" || store.listQuery.Status != "success" || store.listQuery.TraceID != "trace-1" {
		t.Fatalf("unexpected list filters: %#v", store.listQuery)
	}
	if store.listQuery.OperatorID == nil || *store.listQuery.OperatorID != 3 {
		t.Fatalf("unexpected operator filter: %#v", store.listQuery.OperatorID)
	}
}

func TestAdminQATracesNativeRouteRejectsWrongMethod(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for wrong method")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminQATraceStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/qa-traces", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminQATracesDetailNativeRouteUsesReadPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native qa traces detail route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	store := &fakeAdminQATraceStore{
		detail: adminstore.QATraceDetail{
			ID:             21,
			QAType:         "docqa",
			Status:         "success",
			Question:       "trace?",
			RetrievalCount: 2,
			CitationCount:  1,
			CreatedAt:      time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/qa-traces/trace-1", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if gotPermission != "monitor:read" {
		t.Fatalf("expected monitor:read permission, got %s", gotPermission)
	}
	if store.detailKey != "trace-1" {
		t.Fatalf("unexpected detail key: %s", store.detailKey)
	}
}

func TestAdminQATracesCostSummaryNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native qa traces cost summary route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminQATraceStore{
		summary: adminstore.QACostSummary{
			WindowHours:      24,
			TotalCalls:       2,
			SuccessCalls:     1,
			FailedCalls:      1,
			SuccessRate:      0.5,
			PromptTokens:     10,
			CompletionTokens: 5,
			TotalTokens:      15,
			EstimatedCost:    0.0922,
			Currency:         "USD",
			PricingSource:    "not_configured",
			Models:           []adminstore.QACostModelBreakdown{},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/qa-traces/cost-summary?window_hours=24", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["window_hours"] != float64(24) {
		t.Fatalf("unexpected window_hours: %#v", data["window_hours"])
	}
	if data["total_calls"] != float64(2) || data["success_calls"] != float64(1) {
		t.Fatalf("unexpected cost summary data: %#v", data)
	}
	if data["estimated_cost"] != 0.0922 {
		t.Fatalf("unexpected estimated_cost: %#v", data["estimated_cost"])
	}
	for _, key := range []string{
		"total_calls",
		"success_calls",
		"failed_calls",
		"success_rate",
		"prompt_tokens",
		"completion_tokens",
		"total_tokens",
		"estimated_cost",
		"currency",
		"pricing_source",
		"models",
	} {
		if _, exists := data[key]; !exists {
			t.Fatalf("missing cost summary key %s in %#v", key, data)
		}
	}
	if _, ok := data["models"].([]interface{}); !ok {
		t.Fatalf("expected models array, got %#v", data["models"])
	}
}

func TestAdminQATracesCostSummaryNativeRouteUsesReadPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native qa traces cost summary route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminQATraceStore{summary: adminstore.QACostSummary{WindowHours: 24, Currency: "USD", PricingSource: "not_configured", Models: []adminstore.QACostModelBreakdown{}}})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/qa-traces/cost-summary?window_hours=24", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if gotPermission != "monitor:read" {
		t.Fatalf("expected monitor:read permission, got %s", gotPermission)
	}
}

func TestAdminRetrievalDiagnosticsRouteForwardsToPythonInternal(t *testing.T) {
	t.Parallel()

	var upstreamPath string
	var upstreamHeader string
	var upstreamPayload adminRetrievalDiagnosticsPayload
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		upstreamPath = r.URL.Path
		upstreamHeader = r.Header.Get("X-Go-Proxy")
		if r.Method != http.MethodPost {
			t.Fatalf("unexpected method: %s", r.Method)
		}
		if err := json.NewDecoder(r.Body).Decode(&upstreamPayload); err != nil {
			t.Fatalf("decode upstream payload: %v", err)
		}
		WriteJSON(w, http.StatusOK, "ok", map[string]interface{}{"accepted": true})
	})
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	mux := http.NewServeMux()
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminQATraceStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(
		http.MethodPost,
		"/api/v1/admin/qa/retrieval-diagnostics",
		strings.NewReader(`{"question":"  hybrid search  ","top_k":30,"modes":["hybrid","bad","vector","hybrid"]}`),
	)
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-control-plane" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if upstreamPath != "/api/internal/docqa/retrieval-diagnostics" {
		t.Fatalf("unexpected upstream path: %s", upstreamPath)
	}
	if upstreamHeader != "graphinsight-go" {
		t.Fatalf("expected go control header, got %q", upstreamHeader)
	}
	if upstreamPayload.Question != "hybrid search" || upstreamPayload.TopK != 20 {
		t.Fatalf("unexpected upstream payload: %#v", upstreamPayload)
	}
	if len(upstreamPayload.Modes) != 2 || upstreamPayload.Modes[0] != "hybrid" || upstreamPayload.Modes[1] != "vector" {
		t.Fatalf("unexpected modes: %#v", upstreamPayload.Modes)
	}
}

func TestAdminRetrievalDiagnosticsRejectsBlankQuestionBeforeProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python client should not be called for invalid diagnostics payload")
	})
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	mux := http.NewServeMux()
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminQATraceStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/qa/retrieval-diagnostics", strings.NewReader(`{"question":"   "}`))
	req.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Header().Get(routeOwnerHeader) != "go-control-plane" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

type fakeAdminQATraceStore struct {
	listQuery    adminstore.QATraceListQuery
	listResult   adminstore.QATraceListResult
	listErr      error
	detailKey    string
	detail       adminstore.QATraceDetail
	detailErr    error
	summaryQuery adminstore.QACostSummaryQuery
	summary      adminstore.QACostSummary
	summaryErr   error
}

func (s *fakeAdminQATraceStore) ListQATraces(_ context.Context, query adminstore.QATraceListQuery) (adminstore.QATraceListResult, error) {
	s.listQuery = query
	return s.listResult, s.listErr
}

func (s *fakeAdminQATraceStore) GetQATrace(_ context.Context, traceIDOrPK string) (adminstore.QATraceDetail, error) {
	s.detailKey = traceIDOrPK
	return s.detail, s.detailErr
}

func (s *fakeAdminQATraceStore) GetQACostSummary(_ context.Context, query adminstore.QACostSummaryQuery) (adminstore.QACostSummary, error) {
	s.summaryQuery = query
	return s.summary, s.summaryErr
}

type fakeAdminMonitorStore struct {
	qa               adminstore.QAQualitySnapshot
	qaErr            error
	qaWindowSeconds  int
	logSeverity      adminstore.LogSeverityMetrics
	logSeverityErr   error
	logWindowMinutes int
	jobSLO           adminstore.JobSLOMetrics
	jobSLOErr        error
	jobWindowMinutes int
}

func (s *fakeAdminMonitorStore) GetQAQualityMetrics(_ context.Context, windowSeconds int) (adminstore.QAQualitySnapshot, error) {
	s.qaWindowSeconds = windowSeconds
	return s.qa, s.qaErr
}

func (s *fakeAdminMonitorStore) GetLogSeverityMetrics(_ context.Context, windowMinutes int) (adminstore.LogSeverityMetrics, error) {
	s.logWindowMinutes = windowMinutes
	return s.logSeverity, s.logSeverityErr
}

func (s *fakeAdminMonitorStore) GetJobSLOMetrics(_ context.Context, windowMinutes int) (adminstore.JobSLOMetrics, error) {
	s.jobWindowMinutes = windowMinutes
	return s.jobSLO, s.jobSLOErr
}

func TestAdminConfigMutationNativeRoutesMarkOwnerAndSkipProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native config mutation route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	updatedAt := time.Date(2026, 6, 6, 12, 0, 0, 0, time.UTC)
	store := &fakeAdminConfigStore{
		createResult: adminstore.ConfigItem{ID: 10, Category: "ai_service", Key: "model", Value: "gpt-4o-mini", UpdatedAt: &updatedAt, Version: 1},
		updateResult: adminstore.ConfigItem{ID: 11, Category: "ai_service", Key: "temperature", Value: "0.2", UpdatedAt: &updatedAt, Version: 2},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	createRec := httptest.NewRecorder()
	createReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config", strings.NewReader(`{"category":"ai_service","key":"model","value":"gpt-4o-mini","description":"Model","is_sensitive":false}`))
	createReq.Header.Set("Content-Type", "application/json")
	createReq.Header.Set(traceHeader, "trace-create")
	mux.ServeHTTP(createRec, createReq)
	if createRec.Code != http.StatusCreated {
		t.Fatalf("expected create 201, got %d", createRec.Code)
	}
	if createRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected create route owner: %s", createRec.Header().Get(routeOwnerHeader))
	}
	if store.createReq.Category != "ai_service" || store.createReq.Key != "model" || store.createReq.Value != "gpt-4o-mini" {
		t.Fatalf("unexpected create request: %#v", store.createReq)
	}
	if store.createReq.TraceID == nil || *store.createReq.TraceID != "trace-create" {
		t.Fatalf("expected create trace id, got %#v", store.createReq.TraceID)
	}

	updateRec := httptest.NewRecorder()
	updateReq := httptest.NewRequest(http.MethodPut, "/api/v1/admin/config/ai-service/temperature", strings.NewReader(`{"value":"0.2"}`))
	updateReq.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(updateRec, updateReq)
	if updateRec.Code != http.StatusOK {
		t.Fatalf("expected update 200, got %d", updateRec.Code)
	}
	if updateRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected update route owner: %s", updateRec.Header().Get(routeOwnerHeader))
	}
	if store.updateReq.Category != "ai_service" || store.updateReq.Key != "temperature" || store.updateReq.Value != "0.2" {
		t.Fatalf("unexpected update request: %#v", store.updateReq)
	}

	deleteRec := httptest.NewRecorder()
	deleteReq := httptest.NewRequest(http.MethodDelete, "/api/v1/admin/config/ai-service/temperature", nil)
	mux.ServeHTTP(deleteRec, deleteReq)
	if deleteRec.Code != http.StatusOK {
		t.Fatalf("expected delete 200, got %d", deleteRec.Code)
	}
	if deleteRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected delete route owner: %s", deleteRec.Header().Get(routeOwnerHeader))
	}
	if store.deleteReq.Category != "ai_service" || store.deleteReq.Key != "temperature" {
		t.Fatalf("unexpected delete request: %#v", store.deleteReq)
	}
}

func TestAdminConfigBatchNativeRouteSkipsProxyAndInitIsRetired(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native config batch/init route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminConfigStore{
		batchUpdatedCount: 1,
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	batchRec := httptest.NewRecorder()
	batchReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/batch", strings.NewReader(`{"configs":[{"category":"ai-service","key":"model","value":"gpt-4o-mini"},{"category":"nl2cypher","key":"enabled","value":"true"}]}`))
	batchReq.Header.Set("Content-Type", "application/json")
	mux.ServeHTTP(batchRec, batchReq)
	if batchRec.Code != http.StatusOK {
		t.Fatalf("expected batch 200, got %d", batchRec.Code)
	}
	if batchRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected batch route owner: %s", batchRec.Header().Get(routeOwnerHeader))
	}
	if len(store.batchReq.Items) != 2 || store.batchReq.Items[0].Category != "ai_service" {
		t.Fatalf("unexpected batch request: %#v", store.batchReq)
	}

	initRec := httptest.NewRecorder()
	initReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/init", nil)
	mux.ServeHTTP(initRec, initReq)
	if initRec.Code != http.StatusNotFound {
		t.Fatalf("expected retired init route to return 404, got %d body=%s", initRec.Code, initRec.Body.String())
	}
	if initRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected retired init route owner: %s", initRec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminConfigNeo4jConnectionTestIsNative(t *testing.T) {
	originalProbe := adminNeo4jConnectionProbe
	defer func() { adminNeo4jConnectionProbe = originalProbe }()
	adminNeo4jConnectionProbe = func(ctx context.Context, uri string, user string, password string, database string) error {
		if uri != "bolt://neo4j:7687" || user != "neo4j" || password != "password" || database != "neo4j" {
			t.Fatalf("unexpected probe config uri=%q user=%q password=%q database=%q", uri, user, password, database)
		}
		return nil
	}
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native neo4j config test route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	graphSvc := &stubGraphService{}
	guard := newGoDBPermissionGuardForTest(nil, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{
		Neo4jURI:      "bolt://neo4j:7687",
		Neo4jUser:     "neo4j",
		Neo4jPassword: "password",
		Neo4jDatabase: "neo4j",
	}, logger, graphSvc, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/neo4j", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected neo4j test 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected neo4j test route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != true {
		t.Fatalf("expected success=true, got %#v", data)
	}
	if !strings.Contains(data["message"].(string), "Neo4j 连接成功") {
		t.Fatalf("unexpected message: %#v", data["message"])
	}
}

func TestAdminConfigNeo4jConnectionTestUsesAdminDatabaseName(t *testing.T) {
	originalProbe := adminNeo4jConnectionProbe
	defer func() { adminNeo4jConnectionProbe = originalProbe }()
	adminNeo4jConnectionProbe = func(ctx context.Context, uri string, user string, password string, database string) error {
		if uri != "neo4j+s://example.databases.neo4j.io" || user != "aura-user" || password != "aura-password" || database != "aura-db" {
			t.Fatalf("unexpected probe config uri=%q user=%q password=%q database=%q", uri, user, password, database)
		}
		return nil
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{
		Neo4jURI:      "bolt://env-neo4j:7687",
		Neo4jUser:     "env-user",
		Neo4jPassword: "env-password",
		Neo4jDatabase: "env-db",
	}, logger, nil, nil, newAPIMetrics(10), nil, nil, guard, &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"neo4j": {
				"uri":      "neo4j+s://example.databases.neo4j.io",
				"user":     "aura-user",
				"password": "aura-password",
				"database": "aura-db",
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/neo4j", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected neo4j test 200, got %d", rec.Code)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != true {
		t.Fatalf("expected success=true, got %#v", data)
	}
}

func TestAdminConfigNeo4jConnectionTestSavesPayloadOnlyAfterSuccess(t *testing.T) {
	originalProbe := adminNeo4jConnectionProbe
	defer func() { adminNeo4jConnectionProbe = originalProbe }()
	adminNeo4jConnectionProbe = func(ctx context.Context, uri string, user string, password string, database string) error {
		if uri != "neo4j+s://payload.databases.neo4j.io" || user != "payload-user" || password != "new-password" || database != "payload-db" {
			t.Fatalf("unexpected probe config uri=%q user=%q password=%q database=%q", uri, user, password, database)
		}
		return nil
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	store := &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"neo4j": {
				"uri":      "bolt://saved:7687",
				"user":     "saved-user",
				"password": "saved-password",
				"database": "saved-db",
			},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), nil, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/neo4j", strings.NewReader(`{
		"uri":"neo4j+s://payload.databases.neo4j.io",
		"user":"payload-user",
		"password":"new-password",
		"database":"payload-db"
	}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected neo4j test 200, got %d", rec.Code)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != true || data["saved"] != true {
		t.Fatalf("expected success=true saved=true, got %#v", data)
	}
	if got := store.values["neo4j"]["uri"]; got != "neo4j+s://payload.databases.neo4j.io" {
		t.Fatalf("expected saved uri payload value, got %q", got)
	}
	if got := store.values["neo4j"]["user"]; got != "payload-user" {
		t.Fatalf("expected saved user payload value, got %q", got)
	}
	if got := store.values["neo4j"]["password"]; got != "new-password" {
		t.Fatalf("expected saved password payload value, got %q", got)
	}
	if got := store.values["neo4j"]["database"]; got != "payload-db" {
		t.Fatalf("expected saved database payload value, got %q", got)
	}
}

func TestAdminConfigNeo4jConnectionTestDoesNotSavePayloadAfterFailure(t *testing.T) {
	originalProbe := adminNeo4jConnectionProbe
	defer func() { adminNeo4jConnectionProbe = originalProbe }()
	adminNeo4jConnectionProbe = func(ctx context.Context, uri string, user string, password string, database string) error {
		return errors.New("dial failed")
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	store := &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"neo4j": {
				"uri":      "bolt://saved:7687",
				"user":     "saved-user",
				"password": "saved-password",
				"database": "saved-db",
			},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), nil, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/neo4j", strings.NewReader(`{
		"uri":"neo4j+s://bad.databases.neo4j.io",
		"user":"bad-user",
		"password":"bad-password",
		"database":"bad-db"
	}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected neo4j test 200, got %d", rec.Code)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != false || data["saved"] != false {
		t.Fatalf("expected success=false saved=false, got %#v", data)
	}
	if got := store.values["neo4j"]["uri"]; got != "bolt://saved:7687" {
		t.Fatalf("expected saved uri unchanged, got %q", got)
	}
	if got := store.values["neo4j"]["user"]; got != "saved-user" {
		t.Fatalf("expected saved user unchanged, got %q", got)
	}
	if got := store.values["neo4j"]["password"]; got != "saved-password" {
		t.Fatalf("expected saved password unchanged, got %q", got)
	}
	if got := store.values["neo4j"]["database"]; got != "saved-db" {
		t.Fatalf("expected saved database unchanged, got %q", got)
	}
}

func TestAdminConfigNeo4jConnectionTestExplainsDNSFailure(t *testing.T) {
	originalProbe := adminNeo4jConnectionProbe
	defer func() { adminNeo4jConnectionProbe = originalProbe }()
	adminNeo4jConnectionProbe = func(ctx context.Context, uri string, user string, password string, database string) error {
		return errors.New("lookup missing.databases.neo4j.io on 127.0.0.53:53: no such host")
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	store := &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"neo4j": {
				"uri":      "bolt://saved:7687",
				"user":     "saved-user",
				"password": "saved-password",
				"database": "saved-db",
			},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), nil, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/neo4j", strings.NewReader(`{
		"uri":"neo4j+s://missing.databases.neo4j.io",
		"user":"aura-user",
		"password":"aura-password",
		"database":"aura-db"
	}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected neo4j test 200, got %d", rec.Code)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	message, _ := data["message"].(string)
	if data["success"] != false || data["saved"] != false {
		t.Fatalf("expected success=false saved=false, got %#v", data)
	}
	if !strings.Contains(message, "地址无法解析") || !strings.Contains(message, "还没有验证到用户名或密码") {
		t.Fatalf("expected dns-specific message, got %q", message)
	}
	if got := store.values["neo4j"]["uri"]; got != "bolt://saved:7687" {
		t.Fatalf("expected saved uri unchanged, got %q", got)
	}
}

func TestAdminConfigNeo4jConnectionTestFailsWhenPasswordMissingInResolvedConfig(t *testing.T) {
	originalProbe := adminNeo4jConnectionProbe
	defer func() { adminNeo4jConnectionProbe = originalProbe }()
	adminNeo4jConnectionProbe = func(ctx context.Context, uri string, user string, password string, database string) error {
		t.Fatalf("probe should not run when password is missing")
		return nil
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{
		Neo4jURI:      "bolt://neo4j:7687",
		Neo4jUser:     "neo4j",
		Neo4jPassword: "",
		Neo4jDatabase: "neo4j",
	}, logger, nil, nil, newAPIMetrics(10), nil, nil, guard, &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"neo4j": {
				"uri":      "bolt://db:7687",
				"user":     "neo4j",
				"database": "neo4j",
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/neo4j", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected neo4j test 200, got %d", rec.Code)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != false {
		t.Fatalf("expected success=false, got %#v", data)
	}
	if !strings.Contains(data["message"].(string), "password is empty") {
		t.Fatalf("unexpected message: %#v", data["message"])
	}
}

func TestAdminConfigAIServiceConnectionTestIsNative(t *testing.T) {
	t.Parallel()
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native ai service config test route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{
		AIProvider: "openai",
		AIAPIKey:   "sk-test",
	}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"ai_service": {
				"provider": "openai",
				"enabled":  "true",
				"api_key":  "vg-test-native",
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/ai_service", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected ai_service test 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected ai_service test route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != true {
		t.Fatalf("expected success=true, got %#v", data)
	}
	if data["message"] != "OpenAI API 配置完整；请使用“测试当前模型”验证实际鉴权与连通性" {
		t.Fatalf("unexpected message: %#v", data["message"])
	}
}

func TestAdminConfigEmbeddingConnectionTestIsNative(t *testing.T) {
	t.Parallel()
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native embedding config test route")
	})
	embeddingServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/embeddings" {
			t.Fatalf("unexpected embedding endpoint path: %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer sk-test-embedding" {
			t.Fatalf("unexpected auth header: %s", r.Header.Get("Authorization"))
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"object":"list","data":[{"object":"embedding","index":0,"embedding":[0.1,0.2]}],"model":"text-embedding-3-small"}`))
	}))
	t.Cleanup(embeddingServer.Close)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"ai_service": {
				"provider": "openai",
				"enabled":  "true",
				"api_key":  "sk-test-ai",
			},
			"embedding": {
				"provider": "openai",
				"enabled":  "true",
				"api_key":  "sk-test-embedding",
				"base_url": embeddingServer.URL,
				"model":    "text-embedding-3-small",
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/embedding", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected embedding test 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected embedding test route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != true || data["model"] != "text-embedding-3-small" {
		t.Fatalf("unexpected embedding test data: %#v", data)
	}
}

func TestAdminConfigVectorStoreConnectionTestIsNative(t *testing.T) {
	t.Parallel()
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native vector store config test route")
	})
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("start tcp listener: %v", err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	done := make(chan struct{})
	go func() {
		defer close(done)
		conn, acceptErr := listener.Accept()
		if acceptErr == nil {
			_ = conn.Close()
		}
	}()

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(nil, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"vector_store": {
				"provider":   "milvus",
				"enabled":    "false",
				"uri":        "http://" + listener.Addr().String(),
				"db_name":    "default",
				"collection": "graphinsight_chunks",
			},
		},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/vector_store", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected vector store test 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected vector store test route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	<-done

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["success"] != true || data["provider"] != "milvus" {
		t.Fatalf("unexpected vector store test data: %#v", data)
	}
}

func TestReadProviderErrorMessageExtractsOpenAICompatibleError(t *testing.T) {
	t.Parallel()

	message := readProviderErrorMessage(strings.NewReader(`{"error":{"message":"model does not support embeddings"}}`))
	if message != "model does not support embeddings" {
		t.Fatalf("unexpected provider error message: %q", message)
	}
}

func TestAdminConfigReadNativeRoutesMarkOwnerAndSkipProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native config read route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{
		Neo4jURI:                  "bolt://neo4j:7687",
		Neo4jUser:                 "neo4j",
		Neo4jPassword:             "secret-password",
		Neo4jDatabase:             "neo4j",
		Neo4jConfigSource:         "env",
		Neo4jConfigResolvedSource: "env",
		AIProvider:                "openai",
		AIModel:                   "custom-model",
		AIAPIKey:                  "sk-secret",
		RBACEnforceBusinessAPI:    false,
	}
	guard := newBusinessPermissionGuard(cfg, logger)
	updatedAt := time.Date(2026, 6, 5, 12, 0, 0, 0, time.UTC)
	store := &fakeAdminConfigStore{
		listResult: adminstore.ConfigListResult{
			Items: []adminstore.ConfigItem{
				{ID: 1, Category: "neo4j", Key: "uri", Value: "bolt://db:7687", IsSensitive: false, UpdatedAt: &updatedAt, Version: 1},
			},
			Total: 1,
		},
		categories: map[string]map[string]adminstore.ConfigItem{
			"neo4j": {
				"uri":      {ID: 1, Category: "neo4j", Key: "uri", Value: "bolt://db:7687", UpdatedAt: &updatedAt, Version: 1},
				"password": {ID: 2, Category: "neo4j", Key: "password", Value: "", IsSensitive: true, UpdatedAt: &updatedAt, Version: 1},
			},
		},
		values: map[string]map[string]string{
			"neo4j": {"uri": "bolt://db:7687", "password": "configured-secret", "database": "neo4j"},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/neo4j/all?include_sensitive=true", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["uri"] != "bolt://db:7687" {
		t.Fatalf("unexpected neo4j uri: %#v", data)
	}
	if data["password"] != "" {
		t.Fatalf("expected password to be redacted, got %#v", data["password"])
	}
	if data["password_configured"] != true {
		t.Fatalf("expected password_configured=true, got %#v", data["password_configured"])
	}

	listRec := httptest.NewRecorder()
	listReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config?page=1&page_size=10&category=neo4j&key=ur", nil)
	mux.ServeHTTP(listRec, listReq)
	if listRec.Code != http.StatusOK {
		t.Fatalf("expected config list 200, got %d", listRec.Code)
	}
	if store.listQuery.Category != "neo4j" || store.listQuery.Key != "ur" {
		t.Fatalf("unexpected config list query: %#v", store.listQuery)
	}

	categoryRec := httptest.NewRecorder()
	categoryReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/neo4j", nil)
	mux.ServeHTTP(categoryRec, categoryReq)
	if categoryRec.Code != http.StatusOK {
		t.Fatalf("expected config category 200, got %d", categoryRec.Code)
	}

	itemRec := httptest.NewRecorder()
	itemReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/neo4j/uri", nil)
	mux.ServeHTTP(itemRec, itemReq)
	if itemRec.Code != http.StatusOK {
		t.Fatalf("expected config item 200, got %d", itemRec.Code)
	}

	modelsRec := httptest.NewRecorder()
	modelsReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/ai-service/models", nil)
	mux.ServeHTTP(modelsRec, modelsReq)
	if modelsRec.Code != http.StatusOK {
		t.Fatalf("expected models 200, got %d", modelsRec.Code)
	}
	if modelsRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected models route owner: %s", modelsRec.Header().Get(routeOwnerHeader))
	}
	var modelsResp APIResponse
	if err := json.NewDecoder(modelsRec.Body).Decode(&modelsResp); err != nil {
		t.Fatalf("decode models response: %v", err)
	}
	modelsData, ok := modelsResp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected models object data, got %#v", modelsResp.Data)
	}
	models, ok := modelsData["models"].([]interface{})
	if !ok || len(models) == 0 {
		t.Fatalf("expected non-empty models array, got %#v", modelsData["models"])
	}
	if models[0] != "custom-model" {
		t.Fatalf("expected current model first, got %#v", models)
	}
	catalog, ok := modelsData["catalog"].([]interface{})
	if !ok || len(catalog) == 0 {
		t.Fatalf("expected non-empty catalog, got %#v", modelsData["catalog"])
	}
	firstCatalog, ok := catalog[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected catalog object, got %#v", catalog[0])
	}
	if firstCatalog["provider"] != "openai" || firstCatalog["default_profile"] != "balanced" {
		t.Fatalf("unexpected catalog entry: %#v", firstCatalog)
	}
	if firstCatalog["label"] != "Custom Model" {
		t.Fatalf("unexpected catalog label: %#v", firstCatalog["label"])
	}
	if firstCatalog["supports_reasoning"] != false {
		t.Fatalf("expected custom-model to not infer reasoning support, got %#v", firstCatalog["supports_reasoning"])
	}
	scenarioProfiles, ok := modelsData["scenario_profiles"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected scenario_profiles object, got %#v", modelsData["scenario_profiles"])
	}
	if scenarioProfiles["docqa"] != "balanced" || scenarioProfiles["deep_research"] != "deep" {
		t.Fatalf("unexpected scenario profiles: %#v", scenarioProfiles)
	}
	if scenarioProfiles["model_probe"] != "fast" {
		t.Fatalf("unexpected model_probe scenario profile: %#v", scenarioProfiles)
	}

	latestRec := httptest.NewRecorder()
	latestReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/test/model/latest", nil)
	mux.ServeHTTP(latestRec, latestReq)
	if latestRec.Code != http.StatusOK {
		t.Fatalf("expected latest model test 200, got %d", latestRec.Code)
	}
	if latestRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected latest route owner: %s", latestRec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminConfigReadNativeRouteUsesReadPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native config read route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{RBACEnforceBusinessAPI: true}
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{
		values: map[string]map[string]string{"ai_service": {"model": "gpt-4o-mini", "provider": "openai", "docqa_reasoning_profile": "balanced", "deep_research_reasoning_profile": "deep"}},
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/ai-service/models", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if gotPermission != "config:read" {
		t.Fatalf("expected config:read permission, got %s", gotPermission)
	}
}

func TestBuildAdminModelCatalogResponseInfersReasoningCapabilities(t *testing.T) {
	t.Parallel()

	resp := buildAdminModelCatalogResponse(context.Background(), config.Config{}, map[string]string{
		"provider":                        "openai_compatible",
		"docqa_reasoning_profile":         "balanced",
		"deep_research_reasoning_profile": "deep",
	}, map[string][]string{"model": {"deepseek-reasoner"}})

	if len(resp.Catalog) == 0 {
		t.Fatal("expected non-empty catalog")
	}
	first := resp.Catalog[0]
	if first.Model != "deepseek-reasoner" {
		t.Fatalf("unexpected first model: %#v", first)
	}
	if first.Label != "Deepseek Reasoner" {
		t.Fatalf("unexpected label: %#v", first.Label)
	}
	if !first.SupportsReasoning {
		t.Fatalf("expected reasoning support: %#v", first)
	}
	if len(first.SupportedProfiles) != 3 || first.SupportedProfiles[2] != "deep" {
		t.Fatalf("unexpected supported profiles: %#v", first.SupportedProfiles)
	}
}

func TestFilterModelsForPurposeKeepsOnlyEmbeddingCandidates(t *testing.T) {
	t.Parallel()

	models := filterModelsForPurpose([]string{
		"DeepSeek-V4-Flash",
		"text-embedding-3-small",
		"BAAI/bge-m3",
		"qwen-plus",
	}, "embedding", "text-embedding-3-small")
	if len(models) != 2 {
		t.Fatalf("expected two embedding models, got %#v", models)
	}
	if models[0] != "text-embedding-3-small" || models[1] != "BAAI/bge-m3" {
		t.Fatalf("unexpected embedding models: %#v", models)
	}
}

func TestAdminConfigModelConnectionTestStoresLatestSnapshot(t *testing.T) {
	t.Parallel()
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native model connection test route")
	})
	modelServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			t.Fatalf("unexpected model endpoint path: %s", r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer sk-test-model" {
			t.Fatalf("unexpected auth header: %s", r.Header.Get("Authorization"))
		}
		var payload map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatalf("decode model probe payload: %v", err)
		}
		if _, exists := payload["temperature"]; exists {
			t.Fatalf("model connectivity probe must not force temperature: %#v", payload)
		}
		if _, exists := payload["max_tokens"]; exists {
			t.Fatalf("model connectivity probe must not force provider-specific token fields: %#v", payload)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"id":"chatcmpl-1","object":"chat.completion","choices":[{"index":0,"message":{"role":"assistant","content":"ok"}}]}`))
	}))
	t.Cleanup(modelServer.Close)

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{RBACEnforceBusinessAPI: true}
	guard := newGoDBPermissionGuardForTest(nil, 1)
	snapshots := &adminModelConnectionSnapshotStore{}
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{
		values: map[string]map[string]string{
			"ai_service": {
				"provider": "openai",
				"enabled":  "true",
				"api_key":  "sk-test-model",
				"base_url": modelServer.URL,
				"model":    "gpt-4o-mini",
			},
		},
	}, snapshots)

	initialRec := httptest.NewRecorder()
	initialReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/test/model/latest", nil)
	initialReq.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(initialRec, initialReq)
	if initialRec.Code != http.StatusOK {
		t.Fatalf("expected initial latest 200, got %d", initialRec.Code)
	}
	var initialResp APIResponse
	if err := json.NewDecoder(initialRec.Body).Decode(&initialResp); err != nil {
		t.Fatalf("decode initial latest response: %v", err)
	}
	if initialResp.Data != nil {
		t.Fatalf("expected empty latest snapshot, got %#v", initialResp.Data)
	}

	testRec := httptest.NewRecorder()
	testReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/test/model", nil)
	testReq.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(testRec, testReq)
	if testRec.Code != http.StatusOK {
		t.Fatalf("expected model test 200, got %d", testRec.Code)
	}
	if testRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected model test route owner: %s", testRec.Header().Get(routeOwnerHeader))
	}

	latestRec := httptest.NewRecorder()
	latestReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/test/model/latest", nil)
	latestReq.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(latestRec, latestReq)
	if latestRec.Code != http.StatusOK {
		t.Fatalf("expected latest 200, got %d", latestRec.Code)
	}
	if latestRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected latest route owner: %s", latestRec.Header().Get(routeOwnerHeader))
	}
	var latestResp APIResponse
	if err := json.NewDecoder(latestRec.Body).Decode(&latestResp); err != nil {
		t.Fatalf("decode latest response: %v", err)
	}
	data, ok := latestResp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected latest data object, got %#v", latestResp.Data)
	}
	if data["reasoning_profile"] != "fast" {
		t.Fatalf("expected latest snapshot reasoning_profile=fast, got %#v", data["reasoning_profile"])
	}
	if data["success"] != true || data["model"] != "gpt-4o-mini" {
		t.Fatalf("unexpected latest model snapshot: %#v", data)
	}
}

func TestAdminConfigUnknownSubpathReturnsGoOwned404(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unsupported config subpath")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/config/unsupported/path", strings.NewReader(`{}`))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminConfigTestReadPathReturnsGoOwned404(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unsupported config test read path")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/config/test/model", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestLegacyOpenAIConfigAliasesAreRemoved(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for removed openai config alias")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminConfigStore{})

	for _, tc := range []struct {
		method string
		path   string
		body   string
	}{
		{method: http.MethodGet, path: "/api/v1/admin/config/openai/models"},
		{method: http.MethodGet, path: "/api/v1/admin/config/openai/all"},
		{method: http.MethodPost, path: "/api/v1/admin/config/test/openai", body: `{}`},
	} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(tc.method, tc.path, strings.NewReader(tc.body))
		mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("expected removed alias %s %s to return 404, got %d", tc.method, tc.path, rec.Code)
		}
		if rec.Header().Get(routeOwnerHeader) != "go-native" {
			t.Fatalf("unexpected route owner for %s: %s", tc.path, rec.Header().Get(routeOwnerHeader))
		}
	}
}

type fakeAdminConfigStore struct {
	listQuery         adminstore.ConfigListQuery
	listResult        adminstore.ConfigListResult
	listErr           error
	categories        map[string]map[string]adminstore.ConfigItem
	values            map[string]map[string]string
	itemErr           error
	createReq         adminstore.ConfigMutationRequest
	createResult      adminstore.ConfigItem
	createErr         error
	updateReq         adminstore.ConfigMutationRequest
	updateResult      adminstore.ConfigItem
	updateErr         error
	deleteReq         adminstore.ConfigMutationRequest
	deleteErr         error
	batchReq          adminstore.ConfigBatchUpdateRequest
	batchUpdatedCount int
	batchErr          error
}

func (s *fakeAdminConfigStore) ListConfigs(_ context.Context, query adminstore.ConfigListQuery) (adminstore.ConfigListResult, error) {
	s.listQuery = query
	return s.listResult, s.listErr
}

func (s *fakeAdminConfigStore) ListConfigCategory(_ context.Context, category string) (map[string]adminstore.ConfigItem, error) {
	if s.categories == nil {
		return map[string]adminstore.ConfigItem{}, nil
	}
	if items, ok := s.categories[category]; ok {
		return items, nil
	}
	return map[string]adminstore.ConfigItem{}, nil
}

func (s *fakeAdminConfigStore) GetConfigItem(_ context.Context, category string, key string) (adminstore.ConfigItem, error) {
	if s.itemErr != nil {
		return adminstore.ConfigItem{}, s.itemErr
	}
	if s.categories != nil {
		if items, ok := s.categories[category]; ok {
			if item, ok := items[key]; ok {
				return item, nil
			}
		}
	}
	return adminstore.ConfigItem{}, adminstore.ErrConfigNotFound
}

func (s *fakeAdminConfigStore) GetConfigValueMap(_ context.Context, category string) (map[string]string, error) {
	if s.values == nil {
		return map[string]string{}, nil
	}
	if values, ok := s.values[category]; ok {
		return values, nil
	}
	return map[string]string{}, nil
}

func (s *fakeAdminConfigStore) CreateConfig(_ context.Context, req adminstore.ConfigMutationRequest) (adminstore.ConfigItem, error) {
	s.createReq = req
	return s.createResult, s.createErr
}

func (s *fakeAdminConfigStore) UpdateConfig(_ context.Context, req adminstore.ConfigMutationRequest) (adminstore.ConfigItem, error) {
	s.updateReq = req
	if s.updateErr == nil {
		if s.values == nil {
			s.values = map[string]map[string]string{}
		}
		if _, ok := s.values[req.Category]; !ok {
			s.values[req.Category] = map[string]string{}
		}
		s.values[req.Category][req.Key] = req.Value
	}
	return s.updateResult, s.updateErr
}

func (s *fakeAdminConfigStore) DeleteConfig(_ context.Context, req adminstore.ConfigMutationRequest) error {
	s.deleteReq = req
	return s.deleteErr
}

func (s *fakeAdminConfigStore) BatchUpdateConfigs(_ context.Context, req adminstore.ConfigBatchUpdateRequest) (int, error) {
	s.batchReq = req
	return s.batchUpdatedCount, s.batchErr
}

func TestAdminLogsListNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native logs list route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminLogStore{
		listResult: adminstore.LogListResult{
			Items: []adminstore.LogItem{
				{ID: 10, Action: "login", Status: "failed", Severity: "error", CreatedAt: time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC)},
			},
			Total: 21,
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/logs?page=2&page_size=10&status=failed&action=login&resource=auth&trace_id=trace-1&ip_address=127.0.0.1", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["page"] != float64(2) || data["page_size"] != float64(10) {
		t.Fatalf("unexpected pagination: %#v", data)
	}
	if data["total"] != float64(21) || data["total_pages"] != float64(3) {
		t.Fatalf("unexpected totals: %#v", data)
	}
	if _, ok := data["items"].([]interface{}); !ok {
		t.Fatalf("expected items array, got %#v", data["items"])
	}
	if store.listQuery.Page != 2 || store.listQuery.PageSize != 10 || store.listQuery.Status != "failed" || store.listQuery.Action != "login" {
		t.Fatalf("unexpected list query: %#v", store.listQuery)
	}
	if store.listQuery.Resource != "auth" || store.listQuery.TraceID != "trace-1" || store.listQuery.IPAddress != "127.0.0.1" {
		t.Fatalf("unexpected list filters: %#v", store.listQuery)
	}
}

func TestAdminLogsExportCSVNativeRouteMarksOwnerAndWritesAudit(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native logs export route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminLogStore{
		listResult: adminstore.LogListResult{
			Items: []adminstore.LogItem{
				{
					ID:        10,
					Action:    "login",
					Status:    "failed",
					Severity:  "error",
					TraceID:   optionalStringPtr("trace-1"),
					Username:  optionalStringPtr("auditor"),
					Details:   optionalStringPtr(`{"reason":"bad_password"}`),
					CreatedAt: time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC),
				},
			},
			Total: 1,
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/logs?export=true&status=failed&action=login&trace_id=trace-1", nil)
	req.Header.Set(traceHeader, "trace-export-log")
	req.Header.Set("x-auth-user-id", "99")
	req.Header.Set("x-scope-tenant-id", "tenant-a")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if got := rec.Header().Get("Content-Type"); got != "text/csv; charset=utf-8" {
		t.Fatalf("unexpected content-type: %s", got)
	}
	if store.listQuery.Page != 1 || store.listQuery.PageSize != maxAdminLogsCSVExportRows {
		t.Fatalf("unexpected export pagination: %#v", store.listQuery)
	}
	if store.exportAuditReq.Rows != 1 {
		t.Fatalf("unexpected export rows: %#v", store.exportAuditReq)
	}
	if store.exportAuditReq.TraceID == nil || *store.exportAuditReq.TraceID != "trace-export-log" {
		t.Fatalf("unexpected export trace: %#v", store.exportAuditReq.TraceID)
	}
	body := rec.Body.String()
	if !strings.Contains(body, "login") || !strings.Contains(body, "auditor") {
		t.Fatalf("unexpected csv body: %q", body)
	}
}

func TestAdminLogsStatsAndRecentNativeRoutesSkipProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native logs read route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminLogStore{
		stats: adminstore.LogStats{
			TotalLogs:     3,
			SuccessCount:  2,
			FailedCount:   1,
			SuccessRate:   0.6667,
			SeverityStats: map[string]int{"info": 2, "warn": 0, "error": 1},
			ActionStats:   map[string]int{"login": 3},
			UserStats:     map[string]int{"admin": 3},
			HourlyStats:   map[string]int{"09": 3},
		},
		recent: []adminstore.LogItem{
			{ID: 11, Action: "login", Status: "success", Severity: "info", CreatedAt: time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC)},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	statsRec := httptest.NewRecorder()
	statsReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/logs/stats/summary", nil)
	mux.ServeHTTP(statsRec, statsReq)
	if statsRec.Code != http.StatusOK {
		t.Fatalf("expected stats 200, got %d", statsRec.Code)
	}
	if statsRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected stats route owner: %s", statsRec.Header().Get(routeOwnerHeader))
	}
	var statsResp APIResponse
	if err := json.NewDecoder(statsRec.Body).Decode(&statsResp); err != nil {
		t.Fatalf("decode stats response: %v", err)
	}
	statsData, ok := statsResp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected stats object data, got %#v", statsResp.Data)
	}
	if statsData["total_logs"] != float64(3) || statsData["failed_count"] != float64(1) {
		t.Fatalf("unexpected stats data: %#v", statsData)
	}
	for _, key := range []string{"total_logs", "success_count", "failed_count", "success_rate", "severity_stats", "action_stats", "user_stats", "hourly_stats"} {
		if _, exists := statsData[key]; !exists {
			t.Fatalf("missing stats key %s in %#v", key, statsData)
		}
	}

	recentRec := httptest.NewRecorder()
	recentReq := httptest.NewRequest(http.MethodGet, "/api/v1/admin/logs/recent/list?limit=5", nil)
	mux.ServeHTTP(recentRec, recentReq)
	if recentRec.Code != http.StatusOK {
		t.Fatalf("expected recent 200, got %d", recentRec.Code)
	}
	if recentRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected recent route owner: %s", recentRec.Header().Get(routeOwnerHeader))
	}
	var recentResp APIResponse
	if err := json.NewDecoder(recentRec.Body).Decode(&recentResp); err != nil {
		t.Fatalf("decode recent response: %v", err)
	}
	if _, ok := recentResp.Data.([]interface{}); !ok {
		t.Fatalf("expected recent array data, got %#v", recentResp.Data)
	}
	if store.recentLimit != 5 {
		t.Fatalf("unexpected recent limit: %d", store.recentLimit)
	}
}

func TestAdminLogsDetailNativeRouteUsesReadPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native logs detail route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	store := &fakeAdminLogStore{
		detail: adminstore.LogDetail{
			ID:        123,
			Action:    "login",
			Status:    "success",
			Severity:  "info",
			CreatedAt: time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC),
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/logs/123", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if gotPermission != "logs:read" {
		t.Fatalf("expected logs:read permission, got %s", gotPermission)
	}
	if store.detailID != 123 {
		t.Fatalf("unexpected detail id: %d", store.detailID)
	}
}

func TestAdminLogsCleanNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native logs clean route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	store := &fakeAdminLogStore{
		cleanResult: adminstore.LogCleanResult{
			DeletedCount: 4,
			Days:         30,
			DryRun:       true,
			CutoffAt:     "2026-01-01T00:00:00Z",
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodDelete, "/api/v1/admin/logs/clean?days=30&dry_run=true", nil)
	req.Header.Set(traceHeader, "trace-clean")
	req.Header.Set("x-auth-user-id", "12")
	req.Header.Set("x-scope-tenant-id", "tenant-a")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.cleanReq.Days != 30 || !store.cleanReq.DryRun {
		t.Fatalf("unexpected clean request: %#v", store.cleanReq)
	}
	if store.cleanReq.OperatorID == nil || *store.cleanReq.OperatorID != 12 {
		t.Fatalf("unexpected operator id: %#v", store.cleanReq.OperatorID)
	}
	if store.cleanReq.TenantID == nil || *store.cleanReq.TenantID != "tenant-a" {
		t.Fatalf("unexpected tenant id: %#v", store.cleanReq.TenantID)
	}
	if store.cleanReq.TraceID == nil || *store.cleanReq.TraceID != "trace-clean" {
		t.Fatalf("unexpected trace id: %#v", store.cleanReq.TraceID)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	if data["deleted_count"] != float64(4) || data["days"] != float64(30) || data["dry_run"] != true {
		t.Fatalf("unexpected clean response: %#v", data)
	}
}

func TestAdminLogsCleanNativeRouteUsesCleanPermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native logs clean route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	store := &fakeAdminLogStore{
		cleanResult: adminstore.LogCleanResult{
			DeletedCount: 0,
			Days:         90,
			DryRun:       true,
			CutoffAt:     "2026-01-01T00:00:00Z",
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodDelete, "/api/v1/admin/logs/clean?dry_run=true", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if gotPermission != "logs:clean" {
		t.Fatalf("expected logs:clean permission, got %s", gotPermission)
	}
}

func TestAdminLogsUnsupportedDeletePathReturnsGoOwned405(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unsupported logs delete path")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminLogStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodDelete, "/api/v1/admin/logs/123", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("expected 405, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

type fakeAdminLogStore struct {
	listQuery        adminstore.LogListQuery
	listResult       adminstore.LogListResult
	listErr          error
	exportAuditReq   adminstore.LogExportAuditRequest
	exportAuditErr   error
	detailID         int
	detail           adminstore.LogDetail
	detailErr        error
	statsStart       *time.Time
	statsEnd         *time.Time
	stats            adminstore.LogStats
	statsErr         error
	recentLimit      int
	recentAction     string
	recent           []adminstore.LogItem
	recentErr        error
	cleanReq         adminstore.LogCleanRequest
	cleanResult      adminstore.LogCleanResult
	cleanErr         error
	businessAuditReq adminstore.BusinessAuditRequest
	businessAuditErr error
}

func (s *fakeAdminLogStore) ListLogs(_ context.Context, query adminstore.LogListQuery) (adminstore.LogListResult, error) {
	s.listQuery = query
	return s.listResult, s.listErr
}

func (s *fakeAdminLogStore) RecordLogExportAudit(_ context.Context, req adminstore.LogExportAuditRequest) error {
	s.exportAuditReq = req
	return s.exportAuditErr
}

func (s *fakeAdminLogStore) GetLogByID(_ context.Context, logID int) (adminstore.LogDetail, error) {
	s.detailID = logID
	return s.detail, s.detailErr
}

func (s *fakeAdminLogStore) GetLogStats(_ context.Context, startDate *time.Time, endDate *time.Time) (adminstore.LogStats, error) {
	s.statsStart = startDate
	s.statsEnd = endDate
	return s.stats, s.statsErr
}

func (s *fakeAdminLogStore) ListRecentLogs(_ context.Context, limit int, action string) ([]adminstore.LogItem, error) {
	s.recentLimit = limit
	s.recentAction = action
	return s.recent, s.recentErr
}

func (s *fakeAdminLogStore) CleanOldLogs(_ context.Context, req adminstore.LogCleanRequest) (adminstore.LogCleanResult, error) {
	s.cleanReq = req
	return s.cleanResult, s.cleanErr
}

func (s *fakeAdminLogStore) RecordBusinessAudit(_ context.Context, req adminstore.BusinessAuditRequest) error {
	s.businessAuditReq = req
	return s.businessAuditErr
}

func optionalStringPtr(value string) *string {
	return &value
}

func TestAdminRbacRolesNativeRouteUsesManagePermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native rbac roles route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	store := &fakeRbacCatalogStore{
		roles: []adminstore.RbacRole{
			{ID: 1, Name: "super_admin", IsSystem: true, CreatedAt: createdAt},
			{ID: 2, Name: "viewer", IsSystem: true, CreatedAt: createdAt},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/rbac/roles", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if gotPermission != "user:manage" {
		t.Fatalf("expected user:manage permission, got %s", gotPermission)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	items, ok := resp.Data.([]interface{})
	if !ok || len(items) != 2 {
		t.Fatalf("expected two role items, got %#v", resp.Data)
	}
	first, ok := items[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected role object, got %#v", items[0])
	}
	if first["name"] != "super_admin" || first["is_system"] != true {
		t.Fatalf("unexpected first role: %#v", first)
	}
}

func TestAdminRbacPermissionsNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native rbac permissions route")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	store := &fakeRbacCatalogStore{
		permissions: []adminstore.RbacPermission{
			{ID: 13, Code: "user:manage", ResourceType: "user", Action: "manage", CreatedAt: createdAt},
			{ID: 14, Code: "job:read", ResourceType: "job", Action: "read", CreatedAt: createdAt},
		},
	}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/rbac/permissions", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	items, ok := resp.Data.([]interface{})
	if !ok || len(items) == 0 {
		t.Fatalf("expected permission items, got %#v", resp.Data)
	}
	seenUserManage := false
	for _, item := range items {
		permission, ok := item.(map[string]interface{})
		if !ok {
			t.Fatalf("expected permission object, got %#v", item)
		}
		if permission["code"] == "user:manage" {
			seenUserManage = true
			if permission["resource_type"] != "user" || permission["action"] != "manage" {
				t.Fatalf("unexpected user:manage permission shape: %#v", permission)
			}
		}
	}
	if !seenUserManage {
		t.Fatalf("expected user:manage permission in %#v", items)
	}
}

type fakeRbacCatalogStore struct {
	roles       []adminstore.RbacRole
	rolesErr    error
	permissions []adminstore.RbacPermission
	permsErr    error
}

func (s *fakeRbacCatalogStore) ListRbacRoles(_ context.Context) ([]adminstore.RbacRole, error) {
	return s.roles, s.rolesErr
}

func (s *fakeRbacCatalogStore) ListRbacPermissions(_ context.Context) ([]adminstore.RbacPermission, error) {
	return s.permissions, s.permsErr
}

func TestAdminRbacBindingsNativeReadRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native rbac bindings read route")
	})
	username := "admin"
	email := "admin@example.com"
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	store := &fakeRbacBindingStore{
		bindings: []adminstore.RbacBinding{
			{
				ID:        7,
				UserID:    3,
				Username:  &username,
				Email:     &email,
				RoleID:    1,
				RoleName:  "super_admin",
				ScopeType: "global",
				CreatedAt: createdAt,
			},
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/rbac/bindings?user_id=3", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.userID == nil || *store.userID != 3 {
		t.Fatalf("expected user_id filter 3, got %#v", store.userID)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	items, ok := resp.Data.([]interface{})
	if !ok || len(items) != 1 {
		t.Fatalf("expected one binding item, got %#v", resp.Data)
	}
	first, ok := items[0].(map[string]interface{})
	if !ok {
		t.Fatalf("expected binding object, got %#v", items[0])
	}
	if first["role_name"] != "super_admin" || first["scope_type"] != "global" || first["user_id"] != float64(3) {
		t.Fatalf("unexpected binding item: %#v", first)
	}
}

func TestAdminRbacBindingsCreateNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native rbac binding create route")
	})
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	username := "admin"
	email := "admin@example.com"
	createdBy := 12
	tenantID := "tenant-a"
	store := &fakeRbacBindingStore{
		createResult: adminstore.RbacBinding{
			ID:        9,
			UserID:    3,
			Username:  &username,
			Email:     &email,
			RoleID:    2,
			RoleName:  "viewer",
			ScopeType: "tenant",
			TenantID:  &tenantID,
			CreatedBy: &createdBy,
			CreatedAt: createdAt,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/rbac/bindings", strings.NewReader(`{"user_id":3,"role_name":"viewer","scope_type":"tenant","tenant_id":"tenant-a"}`))
	req.Header.Set(traceHeader, "trace-rbac-create")
	req.Header.Set("x-auth-user-id", "12")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.createReq.UserID != 3 || store.createReq.RoleName != "viewer" || store.createReq.ScopeType != "tenant" {
		t.Fatalf("unexpected create request: %#v", store.createReq)
	}
	if store.createReq.TenantID == nil || *store.createReq.TenantID != "tenant-a" {
		t.Fatalf("unexpected tenant id: %#v", store.createReq.TenantID)
	}
	if store.createReq.OperatorID == nil || *store.createReq.OperatorID != 12 {
		t.Fatalf("unexpected operator id: %#v", store.createReq.OperatorID)
	}
	if store.createReq.TraceID == nil || *store.createReq.TraceID != "trace-rbac-create" {
		t.Fatalf("unexpected trace id: %#v", store.createReq.TraceID)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected binding object, got %#v", resp.Data)
	}
	if data["id"] != float64(9) || data["role_name"] != "viewer" || data["scope_type"] != "tenant" {
		t.Fatalf("unexpected binding response: %#v", data)
	}
}

func TestAdminRbacBindingsDeleteNativeRouteUsesManagePermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native rbac binding delete route")
	})
	store := &fakeRbacBindingStore{}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodDelete, "/api/v1/admin/rbac/bindings/9", nil)
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	req.Header.Set(traceHeader, "trace-rbac-delete")
	req.Header.Set("x-auth-user-id", "12")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if gotPermission != "user:manage" {
		t.Fatalf("expected user:manage permission, got %s", gotPermission)
	}
	if store.deleteReq.BindingID != 9 {
		t.Fatalf("unexpected delete request: %#v", store.deleteReq)
	}
	if store.deleteReq.OperatorID == nil || *store.deleteReq.OperatorID != 1 {
		t.Fatalf("unexpected operator id: %#v", store.deleteReq.OperatorID)
	}
}

type fakeRbacBindingStore struct {
	bindings     []adminstore.RbacBinding
	userID       *int
	err          error
	createReq    adminstore.RbacBindingMutationRequest
	createResult adminstore.RbacBinding
	createErr    error
	deleteReq    adminstore.RbacBindingDeleteRequest
	deleteErr    error
}

func (s *fakeRbacBindingStore) ListRbacBindings(_ context.Context, userID *int) ([]adminstore.RbacBinding, error) {
	s.userID = userID
	return s.bindings, s.err
}

func (s *fakeRbacBindingStore) CreateRbacBinding(_ context.Context, req adminstore.RbacBindingMutationRequest) (adminstore.RbacBinding, error) {
	s.createReq = req
	return s.createResult, s.createErr
}

func (s *fakeRbacBindingStore) DeleteRbacBinding(_ context.Context, req adminstore.RbacBindingDeleteRequest) error {
	s.deleteReq = req
	return s.deleteErr
}

func (s *fakeRbacBindingStore) ListUsers(_ context.Context, query adminstore.UserListQuery) (adminstore.UserListResult, error) {
	return adminstore.UserListResult{}, nil
}

func (s *fakeRbacBindingStore) GetActiveUserBySubject(_ context.Context, subject string) (adminstore.UserItem, error) {
	return adminstore.UserItem{}, nil
}

func TestAdminRbacBindingsRejectUnknownPath(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unknown rbac path")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutes(mux, logger, pythonWakeClient, nil, guard)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/rbac/bindings-extra", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
}

func TestAdminUsersListNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native users list route")
	})
	fullName := "Admin User"
	createdAt := time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC)
	store := &fakeAdminUserStore{
		result: adminstore.UserListResult{
			Total: 1,
			Items: []adminstore.UserItem{
				{
					ID:         3,
					Username:   "admin",
					Email:      "admin@example.com",
					FullName:   &fullName,
					IsActive:   true,
					LoginCount: 2,
					CreatedAt:  createdAt,
				},
			},
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/users?page=2&page_size=10&search=adm&is_active=true&department=ops&order_by=email&order_desc=false", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.query.Page != 2 || store.query.PageSize != 10 || store.query.Search != "adm" || store.query.Department != "ops" || store.query.OrderBy != "email" || store.query.OrderDesc != false {
		t.Fatalf("unexpected user query: %#v", store.query)
	}
	if store.query.IsActive == nil || *store.query.IsActive != true {
		t.Fatalf("expected is_active=true, got %#v", store.query.IsActive)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected paginated data object, got %#v", resp.Data)
	}
	if data["total"] != float64(1) || data["page"] != float64(2) || data["page_size"] != float64(10) || data["total_pages"] != float64(1) {
		t.Fatalf("unexpected pagination: %#v", data)
	}
	items, ok := data["items"].([]interface{})
	if !ok || len(items) != 1 {
		t.Fatalf("expected one user item, got %#v", data["items"])
	}
	first, ok := items[0].(map[string]interface{})
	if !ok || first["username"] != "admin" || first["email"] != "admin@example.com" {
		t.Fatalf("unexpected user item: %#v", items[0])
	}
}

func TestAdminUsersCreateNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native users create route")
	})
	fullName := "Created User"
	store := &fakeAdminUserStore{
		createUser: adminstore.UserItem{
			ID:         17,
			Username:   "created",
			Email:      "created@example.com",
			FullName:   &fullName,
			IsActive:   true,
			LoginCount: 0,
			CreatedAt:  time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC),
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/users", strings.NewReader(`{"username":"created","email":"Created@Example.com","password":"SmokePass123","full_name":"Created User"}`))
	req.Header.Set("x-auth-user-id", "8")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.createReq.Username != "created" || store.createReq.Email != "created@example.com" || store.createReq.Password != "SmokePass123" {
		t.Fatalf("unexpected create request: %#v", store.createReq)
	}
	if store.createReq.OperatorID == nil || *store.createReq.OperatorID != 8 {
		t.Fatalf("unexpected operator id: %#v", store.createReq.OperatorID)
	}
}

type fakeAdminUserStore struct {
	result             adminstore.UserListResult
	query              adminstore.UserListQuery
	err                error
	createReq          adminstore.UserCreateRequest
	createUser         adminstore.UserItem
	createErr          error
	updateReq          adminstore.UserUpdateRequest
	updateUser         adminstore.UserItem
	updateErr          error
	toggleReq          adminstore.UserToggleStatusRequest
	toggleUser         adminstore.UserItem
	toggleErr          error
	resetReq           adminstore.UserResetPasswordRequest
	resetErr           error
	deleteReq          adminstore.UserDeleteRequest
	deleteErr          error
	batchStatusReq     adminstore.UserBatchStatusRequest
	batchStatus        adminstore.UserBatchStatusResult
	batchStatusErr     error
	batchDeleteReq     adminstore.UserBatchDeleteRequest
	batchDelete        adminstore.UserBatchDeleteResult
	batchDeleteErr     error
	batchResetReq      adminstore.UserBatchResetPasswordRequest
	batchReset         adminstore.UserBatchResetPasswordResult
	batchResetErr      error
	exportAuditReq     adminstore.UserExportAuditRequest
	exportAuditErr     error
	profileSubject     string
	profileUser        adminstore.UserItem
	profileErr         error
	profileUpdateReq   adminstore.ProfileUpdateRequest
	profileUpdateUser  adminstore.UserItem
	profileUpdateErr   error
	profilePasswordReq adminstore.ProfilePasswordChangeRequest
	profilePasswordErr error
	statsSubject       string
	stats              adminstore.ProfileStats
	statsErr           error
	loginReq           adminstore.UserLoginRequest
	loginUser          adminstore.UserItem
	loginErr           error
	registerReq        adminstore.UserRegisterRequest
	registerUser       adminstore.UserItem
	registerErr        error
	logoutReq          adminstore.UserLogoutRequest
	logoutErr          error
	permissionResult   authz.CheckResult
	permissionErr      error
	permissionSubject  string
	permissionCode     string
	permissionScope    map[string]string
}

func (s *fakeAdminUserStore) ListUsers(_ context.Context, query adminstore.UserListQuery) (adminstore.UserListResult, error) {
	s.query = query
	return s.result, s.err
}

func (s *fakeAdminUserStore) CreateUser(_ context.Context, req adminstore.UserCreateRequest) (adminstore.UserItem, error) {
	s.createReq = req
	return s.createUser, s.createErr
}

func (s *fakeAdminUserStore) UpdateUser(_ context.Context, req adminstore.UserUpdateRequest) (adminstore.UserItem, error) {
	s.updateReq = req
	return s.updateUser, s.updateErr
}

func (s *fakeAdminUserStore) ToggleUserStatus(_ context.Context, req adminstore.UserToggleStatusRequest) (adminstore.UserItem, error) {
	s.toggleReq = req
	return s.toggleUser, s.toggleErr
}

func (s *fakeAdminUserStore) DeleteUser(_ context.Context, req adminstore.UserDeleteRequest) error {
	s.deleteReq = req
	return s.deleteErr
}

func (s *fakeAdminUserStore) ResetUserPassword(_ context.Context, req adminstore.UserResetPasswordRequest) error {
	s.resetReq = req
	return s.resetErr
}

func (s *fakeAdminUserStore) BatchUpdateUserStatus(_ context.Context, req adminstore.UserBatchStatusRequest) (adminstore.UserBatchStatusResult, error) {
	s.batchStatusReq = req
	return s.batchStatus, s.batchStatusErr
}

func (s *fakeAdminUserStore) BatchDeleteUsers(_ context.Context, req adminstore.UserBatchDeleteRequest) (adminstore.UserBatchDeleteResult, error) {
	s.batchDeleteReq = req
	return s.batchDelete, s.batchDeleteErr
}

func (s *fakeAdminUserStore) BatchResetUserPasswords(_ context.Context, req adminstore.UserBatchResetPasswordRequest) (adminstore.UserBatchResetPasswordResult, error) {
	s.batchResetReq = req
	return s.batchReset, s.batchResetErr
}

func (s *fakeAdminUserStore) RecordUserExportAudit(_ context.Context, req adminstore.UserExportAuditRequest) error {
	s.exportAuditReq = req
	return s.exportAuditErr
}

func (s *fakeAdminUserStore) ListRbacBindings(_ context.Context, userID *int) ([]adminstore.RbacBinding, error) {
	return nil, nil
}

func (s *fakeAdminUserStore) CreateRbacBinding(_ context.Context, req adminstore.RbacBindingMutationRequest) (adminstore.RbacBinding, error) {
	return adminstore.RbacBinding{}, nil
}

func (s *fakeAdminUserStore) DeleteRbacBinding(_ context.Context, req adminstore.RbacBindingDeleteRequest) error {
	return nil
}

func (s *fakeAdminUserStore) GetActiveUserBySubject(_ context.Context, subject string) (adminstore.UserItem, error) {
	s.profileSubject = subject
	return s.profileUser, s.profileErr
}

func (s *fakeAdminUserStore) UpdateProfileBySubject(_ context.Context, req adminstore.ProfileUpdateRequest) (adminstore.UserItem, error) {
	s.profileUpdateReq = req
	return s.profileUpdateUser, s.profileUpdateErr
}

func (s *fakeAdminUserStore) ChangeProfilePasswordBySubject(_ context.Context, req adminstore.ProfilePasswordChangeRequest) error {
	s.profilePasswordReq = req
	return s.profilePasswordErr
}

func (s *fakeAdminUserStore) GetProfileStatsBySubject(_ context.Context, subject string) (adminstore.ProfileStats, error) {
	s.statsSubject = subject
	return s.stats, s.statsErr
}

func (s *fakeAdminUserStore) LoginAdminUser(_ context.Context, req adminstore.UserLoginRequest) (adminstore.UserItem, error) {
	s.loginReq = req
	return s.loginUser, s.loginErr
}

func (s *fakeAdminUserStore) RegisterAdminUser(_ context.Context, req adminstore.UserRegisterRequest) (adminstore.UserItem, error) {
	s.registerReq = req
	return s.registerUser, s.registerErr
}

func (s *fakeAdminUserStore) LogoutAdminUser(_ context.Context, req adminstore.UserLogoutRequest) error {
	s.logoutReq = req
	return s.logoutErr
}

func (s *fakeAdminUserStore) CheckPermission(_ context.Context, subject string, permission string, scope map[string]string) (authz.CheckResult, error) {
	s.permissionSubject = subject
	s.permissionCode = permission
	s.permissionScope = scope
	return s.permissionResult, s.permissionErr
}

func TestAdminAuthLoginNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth login route")
	})
	createdAt := time.Date(2026, 6, 5, 8, 0, 0, 0, time.UTC)
	store := &fakeAdminUserStore{
		loginUser: adminstore.UserItem{
			ID:         9,
			Username:   "login",
			Email:      "login@example.com",
			IsActive:   true,
			LoginCount: 3,
			CreatedAt:  createdAt,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/auth/login", strings.NewReader(`{"username":"LOGIN@example.com","password":"Secret123"}`))
	req.Header.Set(traceHeader, "trace-login")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.loginReq.Email != "login@example.com" || store.loginReq.Password != "Secret123" {
		t.Fatalf("unexpected login request: %#v", store.loginReq)
	}
	if store.loginReq.TraceID == nil || *store.loginReq.TraceID != "trace-login" {
		t.Fatalf("unexpected trace id: %#v", store.loginReq.TraceID)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected login response object, got %#v", resp.Data)
	}
	token, _ := data["token"].(string)
	if token == "" {
		t.Fatalf("expected token in response: %#v", data)
	}
	claims, err := newAdminJWTVerifier("test-secret").verify(token)
	if err != nil {
		t.Fatalf("verify issued token: %v", err)
	}
	if claims.Subject != "login@example.com" {
		t.Fatalf("unexpected token subject: %s", claims.Subject)
	}
	if data["expires_in"] != float64(86400) {
		t.Fatalf("unexpected expires_in: %#v", data["expires_in"])
	}
}

func TestAdminAuthRegisterNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth register route")
	})
	createdAt := time.Date(2026, 6, 5, 8, 30, 0, 0, time.UTC)
	store := &fakeAdminUserStore{
		registerUser: adminstore.UserItem{
			ID:         10,
			Username:   "firstadmin",
			Email:      "firstadmin@example.com",
			IsActive:   true,
			LoginCount: 0,
			CreatedAt:  createdAt,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/auth/register", strings.NewReader(`{"email":"FIRSTADMIN@example.com","password":"Secret123"}`))
	req.Header.Set(traceHeader, "trace-register")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.registerReq.Email != "firstadmin@example.com" || store.registerReq.Password != "Secret123" {
		t.Fatalf("unexpected register request: %#v", store.registerReq)
	}
	if store.registerReq.TraceID == nil || *store.registerReq.TraceID != "trace-register" {
		t.Fatalf("unexpected trace id: %#v", store.registerReq.TraceID)
	}

	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected register response object, got %#v", resp.Data)
	}
	if data["message"] != "注册成功，请登录" {
		t.Fatalf("unexpected register message: %#v", data["message"])
	}
	user, ok := data["user"].(map[string]interface{})
	if !ok {
		t.Fatalf("expected register user object, got %#v", data["user"])
	}
	if user["email"] != "firstadmin@example.com" {
		t.Fatalf("unexpected registered user: %#v", user)
	}
}

func TestAdminAuthRegisterNativeRouteReturnsConflict(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth register conflict route")
	})
	store := &fakeAdminUserStore{
		registerErr: adminstore.ErrUserConflict,
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/auth/register", strings.NewReader(`{"email":"firstadmin@example.com","password":"Secret123"}`))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminAuthRegisterNativeRouteRejectsWeakPassword(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for invalid register payload")
	})
	store := &fakeAdminUserStore{}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/auth/register", strings.NewReader(`{"email":"firstadmin@example.com","password":"weak"}`))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	if store.registerReq.Email != "" {
		t.Fatalf("store should not be called for invalid payload: %#v", store.registerReq)
	}
}

func TestAdminAuthLogoutNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth logout route")
	})
	store := &fakeAdminUserStore{}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/auth/logout", nil)
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "logout@example.com", "test-secret", time.Now().Add(time.Hour)))
	req.Header.Set(traceHeader, "trace-logout")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.logoutReq.Subject != "logout@example.com" {
		t.Fatalf("unexpected logout subject: %#v", store.logoutReq)
	}
	if store.logoutReq.TraceID == nil || *store.logoutReq.TraceID != "trace-logout" {
		t.Fatalf("unexpected trace id: %#v", store.logoutReq.TraceID)
	}
}

func TestAdminAuthAuthorizeNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth authorize route")
	})
	store := &fakeAdminUserStore{
		permissionResult: authz.CheckResult{
			Allowed: true,
			Reason:  "allowed",
			UserID:  11,
			User:    "authz",
			Email:   "authz@example.com",
			Scope:   map[string]string{"tenant_id": "tenant-a", "project_id": "", "kb_id": ""},
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/auth/authorize?permission=user:manage", nil)
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "authz@example.com", "test-secret", time.Now().Add(time.Hour)))
	req.Header.Set("x-tenant-id", "tenant-a")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.permissionSubject != "authz@example.com" || store.permissionCode != "user:manage" {
		t.Fatalf("unexpected permission call: subject=%s permission=%s", store.permissionSubject, store.permissionCode)
	}
	if store.permissionScope["x-tenant-id"] != "tenant-a" {
		t.Fatalf("unexpected permission scope: %#v", store.permissionScope)
	}
}

func TestAdminAuthChangePasswordNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth change-password route")
	})
	store := &fakeAdminUserStore{}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/auth/change-password", strings.NewReader(`{"old_password":"OldPass123","new_password":"NewPass123"}`))
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "profile@example.com", "test-secret", time.Now().Add(time.Hour)))
	req.Header.Set(traceHeader, "trace-auth-password")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.profilePasswordReq.Subject != "profile@example.com" || store.profilePasswordReq.OldPassword != "OldPass123" || store.profilePasswordReq.NewPassword != "NewPass123" {
		t.Fatalf("unexpected profile password request: %#v", store.profilePasswordReq)
	}
	if store.profilePasswordReq.TraceID == nil || *store.profilePasswordReq.TraceID != "trace-auth-password" {
		t.Fatalf("unexpected trace id: %#v", store.profilePasswordReq.TraceID)
	}
}

func TestAdminAuthProfileLegacyAliasMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth profile route")
	})
	createdAt := time.Date(2026, 6, 5, 10, 0, 0, 0, time.UTC)
	store := &fakeAdminUserStore{
		profileUser: adminstore.UserItem{
			ID:         12,
			Username:   "profile",
			Email:      "profile@example.com",
			IsActive:   true,
			LoginCount: 5,
			CreatedAt:  createdAt,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	// /auth/profile is kept only as a legacy alias of /auth/me for older clients.
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/auth/profile", nil)
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "profile@example.com", "test-secret", time.Now().Add(time.Hour)))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.profileSubject != "profile@example.com" {
		t.Fatalf("unexpected profile subject: %s", store.profileSubject)
	}
}

func TestAdminUsersUpdateNativeRouteUsesManagePermission(t *testing.T) {
	t.Parallel()
	var gotPermission string
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native users update route")
	})
	fullName := "Updated User"
	store := &fakeAdminUserStore{
		updateUser: adminstore.UserItem{
			ID:         15,
			Username:   "usr",
			Email:      "updated@example.com",
			FullName:   &fullName,
			IsActive:   true,
			LoginCount: 1,
			CreatedAt:  time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC),
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newGoDBPermissionGuardForTest(&gotPermission, 1)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPut, "/api/v1/admin/users/15", strings.NewReader(`{"email":"Updated@Example.com","full_name":"Updated User","is_active":true}`))
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	req.Header.Set(traceHeader, "trace-user-update")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if gotPermission != "user:manage" {
		t.Fatalf("expected user:manage permission, got %s", gotPermission)
	}
	if store.updateReq.UserID != 15 || store.updateReq.Email == nil || *store.updateReq.Email != "updated@example.com" {
		t.Fatalf("unexpected update request: %#v", store.updateReq)
	}
	if store.updateReq.OperatorID == nil || *store.updateReq.OperatorID != 1 {
		t.Fatalf("unexpected operator id: %#v", store.updateReq.OperatorID)
	}
}

func TestAdminUsersToggleAndDeleteNativeRoutesSkipProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native users mutation route")
	})
	store := &fakeAdminUserStore{
		toggleUser: adminstore.UserItem{
			ID:         16,
			Username:   "target",
			Email:      "target@example.com",
			IsActive:   false,
			LoginCount: 1,
			CreatedAt:  time.Date(2026, 6, 5, 9, 0, 0, 0, time.UTC),
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	toggleRec := httptest.NewRecorder()
	toggleReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/users/16/toggle-status", nil)
	toggleReq.Header.Set("x-auth-user-id", "8")
	mux.ServeHTTP(toggleRec, toggleReq)

	if toggleRec.Code != http.StatusOK {
		t.Fatalf("expected toggle 200, got %d", toggleRec.Code)
	}
	if toggleRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected toggle route owner: %s", toggleRec.Header().Get(routeOwnerHeader))
	}
	if store.toggleReq.UserID != 16 || store.toggleReq.OperatorID == nil || *store.toggleReq.OperatorID != 8 {
		t.Fatalf("unexpected toggle request: %#v", store.toggleReq)
	}

	deleteRec := httptest.NewRecorder()
	deleteReq := httptest.NewRequest(http.MethodDelete, "/api/v1/admin/users/16?soft_delete=false", nil)
	deleteReq.Header.Set("x-auth-user-id", "8")
	mux.ServeHTTP(deleteRec, deleteReq)

	if deleteRec.Code != http.StatusOK {
		t.Fatalf("expected delete 200, got %d", deleteRec.Code)
	}
	if deleteRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected delete route owner: %s", deleteRec.Header().Get(routeOwnerHeader))
	}
	if store.deleteReq.UserID != 16 || store.deleteReq.SoftDelete {
		t.Fatalf("unexpected delete request: %#v", store.deleteReq)
	}
}

func TestAdminUsersBatchStatusAndDeleteNativeRoutesSkipProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native users batch mutation route")
	})
	store := &fakeAdminUserStore{
		batchStatus: adminstore.UserBatchStatusResult{
			UpdatedCount:   1,
			UpdatedIDs:     []int{16},
			NotFoundIDs:    []int{999},
			SkippedSelfIDs: []int{8},
		},
		batchDelete: adminstore.UserBatchDeleteResult{
			DeletedCount:   1,
			DeletedIDs:     []int{16},
			NotFoundIDs:    []int{},
			SkippedSelfIDs: []int{8},
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	statusRec := httptest.NewRecorder()
	statusReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/users/batch-status", strings.NewReader(`{"user_ids":[16,999,8],"is_active":false}`))
	statusReq.Header.Set("x-auth-user-id", "8")
	mux.ServeHTTP(statusRec, statusReq)

	if statusRec.Code != http.StatusOK {
		t.Fatalf("expected batch status 200, got %d", statusRec.Code)
	}
	if statusRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected batch status route owner: %s", statusRec.Header().Get(routeOwnerHeader))
	}
	if len(store.batchStatusReq.UserIDs) != 3 || store.batchStatusReq.IsActive {
		t.Fatalf("unexpected batch status request: %#v", store.batchStatusReq)
	}

	deleteRec := httptest.NewRecorder()
	deleteReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/users/batch-delete", strings.NewReader(`{"user_ids":[16,8]}`))
	deleteReq.Header.Set("x-auth-user-id", "8")
	mux.ServeHTTP(deleteRec, deleteReq)

	if deleteRec.Code != http.StatusOK {
		t.Fatalf("expected batch delete 200, got %d", deleteRec.Code)
	}
	if deleteRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected batch delete route owner: %s", deleteRec.Header().Get(routeOwnerHeader))
	}
	if len(store.batchDeleteReq.UserIDs) != 2 || !store.batchDeleteReq.SoftDelete {
		t.Fatalf("unexpected batch delete request: %#v", store.batchDeleteReq)
	}
}

func TestAdminUsersResetPasswordNativeRoutesSkipProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native users password reset route")
	})
	store := &fakeAdminUserStore{
		batchReset: adminstore.UserBatchResetPasswordResult{
			ResetCount:     1,
			ResetIDs:       []int{16},
			NotFoundIDs:    []int{999},
			SkippedSelfIDs: []int{8},
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	resetRec := httptest.NewRecorder()
	resetReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/users/16/reset-password", strings.NewReader(`{"new_password":"SmokePass123"}`))
	resetReq.Header.Set("x-auth-user-id", "8")
	mux.ServeHTTP(resetRec, resetReq)

	if resetRec.Code != http.StatusOK {
		t.Fatalf("expected reset 200, got %d", resetRec.Code)
	}
	if resetRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected reset route owner: %s", resetRec.Header().Get(routeOwnerHeader))
	}
	if store.resetReq.UserID != 16 || store.resetReq.NewPassword != "SmokePass123" {
		t.Fatalf("unexpected reset request: %#v", store.resetReq)
	}

	batchRec := httptest.NewRecorder()
	batchReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/users/batch-reset-password", strings.NewReader(`{"user_ids":[16,999,8],"new_password":"SmokePass123"}`))
	batchReq.Header.Set("x-auth-user-id", "8")
	mux.ServeHTTP(batchRec, batchReq)

	if batchRec.Code != http.StatusOK {
		t.Fatalf("expected batch reset 200, got %d", batchRec.Code)
	}
	if batchRec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected batch reset route owner: %s", batchRec.Header().Get(routeOwnerHeader))
	}
	if len(store.batchResetReq.UserIDs) != 3 || store.batchResetReq.NewPassword != "SmokePass123" {
		t.Fatalf("unexpected batch reset request: %#v", store.batchResetReq)
	}
}

func TestAdminUsersRootRejectsUnknownPath(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unknown users root path")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutes(mux, logger, pythonWakeClient, nil, guard)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/users-extra", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
}

func TestAdminUsersExportCSVNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native users export route")
	})
	fullName := "Export User"
	department := "ops"
	lastLogin := time.Date(2026, 6, 5, 7, 30, 0, 0, time.UTC)
	updatedAt := time.Date(2026, 6, 5, 8, 30, 0, 0, time.UTC)
	lastLoginIP := "127.0.0.1"
	store := &fakeAdminUserStore{
		result: adminstore.UserListResult{
			Items: []adminstore.UserItem{
				{
					ID:          21,
					Username:    "exporter",
					Email:       "exporter@example.com",
					FullName:    &fullName,
					Department:  &department,
					IsActive:    true,
					LastLogin:   &lastLogin,
					LoginCount:  8,
					CreatedAt:   time.Date(2026, 6, 1, 9, 0, 0, 0, time.UTC),
					UpdatedAt:   &updatedAt,
					LastLoginIP: &lastLoginIP,
				},
			},
			Total: 1,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/users/export-csv?search=exp&is_active=true&department=ops&order_by=username&order_desc=false", nil)
	req.Header.Set(traceHeader, "trace-export")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if got := rec.Header().Get("Content-Type"); got != "text/csv; charset=utf-8" {
		t.Fatalf("unexpected content type: %s", got)
	}
	if store.query.PageSize != 200 || store.query.Search != "exp" || store.query.Department != "ops" || store.query.OrderBy != "username" || store.query.OrderDesc {
		t.Fatalf("unexpected export query: %#v", store.query)
	}
	if store.exportAuditReq.Rows != 1 || store.exportAuditReq.TraceID == nil || *store.exportAuditReq.TraceID != "trace-export" {
		t.Fatalf("unexpected export audit request: %#v", store.exportAuditReq)
	}

	body := rec.Body.String()
	if !strings.Contains(body, "username,email,full_name") {
		t.Fatalf("expected csv header in body: %q", body)
	}
	if !strings.Contains(body, "exporter,exporter@example.com,Export User") {
		t.Fatalf("expected csv row in body: %q", body)
	}
}

func TestAdminUsersUnknownSubpathReturnsGoOwned404(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unsupported users subpath")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminUserStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/users/15/details", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}

func TestAdminProfileReadNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native profile read route")
	})
	createdAt := time.Date(2026, 6, 5, 8, 0, 0, 0, time.UTC)
	preferredHomePath := "/workspace"
	store := &fakeAdminUserStore{
		profileUser: adminstore.UserItem{
			ID:                5,
			Username:          "profile",
			Email:             "profile@example.com",
			PreferredHomePath: &preferredHomePath,
			IsActive:          true,
			LoginCount:        3,
			CreatedAt:         createdAt,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/profile", nil)
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "profile@example.com", "test-secret", time.Now().Add(time.Hour)))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.profileSubject != "profile@example.com" {
		t.Fatalf("unexpected profile subject: %s", store.profileSubject)
	}
	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok || data["username"] != "profile" || data["email"] != "profile@example.com" {
		t.Fatalf("unexpected profile data: %#v", resp.Data)
	}
	if data["preferred_home_path"] != "/workspace" {
		t.Fatalf("unexpected preferred home path: %#v", data["preferred_home_path"])
	}
}

func TestAdminAuthMeNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native auth me route")
	})
	createdAt := time.Date(2026, 6, 5, 8, 0, 0, 0, time.UTC)
	preferredHomePath := "/admin/dashboard"
	store := &fakeAdminUserStore{
		profileUser: adminstore.UserItem{
			ID:                7,
			Username:          "me",
			Email:             "me@example.com",
			PreferredHomePath: &preferredHomePath,
			IsActive:          true,
			LoginCount:        9,
			CreatedAt:         createdAt,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/auth/me", nil)
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "me@example.com", "test-secret", time.Now().Add(time.Hour)))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.profileSubject != "me@example.com" {
		t.Fatalf("unexpected auth me subject: %s", store.profileSubject)
	}
	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok || data["preferred_home_path"] != "/admin/dashboard" {
		t.Fatalf("unexpected auth me data: %#v", resp.Data)
	}
}

func TestAdminProfileStatsNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native profile stats route")
	})
	createdAt := time.Date(2026, 6, 5, 8, 0, 0, 0, time.UTC)
	store := &fakeAdminUserStore{
		stats: adminstore.ProfileStats{
			TotalLogins:     12,
			RecentLogins30D: 4,
			TotalOperations: 21,
			AccountCreated:  createdAt,
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/profile/stats", nil)
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "stats@example.com", "test-secret", time.Now().Add(time.Hour)))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.statsSubject != "stats@example.com" {
		t.Fatalf("unexpected profile stats subject: %s", store.statsSubject)
	}
	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok || data["total_logins"].(float64) != 12 || data["recent_logins_30d"].(float64) != 4 {
		t.Fatalf("unexpected profile stats data: %#v", resp.Data)
	}
}

func TestAdminProfileUpdateNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native profile update route")
	})
	fullName := "Updated Profile"
	preferredHomePath := "/workspace"
	store := &fakeAdminUserStore{
		profileUpdateUser: adminstore.UserItem{
			ID:                7,
			Username:          "profile",
			Email:             "updated-profile@example.com",
			FullName:          &fullName,
			PreferredHomePath: &preferredHomePath,
			IsActive:          true,
			CreatedAt:         time.Date(2026, 6, 5, 8, 0, 0, 0, time.UTC),
		},
	}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPut, "/api/v1/admin/profile", strings.NewReader(`{"email":"Updated-Profile@Example.com","full_name":"Updated Profile","phone":"123","preferred_home_path":"/workspace"}`))
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "profile@example.com", "test-secret", time.Now().Add(time.Hour)))
	req.Header.Set("x-auth-user-id", "7")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.profileUpdateReq.Subject != "profile@example.com" {
		t.Fatalf("unexpected profile update subject: %s", store.profileUpdateReq.Subject)
	}
	if store.profileUpdateReq.Email == nil || *store.profileUpdateReq.Email != "updated-profile@example.com" {
		t.Fatalf("unexpected profile update email: %#v", store.profileUpdateReq.Email)
	}
	if store.profileUpdateReq.PreferredHomePath == nil || *store.profileUpdateReq.PreferredHomePath != "/workspace" {
		t.Fatalf("unexpected profile update preferred home path: %#v", store.profileUpdateReq.PreferredHomePath)
	}
	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	data, ok := resp.Data.(map[string]interface{})
	if !ok || data["preferred_home_path"] != "/workspace" {
		t.Fatalf("unexpected profile update response: %#v", resp.Data)
	}
}

func TestAdminProfileUpdateRejectsInvalidPreferredHomePath(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for invalid preferred home path")
	})
	store := &fakeAdminUserStore{}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPut, "/api/v1/admin/profile", strings.NewReader(`{"preferred_home_path":"/admin/unknown"}`))
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "profile@example.com", "test-secret", time.Now().Add(time.Hour)))
	req.Header.Set("x-auth-user-id", "7")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d", rec.Code)
	}
	if store.profileUpdateReq.Subject != "" {
		t.Fatalf("expected update store to be skipped, got %#v", store.profileUpdateReq)
	}
}

func TestAdminProfilePasswordNativeRouteMarksOwnerAndSkipsProxy(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for native profile password route")
	})
	store := &fakeAdminUserStore{}

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := config.Config{AdminSecretKey: "test-secret", RBACEnforceBusinessAPI: false}
	guard := newBusinessPermissionGuard(cfg, logger)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPut, "/api/v1/admin/profile/password", strings.NewReader(`{"old_password":"OldPass123","new_password":"NewPass123"}`))
	req.Header.Set("Authorization", "Bearer "+issueTestAdminJWT(t, "profile@example.com", "test-secret", time.Now().Add(time.Hour)))
	req.Header.Set("x-auth-user-id", "7")
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
	if store.profilePasswordReq.Subject != "profile@example.com" || store.profilePasswordReq.OldPassword != "OldPass123" || store.profilePasswordReq.NewPassword != "NewPass123" {
		t.Fatalf("unexpected password request: %#v", store.profilePasswordReq)
	}
}

func TestAdminProfileUnknownSubpathReturnsGoOwned404(t *testing.T) {
	t.Parallel()

	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("python wake client should not be called for unsupported profile subpath")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newBusinessPermissionGuard(config.Config{RBACEnforceBusinessAPI: false}, logger)
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, &fakeAdminUserStore{})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/admin/profile/details", nil)
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
	if rec.Header().Get(routeOwnerHeader) != "go-native" {
		t.Fatalf("unexpected route owner: %s", rec.Header().Get(routeOwnerHeader))
	}
}
