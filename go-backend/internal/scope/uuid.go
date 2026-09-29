package scope

import (
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"sync/atomic"
	"time"
)

// fallbackUUIDCounter crypto/rand 不可用时的进程内计数器，
// 与 internal/httpserver/middleware.go newTraceID 的降级策略一致。
var fallbackUUIDCounter atomic.Uint64

// NewUUID 生成 RFC 4122 v4 UUID（契约 §2.3：服务端稳定身份，与文件路径解耦，不复用）。
// 仅使用标准库 crypto/rand，不引入第三方依赖；crypto/rand 读取失败时
// 退化为时间戳 + 计数器派生值，保证进程内不重复。
func NewUUID() string {
	var buf [16]byte
	if _, err := rand.Read(buf[:]); err != nil {
		counter := fallbackUUIDCounter.Add(1)
		binary.BigEndian.PutUint64(buf[:8], uint64(time.Now().UnixNano()))
		binary.BigEndian.PutUint64(buf[8:], counter)
	}
	buf[6] = (buf[6] & 0x0f) | 0x40 // version 4
	buf[8] = (buf[8] & 0x3f) | 0x80 // RFC 4122 variant
	return fmt.Sprintf("%x-%x-%x-%x-%x", buf[0:4], buf[4:6], buf[6:8], buf[8:10], buf[10:16])
}
