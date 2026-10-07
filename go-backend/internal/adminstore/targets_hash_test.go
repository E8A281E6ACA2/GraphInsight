package adminstore

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type targetsHashVector struct {
	Name      string `json:"name"`
	Canonical string `json:"canonical"`
	Hash      string `json:"hash"`
	Input     []struct {
		ChunkID        string `json:"chunk_id"`
		TargetRevision int    `json:"target_revision"`
	} `json:"input"`
}

// 跨语言字节对等契约：向量由 backend/services/reindex_queue.py:canonical_targets_hash
// 真实产出（见 backend/tests/_w8_gen_vectors.py），不是手写期望值。
// Go 算出的 hash 与 Python 不同 = 同一批 targets 在两条写入路径上落成两行，
// §16.3 的"重跑复用同一 job"直接失效，所以这里必须逐字比对。
func TestCanonicalTargetsHashMatchesPythonVectors(t *testing.T) {
	raw, err := os.ReadFile(filepath.Join("testdata", "targets_hash_vectors.json"))
	if err != nil {
		t.Fatalf("读取对等向量失败: %v", err)
	}
	var doc struct {
		Vectors []targetsHashVector `json:"vectors"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("解析对等向量失败: %v", err)
	}
	if len(doc.Vectors) == 0 {
		t.Fatal("对等向量文件为空契约，等于没测")
	}
	for _, vector := range doc.Vectors {
		targets := make([]CanonicalTarget, 0, len(vector.Input))
		for _, item := range vector.Input {
			targets = append(targets, CanonicalTarget{ChunkID: item.ChunkID, TargetRevision: item.TargetRevision})
		}
		if got := canonicalTargetsJSON(targets); got != vector.Canonical {
			t.Errorf("%s 规范化文本不一致\n Go: %s\n Py: %s", vector.Name, got, vector.Canonical)
			continue
		}
		if got := CanonicalTargetsHash(targets); got != vector.Hash {
			t.Errorf("%s hash 不一致\n Go: %s\n Py: %s", vector.Name, got, vector.Hash)
		}
	}
}

// 排序是 hash 的一部分：乱序输入必须收敛到同一文本（basic_2 与 order_swapped 同 hash
// 已在向量里钉住，这里额外钉住"调用方切片不被就地改写"）。
func TestCanonicalTargetsHashDoesNotMutateInput(t *testing.T) {
	targets := []CanonicalTarget{
		{ChunkID: "c2", TargetRevision: 2},
		{ChunkID: "c1", TargetRevision: 1},
	}
	first := CanonicalTargetsHash(targets)
	if targets[0].ChunkID != "c2" || targets[1].ChunkID != "c1" {
		t.Fatalf("输入切片被就地排序改写: %+v", targets)
	}
	if CanonicalTargetsHash(targets) != first {
		t.Fatal("同一输入两次 hash 不稳定")
	}
}
