package ledger

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// samplePayloads — 이벤트 타입 전수의 비어 있지 않은 표본. 전수 검사가
// 이 표를 분모로 쓰므로, 새 이벤트를 만들고 여기에 안 넣으면 시험이
// 실패한다(디코더 등록표 누락의 조기 경보).
func samplePayloads() map[EventType]Payload {
	rv := true
	return map[EventType]Payload{
		EvHypothesisCreated:            HypothesisCreated{Entry: hypo("H1", PriorHigh, 2)},
		EvHypothesisRejectedAtCreation: HypothesisRejectedAtCreation{ProposalSummary: "비트 플립", ViolatedRule: "no_refutation_condition"},
		EvProbePredicted:               ProbePredicted{ObservationKey: "k", PredIDs: []string{"H1-P1"}, Predictions: []HypoPrediction{{Hypothesis: "H1", ExpectedIfTrue: "느려짐"}}, Note: "분별력"},
		EvProbeExecuted:                ProbeExecuted{ObservationKey: "k", PredIDs: []string{"H1-P1"}, Outcome: ProbeDone, Note: "n", ProducedEIDs: []string{"EID-1", "EID-2"}},
		EvDiscriminationExhausted:      DiscriminationExhausted{Note: "갈릴 정보 없음"},
		EvEvidenceProposed: EvidenceProposed{Entry: EvidenceEntry{
			ID: "E1", Observation: "풀스캔", ObservedWindow: &Window{From: ts(1), To: ts(5)},
			Refs: []string{"EST-0001:scan_metrics"}, SourceStatus: SourceAnomalous,
			Links: []LinkClaim{{Hypothesis: "H1", Direction: DirSupports, ChainStep: step(0),
				DimensionClaim: &Dimension{Label: "sql_hash", Value: "abc123"}}},
		}},
		EvEvidenceLinkPassed:           EvidenceLinkPassed{Evidence: "E1", Hypothesis: "H1", Note: "ok"},
		EvEvidenceLinkRejected:         EvidenceLinkRejected{Evidence: "E1", Hypothesis: "H2", Note: "무관"},
		EvHypothesisRefuted:            HypothesisRefuted{Hypothesis: "H2", ByEvidence: "E1", Reason: "반증 조건 충족"},
		EvHypothesisDimensionSpecified: HypothesisDimensionSpecified{Hypothesis: "H1", Dimension: Dimension{Label: "sql_hash", Value: "abc123"}, ByEvidence: "E1"},
		EvObligationUpdated:            ObligationUpdated{Axis: AxisDepth, Status: ObligationSatisfied, Note: "차원 특정", Refs: []string{"E1"}},
		EvHypothesisAdopted:            HypothesisAdopted{Hypothesis: "H1", Reason: "row3"},
		EvHypothesisMarkedInferior:     HypothesisMarkedInferior{Hypothesis: "H3", Reason: "열세"},
		EvLoopTerminated:               LoopTerminated{Decision: Decision{Terminate: true, Reason: "완주", AdoptedID: "H1"}},
		EvReportAssembled:              ReportAssembled{AsOfSeq: 3},

		EvPredicateAudited: PredicateAudited{Hypothesis: "H1", PredID: "P1",
			Verdict: PredicateVerdict{Pertinent: true, Substantive: true, RoleValid: &rv}, Note: "적합"},
		EvChainClaimAsserted: ChainClaimAsserted{Hypothesis: "H1", Claim: ChainClaim{
			ClaimID: "C1", Kind: ChainTerminalEntity, EntityKey: "svc-order", Effect: "지연", Origin: OriginProjector}},
		EvTemporalJudged: TemporalJudged{Hypothesis: "H1", CauseEID: "EID-1", SymptomEID: "EID-2",
			Claim: TemporalPrecedes, Verdict: TemporalPrecedesVerdict, Note: "선행"},
		EvAdoptionAudited: AdoptionAudited{Hypothesis: "H1", ClaimID: "C1", Passed: true, Note: "지지 실질 확인"},
		EvRequiredViewsExtended: RequiredViewsExtended{Phase: PhaseLayerA,
			Keys: []RequiredViewKey{{TargetID: "t1", Tool: "scan_metrics"}}, Reason: "admission"},
		EvRequiredViewNotApplicable: RequiredViewNotApplicable{
			Key: RequiredViewKey{TargetID: "t1", Tool: "db_blocking"}, Reason: "not_collected"},

		EvPriorLowered: PriorLowered{Hypothesis: "H1", From: PriorHigh, To: PriorMedium,
			ProbeKey: "scan_metrics|t1|metric||cpu.util|full", IndexVersion: 12, Reason: "inconclusive"},
		EvLoopStepDecided: LoopStepDecided{Step: StepRegeneration, Verdict: LoopStepSkipped,
			Remaining: 40000, Required: 80800, Reason: "reserve 미달"},
		EvWideningExecuted: WideningExecuted{Step: WideningW1, ObservationKey: "scan_metrics|t1|metric||cpu.util|full",
			Tool: "scan_metrics", Outcome: ProbeDone, ProducedEIDs: []string{"EIX-0001"}, Reason: "confidence=low 재조회"},

		EvLLMUsage: LLMUsage{Model: "gemma4-26b-a4b-it-fp8", Purpose: "candidates_investigation",
			PromptTokens: 1200, CompletionTokens: 340, TotalTokens: 1540},

		EvProjectorSpotChecked: ProjectorSpotChecked{EID: "EIX-0001",
			EnvelopeRef: "EST-0001:scan_metrics", Consistent: false, Note: "effect_mismatch"},
	}
}

