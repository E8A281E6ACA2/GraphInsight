package httpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/orchestrator"
)

type publicConversationTurn struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// publicDocQAPayload 的 kb_id/kb_ids（M4）：与 header/query 三来源严格一致合并
// （scope.ResolveRequestWithBody），服务端再与授权求交出有效范围后转发。
type publicDocQAPayload struct {
	Question            string                   `json:"question"`
	KBID                *string                  `json:"kb_id,omitempty"`
	KBIDs               []string                 `json:"kb_ids,omitempty"`
	TopK                *int                     `json:"top_k,omitempty"`
	RequireCitation     *bool                    `json:"require_citation,omitempty"`
	ReasoningProfile    string                   `json:"reasoning_profile,omitempty"`
	ConversationHistory []publicConversationTurn `json:"conversation_history,omitempty"`
}

type publicDeepResearchPayload struct {
	Question         string   `json:"question"`
	KBID             *string  `json:"kb_id,omitempty"`
	KBIDs            []string `json:"kb_ids,omitempty"`
	TopK             *int     `json:"top_k,omitempty"`
	MaxSubQuestions  *int     `json:"max_sub_questions,omitempty"`
	ReasoningProfile string   `json:"reasoning_profile,omitempty"`
}

func buildNativeDocQAHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
	logStore adminLogStore,
	configStore adminConfigStore,
	safeRetry bool,
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
			metrics.Observe("/api/internal/docqa", http.MethodPost, status, err, time.Since(start))
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
			writeDocQAAudit(r, logStore, nil, "failed", optionalAuditMessage("invalid_request"), map[string]interface{}{"reason": "read_body_failed"})
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}

		var payload publicDocQAPayload
		if err := json.NewDecoder(bytes.NewReader(body)).Decode(&payload); err != nil {
			record(http.StatusBadRequest, err)
			writeDocQAAudit(r, logStore, nil, "failed", optionalAuditMessage("invalid_body"), map[string]interface{}{"reason": "decode_failed"})
			WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}
		if strings.TrimSpace(payload.Question) == "" {
			record(http.StatusBadRequest, nil)
			writeDocQAAudit(r, logStore, &payload, "failed", optionalAuditMessage("question_required"), map[string]interface{}{"reason": "blank_question"})
			WriteJSON(w, http.StatusBadRequest, "问题不能为空", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}
		// M4：kb 作用域解析（header/query/body 三来源严格一致）+ 授权求交。
		// 失败路径已写响应与拒绝审计，直接返回。
		target, scopeErr := resolveQARequestScope(r, optionalStringValue(payload.KBID), payload.KBIDs)
		if scopeErr != nil {
			record(scopeErr.Status, nil)
			writeQAScopeRejectionAudit(r, logger, logStore, "qa:ask", scopeErr)
			writeScopeError(w, scopeErr)
			return
		}
		effectiveKBIDs, ok := guard.authorizeQAEffectiveKBIDs(w, r, logStore, "qa:ask", target)
		if !ok {
			record(http.StatusForbidden, nil)
			return
		}
		if strings.TrimSpace(payload.ReasoningProfile) == "" {
			payload.ReasoningProfile = resolveScenarioReasoningProfile(r.Context(), configStore, "docqa", "balanced")
			body = encodeDocQAPayload(body, payload)
		}

		status, forwardErr := forwardOrchestratorJSONWithHeaders(w, r, logger, client, "/api/internal/docqa", body, buildScopedForwardHeaders(r, target, effectiveKBIDs), safeRetry)
		auditStatus := "success"
		errorMessage := (*string)(nil)
		if status >= http.StatusBadRequest {
			auditStatus = "failed"
			errorMessage = optionalAuditMessage("upstream_request_failed")
		}
		writeDocQAAudit(r, logStore, &payload, auditStatus, errorMessage, map[string]interface{}{
			"http_status": status,
			"forwarded":   forwardErr == nil,
			"kb_ids":      effectiveKBIDs,
			"tenant_id":   target.TenantID,
			"project_id":  target.ProjectID,
		})
		record(status, forwardErr)
	})
}

