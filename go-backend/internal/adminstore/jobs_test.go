package adminstore

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// 本文件钉住 §16.3 / Wave 4 发现 W4-F2 与 W4-F6 的两条语义：
//   1) 建任务白名单 supportedJobTypes 判定本身；
//   2) 白名单拒绝必须发生在任何一次数据库调用之前（fail-closed 且不写库）。

// recordingDriver 只记录"数据库被触达了几次"，任何真实操作都返回 errStoreTouched。
// 用它而不是真库连接，是为了让"零次触达"成为可断言的事实而不是网络失败的副产品。
type recordingDriver struct{}

var (
	storeCalls  int64
	errNotReady = errors.New("recording driver: store must not be reached")
)

func (recordingDriver) Open(string) (driver.Conn, error) { return recordingConn{}, nil }

type recordingConn struct{}

func (recordingConn) Prepare(string) (driver.Stmt, error) {
	atomic.AddInt64(&storeCalls, 1)
	return nil, errNotReady
}
func (recordingConn) Close() error { return nil }
func (recordingConn) Begin() (driver.Tx, error) {
	atomic.AddInt64(&storeCalls, 1)
	return nil, errNotReady
}

func init() { sql.Register("jobrecorder", recordingDriver{}) }

func newRecordingStore(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("jobrecorder", "postgresql://unused/unused")
	if err != nil {
		t.Fatalf("open recording store failed: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return db
}

func callsSince(base int64) int64 { return atomic.LoadInt64(&storeCalls) - base }

func TestValidateJobCreateRequestWhitelist(t *testing.T) {
	t.Parallel()

	long := strings.Repeat("x", 101)
	cases := []struct {
		name    string
		req     JobCreateRequest
		wantErr bool
	}{
		{"build_graph 放行", JobCreateRequest{JobType: "build_graph"}, false},
		{"clear_kb 放行", JobCreateRequest{JobType: "clear_kb"}, false},
		{"reindex 放行", JobCreateRequest{JobType: "reindex"}, false},
		{"reindex_chunks 拒绝", JobCreateRequest{JobType: "reindex_chunks"}, true},
		{"空类型拒绝", JobCreateRequest{JobType: ""}, true},
		{"未知类型拒绝", JobCreateRequest{JobType: "whatever"}, true},
		{"max_retries 下界拒绝", JobCreateRequest{JobType: "build_graph", MaxRetries: -1}, true},
		{"max_retries 上界拒绝", JobCreateRequest{JobType: "build_graph", MaxRetries: 21}, true},
		{"max_retries 边界放行", JobCreateRequest{JobType: "build_graph", MaxRetries: 20}, false},
		{"kb_id 超长拒绝", JobCreateRequest{JobType: "build_graph", KBID: &long}, true},
		{"tenant_id 超长拒绝", JobCreateRequest{JobType: "build_graph", TenantID: &long}, true},
		{"project_id 超长拒绝", JobCreateRequest{JobType: "build_graph", ProjectID: &long}, true},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			err := validateJobCreateRequest(tc.req)
			if tc.wantErr && !errors.Is(err, ErrJobValidation) {
				t.Fatalf("want ErrJobValidation, got %v", err)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("want nil, got %v", err)
			}
		})
	}
}

// CreateJob 在 INSERT 之前拒绝 reindex_chunks：白名单命中即返回，且一次数据库调用都没有发生。
func TestCreateJobRejectsUnsupportedTypeWithoutTouchingStore(t *testing.T) {
	base := atomic.LoadInt64(&storeCalls)
	client := &Client{db: newRecordingStore(t)}

	_, err := client.CreateJob(context.Background(), JobCreateRequest{JobType: "reindex_chunks"})
	if !errors.Is(err, ErrJobValidation) {
		t.Fatalf("want ErrJobValidation, got %v", err)
	}
	if got := callsSince(base); got != 0 {
		t.Fatalf("白名单拒绝不得触达数据库，实际触达 %d 次", got)
	}
}

