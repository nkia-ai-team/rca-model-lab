// 토큰 계정 시험 — §14-4 4a 완료 기준 ④(usage 파싱 + run 누적, 두 경로 캡처).
//
// 응답 본문은 **라이브 실측 서식**이다(2026-08-03, vLLM 0.21.0 /
// gemma4-26b-a4b-it-fp8): usage 절에 prompt_tokens·completion_tokens·
// total_tokens와 잉여 키(prompt_tokens_details)가 함께 실린다.
package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

const liveUsage = `"usage":{"prompt_tokens":14,"total_tokens":19,"completion_tokens":5,"prompt_tokens_details":null}`

func TestUsageParsedFromLiveShape(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintf(w, `{"id":"chatcmpl-1","object":"chat.completion","model":"m",
			"choices":[{"index":0,"message":{"role":"assistant","content":"{\"ok\":true}"},
			"finish_reason":"stop"}],%s}`, liveUsage)
	}))
	defer srv.Close()

	meter := &UsageMeter{}
	c := &Client{BaseURL: srv.URL, Model: "m", Usage: meter}
	var out struct {
		OK bool `json:"ok"`
	}
	for i := 0; i < 3; i++ {
		if err := c.CompleteJSON(context.Background(), "s", "u", &out); err != nil {
			t.Fatalf("호출 %d: %v", i, err)
		}
	}
	got := meter.Snapshot()
	want := Usage{PromptTokens: 42, CompletionTokens: 15, TotalTokens: 57, Calls: 3}
	if got != want {
		t.Fatalf("누적 %+v, 기대 %+v", got, want)
	}
}

// total_tokens가 없으면 prompt+completion으로 채운다 — 상한 검사의 분자가
// 백엔드 사정으로 0에 굳지 않게 한다.
func TestUsageDerivesTotal(t *testing.T) {
	var m UsageMeter
	m.add(usage{PromptTokens: 10, CompletionTokens: 4})
	if got := m.Snapshot(); got.TotalTokens != 14 || got.Calls != 1 {
		t.Fatalf("%+v", got)
	}
	// usage 절이 없는 응답은 세지 않는다(빈 계정과 "0 토큰 호출"의 구분).
	m.add(usage{})
	if got := m.Snapshot(); got.Calls != 1 {
		t.Fatalf("usage 없는 응답이 셈에 듦: %+v", got)
	}
}

// 계정 미설정(nil)은 무동작이다 — additive 계약.
func TestUsageNilMeter(t *testing.T) {
	var m *UsageMeter
	m.add(usage{PromptTokens: 1})
	if got := m.Snapshot(); got != (Usage{}) {
		t.Fatalf("nil 계정이 값을 냄: %+v", got)
	}
}

// ChatTools는 **턴마다** 센다 — 마지막 턴만 세면 도구 왕복 지출이 통째로 빠진다.
func TestUsageChatToolsPerTurn(t *testing.T) {
	turn := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		turn++
		if turn == 1 {
			fmt.Fprintf(w, `{"choices":[{"message":{"content":"","tool_calls":[
				{"id":"c1","type":"function","function":{"name":"probe","arguments":"{}"}}]}}],%s}`, liveUsage)
			return
		}
		fmt.Fprintf(w, `{"choices":[{"message":{"content":"끝"}}],%s}`, liveUsage)
	}))
	defer srv.Close()

	meter := &UsageMeter{}
	c := &Client{BaseURL: srv.URL, Model: "m", Usage: meter}
	tool := Tool{Name: "probe", Call: func(context.Context, json.RawMessage) (any, error) {
		return map[string]any{"status": "normal"}, nil
	}}
	if _, _, err := c.ChatTools(context.Background(), "s", "u", []Tool{tool}, 4); err != nil {
		t.Fatalf("ChatTools: %v", err)
	}
	if got := meter.Snapshot(); got.Calls != 2 || got.TotalTokens != 38 {
		t.Fatalf("턴별 누적 실패: %+v", got)
	}
}