func writeDocQAAudit(
	r *http.Request,
	logStore adminLogStore,
	payload *publicDocQAPayload,
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
		question := strings.TrimSpace(payload.Question)
		if question != "" {
			if len(question) > 100 {
				question = question[:100]
			}
			details["question_preview"] = question
		}
		if payload.TopK != nil {
			details["top_k"] = *payload.TopK
		}
		if payload.RequireCitation != nil {
			details["require_citation"] = *payload.RequireCitation
		}
		if strings.TrimSpace(payload.ReasoningProfile) != "" {
			details["reasoning_profile"] = payload.ReasoningProfile
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
		Action:       "docqa_ask",
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

func buildNativeDeepResearchHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
	logStore adminLogStore,
	configStore adminConfigStore,
	safeRetry bool,
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
			metrics.Observe("/api/internal/docqa/deep-research", http.MethodPost, status, err, time.Since(start))
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
			writeDeepResearchAudit(r, logStore, nil, "failed", optionalAuditMessage("invalid_request"), map[string]interface{}{"reason": "read_body_failed"})
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}

		var payload publicDeepResearchPayload
		if err := json.NewDecoder(bytes.NewReader(body)).Decode(&payload); err != nil {
			record(http.StatusBadRequest, err)
			writeDeepResearchAudit(r, logStore, nil, "failed", optionalAuditMessage("invalid_body"), map[string]interface{}{"reason": "decode_failed"})
			WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}
		if strings.TrimSpace(payload.Question) == "" {
			record(http.StatusBadRequest, nil)
			writeDeepResearchAudit(r, logStore, &payload, "failed", optionalAuditMessage("question_required"), map[string]interface{}{"reason": "blank_question"})
			WriteJSON(w, http.StatusBadRequest, "问题不能为空", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}
		// M4：kb 作用域解析（header/query/body 三来源严格一致）+ 授权求交。
		target, scopeErr := resolveQARequestScope(r, optionalStringValue(payload.KBID), payload.KBIDs)
		if scopeErr != nil {
			record(scopeErr.Status, nil)
			writeQAScopeRejectionAudit(r, logger, logStore, "qa:ask", scopeErr)
			writeScopeError(w, scopeErr)
			return
		}
		effectiveKBIDs, ok := guard.authorizeQAEffectiveKBIDs(w, r, logStore, "qa:ask", target)
		if !ok {
			record(http.StatusForbidden, nil)
			return
		}
		if strings.TrimSpace(payload.ReasoningProfile) == "" {
			payload.ReasoningProfile = resolveScenarioReasoningProfile(r.Context(), configStore, "deep_research", "deep")
			body = encodeDocQAPayload(body, payload)
		}

		status, forwardErr := forwardOrchestratorJSONWithHeaders(w, r, logger, client, "/api/internal/docqa/deep-research", body, buildScopedForwardHeaders(r, target, effectiveKBIDs), safeRetry)
		auditStatus := "success"
		errorMessage := (*string)(nil)
		if status >= http.StatusBadRequest {
			auditStatus = "failed"
			errorMessage = optionalAuditMessage("upstream_request_failed")
		}
		writeDeepResearchAudit(r, logStore, &payload, auditStatus, errorMessage, map[string]interface{}{
			"http_status": status,
			"forwarded":   forwardErr == nil,
			"kb_ids":      effectiveKBIDs,
			"tenant_id":   target.TenantID,
			"project_id":  target.ProjectID,
		})
		record(status, forwardErr)
	})
}

func writeDeepResearchAudit(
	r *http.Request,
	logStore adminLogStore,
	payload *publicDeepResearchPayload,
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
		question := strings.TrimSpace(payload.Question)
		if question != "" {
			if len(question) > 100 {
				question = question[:100]
			}
			details["question_preview"] = question
		}
		if payload.TopK != nil {
			details["top_k"] = *payload.TopK
		}
		if payload.MaxSubQuestions != nil {
			details["max_sub_questions"] = *payload.MaxSubQuestions
		}
		if strings.TrimSpace(payload.ReasoningProfile) != "" {
			details["reasoning_profile"] = payload.ReasoningProfile
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
		Action:       "docqa_deep_research",
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

func buildNativeDocQAHealthHandler(
	logger *slog.Logger,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
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
			metrics.Observe("/api/internal/docqa/health", http.MethodGet, status, err, time.Since(start))
		}

		if r.Method != http.MethodGet {
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
		if _, ok := optionalBoolQuery(w, r, "probe_llm"); !ok {
			record(http.StatusBadRequest, nil)
			return
		}

		status, forwardErr := forwardOrchestratorJSON(w, r, logger, client, "/api/internal/docqa/health", nil, false)
		record(status, forwardErr)
	})
}

func resolveScenarioReasoningProfile(ctx context.Context, configStore adminConfigStore, scenario string, fallback string) string {
	if configStore == nil {
		return fallback
	}
	values, err := safeConfigValues(ctx, configStore, "ai_service")
	if err != nil {
		return fallback
	}
	switch scenario {
	case "docqa":
		return firstNonEmptyString(values["docqa_reasoning_profile"], fallback)
	case "deep_research":
		return firstNonEmptyString(values["deep_research_reasoning_profile"], fallback)
	case "graph_extract":
		return firstNonEmptyString(values["graph_extract_reasoning_profile"], fallback)
	case "graph_extract_complex":
		return firstNonEmptyString(values["graph_extract_complex_reasoning_profile"], fallback)
	default:
		return fallback
	}
}

func encodeDocQAPayload(original []byte, payload interface{}) []byte {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return original
	}
	return encoded
}
