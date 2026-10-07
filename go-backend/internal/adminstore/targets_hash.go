package adminstore

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
)

// CanonicalTarget 是 §16.3 targets_hash 的全部输入字段。
// 多加一个字段、换一个键名、改一处排序都会改变 hash，进而让"同批 targets 重跑复用同一 job"失效。
type CanonicalTarget struct {
	ChunkID        string
	TargetRevision int
}

// CanonicalTargetsHash 与 backend/services/reindex_queue.py:canonical_targets_hash 字节级一致。
//
// Go 不能直接用 encoding/json 出这份文本，两处会静默分叉：
//  1. Go 默认把 < > & 转成 \u003c \u003e \u0026（SetEscapeHTML），Python 不转；
//  2. Go 输出原始 UTF-8 字节，Python json.dumps(ensure_ascii=True) 输出 \uXXXX。
//
// 因此这里手写成 Python 编码器。排序按 (chunk_id, target_revision)：Go 字符串比较是
// UTF-8 字节序，与 Python 的码点序对合法 UTF-8 等价。
func CanonicalTargetsHash(targets []CanonicalTarget) string {
	sum := sha256.Sum256([]byte(canonicalTargetsJSON(targets)))
	return hex.EncodeToString(sum[:])
}

func canonicalTargetsJSON(targets []CanonicalTarget) string {
	ordered := make([]CanonicalTarget, len(targets))
	copy(ordered, targets)
	sort.SliceStable(ordered, func(i, j int) bool {
		if ordered[i].ChunkID != ordered[j].ChunkID {
			return ordered[i].ChunkID < ordered[j].ChunkID
		}
		return ordered[i].TargetRevision < ordered[j].TargetRevision
	})

	var builder strings.Builder
	builder.WriteByte('[')
	for index, target := range ordered {
		if index > 0 {
			builder.WriteByte(',')
		}
		builder.WriteByte('{')
		builder.WriteString(`"chunk_id":`)
		builder.WriteString(encodePythonJSONString(target.ChunkID))
		builder.WriteString(`,"target_revision":`)
		builder.WriteString(strconv.Itoa(target.TargetRevision))
		builder.WriteByte('}')
	}
	builder.WriteByte(']')
	return builder.String()
}

// encodePythonJSONString 复刻 Python json.encoder.encode_basestring_ascii：
// 引号与反斜杠转义、\b \f \n \r \t 用短转义、其余 C0 控制符和所有非 ASCII 走小写 \uXXXX
// （星平面字符拆代理对）；/ < > & 保持字面量。
func encodePythonJSONString(value string) string {
	var builder strings.Builder
	builder.WriteByte('"')
	for offset, runeValue := range value {
		switch runeValue {
		case '"':
			builder.WriteString(`\"`)
		case '\\':
			builder.WriteString(`\\`)
		case '\b':
			builder.WriteString(`\b`)
		case '\f':
			builder.WriteString(`\f`)
		case '\n':
			builder.WriteString(`\n`)
		case '\r':
			builder.WriteString(`\r`)
		case '\t':
			builder.WriteString(`\t`)
		default:
			switch {
			case runeValue < 0x20:
				writePythonUnicodeEscape(&builder, runeValue)
			case runeValue <= 0x7E:
				builder.WriteString(value[offset : offset+1])
			default:
				writePythonUnicodeEscape(&builder, runeValue)
			}
		}
	}
	builder.WriteByte('"')
	return builder.String()
}

func writePythonUnicodeEscape(builder *strings.Builder, runeValue rune) {
	if runeValue > 0xFFFF {
		value := runeValue - 0x10000
		writeHex4(builder, 0xD800+(value>>10))
		writeHex4(builder, 0xDC00+(value&0x3FF))
		return
	}
	writeHex4(builder, runeValue)
}

func writeHex4(builder *strings.Builder, value rune) {
	const digits = "0123456789abcdef"
	builder.WriteString(`\u`)
	builder.WriteByte(digits[(value>>12)&0xF])
	builder.WriteByte(digits[(value>>8)&0xF])
	builder.WriteByte(digits[(value>>4)&0xF])
	builder.WriteByte(digits[value&0xF])
}
