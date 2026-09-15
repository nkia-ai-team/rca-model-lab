// 도구 공통 인자 파싱 — target+from+to 3인자 도구의 공유 헬퍼.
// (get_events에서 살다가 baad4e9 계열 자리 교체로 events.go가 사라지며
// 독립 파일로 이동.)
package tools

import (
	"encoding/json"
	"fmt"
	"time"
)

// parseTargetWindow는 target+from+to 3인자 도구의 공통 파싱·검증이다.
type targetWindow struct {
	Target, From, To string
	FromT, ToT       time.Time
}

func parseTargetWindow(args json.RawMessage) (targetWindow, error) {
	var in targetWindow
	if err := json.Unmarshal(args, &in); err != nil {
		return in, fmt.Errorf(`인자 오류: {"target": "<uuid>", "from": "<RFC3339>", "to": "<RFC3339>"} 필요`)
	}
	if !uuidRe.MatchString(in.Target) {
		return in, fmt.Errorf("%q는 target_id가 아님 — target_id는 순수 UUID다(접두 없음)", in.Target)
	}
	var err1, err2 error
	in.FromT, err1 = time.Parse(time.RFC3339, in.From)
	in.ToT, err2 = time.Parse(time.RFC3339, in.To)
	if err1 != nil || err2 != nil || !in.FromT.Before(in.ToT) {
		return in, fmt.Errorf("시간창 오류: from·to는 UTC RFC3339(예: 2026-07-15T03:00:00Z), from < to 필요 (받은 값 from=%q to=%q)", in.From, in.To)
	}
	return in, nil
}

// asInt는 CH JSONEachRow의 숫자(float64)나 문자열 숫자를 int로 바꾼다.
func asInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	}
	return 0
}
