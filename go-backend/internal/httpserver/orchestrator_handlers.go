package httpserver

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"graphinsight/go-backend/internal/orchestrator"
	"graphinsight/go-backend/internal/scope"
)

func buildOrchestratorHandler(
	logger *slog.Logger,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
	safeRetry bool,
	method string,
	path string,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-orchestrator")
		start := time.Now()
		recorded := false
		record := func(status int, err error) {
			if recorded {
				return
			}
			recorded = true
			metrics.Observe(path, method, status, err, time.Since(start))
		}

		if r.Method != method {
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
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}

		status, forwardErr := forwardOrchestratorJSON(w, r, logger, client, path, body, safeRetry)
		record(status, forwardErr)
	}
}

func buildOrchestratorIdempotentJSONHandler(
	logger *slog.Logger,
	client *orchestrator.Client,
	clientErr error,
	store *idempotencyStore,
	metrics *orchestratorMetrics,
	upstreamTimeout time.Duration,
	method string,
	path string,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-orchestrator")
		start := time.Now()
		recorded := false
		record := func(status int, err error) {
			if recorded {
				return
			}
			recorded = true
			metrics.Observe(path, method, status, err, time.Since(start))
		}

		if r.Method != method {
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
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}

		idempotencyKey := getIdempotencyKey(r)
		status, respBody, execErr := store.execute(r.Context(), idempotencyKey, body, func() (int, []byte, error) {
			headers := buildForwardHeaders(r)
			if strings.TrimSpace(idempotencyKey) != "" {
				headers["x-idempotency-key"] = idempotencyKey
			}
			return client.DoJSONWithOptions(
				r.Context(),
				r.Method,
				path,
				r.URL.RawQuery,
				body,
				headers,
				orchestrator.RequestOptions{Timeout: upstreamTimeout},
			)
		})
		if execErr != nil {
			if errors.Is(execErr, ErrIdempotencyConflict) {
				record(http.StatusConflict, execErr)
				WriteJSON(w, http.StatusConflict, "幂等键与请求体不一致", map[string]interface{}{
					"error_code":      "IDEMPOTENCY_KEY_CONFLICT",
					"idempotency_key": idempotencyKey,
				})
				return
			}
			record(http.StatusBadGateway, execErr)
			logger.Error("idempotent orchestrator request failed", "path", path, "error", execErr.Error())
			WriteJSON(w, http.StatusBadGateway, "上游请求失败", map[string]interface{}{
				"error_code": "UPSTREAM_REQUEST_FAILED",
				"upstream":   "python-backend",
				"message":    execErr.Error(),
			})
			return
		}

		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		if idempotencyKey != "" {
			w.Header().Set("X-Idempotency-Key", idempotencyKey)
		}
		w.WriteHeader(status)
		_, _ = w.Write(respBody)
		record(status, nil)
	}
}

func buildOrchestratorPassthroughPathHandler(
	logger *slog.Logger,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
	method string,
	pathPrefix string,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-orchestrator")
		start := time.Now()
		recorded := false
		record := func(status int, err error) {
			if recorded {
				return
			}
			recorded = true
			metrics.Observe(pathPrefix, method, status, err, time.Since(start))
		}

		if r.Method != method {
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
		if pathPrefix != "" && len(r.URL.Path) < len(pathPrefix) {
			record(http.StatusNotFound, nil)
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}

		body, err := io.ReadAll(r.Body)
		if err != nil {
			record(http.StatusBadRequest, err)
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}
		status, forwardErr := forwardOrchestratorJSON(w, r, logger, client, r.URL.Path, body, false)
		record(status, forwardErr)
	}
}

func buildOrchestratorMappedPathHandler(
	logger *slog.Logger,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
	method string,
	publicPrefix string,
	targetPrefix string,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-orchestrator")
		start := time.Now()
		recorded := false
		record := func(status int, err error) {
			if recorded {
				return
			}
			recorded = true
			metrics.Observe(targetPrefix, method, status, err, time.Since(start))
		}

		if r.Method != method {
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
		if !strings.HasPrefix(r.URL.Path, publicPrefix) {
			record(http.StatusNotFound, nil)
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
			return
		}

		body, err := io.ReadAll(r.Body)
		if err != nil {
			record(http.StatusBadRequest, err)
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]interface{}{"error_code": "INVALID_REQUEST"})
			return
		}

		targetPath := targetPrefix + strings.TrimPrefix(r.URL.Path, publicPrefix)
		status, forwardErr := forwardOrchestratorJSON(w, r, logger, client, targetPath, body, false)
		record(status, forwardErr)
	}
}

