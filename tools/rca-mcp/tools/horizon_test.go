// 도착 수평선 → observed_range 계산 시험(§14-5 5c 결정 ①).
// fail-open 금지 경계: epoch(빈 스트림)·창 시작 이전 수평선은 주장 없음,
// 창 끝 초과 수평선은 창 끝으로 캡(커버 완전 = lag 0의 원천).
package tools

import (
	"testing"
	"time"
)

func TestHorizonObservedRange(t *testing.T) {
	from := time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC)
	to := from.Add(time.Hour)

	// 수평선이 창 중간 — To가 수평선까지로 좁혀져 lag>0의 원천이 된다.
	mid := from.Add(30 * time.Minute)
	if r := horizonObservedRange(from, to, mid); r == nil || !r.To.Equal(mid) || !r.From.Equal(from) {
		t.Fatalf("창 중간 수평선: %+v", r)
	}
	// 수평선이 창 끝 이후 — 창 끝으로 캡(커버 완전).
	if r := horizonObservedRange(from, to, to.Add(5*time.Minute)); r == nil || !r.To.Equal(to) {
		t.Fatalf("창 끝 초과 수평선 캡 실패: %+v", r)
	}
	// 빈 스트림의 max = epoch — 주장 없음(fail-closed).
	if r := horizonObservedRange(from, to, time.Unix(0, 0)); r != nil {
		t.Fatalf("epoch 수평선이 주장을 만들었다: %+v", r)
	}
	if r := horizonObservedRange(from, to, time.Time{}); r != nil {
		t.Fatalf("zero 수평선이 주장을 만들었다: %+v", r)
	}
	// 수평선이 창 시작 이전 — 역전 범위 금지.
	if r := horizonObservedRange(from, to, from.Add(-time.Minute)); r != nil {
		t.Fatalf("창 이전 수평선이 주장을 만들었다: %+v", r)
	}
}

func TestChParseTS(t *testing.T) {
	for _, s := range []string{"2026-08-10 12:00:00.123456789", "2026-08-10 12:00:00", "2026-08-10T12:00:00Z"} {
		if _, ok := chParseTS(s); !ok {
			t.Fatalf("해석 실패: %q", s)
		}
	}
	if _, ok := chParseTS("확실히 시각 아님"); ok {
		t.Fatal("비시각 문자열이 통과했다")
	}
}
