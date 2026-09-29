package httpserver

import (
	"bytes"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"graphinsight/go-backend/internal/proxy"
)

type adminRetrievalDiagnosticsPayload struct {
	Question string   `json:"question"`
	KBID     *string  `json:"kb_id,omitempty"`
	KBIDs    []string `json:"kb_ids,omitempty"`
	TopK     int      `json:"top_k,omitempty"`
	Modes    []string `json:"modes,omitempty"`
}

func buildAdminRetrievalDiagnosticsHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	pythonClient *proxy.Client,
	kbStore adminKBStore,
	logStore adminLogStore,
) http.HandlerFunc {
	return withRouteOwner("go-control-plane", guard.wrap("qa:ask", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if pythonClient == nil {
			WriteJSON(w, http.StatusServiceUnavailable, "上游服务不可用", map[string]interface{}{
				"error_code": "UPSTREAM_UNAVAILABLE",
				"upstream":   "python-backend",
			})
			return
		}

		body, err := io.ReadAll(r.Body)
		if err != nil {
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}
		var payload adminRetrievalDiagnosticsPayload
		if err := json.NewDecoder(bytes.NewReader(body)).Decode(&payload); err != nil {
			WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}
		payload.Question = strings.TrimSpace(payload.Question)
		if payload.Question == "" {
			WriteJSON(w, http.StatusBadRequest, "问题不能为空", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}
		if len(payload.Question) > 2000 {
			WriteJSON(w, http.StatusBadRequest, "问题长度超过限制", map[string]interface{}{"error_code": "INVALID_BODY"})
			return
		}

		// M4-R1 审计 P0-1：第二阶段 KB 授权完整链（解析 scope -> 加载 KB 权威行 ->
		// 校验 tenant/project/kb 一致 -> 权限求交）。任一步失败已在内部写出响应与
		// 拒绝审计并返回 false，此时绝不能转发 Python（拒绝路径不调用上游）。
		target, effectiveKBIDs, ok := guard.authorizeQAKBScopedRequest(
			w, r, logStore, kbStore, "qa:ask",
			optionalStringValue(payload.KBID), payload.KBIDs,
		)
		if !ok {
			return
		}

		if payload.TopK <= 0 {
			payload.TopK = 5
		}
		if payload.TopK > 20 {
			payload.TopK = 20
		}
		payload.Modes = normalizeRetrievalDiagnosticModes(payload.Modes)
		// 转发服务端规范化后的作用域：清空客户端自报 kb_id，kb_ids 置为授权求交结果。
		payload.KBID = nil
		payload.KBIDs = effectiveKBIDs
		forwardBody, err := json.Marshal(payload)
		if err != nil {
			WriteJSON(w, http.StatusInternalServerError, "构造诊断请求失败", map[string]interface{}{"error_code": "INTERNAL_ERROR"})
			return
		}

		upstreamReq, err := http.NewRequestWithContext(
			r.Context(),
			http.MethodPost,
			"/api/internal/docqa/retrieval-diagnostics",
			bytes.NewReader(forwardBody),
		)
		if err != nil {
			WriteJSON(w, http.StatusInternalServerError, "构造诊断请求失败", map[string]interface{}{"error_code": "INTERNAL_ERROR"})
			return
		}
		for key, values := range r.Header {
			upstreamReq.Header.Del(key)
			for _, value := range values {
				upstreamReq.Header.Add(key, value)
			}
		}
		// 以服务端规范化后的作用域覆盖客户端自报的 scope 头，避免透传未校验的 kb/project。
		for key, value := range buildScopedForwardHeaders(r, target, effectiveKBIDs) {
			switch key {
			case "x-tenant-id", "x-project-id", "x-kb-id", "x-kb-ids":
				upstreamReq.Header.Del(key)
				if strings.TrimSpace(value) != "" {
					upstreamReq.Header.Set(key, value)
				}
			}
		}
		upstreamReq.Header.Set("Content-Type", "application/json; charset=utf-8")
		upstreamReq.Header.Set("Accept", "application/json")

		resp, err := pythonClient.Capture(upstreamReq)
		if err != nil {
			logger.Error("retrieval diagnostics upstream request failed", "error", err.Error())
			WriteJSON(w, http.StatusBadGateway, "上游请求失败", map[string]interface{}{
				"error_code": "UPSTREAM_REQUEST_FAILED",
				"upstream":   "python-backend",
				"message":    err.Error(),
			})
			return
		}
		resp.WriteTo(w)
	}))
}

func normalizeRetrievalDiagnosticModes(values []string) []string {
	allowed := map[string]struct{}{
		"keyword":      {},
		"vector":       {},
		"hybrid":       {},
		"graph_hybrid": {},
	}
	result := make([]string, 0, 4)
	seen := map[string]struct{}{}
	for _, value := range values {
		mode := strings.ToLower(strings.TrimSpace(value))
		if _, ok := allowed[mode]; !ok {
			continue
		}
		if _, ok := seen[mode]; ok {
			continue
		}
		seen[mode] = struct{}{}
		result = append(result, mode)
	}
	if len(result) == 0 {
		return []string{"keyword", "vector", "hybrid", "graph_hybrid"}
	}
	return result
}
