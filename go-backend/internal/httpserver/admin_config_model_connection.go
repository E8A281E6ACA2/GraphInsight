package httpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/neo4j/neo4j-go-driver/v5/neo4j"
	"graphinsight/go-backend/internal/config"
)

type adminModelConnectionSnapshotStore struct {
	mu      sync.RWMutex
	payload interface{}
}

func (s *adminModelConnectionSnapshotStore) Get() interface{} {
	if s == nil {
		return nil
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.payload
}

func (s *adminModelConnectionSnapshotStore) Set(payload interface{}) {
	if s == nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.payload = payload
}

func buildAdminModelConnectionLatestNativeHandler(guard businessPermissionGuard, snapshots *adminModelConnectionSnapshotStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", snapshots.Get())
	}))
}

type adminConfigConnectionGraphService interface {
	CheckHealth(ctx context.Context) error
}

type adminNeo4jConnectionTestRequest struct {
	URI      *string `json:"uri"`
	User     *string `json:"user"`
	Username *string `json:"username"`
	Password *string `json:"password"`
	Database *string `json:"database"`
}

func hasAdminNeo4jConnectionTestPayload(payload adminNeo4jConnectionTestRequest) bool {
	return payload.URI != nil ||
		payload.User != nil ||
		payload.Username != nil ||
		payload.Password != nil ||
		payload.Database != nil
}

func trimNeo4jPayloadString(value *string) string {
	if value == nil {
		return ""
	}
	return strings.TrimSpace(*value)
}

func neo4jConnectionFailureMessage(uri string, err error) string {
	if err == nil {
		return ""
	}
	message := err.Error()
	host := neo4jURIHost(uri)
	if host != "" && (strings.Contains(message, "no such host") || strings.Contains(message, "lookup ")) {
		return fmt.Sprintf("Neo4j 地址无法解析: %s。当前还没有验证到用户名或密码，请检查域名、网络/DNS，或确认 Aura 实例仍在运行。配置未保存。", host)
	}
	if strings.Contains(message, "authentication") || strings.Contains(message, "unauthorized") || strings.Contains(message, "The client is unauthorized") {
		return "Neo4j 认证失败：用户名或密码不正确。配置未保存。"
	}
	return fmt.Sprintf("Neo4j 连接失败: %s", message)
}

func neo4jURIHost(rawURI string) string {
	parsed, err := url.Parse(strings.TrimSpace(rawURI))
	if err != nil {
		return ""
	}
	return strings.TrimSpace(parsed.Hostname())
}

func preflightNeo4jHost(ctx context.Context, uri string) error {
	host := neo4jURIHost(uri)
	if host == "" || net.ParseIP(host) != nil {
		return nil
	}
	if _, err := net.DefaultResolver.LookupHost(ctx, host); err != nil {
		return err
	}
	return nil
}

var adminNeo4jConnectionProbe = func(ctx context.Context, uri string, user string, password string, database string) error {
	if err := preflightNeo4jHost(ctx, uri); err != nil {
		return err
	}
	driver, driverErr := neo4j.NewDriverWithContext(
		uri,
		neo4j.BasicAuth(user, password, ""),
		func(c *neo4j.Config) {
			c.MaxConnectionPoolSize = 2
			c.SocketConnectTimeout = 3 * time.Second
		},
	)
	if driverErr != nil {
		return driverErr
	}
	defer driver.Close(ctx)

	if verifyErr := driver.VerifyConnectivity(ctx); verifyErr != nil {
		return verifyErr
	}

	session := driver.NewSession(ctx, neo4j.SessionConfig{
		DatabaseName: firstNonEmptyString(database, "neo4j"),
	})
	defer session.Close(ctx)
	_, runErr := session.Run(ctx, "RETURN 1 AS ok", nil)
	return runErr
}

func buildAdminConfigConnectionTestHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
	graphSvc adminConfigConnectionGraphService,
	graphInitErr error,
	snapshots *adminModelConnectionSnapshotStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/v1/admin/config/test/openai":
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		case r.URL.Path == "/api/v1/admin/config/test/model" && r.Method == http.MethodPost:
			buildAdminModelConnectionTestNativeHandler(cfg, logger, guard, configStore, snapshots)(w, r)
			return
		case r.URL.Path == "/api/v1/admin/config/test/embedding" && r.Method == http.MethodPost:
			buildAdminEmbeddingConnectionTestNativeHandler(cfg, logger, guard, configStore)(w, r)
			return
		case (r.URL.Path == "/api/v1/admin/config/test/vector_store" || r.URL.Path == "/api/v1/admin/config/test/vector-store") && r.Method == http.MethodPost:
			buildAdminVectorStoreConnectionTestNativeHandler(logger, guard, configStore)(w, r)
			return
		case (r.URL.Path == "/api/v1/admin/config/test/document_parser" || r.URL.Path == "/api/v1/admin/config/test/document-parser") && r.Method == http.MethodPost:
			buildAdminDocumentParserConnectionTestNativeHandler(logger, guard, configStore)(w, r)
			return
		case r.URL.Path == "/api/v1/admin/config/test/neo4j" && r.Method == http.MethodPost:
			buildAdminNeo4jConnectionTestNativeHandler(cfg, logger, guard, configStore, graphSvc, graphInitErr)(w, r)
			return
		case r.URL.Path == "/api/v1/admin/config/test/ai_service" && r.Method == http.MethodPost:
			buildAdminAIServiceConnectionTestNativeHandler(cfg, logger, guard, configStore)(w, r)
			return
		case strings.HasPrefix(r.URL.Path, "/api/v1/admin/config/test/") && r.Method == http.MethodPost:
			buildAdminUnsupportedConnectionTestNativeHandler(guard)(w, r)
			return
		case strings.HasPrefix(r.URL.Path, "/api/v1/admin/config/test/"):
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		default:
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	})
}

