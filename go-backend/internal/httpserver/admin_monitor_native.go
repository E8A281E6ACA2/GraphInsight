package httpserver

import (
	"bufio"
	"context"
	"fmt"
	"log/slog"
	"math"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
)

var adminMonitorStartedAt = time.Now()

type adminSystemStats struct {
	CPUPercent    float64 `json:"cpu_percent"`
	MemoryPercent float64 `json:"memory_percent"`
	MemoryUsedMB  float64 `json:"memory_used_mb"`
	MemoryTotalMB float64 `json:"memory_total_mb"`
	DiskPercent   float64 `json:"disk_percent"`
	DiskUsedGB    float64 `json:"disk_used_gb"`
	DiskTotalGB   float64 `json:"disk_total_gb"`
	UptimeSeconds float64 `json:"uptime_seconds"`
	Timestamp     string  `json:"timestamp"`
}

type adminDatabaseStatus struct {
	Connected   bool   `json:"connected"`
	Database    string `json:"database"`
	TablesCount *int   `json:"tables_count,omitempty"`
	Message     string `json:"message,omitempty"`
	Error       string `json:"error,omitempty"`
}

type adminNeo4jStatus struct {
	Connected          bool   `json:"connected"`
	URI                string `json:"uri"`
	Database           string `json:"database"`
	NodesCount         *int64 `json:"nodes_count,omitempty"`
	RelationshipsCount *int64 `json:"relationships_count,omitempty"`
	Message            string `json:"message,omitempty"`
	Error              string `json:"error,omitempty"`
}

type adminAIServiceStatus struct {
	Connected        bool   `json:"connected"`
	ServiceName      string `json:"service_name"`
	Model            string `json:"model,omitempty"`
	APIKeyConfigured bool   `json:"api_key_configured"`
	Error            string `json:"error,omitempty"`
}

type adminDependencyStatus struct {
	Connected bool   `json:"connected"`
	Enabled   bool   `json:"enabled"`
	BaseURL   string `json:"base_url,omitempty"`
	Message   string `json:"message,omitempty"`
	Error     string `json:"error,omitempty"`
}

type adminHealthStatus struct {
	Status        string                `json:"status"`
	Timestamp     string                `json:"timestamp"`
	Database      adminDatabaseStatus   `json:"database"`
	Neo4j         adminNeo4jStatus      `json:"neo4j"`
	Milvus        adminDependencyStatus `json:"milvus"`
	PythonBackend adminDependencyStatus `json:"python_backend"`
	AIService     adminAIServiceStatus  `json:"ai_service"`
	System        adminSystemStats      `json:"system"`
	Checks        map[string]bool       `json:"checks"`
}

type adminJobSLOMetrics struct {
	WindowMinutes     int     `json:"window_minutes"`
	TotalJobs         int     `json:"total_jobs"`
	SucceededJobs     int     `json:"succeeded_jobs"`
	FailedJobs        int     `json:"failed_jobs"`
	CancelledJobs     int     `json:"cancelled_jobs"`
	RunningJobs       int     `json:"running_jobs"`
	PendingJobs       int     `json:"pending_jobs"`
	TimeoutFailedJobs int     `json:"timeout_failed_jobs"`
	SuccessRate       float64 `json:"success_rate"`
	TimeoutRate       float64 `json:"timeout_rate"`
	P95DurationMS     float64 `json:"p95_duration_ms"`
	P99DurationMS     float64 `json:"p99_duration_ms"`
	Timestamp         string  `json:"timestamp"`
}

type adminSLOTargetItem struct {
	Value  interface{} `json:"value"`
	Target string      `json:"target"`
}

type adminSLOSnapshot struct {
	API       apiMetricsSnapshot            `json:"api"`
	Jobs      adminstore.JobSLOMetrics      `json:"jobs"`
	SLO       map[string]adminSLOTargetItem `json:"slo"`
	Timestamp string                        `json:"timestamp"`
}

