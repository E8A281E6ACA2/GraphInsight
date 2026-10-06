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
	"database/sql"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"sync"
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
	docID       string
	targetsJSON string
	// operatorID 是夹具种下的 admin_users 主键：Go 写侧提交的 requested_by / 审计的
	// operator_id 必须就是它，也是那两条外键唯一能指向的行。
	operatorID int
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
		docID:       strings.TrimSpace(os.Getenv("GI_P6_DOC_ID")),
		targetsJSON: strings.TrimSpace(os.Getenv("GI_P6_TARGETS_JSON")),
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
	env.operatorID = p6EnvInt(t, "GI_P6_OPERATOR_ID")
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

// requirePostMigrate 是写侧用例的统一入口：阶段不对就 Fatal（不是 Skip）。
// 编排器对每个阶段点名用例并断言 skipped=0，写侧用例若被静默跳过就会假绿；
// 用 Fatalf 让"没跑在该阶段"和"跑挂"一样红，且原因可读。
func (e p6Env) requirePostMigrate(t *testing.T) {
	t.Helper()
	if e.phase != p6PhasePostMigrate {
		t.Fatalf("写侧用例只在 %s 阶段执行（真实迁移脚本已补齐 targets_hash 列之后），实际 %q", p6PhasePostMigrate, e.phase)
	}
	if e.docID == "" || e.targetsJSON == "" {
		t.Fatalf("写侧用例需要 GI_P6_DOC_ID / GI_P6_TARGETS_JSON（Python 提交那批 targets 的原文），实际 doc=%q targets=%q", e.docID, e.targetsJSON)
	}
	// 操作员 id 缺失就等于"这条提交链根本不该跑"：admin_jobs.requested_by 与
	// admin_logs.operator_id 两条外键会把它撞死，而错误在 HTTP 层只表现为 503，
	// 排查成本高。所以在这里先要齐，别把夹具缺口留成一次难读的失败。
	if e.operatorID <= 0 {
		t.Fatalf("写侧用例需要 GI_P6_OPERATOR_ID（夹具种下的 admin_users 主键），实际 %d", e.operatorID)
	}
}

// p6ProbeStore 只加计数器，SQL 全部委托给真实 *adminstore.Client。
// 目的是把"未知类型在路由分派处就被拦掉、CreateJob 一次都没被调用"和
// "reindex_chunks 的写入真的走了去重入队而不是裸 INSERT"从推断变成观测。
type p6ProbeStore struct {
	*adminstore.Client
	createCalls  int
	enqueueCalls int
}

func (p *p6ProbeStore) CreateJob(ctx context.Context, req adminstore.JobCreateRequest) (adminstore.JobItem, error) {
	p.createCalls++
	return p.Client.CreateJob(ctx, req)
}

func (p *p6ProbeStore) EnqueueReindexChunks(ctx context.Context, req adminstore.ReindexEnqueueRequest) (adminstore.ReindexEnqueueReport, error) {
	p.enqueueCalls++
	return p.Client.EnqueueReindexChunks(ctx, req)
}

type p6Harness struct {
	mux     *http.ServeMux
	store   *p6ProbeStore
	logs    *bytes.Buffer
	headers func(*http.Request)
}

func newP6Harness(t *testing.T, env p6Env) *p6Harness {
	t.Helper()
	logs := &bytes.Buffer{}
	h := newP6HarnessWithLogWriter(t, env, logs)
	h.logs = logs
	return h
}

// newP6HarnessWithLogWriter 让并发写侧用例把 slog 指到 io.Discard：
// 多个 goroutine 同时往同一个 bytes.Buffer 写是数据竞争，取证用例不能自带 race。
func newP6HarnessWithLogWriter(t *testing.T, env p6Env, w io.Writer) *p6Harness {
	t.Helper()

	client, err := adminstore.New(config.Config{AdminDatabaseURL: env.dsn})
	if err != nil {
		t.Fatalf("连接一次性 Postgres 失败: %v", err)
	}
	t.Cleanup(func() { _ = client.Close() })
	if err := client.CheckHealth(context.Background()); err != nil {
		t.Fatalf("一次性 Postgres 健康检查失败: %v", err)
	}

	logger := slog.New(slog.NewTextHandler(w, nil))
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
		headers: func(r *http.Request) {
			r.Header.Set("Content-Type", "application/json")
			r.Header.Set("Authorization", "Bearer "+token)
		},
	}
}

// submitReindex 走真实路由 + 真实 store 提交一批重建目标。
// 查询串带齐 kb/tenant/project：作用域不一致时路由必须在入队之前就拒（判据 14）。
func (h *p6Harness) submitReindex(t *testing.T, env p6Env, docID, targets string) (int, map[string]interface{}, string) {
	t.Helper()
	code, resp, raw := h.do(t, http.MethodPost, "/api/v1/admin/jobs/reindex-chunks?"+h.scopeQuery(env),
		`{"kb_id":"`+env.kbID+`","max_retries":3,"payload":{"doc_id":"`+docID+`","targets":[`+targets+`]}}`)
	data, _ := resp.Data.(map[string]interface{})
	return code, data, raw
}

// submitReindexRaw 与 submitReindex 同一条路径，但不调用 t.Fatalf：并发用例里
// 提交发生在别的 goroutine，只能把结果带回主 goroutine 再断言。
func (h *p6Harness) submitReindexRaw(env p6Env, docID, targets string) (int, map[string]interface{}, string) {
	var reader io.Reader = strings.NewReader(`{"kb_id":"` + env.kbID + `","max_retries":3,"payload":{"doc_id":"` + docID + `","targets":[` + targets + `]}}`)
	req := httptest.NewRequest(http.MethodPost, "/api/v1/admin/jobs/reindex-chunks?"+h.scopeQuery(env), reader)
	h.headers(req)
	rec := httptest.NewRecorder()
	h.mux.ServeHTTP(rec, req)
	raw := rec.Body.String()
	var resp APIResponse
	_ = json.Unmarshal([]byte(raw), &resp)
	data, _ := resp.Data.(map[string]interface{})
	return rec.Code, data, raw
}

