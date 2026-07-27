package httpserver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/graph"
)

type adminModelCatalogItem struct {
	Provider           string   `json:"provider"`
	Model              string   `json:"model"`
	Label              string   `json:"label"`
	Enabled            bool     `json:"enabled"`
	SupportsReasoning  bool     `json:"supports_reasoning"`
	SupportedProfiles  []string `json:"supported_profiles"`
	DefaultProfile     string   `json:"default_profile"`
	ContextWindow      int      `json:"context_window,omitempty"`
	MaxOutputTokens    int      `json:"max_output_tokens,omitempty"`
	SuggestedMaxTokens int      `json:"suggested_max_tokens,omitempty"`
	TokenLimitSource   string   `json:"token_limit_source,omitempty"`
}

type adminModelTokenLimit struct {
	ContextWindow      int
	MaxOutputTokens    int
	SuggestedMaxTokens int
	Source             string
}

type adminModelCatalogResponse struct {
	Models           []string                `json:"models"`
	Catalog          []adminModelCatalogItem `json:"catalog"`
	Source           string                  `json:"source"`
	SourceMessage    string                  `json:"source_message,omitempty"`
	ScenarioProfiles map[string]string       `json:"scenario_profiles"`
}

type adminConfigStore interface {
	ListConfigs(ctx context.Context, query adminstore.ConfigListQuery) (adminstore.ConfigListResult, error)
	ListConfigCategory(ctx context.Context, category string) (map[string]adminstore.ConfigItem, error)
	GetConfigItem(ctx context.Context, category string, key string) (adminstore.ConfigItem, error)
	GetConfigValueMap(ctx context.Context, category string) (map[string]string, error)
	CreateConfig(ctx context.Context, req adminstore.ConfigMutationRequest) (adminstore.ConfigItem, error)
	UpdateConfig(ctx context.Context, req adminstore.ConfigMutationRequest) (adminstore.ConfigItem, error)
	DeleteConfig(ctx context.Context, req adminstore.ConfigMutationRequest) error
	BatchUpdateConfigs(ctx context.Context, req adminstore.ConfigBatchUpdateRequest) (int, error)
}

func asAdminConfigStore(store interface{}) adminConfigStore {
	typed, _ := store.(adminConfigStore)
	return typed
}