func buildAdminNeo4jConnectionTestNativeHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
	graphSvc adminConfigConnectionGraphService,
	graphInitErr error,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		uri := strings.TrimSpace(cfg.Neo4jURI)
		user := strings.TrimSpace(cfg.Neo4jUser)
		password := strings.TrimSpace(cfg.Neo4jPassword)
		database := strings.TrimSpace(cfg.Neo4jDatabase)

		var payload adminNeo4jConnectionTestRequest
		if r.Body != nil {
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil && err != io.EOF {
				WriteJSON(w, http.StatusBadRequest, "请求体格式错误", map[string]string{"error_code": "INVALID_JSON"})
				return
			}
		}

		if configStore != nil {
			values, err := safeConfigValues(r.Context(), configStore, "neo4j")
			if err != nil {
				logger.Warn("resolve neo4j config values for test failed", "error", err.Error())
			} else {
				snapshot := buildAdminNeo4jConfigSnapshot(cfg, values)
				if value, ok := snapshot["uri"].(string); ok && value != "" {
					uri = value
				}
				if value, ok := snapshot["user"].(string); ok && value != "" {
					user = value
				}
				if value, ok := values["password"]; ok && strings.TrimSpace(value) != "" {
					password = strings.TrimSpace(value)
				}
				if value, ok := snapshot["database"].(string); ok && value != "" {
					database = value
				}
			}
		}

		hasPayload := hasAdminNeo4jConnectionTestPayload(payload)
		if payload.URI != nil {
			uri = trimNeo4jPayloadString(payload.URI)
		}
		if payload.User != nil {
			user = trimNeo4jPayloadString(payload.User)
		} else if payload.Username != nil {
			user = trimNeo4jPayloadString(payload.Username)
		}
		if payload.Password != nil {
			password = trimNeo4jPayloadString(payload.Password)
		}
		if payload.Database != nil {
			database = trimNeo4jPayloadString(payload.Database)
		}

		probeCtx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()

		var err error
		switch {
		case strings.TrimSpace(uri) == "":
			err = fmt.Errorf("neo4j uri is empty")
		case strings.TrimSpace(user) == "":
			err = fmt.Errorf("neo4j user is empty")
		case strings.TrimSpace(password) == "":
			err = fmt.Errorf("neo4j password is empty")
		default:
			err = adminNeo4jConnectionProbe(probeCtx, uri, user, password, database)
		}

		if err != nil {
			WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
				"success": false,
				"message": neo4jConnectionFailureMessage(uri, err),
				"saved":   false,
			})
			return
		}

		saved := false
		if hasPayload && configStore != nil {
			for _, item := range []struct {
				key        string
				value      string
				shouldSave bool
			}{
				{key: "uri", value: uri, shouldSave: payload.URI != nil},
				{key: "user", value: user, shouldSave: payload.User != nil || payload.Username != nil},
				{key: "password", value: password, shouldSave: payload.Password != nil && strings.TrimSpace(password) != ""},
				{key: "database", value: firstNonEmptyString(database, "neo4j"), shouldSave: payload.Database != nil},
			} {
				if !item.shouldSave || strings.TrimSpace(item.value) == "" {
					continue
				}
				_, updateErr := configStore.UpdateConfig(r.Context(), buildConfigMutationRequest(
					r,
					"neo4j",
					item.key,
					item.value,
					nil,
					boolPtr(isSensitiveConfigKey(item.key)),
				))
				if updateErr != nil {
					logger.Error("save verified neo4j config failed", "key", item.key, "error", updateErr.Error())
					WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
						"success": false,
						"message": "Neo4j 连接成功，但保存配置失败",
						"saved":   false,
					})
					return
				}
			}
			saved = true
		}

		message := fmt.Sprintf("Neo4j 连接成功 (%s)", uri)
		if saved {
			message = fmt.Sprintf("Neo4j 连接成功，配置已保存 (%s)", uri)
		}
		WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
			"success": true,
			"message": message,
			"saved":   saved,
		})
	}))
}