func buildOrchestratorUploadHandler(
	logger *slog.Logger,
	client *orchestrator.Client,
	clientErr error,
	metrics *orchestratorMetrics,
	method string,
	path string,
) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		markRouteOwner(w, "go-orchestrator")
		start := time.Now()
		recorded := false
		record := func(status int, err error) {
			if recorded {
				return
			}
			recorded = true
			metrics.Observe(path, method, status, err, time.Since(start))
		}

		if r.Method != method {
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

		headers := buildForwardHeaders(r)
		status, respBody, err := client.DoStream(
			r.Context(),
			r.Method,
			path,
			r.URL.RawQuery,
			r.Body,
			r.Header.Get("Content-Type"),
			headers,
		)
		if err != nil {
			record(http.StatusBadGateway, err)
			logger.Error("orchestrator upload failed", "path", path, "error", err.Error())
			WriteJSON(w, http.StatusBadGateway, "上游请求失败", map[string]interface{}{
				"error_code": "UPSTREAM_REQUEST_FAILED",
				"upstream":   "python-backend",
				"message":    err.Error(),
			})
			return
		}

		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(status)
		_, _ = w.Write(respBody)
		record(status, nil)
	}
}

func forwardOrchestratorJSON(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	client *orchestrator.Client,
	path string,
	body []byte,
	safeRetry bool,
) (int, error) {
	return forwardOrchestratorJSONWithHeaders(w, r, logger, client, path, body, buildForwardHeaders(r), safeRetry)
}

// forwardOrchestratorJSONWithHeaders 允许调用方显式指定转发头
// （QA/NL2Cypher 需要把解析+授权后的有效 kb 范围转发给 Python，契约 §3.3）。
func forwardOrchestratorJSONWithHeaders(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	client *orchestrator.Client,
	path string,
	body []byte,
	headers map[string]string,
	safeRetry bool,
) (int, error) {
	status, respBody, err := client.DoJSONWithOptions(
		r.Context(),
		r.Method,
		path,
		r.URL.RawQuery,
		body,
		headers,
		orchestrator.RequestOptions{Retryable: safeRetry},
	)
	if err != nil {
		logger.Error("orchestrator request failed", "path", path, "error", err.Error())
		WriteJSON(w, http.StatusBadGateway, "上游请求失败", map[string]interface{}{
			"error_code": "UPSTREAM_REQUEST_FAILED",
			"upstream":   "python-backend",
			"message":    err.Error(),
		})
		return http.StatusBadGateway, err
	}

	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write(respBody)
	return status, nil
}

func buildForwardHeaders(r *http.Request) map[string]string {
	traceID := strings.TrimSpace(r.Header.Get("X-Trace-Id"))
	if traceID == "" {
		traceID = newForwardTraceID()
	}
	return map[string]string{
		"Authorization":      r.Header.Get("Authorization"),
		"x-tenant-id":        r.Header.Get("x-tenant-id"),
		"x-project-id":       r.Header.Get("x-project-id"),
		"x-kb-id":            r.Header.Get("x-kb-id"),
		"X-Trace-Id":         traceID,
		"x-auth-user-id":     r.Header.Get("x-auth-user-id"),
		"x-auth-user-name":   r.Header.Get("x-auth-user-name"),
		"x-auth-user-email":  r.Header.Get("x-auth-user-email"),
		"x-authz-permission": r.Header.Get("x-authz-permission"),
		"x-authz-reason":     r.Header.Get("x-authz-reason"),
	}
}

// buildScopedForwardHeaders 在 buildForwardHeaders 基础上以服务端解析并授权后的
// 有效作用域覆盖转发头（M4）：x-tenant-id/x-project-id 来自解析出的 SearchTarget，
// x-kb-ids 为逗号连接的有效 KB 集合（请求 ∩ 授权）；单一 KB 时同时携带 x-kb-id。
// Python 侧独立校验 payload，本头仅用于链路内 scope 传递。
func buildScopedForwardHeaders(r *http.Request, target *scope.SearchTarget, effectiveKBIDs []string) map[string]string {
	headers := buildForwardHeaders(r)
	if target != nil {
		if strings.TrimSpace(target.TenantID) != "" {
			headers["x-tenant-id"] = target.TenantID
		}
		if strings.TrimSpace(target.ProjectID) != "" {
			headers["x-project-id"] = target.ProjectID
		}
	}
	if len(effectiveKBIDs) == 1 {
		headers["x-kb-id"] = effectiveKBIDs[0]
	}
	if len(effectiveKBIDs) > 0 {
		headers["x-kb-ids"] = strings.Join(effectiveKBIDs, ",")
	}
	return headers
}

func newForwardTraceID() string {
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err == nil {
		return hex.EncodeToString(buf[:])
	}
	return time.Now().UTC().Format("20060102T150405.000000000")
}

func getIdempotencyKey(r *http.Request) string {
	v := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if v != "" {
		return v
	}
	return strings.TrimSpace(r.Header.Get("x-idempotency-key"))
}