func p6FieldInt(data map[string]interface{}, key string) int {
	value, _ := data[key].(float64)
	return int(value)
}

func p6FieldString(data map[string]interface{}, key string) string {
	value, _ := data[key].(string)
	return value
}

func p6ChunkTargets(prefix string) string {
	return `{"chunk_id":"` + prefix + `-1","target_revision":1},{"chunk_id":"` + prefix + `-2","target_revision":2}`
}

func p6ChunkTargetsReversed(prefix string) string {
	return `{"chunk_id":"` + prefix + `-2","target_revision":2},{"chunk_id":"` + prefix + `-1","target_revision":1}`
}

// p6TargetsFromArrayJSON 把 Python 提交那批 targets 的 JSON **数组原文**换成提交体需要的
// 数组内部形状：只剥掉最外层方括号，每个元素按原始字节拼接，绝不重新序列化 —— 一旦重拼，
// 键序/数字/空格的任何漂移都可能让两语言算出的 targets_hash 不再同形，判据 8/9/12 就
// 退化成"各自算各自的对"。
func p6TargetsFromArrayJSON(t *testing.T, arrayJSON string) string {
	t.Helper()
	var items []json.RawMessage
	if err := json.Unmarshal([]byte(arrayJSON), &items); err != nil {
		t.Fatalf("GI_P6_TARGETS_JSON 必须是 JSON 数组（Python payload_targets 原文）: %v", err)
	}
	if len(items) == 0 {
		t.Fatalf("GI_P6_TARGETS_JSON 是空数组：写侧判据没有 targets 可提交")
	}
	for _, item := range items {
		var object map[string]json.RawMessage
		if err := json.Unmarshal(item, &object); err != nil {
			t.Fatalf("GI_P6_TARGETS_JSON 的元素必须是对象（提交体会拼成 targets:[…]）: %v", err)
		}
		if _, ok := object["chunk_id"]; !ok {
			t.Fatalf("GI_P6_TARGETS_JSON 的元素缺 chunk_id，实际 %s", string(item))
		}
	}
	parts := make([]string, len(items))
	for index, item := range items {
		parts[index] = string(item)
	}
	return strings.Join(parts, ",")
}