func buildAdminAIServiceConnectionTestNativeHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		values := map[string]string{}
		if configStore != nil {
			resolved, err := safeConfigValues(r.Context(), configStore, "ai_service")
			if err != nil {
				logger.Warn("resolve ai service config values for test failed", "error", err.Error())
			} else {
				values = resolved
			}
		}
		snapshot := buildAdminAIServiceConfigSnapshot(cfg, values)
		provider, _ := snapshot["provider"].(string)
		enabled, _ := snapshot["enabled"].(bool)
		apiKeyConfigured, _ := snapshot["api_key_configured"].(bool)
		baseURL, _ := snapshot["base_url"].(string)

		if !enabled {
			WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
				"success": false,
				"message": "AI服务未启用",
			})
			return
		}
		if !apiKeyConfigured {
			WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
				"success": false,
				"message": fmt.Sprintf("%s API Key 未配置", strings.ToUpper(provider)),
			})
			return
		}

		switch provider {
		case "openai":
			WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
				"success": true,
				"message": "OpenAI API 配置完整；请使用“测试当前模型”验证实际鉴权与连通性",
			})
			return
		case "claude":
			WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
				"success": true,
				"message": "Claude API 配置完整；请使用“测试当前模型”验证实际鉴权与连通性",
			})
			return
		case "openai_compatible":
			if strings.TrimSpace(baseURL) == "" {
				WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
					"success": false,
					"message": "OpenAI 兼容接口需要配置 API 地址",
				})
				return
			}
			WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
				"success": true,
				"message": fmt.Sprintf("OpenAI 兼容接口已配置 (API: %s)", baseURL),
			})
			return
		default:
			WriteJSON(w, http.StatusOK, "测试完成", map[string]interface{}{
				"success": true,
				"message": fmt.Sprintf("%s API Key 已配置", strings.ToUpper(provider)),
			})
			return
		}
	}))
}