type adminLogTopItem struct {
	Name  string `json:"name"`
	Count int    `json:"count"`
}

type adminLogAlertRoute struct {
	Policy       string `json:"policy"`
	Count        int    `json:"count"`
	ThresholdEnv string `json:"threshold_env"`
}

type adminLogAlertItem struct {
	ID         int     `json:"id"`
	Severity   string  `json:"severity"`
	Action     string  `json:"action,omitempty"`
	Resource   string  `json:"resource,omitempty"`
	ResourceID *string `json:"resource_id,omitempty"`
	Status     string  `json:"status,omitempty"`
	TraceID    string  `json:"trace_id,omitempty"`
	CreatedAt  *string `json:"created_at,omitempty"`
	Message    string  `json:"message,omitempty"`
}

type adminLogSeverityMetrics struct {
	WindowMinutes int                           `json:"window_minutes"`
	TotalLogs     int                           `json:"total_logs"`
	FailedLogs    int                           `json:"failed_logs"`
	SeverityCount map[string]int                `json:"severity_counts"`
	StatusCounts  map[string]int                `json:"status_counts"`
	ErrorRate     float64                       `json:"error_rate"`
	WarnRate      float64                       `json:"warn_rate"`
	FailedRate    float64                       `json:"failed_rate"`
	TopActions    []adminLogTopItem             `json:"top_actions"`
	TopResources  []adminLogTopItem             `json:"top_resources"`
	AlertRoutes   map[string]adminLogAlertRoute `json:"alert_routes"`
	RecentAlerts  []adminLogAlertItem           `json:"recent_alerts"`
	Timestamp     string                        `json:"timestamp"`
}

type adminAlertItem struct {
	Type     string `json:"type"`
	Severity string `json:"severity"`
	Message  string `json:"message"`
}

type adminAlertCheckResult struct {
	Alerts            []adminAlertItem `json:"alerts"`
	AlertCount        int              `json:"alert_count"`
	Sent              bool             `json:"sent"`
	DeliveryError     *string          `json:"delivery_error,omitempty"`
	WebhookConfigured bool             `json:"webhook_configured"`
	Snapshot          adminSLOSnapshot `json:"snapshot"`
	Timestamp         string           `json:"timestamp"`
}

type adminUnifiedMetricsSnapshot struct {
	Summary   map[string]float64            `json:"summary"`
	API       apiMetricsSnapshot            `json:"api"`
	QA        adminstore.QAQualitySnapshot  `json:"qa"`
	Jobs      adminstore.JobSLOMetrics      `json:"jobs"`
	Logs      adminstore.LogSeverityMetrics `json:"logs"`
	Timestamp string                        `json:"timestamp"`
}

type adminQATypeMetric struct {
	QAType       string  `json:"qa_type"`
	Total        int     `json:"total"`
	Failed       int     `json:"failed"`
	SuccessRate  float64 `json:"success_rate"`
	CitationRate float64 `json:"citation_rate"`
	AvgCitations float64 `json:"avg_citations"`
	P95LatencyMS float64 `json:"p95_latency_ms"`
}

type adminQAQualitySnapshot struct {
	WindowSeconds  int                 `json:"window_seconds"`
	TotalRequests  int                 `json:"total_requests"`
	FailedRequests int                 `json:"failed_requests"`
	SuccessRate    float64             `json:"success_rate"`
	FailureRate    float64             `json:"failure_rate"`
	CitationRate   float64             `json:"citation_rate"`
	AvgCitations   float64             `json:"avg_citations"`
	AvgLatencyMS   float64             `json:"avg_latency_ms"`
	P50LatencyMS   float64             `json:"p50_latency_ms"`
	P95LatencyMS   float64             `json:"p95_latency_ms"`
	P99LatencyMS   float64             `json:"p99_latency_ms"`
	ByType         []adminQATypeMetric `json:"by_type"`
	Timestamp      string              `json:"timestamp"`
}

