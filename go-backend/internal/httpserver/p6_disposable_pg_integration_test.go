package httpserver

// P6（Wave 6 候选）：一次性 Postgres 上的 Go HTTP 集成验证。
//
// 为什么要集成层：Wave 4 §11.3 只证到 SQL 层（缺 targets_hash 列 → PG 42703），
// "读侧因此整体 503"与"加列后迁移前的旧行仍可读"两条一直停在推断。本文件在真实
// database/sql + 真实 adminstore.Client + 真实 HTTP 路由上把它们钉成观测结果。
//
// 隔离口径：GI_P6_PG_DSN 未注入即 t.Skip —— `go test ./...`（CI）永远不会连库；
// 只有编排器 check_m5_p6_disposable_pg.py 在一次性 docker network 上注入它才执行。
// 连接对象只能是 gi-p6-* 容器（不发布宿主端口），绝不指向共享开发 PG。
//
// 不用 t.Parallel：多个用例共写同一份日志缓冲做归因，串行才有可判读的证据。

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"

	"graphinsight/go-backend/internal/adminstore"
	"graphinsight/go-backend/internal/config"
)

const (
	p6PhasePreMigrate  = "pre_migrate"
	p6PhasePostMigrate = "post_migrate"
)

type p6Env struct {
	dsn         string
	phase       string
	kbID        string
	tenantID    string
	projectID   string
	legacyID    int
	jobID       int
	targetsHash string
}

func loadP6Env(t *testing.T) p6Env {
	t.Helper()
	env := p6Env{
		dsn:         strings.TrimSpace(os.Getenv("GI_P6_PG_DSN")),
		phase:       strings.TrimSpace(os.Getenv("GI_P6_PHASE")),
		kbID:        strings.TrimSpace(os.Getenv("GI_P6_KB_ID")),
		tenantID:    strings.TrimSpace(os.Getenv("GI_P6_TENANT_ID")),
		projectID:   strings.TrimSpace(os.Getenv("GI_P6_PROJECT_ID")),
		targetsHash: strings.TrimSpace(os.Getenv("GI_P6_TARGETS_HASH")),
	}
	if env.dsn == "" {
		t.Skip("GI_P6_PG_DSN 未注入：P6 集成用例只在一次性容器网络上执行，CI 与本机默认跳过")
	}
	if env.phase != p6PhasePreMigrate && env.phase != p6PhasePostMigrate {
		t.Fatalf("GI_P6_PHASE 必须是 %s 或 %s，实际 %q", p6PhasePreMigrate, p6PhasePostMigrate, env.phase)
	}
	if env.kbID == "" || env.tenantID == "" || env.projectID == "" {
		t.Fatalf("P6 作用域三元组必须齐备，实际 kb=%q tenant=%q project=%q", env.kbID, env.tenantID, env.projectID)
	}
	env.legacyID = p6EnvInt(t, "GI_P6_LEGACY_JOB_ID")
	env.jobID = p6EnvInt(t, "GI_P6_JOB_ID")
	return env
}

func p6EnvInt(t *testing.T, key string) int {
	t.Helper()
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return 0
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		t.Fatalf("%s 非法: %q", key, raw)
	}
	return value
}

func (e p6Env) requirePostMigrateIDs(t *testing.T) {
	t.Helper()
	if e.jobID <= 0 || e.legacyID <= 0 || e.targetsHash == "" {
		t.Fatalf("post_migrate 阶段需要 GI_P6_JOB_ID / GI_P6_LEGACY_JOB_ID / GI_P6_TARGETS_HASH 才能做精确对账")
	}
}

// p6ProbeStore 只加一个计数器，SQL 全部委托给真实 *adminstore.Client。
// 目的是把"未知类型在路由分派处就被拦掉、CreateJob 一次都没被调用"从推断变成观测。
type p6ProbeStore struct {
	*adminstore.Client
	createCalls int
}

func (p *p6ProbeStore) CreateJob(ctx context.Context, req adminstore.JobCreateRequest) (adminstore.JobItem, error) {
	p.createCalls++
	return p.Client.CreateJob(ctx, req)
}

type p6Harness struct {
	mux     *http.ServeMux
	store   *p6ProbeStore
	logs    *bytes.Buffer
	headers func(*http.Request)
}