// 이벤트 타입 전수에 디코더가 있는가 — 하나라도 빠지면 그 이벤트가 실린
// run은 재기동 복원이 불가능하다(§15.5-④).
func TestDecoderCoversEveryEventType(t *testing.T) {
	// probe_added(주체가 출처에 따라 갈리던 예외)는 §14-4 4c에서 폐기됐다 —
	// 이제 알려진 타입 = actor 표 전량이다.
	known := map[EventType]bool{}
	for tp := range allowedActors {
		known[tp] = true
	}
	for tp := range known {
		if _, ok := payloadDecoders[tp]; !ok {
			t.Errorf("이벤트 타입 %s의 디코더 없음", tp)
		}
		if _, ok := samplePayloads()[tp]; !ok {
			t.Errorf("이벤트 타입 %s의 왕복 표본 없음", tp)
		}
	}
	for tp := range payloadDecoders {
		if !known[tp] {
			t.Errorf("디코더 등록표의 %s는 알려진 이벤트 타입이 아님", tp)
		}
	}
	if len(payloadDecoders) != 26 {
		t.Errorf("디코더 %d종 — 구 16 − probe_added(§14-4 4c) + 신 6(adoption_audited는 5c) + Loop 3(widening_executed는 4d) + llm_usage(§14-6 6a) + projector_spot_checked(§14-7 ③) = 26이어야 함",
			len(payloadDecoders))
	}
}

// 21종 전부가 바이트를 왕복해도 같은 concrete 값으로 돌아오는가.
func TestPayloadRoundTrip(t *testing.T) {
	for tp, p := range samplePayloads() {
		ev := Event{Seq: 7, Time: ts(3), Actor: allowedActors[tp], Type: tp, Payload: p}
		b, err := EncodeEvent(ev)
		if err != nil {
			t.Fatalf("%s 인코드: %v", tp, err)
		}
		got, err := DecodeEvent(b)
		if err != nil {
			t.Fatalf("%s 디코드: %v", tp, err)
		}
		if !reflect.DeepEqual(got, ev) {
			t.Errorf("%s 왕복 불일치:\n 원본 %#v\n 복원 %#v", tp, ev, got)
		}
	}
}