type adminMonitorStore interface {
	GetQAQualityMetrics(ctx context.Context, windowSeconds int) (adminstore.QAQualitySnapshot, error)
	GetLogSeverityMetrics(ctx context.Context, windowMinutes int) (adminstore.LogSeverityMetrics, error)
	GetJobSLOMetrics(ctx context.Context, windowMinutes int) (adminstore.JobSLOMetrics, error)
}

func asAdminMonitorStore(store interface{}) adminMonitorStore {
	typed, _ := store.(adminMonitorStore)
	return typed
}

func buildAdminMonitorStatsNativeHandler(logger *slog.Logger, guard businessPermissionGuard) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		stats, err := collectAdminSystemStats()
		if err != nil {
			logger.Warn("collect monitor stats partially failed", "error", err.Error())
		}
		WriteJSON(w, http.StatusOK, "获取成功", stats)
	}))
}

func buildAdminMonitorHealthNativeHandler(
	cfg config.Config,
	logger *slog.Logger,
	graphSvc graphService,
	graphInitErr error,
	guard businessPermissionGuard,
	configStore adminConfigStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		health := collectAdminHealthStatus(r.Context(), cfg, logger, graphSvc, graphInitErr, configStore)
		WriteJSON(w, http.StatusOK, "获取成功", health)
	}))
}

func buildAdminMonitorPerformanceNativeHandler(metrics *apiMetrics, guard businessPermissionGuard) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		windowSeconds := boundedIntQuery(r, "window_seconds", 900, 60, 86400)
		WriteJSON(w, http.StatusOK, "获取成功", metrics.Snapshot(windowSeconds))
	}))
}

func buildAdminMonitorUnifiedMetricsNativeHandler(logger *slog.Logger, metrics *apiMetrics, guard businessPermissionGuard, monitorStore adminMonitorStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if monitorStore == nil {
			logger.Error("admin monitor store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "监控数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		apiWindowSeconds := boundedIntQuery(r, "api_window_seconds", 900, 60, 86400)
		qaWindowSeconds := boundedIntQuery(r, "qa_window_seconds", 900, 60, 86400)
		jobWindowMinutes := boundedIntQuery(r, "job_window_minutes", 60, 5, 10080)
		snapshot, err := buildAdminUnifiedMetricsSnapshot(r.Context(), metrics, monitorStore, apiWindowSeconds, qaWindowSeconds, jobWindowMinutes)
		if err != nil {
			logger.Error("build unified metrics snapshot failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取统一指标失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", snapshot)
	}))
}

func buildAdminMonitorSLONativeHandler(logger *slog.Logger, metrics *apiMetrics, guard businessPermissionGuard, monitorStore adminMonitorStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if monitorStore == nil {
			logger.Error("admin monitor store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "监控数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		apiWindowSeconds := boundedIntQuery(r, "api_window_seconds", 900, 60, 86400)
		jobWindowMinutes := boundedIntQuery(r, "job_window_minutes", 60, 5, 10080)
		snapshot, err := buildAdminSLOSnapshot(r.Context(), metrics, monitorStore, apiWindowSeconds, jobWindowMinutes)
		if err != nil {
			logger.Error("build admin slo snapshot failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取 SLO 指标失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", snapshot)
	}))
}