func buildAdminEmbeddingConnectionTestNativeHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		aiValues := map[string]string{}
		embeddingValues := map[string]string{}
		if configStore != nil {
			if resolved, err := safeConfigValues(r.Context(), configStore, "ai_service"); err != nil {
				logger.Warn("resolve ai service config values for embedding test failed", "error", err.Error())
			} else {
				aiValues = resolved
			}
			if resolved, err := safeConfigValues(r.Context(), configStore, "embedding"); err != nil {
				logger.Warn("resolve embedding config values for test failed", "error", err.Error())
			} else {
				embeddingValues = resolved
			}
		}
		aiSnapshot := buildAdminAIServiceConfigSnapshot(cfg, aiValues)
		aiProvider, _ := aiSnapshot["provider"].(string)
		aiBaseURL, _ := aiSnapshot["base_url"].(string)

		provider := firstNonEmptyString(embeddingValues["provider"], os.Getenv("EMBEDDING_PROVIDER"), aiProvider, os.Getenv("AI_SERVICE_PROVIDER"), "openai")
		enabled := configBool(embeddingValues, "enabled", envBool("EMBEDDING_ENABLED", true))
		baseURL := firstNonEmptyString(embeddingValues["base_url"], os.Getenv("EMBEDDING_BASE_URL"), aiBaseURL, os.Getenv("AI_SERVICE_BASE_URL"), os.Getenv("OPENAI_BASE_URL"))
		apiKey := firstNonEmptyString(embeddingValues["api_key"], os.Getenv("EMBEDDING_API_KEY"), aiValues["api_key"], cfg.AIAPIKey)
		model := firstNonEmptyString(embeddingValues["model"], os.Getenv("EMBEDDING_MODEL"), "text-embedding-3-small")
		endpoint := buildEmbeddingURL(baseURL)
		checkedAt := time.Now().UTC().Format(time.RFC3339)
		startedAt := time.Now()
		checks := make([]map[string]interface{}, 0, 4)
		finish := func(result map[string]interface{}) {
			result["checked_at"] = checkedAt
			result["latency_ms"] = float64(time.Since(startedAt).Microseconds()) / 1000
			result["checks"] = checks
			WriteJSON(w, http.StatusOK, "测试完成", result)
		}

		if !enabled {
			checks = append(checks, map[string]interface{}{"name": "enabled", "success": false, "message": "Embedding 未启用"})
			finish(map[string]interface{}{
				"success":  false,
				"message":  "Embedding 未启用",
				"provider": provider,
				"model":    model,
				"base_url": defaultModelBaseURL(provider, baseURL),
				"endpoint": endpoint,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "enabled", "success": true, "message": "Embedding 已启用"})

		if strings.TrimSpace(apiKey) == "" || strings.TrimSpace(apiKey) == "your-api-key-here" {
			checks = append(checks, map[string]interface{}{"name": "api_key", "success": false, "message": "Embedding API Key 未配置"})
			finish(map[string]interface{}{
				"success":  false,
				"message":  "Embedding API Key 未配置",
				"provider": provider,
				"model":    model,
				"base_url": defaultModelBaseURL(provider, baseURL),
				"endpoint": endpoint,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "api_key", "success": true, "message": "Embedding API Key 已配置"})

		if strings.TrimSpace(model) == "" {
			checks = append(checks, map[string]interface{}{"name": "model", "success": false, "message": "Embedding 模型未配置"})
			finish(map[string]interface{}{
				"success":  false,
				"message":  "Embedding 模型未配置",
				"provider": provider,
				"model":    model,
				"base_url": defaultModelBaseURL(provider, baseURL),
				"endpoint": endpoint,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "model", "success": true, "message": fmt.Sprintf("当前嵌入模型: %s", model)})

		payload := map[string]interface{}{
			"model": model,
			"input": []string{"GraphInsight embedding connectivity probe."},
		}
		body, err := json.Marshal(payload)
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "probe", "success": false, "message": err.Error()})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("Embedding 连通性测试失败: %s", err.Error()),
				"provider": provider,
				"model":    model,
				"base_url": defaultModelBaseURL(provider, baseURL),
				"endpoint": endpoint,
			})
			return
		}

		probeStarted := time.Now()
		req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "probe", "success": false, "message": err.Error()})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("Embedding 连通性测试失败: %s", err.Error()),
				"provider": provider,
				"model":    model,
				"base_url": defaultModelBaseURL(provider, baseURL),
				"endpoint": endpoint,
			})
			return
		}
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(apiKey))
		req.Header.Set("Content-Type", "application/json")

		client := &http.Client{Timeout: 20 * time.Second}
		resp, err := client.Do(req)
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "probe", "success": false, "message": err.Error()})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("Embedding 连通性测试失败: %s", err.Error()),
				"provider": provider,
				"model":    model,
				"base_url": defaultModelBaseURL(provider, baseURL),
				"endpoint": endpoint,
			})
			return
		}
		defer resp.Body.Close()

		requestMS := float64(time.Since(probeStarted).Microseconds()) / 1000
		if resp.StatusCode < http.StatusBadRequest {
			checks = append(checks, map[string]interface{}{
				"name":       "probe",
				"success":    true,
				"message":    fmt.Sprintf("Embedding 探测成功，HTTP %d", resp.StatusCode),
				"latency_ms": requestMS,
			})
			finish(map[string]interface{}{
				"success":  true,
				"message":  "Embedding 连通性测试成功",
				"provider": provider,
				"model":    model,
				"base_url": defaultModelBaseURL(provider, baseURL),
				"endpoint": endpoint,
			})
			return
		}
		providerMessage := readProviderErrorMessage(resp.Body)
		failureMessage := fmt.Sprintf("Embedding 探测失败，HTTP %d", resp.StatusCode)
		if providerMessage != "" {
			failureMessage = fmt.Sprintf("%s：%s", failureMessage, providerMessage)
		}
		if resp.StatusCode == http.StatusBadRequest {
			failureMessage += "；请确认所选模型支持 Embeddings API"
		}

		checks = append(checks, map[string]interface{}{
			"name":       "probe",
			"success":    false,
			"message":    failureMessage,
			"latency_ms": requestMS,
		})
		finish(map[string]interface{}{
			"success":  false,
			"message":  failureMessage,
			"provider": provider,
			"model":    model,
			"base_url": defaultModelBaseURL(provider, baseURL),
			"endpoint": endpoint,
		})
	}))
}

func buildAdminVectorStoreConnectionTestNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		values := map[string]string{}
		if configStore != nil {
			resolved, err := safeConfigValues(r.Context(), configStore, "vector_store")
			if err != nil {
				logger.Warn("resolve vector store config values for test failed", "error", err.Error())
			} else {
				values = resolved
			}
		}
		provider := firstNonEmptyString(values["provider"], os.Getenv("VECTOR_STORE_PROVIDER"), "milvus")
		enabled := configBool(values, "enabled", envBool("VECTOR_STORE_ENABLED", false))
		uri := firstNonEmptyString(values["uri"], os.Getenv("MILVUS_URI"), "http://127.0.0.1:19530")
		dbName := firstNonEmptyString(values["db_name"], os.Getenv("MILVUS_DB_NAME"), "default")
		collection := firstNonEmptyString(values["collection"], os.Getenv("MILVUS_COLLECTION"), "graphinsight_chunks")
		checkedAt := time.Now().UTC().Format(time.RFC3339)
		startedAt := time.Now()
		checks := make([]map[string]interface{}, 0, 4)
		finish := func(result map[string]interface{}) {
			result["checked_at"] = checkedAt
			result["latency_ms"] = float64(time.Since(startedAt).Microseconds()) / 1000
			result["checks"] = checks
			WriteJSON(w, http.StatusOK, "测试完成", result)
		}

		if strings.ToLower(strings.TrimSpace(provider)) != "milvus" {
			checks = append(checks, map[string]interface{}{"name": "provider", "success": false, "message": fmt.Sprintf("暂不支持的向量库: %s", provider)})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("暂不支持的向量库: %s", provider),
				"provider": provider,
				"base_url": uri,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "provider", "success": true, "message": "向量库类型: milvus"})

		if enabled {
			checks = append(checks, map[string]interface{}{"name": "enabled", "success": true, "message": "向量检索已启用"})
		} else {
			checks = append(checks, map[string]interface{}{"name": "enabled", "success": true, "message": "向量检索当前禁用；继续测试 Milvus 服务连通性"})
		}

		target := milvusProbeTarget(uri)
		if target == "" {
			checks = append(checks, map[string]interface{}{"name": "uri", "success": false, "message": "Milvus 地址格式不正确"})
			finish(map[string]interface{}{
				"success":  false,
				"message":  "Milvus 地址格式不正确",
				"provider": provider,
				"base_url": uri,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "uri", "success": true, "message": fmt.Sprintf("Milvus 地址: %s", target)})

		probeStarted := time.Now()
		dialer := net.Dialer{Timeout: 3 * time.Second}
		conn, err := dialer.DialContext(r.Context(), "tcp", target)
		requestMS := float64(time.Since(probeStarted).Microseconds()) / 1000
		if err != nil {
			checks = append(checks, map[string]interface{}{
				"name":       "probe",
				"success":    false,
				"message":    err.Error(),
				"latency_ms": requestMS,
			})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("Milvus 连通性测试失败: %s", err.Error()),
				"provider": provider,
				"base_url": uri,
				"endpoint": target,
			})
			return
		}
		_ = conn.Close()
		checks = append(checks, map[string]interface{}{
			"name":       "probe",
			"success":    true,
			"message":    "Milvus TCP 端口可达",
			"latency_ms": requestMS,
		})
		finish(map[string]interface{}{
			"success":    true,
			"message":    fmt.Sprintf("Milvus 连通性测试成功 (%s / %s)", dbName, collection),
			"provider":   provider,
			"base_url":   uri,
			"endpoint":   target,
			"db_name":    dbName,
			"collection": collection,
		})
	}))
}

func readProviderErrorMessage(reader io.Reader) string {
	body, err := io.ReadAll(io.LimitReader(reader, 4096))
	if err != nil || len(body) == 0 {
		return ""
	}
	var payload map[string]interface{}
	if json.Unmarshal(body, &payload) == nil {
		if errorPayload, ok := payload["error"].(map[string]interface{}); ok {
			if message, ok := errorPayload["message"].(string); ok {
				return compactProviderMessage(message)
			}
		}
		for _, key := range []string{"message", "detail", "error"} {
			if message, ok := payload[key].(string); ok {
				return compactProviderMessage(message)
			}
		}
	}
	return compactProviderMessage(string(body))
}

func compactProviderMessage(message string) string {
	message = strings.Join(strings.Fields(message), " ")
	if len(message) > 300 {
		return message[:300] + "..."
	}
	return message
}

func buildAdminDocumentParserConnectionTestNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		values := map[string]string{}
		if configStore != nil {
			resolved, err := safeConfigValues(r.Context(), configStore, "document_parser")
			if err != nil {
				logger.Warn("resolve document parser config values for test failed", "error", err.Error())
			} else {
				values = resolved
			}
		}
		provider := firstNonEmptyString(values["provider"], os.Getenv("DOCUMENT_PARSER_PROVIDER"), "native")
		fallbackProvider := firstNonEmptyString(values["fallback_provider"], os.Getenv("DOCUMENT_PARSER_FALLBACK_PROVIDER"), "native")
		baseURL := strings.TrimRight(firstNonEmptyString(values["base_url"], os.Getenv("MINERU_BASE_URL")), "/")
		endpointPath := firstNonEmptyString(values["endpoint_path"], os.Getenv("MINERU_ENDPOINT_PATH"), "/file_parse")
		fileField := firstNonEmptyString(values["file_field"], os.Getenv("MINERU_FILE_FIELD"), "files")
		parseMode := firstNonEmptyString(values["parse_mode"], os.Getenv("MINERU_PARSE_MODE"), "auto")
		outputFormat := firstNonEmptyString(values["output_format"], os.Getenv("MINERU_OUTPUT_FORMAT"), "markdown,json")
		timeoutSeconds := configInt(values, "timeout_seconds", envInt("MINERU_TIMEOUT_SECONDS", 300))
		checkedAt := time.Now().UTC().Format(time.RFC3339)
		startedAt := time.Now()
		checks := make([]map[string]interface{}, 0, 5)
		finish := func(result map[string]interface{}) {
			result["checked_at"] = checkedAt
			result["latency_ms"] = float64(time.Since(startedAt).Microseconds()) / 1000
			result["checks"] = checks
			WriteJSON(w, http.StatusOK, "测试完成", result)
		}

		normalizedProvider := strings.ToLower(strings.TrimSpace(provider))
		if normalizedProvider == "" {
			normalizedProvider = "native"
		}
		if normalizedProvider == "native" {
			checks = append(checks, map[string]interface{}{"name": "provider", "success": true, "message": "当前使用内置解析器"})
			finish(map[string]interface{}{
				"success":           true,
				"message":           "内置文档解析器可用",
				"provider":          "native",
				"fallback_provider": fallbackProvider,
				"parse_mode":        "builtin",
			})
			return
		}
		if normalizedProvider != "mineru" {
			checks = append(checks, map[string]interface{}{"name": "provider", "success": false, "message": fmt.Sprintf("暂不支持的解析器: %s", provider)})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("暂不支持的解析器: %s", provider),
				"provider": provider,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "provider", "success": true, "message": "解析器类型: mineru"})

		if baseURL == "" {
			checks = append(checks, map[string]interface{}{"name": "base_url", "success": false, "message": "MinerU API 地址未配置"})
			finish(map[string]interface{}{
				"success":           false,
				"message":           "MinerU API 地址未配置",
				"provider":          "mineru",
				"fallback_provider": fallbackProvider,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "base_url", "success": true, "message": baseURL})

		healthURL := baseURL + "/health"
		probeStarted := time.Now()
		req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, healthURL, nil)
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "health", "success": false, "message": err.Error()})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("MinerU 连通性测试失败: %s", err.Error()),
				"provider": "mineru",
				"base_url": baseURL,
				"endpoint": healthURL,
			})
			return
		}
		client := &http.Client{Timeout: 12 * time.Second}
		resp, err := client.Do(req)
		requestMS := float64(time.Since(probeStarted).Microseconds()) / 1000
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "health", "success": false, "message": err.Error(), "latency_ms": requestMS})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("MinerU 连通性测试失败: %s", err.Error()),
				"provider": "mineru",
				"base_url": baseURL,
				"endpoint": healthURL,
			})
			return
		}
		defer resp.Body.Close()

		var payload map[string]interface{}
		_ = json.NewDecoder(io.LimitReader(resp.Body, 4096)).Decode(&payload)
		if resp.StatusCode >= http.StatusBadRequest {
			checks = append(checks, map[string]interface{}{"name": "health", "success": false, "message": fmt.Sprintf("HTTP %d", resp.StatusCode), "latency_ms": requestMS})
			finish(map[string]interface{}{
				"success":  false,
				"message":  fmt.Sprintf("MinerU 健康检查失败，HTTP %d", resp.StatusCode),
				"provider": "mineru",
				"base_url": baseURL,
				"endpoint": healthURL,
			})
			return
		}

		version := ""
		if rawVersion, ok := payload["version"].(string); ok {
			version = rawVersion
		}
		status := ""
		if rawStatus, ok := payload["status"].(string); ok {
			status = rawStatus
		}
		checks = append(checks, map[string]interface{}{"name": "health", "success": true, "message": fmt.Sprintf("MinerU 健康检查成功，HTTP %d", resp.StatusCode), "latency_ms": requestMS})
		checks = append(checks, map[string]interface{}{"name": "protocol", "success": true, "message": fmt.Sprintf("file_field=%s, parse_method=%s", fileField, parseMode)})
		finish(map[string]interface{}{
			"success":           true,
			"message":           "MinerU 连通性测试成功",
			"provider":          "mineru",
			"fallback_provider": fallbackProvider,
			"base_url":          baseURL,
			"endpoint":          baseURL + ensureLeadingSlash(endpointPath),
			"health_endpoint":   healthURL,
			"file_field":        fileField,
			"parse_mode":        parseMode,
			"output_format":     outputFormat,
			"timeout_seconds":   timeoutSeconds,
			"version":           version,
			"status":            status,
		})
	}))
}

func ensureLeadingSlash(value string) string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return "/"
	}
	if strings.HasPrefix(trimmed, "/") {
		return trimmed
	}
	return "/" + trimmed
}

func buildAdminModelConnectionTestNativeHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
	snapshots *adminModelConnectionSnapshotStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		values := map[string]string{}
		if configStore != nil {
			resolved, err := safeConfigValues(r.Context(), configStore, "ai_service")
			if err != nil {
				logger.Warn("resolve ai service config values for model test failed", "error", err.Error())
			} else {
				values = resolved
			}
		}
		snapshot := buildAdminAIServiceConfigSnapshot(cfg, values)
		provider, _ := snapshot["provider"].(string)
		enabled, _ := snapshot["enabled"].(bool)
		baseURL, _ := snapshot["base_url"].(string)
		model, _ := snapshot["model"].(string)
		apiKey := strings.TrimSpace(values["api_key"])
		if apiKey == "" {
			apiKey = strings.TrimSpace(cfg.AIAPIKey)
		}
		reasoningProfile := firstNonEmptyString(values["model_probe_reasoning_profile"], os.Getenv("AI_SERVICE_MODEL_PROBE_REASONING_PROFILE"), "fast")

		checkedAt := time.Now().UTC().Format(time.RFC3339)
		startedAt := time.Now()
		checks := make([]map[string]interface{}, 0, 4)
		finish := func(result map[string]interface{}) {
			result["checked_at"] = checkedAt
			result["latency_ms"] = float64(time.Since(startedAt).Microseconds()) / 1000
			result["checks"] = checks
			snapshots.Set(result)
			WriteJSON(w, http.StatusOK, "测试完成", result)
		}

		if !enabled {
			checks = append(checks, map[string]interface{}{"name": "enabled", "success": false, "message": "AI 服务未启用"})
			finish(map[string]interface{}{
				"success":           false,
				"message":           "AI 服务未启用",
				"provider":          provider,
				"model":             model,
				"reasoning_profile": reasoningProfile,
				"base_url":          baseURL,
				"endpoint":          nil,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "enabled", "success": true, "message": "AI 服务已启用"})

		if apiKey == "" || apiKey == "your-api-key-here" {
			checks = append(checks, map[string]interface{}{"name": "api_key", "success": false, "message": "API Key 未配置"})
			finish(map[string]interface{}{
				"success":           false,
				"message":           "API Key 未配置",
				"provider":          provider,
				"model":             model,
				"reasoning_profile": reasoningProfile,
				"base_url":          baseURL,
				"endpoint":          nil,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "api_key", "success": true, "message": "API Key 已配置"})

		if strings.TrimSpace(model) == "" {
			checks = append(checks, map[string]interface{}{"name": "model", "success": false, "message": "模型未配置"})
			finish(map[string]interface{}{
				"success":           false,
				"message":           "模型未配置",
				"provider":          provider,
				"model":             model,
				"reasoning_profile": reasoningProfile,
				"base_url":          baseURL,
				"endpoint":          nil,
			})
			return
		}
		checks = append(checks, map[string]interface{}{"name": "model", "success": true, "message": fmt.Sprintf("当前模型: %s", model)})

		endpoint := buildChatCompletionURL(baseURL)
		headers := map[string]string{
			"Authorization": fmt.Sprintf("Bearer %s", apiKey),
			"Content-Type":  "application/json",
		}
		payload := map[string]interface{}{
			"model": model,
			"messages": []map[string]string{
				{"role": "user", "content": "Reply with ok."},
			},
		}
		if provider == "claude" {
			endpoint = buildClaudeMessagesURL(baseURL)
			headers = map[string]string{
				"x-api-key":         apiKey,
				"anthropic-version": "2023-06-01",
				"Content-Type":      "application/json",
			}
			payload = map[string]interface{}{
				"model":      model,
				"max_tokens": 8,
				"messages": []map[string]string{
					{"role": "user", "content": "Reply with ok."},
				},
			}
		}

		body, err := json.Marshal(payload)
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "probe", "success": false, "message": err.Error()})
			finish(map[string]interface{}{
				"success":           false,
				"message":           fmt.Sprintf("模型连通性测试失败: %s", err.Error()),
				"provider":          provider,
				"model":             model,
				"reasoning_profile": reasoningProfile,
				"base_url":          defaultModelBaseURL(provider, baseURL),
				"endpoint":          endpoint,
			})
			return
		}

		probeStarted := time.Now()
		req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, endpoint, bytes.NewReader(body))
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "probe", "success": false, "message": err.Error()})
			finish(map[string]interface{}{
				"success":           false,
				"message":           fmt.Sprintf("模型连通性测试失败: %s", err.Error()),
				"provider":          provider,
				"model":             model,
				"reasoning_profile": reasoningProfile,
				"base_url":          defaultModelBaseURL(provider, baseURL),
				"endpoint":          endpoint,
			})
			return
		}
		for key, value := range headers {
			req.Header.Set(key, value)
		}

		client := &http.Client{Timeout: 20 * time.Second}
		resp, err := client.Do(req)
		if err != nil {
			checks = append(checks, map[string]interface{}{"name": "probe", "success": false, "message": err.Error()})
			finish(map[string]interface{}{
				"success":           false,
				"message":           fmt.Sprintf("模型连通性测试失败: %s", err.Error()),
				"provider":          provider,
				"model":             model,
				"reasoning_profile": reasoningProfile,
				"base_url":          defaultModelBaseURL(provider, baseURL),
				"endpoint":          endpoint,
			})
			return
		}
		defer resp.Body.Close()

		requestMS := float64(time.Since(probeStarted).Microseconds()) / 1000
		if resp.StatusCode < http.StatusBadRequest {
			checks = append(checks, map[string]interface{}{
				"name":       "probe",
				"success":    true,
				"message":    fmt.Sprintf("模型探测成功，HTTP %d", resp.StatusCode),
				"latency_ms": requestMS,
			})
			finish(map[string]interface{}{
				"success":           true,
				"message":           "模型连通性测试成功",
				"provider":          provider,
				"model":             model,
				"reasoning_profile": reasoningProfile,
				"base_url":          defaultModelBaseURL(provider, baseURL),
				"endpoint":          endpoint,
			})
			return
		}
		providerMessage := readProviderErrorMessage(resp.Body)
		failureMessage := fmt.Sprintf("模型探测失败，HTTP %d", resp.StatusCode)
		if providerMessage != "" {
			failureMessage = fmt.Sprintf("%s：%s", failureMessage, providerMessage)
		}
		if resp.StatusCode == http.StatusBadRequest {
			failureMessage += "；请核对模型名称及该网关的 Chat Completions 兼容性"
		}

		checks = append(checks, map[string]interface{}{
			"name":       "probe",
			"success":    false,
			"message":    failureMessage,
			"latency_ms": requestMS,
		})
		finish(map[string]interface{}{
			"success":           false,
			"message":           failureMessage,
			"provider":          provider,
			"model":             model,
			"reasoning_profile": reasoningProfile,
			"base_url":          defaultModelBaseURL(provider, baseURL),
			"endpoint":          endpoint,
		})
	}))
}

