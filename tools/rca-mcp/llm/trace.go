// LLM 호출 궤적 — 판단의 "입력"을 남긴다. 장부(event sourcing)가
// 판단의 결과를 보존하는 것과 짝: 실패 분석("왜 이 가설/근거가
// 나왔나")은 모델이 실제로 받은 프롬프트와 원문 응답, 도구 응답
// 전문이 있어야 가능하다 (설계 §11.3의 확장, 실 주행 디버깅 경험).
//
// RCA_LLM_DEBUG(stderr, 잘린 요약)는 콘솔 관찰용이고, TraceLog는
// run 산출물용 정본이다 — 둘은 용도가 다르다.
package llm

import (
	"encoding/json"
	"io"
	"sync"
	"time"
)

// TraceLog는 LLM 호출 단위 궤적을 JSON Lines로 기록한다. Client가
// 값 복사되어도 공유되도록 포인터로 꽂는다.
type TraceLog struct {
	mu sync.Mutex
	w  io.Writer
}

func NewTraceLog(w io.Writer) *TraceLog { return &TraceLog{w: w} }

// write는 한 호출의 궤적 한 줄이다. 직렬화 실패는 궤적의 문제이지
// 수사의 문제가 아니므로 조용히 버린다 — 궤적이 본 흐름을 죽이면 안 된다.
func (t *TraceLog) write(entry map[string]any) {
	if t == nil {
		return
	}
	entry["ts"] = time.Now().UTC().Format(time.RFC3339Nano)
	b, err := json.Marshal(entry)
	if err != nil {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.w.Write(append(b, '\n'))
}