func buildAdminMonitorAlertsCheckNativeHandler(logger *slog.Logger, metrics *apiMetrics, guard businessPermissionGuard, monitorStore adminMonitorStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if monitorStore == nil {
			logger.Error("admin monitor store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "监控数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		apiWindowSeconds := boundedIntQuery(r, "api_window_seconds", 900, 60, 86400)
		jobWindowMinutes := boundedIntQuery(r, "job_window_minutes", 60, 5, 10080)
		snapshot, err := buildAdminSLOSnapshot(r.Context(), metrics, monitorStore, apiWindowSeconds, jobWindowMinutes)
		if err != nil {
			logger.Error("build admin alert snapshot failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "检查告警失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		apiErrorThreshold := floatEnvOrDefault("ALERT_API_ERROR_RATE_THRESHOLD", 0.05)
		jobTimeoutThreshold := floatEnvOrDefault("ALERT_JOB_TIMEOUT_RATE_THRESHOLD", 0.10)

		alerts := make([]adminAlertItem, 0, 2)
		if snapshot.API.ErrorRate > apiErrorThreshold {
			alerts = append(alerts, adminAlertItem{
				Type:     "api_error_rate_high",
				Severity: "warning",
				Message:  fmt.Sprintf("API 错误率过高: %.4f > %.4f", snapshot.API.ErrorRate, apiErrorThreshold),
			})
		}
		if snapshot.Jobs.TimeoutRate > jobTimeoutThreshold {
			alerts = append(alerts, adminAlertItem{
				Type:     "job_timeout_rate_high",
				Severity: "warning",
				Message:  fmt.Sprintf("任务超时率过高: %.4f > %.4f", snapshot.Jobs.TimeoutRate, jobTimeoutThreshold),
			})
		}

		WriteJSON(w, http.StatusOK, "检查完成", adminAlertCheckResult{
			Alerts:            alerts,
			AlertCount:        len(alerts),
			Sent:              false,
			WebhookConfigured: false,
			Snapshot:          snapshot,
			Timestamp:         time.Now().UTC().Format(time.RFC3339),
		})
	}))
}

func buildAdminMonitorLogSeverityNativeHandler(logger *slog.Logger, guard businessPermissionGuard, monitorStore adminMonitorStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if monitorStore == nil {
			logger.Error("admin monitor store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "监控数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		windowMinutes := boundedIntQuery(r, "window_minutes", 60, 5, 10080)
		metrics, err := monitorStore.GetLogSeverityMetrics(r.Context(), windowMinutes)
		if err != nil {
			logger.Error("get log severity metrics failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取日志分级指标失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", metrics)
	}))
}

func buildAdminMonitorQANativeHandler(logger *slog.Logger, guard businessPermissionGuard, monitorStore adminMonitorStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if monitorStore == nil {
			logger.Error("admin monitor store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "监控数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		windowSeconds := boundedIntQuery(r, "window_seconds", 900, 60, 86400)
		metrics, err := monitorStore.GetQAQualityMetrics(r.Context(), windowSeconds)
		if err != nil {
			logger.Error("get qa quality metrics failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取问答质量指标失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", metrics)
	}))
}

func collectAdminHealthStatus(
	ctx context.Context,
	cfg config.Config,
	logger *slog.Logger,
	graphSvc graphService,
	graphInitErr error,
	configStore adminConfigStore,
) adminHealthStatus {
	stats, statsErr := collectAdminSystemStats()
	if statsErr != nil && logger != nil {
		logger.Warn("collect monitor health system stats partially failed", "error", statsErr.Error())
	}

	database := adminDatabaseStatus{
		Connected: true,
		Database:  "go-control-plane",
		Message:   "Go control plane is running",
	}
	neo4j := collectAdminNeo4jStatus(ctx, cfg, graphSvc, graphInitErr)
	aiValues := map[string]string{}
	vectorValues := map[string]string{}
	if configStore != nil {
		if values, err := safeConfigValues(ctx, configStore, "ai_service"); err == nil {
			aiValues = values
		} else if logger != nil {
			logger.Warn("collect ai service health config failed", "error", err.Error())
		}
		if values, err := safeConfigValues(ctx, configStore, "vector_store"); err == nil {
			vectorValues = values
		} else if logger != nil {
			logger.Warn("collect vector store health config failed", "error", err.Error())
		}
	}
	aiService := collectAdminAIServiceStatus(cfg, aiValues)
	milvus := collectAdminMilvusStatus(ctx, vectorValues)
	pythonBackend := collectAdminPythonBackendStatus(ctx, cfg.PythonBackendBaseURL)

	checks := map[string]bool{
		"database":       database.Connected,
		"neo4j":          neo4j.Connected,
		"milvus":         !milvus.Enabled || milvus.Connected,
		"python_backend": pythonBackend.Connected,
		"ai_service":     aiService.Connected,
		"disk_space":     stats.DiskPercent < 90,
		"memory":         stats.MemoryPercent < 90,
	}

	status := "healthy"
	for _, ok := range checks {
		if !ok {
			status = "degraded"
			break
		}
	}
	if !checks["database"] && !checks["neo4j"] {
		status = "unhealthy"
	}

	return adminHealthStatus{
		Status:        status,
		Timestamp:     time.Now().UTC().Format(time.RFC3339),
		Database:      database,
		Neo4j:         neo4j,
		Milvus:        milvus,
		PythonBackend: pythonBackend,
		AIService:     aiService,
		System:        stats,
		Checks:        checks,
	}
}

func boundedIntQuery(r *http.Request, key string, fallback int, minValue int, maxValue int) int {
	value := fallback
	if raw := strings.TrimSpace(r.URL.Query().Get(key)); raw != "" {
		if parsed, err := strconv.Atoi(raw); err == nil {
			value = parsed
		}
	}
	if value < minValue {
		return minValue
	}
	if value > maxValue {
		return maxValue
	}
	return value
}

func buildAdminSLOSnapshot(
	ctx context.Context,
	metrics *apiMetrics,
	monitorStore adminMonitorStore,
	apiWindowSeconds int,
	jobWindowMinutes int,
) (adminSLOSnapshot, error) {
	apiSnapshot := metrics.Snapshot(apiWindowSeconds)
	jobSnapshot, err := monitorStore.GetJobSLOMetrics(ctx, jobWindowMinutes)
	if err != nil {
		return adminSLOSnapshot{}, err
	}
	return adminSLOSnapshot{
		API:  apiSnapshot,
		Jobs: jobSnapshot,
		SLO: map[string]adminSLOTargetItem{
			"api_error_rate": {
				Value:  apiSnapshot.ErrorRate,
				Target: "<=0.01",
			},
			"job_success_rate": {
				Value:  jobSnapshot.SuccessRate,
				Target: ">=0.99",
			},
			"job_timeout_rate": {
				Value:  jobSnapshot.TimeoutRate,
				Target: "<=0.10",
			},
			"job_p95_duration_ms": {
				Value:  jobSnapshot.P95DurationMS,
				Target: "track",
			},
		},
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}, nil
}

func buildAdminUnifiedMetricsSnapshot(
	ctx context.Context,
	metrics *apiMetrics,
	monitorStore adminMonitorStore,
	apiWindowSeconds int,
	qaWindowSeconds int,
	jobWindowMinutes int,
) (adminUnifiedMetricsSnapshot, error) {
	apiSnapshot := metrics.Snapshot(apiWindowSeconds)
	qaSnapshot, err := monitorStore.GetQAQualityMetrics(ctx, qaWindowSeconds)
	if err != nil {
		return adminUnifiedMetricsSnapshot{}, err
	}
	jobSnapshot, err := monitorStore.GetJobSLOMetrics(ctx, jobWindowMinutes)
	if err != nil {
		return adminUnifiedMetricsSnapshot{}, err
	}
	logSnapshot, err := monitorStore.GetLogSeverityMetrics(ctx, jobWindowMinutes)
	if err != nil {
		return adminUnifiedMetricsSnapshot{}, err
	}
	return adminUnifiedMetricsSnapshot{
		Summary: map[string]float64{
			"api_error_rate":          apiSnapshot.ErrorRate,
			"api_requests_per_second": apiSnapshot.RequestsPerSecond,
			"qa_success_rate":         qaSnapshot.SuccessRate,
			"qa_citation_rate":        qaSnapshot.CitationRate,
			"job_success_rate":        jobSnapshot.SuccessRate,
			"job_timeout_rate":        jobSnapshot.TimeoutRate,
			"log_error_rate":          logSnapshot.ErrorRate,
			"log_warn_rate":           logSnapshot.WarnRate,
		},
		API:       apiSnapshot,
		QA:        qaSnapshot,
		Jobs:      jobSnapshot,
		Logs:      logSnapshot,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
	}, nil
}

func floatEnvOrDefault(key string, fallback float64) float64 {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return fallback
	}
	return value
}

