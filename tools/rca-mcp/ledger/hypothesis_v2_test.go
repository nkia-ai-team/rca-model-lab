package ledger

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

func fp(v float64) *float64 { return &v }

func okPred() SignalPred {
	return SignalPred{
		PredID: "H1-P1", Tool: evidence.SrcDBBlocking, TargetID: "orders-db",
		Aspect: evidence.AspectDB, Metric: "blocked_sessions",
		EntityKey: "evt:orders-db@04:28:10", Predicate: PredPresent,
		Window: evidence.WindowFull, Expectation: ExpectMustHold, Role: RoleNecessary,
	}
}

func TestSignalPredForm(t *testing.T) {
	if err := okPred().ValidateForm(); err != nil {
		t.Fatalf("정상 술어 거부: %v", err)
	}
	bad := map[string]func(p *SignalPred){
		"미정의 predicate":     func(p *SignalPred) { p.Predicate = "magnitude_gt" },
		"threshold 누락":      func(p *SignalPred) { p.Predicate = PredMagnitudeGE },
		"threshold 과잉":      func(p *SignalPred) { p.Threshold = fp(3) },
		"expectation bool화": func(p *SignalPred) { p.Expectation = "true" },
		"expectation 누락":    func(p *SignalPred) { p.Expectation = "" },
		"role 미정의":          func(p *SignalPred) { p.Role = "optional" },
		"창 미정의":             func(p *SignalPred) { p.Window = "week" },
		"술어 불가 도구":          func(p *SignalPred) { p.Tool = "expand_topology" },
		"aspect 미정의":        func(p *SignalPred) { p.Aspect = "trace" },
		"대상 없음":             func(p *SignalPred) { p.TargetID = "" },
		"pred_id 없음":        func(p *SignalPred) { p.PredID = "" },
	}
	for name, mut := range bad {
		p := okPred()
		mut(&p)
		if err := p.ValidateForm(); err == nil {
			t.Errorf("%s가 통과됨", name)
		}
	}
}

// §6.0의 핵심 계약: 관측 키에 Predicate·Threshold가 없다. 연산자만 바꾼
// 변형은 새 명제가 아니므로 반증 가설이 무한 재등록될 수 없다.
func TestObservationKeyIgnoresPredicateAndThreshold(t *testing.T) {
	base := okPred()
	variants := []SignalPred{
		func() SignalPred { p := base; p.Predicate = PredAbsent; p.Expectation = ExpectMustNotHold; return p }(),
		func() SignalPred { p := base; p.Predicate = PredDirectionUp; return p }(),
		func() SignalPred { p := base; p.Predicate = PredMagnitudeGE; p.Threshold = fp(3.0); return p }(),
		func() SignalPred { p := base; p.Predicate = PredMagnitudeGE; p.Threshold = fp(2.9); return p }(),
		func() SignalPred { p := base; p.Role = RoleCorroborating; p.PredID = "H2-P7"; return p }(),
		func() SignalPred { p := base; p.TargetID = " ORDERS-DB "; return p }(),
	}
	want := base.ObservationKey()
	for i, v := range variants {
		if got := v.ObservationKey(); got != want {
			t.Errorf("변형 %d가 새 명제가 됨: %v ≠ %v", i, got, want)
		}
	}
	// 창은 키의 성분이다 — full과 onset_narrow는 갈린다(§5.7 공존 head).
	w := base
	w.Window = evidence.WindowOnsetNarrow
	if w.ObservationKey() == want {
		t.Fatal("창 축이 키에서 사라짐")
	}
}

// B-5: PredID 유일성. 기계 부여가 정본이고, 검사기는 재생·LLM 경로용 관문.
func TestPredIDAssignmentAndUniqueness(t *testing.T) {
	preds := []SignalPred{okPred(), okPred(), okPred()}
	assigned := AssignPredIDs("H7", preds)
	want := []string{"H7-P1", "H7-P2", "H7-P3"}
	for i, p := range assigned {
		if p.PredID != want[i] {
			t.Fatalf("기계 부여 ID[%d] = %s, want %s", i, p.PredID, want[i])
		}
	}
	if err := ValidatePredIDs(assigned); err != nil {
		t.Fatalf("기계 부여분이 유일성 검사에 걸림: %v", err)
	}
	// 입력은 변형되지 않는다(원본 술어의 ID 유지).
	if preds[0].PredID != "H1-P1" {
		t.Fatal("AssignPredIDs가 입력 슬라이스를 덮어씀")
	}
	dup := []SignalPred{okPred(), okPred()} // 둘 다 H1-P1
	if err := ValidatePredIDs(dup); err == nil {
		t.Fatal("중복 PredID가 통과 — 미심사 술어가 유효 셈에 들어감")
	}
	blank := []SignalPred{{}}
	if err := ValidatePredIDs(blank); err == nil {
		t.Fatal("빈 PredID가 통과")
	}
}

