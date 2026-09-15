package tools

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// 브레이커 전이 3종(§15.2-3): 연속 실패 N→open, cooldown 후 half-open
// 성공→close, half-open 실패→open 연장.
func TestBreakerTransitions(t *testing.T) {
	now := time.Unix(1000, 0)
	b := NewBreaker()
	b.failsMax, b.cooldown = 3, 30*time.Second
	b.now = func() time.Time { return now }
	boom := errors.New("x")

	for i := 0; i < 3; i++ {
		if err := b.Allow("vm"); err != nil {
			t.Fatalf("실패 %d회째 전 Allow 거부: %v", i+1, err)
		}
		b.Record("vm", boom)
	}
	var be *BackendError
	if err := b.Allow("vm"); !errors.As(err, &be) || be.Detail != "circuit_open" {
		t.Fatalf("3연속 실패 후 open이어야 함: %v", err)
	}
	// 다른 백엔드는 독립이다.
	if err := b.Allow("ch"); err != nil {
		t.Fatalf("무관 백엔드가 막힘: %v", err)
	}

	// cooldown 경과 → half-open 시험 1회만 허용.
	now = now.Add(31 * time.Second)
	if err := b.Allow("vm"); err != nil {
		t.Fatalf("half-open 시험이 거부됨: %v", err)
	}
	if err := b.Allow("vm"); err == nil {
		t.Fatal("half-open 중 두 번째 조회가 통과 — 시험은 1회여야 함")
	}
	// 시험 실패 → open 연장(즉시).
	b.Record("vm", boom)
	if err := b.Allow("vm"); err == nil {
		t.Fatal("half-open 실패 후 즉시 open 연장이어야 함")
	}
	// 재-cooldown 후 시험 성공 → close.
	now = now.Add(31 * time.Second)
	if err := b.Allow("vm"); err != nil {
		t.Fatalf("재시험 거부: %v", err)
	}
	b.Record("vm", nil)
	if err := b.Allow("vm"); err != nil {
		t.Fatalf("성공 후 close여야 함: %v", err)
	}
}

// ctx 취소는 백엔드 실패로 세지 않는다 — run이 죽는 것이지 저장소가
// 죽은 것이 아니다.
func TestBreakerIgnoresCancel(t *testing.T) {
	b := NewBreaker()
	b.failsMax = 1
	b.Record("vm", context.Canceled)
	if err := b.Allow("vm"); err != nil {
		t.Fatalf("취소가 open을 만들었다: %v", err)
	}
}

// withBackendGuard: BackendError → no_data(backend_error) 봉투(토큰만),
// ctx 취소 중에는 봉투가 아니라 오류 그대로(§15.2-5).
func TestWithBackendGuardEnvelope(t *testing.T) {
	tool := llm.Tool{Name: "t", Call: func(ctx context.Context, _ json.RawMessage) (any, error) {
		return nil, &BackendError{Backend: "ch", Detail: "http_5xx", Err: errors.New("HTTP 503: 원문")}
	}}
	out, err := withBackendGuard(tool).Call(context.Background(), nil)
	if err != nil {
		t.Fatalf("봉투 사상이어야 함: %v", err)
	}
	env := out.(Envelope)
	if env.Status != "no_data" || env.NoDataReason != NoDataBackendError ||
		env.Backend != "ch" || env.BackendDetail != "http_5xx" {
		t.Fatalf("봉투 내용: %+v", env)
	}
	if s := env.Summary; strings.Contains(s, "503") || strings.Contains(s, "원문") {
		t.Fatalf("봉투에 원문 오류 문자열이 샜다(§15.3-2): %q", s)
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := withBackendGuard(tool).Call(ctx, nil); err == nil {
		t.Fatal("취소 중에는 오류 그대로여야 함")
	}
}

// 사상표는 registry 표면과 1:1이어야 한다(§15.2-3 — 표가 정본, §13.1
// 시험 목록의 분모). Toolset의 이름 전수가 표에 있고, 표의 이름 전수가
// 표면에 있다.
func TestToolBackendDepsMatchSurface(t *testing.T) {
	surface := map[string]bool{}
	for _, tl := range Toolset(Stores{PG: nil, CH: &CH{}, VM: &VM{}}, time.Time{}, time.Time{}) {
		surface[tl.Name] = true
	}
	for name := range surface {
		if _, ok := ToolBackendDeps[name]; !ok {
			t.Errorf("표면 도구 %s가 사상표에 없음", name)
		}
	}
	for name := range ToolBackendDeps {
		if !surface[name] {
			t.Errorf("사상표의 %s가 표면에 없음", name)
		}
	}
	// 다중 원천 실측 9종(4차 A-16) — 표가 이를 유지해야 한다.
	multi := 0
	for _, deps := range ToolBackendDeps {
		if len(deps) > 1 {
			multi++
		}
	}
	if multi != 9 {
		t.Errorf("다중 원천 %d종 — 실측 9종(A-16)이어야 함", multi)
	}
	if got := len(ToolsOn("vm", "")); got == 0 {
		t.Error("ToolsOn(vm) 0건 — 기계 생성 표면이 비었다")
	}
}

// D-1(6b 검증): half-open 시험 허가가 백엔드에 나가기 전에 좌초하면
// (bulkhead·ctx) 반납돼야 한다 — 안 하면 probing 고착으로 영구 차단.
func TestBreakerProbeAborted(t *testing.T) {
	now := time.Unix(1000, 0)
	b := NewBreaker()
	b.failsMax, b.cooldown = 1, 10*time.Second
	b.now = func() time.Time { return now }
	b.Record("vm", errors.New("x")) // open

	now = now.Add(11 * time.Second)
	if err := b.Allow("vm"); err != nil {
		t.Fatalf("half-open 허가 거부: %v", err)
	}
	b.abortProbe("vm") // 시험이 좌초 — 허가 반납
	if err := b.Allow("vm"); err != nil {
		t.Fatalf("반납 후 재시험이 거부됨 — probing 고착(D-1): %v", err)
	}
}

// D-2(6b 검증): cap_exceeded·http_4xx는 백엔드 건강의 신호가 아니다 —
// 몇 번이 와도 브레이커를 열지 않는다.
func TestBreakerIgnoresNonBackendSignals(t *testing.T) {
	b := NewBreaker()
	b.failsMax = 1
	for _, detail := range []string{"cap_exceeded", "http_4xx"} {
		b.Record("ch", &BackendError{Backend: "ch", Detail: detail})
	}
	if err := b.Allow("ch"); err != nil {
		t.Fatalf("비신호 오류가 브레이커를 열었다(D-2): %v", err)
	}
	b.Record("ch", &BackendError{Backend: "ch", Detail: "http_5xx", Err: errors.New("x")})
	if err := b.Allow("ch"); err == nil {
		t.Fatal("5xx는 신호다 — open이어야 함")
	}
}