func collectAdminNeo4jStatus(
	ctx context.Context,
	cfg config.Config,
	graphSvc graphService,
	graphInitErr error,
) adminNeo4jStatus {
	runtimeInfo := graph.RuntimeConnectionInfo{
		URI:             cfg.Neo4jURI,
		Database:        cfg.Neo4jDatabase,
		ConfigMode:      cfg.Neo4jConfigSource,
		ConfigSource:    cfg.Neo4jConfigResolvedSource,
		ResolutionError: cfg.Neo4jConfigResolutionErr,
	}
	if graphSvc != nil {
		runtimeInfo = graphSvc.RuntimeConnectionInfo()
	}
	status := adminNeo4jStatus{
		Connected: false,
		URI:       runtimeInfo.URI,
		Database:  runtimeInfo.Database,
		Message:   "Neo4j unavailable",
	}
	if graphInitErr != nil {
		status.Error = graphInitErr.Error()
		return status
	}
	if graphSvc == nil {
		status.Error = "neo4j service is not initialized"
		return status
	}

	probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if err := graphSvc.CheckHealth(probeCtx); err != nil {
		status.Error = err.Error()
		return status
	}
	status.Connected = true
	status.Message = "Neo4j connected"

	countCtx, countCancel := context.WithTimeout(ctx, 5*time.Second)
	defer countCancel()
	counts, err := graphSvc.CountGraph(countCtx)
	if err == nil {
		status.NodesCount = ptrInt64(counts.NodeCount)
		status.RelationshipsCount = ptrInt64(counts.RelationshipCount)
	}
	return status
}

