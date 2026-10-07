package httpserver

import (
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
)

// Wave 5（P1）：钉死 §16.3 里"超限/非法参数"的 HTTP 语义。
//
// 本包在 Windows 上因 syscall.Statfs_t 不可编译（既有缺陷，见 Wave 4 审计包 §4），
// 这两个测试只能在 linux 上跑（CI ci.yml:103 `go test ./...`；本地用 golang 容器取证）。

func decodeAdminJobResponse(t *testing.T, rec *httptest.ResponseRecorder) APIResponse {
	t.Helper()
	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	return resp
}

func errorCodeOf(t *testing.T, resp APIResponse) string {
	t.Helper()
	data, ok := resp.Data.(map[string]interface{})
	if !ok {
		t.Fatalf("expected object data, got %#v", resp.Data)
	}
	code, _ := data["error_code"].(string)
	return code
}

// 作业状态错误到 HTTP 的映射是 §16.3 口径的唯一落点：409 不存在，
// ErrJobValidation 走 400 INVALID_BODY。
func TestWriteAdminJobMutationResultMapsValidationToBadRequest(t *testing.T) {
	t.Parallel()

	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	item := adminstore.JobItem{ID: 7, JobType: "reindex_chunks", Status: "pending", Payload: map[string]interface{}{}, CreatedAt: time.Now().UTC()}

	cases := []struct {
		name       string
		err        error
		wantStatus int
		wantCode   string
	}{
		{"ErrJobValidation -> 400 INVALID_BODY", adminstore.ErrJobValidation, http.StatusBadRequest, "INVALID_BODY"},
		{"同文案但非哨兵的错误 -> 503（证明按 errors.Is 精确匹配，不按字符串）", errors.New("wrapped: " + adminstore.ErrJobValidation.Error()), http.StatusServiceUnavailable, "ADMIN_STORE_UNAVAILABLE"},
		{"ErrJobNotFound -> 404", adminstore.ErrJobNotFound, http.StatusNotFound, "NOT_FOUND"},
		{"无错误 -> 201", nil, http.StatusCreated, ""},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			rec := httptest.NewRecorder()
			if ok := writeAdminJobMutationResult(rec, logger, tc.err, http.StatusCreated, "任务已创建", item); ok != (tc.err == nil) {
				t.Fatalf("unexpected handled flag: %v (err=%v)", ok, tc.err)
			}
			resp := decodeAdminJobResponse(t, rec)
			if rec.Code != tc.wantStatus || resp.Code != tc.wantStatus {
				t.Fatalf("want status %d, got http=%d body=%d", tc.wantStatus, rec.Code, resp.Code)
			}
			if got := errorCodeOf(t, resp); got != tc.wantCode {
				t.Fatalf("want error_code %q, got %q", tc.wantCode, got)
			}
			// 409 语义在本仓库不存在（P3 定案）：任何分支都不得返回 Conflict。
			if rec.Code == http.StatusConflict {
				t.Fatalf("§16.3 的 409 映射未实现，出现 409 即为口径漂移")
			}
		})
	}
}

// 真路由取证：POST /api/v1/admin/jobs/build-graph 时 store 侧返回 ErrJobValidation，
// 端到端必须是 400 INVALID_BODY，而不是 409 或 5xx。
func TestAdminJobCreateRouteReturnsBadRequestOnStoreValidation(t *testing.T) {
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
		t.Fatalf("store 拒绝时不得唤醒 python worker")
	})

	mux := http.NewServeMux()
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	guard := newSoftKBGuardForTest(logger)
	store := &fakeAdminJobStore{createErr: adminstore.ErrJobValidation}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, guard, store)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/build-graph", strings.NewReader(`{"kb_id":"kb-1","payload":{"doc_ids":["d1"]}}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400, got %d body=%s", rec.Code, rec.Body.String())
	}
	resp := decodeAdminJobResponse(t, rec)
	if got := errorCodeOf(t, resp); got != "INVALID_BODY" {
		t.Fatalf("want INVALID_BODY, got %q", got)
	}
}