// 계정은 run 하나를 공유한다 — 어댑터가 Client를 값으로 복사해 들고 다녀도
// 같은 계정에 쌓이고, 동시 호출에서 값이 깨지지 않는다.
func TestUsageSharedAndConcurrent(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintf(w, `{"choices":[{"message":{"content":"{}"}}],%s}`, liveUsage)
	}))
	defer srv.Close()

	meter := &UsageMeter{}
	base := Client{BaseURL: srv.URL, Model: "m", Usage: meter}
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c := base // 값 복사 — 계정은 포인터라 공유된다
			var out map[string]any
			if err := c.CompleteJSON(context.Background(), "s", "u", &out); err != nil {
				t.Errorf("호출: %v", err)
			}
		}()
	}
	wg.Wait()
	if got := meter.Snapshot(); got.Calls != 8 || got.TotalTokens != 8*19 {
		t.Fatalf("동시 누적 %+v", got)
	}
}

// OnCall 훅 — 호출 하나가 계정에 들어올 때마다 모델·토큰이 넘어온다
// (§14-6 6a — llm_usage 이벤트 배선의 재료). usage 절이 없던 응답(셋 다
// 0)은 add와 같은 이유로 훅도 안 부른다.
func TestUsageMeterOnCall(t *testing.T) {
	var gotModel, gotPurpose string
	var gotTotal int
	calls := 0
	m := &UsageMeter{OnCall: func(model, purpose string, prompt, completion, total int) {
		gotModel, gotPurpose, gotTotal = model, purpose, total
		calls++
	}}
	m.record("m1", "grouper", usage{PromptTokens: 10, CompletionTokens: 5})
	if calls != 1 || gotModel != "m1" || gotTotal != 15 {
		t.Fatalf("훅 호출 %d model=%s total=%d — total 생략 시 합이어야 함", calls, gotModel, gotTotal)
	}
	if gotPurpose != "grouper" {
		t.Fatalf("호출처 딱지 = %q, 기대 grouper(§14-7 ③)", gotPurpose)
	}
	m.record("m1", "grouper", usage{}) // usage 절 없음
	if calls != 1 {
		t.Fatalf("빈 usage에 훅이 불림 (%d)", calls)
	}
	if s := m.Snapshot(); s.TotalTokens != 15 || s.Calls != 1 {
		t.Fatalf("계정 누적: %+v", s)
	}
}

// 호출처 딱지(§14-7 ③) — 컨텍스트 조각이 "/"로 중첩되고, CompleteJSON이
// 그 딱지를 훅에 승계한다. 무딱지 호출은 빈 값 그대로다(지어내지 않는다).
func TestPurposeContextPropagation(t *testing.T) {
	ctx := WithPurpose(context.Background(), "regen")
	ctx = WithPurpose(ctx, "candidates_investigation")
	if got := PurposeFrom(ctx); got != "regen/candidates_investigation" {
		t.Fatalf("중첩 딱지 = %q", got)
	}
	if got := PurposeFrom(context.Background()); got != "" {
		t.Fatalf("무딱지 = %q, 기대 빈 값", got)
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		fmt.Fprintf(w, `{"choices":[{"message":{"content":"{}"}}],%s}`, liveUsage)
	}))
	defer srv.Close()
	var purposes []string
	meter := &UsageMeter{OnCall: func(_, purpose string, _, _, _ int) {
		purposes = append(purposes, purpose)
	}}
	c := &Client{BaseURL: srv.URL, Model: "m", Usage: meter}
	var out map[string]any
	if err := c.CompleteJSON(WithPurpose(context.Background(), "writer"), "s", "u", &out); err != nil {
		t.Fatalf("호출: %v", err)
	}
	if err := c.CompleteJSON(context.Background(), "s", "u", &out); err != nil {
		t.Fatalf("호출: %v", err)
	}
	if len(purposes) != 2 || purposes[0] != "writer" || purposes[1] != "" {
		t.Fatalf("훅 승계 딱지 = %v, 기대 [writer \"\"]", purposes)
	}
}
