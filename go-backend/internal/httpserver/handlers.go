package httpserver

import (
	"bytes"
	"context"
	"encoding/csv"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"mime"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/authz"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
	"graphinsight/go-backend/internal/orchestrator"
	"graphinsight/go-backend/internal/proxy"
)

func registerRoutes(
	mux *http.ServeMux,
	cfg config.Config,
	logger *slog.Logger,
	graphSvc graphService,
	graphInitErr error,
	proxyClient *proxy.Client,
	proxyInitErr error,
	orchestratorClient *orchestrator.Client,
	orchestratorInitErr error,
	adminStore adminNativeStore,
	apiMetricsOpt ...*apiMetrics,
) {
	var metrics *apiMetrics
	if len(apiMetricsOpt) > 0 && apiMetricsOpt[0] != nil {
		metrics = apiMetricsOpt[0]
	} else {
		metrics = newAPIMetrics(5000)
	}
	guard := newBusinessPermissionGuard(cfg, logger, adminStore)
	idempotencyStore := newIdempotencyStore(time.Duration(cfg.IdempotencyCacheTTLSeconds) * time.Second)
	orchestratorMetrics := newOrchestratorMetrics()
	modelConnectionSnapshots := &adminModelConnectionSnapshotStore{}

	mux.HandleFunc("/", withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		WriteJSON(w, http.StatusOK, "欢迎使用 GraphInsight Go API", map[string]interface{}{
			"name":    cfg.AppName,
			"version": cfg.Version,
			"status":  "running",
			"docs":    "/docs",
		})
	}))

	mux.HandleFunc("/health", withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		data := map[string]interface{}{
			"status":  "healthy",
			"service": cfg.AppName,
			"version": cfg.Version,
		}
		if graphInitErr != nil {
			data["status"] = "degraded"
			data["neo4j"] = map[string]interface{}{
				"connected":        false,
				"error":            graphInitErr.Error(),
				"config_mode":      cfg.Neo4jConfigSource,
				"config_source":    cfg.Neo4jConfigResolvedSource,
				"resolution_error": cfg.Neo4jConfigResolutionErr,
			}
		} else if graphSvc == nil {
			data["status"] = "degraded"
			data["neo4j"] = map[string]interface{}{
				"connected":     false,
				"error":         "neo4j service is not initialized",
				"config_mode":   cfg.Neo4jConfigSource,
				"config_source": cfg.Neo4jConfigResolvedSource,
			}
		} else {
			ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
			defer cancel()
			healthErr := graphSvc.CheckHealth(ctx)
			connected := healthErr == nil
			runtimeInfo := graphSvc.RuntimeConnectionInfo()
			data["neo4j"] = map[string]interface{}{
				"connected":     connected,
				"uri":           runtimeInfo.URI,
				"database":      runtimeInfo.Database,
				"config_mode":   runtimeInfo.ConfigMode,
				"config_source": runtimeInfo.ConfigSource,
			}
			if runtimeInfo.ResolutionError != "" {
				data["neo4j"].(map[string]interface{})["resolution_error"] = runtimeInfo.ResolutionError
			}
			if healthErr != nil {
				data["status"] = "degraded"
				data["neo4j"].(map[string]interface{})["error"] = healthErr.Error()
			}
		}
		if proxyInitErr != nil {
			data["python_backend"] = map[string]interface{}{"connected": false, "error": proxyInitErr.Error(), "base_url": cfg.PythonBackendBaseURL}
		} else {
			data["python_backend"] = map[string]interface{}{"connected": true, "base_url": cfg.PythonBackendBaseURL}
		}
		data["authz"] = buildAuthzHealthSummary(cfg)
		if orchestratorInitErr != nil {
			data["orchestrator"] = map[string]interface{}{
				"connected":        false,
				"error":            orchestratorInitErr.Error(),
				"base_url":         cfg.PythonBackendBaseURL,
				"safe_retry_docqa": cfg.OrchestratorSafeRetryDocQA,
			}
		} else {
			data["orchestrator"] = map[string]interface{}{
				"connected":        true,
				"base_url":         cfg.PythonBackendBaseURL,
				"safe_retry_docqa": cfg.OrchestratorSafeRetryDocQA,
			}
		}
		data["orchestrator_metrics"] = orchestratorMetrics.HealthSummary()
		WriteJSON(w, http.StatusOK, "服务正常", data)
	}))

	registerNativeGraphRoutes(mux, logger, graphSvc, graphInitErr, guard)
	registerOrchestratedBusinessRoutes(
		mux,
		cfg,
		logger,
		graphSvc,
		asAdminJobStore(adminStore),
		asAdminConfigStore(adminStore),
		asAdminLogStore(adminStore),
		proxyClient,
		orchestratorClient,
		orchestratorInitErr,
		orchestratorMetrics,
		idempotencyStore,
		guard,
	)
	registerAdminControlPlaneRoutesWithContext(mux, cfg, logger, graphSvc, graphInitErr, metrics, proxyClient, proxyInitErr, guard, adminStore, modelConnectionSnapshots)
	registerPublicCompatibilityRoutes(mux, logger)
	registerMediaRoutes(mux, logger, cfg)
}

func buildAuthzHealthSummary(cfg config.Config) map[string]interface{} {
	mode := strings.ToLower(strings.TrimSpace(cfg.RBACAuthzMode))
	if mode == "" {
		mode = "go_db"
	}

	health := map[string]interface{}{
		"mode":                       mode,
		"enforce_business_api":       cfg.RBACEnforceBusinessAPI,
		"permission_check_via_local": true,
	}

	switch mode {
	case "go_db":
		health["connected"] = true
		health["permission_check_via_upstream"] = false
	case "local_jwt", "local_jwt_soft":
		health["connected"] = true
		health["permission_check_via_upstream"] = false
	default:
		health["permission_check_via_upstream"] = false
		health["connected"] = true
	}

	return health
}