// §16.3 的 HTTP 入口口径：未知作业类型在**路由分派**处就被拦掉（404 NOT_FOUND），
// 请求从未抵达 store.CreateJob —— adminstore 白名单是第二道线，不是入口防线。
// paired 用例（已知类型必须真的抵达 store）用于证明"未触达"不是路由整体失配的假绿。
func TestAdminJobCreateRouteDispatchesByPathType(t *testing.T) {
	buildMux := func(t *testing.T, store *fakeAdminJobStore) *http.ServeMux {
		t.Helper()
		pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {
			// 本测试只断言 CreateJob 是否被调用，worker 唤醒一律吞掉。
		})
		mux := http.NewServeMux()
		logger := slog.New(slog.NewTextHandler(io.Discard, nil))
		registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, newSoftKBGuardForTest(logger), store)
		return mux
	}

	post := func(t *testing.T, mux *http.ServeMux, path string) *httptest.ResponseRecorder {
		t.Helper()
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(`{"kb_id":"kb-1","payload":{"doc_ids":["d1"]}}`))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
		mux.ServeHTTP(rec, req)
		return rec
	}

	// 未登记的写法：404，且 CreateJob / EnqueueReindexChunks 一次都没被调用。
	// `reindex_chunks`（下划线）是 job_type 不是路径；`reindex-document` 在
	// 设计文档里登记过但本轮不实现，两者都必须停在路由分派处。
	unknownStore := &fakeAdminJobStore{createResult: adminstore.JobItem{ID: 1, JobType: "build_graph"}}
	for _, path := range []string{
		"/api/v1/admin/jobs/reindex_chunks",
		"/api/v1/admin/jobs/reindex-document",
		"/api/v1/admin/jobs/some-unknown-type",
	} {
		rec := post(t, buildMux(t, unknownStore), path)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s 应走路由 default 分支 404，实际 %d body=%s", path, rec.Code, rec.Body.String())
		}
		if got := errorCodeOf(t, decodeAdminJobResponse(t, rec)); got != "NOT_FOUND" {
			t.Fatalf("%s want NOT_FOUND, got %q", path, got)
		}
		if rec.Code == http.StatusConflict {
			t.Fatalf("§16.3 的 409 映射未实现，出现 409 即为口径漂移")
		}
	}
	if unknownStore.createReq.JobType != "" {
		t.Fatalf("路由 404 前不得调用 CreateJob，实际 job_type=%q", unknownStore.createReq.JobType)
	}
	if unknownStore.enqueueReq.Targets != nil {
		t.Fatalf("路由 404 前不得调用 EnqueueReindexChunks，实际 targets=%d", len(unknownStore.enqueueReq.Targets))
	}

	// 已登记的 kebab 写法（Wave 8）：必须真的抵达去重入队，而不是继续 404。
	// 同一条断言里钉住"不走 CreateJob"——放行时误接 CreateJob 是最容易假绿的形态。
	writeStore := &fakeAdminJobStore{enqueueResult: reindexReport("created", 41)}
	writeReq := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/reindex-chunks", strings.NewReader(reindexBody(`{"chunk_id":"a","target_revision":1}`)))
	writeReq.Header.Set("Content-Type", "application/json")
	writeReq.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
	writeRec := httptest.NewRecorder()
	buildMux(t, writeStore).ServeHTTP(writeRec, writeReq)
	if writeRec.Code != http.StatusCreated {
		t.Fatalf("reindex-chunks 应已放行并返回 201，实际 %d body=%s", writeRec.Code, writeRec.Body.String())
	}
	if writeStore.createReq.JobType != "" {
		t.Fatalf("reindex_chunks 不得走裸 INSERT 的 CreateJob，实际 job_type=%q", writeStore.createReq.JobType)
	}

	// 已知类型：必须真的抵达 CreateJob 并带上由路径推导出的 job_type。
	knownStore := &fakeAdminJobStore{createResult: adminstore.JobItem{ID: 2, JobType: "build_graph", Status: "pending", Payload: map[string]interface{}{}, CreatedAt: time.Now().UTC()}}
	knownRec := post(t, buildMux(t, knownStore), "/api/v1/admin/jobs/build-graph")
	if knownRec.Code != http.StatusCreated {
		t.Fatalf("已知类型应 201，实际 %d body=%s", knownRec.Code, knownRec.Body.String())
	}
	if knownStore.createReq.JobType != "build_graph" {
		t.Fatalf("CreateJob 应收到路径推导的 job_type=build_graph，实际 %q", knownStore.createReq.JobType)
	}
}

