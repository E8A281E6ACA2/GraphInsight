package httpserver

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/proxy"
	"graphinsight/go-backend/internal/scope"
)

type adminJobStore interface {
	ListJobs(ctx context.Context, query adminstore.JobListQuery) (adminstore.JobListResult, error)
	GetJob(ctx context.Context, jobID int) (adminstore.JobItem, error)
	ListJobLogs(ctx context.Context, jobID int, page int, pageSize int) (adminstore.JobLogListResult, error)
	CreateJob(ctx context.Context, req adminstore.JobCreateRequest) (adminstore.JobItem, error)
	EnqueueReindexChunks(ctx context.Context, req adminstore.ReindexEnqueueRequest) (adminstore.ReindexEnqueueReport, error)
	RetryJob(ctx context.Context, req adminstore.JobRetryRequest) (adminstore.JobItem, error)
	CancelJob(ctx context.Context, req adminstore.JobCancelRequest) (adminstore.JobItem, error)
}

func asAdminJobStore(store interface{}) adminJobStore {
	typed, _ := store.(adminJobStore)
	return typed
}

// requireAdminJobKBScope 是任务读取路由（list/detail/logs）的 kb 作用域强制点
// （M4 FIX #2）：kb_id 从 header/query 解析并经 internal/scope 严格归一化校验，
// 缺失 → 400 KB_SCOPE_REQUIRED，非法 → SCOPE_INVALID，多值/不一致 → KB_CROSS_SCOPE。
// 全局任务列表/详情/日志读取不再可用。
func requireAdminJobKBScope(w http.ResponseWriter, r *http.Request) (string, bool) {
	kbID, scopeErr := resolveSingleKBScopeFromRequest(r)
	if scopeErr != nil {
		writeScopeError(w, scopeErr)
		return "", false
	}
	return kbID, true
}