func newP6Harness(t *testing.T, env p6Env) *p6Harness {
	t.Helper()

	client, err := adminstore.New(config.Config{AdminDatabaseURL: env.dsn})
	if err != nil {
		t.Fatalf("连接一次性 Postgres 失败: %v", err)
	}
	t.Cleanup(func() { _ = client.Close() })
	if err := client.CheckHealth(context.Background()); err != nil {
		t.Fatalf("一次性 Postgres 健康检查失败: %v", err)
	}

	logs := &bytes.Buffer{}
	logger := slog.New(slog.NewTextHandler(logs, nil))
	token := newAdminAuthRequestToken(t)
	pythonWakeClient := newProxyClientForTest(t, func(w http.ResponseWriter, _ *http.Request) {
		// 404/503 分支都不该唤醒 Python worker；真被调用时这里会静默，
		// 因此判据不依赖它，只保证不会因为外部依赖而失败。
		w.WriteHeader(http.StatusAccepted)
	})

	mux := http.NewServeMux()
	store := &p6ProbeStore{Client: client}
	registerAdminControlPlaneRoutesWithContext(mux, config.Config{}, logger, nil, nil, newAPIMetrics(10), pythonWakeClient, nil, newSoftKBGuardForTest(logger), store)

	return &p6Harness{
		mux:   mux,
		store: store,
		logs:  logs,
		headers: func(r *http.Request) {
			r.Header.Set("Content-Type", "application/json")
			r.Header.Set("Authorization", "Bearer "+token)
		},
	}
}

func (h *p6Harness) do(t *testing.T, method, path, body string) (int, APIResponse, string) {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, reader)
	h.headers(req)
	rec := httptest.NewRecorder()
	h.mux.ServeHTTP(rec, req)

	// 先取全文再解码：json.NewDecoder(rec.Body) 会读空缓冲区，
	// 之后 rec.Body.String() 只剩空串，判据 4/5 会被误判成"响应体为空"。
	raw := rec.Body.String()
	var resp APIResponse
	if err := json.Unmarshal([]byte(raw), &resp); err != nil {
		t.Fatalf("解码响应失败: %v body=%s", err, raw)
	}
	return rec.Code, resp, raw
}

func (h *p6Harness) scopeQuery(env p6Env) string {
	return "kb_id=" + env.kbID + "&tenant_id=" + env.tenantID + "&project_id=" + env.projectID
}

type p6JobRow struct {
	ID          int     `json:"id"`
	JobType     string  `json:"job_type"`
	Status      string  `json:"status"`
	RetryCount  int     `json:"retry_count"`
	TargetsHash *string `json:"targets_hash"`
}

type p6ListBody struct {
	Code int `json:"code"`
	Data struct {
		Items []p6JobRow `json:"items"`
		Total int        `json:"total"`
	} `json:"data"`
}

func (h *p6Harness) list(t *testing.T, env p6Env, extra string) (p6ListBody, string) {
	t.Helper()
	code, _, raw := h.do(t, http.MethodGet, "/api/v1/admin/jobs?"+h.scopeQuery(env)+extra, "")
	var body p6ListBody
	if err := json.Unmarshal([]byte(raw), &body); err != nil {
		t.Fatalf("列表响应解码失败 code=%d raw=%s: %v", code, raw, err)
	}
	if body.Code != code {
		t.Fatalf("HTTP 码 %d 与响应体 code %d 不一致: %s", code, body.Code, raw)
	}
	return body, raw
}

func findRow(items []p6JobRow, id int) (p6JobRow, bool) {
	for _, item := range items {
		if item.ID == id {
			return item, true
		}
	}
	return p6JobRow{}, false
}

// 判据 1：路由未放行 reindex_chunks —— 两种写法都必须 404，且 CreateJob 一次都没被调用。
// 409 在本仓库不存在（P3 定案），出现即为口径漂移。
func TestP6UnlistedJobTypeIsRejectedAtRouteDispatch(t *testing.T) {
	env := loadP6Env(t)
	h := newP6Harness(t, env)

	for _, path := range []string{"/api/v1/admin/jobs/reindex_chunks", "/api/v1/admin/jobs/reindex-chunks"} {
		code, resp, raw := h.do(t, http.MethodPost, path, `{"kb_id":"`+env.kbID+`","payload":{"targets":[{"chunk_id":"c1","target_revision":1}]}}`)
		if code != http.StatusNotFound {
			t.Fatalf("%s 期望 404，实际 %d body=%s", path, code, raw)
		}
		if got := errorCodeOf(t, resp); got != "NOT_FOUND" {
			t.Fatalf("%s 期望 NOT_FOUND，实际 %q", path, got)
		}
		if code == http.StatusConflict {
			t.Fatalf("%s 出现 409：§16.3 的 409 映射未实现，口径漂移", path)
		}
	}
	if h.store.createCalls != 0 {
		t.Fatalf("路由 404 前不得调用 CreateJob，实际调用 %d 次", h.store.createCalls)
	}
}

