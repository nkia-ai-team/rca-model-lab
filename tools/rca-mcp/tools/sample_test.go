package tools

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// 가드 경로는 CH 접속 전에 판정되므로 hermetic으로 검증한다 — 오류엔
// 복구 정보(유효값 안내)가 실려야 한다(도구 계약 §2).
func TestSampleLogsGuards(t *testing.T) {
	tool := NewSampleLogsTool(nil, time.Now())
	call := func(m map[string]any) error {
		b, _ := json.Marshal(m)
		_, err := tool.Call(context.Background(), b)
		return err
	}
	base := map[string]any{"target": "11111111-2222-3333-4444-555555555555",
		"from": "2026-07-21T00:00:00Z", "to": "2026-07-21T01:00:00Z"}
	with := func(kv map[string]any) map[string]any {
		m := map[string]any{}
		for k, v := range base {
			m[k] = v
		}
		for k, v := range kv {
			m[k] = v
		}
		return m
	}

	if err := call(with(map[string]any{"mode": "browse"})); err == nil || !strings.Contains(err.Error(), "map") {
		t.Fatalf("무효 mode에 유효값 안내 기대, got %v", err)
	}
	if err := call(with(map[string]any{"mode": "grep"})); err == nil || !strings.Contains(err.Error(), "template_id") {
		t.Fatalf("grep 무검색어에 복구 정보 기대, got %v", err)
	}
	if err := call(with(map[string]any{"mode": "grep", "query": "x", "severity_min": "CRITICAL"})); err == nil ||
		!strings.Contains(err.Error(), "WARN") {
		t.Fatalf("severity_min 무효값에 유효 목록 기대, got %v", err)
	}
	if err := call(with(map[string]any{"mode": "map", "baseline_from": "어제"})); err == nil ||
		!strings.Contains(err.Error(), "RFC3339") {
		t.Fatalf("baseline override 형식 오류 안내 기대, got %v", err)
	}
}

func TestTruncBody(t *testing.T) {
	long := strings.Repeat("a", sampleBodyCap+100)
	if got := truncBody(long); !strings.HasSuffix(got, "…(잘림)") || len(got) >= len(long) {
		t.Fatalf("본문 잘림 실패: len=%d", len(got))
	}
	if got := truncBody("짧은 줄"); got != "짧은 줄" {
		t.Fatalf("짧은 본문 변형: %q", got)
	}
}