func buildAdminConfigReadNativeHandler(cfg config.Config, logger *slog.Logger, guard businessPermissionGuard, configStore adminConfigStore, graphSvc graphService) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if configStore == nil {
			logger.Error("admin config store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "配置数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		switch r.URL.Path {
		case "/api/v1/admin/config":
			page := boundedIntQuery(r, "page", 1, 1, 1_000_000)
			pageSize := boundedIntQuery(r, "page_size", 10, 1, 100)
			isSensitive, ok := optionalBoolQuery(w, r, "is_sensitive")
			if !ok {
				return
			}
			result, err := configStore.ListConfigs(r.Context(), adminstore.ConfigListQuery{
				Category:    strings.TrimSpace(r.URL.Query().Get("category")),
				Key:         strings.TrimSpace(r.URL.Query().Get("key")),
				IsSensitive: isSensitive,
				Page:        page,
				PageSize:    pageSize,
			})
			if err != nil {
				logger.Error("list admin configs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取配置列表失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			totalPages := 0
			if pageSize > 0 && result.Total > 0 {
				totalPages = (result.Total + pageSize - 1) / pageSize
			}
			WriteJSON(w, http.StatusOK, "获取成功", adminPaginatedData{
				Items:      result.Items,
				Total:      result.Total,
				Page:       page,
				PageSize:   pageSize,
				TotalPages: totalPages,
			})
		case "/api/v1/admin/config/neo4j/all":
			values, err := safeConfigValues(r.Context(), configStore, "neo4j")
			if err != nil {
				logger.Error("get neo4j configs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			runtimeInfo := graph.RuntimeConnectionInfo{}
			if graphSvc != nil {
				runtimeInfo = graphSvc.RuntimeConnectionInfo()
			}
			WriteJSON(w, http.StatusOK, "获取成功", buildAdminNeo4jConfigSnapshotWithRuntime(cfg, values, runtimeInfo))
		case "/api/v1/admin/config/ai-service/all":
			values, err := safeConfigValues(r.Context(), configStore, "ai_service")
			if err != nil {
				logger.Error("get ai service configs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", buildAdminAIServiceConfigSnapshot(cfg, values))
		case "/api/v1/admin/config/nl2cypher/all":
			values, err := safeConfigValues(r.Context(), configStore, "nl2cypher")
			if err != nil {
				logger.Error("get nl2cypher configs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", buildAdminNL2CypherConfigSnapshot(values))
		case "/api/v1/admin/config/embedding/all":
			values, err := safeConfigValues(r.Context(), configStore, "embedding")
			if err != nil {
				logger.Error("get embedding configs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", buildAdminEmbeddingConfigSnapshot(cfg, values))
		case "/api/v1/admin/config/vector-store/all":
			values, err := safeConfigValues(r.Context(), configStore, "vector_store")
			if err != nil {
				logger.Error("get vector store configs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", buildAdminVectorStoreConfigSnapshot(values))
		case "/api/v1/admin/config/ai-service/models":
			values, err := safeConfigValues(r.Context(), configStore, "ai_service")
			if err != nil {
				logger.Error("get ai service values for model catalog failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取模型目录失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", buildAdminModelCatalogResponse(r.Context(), cfg, values, r.URL.Query()))
		default:
			if strings.HasPrefix(r.URL.Path, "/api/v1/admin/config/") {
				handleAdminConfigReadSubpath(w, r, logger, configStore)
				return
			}
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	}))
}

func buildAdminConfigMutationNativeHandler(logger *slog.Logger, guard businessPermissionGuard, configStore adminConfigStore) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:write", func(w http.ResponseWriter, r *http.Request) {
		if configStore == nil {
			logger.Error("admin config store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "配置数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		switch {
		case r.URL.Path == "/api/v1/admin/config/batch" && r.Method == http.MethodPost:
			var payload adminConfigBatchPayload
			if !decodeAdminConfigPayload(w, r, &payload) {
				return
			}
			items := make([]adminstore.ConfigBatchItem, 0, len(payload.Configs))
			for _, item := range payload.Configs {
				items = append(items, adminstore.ConfigBatchItem{
					Category: normalizeAdminConfigCategory(item.Category),
					Key:      item.Key,
					Value:    item.Value,
				})
			}
			updatedCount, err := configStore.BatchUpdateConfigs(r.Context(), buildConfigBatchUpdateRequest(r, items))
			if err != nil {
				logger.Error("batch update admin configs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "批量更新配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, fmt.Sprintf("批量更新成功，更新了 %d 个配置", updatedCount), map[string]int{
				"updated_count": updatedCount,
				"total":         len(payload.Configs),
			})
		case r.URL.Path == "/api/v1/admin/config" && r.Method == http.MethodPost:
			var payload adminConfigCreatePayload
			if !decodeAdminConfigPayload(w, r, &payload) {
				return
			}
			isSensitive := payload.IsSensitive
			item, err := configStore.CreateConfig(r.Context(), buildConfigMutationRequest(r, payload.Category, payload.Key, payload.Value, payload.Description, &isSensitive))
			if errors.Is(err, adminstore.ErrConfigAlreadyExists) {
				WriteJSON(w, http.StatusConflict, "配置已存在", map[string]string{"error_code": "CONFIG_ALREADY_EXISTS"})
				return
			}
			if err != nil {
				logger.Error("create admin config failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "创建配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusCreated, "创建成功", item)
		case strings.HasPrefix(r.URL.Path, "/api/v1/admin/config/") && r.Method == http.MethodPut:
			category, key, ok := parseAdminConfigItemPath(r.URL.Path)
			if !ok {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			var payload adminConfigUpdatePayload
			if !decodeAdminConfigPayload(w, r, &payload) {
				return
			}
			item, err := configStore.UpdateConfig(r.Context(), buildConfigMutationRequest(r, category, key, payload.Value, payload.Description, nil))
			if err != nil {
				logger.Error("update admin config failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "更新配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "更新成功", item)
		case strings.HasPrefix(r.URL.Path, "/api/v1/admin/config/") && r.Method == http.MethodDelete:
			category, key, ok := parseAdminConfigItemPath(r.URL.Path)
			if !ok {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			err := configStore.DeleteConfig(r.Context(), buildConfigMutationRequest(r, category, key, "", nil, nil))
			if errors.Is(err, adminstore.ErrConfigNotFound) {
				WriteJSON(w, http.StatusNotFound, "配置不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if err != nil {
				logger.Error("delete admin config failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "删除配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			WriteJSON(w, http.StatusOK, "删除成功", nil)
		default:
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
		}
	}))
}

func handleAdminConfigReadSubpath(w http.ResponseWriter, r *http.Request, logger *slog.Logger, configStore adminConfigStore) {
	rest := strings.TrimPrefix(r.URL.Path, "/api/v1/admin/config/")
	parts := strings.Split(rest, "/")
	if len(parts) == 1 && isAdminConfigCategory(parts[0]) {
		items, err := configStore.ListConfigCategory(r.Context(), normalizeAdminConfigCategory(parts[0]))
		if err != nil {
			logger.Error("get admin config category failed", "category", parts[0], "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取配置失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", items)
		return
	}
	if len(parts) == 2 && isAdminConfigCategory(parts[0]) {
		item, err := configStore.GetConfigItem(r.Context(), normalizeAdminConfigCategory(parts[0]), parts[1])
		if errors.Is(err, adminstore.ErrConfigNotFound) {
			WriteJSON(w, http.StatusNotFound, "配置不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}
		if err != nil {
			logger.Error("get admin config item failed", "category", parts[0], "key", parts[1], "error", err.Error())
			WriteJSON(w, http.StatusServiceUnavailable, "获取配置详情失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		WriteJSON(w, http.StatusOK, "获取成功", item)
		return
	}
	WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
}

type adminConfigCreatePayload struct {
	Category    string  `json:"category"`
	Key         string  `json:"key"`
	Value       string  `json:"value"`
	Description *string `json:"description"`
	IsSensitive bool    `json:"is_sensitive"`
}

type adminConfigUpdatePayload struct {
	Value       string  `json:"value"`
	Description *string `json:"description"`
}

type adminConfigBatchPayload struct {
	Configs []adminConfigBatchItemPayload `json:"configs"`
}

type adminConfigBatchItemPayload struct {
	Category string `json:"category"`
	Key      string `json:"key"`
	Value    string `json:"value"`
}

func decodeAdminConfigPayload(w http.ResponseWriter, r *http.Request, target interface{}) bool {
	if err := json.NewDecoder(r.Body).Decode(target); err != nil {
		WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
		return false
	}
	return true
}

func parseAdminConfigItemPath(path string) (string, string, bool) {
	rest := strings.TrimPrefix(path, "/api/v1/admin/config/")
	parts := strings.Split(rest, "/")
	if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || strings.TrimSpace(parts[1]) == "" {
		return "", "", false
	}
	if !isAdminConfigCategory(parts[0]) {
		return "", "", false
	}
	return normalizeAdminConfigCategory(parts[0]), parts[1], true
}

func buildConfigMutationRequest(
	r *http.Request,
	category string,
	key string,
	value string,
	description *string,
	isSensitive *bool,
) adminstore.ConfigMutationRequest {
	return adminstore.ConfigMutationRequest{
		Category:    normalizeAdminConfigCategory(category),
		Key:         key,
		Value:       value,
		Description: description,
		IsSensitive: isSensitive,
		OperatorID:  optionalIntHeader(r, "x-auth-user-id"),
		TenantID:    optionalStringHeader(r, "x-scope-tenant-id"),
		TraceID:     optionalStringHeader(r, traceHeader),
		IPAddress:   optionalString(firstRemoteAddr(r)),
		UserAgent:   optionalString(r.UserAgent()),
	}
}

func buildConfigBatchUpdateRequest(r *http.Request, items []adminstore.ConfigBatchItem) adminstore.ConfigBatchUpdateRequest {
	return adminstore.ConfigBatchUpdateRequest{
		Items:      items,
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		TenantID:   optionalStringHeader(r, "x-scope-tenant-id"),
		TraceID:    optionalStringHeader(r, traceHeader),
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
	}
}

func optionalIntHeader(r *http.Request, key string) *int {
	raw := strings.TrimSpace(r.Header.Get(key))
	if raw == "" {
		return nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return nil
	}
	return &value
}

func optionalStringHeader(r *http.Request, key string) *string {
	return optionalString(r.Header.Get(key))
}

func optionalString(value string) *string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return nil
	}
	return &trimmed
}

func firstRemoteAddr(r *http.Request) string {
	if forwarded := strings.TrimSpace(r.Header.Get("X-Forwarded-For")); forwarded != "" {
		parts := strings.Split(forwarded, ",")
		return strings.TrimSpace(parts[0])
	}
	if realIP := strings.TrimSpace(r.Header.Get("X-Real-IP")); realIP != "" {
		return realIP
	}
	return strings.TrimSpace(r.RemoteAddr)
}

func buildAdminNeo4jConfigSnapshot(cfg config.Config, values map[string]string) map[string]interface{} {
	return buildAdminNeo4jConfigSnapshotWithRuntime(cfg, values, graph.RuntimeConnectionInfo{})
}

func buildAdminNeo4jConfigSnapshotWithRuntime(cfg config.Config, values map[string]string, runtime graph.RuntimeConnectionInfo) map[string]interface{} {
	runtimeURI := strings.TrimSpace(runtime.URI)
	runtimeDatabase := strings.TrimSpace(runtime.Database)
	runtimeMode := strings.TrimSpace(runtime.ConfigMode)
	runtimeSource := strings.TrimSpace(runtime.ConfigSource)
	runtimeResolutionErr := strings.TrimSpace(runtime.ResolutionError)
	user := firstNonEmptyString(values["user"], values["username"], cfg.Neo4jUser, "neo4j")
	mode := firstNonEmptyString(runtimeMode, cfg.Neo4jConfigSource, "env")
	source := firstNonEmptyString(runtimeSource, cfg.Neo4jConfigResolvedSource, mode)
	passwordConfigured := strings.TrimSpace(cfg.Neo4jPassword) != ""
	if _, exists := values["password"]; exists {
		passwordConfigured = strings.TrimSpace(values["password"]) != ""
	}
	payload := map[string]interface{}{
		"uri":                 firstNonEmptyString(values["uri"], runtimeURI, cfg.Neo4jURI, "bolt://localhost:7687"),
		"user":                user,
		"username":            user,
		"password":            "",
		"password_configured": passwordConfigured,
		"database":            firstNonEmptyString(values["database"], runtimeDatabase, cfg.Neo4jDatabase, "neo4j"),
		"source":              source,
		"mode":                mode,
	}
	if runtimeResolutionErr != "" {
		payload["resolution_error"] = runtimeResolutionErr
	}
	return payload
}

func buildAdminAIServiceConfigSnapshot(cfg config.Config, values map[string]string) map[string]interface{} {
	provider := firstNonEmptyString(values["provider"], cfg.AIProvider, os.Getenv("AI_SERVICE_PROVIDER"), os.Getenv("OPENAI_PROVIDER"), "openai")
	model := firstNonEmptyString(values["model"], cfg.AIModel, os.Getenv("AI_SERVICE_MODEL"), os.Getenv("OPENAI_MODEL"), "gpt-3.5-turbo")
	apiKey := firstNonEmptyString(values["api_key"], cfg.AIAPIKey)
	apiKeyConfigured := strings.TrimSpace(apiKey) != "" && strings.TrimSpace(apiKey) != "your-api-key-here"
	return map[string]interface{}{
		"provider":                        provider,
		"enabled":                         configBool(values, "enabled", envBool("AI_SERVICE_ENABLED", true)),
		"base_url":                        firstNonEmptyString(values["base_url"], os.Getenv("AI_SERVICE_BASE_URL"), os.Getenv("OPENAI_BASE_URL")),
		"api_key":                         "",
		"api_key_configured":              apiKeyConfigured,
		"api_key_preview":                 maskSecretPreview(apiKey),
		"model":                           model,
		"docqa_reasoning_profile":         firstNonEmptyString(values["docqa_reasoning_profile"], os.Getenv("AI_SERVICE_DOCQA_REASONING_PROFILE"), "balanced"),
		"deep_research_reasoning_profile": firstNonEmptyString(values["deep_research_reasoning_profile"], os.Getenv("AI_SERVICE_DEEP_RESEARCH_REASONING_PROFILE"), "deep"),
		"model_probe_reasoning_profile":   firstNonEmptyString(values["model_probe_reasoning_profile"], os.Getenv("AI_SERVICE_MODEL_PROBE_REASONING_PROFILE"), "fast"),
		"graph_extract_reasoning_profile": firstNonEmptyString(values["graph_extract_reasoning_profile"], os.Getenv("AI_SERVICE_GRAPH_EXTRACT_REASONING_PROFILE"), "fast"),
		"graph_extract_complex_reasoning_profile": firstNonEmptyString(values["graph_extract_complex_reasoning_profile"], os.Getenv("AI_SERVICE_GRAPH_EXTRACT_COMPLEX_REASONING_PROFILE"), "balanced"),
		"max_tokens":  configInt(values, "max_tokens", envInt("AI_SERVICE_MAX_TOKENS", envInt("OPENAI_MAX_TOKENS", 2000))),
		"temperature": configFloat(values, "temperature", envFloat("AI_SERVICE_TEMPERATURE", envFloat("OPENAI_TEMPERATURE", 0.7))),
	}
}

func buildAdminNL2CypherConfigSnapshot(values map[string]string) map[string]interface{} {
	return map[string]interface{}{
		"enabled":    configBool(values, "enabled", envBool("NL2CYPHER_ENABLED", true)),
		"cache_size": configInt(values, "cache_size", envInt("NL2CYPHER_CACHE_SIZE", 100)),
		"max_limit":  configInt(values, "max_limit", envInt("NL2CYPHER_MAX_LIMIT", 100)),
	}
}

func buildAdminEmbeddingConfigSnapshot(cfg config.Config, values map[string]string) map[string]interface{} {
	apiKey := firstNonEmptyString(values["api_key"], os.Getenv("EMBEDDING_API_KEY"), cfg.AIAPIKey)
	return map[string]interface{}{
		"enabled":            configBool(values, "enabled", envBool("EMBEDDING_ENABLED", true)),
		"provider":           firstNonEmptyString(values["provider"], os.Getenv("EMBEDDING_PROVIDER"), cfg.AIProvider, "openai"),
		"base_url":           firstNonEmptyString(values["base_url"], os.Getenv("EMBEDDING_BASE_URL")),
		"api_key":            "",
		"api_key_configured": strings.TrimSpace(apiKey) != "" && strings.TrimSpace(apiKey) != "your-api-key-here",
		"api_key_preview":    maskSecretPreview(apiKey),
		"model":              firstNonEmptyString(values["model"], os.Getenv("EMBEDDING_MODEL"), "text-embedding-3-small"),
		"dimension":          configInt(values, "dimension", envInt("EMBEDDING_DIMENSION", 1536)),
		"batch_size":         configInt(values, "batch_size", envInt("EMBEDDING_BATCH_SIZE", 32)),
	}
}

func buildAdminVectorStoreConfigSnapshot(values map[string]string) map[string]interface{} {
	token := firstNonEmptyString(values["token"], os.Getenv("MILVUS_TOKEN"))
	return map[string]interface{}{
		"enabled":          configBool(values, "enabled", envBool("VECTOR_STORE_ENABLED", false)),
		"provider":         firstNonEmptyString(values["provider"], os.Getenv("VECTOR_STORE_PROVIDER"), "milvus"),
		"uri":              firstNonEmptyString(values["uri"], os.Getenv("MILVUS_URI"), "http://127.0.0.1:19530"),
		"db_name":          firstNonEmptyString(values["db_name"], os.Getenv("MILVUS_DB_NAME"), "default"),
		"collection":       firstNonEmptyString(values["collection"], os.Getenv("MILVUS_COLLECTION"), "graphinsight_chunks"),
		"token":            "",
		"token_configured": strings.TrimSpace(token) != "",
		"token_preview":    maskSecretPreview(token),
	}
}

func buildAdminAvailableModels(cfg config.Config, currentOverride string, probedModels []string) []string {
	models := mergeUniqueStrings(probedModels)
	if len(models) == 0 {
		models = mergeUniqueStrings(
			parseModelListEnv("AI_SERVICE_AVAILABLE_MODELS"),
			parseModelListEnv("OPENAI_AVAILABLE_MODELS"),
			parseModelListEnv("LLM_AVAILABLE_MODELS"),
		)
	}
	current := firstNonEmptyString(currentOverride, cfg.AIModel)
	if current == "" {
		return models
	}
	return mergeUniqueStrings([]string{current}, models)
}

func buildAdminModelCatalogResponse(ctx context.Context, cfg config.Config, values map[string]string, query map[string][]string) adminModelCatalogResponse {
	provider := firstNonEmptyString(values["provider"], cfg.AIProvider, os.Getenv("AI_SERVICE_PROVIDER"), os.Getenv("OPENAI_PROVIDER"), "openai")
	defaultProfile := firstNonEmptyString(values["docqa_reasoning_profile"], os.Getenv("AI_SERVICE_DOCQA_REASONING_PROFILE"), "balanced")
	baseURL := firstNonEmptyString(firstQueryValue(query, "base_url"), values["base_url"], os.Getenv("AI_SERVICE_BASE_URL"), os.Getenv("OPENAI_BASE_URL"))
	apiKey := firstNonEmptyString(values["api_key"], cfg.AIAPIKey)
	currentModel := firstNonEmptyString(firstQueryValue(query, "model"), values["model"], os.Getenv("AI_SERVICE_MODEL"), os.Getenv("LLM_QA_MODEL"), os.Getenv("LLM_MODEL"), os.Getenv("OPENAI_MODEL"))
	queryProvider := firstNonEmptyString(firstQueryValue(query, "provider"), provider)
	probedModels, probedLimits, sourceMessage := fetchAdminAvailableModels(ctx, queryProvider, baseURL, apiKey)
	source := "current"
	if len(probedModels) > 0 {
		source = "remote"
	} else if len(parseModelListEnv("AI_SERVICE_AVAILABLE_MODELS")) > 0 || len(parseModelListEnv("OPENAI_AVAILABLE_MODELS")) > 0 || len(parseModelListEnv("LLM_AVAILABLE_MODELS")) > 0 {
		source = "configured"
	}
	models := buildAdminAvailableModels(cfg, currentModel, probedModels)
	purpose := strings.ToLower(firstNonEmptyString(firstQueryValue(query, "purpose"), "chat"))
	models = filterModelsForPurpose(models, purpose, currentModel)
	catalog := make([]adminModelCatalogItem, 0, len(models))
	for _, model := range models {
		supportsReasoning, supportedProfiles := inferReasoningProfiles(model)
		tokenLimit := mergeModelTokenLimit(probedLimits[strings.ToLower(model)], inferModelTokenLimit(model))
		catalog = append(catalog, adminModelCatalogItem{
			Provider:           queryProvider,
			Model:              model,
			Label:              humanizeModelLabel(model),
			Enabled:            true,
			SupportsReasoning:  supportsReasoning,
			SupportedProfiles:  supportedProfiles,
			DefaultProfile:     defaultProfile,
			ContextWindow:      tokenLimit.ContextWindow,
			MaxOutputTokens:    tokenLimit.MaxOutputTokens,
			SuggestedMaxTokens: tokenLimit.SuggestedMaxTokens,
			TokenLimitSource:   tokenLimit.Source,
		})
	}
	return adminModelCatalogResponse{
		Models:        models,
		Catalog:       catalog,
		Source:        source,
		SourceMessage: sourceMessage,
		ScenarioProfiles: map[string]string{
			"docqa":                 defaultProfile,
			"deep_research":         firstNonEmptyString(values["deep_research_reasoning_profile"], os.Getenv("AI_SERVICE_DEEP_RESEARCH_REASONING_PROFILE"), "deep"),
			"model_probe":           firstNonEmptyString(values["model_probe_reasoning_profile"], os.Getenv("AI_SERVICE_MODEL_PROBE_REASONING_PROFILE"), "fast"),
			"graph_extract":         firstNonEmptyString(values["graph_extract_reasoning_profile"], os.Getenv("AI_SERVICE_GRAPH_EXTRACT_REASONING_PROFILE"), "fast"),
			"graph_extract_complex": firstNonEmptyString(values["graph_extract_complex_reasoning_profile"], os.Getenv("AI_SERVICE_GRAPH_EXTRACT_COMPLEX_REASONING_PROFILE"), "balanced"),
		},
	}
}

func filterModelsForPurpose(models []string, purpose string, currentModel string) []string {
	filtered := make([]string, 0, len(models))
	for _, model := range models {
		isEmbedding := isLikelyEmbeddingModel(model)
		if (purpose == "embedding" && isEmbedding) || (purpose != "embedding" && !isEmbedding) {
			filtered = append(filtered, model)
		}
	}
	if purpose == "embedding" && strings.TrimSpace(currentModel) != "" {
		filtered = append([]string{currentModel}, filtered...)
	}
	return mergeUniqueStrings(filtered)
}

func isLikelyEmbeddingModel(model string) bool {
	lower := strings.ToLower(strings.TrimSpace(model))
	markers := []string{"embedding", "embed", "bge-", "bge_", "-bge", "e5-", "e5_", "gte-", "gte_", "jina-embeddings", "text2vec", "multilingual-e5"}
	for _, marker := range markers {
		if strings.Contains(lower, marker) {
			return true
		}
	}
	return false
}

func humanizeModelLabel(model string) string {
	normalized := strings.TrimSpace(model)
	if normalized == "" {
		return "Unknown Model"
	}
	parts := strings.FieldsFunc(normalized, func(r rune) bool {
		return r == '-' || r == '_'
	})
	for i, part := range parts {
		if part == "" {
			continue
		}
		if isAllLowerASCII(part) {
			parts[i] = strings.ToUpper(part[:1]) + part[1:]
		} else {
			parts[i] = strings.ToUpper(part[:1]) + part[1:]
		}
	}
	return strings.Join(parts, " ")
}

func firstQueryValue(query map[string][]string, key string) string {
	if query == nil {
		return ""
	}
	values := query[key]
	if len(values) == 0 {
		return ""
	}
	return strings.TrimSpace(values[0])
}

func fetchAdminAvailableModels(ctx context.Context, provider string, baseURL string, apiKey string) ([]string, map[string]adminModelTokenLimit, string) {
	if strings.TrimSpace(apiKey) == "" || strings.TrimSpace(apiKey) == "your-api-key-here" {
		return nil, nil, "API Key 未配置，仅显示当前模型或手动候选"
	}
	if provider == "claude" {
		return nil, nil, "Claude 暂未提供统一模型列表接口，仅显示当前模型或手动候选"
	}
	urls := buildModelsURLs(baseURL)
	if len(urls) == 0 {
		return nil, nil, "未配置模型列表地址"
	}
	probeCtx, cancel := context.WithTimeout(ctx, 12*time.Second)
	defer cancel()
	client := &http.Client{Timeout: 12 * time.Second}
	for _, url := range urls {
		req, err := http.NewRequestWithContext(probeCtx, http.MethodGet, url, nil)
		if err != nil {
			continue
		}
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(apiKey))
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			continue
		}
		var payload map[string]interface{}
		if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
			continue
		}
		models, limits := parseModelCatalog(payload)
		if len(models) > 0 {
			return models, limits, fmt.Sprintf("来自 %s", url)
		}
	}
	return nil, nil, "未能从服务商模型接口解析到模型，仅显示当前模型或手动候选"
}

func buildModelsURLs(baseURL string) []string {
	normalized := strings.TrimSpace(strings.TrimRight(baseURL, "/"))
	if normalized == "" {
		return []string{"https://api.openai.com/v1/models"}
	}
	if strings.HasSuffix(normalized, "/models") {
		return []string{normalized}
	}
	candidates := make([]string, 0, 2)
	if strings.HasSuffix(normalized, "/v1") || strings.Contains(normalized, "/v1/") {
		candidates = append(candidates, normalized+"/models")
	} else {
		candidates = append(candidates, normalized+"/v1/models", normalized+"/models")
	}
	return mergeUniqueStrings(candidates)
}

func parseModelCatalog(payload map[string]interface{}) ([]string, map[string]adminModelTokenLimit) {
	if payload == nil {
		return nil, nil
	}
	models := make([]string, 0)
	limits := map[string]adminModelTokenLimit{}
	if data, ok := payload["data"].([]interface{}); ok {
		for _, item := range data {
			if objectValue, ok := item.(map[string]interface{}); ok {
				if id, ok := objectValue["id"].(string); ok {
					models = append(models, id)
					if limit := parseModelTokenLimit(objectValue, "remote"); hasModelTokenLimit(limit) {
						limits[strings.ToLower(id)] = limit
					}
				}
			}
		}
	}
	if rawModels, ok := payload["models"].([]interface{}); ok {
		for _, item := range rawModels {
			if id, ok := item.(string); ok {
				models = append(models, id)
			} else if objectValue, ok := item.(map[string]interface{}); ok {
				if id, ok := objectValue["id"].(string); ok {
					models = append(models, id)
					if limit := parseModelTokenLimit(objectValue, "remote"); hasModelTokenLimit(limit) {
						limits[strings.ToLower(id)] = limit
					}
				}
			}
		}
	}
	return mergeUniqueStrings(models), limits
}

func parseModelTokenLimit(payload map[string]interface{}, source string) adminModelTokenLimit {
	contextWindow := firstPositiveModelInt(payload,
		"context_window",
		"context_length",
		"max_context_tokens",
		"input_token_limit",
		"max_input_tokens",
	)
	maxOutput := firstPositiveModelInt(payload,
		"max_output_tokens",
		"output_token_limit",
		"max_completion_tokens",
		"max_response_tokens",
	)
	suggested := maxOutput
	if suggested == 0 && contextWindow > 0 {
		suggested = minInt(contextWindow/4, 8192)
	}
	if suggested > 0 {
		suggested = maxInt(suggested, 512)
	}
	return adminModelTokenLimit{
		ContextWindow:      contextWindow,
		MaxOutputTokens:    maxOutput,
		SuggestedMaxTokens: suggested,
		Source:             source,
	}
}

func firstPositiveModelInt(payload map[string]interface{}, keys ...string) int {
	for _, key := range keys {
		if value := modelInt(payload[key]); value > 0 {
			return value
		}
	}
	return 0
}

func modelInt(value interface{}) int {
	switch typed := value.(type) {
	case int:
		return typed
	case int64:
		return int(typed)
	case float64:
		return int(typed)
	case json.Number:
		parsed, _ := strconv.Atoi(typed.String())
		return parsed
	case string:
		parsed, _ := strconv.Atoi(strings.TrimSpace(typed))
		return parsed
	default:
		return 0
	}
}

func hasModelTokenLimit(limit adminModelTokenLimit) bool {
	return limit.ContextWindow > 0 || limit.MaxOutputTokens > 0 || limit.SuggestedMaxTokens > 0
}

func mergeModelTokenLimit(primary adminModelTokenLimit, fallback adminModelTokenLimit) adminModelTokenLimit {
	merged := fallback
	if primary.ContextWindow > 0 {
		merged.ContextWindow = primary.ContextWindow
	}
	if primary.MaxOutputTokens > 0 {
		merged.MaxOutputTokens = primary.MaxOutputTokens
	}
	if primary.SuggestedMaxTokens > 0 {
		merged.SuggestedMaxTokens = primary.SuggestedMaxTokens
	}
	if primary.Source != "" && hasModelTokenLimit(primary) {
		merged.Source = primary.Source
	}
	return merged
}

func inferModelTokenLimit(model string) adminModelTokenLimit {
	lower := strings.ToLower(strings.TrimSpace(model))
	if lower == "" || strings.Contains(lower, "embedding") || strings.Contains(lower, "text-embedding") {
		return adminModelTokenLimit{}
	}
	limit := adminModelTokenLimit{SuggestedMaxTokens: 4096, Source: "heuristic"}
	switch {
	case strings.Contains(lower, "gpt-5"),
		strings.Contains(lower, "gpt-4.1"),
		strings.Contains(lower, "gpt-4o"),
		strings.Contains(lower, "qwen"),
		strings.Contains(lower, "deepseek"),
		strings.Contains(lower, "glm-4"):
		limit.ContextWindow = 128000
		limit.SuggestedMaxTokens = 8192
	case strings.Contains(lower, "gpt-4"):
		limit.ContextWindow = 128000
		limit.SuggestedMaxTokens = 4096
	}
	return limit
}

func parseModelListEnv(key string) []string {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return nil
	}
	parts := strings.FieldsFunc(raw, func(r rune) bool {
		return r == ',' || r == ';' || r == '\n' || r == '\t'
	})
	models := make([]string, 0, len(parts))
	for _, part := range parts {
		if model := strings.TrimSpace(part); model != "" {
			models = append(models, model)
		}
	}
	return models
}

func maskSecretPreview(secret string) string {
	trimmed := strings.TrimSpace(secret)
	if trimmed == "" || trimmed == "your-api-key-here" {
		return ""
	}
	runes := []rune(trimmed)
	if len(runes) <= 8 {
		return "已配置"
	}
	prefixLen := 3
	if len(runes) < prefixLen {
		prefixLen = len(runes)
	}
	suffixLen := 4
	if len(runes) < prefixLen+suffixLen {
		suffixLen = len(runes) - prefixLen
	}
	return string(runes[:prefixLen]) + "..." + string(runes[len(runes)-suffixLen:])
}

func mergeUniqueStrings(groups ...[]string) []string {
	seen := make(map[string]struct{})
	result := make([]string, 0)
	for _, group := range groups {
		for _, item := range group {
			trimmed := strings.TrimSpace(item)
			if trimmed == "" {
				continue
			}
			key := strings.ToLower(trimmed)
			if _, exists := seen[key]; exists {
				continue
			}
			seen[key] = struct{}{}
			result = append(result, trimmed)
		}
	}
	return result
}

func inferReasoningProfiles(model string) (bool, []string) {
	lower := strings.ToLower(strings.TrimSpace(model))
	switch {
	case lower == "":
		return false, []string{"balanced"}
	case strings.Contains(lower, "reasoner"), strings.Contains(lower, "r1"), strings.Contains(lower, "o1"), strings.Contains(lower, "o3"), strings.Contains(lower, "thinking"):
		return true, []string{"fast", "balanced", "deep"}
	case strings.Contains(lower, "gpt-5"), strings.Contains(lower, "gpt-4"), strings.Contains(lower, "qwen-plus"), strings.Contains(lower, "qwen-max"), strings.Contains(lower, "deepseek"), strings.Contains(lower, "glm-4"):
		return true, []string{"fast", "balanced", "deep"}
	default:
		return false, []string{"balanced"}
	}
}

func isAllLowerASCII(value string) bool {
	for _, r := range value {
		if r >= 'A' && r <= 'Z' {
			return false
		}
	}
	return true
}

func safeConfigValues(ctx context.Context, configStore adminConfigStore, category string) (map[string]string, error) {
	values, err := configStore.GetConfigValueMap(ctx, normalizeAdminConfigCategory(category))
	if err != nil {
		return nil, err
	}
	return values, nil
}

func normalizeAdminConfigCategory(category string) string {
	switch category {
	case "ai-service":
		return "ai_service"
	case "vector-store":
		return "vector_store"
	case "document-parser":
		return "document_parser"
	default:
		return category
	}
}

func isAdminConfigCategory(value string) bool {
	switch value {
	case "neo4j", "ai_service", "ai-service", "nl2cypher", "retrieval", "embedding", "vector_store", "vector-store", "document_parser", "document-parser":
		return true
	default:
		return false
	}
}

func configBool(values map[string]string, key string, fallback bool) bool {
	raw := strings.TrimSpace(values[key])
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseBool(raw)
	if err != nil {
		return fallback
	}
	return value
}

func configInt(values map[string]string, key string, fallback int) int {
	raw := strings.TrimSpace(values[key])
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return value
}

func configFloat(values map[string]string, key string, fallback float64) float64 {
	raw := strings.TrimSpace(values[key])
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return fallback
	}
	return value
}

func isSensitiveConfigKey(key string) bool {
	normalized := strings.ToLower(key)
	if strings.HasSuffix(normalized, "_configured") {
		return false
	}
	return strings.Contains(normalized, "password") ||
		strings.Contains(normalized, "secret") ||
		strings.Contains(normalized, "token") ||
		strings.Contains(normalized, "key")
}

func configValueString(value interface{}, sensitive bool) string {
	if sensitive {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return typed
	case bool:
		return strconv.FormatBool(typed)
	case int:
		return strconv.Itoa(typed)
	case float64:
		return strconv.FormatFloat(typed, 'f', -1, 64)
	default:
		return fmt.Sprint(typed)
	}
}

func envBool(key string, fallback bool) bool {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	value, err := strconv.ParseBool(raw)
	if err != nil {
		return fallback
	}
	return value
}

func envInt(key string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return fallback
	}
	return value
}

func envFloat(key string, fallback float64) float64 {
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