func TestSentinelCause(t *testing.T) {
	c := UnknownCause("svc-1")
	if c != "unknown:svc-1" || !IsSentinelCause(c) {
		t.Fatalf("sentinel = %q", c)
	}
	if IsSentinelCause("orders-db") {
		t.Fatal("실 대상이 sentinel로 판정 — 다양성 계수가 잘못 빠진다")
	}
}

func TestChainClaimValidate(t *testing.T) {
	c := ChainClaim{ClaimID: "C1", Kind: ChainTerminalEntity, EntityKey: "sql_key:abc", Effect: "대기 7", Origin: OriginProjector}
	if err := c.Validate(); err != nil {
		t.Fatalf("정상 사슬 구간 거부: %v", err)
	}
	noEntity := c
	noEntity.EntityKey = ""
	if noEntity.Validate() == nil {
		t.Fatal("entity_key 없는 terminal_entity가 통과 — §8 말단 게이트 판정 불가")
	}
	// 말단이 아니면 EntityKey 없이도 성립한다.
	edge := c
	edge.Kind, edge.EntityKey = ChainPropagationEdge, ""
	if err := edge.Validate(); err != nil {
		t.Fatalf("propagation_edge 거부: %v", err)
	}
	noOrigin := c
	noOrigin.Origin = ""
	if noOrigin.Validate() == nil {
		t.Fatal("origin 없는 구간이 통과 — §7.7 검수 면제 판정 불가")
	}
}

// §7.1 strict 디코드 — 모르는 필드 거부, 필수 누락 반려.
func TestDecodeProbeRequestStrict(t *testing.T) {
	ok := `{"pred_ids":["H1-P1","H1-P2"],"reason":"onset 구간 확인"}`
	r, err := DecodeProbeRequest([]byte(ok))
	if err != nil {
		t.Fatalf("정상 요청 거부: %v", err)
	}
	if len(r.PredIDs) != 2 || r.Reason == "" {
		t.Fatalf("디코드 결과 = %+v", r)
	}

	bad := map[string]string{
		// 4차 A-4가 뺀 필드들이 되살아나면 여기서 죽는다.
		"window_class 부활": `{"pred_ids":["H1-P1"],"reason":"x","window_class":"full"}`,
		"resolution_hint": `{"pred_ids":["H1-P1"],"reason":"x","resolution_hint":"1s"}`,
		"도구 직접 지정":        `{"pred_ids":["H1-P1"],"reason":"x","tool":"read_timeseries"}`,
		"오타 필드":           `{"pred_ids":["H1-P1"],"reasons":"x"}`,
		"pred_ids 누락":     `{"reason":"x"}`,
		"빈 pred_ids":      `{"pred_ids":[],"reason":"x"}`,
		"중복 pred_id":      `{"pred_ids":["H1-P1","H1-P1"],"reason":"x"}`,
		"reason 누락":       `{"pred_ids":["H1-P1"]}`,
		"잉여 토큰":           `{"pred_ids":["H1-P1"],"reason":"x"} {"pred_ids":["H2-P1"]}`,
	}
	for name, js := range bad {
		if _, err := DecodeProbeRequest([]byte(js)); err == nil {
			t.Errorf("%s가 통과됨", name)
		}
	}
}

// §7.1: 한 요청의 전 항목은 동일 canonical 관측 키여야 한다 — 섞이면
// 단일 도구 호출로 인자를 결정할 수 없고 probe 상한이 우회된다.
func TestSameObservationKey(t *testing.T) {
	a := okPred()
	b := okPred()
	b.PredID = "H1-P2"
	b.Predicate = PredMagnitudeGE
	b.Threshold = fp(2)
	if err := SameObservationKey([]evidence.ObservationKey{a.ObservationKey(), b.ObservationKey()}); err != nil {
		t.Fatalf("연산자만 다른 두 술어가 반려됨: %v", err)
	}
	c := okPred()
	c.TargetID = "carts-db"
	err := SameObservationKey([]evidence.ObservationKey{a.ObservationKey(), c.ObservationKey()})
	if err == nil || !strings.Contains(err.Error(), "섞임") {
		t.Fatalf("대상이 다른 술어 묶음이 통과: %v", err)
	}
	if SameObservationKey(nil) == nil {
		t.Fatal("빈 묶음이 통과")
	}
}