// p6OpenFixtureDB 只给写侧用例铺状态（failed / cancelled / retry_count），形状与
// Python 驱动里的 raw SQL 夹具一致。与 Python 侧同一口径：连接只能来自 GI_P6_PG_DSN，
// 且在写入任何一行之前先探方言 —— 夹具落到非 Postgres 上就等于这轮证据作废。
func p6OpenFixtureDB(t *testing.T, env p6Env) *sql.DB {
	t.Helper()
	db, err := sql.Open("pgx", env.dsn)
	if err != nil {
		t.Fatalf("打开夹具连接失败: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	db.SetMaxOpenConns(4)

	var version, currentDB string
	if err := db.QueryRow("SELECT version(), current_database()").Scan(&version, &currentDB); err != nil {
		t.Fatalf("夹具连接方言探测失败: %v", err)
	}
	if !strings.Contains(version, "PostgreSQL") {
		t.Fatalf("夹具连接不是 Postgres（version=%q db=%q）：§16.3 的 FOR UPDATE 与部分唯一索引在别的方言上不存在", version, currentDB)
	}
	// 外键目标必须先在场。Wave 8 第一次实跑就是这里缺行：Go 提交链写 requested_by=认证
	// UserID，admin_jobs_requested_by_fkey 直接违例，而 HTTP 层把它兜成 503，读起来像
	// "去重索引异常"。夹具缺口要在跑之前就红，且红得说出真名。
	var operators int
	if err := db.QueryRow(`SELECT count(*) FROM admin_users WHERE id = $1`, env.operatorID).Scan(&operators); err != nil {
		t.Fatalf("探测操作员夹具行失败 id=%d: %v", env.operatorID, err)
	}
	if operators != 1 {
		t.Fatalf("夹具缺 admin_users 操作员行（id=%d 命中 %d 行）：Go 写侧的 requested_by / operator_id 两条外键会违例", env.operatorID, operators)
	}
	return db
}

func p6SetJobState(t *testing.T, db *sql.DB, jobID int, status string, retryCount int, errorMessage string) {
	t.Helper()
	res, err := db.Exec(`UPDATE admin_jobs SET status = $2, retry_count = $3, error_message = $4 WHERE id = $1`, jobID, status, retryCount, errorMessage)
	if err != nil {
		t.Fatalf("铺夹具状态失败 id=%d: %v", jobID, err)
	}
	if affected, _ := res.RowsAffected(); affected != 1 {
		t.Fatalf("夹具只该改到 1 行（id=%d 不存在？），实际 %d", jobID, affected)
	}
}

func p6CountRowsByHash(t *testing.T, db *sql.DB, targetsHash string) int {
	t.Helper()
	var count int
	if err := db.QueryRow(`SELECT count(*) FROM admin_jobs WHERE job_type = 'reindex_chunks' AND targets_hash = $1`, targetsHash).Scan(&count); err != nil {
		t.Fatalf("按 targets_hash 统计行数失败 hash=%s: %v", targetsHash, err)
	}
	return count
}

func p6JobRowState(t *testing.T, db *sql.DB, jobID int) (string, int, string) {
	t.Helper()
	var status string
	var retryCount int
	var errorMessage sql.NullString
	if err := db.QueryRow(`SELECT status, retry_count, error_message FROM admin_jobs WHERE id = $1`, jobID).Scan(&status, &retryCount, &errorMessage); err != nil {
		t.Fatalf("回读 job 行失败 id=%d: %v", jobID, err)
	}
	return status, retryCount, errorMessage.String
}

// jobLogsDetails 取 Go 读侧（真实 HTTP + 真实 store）的审计行，逐行给出 details 对象。
// 读侧路径本身已由判据 5/6 用例覆盖，这里只借用它取证，不再重复断言状态码。
func (h *p6Harness) jobLogsDetails(t *testing.T, env p6Env, jobID int) []map[string]interface{} {
	t.Helper()
	code, resp, raw := h.do(t, http.MethodGet, "/api/v1/admin/jobs/"+strconv.Itoa(jobID)+"/logs?page_size=50&"+h.scopeQuery(env), "")
	if code != http.StatusOK {
		t.Fatalf("日志读侧应 200，实际 %d body=%s", code, raw)
	}
	data, _ := resp.Data.(map[string]interface{})
	items, _ := data["items"].([]interface{})
	rows := make([]map[string]interface{}, 0, len(items))
	for _, item := range items {
		row, _ := item.(map[string]interface{})
		rows = append(rows, row)
	}
	return rows
}

func auditDetails(row map[string]interface{}) map[string]interface{} {
	details, _ := row["details"].(map[string]interface{})
	return details
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

// 判据 1：路由未放行的写法必须停在分派处 —— 404，且 CreateJob / EnqueueReindexChunks
// 一次都没被调用。Wave 8 放行的是 kebab 路径 `/reindex-chunks`（契约 §168/§185），
// job_type 的下划线写法 `reindex_chunks` 从来不是路径；`reindex-document` 在设计里
// 登记过但本轮不实现。409 在本仓库不存在（P3 定案），出现即为口径漂移。
func TestP6UnlistedJobTypeIsRejectedAtRouteDispatch(t *testing.T) {
	env := loadP6Env(t)
	h := newP6Harness(t, env)

	for _, path := range []string{"/api/v1/admin/jobs/reindex_chunks", "/api/v1/admin/jobs/reindex-document"} {
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
	if h.store.enqueueCalls != 0 {
		t.Fatalf("路由 404 前不得调用 EnqueueReindexChunks，实际调用 %d 次", h.store.enqueueCalls)
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
	sameHash := 0
	for _, item := range body.Data.Items {
		if item.TargetsHash != nil && *item.TargetsHash == env.targetsHash {
			sameHash++
		}
	}
	// 按 hash 计数而不是按 total 计数：去重判据的主体是"同一批 targets 只有一行"，
	// 而 Wave 8 的写侧用例会为本判据之外再造若干不同 hash 的行（见文件末尾说明）。
	if sameHash != 1 {
		t.Fatalf("同 targets_hash 多次提交后应只有 1 行，实际 %d 行（total=%d items=%s）", sameHash, body.Data.Total, raw)
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
		t.Fatalf("四次提交（created/reused/retried/reset）应在审计里各留一行，实际 %d 行 body=%s", len(items), raw)
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
		outcome, _ := details["outcome"].(string)
		if outcome == "" {
			continue
		}
		outcomes[outcome]++
		// Wave 7 口径统一：结构化边界只用 child_job_id 表达"复用/重试/复位指向的既有行"。
		// 旧键名 job_id 若在真实 PG 读侧复活，说明两条写入路径（任务中心提交 / CLI backfill）
		// 又分叉了，运维按 child_job_id 检索就会漏行。
		if _, legacy := details["job_id"]; legacy {
			t.Fatalf("details 仍带旧键名 job_id（应统一为 child_job_id）：%s", raw)
		}
		if id, ok := details["child_job_id"].(float64); !ok || int64(id) != int64(env.jobID) {
			t.Fatalf("outcome=%s 的 details.child_job_id 必须回读为既有行 id=%d，实际 %v", outcome, env.jobID, details["child_job_id"])
		}
	}
	if hashSeen == "" {
		t.Fatalf("四条留痕都没带 targets_hash，无法与列表侧对账：body=%s", raw)
	}
	for _, want := range []string{"created", "reused", "retried", "reset"} {
		if outcomes[want] != 1 {
			t.Fatalf("outcome=%s 应恰好 1 条，实际 %d（全量=%v）", want, outcomes[want], outcomes)
		}
	}
	if hashSeen != env.targetsHash {
		t.Fatalf("审计 details 里的 targets_hash 与库列不一致，期望 %q 实际 %q", env.targetsHash, hashSeen)
	}
	if actions["job_created"] < 1 || actions["job_reused"] < 1 {
		t.Fatalf("action 侧必须与 outcome 同源映射（job_created/job_reused），实际 %v", actions)
	}
}

// ── Wave 8：Go 写侧提交链 ───────────────────────────────────────────────
//
// 下面七个用例把 Wave 8 指令要求的六项证明落在**Go 这条路径**上（真实 HTTP 路由 +
// 真实 adminstore.Client + 真实 Postgres）：§16.3 去重、并发唯一性、原地重试/复位、
// child ID 回读、作用域校验、审计留痕。加白名单放行路由本身不产生任何一条证据。
//
// 声明顺序 = 执行顺序：这批用例必须排在
// TestP6JobLogsExposePythonAuditDetails 之后。复用 Python 那一行会给它追加第二条
// outcome=reused 留痕，而那条用例对每个 outcome 断言"恰好 1 条"；写侧用例排到前面
// 会让 Python 侧审计判据假红。
//
// 夹具 SQL 只把行推进 worker 才会写的状态（failed / cancelled / 重试额度用尽），
// 与 Python 驱动 stage_submit 的 raw SQL 夹具同一口径；被验证的分支全部由真实
// Go handler 触发。

func p6JobScope(t *testing.T, db *sql.DB, jobID int) (tenantID, projectID, kbID, jobType string) {
	t.Helper()
	if err := db.QueryRow(`SELECT tenant_id, project_id, kb_id, job_type FROM admin_jobs WHERE id = $1`, jobID).
		Scan(&tenantID, &projectID, &kbID, &jobType); err != nil {
		t.Fatalf("回读 job 作用域失败 id=%d: %v", jobID, err)
	}
	return
}

// p6JobRequestedBy 回读 admin_jobs.requested_by。它是"谁提交的"唯一的库侧真相：
// Go 写侧把认证 UserID 写进这一列（Python 内部入队传 NULL），夹具缺 admin_users 行时
// 外键直接违例，所以这一列同时证明身份真的落了库、而不只是进了响应体。
func p6JobRequestedBy(t *testing.T, db *sql.DB, jobID int) *int {
	t.Helper()
	var requestedBy sql.NullInt64
	if err := db.QueryRow(`SELECT requested_by FROM admin_jobs WHERE id = $1`, jobID).Scan(&requestedBy); err != nil {
		t.Fatalf("回读 requested_by 失败 id=%d: %v", jobID, err)
	}
	if !requestedBy.Valid {
		return nil
	}
	value := int(requestedBy.Int64)
	return &value
}

type p6AuditOperatorRow struct {
	Action     string
	UserID     *int
	OperatorID *int
	Status     string
}

// p6AuditOperatorRows 用与写侧完全相同的库侧谓词取审计行：
// insertJobAuditLogEntry 固定写 resource='job'、resource_id=jobID 的十进制字符串
// （adminstore/jobs.go:651-667），所以这条检索式就是运维在库里追这条任务留痕的方式。
func p6AuditOperatorRows(t *testing.T, db *sql.DB, jobID int) []p6AuditOperatorRow {
	t.Helper()
	rows, err := db.Query(`SELECT action, user_id, operator_id, status FROM admin_logs WHERE resource = 'job' AND resource_id = $1 ORDER BY id`, strconv.Itoa(jobID))
	if err != nil {
		t.Fatalf("取审计行失败 job=%d: %v", jobID, err)
	}
	defer rows.Close()
	var out []p6AuditOperatorRow
	for rows.Next() {
		var row p6AuditOperatorRow
		var userID, operatorID sql.NullInt64
		var status sql.NullString
		if err := rows.Scan(&row.Action, &userID, &operatorID, &status); err != nil {
			t.Fatalf("扫描审计行失败 job=%d: %v", jobID, err)
		}
		row.Status = status.String
		if userID.Valid {
			v := int(userID.Int64)
			row.UserID = &v
		}
		if operatorID.Valid {
			v := int(operatorID.Int64)
			row.OperatorID = &v
		}
		out = append(out, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("遍历审计行失败 job=%d: %v", jobID, err)
	}
	return out
}

// p6IntValue 把可变指针摊平成日志友好（nil → "NULL"）的字符串。
func p6IntValue(value *int) string {
	if value == nil {
		return "NULL"
	}
	return strconv.Itoa(*value)
}

// 判据 8（child ID 回读 + 跨语言 hash 对等）：Go 用 Python 那批完全相同的 targets
// 重提交，必须复用 Python 建的那一行 —— 响应体里的 id 就是既有行 id，且 Go 端算出的
// targets_hash 与 Python 写入值逐字相同。hash 差一个字节就会各建一行，这条判据会立刻红。
func TestP6GoWriteReusesPythonSubmittedJob(t *testing.T) {
	env := loadP6Env(t)
	env.requirePostMigrate(t)
	env.requirePostMigrateIDs(t)
	h := newP6Harness(t, env)
	db := p6OpenFixtureDB(t, env)

	if before := p6CountRowsByHash(t, db, env.targetsHash); before != 1 {
		t.Fatalf("Python 提交后该 hash 应只有 1 行，实际 %d 行", before)
	}
	enqueueStart := h.store.enqueueCalls

	code, data, raw := h.submitReindex(t, env, env.docID, p6TargetsFromArrayJSON(t, env.targetsJSON))
	if code != http.StatusOK {
		t.Fatalf("复用既有行必须回 200（不得伪装成 201 新建），实际 %d body=%s", code, raw)
	}
	if got := p6FieldInt(data, "id"); got != env.jobID {
		t.Fatalf("child ID 回读：Go 响应体的 id 必须是 Python 那一行 %d，实际 %d", env.jobID, got)
	}
	if got := p6FieldString(data, "targets_hash"); got != env.targetsHash {
		t.Fatalf("跨语言 hash 对等：Go 端 targets_hash 必须与 Python 写入值逐字相同，期望 %q 实际 %q", env.targetsHash, got)
	}
	if got := p6FieldString(data, "job_type"); got != adminstore.ReindexChunksJobType {
		t.Fatalf("响应体 job_type 应为 %s，实际 %q", adminstore.ReindexChunksJobType, got)
	}
	if got := p6FieldString(data, "status"); got != "pending" {
		t.Fatalf("复用不得改写既有行状态，实际 %q", got)
	}
	if h.store.enqueueCalls != enqueueStart+1 {
		t.Fatalf("提交必须走 EnqueueReindexChunks，实际调用 %d 次（起点 %d）", h.store.enqueueCalls, enqueueStart)
	}
	if h.store.createCalls != 0 {
		t.Fatalf("reindex_chunks 绝不走裸 INSERT 的 CreateJob（那样去重整体失效），实际调用 %d 次", h.store.createCalls)
	}
	if after := p6CountRowsByHash(t, db, env.targetsHash); after != 1 {
		t.Fatalf("Go 复用提交后该 hash 仍应只有 1 行，实际 %d 行", after)
	}
	// 复用分支也要留下"谁又提交了一次"：Python 内部入队的行没有提交人（user_id 为 NULL），
	// Go 这一条必须带认证操作员。只认得 Go 的、不误伤 Python 的，才能两侧共用同一条检索式。
	goTagged := 0
	for _, row := range p6AuditOperatorRows(t, db, env.jobID) {
		if row.UserID == nil {
			continue
		}
		if *row.UserID != env.operatorID || row.OperatorID == nil || *row.OperatorID != env.operatorID {
			t.Fatalf("Go 留痕的 user_id/operator_id 应为操作员 %d，实际 %s/%s（action=%q）", env.operatorID, p6IntValue(row.UserID), p6IntValue(row.OperatorID), row.Action)
		}
		goTagged++
	}
	if goTagged == 0 {
		t.Fatalf("Go 复用提交必须在 admin_logs 留下带操作员身份的审计行，实际一条都没有（Python 那批 NULL 不算）")
	}
}

// 判据 9（§16.3 去重，Go 路径）：Go 新建一行后，同批 targets 原样重提交、以及**换序
// 重提交**都必须复用它；部分唯一索引保证同 hash 只有一行。作用域三元组必须来自服务端
// KB 行（t1/p1/kb-p6），不是客户端字符串。
func TestP6GoWriteCreatesRowAndDedupesResubmits(t *testing.T) {
	env := loadP6Env(t)
	env.requirePostMigrate(t)
	h := newP6Harness(t, env)
	db := p6OpenFixtureDB(t, env)
	targets := p6ChunkTargets("go-w8-new")

	code, created, raw := h.submitReindex(t, env, env.docID, targets)
	if code != http.StatusCreated {
		t.Fatalf("首批 targets 应 201 新建，实际 %d body=%s", code, raw)
	}
	jobID := p6FieldInt(created, "id")
	targetsHash := p6FieldString(created, "targets_hash")
	if jobID <= 0 || targetsHash == "" {
		t.Fatalf("201 响应体必须带新建行的 id 与 targets_hash，实际 %#v", created)
	}
	if targetsHash == env.targetsHash {
		t.Fatalf("夹具 targets 与 Python 那批撞了 hash，判据失去区分度：%s", targetsHash)
	}
	if p6CountRowsByHash(t, db, targetsHash) != 1 {
		t.Fatalf("新建后该 hash 应有 1 行")
	}
	tenantID, projectID, kbID, jobType := p6JobScope(t, db, jobID)
	if tenantID != env.tenantID || projectID != env.projectID || kbID != env.kbID || jobType != adminstore.ReindexChunksJobType {
		t.Fatalf("作用域必须冻结自服务端 KB 行，实际 tenant=%q project=%q kb=%q type=%q", tenantID, projectID, kbID, jobType)
	}
	// 提交人身份落库：Go 写侧把认证 UserID 写进 requested_by（Python 内部入队是 NULL），
	// 这一列也是 admin_logs.user_id/operator_id 的兜底来源，所以它必须真等于操作员行的主键，
	// 而不是 0 / NULL —— 否则"谁提交的"在库里无从对账。
	if requestedBy := p6JobRequestedBy(t, db, jobID); requestedBy == nil || *requestedBy != env.operatorID {
		t.Fatalf("新建行的 requested_by 必须是夹具操作员 id=%d，实际 %s", env.operatorID, p6IntValue(requestedBy))
	}

	for _, tc := range []struct {
		name    string
		payload string
	}{
		{"原样重提交", targets},
		{"换序重提交（hash 只认排序后的 canonical 形状）", p6ChunkTargetsReversed("go-w8-new")},
	} {
		repeatCode, repeat, repeatRaw := h.submitReindex(t, env, env.docID, tc.payload)
		if repeatCode != http.StatusOK {
			t.Fatalf("%s 应 200 复用，实际 %d body=%s", tc.name, repeatCode, repeatRaw)
		}
		if got := p6FieldInt(repeat, "id"); got != jobID {
			t.Fatalf("%s 必须回读同一行 id=%d，实际 %d", tc.name, jobID, got)
		}
		if got := p6CountRowsByHash(t, db, targetsHash); got != 1 {
			t.Fatalf("%s 之后同 hash 仍应只有 1 行，实际 %d 行（§16.3 部分唯一索引失效）", tc.name, got)
		}
	}
}

// 判据 10（原地重试/复位）：failed → 复用同一行并把 retry_count 由 1 变 2（消耗配额）；
// cancelled → 复用同一行并把 retry_count 归零（人工取消不消耗配额）。两次都不新建行。
func TestP6GoWriteRetriesAndResetsInPlace(t *testing.T) {
	env := loadP6Env(t)
	env.requirePostMigrate(t)
	h := newP6Harness(t, env)
	db := p6OpenFixtureDB(t, env)
	targets := p6ChunkTargets("go-w8-retry")

	code, created, raw := h.submitReindex(t, env, env.docID, targets)
	if code != http.StatusCreated {
		t.Fatalf("夹具行应 201 新建，实际 %d body=%s", code, raw)
	}
	jobID := p6FieldInt(created, "id")
	targetsHash := p6FieldString(created, "targets_hash")

	p6SetJobState(t, db, jobID, "failed", 1, "go-w8-simulated-failure")
	retryCode, retried, retryRaw := h.submitReindex(t, env, env.docID, targets)
	if retryCode != http.StatusOK {
		t.Fatalf("failed 重提交应 200 复用同一行，实际 %d body=%s", retryCode, retryRaw)
	}
	if got := p6FieldInt(retried, "id"); got != jobID {
		t.Fatalf("failed 分支必须原地重试、不得新建行，期望 id=%d 实际 %d", jobID, got)
	}
	if got := p6FieldInt(retried, "retry_count"); got != 2 {
		t.Fatalf("failed → 重试应消耗一次配额（retry_count 1→2），实际 %d", got)
	}
	if status, retryCount, errorMessage := p6JobRowState(t, db, jobID); status != "pending" || retryCount != 2 || errorMessage != "" {
		t.Fatalf("库里的行应为 pending/2/无 error_message，实际 %s/%d/%q", status, retryCount, errorMessage)
	}

	p6SetJobState(t, db, jobID, "cancelled", 2, "")
	cancelCode, reset, cancelRaw := h.submitReindex(t, env, env.docID, targets)
	if cancelCode != http.StatusOK {
		t.Fatalf("cancelled 重提交应 200 复用同一行，实际 %d body=%s", cancelCode, cancelRaw)
	}
	if got := p6FieldInt(reset, "id"); got != jobID {
		t.Fatalf("cancelled 分支必须原地复位、不得新建行，期望 id=%d 实际 %d", jobID, got)
	}
	if got := p6FieldInt(reset, "retry_count"); got != 0 {
		t.Fatalf("cancelled → 复位应把 retry_count 归零（人工取消不消耗配额），实际 %d", got)
	}
	if status, retryCount, _ := p6JobRowState(t, db, jobID); status != "pending" || retryCount != 0 {
		t.Fatalf("复位后的行应为 pending/0，实际 %s/%d", status, retryCount)
	}
	if got := p6CountRowsByHash(t, db, targetsHash); got != 1 {
		t.Fatalf("retry/reset 全程该 hash 应只有 1 行，实际 %d 行", got)
	}
}

// 判据 11（重试额度用尽 → 拒绝而非无限复活）：status=failed 且 retry_count>=max_retries
// 时，Go 提交必须 400 JOB_MAX_RETRIES_REACHED，带上既有行 id 供人工介入，且**不得**把行
// 重置成 pending —— 否则坏 chunk 会被无限重放。失败留痕同事务落 admin_logs。
func TestP6GoWriteRejectsExhaustedRetriesAndAudits(t *testing.T) {
	env := loadP6Env(t)
	env.requirePostMigrate(t)
	h := newP6Harness(t, env)
	db := p6OpenFixtureDB(t, env)
	targets := p6ChunkTargets("go-w8-exhaust")

	code, created, raw := h.submitReindex(t, env, env.docID, targets)
	if code != http.StatusCreated {
		t.Fatalf("夹具行应 201 新建，实际 %d body=%s", code, raw)
	}
	jobID := p6FieldInt(created, "id")
	targetsHash := p6FieldString(created, "targets_hash")

	p6SetJobState(t, db, jobID, "failed", 3, "go-w8-simulated-failure")
	rejectCode, rejected, rejectRaw := h.submitReindex(t, env, env.docID, targets)
	if rejectCode != http.StatusBadRequest {
		t.Fatalf("重试额度用尽应 400，实际 %d body=%s", rejectCode, rejectRaw)
	}
	if got := p6FieldString(rejected, "error_code"); got != "JOB_MAX_RETRIES_REACHED" {
		t.Fatalf("拒绝错误码应为 JOB_MAX_RETRIES_REACHED，实际 %q body=%s", got, rejectRaw)
	}
	if got := p6FieldInt(rejected, "child_job_id"); got != jobID {
		t.Fatalf("拒绝响应必须回读既有行 id=%d 供人工介入，实际 %v", jobID, rejected["child_job_id"])
	}
	if got := p6FieldString(rejected, "targets_hash"); got != targetsHash {
		t.Fatalf("拒绝响应必须带 targets_hash 以便与库列对账，实际 %q", got)
	}
	if got := p6FieldInt(rejected, "retry_count"); got != 3 {
		t.Fatalf("拒绝响应必须带 retry_count，实际 %d", got)
	}
	if got := p6FieldString(rejected, "reason"); got != "retry_exhausted" {
		t.Fatalf("拒绝原因应为 retry_exhausted，实际 %q", got)
	}
	// 拒绝分支不动行：状态与配额都必须原样留着，否则失败任务被无限重放。
	if status, retryCount, errorMessage := p6JobRowState(t, db, jobID); status != "failed" || retryCount != 3 || errorMessage != "go-w8-simulated-failure" {
		t.Fatalf("拒绝后行必须仍是 failed/3/保留原 error_message，实际 %s/%d/%q", status, retryCount, errorMessage)
	}

	failedLogs := 0
	for _, row := range h.jobLogsDetails(t, env, jobID) {
		action, _ := row["action"].(string)
		if action != adminstore.ReindexAuditActionRejected {
			continue
		}
		failedLogs++
		details := auditDetails(row)
		if got, _ := details["reason"].(string); got != "retry_exhausted" {
			t.Fatalf("失败留痕 reason 应为 retry_exhausted，实际 %q", got)
		}
		if got, _ := details["targets_hash"].(string); got != targetsHash {
			t.Fatalf("失败留痕 targets_hash 应与库列一致，实际 %q", got)
		}
		if id, ok := details["child_job_id"].(float64); !ok || int(id) != jobID {
			t.Fatalf("失败留痕 child_job_id 必须指向既有行 %d，实际 %v", jobID, details["child_job_id"])
		}
		if _, legacy := details["job_id"]; legacy {
			t.Fatalf("失败留痕不得退回旧键名 job_id：%#v", details)
		}
		chunkIDs, _ := details["chunk_ids"].([]interface{})
		if len(chunkIDs) != 2 {
			t.Fatalf("失败留痕必须列出被拒的 chunk_ids 供人工重放，实际 %#v", details["chunk_ids"])
		}
		if status, _ := row["status"].(string); status != "failed" {
			t.Fatalf("失败留痕所在日志行 status 应为 failed，实际 %q", status)
		}
	}
	if failedLogs != 1 {
		t.Fatalf("一次拒绝应恰好留 1 条 %s 审计，实际 %d 条", adminstore.ReindexAuditActionRejected, failedLogs)
	}
	// 拒绝留痕同样是可追责的：库侧那一行必须带提交人，且状态是 failed（不是 success）。
	rejectedRows := p6AuditOperatorRows(t, db, jobID)
	rejectedFound := 0
	for _, row := range rejectedRows {
		if row.Action != adminstore.ReindexAuditActionRejected {
			continue
		}
		rejectedFound++
		if row.UserID == nil || *row.UserID != env.operatorID || row.OperatorID == nil || *row.OperatorID != env.operatorID {
			t.Fatalf("拒绝留痕的 user_id/operator_id 应为操作员 %d，实际 %s/%s", env.operatorID, p6IntValue(row.UserID), p6IntValue(row.OperatorID))
		}
		if row.Status != "failed" {
			t.Fatalf("拒绝留痕在库里也必须是 failed，实际 %q", row.Status)
		}
	}
	if rejectedFound != 1 {
		t.Fatalf("库侧应命中 1 条 %s 留痕，实际 %d 条（共 %d 行）", adminstore.ReindexAuditActionRejected, rejectedFound, len(rejectedRows))
	}
}

// 判据 12（并发唯一性）：两个 goroutine 同时提交同一批 targets。ON CONFLICT DO NOTHING
// + 部分唯一索引是第一道线，冲突后的 SELECT ... FOR UPDATE 回读是第二道线 —— 两者都在
// 真实 PG 上串行化。判据：两次都成功、回读同一个 child ID、库里只有一行，且最多一个 201。
func TestP6GoWriteConcurrentSameHashKeepsSingleRow(t *testing.T) {
	env := loadP6Env(t)
	env.requirePostMigrate(t)
	// 并发提交会并发写 logger，日志指到 io.Discard（取证不靠它）。
	h := newP6HarnessWithLogWriter(t, env, io.Discard)
	db := p6OpenFixtureDB(t, env)
	targets := p6ChunkTargets("go-w8-conc")

	type attempt struct {
		code int
		data map[string]interface{}
		raw  string
	}
	results := make([]attempt, 2)
	var wg sync.WaitGroup
	for index := 0; index < 2; index++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			code, data, raw := h.submitReindexRaw(env, env.docID, targets)
			results[i] = attempt{code: code, data: data, raw: raw}
		}(index)
	}
	wg.Wait()

	ids := make([]int, 0, 2)
	hashes := make([]string, 0, 2)
	created201 := 0
	for _, res := range results {
		if res.code != http.StatusOK && res.code != http.StatusCreated {
			t.Fatalf("并发同 hash 提交不得失败（既不是 409 也不是 500 的地方冒出了 %d）body=%s", res.code, res.raw)
		}
		if res.code == http.StatusCreated {
			created201++
		}
		id := p6FieldInt(res.data, "id")
		if id <= 0 {
			t.Fatalf("并发提交必须回读到 child ID，实际 body=%s", res.raw)
		}
		ids = append(ids, id)
		hashes = append(hashes, p6FieldString(res.data, "targets_hash"))
	}
	if created201 > 1 {
		t.Fatalf("并发同 hash 最多只能有一个 201 新建，实际 %d 个", created201)
	}
	if ids[0] != ids[1] {
		t.Fatalf("并发同 hash 必须落在同一行，实际两行 id=%d / %d", ids[0], ids[1])
	}
	if hashes[0] != hashes[1] || hashes[0] == "" {
		t.Fatalf("两个并发请求算出的 targets_hash 必须相同且非空，实际 %q / %q", hashes[0], hashes[1])
	}
	if got := p6CountRowsByHash(t, db, hashes[0]); got != 1 {
		t.Fatalf("并发后库里该 hash 应只有 1 行，实际 %d 行（FOR UPDATE / 部分唯一索引有一道没生效）", got)
	}
}

// 判据 13（审计留痕形状跨语言一致）：Go 写侧的 admin_logs.details 键集合必须与 Python
// audit_details() 的 13 个冻结键**完全相同**（不多不少），运维才能用同一条检索式覆盖
// 两条写入路径；出现旧键名 job_id 或多出/缺少任一键都算口径分叉。
func TestP6GoWriteAuditMatchesPythonFrozenKeys(t *testing.T) {
	env := loadP6Env(t)
	env.requirePostMigrate(t)
	h := newP6Harness(t, env)
	db := p6OpenFixtureDB(t, env)

	code, created, raw := h.submitReindex(t, env, env.docID, p6ChunkTargets("go-w8-audit"))
	if code != http.StatusCreated {
		t.Fatalf("夹具行应 201 新建，实际 %d body=%s", code, raw)
	}
	jobID := p6FieldInt(created, "id")

	rows := h.jobLogsDetails(t, env, jobID)
	if len(rows) != 1 {
		t.Fatalf("一次 Go 新建应只留 1 条审计，实际 %d 行", len(rows))
	}
	// 库侧同一条检索式必须给出同一行 —— 读侧接口筛得掉不等于库里没写，运维查的是后者。
	dbRows := p6AuditOperatorRows(t, db, jobID)
	if len(dbRows) != 1 {
		t.Fatalf("库侧 resource='job' AND resource_id='%d' 应命中 1 条留痕，实际 %d 条", jobID, len(dbRows))
	}
	// 留痕必须指名提交人：insertJobAuditLogEntry 把同一个 operatorID 同时写进 user_id 与
	// operator_id（jobs.go:646-667），缺一半就说明审计链没有可追责的身份。
	if dbRows[0].UserID == nil || *dbRows[0].UserID != env.operatorID {
		t.Fatalf("审计 user_id 应为操作员 %d，实际 %s", env.operatorID, p6IntValue(dbRows[0].UserID))
	}
	if dbRows[0].OperatorID == nil || *dbRows[0].OperatorID != env.operatorID {
		t.Fatalf("审计 operator_id 应为操作员 %d，实际 %s", env.operatorID, p6IntValue(dbRows[0].OperatorID))
	}
	if dbRows[0].Action != adminstore.ReindexAuditActionCreated || dbRows[0].Status != "success" {
		t.Fatalf("留痕应为 success 的 %s，实际 %q/%q", adminstore.ReindexAuditActionCreated, dbRows[0].Action, dbRows[0].Status)
	}
	action, _ := rows[0]["action"].(string)
	if action != adminstore.ReindexAuditActionCreated {
		t.Fatalf("新建留痕 action 应为 job_created，实际 %q", action)
	}
	details := auditDetails(rows[0])

	frozen := []string{
		"job_type", "kb_id", "status", "outcome", "targets_hash", "child_job_id", "target_count", "source",
		"created", "reused", "retried", "reset", "rejected",
	}
	for _, key := range frozen {
		if _, ok := details[key]; !ok {
			t.Fatalf("Go 审计缺 Python 冻结键 %q，实际 %#v", key, details)
		}
	}
	if len(details) != len(frozen) {
		t.Fatalf("Go 审计键数必须恰好 %d（多出的键会让两侧检索式分叉），实际 %d：%#v", len(frozen), len(details), details)
	}
	if _, legacy := details["job_id"]; legacy {
		t.Fatalf("Go 审计不得使用旧键名 job_id：%#v", details)
	}
	if got, _ := details["outcome"].(string); got != adminstore.ReindexOutcomeCreated {
		t.Fatalf("outcome 应为 created，实际 %q", got)
	}
	if got, _ := details["job_type"].(string); got != adminstore.ReindexChunksJobType {
		t.Fatalf("details.job_type 应为 %s，实际 %q", adminstore.ReindexChunksJobType, got)
	}
	if got, _ := details["source"].(string); got != "admin_api" {
		t.Fatalf("details.source 必须记提交来源 admin_api（与 CreateJob 路径可区分），实际 %q", got)
	}
	if got, _ := details["targets_hash"].(string); got != p6FieldString(created, "targets_hash") {
		t.Fatalf("审计里的 targets_hash 必须与响应体/库列同值，实际 %q", got)
	}
	if id, ok := details["child_job_id"].(float64); !ok || int(id) != jobID {
		t.Fatalf("details.child_job_id 必须指向本次锁到的行 %d，实际 %v", jobID, details["child_job_id"])
	}
	if got, _ := details["target_count"].(float64); int(got) != 2 {
		t.Fatalf("details.target_count 应为 2，实际 %v", details["target_count"])
	}
	// 聚合计数：一条 Go 单批提交的新建，created=1 且其余为 0 —— 与 Python 的报告形状同源。
	counters := map[string]int{"created": 1, "reused": 0, "retried": 0, "reset": 0, "rejected": 0}
	for key, want := range counters {
		if got, ok := details[key].(float64); !ok || int(got) != want {
			t.Fatalf("details.%s 应为 %d，实际 %v", key, want, details[key])
		}
	}
}

// 判据 14（作用域校验在入队之前）：跨租户的 tenant_id 与不存在的 kb_id 都必须在
// EnqueueReindexChunks 之前就拒掉，且该 KB 下的 reindex_chunks 行数一行都不许增加。
// 放行到 store 才拒 = 假防线。KB_CROSS_SCOPE 按契约 §2.9 映射 400（不是 403）。
func TestP6GoWriteBlocksCrossScopeAndUnknownKBBeforeEnqueue(t *testing.T) {
	env := loadP6Env(t)
	env.requirePostMigrate(t)
	h := newP6Harness(t, env)
	db := p6OpenFixtureDB(t, env)

	countKB := func() int {
		var n int
		if err := db.QueryRow(`SELECT count(*) FROM admin_jobs WHERE job_type = 'reindex_chunks' AND kb_id = $1`, env.kbID).Scan(&n); err != nil {
			t.Fatalf("统计该 KB 的 reindex_chunks 行数失败: %v", err)
		}
		return n
	}
	before := countKB()
	targets := p6ChunkTargets("go-w8-scope")

	cases := []struct {
		name       string
		kbID       string
		tenantID   string
		wantCode   int
		wantErrKey string
	}{
		{"跨租户", env.kbID, "not-" + env.tenantID, http.StatusBadRequest, "KB_CROSS_SCOPE"},
		{"知识库不存在", "kb-does-not-exist", env.tenantID, http.StatusNotFound, "KB_NOT_FOUND"},
	}
	for _, tc := range cases {
		path := "/api/v1/admin/jobs/reindex-chunks?kb_id=" + tc.kbID + "&tenant_id=" + tc.tenantID + "&project_id=" + env.projectID
		body := `{"kb_id":"` + tc.kbID + `","max_retries":3,"payload":{"doc_id":"` + env.docID + `","targets":[` + targets + `]}}`
		code, resp, raw := h.do(t, http.MethodPost, path, body)
		if code != tc.wantCode {
			t.Fatalf("%s 应 %d，实际 %d body=%s", tc.name, tc.wantCode, code, raw)
		}
		if got := errorCodeOf(t, resp); got != tc.wantErrKey {
			t.Fatalf("%s 错误码应为 %s，实际 %q", tc.name, tc.wantErrKey, got)
		}
	}
	if h.store.enqueueCalls != 0 {
		t.Fatalf("作用域不匹配必须在入队前拒，实际 EnqueueReindexChunks 调用 %d 次", h.store.enqueueCalls)
	}
	if h.store.createCalls != 0 {
		t.Fatalf("作用域不匹配也不得回落到 CreateJob，实际调用 %d 次", h.store.createCalls)
	}
	if after := countKB(); after != before {
		t.Fatalf("两次被拒的提交不该留下任何行，该 KB 行数 %d → %d", before, after)
	}
	t.Logf("P6_W8_SCOPE_EVIDENCE kb=%s reindex_chunks_rows=%d enqueueCalls=0 createCalls=0", env.kbID, before)
}
