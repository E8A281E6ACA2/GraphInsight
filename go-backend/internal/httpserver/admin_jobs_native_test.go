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

	// 未知类型：404，且 CreateJob 一次都没被调用。
	unknownStore := &fakeAdminJobStore{createResult: adminstore.JobItem{ID: 1, JobType: "build_graph"}}
	rec := post(t, buildMux(t, unknownStore), "/api/v1/admin/jobs/reindex-chunks")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("reindex-chunks 应走路由 default 分支 404，实际 %d body=%s", rec.Code, rec.Body.String())
	}
	if got := errorCodeOf(t, decodeAdminJobResponse(t, rec)); got != "NOT_FOUND" {
		t.Fatalf("want NOT_FOUND, got %q", got)
	}
	if unknownStore.createReq.JobType != "" {
		t.Fatalf("路由 404 前不得调用 CreateJob，实际 job_type=%q", unknownStore.createReq.JobType)
	}
	if rec.Code == http.StatusConflict {
		t.Fatalf("§16.3 的 409 映射未实现，出现 409 即为口径漂移")
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