func registerNativeGraphRoutes(
	mux *http.ServeMux,
	logger *slog.Logger,
	graphSvc graphService,
	graphInitErr error,
	guard businessPermissionGuard,
) {
	mux.HandleFunc("/api/query", withRouteOwner("go-native", guard.wrap("graph:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if graphSvc == nil || graphInitErr != nil {
			logger.Error("query failed: graph service unavailable", "init_error", graphInitErr)
			writeGraphError(w, http.StatusServiceUnavailable, map[string]interface{}{
				"error":   "Database unavailable",
				"code":    "DATABASE_UNAVAILABLE",
				"message": "Cannot connect to Neo4j database",
			})
			return
		}

		var req graph.QueryRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeGraphError(w, http.StatusBadRequest, map[string]interface{}{
				"error":   "Invalid request body",
				"code":    "INVALID_REQUEST",
				"message": err.Error(),
			})
			return
		}

		resp, err := graphSvc.ExecuteQuery(r.Context(), req.Cypher, req.Parameters)
		if err != nil {
			status, body := graph.ClassifyQueryError(err)
			logger.Warn("query failed", "status", status, "error", err.Error())
			writeGraphError(w, status, body)
			return
		}

		WriteJSON(w, http.StatusOK, "查询成功", resp)
	})))

	mux.HandleFunc("/api/graph/schema", withRouteOwner("go-native", guard.wrap("graph:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if graphSvc == nil || graphInitErr != nil {
			logger.Error("schema discovery failed: graph service unavailable", "init_error", graphInitErr)
			writeGraphError(w, http.StatusServiceUnavailable, map[string]interface{}{
				"error":   "Database unavailable",
				"code":    "DATABASE_UNAVAILABLE",
				"message": "Cannot connect to Neo4j database",
			})
			return
		}

		resp, err := graphSvc.DiscoverSchema(r.Context())
		if err != nil {
			status, body := graph.ClassifyQueryError(err)
			logger.Warn("schema discovery failed", "status", status, "error", err.Error())
			writeGraphError(w, status, body)
			return
		}

		WriteJSON(w, http.StatusOK, "获取图谱结构成功", resp)
	})))

	mux.HandleFunc("/api/expand", withRouteOwner("go-native", guard.wrap("graph:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if graphSvc == nil || graphInitErr != nil {
			logger.Error("expand failed: graph service unavailable", "init_error", graphInitErr)
			writeGraphError(w, http.StatusServiceUnavailable, map[string]interface{}{
				"error":   "Database unavailable",
				"code":    "DATABASE_UNAVAILABLE",
				"message": "Cannot connect to Neo4j database",
			})
			return
		}

		var req graph.ExpandRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeGraphError(w, http.StatusBadRequest, map[string]interface{}{
				"error":   "Invalid request body",
				"code":    "INVALID_REQUEST",
				"message": err.Error(),
			})
			return
		}

		resp, err := graphSvc.ExpandNode(r.Context(), req)
		if err != nil {
			status, body := graph.ClassifyQueryError(err)
			logger.Warn("expand failed", "status", status, "error", err.Error())
			writeGraphError(w, status, body)
			return
		}

		WriteJSON(w, http.StatusOK, "展开节点成功", resp)
	})))

	mux.HandleFunc("/api/node/", withRouteOwner("go-native", guard.wrap("graph:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if graphSvc == nil || graphInitErr != nil {
			logger.Error("node detail failed: graph service unavailable", "init_error", graphInitErr)
			writeGraphError(w, http.StatusServiceUnavailable, map[string]interface{}{
				"error":   "Database unavailable",
				"code":    "DATABASE_UNAVAILABLE",
				"message": "Cannot connect to Neo4j database",
			})
			return
		}

		nodeID := strings.TrimPrefix(r.URL.Path, "/api/node/")
		resp, err := graphSvc.GetNodeDetail(r.Context(), nodeID)
		if err != nil {
			if errors.Is(err, graph.ErrNodeNotFound) {
				writeGraphError(w, http.StatusNotFound, map[string]interface{}{
					"error":   "Node not found",
					"code":    "NODE_NOT_FOUND",
					"message": "Node with ID " + nodeID + " does not exist",
				})
				return
			}
			status, body := graph.ClassifyQueryError(err)
			logger.Warn("node detail failed", "status", status, "error", err.Error())
			writeGraphError(w, status, body)
			return
		}

		WriteJSON(w, http.StatusOK, "获取节点详情成功", resp)
	})))
}

func registerOrchestratedBusinessRoutes(
	mux *http.ServeMux,
	cfg config.Config,
	logger *slog.Logger,
	graphSvc graphService,
	jobStore adminJobStore,
	configStore adminConfigStore,
	logStore adminLogStore,
	pythonWakeClient *proxy.Client,
	orchestratorClient *orchestrator.Client,
	orchestratorInitErr error,
	orchestratorMetrics *orchestratorMetrics,
	idempotencyStore *idempotencyStore,
	guard businessPermissionGuard,
) {
	mux.HandleFunc("/api/docqa", guard.wrap("qa:ask", buildNativeDocQAHandler(
		logger, orchestratorClient, orchestratorInitErr, orchestratorMetrics, logStore, configStore, cfg.OrchestratorSafeRetryDocQA,
	)))
	mux.HandleFunc("/api/docqa/deep-research", guard.wrap("qa:ask", buildNativeDeepResearchHandler(
		logger, orchestratorClient, orchestratorInitErr, orchestratorMetrics, logStore, configStore, cfg.OrchestratorSafeRetryDocQA,
	)))
	mux.HandleFunc("/api/docqa/health", guard.wrap("monitor:read", buildNativeDocQAHealthHandler(
		logger, orchestratorClient, orchestratorInitErr, orchestratorMetrics,
	)))
	mux.HandleFunc("/api/nl2cypher", guard.wrap("nl2cypher:use", buildNativeNL2CypherGenerateHandler(
		logger, orchestratorClient, orchestratorInitErr, orchestratorMetrics, logStore,
	)))
	mux.HandleFunc("/api/nl2cypher/examples", buildNativeNL2CypherExamplesHandler())
	mux.HandleFunc("/api/nl2cypher/status", buildNativeNL2CypherStatusHandler(
		cfg, logger, guard, configStore,
	))
	mux.HandleFunc("/api/graph/build", guard.wrap("graph:build", buildNativeGraphBuildJobHandler(
		logger,
		jobStore,
		configStore,
		pythonWakeClient,
		idempotencyStore,
	)))
	documentsListHandler := buildNativeDocumentsListHandler(cfg, logger, guard)
	documentsDeletedListHandler := buildNativeDeletedDocumentsListHandler(cfg, logger, guard)
	documentsUploadHandler := buildNativeDocumentsUploadHandler(cfg, logger, guard)
	documentDeleteNativeHandler := buildNativeDocumentDeleteHandler(cfg, logger, guard, graphSvc)
	documentRestoreNativeHandler := buildNativeDocumentRestoreHandler(cfg, logger, guard)
	documentsClearHandler := buildNativeDocumentsClearHandler(cfg, logger, guard, graphSvc)
	mux.HandleFunc("/api/documents", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			documentsListHandler(w, r)
		case http.MethodDelete:
			documentsClearHandler(w, r)
		default:
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
		}
	})
	mux.HandleFunc("/api/documents/deleted", documentsDeletedListHandler)
	mux.HandleFunc("/api/documents/", func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/restore"):
			documentRestoreNativeHandler(w, r)
		case r.Method == http.MethodDelete:
			documentDeleteNativeHandler(w, r)
		default:
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
		}
	})
	mux.HandleFunc("/api/documents/upload", documentsUploadHandler)
	mux.HandleFunc("/api/monitor/orchestrator", guard.wrap("monitor:read", func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-native")
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		WriteJSON(w, http.StatusOK, "获取编排指标成功", orchestratorMetrics.Snapshot())
	}))
}

func registerMediaRoutes(
	mux *http.ServeMux,
	logger *slog.Logger,
	cfg config.Config,
) {
	registerMediaRoute(mux, logger, cfg, "/api/media")
	registerMediaRoute(mux, logger, cfg, "/api/media/")
}