// 모르는 타입·스키마 어긋남은 조용히 넘어가지 않는다(fail-closed).
func TestDecodeRejectsUnknownTypeAndDrift(t *testing.T) {
	if _, err := DecodeEvent([]byte(`{"Seq":1,"Type":"made_up","Payload":{}}`)); err == nil {
		t.Fatal("미등록 타입이 통과했다")
	}
	line := `{"Seq":1,"Type":"report_assembled","Payload":{"AsOfSeq":1,"Surprise":true}}`
	if _, err := DecodeEvent([]byte(line)); err == nil {
		t.Fatal("모르는 필드(스키마 드리프트)가 통과했다")
	}
}

// 마지막 줄만 잘림을 허용한다 — 중간 줄 손상은 오류다.
func TestDecodeEventsTruncatedTail(t *testing.T) {
	ev := Event{Seq: 1, Time: ts(0), Actor: ActorPipeline, Type: EvReportAssembled, Payload: ReportAssembled{AsOfSeq: 1}}
	line, _ := EncodeEvent(ev)
	full := string(line) + "\n"

	evs, trunc, err := DecodeEvents(strings.NewReader(full + string(line[:len(line)/2])))
	if err != nil || !trunc || len(evs) != 1 {
		t.Fatalf("부분 쓰기 꼬리: evs=%d trunc=%v err=%v", len(evs), trunc, err)
	}
	if _, _, err := DecodeEvents(strings.NewReader("{깨진 줄}\n" + full)); err == nil {
		t.Fatal("중간 줄 손상이 통과했다 — 잘림과 손상은 다르다")
	}
}

// 구 산출물(runs/*/ledger.jsonl)이 같은 디코더로 읽히는가 — journal과
// 구 dump의 줄 형식이 같다는 실측 근거.
func TestDecodeLegacyLedgerDump(t *testing.T) {
	paths, _ := filepath.Glob(filepath.Join("..", "runs", "*", "ledger.jsonl"))
	if len(paths) == 0 {
		t.Skip("runs/*/ledger.jsonl 없음")
	}
	total := 0
	for _, p := range paths {
		evs, trunc, err := ReadJournal(p)
		if err != nil {
			// **명시적 스키마 파기만 면제한다**: 폐기된 필드는 그 전에 만들어진
			// dump에 실려 있고 strict 디코더가 모르는 필드로 거부한다. 다른
			// 드리프트는 그대로 실패다 — 면제를 이름 목록으로 좁혀 두는 것이 그
			// 강도의 담보다. 장부 dump는 run 단위 산출물이지 장기 저장소가
			// 아니므로 재생 호환이 깨지는 것 자체는 허용된 파기다(§14-4 4c).
			legacy := ""
			for _, name := range []string{
				`unknown field "RefutationCondition"`, // §14-3
				`unknown field "Probes"`,              // §14-4 4c (ProbeSpec 폐기)
				`unknown field "Hypothesis"`,          // §14-4 4c (probe 문장의 가설 결박 폐기)
				`페이로드 타입 probe_added`,               // §14-4 4c (문장 자체 폐기)
			} {
				if strings.Contains(err.Error(), name) {
					legacy = name
					break
				}
			}
			if legacy != "" {
				t.Logf("%s: 폐기 스키마 dump(%s) — 건너뜀", p, legacy)
				continue
			}
			t.Fatalf("%s: %v", p, err)
		}
		if trunc {
			t.Errorf("%s: 구 dump에 부분 쓰기 꼬리", p)
		}
		if _, err := Replay(evs); err != nil {
			t.Fatalf("%s 재생: %v", p, err)
		}
		total += len(evs)
	}
	if fi, err := os.Stat(filepath.Join("..", "runs")); err == nil && fi.IsDir() {
		t.Logf("구 dump %d개 파일 %d 이벤트 복원·재생 통과", len(paths), total)
	}
}