// B-10: ProbeRequest는 comparable이 아니므로 map 키로 쓸 수 없다.
// 중복 판정은 요청이 아니라 명제 키로 한다 — 그 키는 comparable이다.
func TestDedupIsByObservationKeyNotRequest(t *testing.T) {
	seen := map[evidence.ObservationKey]bool{}
	a, b := okPred(), okPred()
	b.PredID, b.Predicate = "H2-P1", PredAbsent
	seen[a.ObservationKey()] = true
	if !seen[b.ObservationKey()] {
		t.Fatal("같은 관측 키가 다른 항목으로 셈 — 반복 조회 차단이 뚫린다")
	}
}

// 기계 딱지는 직렬화 왕복에서 살아남는다(3b 결정, 3a 인계 ①).
//
// 왜 시험이 필요한가: 종전 `json:"-"`는 journal 재생(§15.5)에서 딱지를
// 통째로 잃어 **재생이 판정을 바꿨다** — audited_out 술어가 유효로
// 되살아나고 pre_satisfied가 §8 신규 관측 요건에 산입됐다. 주입 방어는
// 서식 부재로 진다(llm/audit_test.go·candidates의 predJSON — 그 서식에
// 이 세 필드가 없다).
func TestSignalPredMachineTagsSurviveRoundTrip(t *testing.T) {
	p := SignalPred{
		PredID: "H1-P1", Tool: evidence.SrcScanMetrics, TargetID: "db-1",
		Aspect: evidence.AspectMetric, Metric: "cpu.util",
		Predicate: evidence.PredDirectionUp, Window: evidence.WindowFull,
		Expectation: evidence.ExpectMustHold, Role: RoleNecessary,
		PreSatisfied: true, Undecidable: true, AuditedOut: true,
	}
	b, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	var got SignalPred
	if err := json.Unmarshal(b, &got); err != nil {
		t.Fatal(err)
	}
	if !got.PreSatisfied || !got.Undecidable || !got.AuditedOut {
		t.Fatalf("딱지 유실: %s", b)
	}
	// 거짓 딱지는 서식에 나타나지도 않는다(omitempty).
	p.PreSatisfied, p.Undecidable, p.AuditedOut = false, false, false
	b2, _ := json.Marshal(p)
	for _, tag := range []string{"pre_satisfied", "undecidable", "audited_out"} {
		if strings.Contains(string(b2), tag) {
			t.Errorf("거짓 딱지 %q가 서식에 실렸다: %s", tag, b2)
		}
	}
}

// 개설 이벤트 payload(가설 엔트리)로도 딱지가 살아 돌아온다 — journal
// 재생 경로의 실물 대조.
func TestHypothesisCreatedKeepsMachineTagsThroughDecode(t *testing.T) {
	e := HypothesisEntry{ID: "H1", TargetID: "db-1",
		Chain:   []ChainStep{{Entity: "db-1", Effect: "포화"}},
		Sources: []HypoSource{SourceInvestigation},
		PredictedSignals: []SignalPred{{PredID: "H1-P1", Tool: evidence.SrcScanMetrics,
			TargetID: "db-1", Aspect: evidence.AspectMetric, Metric: "cpu.util",
			Predicate: evidence.PredDirectionUp, Window: evidence.WindowFull,
			Expectation: evidence.ExpectMustHold, Role: RoleNecessary, AuditedOut: true}},
	}
	line, err := EncodeEvent(Event{Seq: 1, Time: time.Unix(0, 0).UTC(), Actor: ActorRule,
		Type: EvHypothesisCreated, Payload: HypothesisCreated{Entry: e}})
	if err != nil {
		t.Fatal(err)
	}
	ev, err := DecodeEvent(line)
	if err != nil {
		t.Fatal(err)
	}
	got := ev.Payload.(HypothesisCreated)
	if !got.Entry.PredictedSignals[0].AuditedOut {
		t.Errorf("디코드 후 audited_out 유실: %+v", got.Entry.PredictedSignals[0])
	}
}