// 反假绿：同一个 client 放型 build_graph 必须走到数据库层（报触达错而不是校验错）。
// 缺了这条，上一条可能只是"store 未初始化"之类的早退，零触达断言会假绿。
func TestCreateJobSupportedTypeReachesStore(t *testing.T) {
	base := atomic.LoadInt64(&storeCalls)
	client := &Client{db: newRecordingStore(t)}

	_, err := client.CreateJob(context.Background(), JobCreateRequest{JobType: "build_graph"})
	if errors.Is(err, ErrJobValidation) {
		t.Fatalf("build_graph 不应被白名单拒绝，got %v", err)
	}
	if err == nil {
		t.Fatalf("want store error, got nil")
	}
	if got := callsSince(base); got == 0 {
		t.Fatalf("放型必须触达数据库一次以上，实际 0 次（上一条断言将是假绿）")
	}
}

// Wave 5（P2）：作业读取投影的单一真相源。
//
// jobColumns 与 scanJobItem 是"按位置 Scan"的一对多复用（6 个 SELECT/RETURNING 站点共用）。
// 加列只改一边不会编译报错，也不会运行报错 —— 只会把后面所有列静默错位读进错误的字段。
// 这两条测试把"列数 == 扫描目标数"和"targets_hash 能被读出并序列化"钉死。
func TestJobColumnsAndScanTargetsStayAligned(t *testing.T) {
	columns := strings.Split(jobColumns, ",")
	scanner := &captureJobScanner{}
	if _, err := scanJobItem(scanner); err != nil {
		t.Fatalf("scanJobItem: %v", err)
	}
	if len(scanner.dests) != len(columns) {
		t.Fatalf("jobColumns %d 列但 scanJobItem 只有 %d 个目标 —— 位置扫描已错位", len(columns), len(scanner.dests))
	}
	last := strings.TrimSpace(columns[len(columns)-1])
	if last != "targets_hash" {
		t.Fatalf("jobColumns 最后一列应为 targets_hash，实际 %q", last)
	}
	if _, ok := scanner.dests[len(scanner.dests)-1].(*sql.NullString); !ok {
		t.Fatalf("targets_hash 是可空列，扫描目标必须是 *sql.NullString，实际 %T", scanner.dests[len(scanner.dests)-1])
	}
}

func TestScanJobItemReadsTargetsHash(t *testing.T) {
	scanner := &populatedJobScanner{now: time.Now().UTC(), nullString: "sha256:abc", valid: true}
	item, err := scanJobItem(scanner)
	if err != nil {
		t.Fatalf("scanJobItem: %v", err)
	}
	if item.TargetsHash == nil {
		t.Fatalf("targets_hash 未被读出")
	}
	if *item.TargetsHash != "sha256:abc" {
		t.Fatalf("want sha256:abc, got %q", *item.TargetsHash)
	}
	encoded, err := json.Marshal(item)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(encoded), `"targets_hash":"sha256:abc"`) {
		t.Fatalf("响应契约缺少 targets_hash 字段: %s", encoded)
	}
}

// NULL 列必须映射为 nil，前端据此区分"无去重键的历史行"与"有去重键的新行"。
func TestScanJobItemKeepsNullTargetsHash(t *testing.T) {
	item, err := scanJobItem(&populatedJobScanner{now: time.Now().UTC(), nullString: "", valid: false})
	if err != nil {
		t.Fatalf("scanJobItem: %v", err)
	}
	if item.TargetsHash != nil {
		t.Fatalf("NULL targets_hash 应映射为 nil，实际 %q", *item.TargetsHash)
	}
	encoded, _ := json.Marshal(item)
	if strings.Contains(string(encoded), "targets_hash") {
		t.Fatalf("omitempty 字段在 nil 时不应出现: %s", encoded)
	}
}

type captureJobScanner struct{ dests []interface{} }

func (c *captureJobScanner) Scan(dest ...interface{}) error { c.dests = dest; return nil }

// populatedJobScanner 按目标类型灌值，用来验证"列 -> 字段"的落点而不是只数个数。
type populatedJobScanner struct {
	now        time.Time
	nullString string
	valid      bool
}

func (f *populatedJobScanner) Scan(dest ...interface{}) error {
	for _, d := range dest {
		switch ptr := d.(type) {
		case *int:
			*ptr = 1
		case *string:
			*ptr = "reindex_chunks"
		case *sql.NullString:
			*ptr = sql.NullString{String: f.nullString, Valid: f.valid}
		case *sql.NullInt64:
			*ptr = sql.NullInt64{Int64: 9, Valid: true}
		case *sql.NullTime:
			*ptr = sql.NullTime{Time: f.now, Valid: true}
		case *time.Time:
			*ptr = f.now
		default:
			return errNotReady
		}
	}
	return nil
}
