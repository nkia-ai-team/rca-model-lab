// 조사자 어댑터 시험 — §14-4 4b 완료 기준 ①과 1:1 대응한다(§7.1).
package llm

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// selectorWith는 고정 응답을 내는 가짜 백엔드에 붙은 어댑터다. 마지막
// 요청 본문을 남겨 **서식에 무엇이 실렸는지**를 시험이 볼 수 있게 한다.
func selectorWith(t *testing.T, reply string) (*ProbeSelector, *string) {
	t.Helper()
	var body string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		b, _ := io.ReadAll(r.Body)
		body = string(b)
		w.Header().Set("Content-Type", "application/json")
		enc, _ := json.Marshal(reply)
		io.WriteString(w, `{"choices":[{"message":{"content":`+string(enc)+`}}],`+
			`"usage":{"prompt_tokens":10,"completion_tokens":2,"total_tokens":12}}`)
	}))
	t.Cleanup(srv.Close)
	return &ProbeSelector{Client: &Client{BaseURL: srv.URL, Model: "m", Usage: &UsageMeter{}}}, &body
}

func choiceInput() pipeline.ProbeChoiceInput {
	return pipeline.ProbeChoiceInput{
		Wheel: 1, ProbeBudget: 10,
		Hypotheses: []pipeline.HypoBrief{{ID: "H1", Mechanism: "락 경합", Chain: "db-1: 락"}},
		Candidates: []pipeline.ProbeCandidate{{
			PredIDs: []string{"H1-P1", "H2-P1"}, Key: "scan_metrics|db-1|metric||cpu|full",
			Tool: "scan_metrics", TargetID: "db-1", Aspect: "metric", Metric: "cpu",
			Window: "full", Expectations: []string{"H1: must_hold direction_up(necessary)"},
			Priority: pipeline.PriorityDiscriminating, Note: "예측이 갈린다",
		}},
	}
}

// 정상 경로 — 후보의 pred_ids를 그대로 복사한 응답이 통과한다.
func TestProbeSelectorHappyPath(t *testing.T) {
	s, _ := selectorWith(t, `{"reason":"식별력","pred_ids":["H1-P1","H2-P1"]}`)
	req, err := s.NextProbe(context.Background(), choiceInput())
	if err != nil {
		t.Fatalf("NextProbe: %v", err)
	}
	if len(req.PredIDs) != 2 || req.Reason == "" {
		t.Fatalf("요청 = %+v", req)
	}
}

// **strict 디코드**(§7.1): 모르는 필드와 필수 누락은 조용히 흐르지 않는다 —
// 종전 어댑터는 json_object + 일반 Unmarshal이라 누락이 zero value였다.
func TestProbeSelectorStrictDecode(t *testing.T) {
	cases := map[string]string{
		"모르는 필드":      `{"reason":"r","pred_ids":["H1-P1"],"tool":"scan_metrics"}`,
		"pred_ids 누락": `{"reason":"r"}`,
		"reason 누락":   `{"pred_ids":["H1-P1"]}`,
		"빈 목록":        `{"reason":"r","pred_ids":[]}`,
		"중복":          `{"reason":"r","pred_ids":["H1-P1","H1-P1"]}`,
	}
	for name, reply := range cases {
		s, _ := selectorWith(t, reply)
		if _, err := s.NextProbe(context.Background(), choiceInput()); err == nil {
			t.Errorf("%s: 통과함 — strict 디코드가 아니다", name)
		}
	}
}

// **도구별 인자 union이 서식에 없다**(§7.1의 핵심): 조사자가 인자를 작문할
// 수단 자체가 없어야 한다. 후보에 실리는 것은 명제의 좌표뿐이다.
func TestProbeSelectorPromptHasNoToolArgUnion(t *testing.T) {
	s, body := selectorWith(t, `{"reason":"r","pred_ids":["H1-P1","H2-P1"]}`)
	if _, err := s.NextProbe(context.Background(), choiceInput()); err != nil {
		t.Fatalf("NextProbe: %v", err)
	}
	// 실물 도구의 인자 이름들 — 하나라도 서식에 있으면 작문의 문이 열린다.
	for _, arg := range []string{`"targets"`, `"db"`, `"host"`, `"device"`, `"mode"`, `"query"`, `"from"`, `"to"`} {
		if strings.Contains(*body, arg) {
			t.Errorf("서식에 도구 인자 %s가 노출됨", arg)
		}
	}
	// 대신 후보의 pred_ids와 우선순위는 반드시 있어야 한다(선택의 재료).
	for _, want := range []string{`pred_ids`, `priority`, pipeline.PriorityDiscriminating} {
		if !strings.Contains(*body, want) {
			t.Errorf("서식에 %s가 없다 — 기계 열거가 안 실렸다", want)
		}
	}
}

// 후보가 없으면 부르지 않는다 — 고를 것이 없는 호출은 토큰만 태운다.
func TestProbeSelectorRefusesEmptyCandidates(t *testing.T) {
	s, _ := selectorWith(t, `{"reason":"r","pred_ids":["H1-P1"]}`)
	in := choiceInput()
	in.Candidates = nil
	if _, err := s.NextProbe(context.Background(), in); err == nil {
		t.Fatal("후보 0건인데 호출함")
	}
}