// 判据 3：缺 targets_hash 列时 Go 读侧必须是结构化 503，不得退化成"空列表 200"。
// 归因证据：同一份 503 的底层错误必须点名 targets_hash 列不存在（42703 形态）。
func TestP6ReadPathIsStructuredUnavailableWithoutColumn(t *testing.T) {
	env := loadP6Env(t)
	if env.phase != p6PhasePreMigrate {
		t.Skipf("本用例只在 %s 阶段（真实迁移脚本回滚出旧形态之后）执行", p6PhasePreMigrate)
	}
	if env.legacyID <= 0 {
		t.Fatalf("pre_migrate 阶段需要 GI_P6_LEGACY_JOB_ID 才能验证详情/日志两条读路径")
	}
	h := newP6Harness(t, env)

	cases := []struct {
		name string
		path string
	}{
		{"列表", "/api/v1/admin/jobs?" + h.scopeQuery(env)},
		{"详情", "/api/v1/admin/jobs/" + strconv.Itoa(env.legacyID) + "?" + h.scopeQuery(env)},
		{"日志", "/api/v1/admin/jobs/" + strconv.Itoa(env.legacyID) + "/logs?" + h.scopeQuery(env)},
	}
	for _, tc := range cases {
		h.logs.Reset()
		code, resp, raw := h.do(t, http.MethodGet, tc.path, "")
		if code == http.StatusOK {
			t.Fatalf("%s 缺列时退化成 200（空列表假绿），body=%s", tc.name, raw)
		}
		if code != http.StatusServiceUnavailable {
			t.Fatalf("%s 期望 503，实际 %d body=%s", tc.name, code, raw)
		}
		if got := errorCodeOf(t, resp); got != "ADMIN_STORE_UNAVAILABLE" {
			t.Fatalf("%s 期望 ADMIN_STORE_UNAVAILABLE，实际 %q", tc.name, got)
		}
		logged := h.logs.String()
		if !strings.Contains(logged, "targets_hash") || !strings.Contains(logged, "does not exist") {
			t.Fatalf("%s 的 503 归因不到缺列（说明不是判据要的那条路径），日志=%s", tc.name, logged)
		}
	}
}

// 判据 4 + 6（读侧）：真实迁移脚本加列之后，Go 读侧 200 且能读回 Python 写入的那个
// targets_hash；同 hash 二次提交在 HTTP 读侧同样只呈现一行。
func TestP6ReadPathSucceedsWithTargetsHashAfterMigration(t *testing.T) {
	env := loadP6Env(t)
	if env.phase != p6PhasePostMigrate {
		t.Skipf("本用例只在 %s 阶段执行", p6PhasePostMigrate)
	}
	env.requirePostMigrateIDs(t)
	h := newP6Harness(t, env)

	body, raw := h.list(t, env, "&job_type=reindex_chunks&page_size=50")
	if body.Code != http.StatusOK {
		t.Fatalf("迁移后列表应 200，实际 %d body=%s", body.Code, raw)
	}
	if body.Data.Total != 1 || len(body.Data.Items) != 1 {
		t.Fatalf("同 targets_hash 多次提交后应只有 1 行，实际 total=%d items=%s", body.Data.Total, raw)
	}
	row, ok := findRow(body.Data.Items, env.jobID)
	if !ok {
		t.Fatalf("列表里没有 Python 提交的那行 id=%d，body=%s", env.jobID, raw)
	}
	if row.TargetsHash == nil || *row.TargetsHash != env.targetsHash {
		t.Fatalf("Go 读回的 targets_hash 与 Python 写入值不一致，期望 %q 实际 %v", env.targetsHash, row.TargetsHash)
	}
	if row.Status != "pending" {
		t.Fatalf("判据 7 走完后该行应回到 pending，实际 %q", row.Status)
	}
	if row.RetryCount != 0 {
		t.Fatalf("cancelled 分支应把 retry_count 归零，实际 %d", row.RetryCount)
	}

	code, resp, detailRaw := h.do(t, http.MethodGet, "/api/v1/admin/jobs/"+strconv.Itoa(env.jobID)+"?"+h.scopeQuery(env), "")
	if code != http.StatusOK {
		t.Fatalf("详情应 200，实际 %d body=%s", code, detailRaw)
	}
	data, _ := resp.Data.(map[string]interface{})
	if got, _ := data["targets_hash"].(string); got != env.targetsHash {
		t.Fatalf("详情读回的 targets_hash=%v，期望 %q", data["targets_hash"], env.targetsHash)
	}
}