func reindexEntry(outcome string, jobID int) adminstore.ReindexEnqueueEntry {
	return adminstore.ReindexEnqueueEntry{
		KBID:        "kb-1",
		DocID:       "doc-1",
		TargetsHash: "hash-abc",
		TargetCount: 2,
		Outcome:     outcome,
		Job: adminstore.JobItem{
			ID:          jobID,
			JobType:     adminstore.ReindexChunksJobType,
			Status:      adminstore.JobStatusPending,
			TargetsHash: optionalString("hash-abc"),
			CreatedAt:   time.Now().UTC(),
		},
	}
}

func reindexReport(outcome string, jobID int) adminstore.ReindexEnqueueReport {
	entry := reindexEntry(outcome, jobID)
	report := adminstore.ReindexEnqueueReport{Jobs: []adminstore.ReindexEnqueueEntry{entry}}
	switch outcome {
	case "created":
		report.Created = 1
	case "reused":
		report.Reused = 1
	case "retried":
		report.Retried = 1
	case "reset":
		report.Reset = 1
	case "rejected":
		report.Rejected = 1
		report.RejectedDetail = []adminstore.ReindexRejectedDetail{{
			ChildJobID:  &entry.Job.ID,
			KBID:        entry.KBID,
			DocID:       entry.DocID,
			TargetsHash: entry.TargetsHash,
			Reason:      "retry_exhausted",
			RetryCount:  3,
			MaxRetries:  3,
			ChunkIDs:    []string{"a", "b"},
		}}
	}
	return report
}

func reindexBody(targets string) string {
	return `{"kb_id":"kb-1","max_retries":3,"payload":{"doc_id":"doc-1","targets":[` + targets + `]}}`
}

