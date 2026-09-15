package llm

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// captureServer는 요청 본문을 잡아두는 mock이다 — 서식 단언(blind·관측값
// 첨부)은 **실제로 나간 본문**을 봐야 성립한다.
func captureServer(t *testing.T, content string, got *chatRequest) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := json.NewDecoder(r.Body).Decode(got); err != nil {
			t.Errorf("요청 디코드: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"choices":[{"message":{"content":` + jsonString(content) + `}}]}`))
	}))
}

func auditSubject() pipeline.AuditSubject {
	mag := 3.5
	obs := evidence.ObservationValue{Present: true, Metric: "cpu.util", Magnitude: &mag,
		Direction: evidence.DirUp, Availability: evidence.AvailObserved,
		Status: evidence.StatusAnomalous}
	base := ledger.SignalPred{
		Tool: evidence.SrcScanMetrics, TargetID: "db-1", Aspect: evidence.AspectMetric,
		Metric: "cpu.util", Predicate: evidence.PredDirectionUp, Window: evidence.WindowFull,
		Expectation: evidence.ExpectMustHold, Role: ledger.RoleNecessary,
	}
	p1, p2 := base, base
	p1.PredID, p2.PredID = "H1-P1", "H1-P2"
	p2.Metric, p2.Role = "mem.util", ledger.RoleCorroborating
	// 기계 딱지가 박힌 술어 — LLM 서식에 새면 안 된다(주입 방어).
	p1.PreSatisfied, p1.AuditedOut = true, true
	return pipeline.AuditSubject{
		Hypothesis: "H1", Mechanism: "커넥션 포화",
		Chain: []ledger.ChainStep{{Entity: "db-1", Effect: "포화"}, {Entity: "app-1", Effect: "지연"}},
		Predicates: []pipeline.PredicateView{
			{Pred: p1, Observed: &obs},
			{Pred: p2}, // 미실측 — observed는 null
		},
	}
}

// R 입력은 blind다: 서사·순위는 없고, 명제 장부의 기왕 관측값은 있다.
func TestAuditRequestIsBlindAndCarriesObservation(t *testing.T) {
	var req chatRequest
	srv := captureServer(t, `{"items":[{"pred_id":"H1-P1","note":"n","pertinent":true,"substantive":true}]}`, &req)
	defer srv.Close()
	a := &Auditor{Client: &Client{BaseURL: srv.URL, Model: "m"}}
	if _, err := a.AuditPredicates(context.Background(), []pipeline.AuditSubject{auditSubject()}); err != nil {
		t.Fatalf("AuditPredicates: %v", err)
	}
	user := req.Messages[1].Content
	// ① 서사·순위 부재 — 심사 단위에 그 자리가 아예 없다.
	for _, leak := range []string{"prior", "사전확률", "symptom", "incident", "rationale", "source"} {
		if strings.Contains(strings.ToLower(user), leak) {
			t.Errorf("blind 위반: 입력에 %q가 있다\n%s", leak, user)
		}
	}
	// ② 메커니즘·사슬·술어는 간다.
	for _, want := range []string{"커넥션 포화", "\"chain\"", "H1-P1", "\"role\": \"necessary\""} {
		if !strings.Contains(user, want) {
			t.Errorf("입력에 %q가 없다\n%s", want, user)
		}
	}
	// ③ 기왕 관측값 첨부 — 미실측은 null.
	if !strings.Contains(user, `"magnitude": 3.5`) || !strings.Contains(user, `"observed": null`) {
		t.Errorf("관측값 첨부가 계약대로가 아니다\n%s", user)
	}
	// ④ 기계 딱지는 서식에 없다(주입 방어의 정본 — 3b 결정).
	for _, tag := range []string{"pre_satisfied", "audited_out", "undecidable"} {
		if strings.Contains(user, tag) {
			t.Errorf("기계 딱지 %q가 LLM 입력에 실렸다\n%s", tag, user)
		}
	}
	// ⑤ 배치 오염 가드 지시문 + note 먼저 규율이 시스템 프롬프트에 있다.
	sys := req.Messages[0].Content
	if !strings.Contains(sys, "항목별로 독립 판정") || !strings.Contains(sys, "비교하거나 순위") {
		t.Error("배치 독립 판정 지시가 없다")
	}
	if !strings.Contains(sys, "note를 먼저") {
		t.Error("note 먼저 규율이 없다")
	}
	// 출력 서식도 note가 판정보다 앞이다.
	if strings.Index(sys, `"note"`) > strings.Index(sys, `"pertinent"`) {
		t.Error("출력 서식에서 note가 판정 뒤에 있다")
	}
}