func registerMediaRoute(mux *http.ServeMux, logger *slog.Logger, cfg config.Config, path string) {
	mux.HandleFunc(path, func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-native")
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		fileName := strings.TrimPrefix(r.URL.Path, "/api/media/")
		fileName = strings.TrimPrefix(fileName, "/")
		if fileName == "" || strings.Contains(fileName, "..") {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		target := filepath.Join(cfg.MediaStoragePath, filepath.Clean(fileName))
		rel, err := filepath.Rel(cfg.MediaStoragePath, target)
		if err != nil || strings.HasPrefix(rel, "..") {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		info, err := os.Stat(target)
		if err != nil || info.IsDir() {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		if contentType := mime.TypeByExtension(filepath.Ext(target)); contentType != "" {
			w.Header().Set("Content-Type", contentType)
		}
		http.ServeFile(w, r, target)
	})
}

func registerAdminControlPlaneRoutes(
	mux *http.ServeMux,
	logger *slog.Logger,
	pythonWakeClient *proxy.Client,
	pythonWakeInitErr error,
	guard businessPermissionGuard,
) {
	registerAdminControlPlaneRoutesWithContext(
		mux,
		config.Config{},
		logger,
		nil,
		nil,
		newAPIMetrics(5000),
		pythonWakeClient,
		pythonWakeInitErr,
		guard,
		nil,
	)
}

func registerAdminControlPlaneRoutesWithContext(
	mux *http.ServeMux,
	cfg config.Config,
	logger *slog.Logger,
	graphSvc graphService,
	graphInitErr error,
	apiMetrics *apiMetrics,
	pythonWakeClient *proxy.Client,
	pythonWakeInitErr error,
	guard businessPermissionGuard,
	adminStore interface{},
	modelConnectionSnapshotsOpt ...*adminModelConnectionSnapshotStore,
) {
	_ = pythonWakeInitErr
	var modelConnectionSnapshots *adminModelConnectionSnapshotStore
	if len(modelConnectionSnapshotsOpt) > 0 && modelConnectionSnapshotsOpt[0] != nil {
		modelConnectionSnapshots = modelConnectionSnapshotsOpt[0]
	} else {
		modelConnectionSnapshots = &adminModelConnectionSnapshotStore{}
	}
	mux.HandleFunc("/api/v1/admin/auth/login", buildAdminAuthLoginNativeHandler(logger, cfg, asAdminAuthStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/auth/register", buildAdminAuthRegisterNativeHandler(logger, asAdminAuthStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/auth/logout", buildAdminAuthLogoutNativeHandler(logger, cfg, asAdminAuthStore(adminStore)))
	// Keep the legacy alias for older clients, but route new code to /api/v1/admin/auth/me.
	mux.HandleFunc("/api/v1/admin/auth/profile", buildAdminProfileReadNativeHandler(logger, asAdminProfileStore(adminStore), cfg.AdminSecretKey))
	mux.HandleFunc("/api/v1/admin/auth/me", buildAdminProfileReadNativeHandler(logger, asAdminProfileStore(adminStore), cfg.AdminSecretKey))
	mux.HandleFunc("/api/v1/admin/auth/change-password", buildAdminAuthChangePasswordNativeHandler(logger, cfg, asAdminAuthStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/auth/authorize", buildAdminAuthAuthorizeNativeHandler(logger, cfg, asAdminAuthStore(adminStore)))

	mux.HandleFunc("/api/v1/admin/monitor/stats", buildAdminMonitorStatsNativeHandler(logger, guard))
	mux.HandleFunc("/api/v1/admin/monitor/health", buildAdminMonitorHealthNativeHandler(cfg, logger, graphSvc, graphInitErr, guard, asAdminConfigStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/monitor/performance", buildAdminMonitorPerformanceNativeHandler(apiMetrics, guard))
	mux.HandleFunc("/api/v1/admin/monitor/metrics/unified", buildAdminMonitorUnifiedMetricsNativeHandler(logger, apiMetrics, guard, asAdminMonitorStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/monitor/qa", buildAdminMonitorQANativeHandler(logger, guard, asAdminMonitorStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/monitor/slo", buildAdminMonitorSLONativeHandler(logger, apiMetrics, guard, asAdminMonitorStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/monitor/log-severity", buildAdminMonitorLogSeverityNativeHandler(logger, guard, asAdminMonitorStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/monitor/alerts/check", buildAdminMonitorAlertsCheckNativeHandler(logger, apiMetrics, guard, asAdminMonitorStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/monitor/health/simple", buildAdminMonitorSimpleHealthNativeHandler())

	mux.HandleFunc("/api/v1/admin/jobs", buildAdminJobsHandler(logger, pythonWakeClient, guard, asAdminJobStore(adminStore), asAdminConfigStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/jobs/", buildAdminJobsHandler(logger, pythonWakeClient, guard, asAdminJobStore(adminStore), asAdminConfigStore(adminStore)))

	mux.HandleFunc("/api/v1/admin/qa-traces", buildAdminQATracesNativeHandler(logger, guard, asAdminQATraceStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/qa-traces/", buildAdminQATracesNativeHandler(logger, guard, asAdminQATraceStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/qa/retrieval-diagnostics", buildAdminRetrievalDiagnosticsHandler(logger, guard, pythonWakeClient))

	mux.HandleFunc("/api/v1/admin/config", buildAdminConfigHandler(cfg, logger, graphSvc, graphInitErr, guard, asAdminConfigStore(adminStore), modelConnectionSnapshots))
	mux.HandleFunc("/api/v1/admin/config/", buildAdminConfigHandler(cfg, logger, graphSvc, graphInitErr, guard, asAdminConfigStore(adminStore), modelConnectionSnapshots))

	mux.HandleFunc("/api/v1/admin/logs", buildAdminLogsHandler(logger, guard, asAdminLogStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/logs/", buildAdminLogsHandler(logger, guard, asAdminLogStore(adminStore)))

	mux.HandleFunc("/api/v1/admin/rbac/roles", buildAdminRbacCatalogNativeHandler(logger, guard, asRbacCatalogStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/rbac/permissions", buildAdminRbacCatalogNativeHandler(logger, guard, asRbacCatalogStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/rbac/bindings", buildAdminRbacBindingsHandler(logger, guard, asRbacBindingStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/rbac/bindings/", buildAdminRbacBindingsHandler(logger, guard, asRbacBindingStore(adminStore)))

	mux.HandleFunc("/api/v1/admin/users", buildAdminUsersRootHandler(logger, guard, asAdminUserStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/users/", buildAdminUsersSubtreeHandler(logger, guard, asAdminUserStore(adminStore)))

	mux.HandleFunc("/api/v1/admin/profile", buildAdminProfileHandler(cfg, logger, asAdminProfileStore(adminStore), asAdminProfileStatsStore(adminStore)))
	mux.HandleFunc("/api/v1/admin/profile/", buildAdminProfileHandler(cfg, logger, asAdminProfileStore(adminStore), asAdminProfileStatsStore(adminStore)))

	mux.HandleFunc("/api/v1/admin", buildUnknownAdminHandler())
	mux.HandleFunc("/api/v1/admin/", buildUnknownAdminHandler())
}

func buildUnknownAdminHandler() http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		WriteJSON(w, http.StatusNotFound, "Not found", map[string]string{"error_code": "NOT_FOUND"})
	})
}

func buildAdminJobsHandler(
	logger *slog.Logger,
	pythonWakeClient *proxy.Client,
	guard businessPermissionGuard,
	jobStore adminJobStore,
	configStore adminConfigStore,
) http.HandlerFunc {
	readHandler := buildAdminJobsReadNativeHandler(logger, guard, jobStore)
	writeHandler := buildAdminJobsWriteNativeHandler(logger, guard, jobStore, configStore, pythonWakeClient)
	return func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/admin/jobs":
			if r.Method == http.MethodGet {
				readHandler(w, r)
				return
			}
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		case "/api/v1/admin/jobs/build-graph", "/api/v1/admin/jobs/clear-kb", "/api/v1/admin/jobs/reindex":
			if r.Method != http.MethodPost {
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
				return
			}
			writeHandler(w, r)
			return
		default:
			if strings.HasPrefix(r.URL.Path, "/api/v1/admin/jobs/") {
				if r.Method == http.MethodGet {
					readHandler(w, r)
					return
				}
				if r.Method != http.MethodPost {
					WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
					return
				}
				writeHandler(w, r)
				return
			}
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
	}
}

func buildAdminMonitorSimpleHealthNativeHandler() http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		WriteJSON(w, http.StatusOK, "服务正常", map[string]interface{}{"status": "healthy"})
	})
}

func buildAdminConfigHandler(
	cfg config.Config,
	logger *slog.Logger,
	graphSvc graphService,
	graphInitErr error,
	guard businessPermissionGuard,
	configStore adminConfigStore,
	modelConnectionSnapshots *adminModelConnectionSnapshotStore,
) http.HandlerFunc {
	readHandler := buildAdminConfigReadNativeHandler(cfg, logger, guard, configStore, graphSvc)
	mutationHandler := buildAdminConfigMutationNativeHandler(logger, guard, configStore)
	latestModelHandler := buildAdminModelConnectionLatestNativeHandler(guard, modelConnectionSnapshots)
	testConnectionHandler := buildAdminConfigConnectionTestHandler(cfg, logger, guard, configStore, graphSvc, graphInitErr, modelConnectionSnapshots)
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/admin/config/test/model/latest" {
			if r.Method != http.MethodGet {
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
				return
			}
			latestModelHandler(w, r)
			return
		}
		switch r.URL.Path {
		case "/api/v1/admin/config":
			if r.Method == http.MethodGet {
				readHandler(w, r)
				return
			}
			if r.Method == http.MethodPost {
				mutationHandler(w, r)
				return
			}
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		default:
			if strings.HasPrefix(r.URL.Path, "/api/v1/admin/config/") {
				if r.Method == http.MethodGet {
					readHandler(w, r)
					return
				}
				if strings.HasPrefix(r.URL.Path, "/api/v1/admin/config/test/") && r.Method == http.MethodPost {
					testConnectionHandler(w, r)
					return
				}
				if r.URL.Path == "/api/v1/admin/config/batch" && r.Method == http.MethodPost {
					mutationHandler(w, r)
					return
				}
				if r.Method == http.MethodPut || r.Method == http.MethodDelete {
					mutationHandler(w, r)
					return
				}
				if r.Method == http.MethodPost {
					WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
					return
				}
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
				return
			}
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
	})
}

func buildAdminLogsHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	logStore adminLogStore,
) http.HandlerFunc {
	readHandler := buildAdminLogsReadNativeHandler(logger, guard, logStore)
	cleanHandler := buildAdminLogsCleanNativeHandler(logger, guard, logStore)
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/admin/logs":
			if r.Method == http.MethodGet {
				readHandler(w, r)
				return
			}
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		default:
			if strings.HasPrefix(r.URL.Path, "/api/v1/admin/logs/") {
				if r.Method == http.MethodGet {
					readHandler(w, r)
					return
				}
				if r.URL.Path == "/api/v1/admin/logs/clean" && r.Method == http.MethodDelete {
					cleanHandler(w, r)
					return
				}
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
				return
			}
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
	})
}

func buildAdminRbacBindingsHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	bindingStore adminRbacBindingStore,
) http.HandlerFunc {
	readHandler := buildAdminRbacBindingsReadNativeHandler(logger, guard, bindingStore)
	mutationHandler := buildAdminRbacBindingsMutationNativeHandler(logger, guard, bindingStore)
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/admin/rbac/bindings" && !strings.HasPrefix(r.URL.Path, "/api/v1/admin/rbac/bindings/") {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		switch r.Method {
		case http.MethodGet:
			readHandler(w, r)
			return
		case http.MethodPost, http.MethodDelete:
			mutationHandler(w, r)
			return
		default:
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
	}
}

type adminRbacBindingStore interface {
	ListRbacBindings(ctx context.Context, userID *int) ([]adminstore.RbacBinding, error)
	CreateRbacBinding(ctx context.Context, req adminstore.RbacBindingMutationRequest) (adminstore.RbacBinding, error)
	DeleteRbacBinding(ctx context.Context, req adminstore.RbacBindingDeleteRequest) error
}

func asRbacBindingStore(store interface{}) adminRbacBindingStore {
	typed, _ := store.(adminRbacBindingStore)
	return typed
}

func asAdminUserStore(store interface{}) adminUserStore {
	typed, _ := store.(adminUserStore)
	return typed
}

func asAdminProfileStore(store interface{}) adminProfileStore {
	typed, _ := store.(adminProfileStore)
	return typed
}

func asAdminProfileStatsStore(store interface{}) adminProfileStatsStore {
	typed, _ := store.(adminProfileStatsStore)
	return typed
}

func buildAdminRbacBindingsReadNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	bindingStore adminRbacBindingStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("user:manage", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if r.URL.Path != "/api/v1/admin/rbac/bindings" {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		if bindingStore == nil {
			logger.Error("admin rbac binding store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "RBAC 数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		userID, ok := optionalPositiveIntQuery(w, r, "user_id")
		if !ok {
			return
		}
		bindings, err := bindingStore.ListRbacBindings(r.Context(), userID)
		if err != nil {
			logger.Error("list rbac bindings failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取绑定列表失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "ok", bindings)
	}))
}

func buildAdminRbacBindingsMutationNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	bindingStore adminRbacBindingStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("user:manage", func(w http.ResponseWriter, r *http.Request) {
		if bindingStore == nil {
			logger.Error("admin rbac binding store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "RBAC 数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		switch r.Method {
		case http.MethodPost:
			if r.URL.Path != "/api/v1/admin/rbac/bindings" {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			var payload adminRbacBindingCreatePayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			req, ok := buildRbacBindingMutationRequest(w, r, payload)
			if !ok {
				return
			}
			binding, err := bindingStore.CreateRbacBinding(r.Context(), req)
			if errors.Is(err, adminstore.ErrRbacUserNotFound) {
				WriteJSON(w, http.StatusNotFound, "用户不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if errors.Is(err, adminstore.ErrRbacRoleNotFound) {
				WriteJSON(w, http.StatusNotFound, "角色不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if err != nil {
				logger.Error("create rbac binding failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "创建绑定失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "绑定成功", binding)
		case http.MethodDelete:
			bindingID, ok := parseRbacBindingDeletePath(r.URL.Path)
			if !ok {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			err := bindingStore.DeleteRbacBinding(r.Context(), adminstore.RbacBindingDeleteRequest{
				BindingID:  bindingID,
				OperatorID: optionalIntHeader(r, "x-auth-user-id"),
				TraceID:    optionalStringHeader(r, traceHeader),
				IPAddress:  optionalString(firstRemoteAddr(r)),
				UserAgent:  optionalString(r.UserAgent()),
			})
			if errors.Is(err, adminstore.ErrRbacBindingNotFound) {
				WriteJSON(w, http.StatusNotFound, "绑定不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if err != nil {
				logger.Error("delete rbac binding failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "删除绑定失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "删除成功", nil)
		default:
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
		}
	}))
}

type adminRbacBindingCreatePayload struct {
	UserID    int        `json:"user_id"`
	RoleName  string     `json:"role_name"`
	ScopeType string     `json:"scope_type"`
	TenantID  *string    `json:"tenant_id"`
	ProjectID *string    `json:"project_id"`
	KBID      *string    `json:"kb_id"`
	ExpiresAt *time.Time `json:"expires_at"`
}

func buildRbacBindingMutationRequest(w http.ResponseWriter, r *http.Request, payload adminRbacBindingCreatePayload) (adminstore.RbacBindingMutationRequest, bool) {
	scopeType := strings.ToLower(strings.TrimSpace(payload.ScopeType))
	if scopeType == "" {
		scopeType = "global"
	}
	if payload.UserID <= 0 || strings.TrimSpace(payload.RoleName) == "" {
		WriteJSON(w, http.StatusBadRequest, "参数错误", map[string]string{"error_code": "INVALID_BODY"})
		return adminstore.RbacBindingMutationRequest{}, false
	}
	switch scopeType {
	case "global":
	case "tenant":
		if payload.TenantID == nil || strings.TrimSpace(*payload.TenantID) == "" {
			WriteJSON(w, http.StatusBadRequest, "tenant 作用域需要 tenant_id", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.RbacBindingMutationRequest{}, false
		}
	case "project":
		if payload.ProjectID == nil || strings.TrimSpace(*payload.ProjectID) == "" {
			WriteJSON(w, http.StatusBadRequest, "project 作用域需要 project_id", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.RbacBindingMutationRequest{}, false
		}
	case "kb":
		if payload.KBID == nil || strings.TrimSpace(*payload.KBID) == "" {
			WriteJSON(w, http.StatusBadRequest, "kb 作用域需要 kb_id", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.RbacBindingMutationRequest{}, false
		}
	default:
		WriteJSON(w, http.StatusBadRequest, "scope_type 必须是 global/tenant/project/kb", map[string]string{"error_code": "INVALID_BODY"})
		return adminstore.RbacBindingMutationRequest{}, false
	}
	return adminstore.RbacBindingMutationRequest{
		UserID:     payload.UserID,
		RoleName:   strings.TrimSpace(payload.RoleName),
		ScopeType:  scopeType,
		TenantID:   payload.TenantID,
		ProjectID:  payload.ProjectID,
		KBID:       payload.KBID,
		ExpiresAt:  payload.ExpiresAt,
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TraceID:    optionalStringHeader(r, traceHeader),
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
	}, true
}

func parseRbacBindingDeletePath(path string) (int, bool) {
	rest := strings.TrimPrefix(path, "/api/v1/admin/rbac/bindings/")
	if rest == "" || strings.Contains(rest, "/") {
		return 0, false
	}
	bindingID, err := strconv.Atoi(rest)
	if err != nil || bindingID <= 0 {
		return 0, false
	}
	return bindingID, true
}

func optionalPositiveIntQuery(w http.ResponseWriter, r *http.Request, key string) (*int, bool) {
	raw := strings.TrimSpace(r.URL.Query().Get(key))
	if raw == "" {
		return nil, true
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		WriteJSON(w, http.StatusBadRequest, "参数错误", map[string]string{"error_code": "INVALID_QUERY"})
		return nil, false
	}
	return &value, true
}

func optionalBoolQuery(w http.ResponseWriter, r *http.Request, key string) (*bool, bool) {
	raw := strings.TrimSpace(r.URL.Query().Get(key))
	if raw == "" {
		return nil, true
	}
	value, err := strconv.ParseBool(raw)
	if err != nil {
		WriteJSON(w, http.StatusBadRequest, "参数错误", map[string]string{"error_code": "INVALID_QUERY"})
		return nil, false
	}
	return &value, true
}

func optionalBoolQueryDefault(r *http.Request, key string, fallback bool) bool {
	raw := strings.TrimSpace(r.URL.Query().Get(key))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseBool(raw)
	if err != nil {
		return fallback
	}
	return value
}

func buildAdminUsersRootHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	userStore adminUserStore,
) http.HandlerFunc {
	readHandler := buildAdminUsersReadNativeHandler(logger, guard, userStore)
	mutationHandler := buildAdminUsersMutationNativeHandler(logger, guard, userStore)
	return func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/admin/users" {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		switch r.Method {
		case http.MethodGet:
			readHandler(w, r)
			return
		case http.MethodPost:
			mutationHandler(w, r)
			return
		default:
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
	}
}

type adminUserStore interface {
	ListUsers(ctx context.Context, query adminstore.UserListQuery) (adminstore.UserListResult, error)
	RecordUserExportAudit(ctx context.Context, req adminstore.UserExportAuditRequest) error
	CreateUser(ctx context.Context, req adminstore.UserCreateRequest) (adminstore.UserItem, error)
	UpdateUser(ctx context.Context, req adminstore.UserUpdateRequest) (adminstore.UserItem, error)
	ToggleUserStatus(ctx context.Context, req adminstore.UserToggleStatusRequest) (adminstore.UserItem, error)
	DeleteUser(ctx context.Context, req adminstore.UserDeleteRequest) error
	ResetUserPassword(ctx context.Context, req adminstore.UserResetPasswordRequest) error
	BatchUpdateUserStatus(ctx context.Context, req adminstore.UserBatchStatusRequest) (adminstore.UserBatchStatusResult, error)
	BatchDeleteUsers(ctx context.Context, req adminstore.UserBatchDeleteRequest) (adminstore.UserBatchDeleteResult, error)
	BatchResetUserPasswords(ctx context.Context, req adminstore.UserBatchResetPasswordRequest) (adminstore.UserBatchResetPasswordResult, error)
}

func buildAdminUsersReadNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	userStore adminUserStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("user:manage", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if userStore == nil {
			logger.Error("admin user store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "用户数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		if r.URL.Path == "/api/v1/admin/users/export-csv" {
			writeAdminUsersCSVExport(w, r, logger, userStore)
			return
		}
		isActive, ok := optionalBoolQuery(w, r, "is_active")
		if !ok {
			return
		}
		query := adminstore.UserListQuery{
			Page:       boundedIntQuery(r, "page", 1, 1, 1_000_000),
			PageSize:   boundedIntQuery(r, "page_size", 20, 1, 200),
			Search:     strings.TrimSpace(r.URL.Query().Get("search")),
			IsActive:   isActive,
			Department: strings.TrimSpace(r.URL.Query().Get("department")),
			OrderBy:    strings.TrimSpace(r.URL.Query().Get("order_by")),
			OrderDesc:  optionalBoolQueryDefault(r, "order_desc", true),
		}
		result, err := userStore.ListUsers(r.Context(), query)
		if err != nil {
			logger.Error("list users failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取用户列表失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		totalPages := 0
		if query.PageSize > 0 && result.Total > 0 {
			totalPages = (result.Total + query.PageSize - 1) / query.PageSize
		}
		WriteJSON(w, http.StatusOK, "ok", adminPaginatedData{
			Items:      result.Items,
			Total:      result.Total,
			Page:       query.Page,
			PageSize:   query.PageSize,
			TotalPages: totalPages,
		})
	}))
}

const maxAdminUsersCSVExportRows = 100000

func writeAdminUsersCSVExport(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	userStore adminUserStore,
) {
	isActive, ok := optionalBoolQuery(w, r, "is_active")
	if !ok {
		return
	}
	baseQuery := adminstore.UserListQuery{
		Page:       1,
		PageSize:   200,
		Search:     strings.TrimSpace(r.URL.Query().Get("search")),
		IsActive:   isActive,
		Department: strings.TrimSpace(r.URL.Query().Get("department")),
		OrderBy:    strings.TrimSpace(r.URL.Query().Get("order_by")),
		OrderDesc:  optionalBoolQueryDefault(r, "order_desc", true),
	}

	users := make([]adminstore.UserItem, 0, 200)
	total := 0
	for page := 1; len(users) < maxAdminUsersCSVExportRows; page++ {
		query := baseQuery
		query.Page = page
		result, err := userStore.ListUsers(r.Context(), query)
		if err != nil {
			logger.Error("export users csv failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "导出失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		if page == 1 {
			total = result.Total
		}
		if len(result.Items) == 0 {
			break
		}
		remaining := maxAdminUsersCSVExportRows - len(users)
		if len(result.Items) > remaining {
			users = append(users, result.Items[:remaining]...)
			break
		}
		users = append(users, result.Items...)
		if len(users) >= total {
			break
		}
	}

	if err := userStore.RecordUserExportAudit(r.Context(), adminstore.UserExportAuditRequest{
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
		TraceID:    optionalStringHeader(r, traceHeader),
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
		Rows:       len(users),
		Search:     baseQuery.Search,
		IsActive:   baseQuery.IsActive,
		Department: baseQuery.Department,
		OrderBy:    baseQuery.OrderBy,
		OrderDesc:  baseQuery.OrderDesc,
	}); err != nil {
		logger.Warn("write user export audit failed", "error", err.Error())
	}

	var buffer bytes.Buffer
	buffer.WriteString("\ufeff")
	writer := csv.NewWriter(&buffer)
	_ = writer.Write([]string{
		"id",
		"username",
		"email",
		"full_name",
		"phone",
		"department",
		"is_active",
		"last_login",
		"last_login_ip",
		"login_count",
		"created_at",
		"updated_at",
	})
	for _, user := range users {
		_ = writer.Write([]string{
			strconv.Itoa(user.ID),
			user.Username,
			user.Email,
			valueOrEmpty(user.FullName),
			valueOrEmpty(user.Phone),
			valueOrEmpty(user.Department),
			strconv.FormatBool(user.IsActive),
			timeOrEmpty(user.LastLogin),
			valueOrEmpty(user.LastLoginIP),
			strconv.Itoa(user.LoginCount),
			user.CreatedAt.UTC().Format(time.RFC3339),
			timeOrEmpty(user.UpdatedAt),
		})
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		logger.Error("encode users csv failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "导出失败", map[string]string{"error_code": "CSV_ENCODE_FAILED"})
		return
	}

	filename := fmt.Sprintf("users_%s.csv", time.Now().UTC().Format("20060102_150405"))
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf("attachment; filename=%q", filename))
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(buffer.Bytes())
}

func valueOrEmpty(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func timeOrEmpty(value *time.Time) string {
	if value == nil {
		return ""
	}
	return value.UTC().Format(time.RFC3339)
}

func buildAdminUsersSubtreeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	userStore adminUserStore,
) http.HandlerFunc {
	readHandler := buildAdminUsersReadNativeHandler(logger, guard, userStore)
	mutationHandler := buildAdminUsersMutationNativeHandler(logger, guard, userStore)
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.URL.Path, "/api/v1/admin/users/") {
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		if r.URL.Path == "/api/v1/admin/users/export-csv" {
			readHandler(w, r)
			return
		}
		if isAdminUsersNativeMutationRoute(r) {
			mutationHandler(w, r)
			return
		}
		switch r.Method {
		case http.MethodGet, http.MethodPost, http.MethodPut, http.MethodDelete:
		default:
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
	})
}

func buildAdminUsersMutationNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	userStore adminUserStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("user:manage", func(w http.ResponseWriter, r *http.Request) {
		if userStore == nil {
			logger.Error("admin user store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "用户数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/admin/users":
			var payload adminUserCreatePayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			req, ok := buildAdminUserCreateRequest(w, r, payload)
			if !ok {
				return
			}
			user, err := userStore.CreateUser(r.Context(), req)
			writeAdminUserMutationResult(w, logger, err, "创建用户失败", "创建成功", user)
		case r.Method == http.MethodPut:
			userID, ok := parseAdminUserIDPath(r.URL.Path)
			if !ok {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			var payload adminUserUpdatePayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			req, ok := buildAdminUserUpdateRequest(w, r, userID, payload)
			if !ok {
				return
			}
			user, err := userStore.UpdateUser(r.Context(), req)
			writeAdminUserMutationResult(w, logger, err, "更新用户失败", "更新成功", user)
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/toggle-status"):
			userID, ok := parseAdminUserActionPath(r.URL.Path, "toggle-status")
			if !ok {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			user, err := userStore.ToggleUserStatus(r.Context(), adminstore.UserToggleStatusRequest{
				UserID:     userID,
				OperatorID: optionalIntHeader(r, "x-auth-user-id"),
				TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:    optionalStringHeader(r, traceHeader),
				IPAddress:  optionalString(firstRemoteAddr(r)),
				UserAgent:  optionalString(r.UserAgent()),
			})
			writeAdminUserMutationResult(w, logger, err, "切换状态失败", "状态已更新", user)
		case r.Method == http.MethodPost && strings.HasSuffix(r.URL.Path, "/reset-password"):
			userID, ok := parseAdminUserActionPath(r.URL.Path, "reset-password")
			if !ok {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			var payload adminUserResetPasswordPayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			err := userStore.ResetUserPassword(r.Context(), adminstore.UserResetPasswordRequest{
				UserID:      userID,
				NewPassword: payload.NewPassword,
				OperatorID:  optionalIntHeader(r, "x-auth-user-id"),
				TenantID:    optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:     optionalStringHeader(r, traceHeader),
				IPAddress:   optionalString(firstRemoteAddr(r)),
				UserAgent:   optionalString(r.UserAgent()),
			})
			writeAdminUserPasswordResult(w, logger, err, "重置密码失败", "密码重置成功")
		case r.Method == http.MethodDelete:
			userID, ok := parseAdminUserIDPath(r.URL.Path)
			if !ok {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			err := userStore.DeleteUser(r.Context(), adminstore.UserDeleteRequest{
				UserID:     userID,
				SoftDelete: optionalBoolQueryDefault(r, "soft_delete", true),
				OperatorID: optionalIntHeader(r, "x-auth-user-id"),
				TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:    optionalStringHeader(r, traceHeader),
				IPAddress:  optionalString(firstRemoteAddr(r)),
				UserAgent:  optionalString(r.UserAgent()),
			})
			writeAdminUserDeleteResult(w, logger, err)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/admin/users/batch-status":
			var payload adminUserBatchStatusPayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			result, err := userStore.BatchUpdateUserStatus(r.Context(), adminstore.UserBatchStatusRequest{
				UserIDs:    payload.UserIDs,
				IsActive:   payload.IsActive,
				OperatorID: optionalIntHeader(r, "x-auth-user-id"),
				TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:    optionalStringHeader(r, traceHeader),
				IPAddress:  optionalString(firstRemoteAddr(r)),
				UserAgent:  optionalString(r.UserAgent()),
			})
			if err != nil {
				logger.Error("batch update admin user status failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "批量状态更新失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "批量状态更新完成", result)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/admin/users/batch-delete":
			var payload adminUserBatchDeletePayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			softDelete := true
			if payload.SoftDelete != nil {
				softDelete = *payload.SoftDelete
			}
			result, err := userStore.BatchDeleteUsers(r.Context(), adminstore.UserBatchDeleteRequest{
				UserIDs:    payload.UserIDs,
				SoftDelete: softDelete,
				OperatorID: optionalIntHeader(r, "x-auth-user-id"),
				TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:    optionalStringHeader(r, traceHeader),
				IPAddress:  optionalString(firstRemoteAddr(r)),
				UserAgent:  optionalString(r.UserAgent()),
			})
			if err != nil {
				logger.Error("batch delete admin users failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "批量删除失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "批量删除完成", result)
		case r.Method == http.MethodPost && r.URL.Path == "/api/v1/admin/users/batch-reset-password":
			var payload adminUserBatchResetPasswordPayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			result, err := userStore.BatchResetUserPasswords(r.Context(), adminstore.UserBatchResetPasswordRequest{
				UserIDs:     payload.UserIDs,
				NewPassword: payload.NewPassword,
				OperatorID:  optionalIntHeader(r, "x-auth-user-id"),
				TenantID:    optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:     optionalStringHeader(r, traceHeader),
				IPAddress:   optionalString(firstRemoteAddr(r)),
				UserAgent:   optionalString(r.UserAgent()),
			})
			if err != nil {
				logger.Error("batch reset admin user passwords failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "批量重置密码失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "批量重置密码完成", result)
		default:
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	}))
}

type adminUserCreatePayload struct {
	Username   string  `json:"username"`
	Email      string  `json:"email"`
	Password   string  `json:"password"`
	FullName   *string `json:"full_name"`
	Phone      *string `json:"phone"`
	Department *string `json:"department"`
}

type adminUserUpdatePayload struct {
	Email      *string `json:"email"`
	FullName   *string `json:"full_name"`
	Phone      *string `json:"phone"`
	Department *string `json:"department"`
	Avatar     *string `json:"avatar"`
	IsActive   *bool   `json:"is_active"`
}

type adminUserResetPasswordPayload struct {
	NewPassword string `json:"new_password"`
}

type adminUserBatchStatusPayload struct {
	UserIDs  []int `json:"user_ids"`
	IsActive bool  `json:"is_active"`
}

type adminUserBatchDeletePayload struct {
	UserIDs    []int `json:"user_ids"`
	SoftDelete *bool `json:"soft_delete"`
}

type adminUserBatchResetPasswordPayload struct {
	UserIDs     []int  `json:"user_ids"`
	NewPassword string `json:"new_password"`
}

func isAdminUsersNativeMutationRoute(r *http.Request) bool {
	switch r.Method {
	case http.MethodPut, http.MethodDelete:
		_, ok := parseAdminUserIDPath(r.URL.Path)
		return ok
	case http.MethodPost:
		if r.URL.Path == "/api/v1/admin/users" || r.URL.Path == "/api/v1/admin/users/batch-status" || r.URL.Path == "/api/v1/admin/users/batch-delete" || r.URL.Path == "/api/v1/admin/users/batch-reset-password" {
			return true
		}
		if _, ok := parseAdminUserActionPath(r.URL.Path, "reset-password"); ok {
			return true
		}
		_, ok := parseAdminUserActionPath(r.URL.Path, "toggle-status")
		return ok
	default:
		return false
	}
}

func buildAdminUserCreateRequest(w http.ResponseWriter, r *http.Request, payload adminUserCreatePayload) (adminstore.UserCreateRequest, bool) {
	username := strings.TrimSpace(payload.Username)
	email := strings.ToLower(strings.TrimSpace(payload.Email))
	if len(username) < 3 || len(username) > 50 || !isValidAdminUsername(username) {
		WriteJSON(w, http.StatusBadRequest, "用户名只能包含字母、数字、下划线和连字符", map[string]string{"error_code": "INVALID_BODY"})
		return adminstore.UserCreateRequest{}, false
	}
	if !isValidAdminUserEmail(email) {
		WriteJSON(w, http.StatusBadRequest, "请输入有效的邮箱地址", map[string]string{"error_code": "INVALID_BODY"})
		return adminstore.UserCreateRequest{}, false
	}
	if !isValidAdminUserPassword(payload.Password) {
		WriteJSON(w, http.StatusBadRequest, "密码必须包含字母和数字，长度 8-100 位", map[string]string{"error_code": "INVALID_BODY"})
		return adminstore.UserCreateRequest{}, false
	}
	return adminstore.UserCreateRequest{
		Username:   username,
		Email:      email,
		Password:   payload.Password,
		FullName:   payload.FullName,
		Phone:      payload.Phone,
		Department: payload.Department,
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
		TraceID:    optionalStringHeader(r, traceHeader),
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
	}, true
}

func buildAdminUserUpdateRequest(w http.ResponseWriter, r *http.Request, userID int, payload adminUserUpdatePayload) (adminstore.UserUpdateRequest, bool) {
	if payload.Email != nil {
		email := strings.ToLower(strings.TrimSpace(*payload.Email))
		if !isValidAdminUserEmail(email) {
			WriteJSON(w, http.StatusBadRequest, "请输入有效的邮箱地址", map[string]string{"error_code": "INVALID_BODY"})
			return adminstore.UserUpdateRequest{}, false
		}
		payload.Email = &email
	}
	return adminstore.UserUpdateRequest{
		UserID:     userID,
		Email:      payload.Email,
		FullName:   payload.FullName,
		Phone:      payload.Phone,
		Department: payload.Department,
		Avatar:     payload.Avatar,
		IsActive:   payload.IsActive,
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
		TraceID:    optionalStringHeader(r, traceHeader),
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
	}, true
}

func isValidAdminUserEmail(email string) bool {
	at := strings.Index(email, "@")
	return at > 0 && at < len(email)-1 && strings.Contains(email[at+1:], ".")
}

func isValidAdminUsername(username string) bool {
	if username == "" {
		return false
	}
	for _, char := range username {
		if (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z') || (char >= '0' && char <= '9') || char == '_' || char == '-' {
			continue
		}
		return false
	}
	return true
}

func isValidAdminUserPassword(password string) bool {
	if len(password) < 8 || len(password) > 100 {
		return false
	}
	hasLetter := false
	hasDigit := false
	for _, char := range password {
		if char >= '0' && char <= '9' {
			hasDigit = true
		}
		if (char >= 'a' && char <= 'z') || (char >= 'A' && char <= 'Z') {
			hasLetter = true
		}
	}
	return hasLetter && hasDigit
}

func writeAdminUserMutationResult(w http.ResponseWriter, logger *slog.Logger, err error, failureMessage string, successMessage string, user adminstore.UserItem) {
	if errors.Is(err, adminstore.ErrUserNotFound) {
		WriteJSON(w, http.StatusNotFound, "用户不存在", map[string]string{"error_code": "NOT_FOUND"})
		return
	}
	if errors.Is(err, adminstore.ErrUserConflict) {
		WriteJSON(w, http.StatusBadRequest, "邮箱已存在", map[string]string{"error_code": "USER_CONFLICT"})
		return
	}
	if errors.Is(err, adminstore.ErrUserSelfOperation) {
		WriteJSON(w, http.StatusBadRequest, "不能停用当前登录账号", map[string]string{"error_code": "SELF_OPERATION_FORBIDDEN"})
		return
	}
	if err != nil {
		logger.Error("admin user mutation failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, failureMessage, map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return
	}
	WriteJSON(w, http.StatusOK, successMessage, user)
}

func writeAdminUserDeleteResult(w http.ResponseWriter, logger *slog.Logger, err error) {
	if errors.Is(err, adminstore.ErrUserNotFound) {
		WriteJSON(w, http.StatusNotFound, "用户不存在", map[string]string{"error_code": "NOT_FOUND"})
		return
	}
	if errors.Is(err, adminstore.ErrUserSelfOperation) {
		WriteJSON(w, http.StatusBadRequest, "不能删除当前登录账号", map[string]string{"error_code": "SELF_OPERATION_FORBIDDEN"})
		return
	}
	if err != nil {
		logger.Error("delete admin user failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "删除用户失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return
	}
	WriteJSON(w, http.StatusOK, "删除成功", nil)
}

func writeAdminUserPasswordResult(w http.ResponseWriter, logger *slog.Logger, err error, failureMessage string, successMessage string) {
	if errors.Is(err, adminstore.ErrUserNotFound) {
		WriteJSON(w, http.StatusNotFound, "用户不存在", map[string]string{"error_code": "NOT_FOUND"})
		return
	}
	if err != nil {
		logger.Error("admin user password mutation failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, failureMessage, map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return
	}
	WriteJSON(w, http.StatusOK, successMessage, nil)
}

func parseAdminUserIDPath(path string) (int, bool) {
	rest := strings.TrimPrefix(path, "/api/v1/admin/users/")
	if rest == "" || strings.Contains(rest, "/") {
		return 0, false
	}
	userID, err := strconv.Atoi(rest)
	if err != nil || userID <= 0 {
		return 0, false
	}
	return userID, true
}

func parseAdminUserActionPath(path string, action string) (int, bool) {
	rest := strings.TrimPrefix(path, "/api/v1/admin/users/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 || parts[1] != action {
		return 0, false
	}
	userID, err := strconv.Atoi(parts[0])
	if err != nil || userID <= 0 {
		return 0, false
	}
	return userID, true
}

func buildAdminProfileHandler(
	cfg config.Config,
	logger *slog.Logger,
	profileStore adminProfileStore,
	statsStore adminProfileStatsStore,
) http.HandlerFunc {
	readHandler := buildAdminProfileReadNativeHandler(logger, profileStore, cfg.AdminSecretKey)
	statsHandler := buildAdminProfileStatsNativeHandler(logger, statsStore, cfg.AdminSecretKey)
	mutationHandler := buildAdminProfileMutationNativeHandler(logger, profileStore, cfg.AdminSecretKey)
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/v1/admin/profile" || r.URL.Path == "/api/v1/admin/profile/" {
			if r.Method == http.MethodGet {
				readHandler(w, r)
				return
			}
			if r.Method != http.MethodPut {
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
				return
			}
			mutationHandler(w, r)
			return
		}
		if r.URL.Path == "/api/v1/admin/profile/stats" {
			if r.Method == http.MethodGet {
				statsHandler(w, r)
				return
			}
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if strings.HasPrefix(r.URL.Path, "/api/v1/admin/profile/") {
			switch r.Method {
			case http.MethodGet, http.MethodPost, http.MethodPut, http.MethodDelete:
				if r.Method == http.MethodPut && r.URL.Path == "/api/v1/admin/profile/password" {
					mutationHandler(w, r)
					return
				}
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			default:
				WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
				return
			}
		}
		WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
	})
}

type adminProfileStore interface {
	GetActiveUserBySubject(ctx context.Context, subject string) (adminstore.UserItem, error)
	UpdateProfileBySubject(ctx context.Context, req adminstore.ProfileUpdateRequest) (adminstore.UserItem, error)
	ChangeProfilePasswordBySubject(ctx context.Context, req adminstore.ProfilePasswordChangeRequest) error
}

type adminProfileStatsStore interface {
	GetProfileStatsBySubject(ctx context.Context, subject string) (adminstore.ProfileStats, error)
}

type adminProfileUpdatePayload struct {
	Email             *string `json:"email"`
	FullName          *string `json:"full_name"`
	Phone             *string `json:"phone"`
	Avatar            *string `json:"avatar"`
	PreferredHomePath *string `json:"preferred_home_path"`
}

type adminProfilePasswordPayload struct {
	OldPassword string `json:"old_password"`
	NewPassword string `json:"new_password"`
}

func buildAdminProfileMutationNativeHandler(logger *slog.Logger, profileStore adminProfileStore, adminSecret string) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if profileStore == nil {
			logger.Error("admin profile store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "个人资料服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		subject, ok := adminJWTSubjectFromRequest(w, r, adminSecret)
		if !ok {
			return
		}
		switch {
		case r.Method == http.MethodPut && (r.URL.Path == "/api/v1/admin/profile" || r.URL.Path == "/api/v1/admin/profile/"):
			var payload adminProfileUpdatePayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			if payload.Email != nil {
				email := strings.ToLower(strings.TrimSpace(*payload.Email))
				if !isValidAdminUserEmail(email) {
					WriteJSON(w, http.StatusBadRequest, "请输入有效的邮箱地址", map[string]string{"error_code": "INVALID_BODY"})
					return
				}
				payload.Email = &email
			}
			if payload.PreferredHomePath != nil {
				preferredHomePath := strings.TrimSpace(*payload.PreferredHomePath)
				if preferredHomePath != "/admin/dashboard" && preferredHomePath != "/workspace" {
					WriteJSON(w, http.StatusBadRequest, "默认首页配置无效", map[string]string{"error_code": "INVALID_BODY"})
					return
				}
				payload.PreferredHomePath = &preferredHomePath
			}
			user, err := profileStore.UpdateProfileBySubject(r.Context(), adminstore.ProfileUpdateRequest{
				Subject:           subject,
				Email:             payload.Email,
				FullName:          payload.FullName,
				Phone:             payload.Phone,
				Avatar:            payload.Avatar,
				PreferredHomePath: payload.PreferredHomePath,
				OperatorID:        optionalIntHeader(r, "x-auth-user-id"),
				TenantID:          optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:           optionalStringHeader(r, traceHeader),
				IPAddress:         optionalString(firstRemoteAddr(r)),
				UserAgent:         optionalString(r.UserAgent()),
			})
			writeAdminProfileMutationResult(w, logger, err, "个人信息更新成功", user)
		case r.Method == http.MethodPut && r.URL.Path == "/api/v1/admin/profile/password":
			var payload adminProfilePasswordPayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			if !isValidAdminUserPassword(payload.NewPassword) {
				WriteJSON(w, http.StatusBadRequest, "密码必须包含字母和数字，长度 8-100 位", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			err := profileStore.ChangeProfilePasswordBySubject(r.Context(), adminstore.ProfilePasswordChangeRequest{
				Subject:     subject,
				OldPassword: payload.OldPassword,
				NewPassword: payload.NewPassword,
				OperatorID:  optionalIntHeader(r, "x-auth-user-id"),
				TenantID:    optionalStringHeader(r, "x-scope-tenant-id"),
				TraceID:     optionalStringHeader(r, traceHeader),
				IPAddress:   optionalString(firstRemoteAddr(r)),
				UserAgent:   optionalString(r.UserAgent()),
			})
			writeAdminProfilePasswordResult(w, logger, err)
		default:
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	})
}

func writeAdminProfileMutationResult(w http.ResponseWriter, logger *slog.Logger, err error, successMessage string, user adminstore.UserItem) {
	if errors.Is(err, authz.ErrUnauthorized) {
		WriteJSON(w, http.StatusUnauthorized, "认证失败", map[string]string{"error_code": "UNAUTHORIZED"})
		return
	}
	if errors.Is(err, adminstore.ErrUserConflict) {
		WriteJSON(w, http.StatusBadRequest, "邮箱已存在", map[string]string{"error_code": "USER_CONFLICT"})
		return
	}
	if err != nil {
		logger.Error("profile mutation failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "更新个人信息失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return
	}
	WriteJSON(w, http.StatusOK, successMessage, user)
}

func writeAdminProfilePasswordResult(w http.ResponseWriter, logger *slog.Logger, err error) {
	if errors.Is(err, authz.ErrUnauthorized) {
		WriteJSON(w, http.StatusUnauthorized, "认证失败", map[string]string{"error_code": "UNAUTHORIZED"})
		return
	}
	if errors.Is(err, adminstore.ErrUserPasswordMismatch) {
		WriteJSON(w, http.StatusUnauthorized, "旧密码错误", map[string]string{"error_code": "INVALID_PASSWORD"})
		return
	}
	if err != nil {
		logger.Error("profile password change failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "修改密码失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return
	}
	WriteJSON(w, http.StatusOK, "密码修改成功，请重新登录", nil)
}

func buildAdminProfileReadNativeHandler(logger *slog.Logger, profileStore adminProfileStore, adminSecret string) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if profileStore == nil {
			logger.Error("admin profile store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "用户数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		subject, ok := adminJWTSubjectFromRequest(w, r, adminSecret)
		if !ok {
			return
		}
		user, err := profileStore.GetActiveUserBySubject(r.Context(), subject)
		if err != nil {
			if errors.Is(err, authz.ErrUnauthorized) {
				w.Header().Set("WWW-Authenticate", "Bearer")
				WriteJSON(w, http.StatusUnauthorized, "Token 已过期或无效", map[string]string{"error_code": "INVALID_TOKEN"})
				return
			}
			logger.Error("get profile failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取个人信息失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取个人信息成功", user)
	})
}

func buildAdminProfileStatsNativeHandler(logger *slog.Logger, statsStore adminProfileStatsStore, adminSecret string) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if statsStore == nil {
			logger.Error("admin profile stats store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "用户数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		subject, ok := adminJWTSubjectFromRequest(w, r, adminSecret)
		if !ok {
			return
		}
		stats, err := statsStore.GetProfileStatsBySubject(r.Context(), subject)
		if err != nil {
			if errors.Is(err, authz.ErrUnauthorized) {
				w.Header().Set("WWW-Authenticate", "Bearer")
				WriteJSON(w, http.StatusUnauthorized, "Token 已过期或无效", map[string]string{"error_code": "INVALID_TOKEN"})
				return
			}
			logger.Error("get profile stats failed", "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取统计信息失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取统计信息成功", stats)
	})
}

func adminJWTSubjectFromRequest(w http.ResponseWriter, r *http.Request, adminSecret string) (string, bool) {
	token, hasToken := extractBearerToken(r.Header.Get("Authorization"))
	if !hasToken {
		w.Header().Set("WWW-Authenticate", "Bearer")
		WriteJSON(w, http.StatusUnauthorized, "缺少认证凭证", map[string]string{"error_code": "UNAUTHORIZED"})
		return "", false
	}
	claims, err := newAdminJWTVerifier(adminSecret).verify(token)
	if err != nil {
		w.Header().Set("WWW-Authenticate", "Bearer")
		WriteJSON(w, http.StatusUnauthorized, "Token 已过期或无效", map[string]string{"error_code": "INVALID_TOKEN"})
		return "", false
	}
	return claims.Subject, true
}

func writeGraphError(w http.ResponseWriter, status int, payload map[string]interface{}) {
	message := "请求失败"
	if v, ok := payload["message"].(string); ok && v != "" {
		message = v
	} else if v, ok := payload["error"].(string); ok && v != "" {
		message = v
	}

	data := make(map[string]interface{}, len(payload))
	for k, v := range payload {
		if k == "code" {
			data["error_code"] = v
			continue
		}
		data[k] = v
	}
	WriteJSON(w, status, message, data)
}