// Wave 8：reindex-chunks 提交路由的入口语义。这里只证"路由把请求交给谁、按 outcome
// 回什么码"；§16.3 的去重/并发/原地重试本身在一次性 PG 的集成层（P6）取证。
func TestReindexChunksRouteSubmitsThroughDedupeQueue(t *testing.T) {
	const validTargets = `{"chunk_id":"b","target_revision":2},{"chunk_id":"a","target_revision":1}`

	newRouteMux := func(t *testing.T, store *fakeAdminJobStore) *http.ServeMux {
		t.Helper()
		pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, r *http.Request) {})
		mux := http.NewServeMux()
		logger := slog.New(slog.NewTextHandler(io.Discard, nil))
		// go_db 假授权：verified UserID=7。用它是为了证明审计里的 operator 来自认证结果，
		// 不是请求头自报的值（businessPermissionGuard 会先删掉伪造的 x-auth-user-id）。
		registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, newGoDBPermissionGuardForTest(nil, 7), store)
		return mux
	}
	submit := func(t *testing.T, store *fakeAdminJobStore, body string) (int, APIResponse) {
		t.Helper()
		req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/reindex-chunks", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
		req.Header.Set("X-Auth-User-Id", "99")
		rec := httptest.NewRecorder()
		newRouteMux(t, store).ServeHTTP(rec, req)
		return rec.Code, decodeAdminJobResponse(t, rec)
	}

	// created → 201，响应体就是那一行 job（child ID 可直接回读）。
	createdStore := &fakeAdminJobStore{enqueueResult: reindexReport("created", 41)}
	code, resp := submit(t, createdStore, reindexBody(validTargets))
	if code != http.StatusCreated || resp.Code != http.StatusCreated {
		t.Fatalf("created 应 201，实际 %d", code)
	}
	data, _ := resp.Data.(map[string]interface{})
	if id, _ := data["id"].(float64); int(id) != 41 {
		t.Fatalf("响应体必须回读 child job 行，实际 id=%v", data["id"])
	}
	if hash, _ := data["targets_hash"].(string); hash != "hash-abc" {
		t.Fatalf("响应体必须带 targets_hash，实际 %v", data["targets_hash"])
	}
	req := createdStore.enqueueReq
	if req.MaxRetries != 3 || req.Source != "admin_api" || len(req.Targets) != 2 {
		t.Fatalf("入队请求形状不对：max_retries=%d source=%q targets=%d", req.MaxRetries, req.Source, len(req.Targets))
	}
	if req.OperatorID == nil || *req.OperatorID != 7 {
		t.Fatalf("operator 必须取认证结果的 UserID=7，自报的 99 不得生效，实际 %v", req.OperatorID)
	}
	for _, target := range req.Targets {
		// 作用域四元组冻结自服务端 KB 行（fake 返回 tenant-a/project-a），不是客户端字符串。
		if target.KBID != "kb-1" || target.TenantID != "tenant-a" || target.ProjectID != "project-a" || target.DocID != "doc-1" {
			t.Fatalf("target 作用域未冻结： %#v", target)
		}
	}
	if req.Targets[0].ChunkID != "b" || req.Targets[0].TargetRevision != 2 {
		t.Fatalf("targets 顺序必须按提交原样传给 store（排序是 hash 层的事）：%#v", req.Targets)
	}

	// reused / retried / reset → 200，都指向既有行。
	for _, outcome := range []string{"reused", "retried", "reset"} {
		store := &fakeAdminJobStore{enqueueResult: reindexReport(outcome, 41)}
		code, _ = submit(t, store, reindexBody(validTargets))
		if code != http.StatusOK {
			t.Fatalf("outcome=%s 应 200，实际 %d", outcome, code)
		}
	}

	// rejected（重试额度用尽）→ 400 JOB_MAX_RETRIES_REACHED + child_job_id 回读。
	rejectedStore := &fakeAdminJobStore{enqueueResult: reindexReport("rejected", 41)}
	code, resp = submit(t, rejectedStore, reindexBody(validTargets))
	if code != http.StatusBadRequest {
		t.Fatalf("rejected 应 400，实际 %d", code)
	}
	rejectedData, _ := resp.Data.(map[string]interface{})
	if got, _ := rejectedData["error_code"].(string); got != "JOB_MAX_RETRIES_REACHED" {
		t.Fatalf("rejected 错误码应为 JOB_MAX_RETRIES_REACHED，实际 %q", got)
	}
	if id, _ := rejectedData["child_job_id"].(float64); int(id) != 41 {
		t.Fatalf("拒绝响应必须带 child_job_id 供人工介入，实际 %v", rejectedData["child_job_id"])
	}
	if n, _ := rejectedData["retry_count"].(float64); int(n) != 3 {
		t.Fatalf("拒绝响应必须带 retry_count，实际 %v", rejectedData["retry_count"])
	}

	// store 侧作用域哨兵 → 400 REINDEX_SCOPE_REQUIRED。
	scopeStore := &fakeAdminJobStore{enqueueErr: adminstore.ErrReindexScopeRequired}
	code, resp = submit(t, scopeStore, reindexBody(validTargets))
	if code != http.StatusBadRequest || errorCodeOf(t, resp) != "REINDEX_SCOPE_REQUIRED" {
		t.Fatalf("ErrReindexScopeRequired 应 400 REINDEX_SCOPE_REQUIRED，实际 %d %#v", code, resp.Data)
	}

	// 命中去重索引却读不到既有行 → 503，不降级成"参数错误"。
	anomalyStore := &fakeAdminJobStore{enqueueErr: adminstore.ErrReindexEnqueueAnomaly}
	code, resp = submit(t, anomalyStore, reindexBody(validTargets))
	if code != http.StatusServiceUnavailable || errorCodeOf(t, resp) != "ADMIN_STORE_UNAVAILABLE" {
		t.Fatalf("入队异常应 503 ADMIN_STORE_UNAVAILABLE，实际 %d %#v", code, resp.Data)
	}
	if !strings.Contains(resp.Message, "命中去重索引") {
		t.Fatalf("去重异常的文案必须点名去重索引，实际 %q", resp.Message)
	}

	// 非去重异常的存储故障（外键违例、连接断开……）不得复用上一条文案：Wave 8 第一次实跑
	// 就是夹具缺 admin_users 行使 requested_by 外键违例，却被兜成"命中去重索引"，
	// 让排障方向整个跑偏。同一个 503/同一错误码，但 message 只说它知道的那件事。
	fkStore := &fakeAdminJobStore{enqueueErr: errors.New(`insert admin_jobs failed: pq: insert violates foreign key constraint "admin_jobs_requested_by_fkey"`)}
	code, resp = submit(t, fkStore, reindexBody(validTargets))
	if code != http.StatusServiceUnavailable || errorCodeOf(t, resp) != "ADMIN_STORE_UNAVAILABLE" {
		t.Fatalf("外键违例应 503 ADMIN_STORE_UNAVAILABLE，实际 %d %#v", code, resp.Data)
	}
	if strings.Contains(resp.Message, "去重") {
		t.Fatalf("通用存储故障不得冒充去重异常，实际 %q", resp.Message)
	}
	if !strings.Contains(resp.Message, "存储层不可用") {
		t.Fatalf("通用存储故障文案应说明存储层不可用，实际 %q", resp.Message)
	}
}