// 배치 1콜: 여러 가설의 술어가 한 요청에 실린다.
func TestAuditBatchesAllHypothesesInOneCall(t *testing.T) {
	var req chatRequest
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		json.NewDecoder(r.Body).Decode(&req)
		w.Write([]byte(`{"choices":[{"message":{"content":"{\"items\":[]}"}}]}`))
	}))
	defer srv.Close()
	s1 := auditSubject()
	s2 := auditSubject()
	s2.Hypothesis = "H2"
	s2.Predicates[0].Pred.PredID = "H2-P1"
	a := &Auditor{Client: &Client{BaseURL: srv.URL, Model: "m"}}
	if _, err := a.AuditPredicates(context.Background(), []pipeline.AuditSubject{s1, s2}); err != nil {
		t.Fatal(err)
	}
	if calls != 1 {
		t.Fatalf("호출 = %d, want 1(배치 1콜)", calls)
	}
	if !strings.Contains(req.Messages[1].Content, "H2-P1") {
		t.Error("두 번째 가설이 배치에 없다")
	}
}

// 항목 디코드 — role_valid는 **부재와 false를 구분한다**(C-2).
func TestAuditDecodesRoleValidTristate(t *testing.T) {
	srv := fakeServer(t, `{"items":[
      {"pred_id":"H1-P1","note":"자명","pertinent":true,"substantive":true,"role_valid":false},
      {"pred_id":"H1-P2","note":"관련","pertinent":true,"substantive":false}]}`)
	defer srv.Close()
	a := &Auditor{Client: &Client{BaseURL: srv.URL, Model: "m"}}
	items, err := a.AuditPredicates(context.Background(), []pipeline.AuditSubject{auditSubject()})
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 2 {
		t.Fatalf("items = %d", len(items))
	}
	if items[0].RoleValid == nil || *items[0].RoleValid {
		t.Errorf("role_valid=false가 안 실렸다: %+v", items[0])
	}
	if items[1].RoleValid != nil {
		t.Errorf("role_valid 부재가 false로 떨어졌다: %+v", items[1])
	}
	if items[1].Substantive || items[1].Note != "관련" {
		t.Errorf("판정 디코드 이상: %+v", items[1])
	}
}

// 재요구는 위반 사유를 싣고 술어 목록을 typed로 돌려준다.
func TestRevisePredicates(t *testing.T) {
	var req chatRequest
	srv := captureServer(t, `{"note":"자명 술어를 뺐다","predicted_signals":[
      {"eid_hint":"","tool":"scan_metrics","target_id":"db-1","aspect":"metric",
       "metric":"mem.util","entity_key":"","predicate":"direction_up",
       "window":"onset_narrow","expectation":"must_hold","role":"necessary"}]}`, &req)
	defer srv.Close()
	a := &Auditor{Client: &Client{BaseURL: srv.URL, Model: "m"}}
	preds, err := a.RevisePredicates(context.Background(), auditSubject(),
		[]pipeline.AdmissionReject{{Rule: "규칙 5", PredID: "H1-P1", Reason: "반증형 0개"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(preds) != 1 || preds[0].Role != ledger.RoleNecessary ||
		preds[0].Window != evidence.WindowOnsetNarrow {
		t.Fatalf("재생성 술어 = %+v", preds)
	}
	if !strings.Contains(req.Messages[1].Content, "반증형 0개") {
		t.Error("위반 피드백이 안 갔다")
	}
	// 재요구 서식에도 기계 딱지는 없다.
	if strings.Contains(req.Messages[1].Content, "audited_out") {
		t.Error("재요구 입력에 기계 딱지가 실렸다")
	}
}