func collectAdminAIServiceStatus(cfg config.Config, values map[string]string) adminAIServiceStatus {
	provider := firstNonEmptyString(values["provider"], cfg.AIProvider, "openai")
	model := firstNonEmptyString(values["model"], cfg.AIModel)
	apiKey := firstNonEmptyString(values["api_key"], cfg.AIAPIKey)
	apiKeyConfigured := strings.TrimSpace(apiKey) != "" && strings.TrimSpace(apiKey) != "your-api-key-here"
	status := adminAIServiceStatus{
		Connected:        apiKeyConfigured,
		ServiceName:      strings.ToUpper(provider),
		Model:            model,
		APIKeyConfigured: apiKeyConfigured,
	}
	if !apiKeyConfigured {
		status.Error = "API key not configured"
	}
	return status
}

func collectAdminMilvusStatus(ctx context.Context, values map[string]string) adminDependencyStatus {
	enabled := configBool(values, "enabled", envBool("VECTOR_STORE_ENABLED", false))
	uri := firstNonEmptyString(values["uri"], os.Getenv("MILVUS_URI"), "http://127.0.0.1:19530")
	status := adminDependencyStatus{
		Enabled: enabled,
		BaseURL: uri,
		Message: "Milvus unavailable",
	}
	target := milvusProbeTarget(uri)
	if target == "" {
		status.Error = "invalid Milvus URI"
		return status
	}
	dialer := net.Dialer{Timeout: 3 * time.Second}
	conn, err := dialer.DialContext(ctx, "tcp", target)
	if err != nil {
		status.Error = err.Error()
		return status
	}
	_ = conn.Close()
	status.Connected = true
	status.Message = "Milvus connected"
	return status
}