func buildAdminUnsupportedConnectionTestNativeHandler(guard businessPermissionGuard) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		serviceType := strings.TrimPrefix(r.URL.Path, "/api/v1/admin/config/test/")
		WriteJSON(w, http.StatusBadRequest, fmt.Sprintf("不支持的服务类型: %s", serviceType), nil)
	}))
}

func buildChatCompletionURL(baseURL string) string {
	normalized := strings.TrimSpace(strings.TrimRight(baseURL, "/"))
	if normalized == "" {
		return "https://api.openai.com/v1/chat/completions"
	}
	if strings.HasSuffix(normalized, "/chat/completions") {
		return normalized
	}
	if strings.HasSuffix(normalized, "/v1") || strings.Contains(normalized, "/v1/") {
		return normalized + "/chat/completions"
	}
	return normalized + "/v1/chat/completions"
}

func buildEmbeddingURL(baseURL string) string {
	normalized := strings.TrimSpace(strings.TrimRight(baseURL, "/"))
	if normalized == "" {
		return "https://api.openai.com/v1/embeddings"
	}
	if strings.HasSuffix(normalized, "/embeddings") {
		return normalized
	}
	if strings.HasSuffix(normalized, "/v1") || strings.Contains(normalized, "/v1/") {
		return normalized + "/embeddings"
	}
	return normalized + "/v1/embeddings"
}

func milvusProbeTarget(rawURI string) string {
	trimmed := strings.TrimSpace(rawURI)
	if trimmed == "" {
		return ""
	}
	parsed, err := url.Parse(trimmed)
	if err == nil && parsed.Host != "" {
		host := parsed.Hostname()
		port := parsed.Port()
		if host == "" {
			return ""
		}
		if port == "" {
			port = "19530"
		}
		return net.JoinHostPort(host, port)
	}
	if strings.Contains(trimmed, ":") {
		return trimmed
	}
	return net.JoinHostPort(trimmed, "19530")
}

func buildClaudeMessagesURL(baseURL string) string {
	normalized := strings.TrimSpace(strings.TrimRight(baseURL, "/"))
	if normalized == "" {
		return "https://api.anthropic.com/v1/messages"
	}
	if strings.HasSuffix(normalized, "/messages") {
		return normalized
	}
	if strings.HasSuffix(normalized, "/v1") || strings.Contains(normalized, "/v1/") {
		return normalized + "/messages"
	}
	return normalized + "/v1/messages"
}

func defaultModelBaseURL(provider string, baseURL string) string {
	trimmed := strings.TrimSpace(baseURL)
	if trimmed != "" {
		return trimmed
	}
	if provider == "claude" {
		return "https://api.anthropic.com/v1"
	}
	return "https://api.openai.com/v1"
}

func extractAdminModelConnectionSnapshot(body []byte) (map[string]interface{}, bool) {
	var decoded map[string]interface{}
	if err := json.Unmarshal(body, &decoded); err != nil {
		return nil, false
	}
	data, ok := decoded["data"].(map[string]interface{})
	if !ok {
		return nil, false
	}
	if _, exists := data["checked_at"]; !exists {
		data["checked_at"] = time.Now().UTC().Format(time.RFC3339)
	}
	return data, true
}
