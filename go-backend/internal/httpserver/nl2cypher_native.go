package httpserver

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
	"graphinsight/go-backend/internal/orchestrator"
)

// publicNL2CypherPayload 的 kb_id/kb_ids（M4）：与 header/query 三来源严格一致合并，
// 服务端再与授权求交出有效范围后转发。
type publicNL2CypherPayload struct {
	NaturalLanguage string                 `json:"natural_language"`
	KBID            *string                `json:"kb_id,omitempty"`
	KBIDs           []string               `json:"kb_ids,omitempty"`
	Context         map[string]interface{} `json:"context"`
}

func optionalAuditMessage(value string) *string {
	trimmed := strings.TrimSpace(value)
	if trimmed == "" {
		return nil
	}
	return &trimmed
}

func buildNativeNL2CypherGenerateHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-orchestrator", func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-orchestrator")
		start := time.Now()
		recorded := false
		record := func(status int, err error) {
			if recorded {
				return
			}
			recorded = true
			metrics.Observe("/api/internal/nl2cypher", http.MethodPost, status, err, time.Since(start))
		}

		if r.Method != http.MethodPost {
			record(http.StatusMethodNotAllowed, nil)
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if client == nil {
			record(http.StatusServiceUnavailable, clientErr)
			logger.Error("orchestrator unavailable", "path", r.URL.Path, "init_error", clientErr)
			WriteJSON(w, http.StatusServiceUnavailable, "上游服务不可用", map[string]interface{}{
				"error_code": "UPSTREAM_UNAVAILABLE",
				"upstream":   "python-backend",
			})
			return
		}

		body, err := io.ReadAll(r.Body)
		if err != nil {
			record(http.StatusBadRequest, err)
			writeNL2CypherAudit(r, logStore, nil, "failed", optionalAuditMessage("invalid_request"), map[string]interface{}{"reason": "read_body_failed"})
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}

		var payload publicNL2CypherPayload
		if err := json.NewDecoder(bytes.NewReader(body)).Decode(&payload); err != nil {
			record(http.StatusBadRequest, err)
			writeNL2CypherAudit(r, logStore, nil, "failed", optionalAuditMessage("invalid_body"), map[string]interface{}{"reason": "decode_failed"})
			WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}
		if strings.TrimSpace(payload.NaturalLanguage) == "" {
			record(http.StatusBadRequest, nil)
			writeNL2CypherAudit(r, logStore, &payload, "failed", optionalAuditMessage("natural_language_required"), map[string]interface{}{"reason": "blank_natural_language"})
			WriteJSON(w, http.StatusBadRequest, "自然语言查询不能为空", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}

		// M4：kb 作用域解析（header/query/body 三来源严格一致）+ 授权求交。
		target, scopeErr := resolveQARequestScope(r, optionalStringValue(payload.KBID), payload.KBIDs)
		if scopeErr != nil {
			record(scopeErr.Status, nil)
			writeQAScopeRejectionAudit(r, logger, logStore, "nl2cypher:use", scopeErr)
			writeScopeError(w, scopeErr)
			return
		}
		effectiveKBIDs, ok := guard.authorizeQAEffectiveKBIDs(w, r, logStore, "nl2cypher:use", target)
		if !ok {
			record(http.StatusForbidden, nil)
			return
		}

		status, forwardErr := forwardOrchestratorJSONWithHeaders(w, r, logger, client, "/api/internal/nl2cypher", body, buildScopedForwardHeaders(r, target, effectiveKBIDs), false)
		auditStatus := "success"
		errorMessage := (*string)(nil)
		if status >= http.StatusBadRequest {
			auditStatus = "failed"
			errorMessage = optionalAuditMessage("upstream_request_failed")
		}
		writeNL2CypherAudit(r, logStore, &payload, auditStatus, errorMessage, map[string]interface{}{
			"http_status": status,
			"forwarded":   forwardErr == nil,
			"kb_ids":      effectiveKBIDs,
			"tenant_id":   target.TenantID,
			"project_id":  target.ProjectID,
		})
		record(status, forwardErr)
	})
}