// 判据 5：迁移前写入的历史行（targets_hash 为 NULL）仍能被 Go 的真实查询路径读出，
// 且 JSON 里不出现 targets_hash（omitempty + nil 指针）。加列不破坏旧行读兼容。
func TestP6LegacyRowStaysReadableAfterMigration(t *testing.T) {
	env := loadP6Env(t)
	if env.phase != p6PhasePostMigrate {
		t.Skipf("本用例只在 %s 阶段执行", p6PhasePostMigrate)
	}
	env.requirePostMigrateIDs(t)
	h := newP6Harness(t, env)

	body, raw := h.list(t, env, "&page_size=50")
	if body.Code != http.StatusOK {
		t.Fatalf("历史行读取应 200，实际 %d body=%s", body.Code, raw)
	}
	legacy, ok := findRow(body.Data.Items, env.legacyID)
	if !ok {
		t.Fatalf("旧形态写入的行在迁移后读不到了，id=%d body=%s", env.legacyID, raw)
	}
	if legacy.JobType != "build_graph" {
		t.Fatalf("历史行 job_type 对不上，实际 %q", legacy.JobType)
	}
	if legacy.TargetsHash != nil {
		t.Fatalf("历史行 targets_hash 应为 NULL，实际 %q", *legacy.TargetsHash)
	}
	// 键存在性只能在原始 JSON 上判定，且必须限定到历史行那个对象：
	// 同页里 Python 提交的行是带 targets_hash 的，扫整个响应体会假红。
	var page struct {
		Data struct {
			Items []map[string]interface{} `json:"items"`
		} `json:"data"`
	}
	if err := json.Unmarshal([]byte(raw), &page); err != nil {
		t.Fatalf("按对象解码列表失败: %v body=%s", err, raw)
	}
	for _, item := range page.Data.Items {
		id, _ := item["id"].(float64)
		if int(id) != env.legacyID {
			continue
		}
		if _, exists := item["targets_hash"]; exists {
			t.Fatalf("NULL 历史行不应序列化出 targets_hash 字段，item=%#v", item)
		}
		return
	}
	t.Fatalf("列表对象中找不到历史行 id=%d，body=%s", env.legacyID, raw)
}

// 附带收口 §11.5-3 / §12.1 P5 缺口 3：Go 日志读侧在真实 PG 上把 Python 写的
// admin_logs.details 原样读出来（含 outcome 与 targets_hash）。
func TestP6JobLogsExposePythonAuditDetails(t *testing.T) {
	env := loadP6Env(t)
	if env.phase != p6PhasePostMigrate {
		t.Skipf("本用例只在 %s 阶段执行", p6PhasePostMigrate)
	}
	env.requirePostMigrateIDs(t)
	h := newP6Harness(t, env)

	code, resp, raw := h.do(t, http.MethodGet, "/api/v1/admin/jobs/"+strconv.Itoa(env.jobID)+"/logs?page_size=50&"+h.scopeQuery(env), "")
	if code != http.StatusOK {
		t.Fatalf("日志读侧应 200，实际 %d body=%s", code, raw)
	}
	data, _ := resp.Data.(map[string]interface{})
	items, _ := data["items"].([]interface{})
	if len(items) < 4 {
		t.Fatalf("四次提交（enqueued/reused/retried/reset）应在审计里各留一行，实际 %d 行 body=%s", len(items), raw)
	}
	actions := map[string]int{}
	hashSeen := ""
	outcomes := map[string]int{}
	for _, entry := range items {
		row, _ := entry.(map[string]interface{})
		action, _ := row["action"].(string)
		actions[action]++
		details, _ := row["details"].(map[string]interface{})
		if value, _ := details["targets_hash"].(string); value != "" {
			hashSeen = value
		}
		if value, _ := details["outcome"].(string); value != "" {
			outcomes[value]++
		}
	}
	if hashSeen != env.targetsHash {
		t.Fatalf("审计 details 里的 targets_hash 与库列不一致，期望 %q 实际 %q", env.targetsHash, hashSeen)
	}
	for _, want := range []string{"enqueued", "reused", "retried", "reset"} {
		if outcomes[want] == 0 {
			t.Fatalf("details.outcome 缺少 %q 分支，实际 %v（actions=%v）", want, outcomes, actions)
		}
	}
}