// authorizeJobKBReadScope 是任务读取路由的第二阶段 KB 鉴权（M4-R1 FIX #2，
// 契约 §14.2 层 2）：加载 KB 行（adminstore 为权威）后，携带
// {tenant_id, project_id, kb_id} 完整作用域重新执行 job:read 权限求交，
// 防止只有 project-a 绑定的调用方通过 kb_id 参数读取 project-b 的任务。
// 与文档路由不同：任务读取是历史数据查看，归档/删除中的 KB 不阻断（只鉴权）。
// 拒绝 → 403 KB_ACCESS_DENIED；KB 不存在 → 404 KB_NOT_FOUND。
func authorizeJobKBReadScope(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	guard businessPermissionGuard,
	kbStore adminKBStore,
	kbID string,
) (adminstore.KnowledgeBaseItem, bool) {
	if kbStore == nil {
		logger.Error("knowledge base store unavailable for job read route")
		WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	item, err := kbStore.GetKnowledgeBase(r.Context(), kbID)
	if errors.Is(err, adminstore.ErrKBNotFound) {
		WriteJSON(w, http.StatusNotFound, "知识库不存在", map[string]string{"error_code": scope.CodeKBNotFound})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if err != nil {
		logger.Error("get knowledge base for job read route failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "查询知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if scopeErr := ensureKBRequestScopeMatches(r, item); scopeErr != nil {
		writeScopeError(w, scopeErr)
		return adminstore.KnowledgeBaseItem{}, false
	}
	if !guard.checkPermissionWithScope(r, "job:read", kbScopeMap(item)) {
		writeScopeError(w, scope.ErrAccessDenied(nil))
		return adminstore.KnowledgeBaseItem{}, false
	}
	return item, true
}

// jobBelongsToKB 判断任务行是否属于目标 KB。kb_id 为空或与目标不一致均视为不属于，
// 详情/日志按“不存在”响应，避免跨 KB 存在性泄露。
func jobBelongsToKB(job adminstore.JobItem, kbID string) bool {
	if job.KBID == nil {
		return false
	}
	normalized, err := scope.NormalizeScopeID("kb_id", *job.KBID)
	if err != nil || normalized == "" {
		return false
	}
	return normalized == kbID
}

func buildAdminJobsReadNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	jobStore adminJobStore,
	kbStore adminKBStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("job:read", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if jobStore == nil {
			logger.Error("admin job store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "任务数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}
		// M4 FIX #2：list/detail/logs 全部强制 kb 作用域，先于任何 store 调用。
		kbID, ok := requireAdminJobKBScope(w, r)
		if !ok {
			return
		}
		// M4-R1 FIX #2：第二阶段按 KB 行真实作用域重新鉴权，先于任何 store 读取。
		kbItem, ok := authorizeJobKBReadScope(w, r, logger, guard, kbStore, kbID)
		if !ok {
			return
		}

		switch {
		case r.URL.Path == "/api/v1/admin/jobs":
			page := boundedIntQuery(r, "page", 1, 1, 1_000_000)
			pageSize := boundedIntQuery(r, "page_size", 20, 1, 200)
			result, err := jobStore.ListJobs(r.Context(), adminstore.JobListQuery{
				JobType: strings.TrimSpace(r.URL.Query().Get("job_type")),
				Status:  strings.TrimSpace(r.URL.Query().Get("status")),
				// 鉴权通过的 KB 行是 tenant/project 权威来源（与 kb_id 硬条件叠加，
				// 参数不一致已在 ensureKBRequestScopeMatches 处拒绝）。
				TenantID:  kbItem.TenantID,
				ProjectID: kbItem.ProjectID,
				KBID:      kbID,
				Page:      page,
				PageSize:  pageSize,
			})
			if err != nil {
				logger.Error("list admin jobs failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "查询任务列表失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
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
		case strings.HasPrefix(r.URL.Path, "/api/v1/admin/jobs/"):
			jobID, isLogsRoute, ok := parseAdminJobReadPath(r.URL.Path)
			if !ok || jobID <= 0 {
				WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			// 详情/日志先加载任务行校验 kb 归属（跨 KB 按 404 处理，防存在性泄露）。
			job, err := jobStore.GetJob(r.Context(), jobID)
			if errors.Is(err, adminstore.ErrJobNotFound) {
				WriteJSON(w, http.StatusNotFound, "任务不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if err != nil {
				logger.Error("get admin job failed", "error", err.Error())
				WriteJSON(w, http.StatusServiceUnavailable, "查询任务详情失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
				return
			}
			if !jobBelongsToKB(job, kbID) {
				WriteJSON(w, http.StatusNotFound, "任务不存在", map[string]string{"error_code": "NOT_FOUND"})
				return
			}
			if isLogsRoute {
				page := boundedIntQuery(r, "page", 1, 1, 1_000_000)
				pageSize := boundedIntQuery(r, "page_size", 50, 1, 200)
				result, err := jobStore.ListJobLogs(r.Context(), jobID, page, pageSize)
				if err != nil {
					logger.Error("list admin job logs failed", "error", err.Error())
					WriteJSON(w, http.StatusServiceUnavailable, "查询任务日志失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
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
				return
			}
			WriteJSON(w, http.StatusOK, "获取成功", job)
		default:
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	}))
}

func buildAdminJobsWriteNativeHandler(
	logger *slog.Logger,
	guard businessPermissionGuard,
	jobStore adminJobStore,
	configStore adminConfigStore,
	pythonWakeClient *proxy.Client,
	kbStore adminKBStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", guard.wrap("job:manage", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if jobStore == nil {
			logger.Error("admin job store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "任务数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		switch r.URL.Path {
		case "/api/v1/admin/jobs/reindex-chunks":
			// §16.3 的提交链刻意不走上面的 CreateJob 分支：CreateJob 是裸 INSERT，
			// 同批 targets 重复提交会各建一行 pending，去重/复用/原地 retry 全部失效。
			submitReindexChunksJob(w, r, logger, jobStore, kbStore, pythonWakeClient)
		case "/api/v1/admin/jobs/build-graph", "/api/v1/admin/jobs/clear-kb", "/api/v1/admin/jobs/reindex":
			var payload adminJobCreatePayload
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
				return
			}
			jobType := adminJobTypeFromPath(r.URL.Path)
			// 契约 §2/手册 §13.2：build_graph / clear_kb 必须携带 kb 作用域且 KB 可用；
			// reindex 是基础设施操作（Neo4j 全文索引），保持 kb 可选。
			if jobType == "build_graph" || jobType == "clear_kb" {
				kbItem, ok := requireJobKnowledgeBase(w, r, logger, kbStore, jobType, &payload)
				if !ok {
					return
				}
				freezeJobPayloadScope(&payload, kbItem, jobType)
			}
			payload.Payload = enrichAdminJobPayloadWithScenarioDefaults(r.Context(), configStore, jobType, payload.Payload)
			req := buildAdminJobCreateRequest(r, jobType, payload)
			job, err := jobStore.CreateJob(r.Context(), req)
			if !writeAdminJobMutationResult(w, logger, err, http.StatusCreated, "任务已创建", job) {
				return
			}
			nudgePythonJobWorker(r, logger, pythonWakeClient)
		default:
			if jobID, ok := parseAdminJobActionPath(r.URL.Path, "retry"); ok {
				job, err := jobStore.RetryJob(r.Context(), adminstore.JobRetryRequest{
					JobID:      jobID,
					OperatorID: optionalIntHeader(r, "x-auth-user-id"),
					TraceID:    optionalStringHeader(r, traceHeader),
					IPAddress:  optionalString(firstRemoteAddr(r)),
					UserAgent:  optionalString(r.UserAgent()),
				})
				if !writeAdminJobMutationResult(w, logger, err, http.StatusOK, "重试已提交", job) {
					return
				}
				nudgePythonJobWorker(r, logger, pythonWakeClient)
				return
			}
			if jobID, ok := parseAdminJobActionPath(r.URL.Path, "cancel"); ok {
				job, err := jobStore.CancelJob(r.Context(), adminstore.JobCancelRequest{
					JobID:      jobID,
					OperatorID: optionalIntHeader(r, "x-auth-user-id"),
					TraceID:    optionalStringHeader(r, traceHeader),
					IPAddress:  optionalString(firstRemoteAddr(r)),
					UserAgent:  optionalString(r.UserAgent()),
				})
				writeAdminJobMutationResult(w, logger, err, http.StatusOK, "任务已取消", job)
				return
			}
			WriteJSON(w, http.StatusNotFound, "资源不存在", map[string]string{"error_code": "NOT_FOUND"})
		}
	}))
}

// submitReindexChunksJob 是 §16.3 的 Go 写侧提交入口：KB 作用域强制 → targets 解析 →
// 共享去重入队（EnqueueReindexChunks）→ 按 outcome 映射响应。
//
// 成功响应体是 entry.Job（含 id / targets_hash / status / retry_count），所以"复用"与
// "原地重试"到底落在哪一行，调用方和集成测试都能靠 child_job_id 回读直接判定，
// 不需要相信任何计数。
//
// 超限拒绝用既有码 JOB_MAX_RETRIES_REACHED + HTTP 400。设计文档 §16.3 写的 409 JOB_409
// 在 Go 控制面没有实现（NOT-IMPLEMENTED：错误映射表里不存在该分支），这里不假造状态码。
func submitReindexChunksJob(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	jobStore adminJobStore,
	kbStore adminKBStore,
	pythonWakeClient *proxy.Client,
) {
	var payload adminJobCreatePayload
	if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
		WriteJSON(w, http.StatusBadRequest, "请求体错误", map[string]string{"error_code": "INVALID_BODY"})
		return
	}
	kbItem, ok := requireJobKnowledgeBase(w, r, logger, kbStore, adminstore.ReindexChunksJobType, &payload)
	if !ok {
		return
	}
	freezeJobPayloadScope(&payload, kbItem, adminstore.ReindexChunksJobType)
	targets, err := reindexTargetsFromPayload(payload.Payload)
	if err != nil {
		writeScopeError(w, &scope.Error{
			Code:    scope.CodeReindexScopeRequired,
			Message: err.Error(),
			Status:  http.StatusBadRequest,
		})
		return
	}
	maxRetries := 3
	if payload.MaxRetries != nil {
		maxRetries = *payload.MaxRetries
	}
	report, err := jobStore.EnqueueReindexChunks(r.Context(), adminstore.ReindexEnqueueRequest{
		Targets:    targets,
		Source:     "admin_api",
		TraceID:    optionalStringValue(optionalStringHeader(r, traceHeader)),
		MaxRetries: maxRetries,
		OperatorID: optionalIntHeader(r, "x-auth-user-id"),
		IPAddress:  optionalString(firstRemoteAddr(r)),
		UserAgent:  optionalString(r.UserAgent()),
	})
	if err != nil {
		writeReindexEnqueueError(w, logger, err)
		return
	}
	if len(report.Jobs) != 1 {
		// kb/doc 作用域冻结自服务端 KB 行，一次提交只可能有一个分组。分组数不是 1
		// 说明入队实现与作用域冻结脱节（例如 target 自带作用域被采信），不能返回"成功"。
		logger.Error("reindex_chunks enqueue returned unexpected group count", "groups", len(report.Jobs))
		WriteJSON(w, http.StatusServiceUnavailable, "reindex_chunks 入队结果异常", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return
	}
	entry := report.Jobs[0]
	if entry.Outcome == adminstore.ReindexOutcomeRejected {
		writeReindexRetriesExhausted(w, logger, entry, report)
		return
	}
	nudgePythonJobWorker(r, logger, pythonWakeClient)
	status, message := http.StatusOK, "任务已复用"
	if entry.Outcome == adminstore.ReindexOutcomeCreated {
		status, message = http.StatusCreated, "任务已创建"
	}
	WriteJSON(w, status, message, entry.Job)
}

// reindexTargetsFromPayload 把 §8.2 冻结的 payload 形状 `{kb_id, tenant_id, project_id,
// doc_id?, targets:[{chunk_id, target_revision}]}` 展开成入队 targets。
//
// 作用域四元组一律取自 freezeJobPayloadScope 后的 payload：target 自带 kb_id/tenant_id
// 直接忽略，否则一次提交可以夹带跨 KB 的 chunk 绕开 KB 鉴权。
// 空 targets 必须拒绝（§15.7）：入队成功后 worker 会"成功"消费掉一个什么都不重建的任务。
func reindexTargetsFromPayload(payload map[string]interface{}) ([]adminstore.ReindexTarget, error) {
	rawTargets, ok := payload["targets"].([]interface{})
	if !ok || len(rawTargets) == 0 {
		return nil, errors.New("reindex_chunks payload.targets 不能为空")
	}
	kbID := strings.TrimSpace(stringValue(payload["kb_id"]))
	docID := strings.TrimSpace(stringValue(payload["doc_id"]))
	tenantID := strings.TrimSpace(stringValue(payload["tenant_id"]))
	projectID := strings.TrimSpace(stringValue(payload["project_id"]))
	targets := make([]adminstore.ReindexTarget, 0, len(rawTargets))
	for _, raw := range rawTargets {
		item, ok := raw.(map[string]interface{})
		if !ok {
			return nil, errors.New("reindex_chunks payload.targets 必须是对象列表")
		}
		chunkID := strings.TrimSpace(stringValue(item["chunk_id"]))
		if chunkID == "" {
			return nil, errors.New("reindex_chunks 需要每条 target 都带 kb_id 与 chunk_id")
		}
		revision, ok := targetRevisionFromJSON(item["target_revision"])
		if !ok {
			return nil, fmt.Errorf("target_revision 非法: chunk_id=%s", chunkID)
		}
		if revision < 1 {
			return nil, fmt.Errorf("target_revision 必须 >= 1: chunk_id=%s", chunkID)
		}
		targets = append(targets, adminstore.ReindexTarget{
			KBID:           kbID,
			DocID:          docID,
			TenantID:       tenantID,
			ProjectID:      projectID,
			ChunkID:        chunkID,
			TargetRevision: revision,
		})
	}
	return targets, nil
}

// maxTargetRevision 是 float64 仍能精确表示的最大整数（2^53）。超过它的 JSON 数字
// 在 encoding/json 解出来时就已经是四舍五入值，拿它算 targets_hash 等于对不存在
// 的 revision 建索引，所以在入口拒绝。
const maxTargetRevision = 1 << 53

// targetRevisionFromJSON 只接受整数语义的 JSON 数字。encoding/json 把所有数字解成
// float64，而 Python 侧 `int(raw["target_revision"])` 会静默把 1.9 截断成 1、把 True 当 1；
// 两端不一致就会算出不同 targets_hash，所以小数/字符串/布尔一律在入口拒绝。
func targetRevisionFromJSON(value interface{}) (int, bool) {
	typed, ok := value.(float64)
	if !ok || typed < -maxTargetRevision || typed > maxTargetRevision {
		return 0, false
	}
	exact := int(typed)
	if float64(exact) != typed {
		return 0, false
	}
	return exact, true
}

func writeReindexEnqueueError(w http.ResponseWriter, logger *slog.Logger, err error) {
	switch {
	case errors.Is(err, adminstore.ErrReindexScopeRequired):
		writeScopeError(w, &scope.Error{
			Code:    scope.CodeReindexScopeRequired,
			Message: "reindex_chunks targets 作用域不完整",
			Status:  http.StatusBadRequest,
		})
	case errors.Is(err, adminstore.ErrJobValidation):
		WriteJSON(w, http.StatusBadRequest, "任务参数错误", map[string]string{"error_code": "INVALID_BODY"})
	case errors.Is(err, adminstore.ErrReindexEnqueueAnomaly):
		// 命中去重索引却读不到既有行：存储层处于我们无法服务的关系。判据只写在 message 与
		// Error 日志里，不降级成"参数错误"诱导调用方去改请求体。
		logger.Error("reindex_chunks enqueue anomaly", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "reindex_chunks 入队失败：命中去重索引但读不到既有任务行", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
	default:
		// 其余存储层故障（外键违例、连接断开、列不存在……）不得复用上一条文案：Wave 8 第一次
		// 实跑就是夹具缺 admin_users 行导致 requested_by 外键违例，却被回成"命中去重索引"，
		// 把排障方向整个带偏。文案只说它确定知道的那件事。
		logger.Error("reindex_chunks enqueue failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "reindex_chunks 入队失败：存储层不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
	}
}

// writeReindexRetriesExhausted 把 §16.3 的超限拒绝回给调用方，并原样带出人工介入需要的
// 三件事：哪一行（child_job_id）、哪批目标（targets_hash）、额度还剩多少。
func writeReindexRetriesExhausted(w http.ResponseWriter, logger *slog.Logger, entry adminstore.ReindexEnqueueEntry, report adminstore.ReindexEnqueueReport) {
	data := map[string]interface{}{
		"error_code":   "JOB_MAX_RETRIES_REACHED",
		"child_job_id": entry.Job.ID,
		"targets_hash": entry.TargetsHash,
		"retry_count":  entry.Job.RetryCount,
		"max_retries":  entry.Job.MaxRetries,
		"reason":       "retry_exhausted",
	}
	for _, detail := range report.RejectedDetail {
		if detail.TargetsHash != entry.TargetsHash || detail.KBID != entry.KBID {
			continue
		}
		data["retry_count"] = detail.RetryCount
		data["max_retries"] = detail.MaxRetries
		data["chunk_ids"] = detail.ChunkIDs
	}
	logger.Warn("reindex_chunks rejected: retry quota exhausted", "child_job_id", entry.Job.ID, "targets_hash", entry.TargetsHash)
	WriteJSON(w, http.StatusBadRequest, "reindex_chunks 重试额度已用尽，需人工介入后再提交", data)
}

func enrichAdminJobPayloadWithScenarioDefaults(
	ctx context.Context,
	configStore adminConfigStore,
	jobType string,
	payload map[string]interface{},
) map[string]interface{} {
	if jobType != "build_graph" {
		return payload
	}
	normalized := map[string]interface{}{}
	for key, value := range payload {
		normalized[key] = value
	}
	if strings.TrimSpace(stringValue(normalized["reasoning_profile"])) != "" {
		return normalized
	}
	complexExtraction := false
	if value, ok := normalized["complex_extraction"]; ok {
		switch typed := value.(type) {
		case bool:
			complexExtraction = typed
		case string:
			complexExtraction = strings.EqualFold(strings.TrimSpace(typed), "true")
		}
	}
	scenario := "graph_extract"
	fallback := "fast"
	if complexExtraction {
		scenario = "graph_extract_complex"
		fallback = "balanced"
	}
	normalized["reasoning_profile"] = resolveScenarioReasoningProfile(ctx, configStore, scenario, fallback)
	return normalized
}

func stringValue(value interface{}) string {
	if value == nil {
		return ""
	}
	switch typed := value.(type) {
	case string:
		return typed
	default:
		return ""
	}
}

type adminJobCreatePayload struct {
	TenantID   *string                `json:"tenant_id"`
	ProjectID  *string                `json:"project_id"`
	KBID       *string                `json:"kb_id"`
	Payload    map[string]interface{} `json:"payload"`
	MaxRetries *int                   `json:"max_retries"`
}

type publicGraphBuildPayload struct {
	Source            string   `json:"source"`
	Force             bool     `json:"force"`
	Note              *string  `json:"note"`
	DocIDs            []string `json:"doc_ids"`
	ComplexExtraction bool     `json:"complex_extraction,omitempty"`
	ReasoningProfile  string   `json:"reasoning_profile,omitempty"`
	ParserProvider    string   `json:"parser_provider,omitempty"`
}

// requireJobKnowledgeBase 是 build_graph / clear_kb 任务创建的作用域强制点（契约 §2、手册 §13.2）：
//  1. kb 作用域从 outer kb_id / payload kb_id / header x-kb-id / query kb_id 归一化解析，
//     缺失 → KB_SCOPE_REQUIRED；格式非法 → SCOPE_INVALID；多来源不一致 → KB_CROSS_SCOPE。
//  2. KB 必须存在（adminstore 为权威）：不存在 → 404 KB_NOT_FOUND。
//  3. KB 状态必须允许知识任务：archived → KB_ARCHIVED，deleting → KB_INVALID_STATE。
//  4. 请求自带 tenant/project/kb scope 与 KB 行不一致 → KB_CROSS_SCOPE。
func requireJobKnowledgeBase(
	w http.ResponseWriter,
	r *http.Request,
	logger *slog.Logger,
	kbStore adminKBStore,
	jobType string,
	payload *adminJobCreatePayload,
) (adminstore.KnowledgeBaseItem, bool) {
	if kbStore == nil {
		logger.Error("knowledge base store unavailable for scoped job creation", "job_type", jobType)
		WriteJSON(w, http.StatusServiceUnavailable, "知识库数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	payloadKBID := ""
	if payload.Payload != nil {
		payloadKBID = stringValue(payload.Payload["kb_id"])
	}
	kbID, scopeErr := resolveJobKBScope(
		optionalStringValue(payload.KBID),
		payloadKBID,
		r.Header.Get("x-kb-id"),
		r.URL.Query().Get("kb_id"),
	)
	if scopeErr != nil {
		writeScopeError(w, scopeErr)
		return adminstore.KnowledgeBaseItem{}, false
	}
	item, err := kbStore.GetKnowledgeBase(r.Context(), kbID)
	if errors.Is(err, adminstore.ErrKBNotFound) {
		WriteJSON(w, http.StatusNotFound, "知识库不存在", map[string]string{"error_code": scope.CodeKBNotFound})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if err != nil {
		logger.Error("get knowledge base for job failed", "job_type", jobType, "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "查询知识库失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	switch item.Status {
	case adminstore.KBStatusArchived:
		WriteJSON(w, http.StatusConflict, "知识库已归档，禁止创建知识任务", map[string]string{"error_code": scope.CodeArchived})
		return adminstore.KnowledgeBaseItem{}, false
	case adminstore.KBStatusDeleting:
		WriteJSON(w, http.StatusConflict, "知识库正在删除，禁止创建知识任务", map[string]string{"error_code": "KB_INVALID_STATE"})
		return adminstore.KnowledgeBaseItem{}, false
	}
	if scopeErr := ensureKBRequestScopeMatches(r, item); scopeErr != nil {
		writeScopeError(w, scopeErr)
		return adminstore.KnowledgeBaseItem{}, false
	}
	return item, true
}

// freezeJobPayloadScope 把 KB 行的权威作用域冻结进任务 payload 与任务行
// （tenant/project 以服务端 KB 行为准，不信任客户端），doc_ids 原样透传；
// clear_kb 补齐 purge_graph 默认值，保证 Python worker 读到的 envelope 完整。
func freezeJobPayloadScope(payload *adminJobCreatePayload, item adminstore.KnowledgeBaseItem, jobType string) {
	if payload.Payload == nil {
		payload.Payload = map[string]interface{}{}
	}
	payload.Payload["kb_id"] = item.ID
	payload.Payload["tenant_id"] = item.TenantID
	payload.Payload["project_id"] = item.ProjectID
	if jobType == "clear_kb" {
		if _, exists := payload.Payload["purge_graph"]; !exists {
			payload.Payload["purge_graph"] = true
		}
	}
	payload.KBID = optionalString(item.ID)
	payload.TenantID = optionalString(item.TenantID)
	payload.ProjectID = optionalString(item.ProjectID)
}

func buildAdminJobCreateRequest(r *http.Request, jobType string, payload adminJobCreatePayload) adminstore.JobCreateRequest {
	maxRetries := 3
	if payload.MaxRetries != nil {
		maxRetries = *payload.MaxRetries
	}
	return adminstore.JobCreateRequest{
		JobType:     jobType,
		TenantID:    trimOptionalString(payload.TenantID),
		ProjectID:   trimOptionalString(payload.ProjectID),
		KBID:        trimOptionalString(payload.KBID),
		Payload:     payload.Payload,
		MaxRetries:  maxRetries,
		RequestedBy: optionalIntHeader(r, "x-auth-user-id"),
		TraceID:     optionalStringHeader(r, traceHeader),
		IPAddress:   optionalString(firstRemoteAddr(r)),
		UserAgent:   optionalString(r.UserAgent()),
	}
}

func buildNativeGraphBuildJobHandler(
	logger *slog.Logger,
	jobStore adminJobStore,
	configStore adminConfigStore,
	pythonWakeClient *proxy.Client,
	store *idempotencyStore,
	kbStore adminKBStore,
) http.HandlerFunc {
	return withRouteOwner("go-native", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			WriteJSON(w, http.StatusMethodNotAllowed, "Method not allowed", nil)
			return
		}
		if jobStore == nil {
			logger.Error("admin job store unavailable")
			WriteJSON(w, http.StatusServiceUnavailable, "任务数据服务不可用", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
			return
		}

		body, err := io.ReadAll(r.Body)
		if err != nil {
			WriteJSON(w, http.StatusBadRequest, "无效请求体", map[string]string{"error_code": "INVALID_REQUEST"})
			return
		}

		idempotencyKey := getIdempotencyKey(r)
		executor := func() (int, []byte, error) {
			var payload publicGraphBuildPayload
			if strings.TrimSpace(string(body)) != "" {
				if err := json.NewDecoder(bytes.NewReader(body)).Decode(&payload); err != nil {
					return marshalAPIResponse(
						http.StatusBadRequest,
						"请求体错误",
						map[string]string{"error_code": "INVALID_BODY"},
						r.Header.Get(traceHeader),
					)
				}
			}
			// M3 作用域强制点（契约 §2）：公开建图入口同样必须携带 kb 作用域，
			// KB 必须存在且处于 active 状态；payload 冻结 KB 行的权威作用域。
			kbItem, scopeErr := loadKBForGraphBuildJob(r, kbStore, logger)
			if scopeErr != nil {
				return marshalAPIResponse(
					scopeErr.Status,
					scopeErr.Message,
					map[string]string{"error_code": scopeErr.Code},
					r.Header.Get(traceHeader),
				)
			}
			if strings.TrimSpace(payload.ReasoningProfile) == "" {
				scenario := "graph_extract"
				fallback := "fast"
				if payload.ComplexExtraction {
					scenario = "graph_extract_complex"
					fallback = "balanced"
				}
				payload.ReasoningProfile = resolveScenarioReasoningProfile(r.Context(), configStore, scenario, fallback)
			}

			job, err := jobStore.CreateJob(r.Context(), buildPublicGraphBuildJobCreateRequest(r, payload, kbItem))
			if errors.Is(err, adminstore.ErrJobValidation) {
				return marshalAPIResponse(
					http.StatusBadRequest,
					"任务参数错误",
					map[string]string{"error_code": "INVALID_BODY"},
					r.Header.Get(traceHeader),
				)
			}
			if err != nil {
				logger.Error("public graph build job create failed", "error", err.Error())
				return marshalAPIResponse(
					http.StatusServiceUnavailable,
					"任务创建失败",
					map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"},
					r.Header.Get(traceHeader),
				)
			}

			nudgePythonJobWorker(r, logger, pythonWakeClient)
			return marshalAPIResponse(
				http.StatusOK,
				"建图任务已提交",
				map[string]interface{}{
					"job_id":  job.ID,
					"status":  "queued",
					"message": "建图任务已提交，请在任务中心查看进度",
					"job":     job,
				},
				r.Header.Get(traceHeader),
			)
		}

		var (
			status   int
			respBody []byte
			execErr  error
		)
		if store == nil || idempotencyKey == "" {
			status, respBody, execErr = executor()
		} else {
			status, respBody, execErr = store.execute(r.Context(), idempotencyKey, body, executor)
		}
		if execErr != nil {
			if errors.Is(execErr, ErrIdempotencyConflict) {
				WriteJSON(w, http.StatusConflict, "幂等键与请求体不一致", map[string]interface{}{
					"error_code":      "IDEMPOTENCY_KEY_CONFLICT",
					"idempotency_key": idempotencyKey,
				})
				return
			}
			logger.Error("public graph build job request failed", "error", execErr.Error())
			WriteJSON(w, http.StatusBadGateway, "建图任务提交失败", map[string]string{"error_code": "UPSTREAM_REQUEST_FAILED"})
			return
		}

		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		if idempotencyKey != "" {
			w.Header().Set("X-Idempotency-Key", idempotencyKey)
		}
		w.WriteHeader(status)
		_, _ = w.Write(respBody)
	})
}

// loadKBForGraphBuildJob 公开建图入口的 KB 加载（scope 解析 → 存在校验 → active 校验）。
// 返回 *scope.Error 时 handler 直接映射为统一错误响应。
func loadKBForGraphBuildJob(r *http.Request, kbStore adminKBStore, logger *slog.Logger) (adminstore.KnowledgeBaseItem, *scope.Error) {
	kbID, scopeErr := resolveJobKBScope(
		r.Header.Get("x-kb-id"),
		r.URL.Query().Get("kb_id"),
	)
	if scopeErr != nil {
		return adminstore.KnowledgeBaseItem{}, scopeErr
	}
	if kbStore == nil {
		logger.Error("knowledge base store unavailable for graph build job")
		return adminstore.KnowledgeBaseItem{}, &scope.Error{
			Code:    "ADMIN_STORE_UNAVAILABLE",
			Message: "知识库数据服务不可用",
			Status:  http.StatusServiceUnavailable,
		}
	}
	item, err := kbStore.GetKnowledgeBase(r.Context(), kbID)
	if errors.Is(err, adminstore.ErrKBNotFound) {
		return adminstore.KnowledgeBaseItem{}, &scope.Error{
			Code:    scope.CodeKBNotFound,
			Message: "知识库不存在",
			Status:  http.StatusNotFound,
		}
	}
	if err != nil {
		logger.Error("get knowledge base for graph build failed", "error", err.Error())
		return adminstore.KnowledgeBaseItem{}, &scope.Error{
			Code:    "ADMIN_STORE_UNAVAILABLE",
			Message: "查询知识库失败",
			Status:  http.StatusServiceUnavailable,
		}
	}
	switch item.Status {
	case adminstore.KBStatusArchived:
		return adminstore.KnowledgeBaseItem{}, &scope.Error{
			Code:    scope.CodeArchived,
			Message: "知识库已归档，禁止建图",
			Status:  http.StatusConflict,
		}
	case adminstore.KBStatusDeleting:
		return adminstore.KnowledgeBaseItem{}, &scope.Error{
			Code:    "KB_INVALID_STATE",
			Message: "知识库正在删除，禁止建图",
			Status:  http.StatusConflict,
		}
	}
	if scopeErr := ensureKBRequestScopeMatches(r, item); scopeErr != nil {
		return adminstore.KnowledgeBaseItem{}, scopeErr
	}
	return item, nil
}

func buildPublicGraphBuildJobCreateRequest(r *http.Request, payload publicGraphBuildPayload, kbItem adminstore.KnowledgeBaseItem) adminstore.JobCreateRequest {
	source := strings.TrimSpace(payload.Source)
	if source == "" {
		source = "documents"
	}

	docIDs := make([]string, 0, len(payload.DocIDs))
	for _, item := range payload.DocIDs {
		trimmed := strings.TrimSpace(item)
		if trimmed == "" {
			continue
		}
		docIDs = append(docIDs, trimmed)
	}

	jobPayload := map[string]interface{}{
		"source":             source,
		"force":              payload.Force,
		"doc_ids":            docIDs,
		"complex_extraction": payload.ComplexExtraction,
		// KB 行是作用域权威：tenant/project/kb 冻结自服务端，不信任客户端。
		"kb_id":      kbItem.ID,
		"tenant_id":  kbItem.TenantID,
		"project_id": kbItem.ProjectID,
	}
	if profile := strings.TrimSpace(payload.ReasoningProfile); profile != "" {
		jobPayload["reasoning_profile"] = profile
	}
	if parserProvider := strings.TrimSpace(payload.ParserProvider); parserProvider != "" {
		jobPayload["parser_provider"] = parserProvider
	}
	if note := trimOptionalString(payload.Note); note != nil {
		jobPayload["note"] = *note
	}

	return adminstore.JobCreateRequest{
		JobType:     "build_graph",
		TenantID:    optionalString(kbItem.TenantID),
		ProjectID:   optionalString(kbItem.ProjectID),
		KBID:        optionalString(kbItem.ID),
		Payload:     jobPayload,
		MaxRetries:  3,
		RequestedBy: optionalIntHeader(r, "x-auth-user-id"),
		TraceID:     optionalStringHeader(r, traceHeader),
		IPAddress:   optionalString(firstRemoteAddr(r)),
		UserAgent:   optionalString(r.UserAgent()),
	}
}

func marshalAPIResponse(status int, message string, data interface{}, traceID string) (int, []byte, error) {
	body, err := json.Marshal(APIResponse{
		Code:      status,
		Message:   message,
		Data:      data,
		Timestamp: time.Now().UTC().Format(time.RFC3339),
		TraceID:   traceID,
	})
	if err != nil {
		return 0, nil, err
	}
	return status, body, nil
}

func adminJobTypeFromPath(path string) string {
	switch path {
	case "/api/v1/admin/jobs/build-graph":
		return "build_graph"
	case "/api/v1/admin/jobs/clear-kb":
		return "clear_kb"
	case "/api/v1/admin/jobs/reindex":
		return "reindex"
	default:
		return ""
	}
}

func parseAdminJobActionPath(path string, action string) (int, bool) {
	rest := strings.TrimPrefix(path, "/api/v1/admin/jobs/")
	suffix := ":" + action
	if rest == "" || !strings.HasSuffix(rest, suffix) {
		return 0, false
	}
	rawID := strings.TrimSuffix(rest, suffix)
	if rawID == "" || strings.Contains(rawID, "/") || strings.Contains(rawID, ":") {
		return 0, false
	}
	id, err := strconv.Atoi(rawID)
	if err != nil || id <= 0 {
		return 0, false
	}
	return id, true
}

func trimOptionalString(value *string) *string {
	if value == nil {
		return nil
	}
	trimmed := strings.TrimSpace(*value)
	if trimmed == "" {
		return nil
	}
	return &trimmed
}

func nudgePythonJobWorker(r *http.Request, logger *slog.Logger, pythonWakeClient *proxy.Client) {
	if pythonWakeClient == nil {
		return
	}

	wakeReq := httptest.NewRequest(http.MethodPost, "/api/internal/jobs/wake", strings.NewReader("{}"))
	wakeReq = wakeReq.WithContext(r.Context())
	for key, values := range r.Header {
		wakeReq.Header.Del(key)
		for _, value := range values {
			wakeReq.Header.Add(key, value)
		}
	}
	wakeReq.Header.Set("Content-Type", "application/json; charset=utf-8")
	wakeReq.Header.Set("Accept", "application/json")

	resp, err := pythonWakeClient.Capture(wakeReq)
	if err != nil {
		logger.Warn("python job worker wake failed", "error", err.Error())
		return
	}
	if resp.StatusCode >= http.StatusBadRequest {
		logger.Warn("python job worker wake returned error status", "status", resp.StatusCode)
	}
}

func writeAdminJobMutationResult(w http.ResponseWriter, logger *slog.Logger, err error, successStatus int, successMessage string, item adminstore.JobItem) bool {
	if errors.Is(err, adminstore.ErrJobNotFound) {
		WriteJSON(w, http.StatusNotFound, "任务不存在", map[string]string{"error_code": "NOT_FOUND"})
		return false
	}
	if errors.Is(err, adminstore.ErrJobValidation) {
		WriteJSON(w, http.StatusBadRequest, "任务参数错误", map[string]string{"error_code": "INVALID_BODY"})
		return false
	}
	if errors.Is(err, adminstore.ErrJobInvalidTransition) {
		WriteJSON(w, http.StatusBadRequest, "当前任务状态不支持该操作", map[string]string{"error_code": "INVALID_JOB_STATUS"})
		return false
	}
	if errors.Is(err, adminstore.ErrJobMaxRetriesReached) {
		WriteJSON(w, http.StatusBadRequest, "任务已达到最大重试次数", map[string]string{"error_code": "JOB_MAX_RETRIES_REACHED"})
		return false
	}
	if err != nil {
		logger.Error("admin job mutation failed", "error", err.Error())
		WriteJSON(w, http.StatusServiceUnavailable, "任务操作失败", map[string]string{"error_code": "ADMIN_STORE_UNAVAILABLE"})
		return false
	}
	WriteJSON(w, successStatus, successMessage, item)
	return true
}

func parseAdminJobReadPath(path string) (jobID int, isLogsRoute bool, ok bool) {
	rest := strings.TrimPrefix(path, "/api/v1/admin/jobs/")
	if rest == "" || strings.Contains(rest, ":") {
		return 0, false, false
	}
	if strings.HasSuffix(rest, "/logs") {
		rawID := strings.TrimSuffix(rest, "/logs")
		rawID = strings.TrimSuffix(rawID, "/")
		id, err := strconv.Atoi(rawID)
		if err != nil {
			return 0, false, false
		}
		return id, true, true
	}
	if strings.Contains(rest, "/") {
		return 0, false, false
	}
	id, err := strconv.Atoi(rest)
	if err != nil {
		return 0, false, false
	}
	return id, false, true
}