func writeNL2CypherAudit(
	r *http.Request,
	logStore adminLogStore,
	payload *publicNL2CypherPayload,
	status string,
	errorMessage *string,
	details map[string]interface{},
) {
	if logStore == nil {
		return
	}
	if details == nil {
		details = map[string]interface{}{}
	}
	if payload != nil {
		nl := strings.TrimSpace(payload.NaturalLanguage)
		if nl != "" {
			if len(nl) > 100 {
				nl = nl[:100]
			}
			details["natural_language_preview"] = nl
		}
		if payload.Context != nil {
			details["has_context"] = true
		}
	}
	if _, exists := details["permission"]; !exists {
		details["permission"] = r.Header.Get("x-authz-permission")
	}
	traceID := optionalStringHeader(r, traceHeader)
	operatorID := optionalIntHeader(r, "x-auth-user-id")
	resourceID := (*string)(nil)
	if traceID != nil && strings.TrimSpace(*traceID) != "" {
		resourceID = traceID
	} else if operatorID != nil {
		value := strconv.Itoa(*operatorID)
		resourceID = &value
	}
	if err := logStore.RecordBusinessAudit(r.Context(), adminstore.BusinessAuditRequest{
		OperatorID:   operatorID,
		TenantID:     optionalString(resolveScopeHeaders(r)["x-tenant-id"]),
		TraceID:      traceID,
		Action:       "nl2cypher_generate",
		Resource:     "ai_query",
		ResourceID:   resourceID,
		Details:      details,
		IPAddress:    optionalString(firstRemoteAddr(r)),
		UserAgent:    optionalString(r.UserAgent()),
		Status:       status,
		ErrorMessage: errorMessage,
	}); err != nil {
		// Keep audit best-effort so business flow is not blocked by logging failure.
	}
}

func buildNativeNL2CypherExamplesHandler() http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		writeRawJSON(w, http.StatusOK, map[string]interface{}{
			"success": true,
			"examples": []map[string]string{
				{"nl": "查找所有水稻相关的病虫害", "description": "查询特定作物的病虫害"},
				{"nl": "显示小麦和它的防治方法", "description": "查询作物及其防治方法"},
				{"nl": "找出影响玉米的所有疾病", "description": "查询作物的疾病"},
				{"nl": "查询所有作物和它们的病害数量", "description": "统计查询"},
				{"nl": "显示水稻的完整知识图谱", "description": "查询节点的所有关系"},
			},
		})
	})
}

func buildNativeNL2CypherStatusHandler(
	cfg config.Config,
	logger *slog.Logger,
	guard businessPermissionGuard,
	configStore adminConfigStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("config:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}

		values := map[string]string{}
		if configStore != nil {
			resolved, err := safeConfigValues(r.Context(), configStore, "nl2cypher")
			if err != nil {
				logger.Error("get nl2cypher status config failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "获取 NL2Cypher 状态失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			values = resolved
		}

		model := firstNonEmptyString(
			values["model"],
			cfg.AIModel,
			"gpt-3.5-turbo",
		)
		apiKeyConfigured := cfg.AIAPIKey != ""
		if _, exists := values["api_key"]; exists {
			apiKeyConfigured = values["api_key"] != "" && values["api_key"] != "your-api-key-here"
		}

		writeRawJSON(w, http.StatusOK, map[string]interface{}{
			"enabled":            configBool(values, "enabled", envBool("NL2CYPHER_ENABLED", true)),
			"model":              model,
			"api_key_configured": apiKeyConfigured,
			"max_limit":          configInt(values, "max_limit", envInt("NL2CYPHER_MAX_LIMIT", 100)),
			"config_source":      "go-native",
		})
	}))
}

func writeRawJSON(w http.ResponseWriter, status int, data interface{}) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(data)
}