func collectAdminPythonBackendStatus(ctx context.Context, baseURL string) adminDependencyStatus {
	baseURL = strings.TrimRight(firstNonEmptyString(baseURL, "http://127.0.0.1:8001"), "/")
	status := adminDependencyStatus{
		Enabled: true,
		BaseURL: baseURL,
		Message: "Python capability service unavailable",
	}
	probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, baseURL+"/health", nil)
	if err != nil {
		status.Error = err.Error()
		return status
	}
	resp, err := (&http.Client{Timeout: 3 * time.Second}).Do(req)
	if err != nil {
		status.Error = err.Error()
		return status
	}
	defer resp.Body.Close()
	if resp.StatusCode >= http.StatusBadRequest {
		status.Error = fmt.Sprintf("health endpoint returned HTTP %d", resp.StatusCode)
		return status
	}
	status.Connected = true
	status.Message = "Python capability service connected"
	return status
}

func collectAdminSystemStats() (adminSystemStats, error) {
	memTotal, memAvailable, memErr := readLinuxMemInfo()
	diskTotal, diskFree, diskErr := readDiskUsage("/")

	var runtimeMem runtime.MemStats
	runtime.ReadMemStats(&runtimeMem)

	if memTotal <= 0 && runtime.GOOS == "darwin" {
		if darwinTotal, darwinUsed, err := readDarwinMemInfo(); err == nil {
			memTotal = darwinTotal
			memAvailable = darwinTotal - darwinUsed
			memErr = nil
		}
	}

	memoryTotalMB := bytesToMB(memTotal)
	memoryUsedMB := bytesToMB(memTotal - memAvailable)
	if memTotal <= 0 {
		memoryUsedMB = bytesToMB(runtimeMem.Alloc)
	}

	diskTotalGB := bytesToGB(diskTotal)
	diskUsedGB := bytesToGB(diskTotal - diskFree)

	stats := adminSystemStats{
		CPUPercent:    round2(readCPUPercent()),
		MemoryPercent: percent(memoryUsedMB, memoryTotalMB),
		MemoryUsedMB:  round2(memoryUsedMB),
		MemoryTotalMB: round2(memoryTotalMB),
		DiskPercent:   percent(diskUsedGB, diskTotalGB),
		DiskUsedGB:    round2(diskUsedGB),
		DiskTotalGB:   round2(diskTotalGB),
		UptimeSeconds: round2(time.Since(adminMonitorStartedAt).Seconds()),
		Timestamp:     time.Now().UTC().Format(time.RFC3339),
	}

	if memErr != nil {
		return stats, memErr
	}
	return stats, diskErr
}

func readCPUPercent() float64 {
	if runtime.GOOS == "linux" {
		first, err := readLinuxCPUStat()
		if err != nil {
			return 0
		}
		time.Sleep(100 * time.Millisecond)
		second, err := readLinuxCPUStat()
		if err != nil {
			return 0
		}
		totalDelta := second.total - first.total
		idleDelta := second.idle - first.idle
		if totalDelta <= 0 {
			return 0
		}
		return math.Max(0, math.Min(100, (1-float64(idleDelta)/float64(totalDelta))*100))
	}
	if runtime.GOOS == "darwin" {
		output, err := exec.Command("ps", "-A", "-o", "%cpu").Output()
		if err != nil {
			return 0
		}
		var total float64
		for _, line := range strings.Split(string(output), "\n") {
			value, parseErr := strconv.ParseFloat(strings.TrimSpace(line), 64)
			if parseErr == nil {
				total += value
			}
		}
		cpus := runtime.NumCPU()
		if cpus <= 0 {
			return 0
		}
		return math.Min(100, total/float64(cpus))
	}
	return 0
}

type linuxCPUStat struct {
	total uint64
	idle  uint64
}