// 入口防线：targets 非法的提交必须在抵达 store 之前被拒，否则会入队一个
// worker 会"成功"消费掉、却什么都不重建的任务（§15.7）。
func TestReindexChunksRouteRejectsInvalidTargetsBeforeStore(t *testing.T) {
	cases := []struct {
		name   string
		target string
	}{
		{"空 targets", ""},
		{"缺 chunk_id", `{"target_revision":1}`},
		{"chunk_id 是空白", `{"chunk_id":"   ","target_revision":1}`},
		{"缺 target_revision", `{"chunk_id":"a"}`},
		{"target_revision 为 0", `{"chunk_id":"a","target_revision":0}`},
		{"target_revision 为负", `{"chunk_id":"a","target_revision":-1}`},
		{"target_revision 是小数（Python int() 会截断，两端会算出不同 hash）", `{"chunk_id":"a","target_revision":1.5}`},
		{"target_revision 是字符串", `{"chunk_id":"a","target_revision":"1"}`},
		{"target_revision 是布尔", `{"chunk_id":"a","target_revision":true}`},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			store := &fakeAdminJobStore{enqueueResult: reindexReport("created", 1)}
			req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/reindex-chunks", strings.NewReader(reindexBody(tc.target)))
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Authorization", "Bearer "+newAdminAuthRequestToken(t))
			rec := httptest.NewRecorder()
			logger := slog.New(slog.NewTextHandler(io.Discard, nil))
			mux := http.NewServeMux()
			registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), nil, nil, newSoftKBGuardForTest(logger), store)
			mux.ServeHTTP(rec, req)
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("应 400，实际 %d body=%s", rec.Code, rec.Body.String())
			}
			if got := errorCodeOf(t, decodeAdminJobResponse(t, rec)); got != "REINDEX_SCOPE_REQUIRED" {
				t.Fatalf("应 REINDEX_SCOPE_REQUIRED，实际 %q", got)
			}
			if store.enqueueReq.Targets != nil {
				t.Fatalf("非法 targets 不得进入入队，实际 %d 条", len(store.enqueueReq.Targets))
			}
			if store.createReq.JobType != "" {
				t.Fatalf("非法 targets 也不得回落到 CreateJob，实际 job_type=%q", store.createReq.JobType)
			}
		})
	}
}