func readLinuxCPUStat() (linuxCPUStat, error) {
	file, err := os.Open("/proc/stat")
	if err != nil {
		return linuxCPUStat{}, err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	if !scanner.Scan() {
		if err := scanner.Err(); err != nil {
			return linuxCPUStat{}, err
		}
		return linuxCPUStat{}, fmt.Errorf("missing cpu line in /proc/stat")
	}
	fields := strings.Fields(scanner.Text())
	if len(fields) < 5 || fields[0] != "cpu" {
		return linuxCPUStat{}, fmt.Errorf("invalid cpu line in /proc/stat")
	}
	var values []uint64
	for _, field := range fields[1:] {
		value, parseErr := strconv.ParseUint(field, 10, 64)
		if parseErr != nil {
			return linuxCPUStat{}, parseErr
		}
		values = append(values, value)
	}
	var total uint64
	for _, value := range values {
		total += value
	}
	idle := values[3]
	if len(values) > 4 {
		idle += values[4]
	}
	return linuxCPUStat{total: total, idle: idle}, nil
}

func readLinuxMemInfo() (totalBytes uint64, availableBytes uint64, err error) {
	file, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, 0, err
	}
	defer file.Close()

	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 2 {
			continue
		}
		valueKB, parseErr := strconv.ParseUint(fields[1], 10, 64)
		if parseErr != nil {
			continue
		}
		switch strings.TrimSuffix(fields[0], ":") {
		case "MemTotal":
			totalBytes = valueKB * 1024
		case "MemAvailable":
			availableBytes = valueKB * 1024
		}
	}
	if scanErr := scanner.Err(); scanErr != nil {
		return totalBytes, availableBytes, scanErr
	}
	return totalBytes, availableBytes, nil
}

func readDarwinMemInfo() (totalBytes uint64, usedBytes uint64, err error) {
	totalOutput, err := exec.Command("sysctl", "-n", "hw.memsize").Output()
	if err != nil {
		return 0, 0, err
	}
	totalBytes, err = strconv.ParseUint(strings.TrimSpace(string(totalOutput)), 10, 64)
	if err != nil {
		return 0, 0, err
	}

	pageSize := uint64(os.Getpagesize())
	vmOutput, err := exec.Command("vm_stat").Output()
	if err != nil {
		return totalBytes, 0, err
	}
	var activePages, wiredPages, compressedPages uint64
	for _, line := range strings.Split(string(vmOutput), "\n") {
		parts := strings.SplitN(line, ":", 2)
		if len(parts) != 2 {
			continue
		}
		valueText := strings.Trim(strings.TrimSpace(parts[1]), ".")
		value, parseErr := strconv.ParseUint(strings.ReplaceAll(valueText, ".", ""), 10, 64)
		if parseErr != nil {
			continue
		}
		switch strings.TrimSpace(parts[0]) {
		case "Pages active":
			activePages = value
		case "Pages wired down":
			wiredPages = value
		case "Pages occupied by compressor":
			compressedPages = value
		}
	}
	usedBytes = (activePages + wiredPages + compressedPages) * pageSize
	if usedBytes > totalBytes {
		usedBytes = totalBytes
	}
	return totalBytes, usedBytes, nil
}

func readDiskUsage(path string) (totalBytes uint64, freeBytes uint64, err error) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, 0, err
	}
	total := stat.Blocks * uint64(stat.Bsize)
	free := stat.Bavail * uint64(stat.Bsize)
	return total, free, nil
}

func bytesToMB(value uint64) float64 {
	if value == 0 {
		return 0
	}
	return float64(value) / 1024 / 1024
}

func bytesToGB(value uint64) float64 {
	if value == 0 {
		return 0
	}
	return float64(value) / 1024 / 1024 / 1024
}

func percent(used float64, total float64) float64 {
	if total <= 0 {
		return 0
	}
	return round2((used / total) * 100)
}

func round2(value float64) float64 {
	return math.Round(value*100) / 100
}

func ptrInt64(value int64) *int64 {
	return &value
}

func firstNonEmptyString(values ...string) string {
	for _, value := range values {
		trimmed := strings.TrimSpace(value)
		if trimmed != "" {
			return trimmed
		}
	}
	return ""
}
