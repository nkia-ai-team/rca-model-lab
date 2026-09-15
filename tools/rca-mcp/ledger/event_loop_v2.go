// 신 Loop(§14-4 4b)가 쓰는 이벤트 2종 — docs/spec-agent-structure.md
// §7.2-3(Prior 1단 하향)·§7.4~7.6(이음새 판정 기록)이 정본이다.
//
//	prior_lowered      §7.2-3 inconclusive의 유일한 상태 전이
//	loop_step_decided  §7.4·§7.5 갈래의 승인/생략 판정
//	widening_executed  §7.4 사다리 한 칸의 도구 호출 (§14-4 4d)
//
// **왜 새 타입이 필요한가**: 둘 다 "기록되지 않으면 없었던 일과 구분되지
// 않는" 판정이다. Prior 하향은 (가설, probe, index 버전)당 1회라는 계약이
// 재생으로 검증돼야 하고(§7.2-3), 이음새 생략은 §14-4 4b의 완료 기준이
// 명시적으로 "조용한 우회 금지"를 요구한다 — 4d가 본체를 붙일 때까지
// widening·정밀화·재생성이 왜 안 돌았는지가 장부에 남아야 한다.
package ledger

import (
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

const (
	EvPriorLowered     EventType = "prior_lowered"
	EvLoopStepDecided  EventType = "loop_step_decided"
	EvWideningExecuted EventType = "widening_executed"
)

var allowedActorsLoopV2 = map[EventType]Actor{
	// 둘 다 규칙 계산이다 — 하향은 진리표 판정의 귀결이고, 이음새 판정은
	// 예산 산술이다. LLM이 쓰는 문장이 아니다(§7.2-3 "LLM은 판정에 개입하지
	// 않는다").
	EvPriorLowered:    ActorRule,
	EvLoopStepDecided: ActorRule,
	// widening 대상은 **규칙이 고른다**(§7.4 사다리는 index 상태의 함수다) —
	// probe_executed의 actor가 investigator인 것과 갈리는 지점이 여기다.
	EvWideningExecuted: ActorRule,
}

func init() {
	for t, a := range allowedActorsLoopV2 {
		allowedActors[t] = a
	}
}

// ── prior_lowered (§7.2-3) ──────────────────────────────────────

// PriorLowered — inconclusive 판정에 따른 Prior 1단 하향.
//
// **같은 관측의 반복으로 여러 번 깎지 않는다**(§7.2-3): 중복 차단 키는
// (가설, probe, index 버전)이고 그 키를 payload에 싣는 이유는 "1회"라는
// 계약을 장부만 보고 검사할 수 있어야 하기 때문이다.
type PriorLowered struct {
	Hypothesis string
	From, To   Prior
	// ProbeKey — 하향을 낳은 probe의 canonical 관측 키(§6.0) 표기.
	ProbeKey string
	// IndexVersion — 그 시점 index 레코드 수(단조 증가 — append-only).
	IndexVersion int
	Reason       string
}

// LowerPrior는 1단 하향의 계산이다 — low가 바닥이다(음수 등급은 없다).
func LowerPrior(p Prior) Prior {
	if p <= PriorLow {
		return PriorLow
	}
	return p - 1
}

// ── loop_step_decided (§7.4·§7.5) ───────────────────────────────

// LoopStep은 루프의 조건부 갈래다(닫힌 어휘).
type LoopStep string

const (
	// StepWidening — §7.4 progressive widening(W1~W4).
	StepWidening LoopStep = "widening"
	// StepRefinement — §7.3 정밀화 걸음.
	StepRefinement LoopStep = "refinement"
	// StepRegeneration — §7.5 재생성.
	StepRegeneration LoopStep = "regeneration"
)

func ValidLoopStep(s LoopStep) bool {
	return s == StepWidening || s == StepRefinement || s == StepRegeneration
}

// LoopStepVerdict는 그 갈래의 처분이다.
type LoopStepVerdict string

const (
	// LoopStepApproved — 발동 조건·예산 검사를 통과했다.
	LoopStepApproved LoopStepVerdict = "approved"
	// LoopStepSkipped — 생략했다. 사유는 예산 미달일 수도, 구현 미도래
	// (§14-4 4d)일 수도 있다 — 둘을 Reason이 구분한다.
	LoopStepSkipped LoopStepVerdict = "skipped"
)

func ValidLoopStepVerdict(v LoopStepVerdict) bool {
	return v == LoopStepApproved || v == LoopStepSkipped
}

// LoopStepDecided — 조건부 갈래 하나의 판정. 승인된 갈래가 실행되지 않으면
// **생략 문장이 한 줄 더 붙는다**(승인과 실행은 다른 사실이다).
type LoopStepDecided struct {
	Step    LoopStep
	Verdict LoopStepVerdict
	// Remaining·Required — §7.6 잔여 검사의 피연산자(토큰). 판정을 사후에
	// 재계산할 수 있어야 예산 규율이 감사 가능하다.
	Remaining int
	Required  int
	Reason    string
}

// ── widening_executed (§7.4 — §14-4 4d) ─────────────────────────

// WideningStep은 §7.4 사다리의 칸이다(닫힌 어휘 — 순서가 곧 값의 순서다).
type WideningStep string

const (
	// W1 — Confidence=low·OmittedN>0 레코드의 정밀 재조회.
	WideningW1 WideningStep = "W1"
	// W2 — 증상 onset ±5분 창 재조회.
	WideningW2 WideningStep = "W2"
	// W3 — expand_topology 2-hop 확장 + 신규 대상의 유형별 필수 관점.
	WideningW3 WideningStep = "W3"
	// W4 — 차원 재집계(breakdown 타 구획·compare_peers 전 피어·db 타 접기 축).
	WideningW4 WideningStep = "W4"
)

func ValidWideningStep(s WideningStep) bool {
	return s == WideningW1 || s == WideningW2 || s == WideningW3 || s == WideningW4
}

// WideningExecuted — widening 도구 호출 하나.
//
// **probe_executed와 다른 문장인 이유**: probe 문장의 계약은 "어느 명제를
// 해소했는가"(pred_id 지목 필수 — ledger.validateProbeTarget)인데, W3가
// 편입한 신규 대상에는 그 대상을 지목하는 술어가 아직 없다. 그 자리를
// pred_id 없이 통과시키려고 probe 문장의 계약을 느슨하게 하면, 조사자
// 경로의 지목 강제까지 같이 풀린다 — 계약이 다른 두 행위는 문장도 다르다.
// **술어 불가 도구 호출도 이 문장으로 남는다**(§14-4 4d 정정): W3의
// expand_topology·get_data_coverage는 index 레코드를 만들지 않아 관측 키가
// 없지만, 예산은 똑같이 나간다. 기록하지 않으면 사다리가 쓴 예산이 장부에서
// 사라져 §7.6 예산 감사가 성립하지 않는다(라이브 실측 6b8e87c9: W3 14콜이
// 전부 무기록이었다). 관측 키 없는 호출은 EnvelopeRef가 그 자리를 대신한다 —
// 봉투는 어느 경로든 store에 남으므로 재생 감사의 지목이 사라지지 않는다.
type WideningExecuted struct {
	Step WideningStep
	// ObservationKey — canonical 관측 키(§6.0) 표기. 술어 불가 도구
	// 호출에서만 비며, 그때는 EnvelopeRef가 필수다.
	ObservationKey string
	Tool           string
	// EnvelopeRef — evidence store 키. 관측 키 없는 호출의 재생 지목이다.
	EnvelopeRef string
	Outcome     ProbeOutcome
	// ProducedEIDs — 이 호출이 index에 적재한 레코드 전량. **material
	// delta(§7.4)의 재생 근거**다 — 무엇이 실렸는지 없이는 "이 칸이
	// delta를 만들었나"를 사후에 다시 셀 수 없다.
	ProducedEIDs []string
	// Reason — 이 칸이 이 키를 고른 사유(사다리 규칙의 발행).
	Reason string
	// Note — infeasible 사유 등 부가 사실.
	Note string
}

func (PriorLowered) eventType() EventType     { return EvPriorLowered }
func (LoopStepDecided) eventType() EventType  { return EvLoopStepDecided }
func (WideningExecuted) eventType() EventType { return EvWideningExecuted }

// validateLoopV2는 두 신 문장의 형식 계약이다(ledger.validate에서 호출).
func (l *Ledger) validateLoopV2(p Payload) error {
	switch v := p.(type) {
	case PriorLowered:
		h, err := l.activeHypo(v.Hypothesis)
		if err != nil {
			return err
		}
		if v.To != LowerPrior(v.From) {
			return fmt.Errorf("prior 하향은 1단이어야 함(%v→%v)", v.From, v.To)
		}
		if v.From != h.Entry.Prior {
			return fmt.Errorf("가설 %s의 현재 prior는 %v인데 %v에서 내리려 함 — 재생 불일치",
				v.Hypothesis, h.Entry.Prior, v.From)
		}
		if v.ProbeKey == "" {
			return fmt.Errorf("prior 하향에는 probe 키 필수 — (가설, probe, index 버전)당 1회의 재생 근거")
		}
		if v.Reason == "" {
			return fmt.Errorf("prior 하향에는 사유 필수")
		}
	case LoopStepDecided:
		if !ValidLoopStep(v.Step) {
			return fmt.Errorf("루프 갈래 %q 미정의", v.Step)
		}
		if !ValidLoopStepVerdict(v.Verdict) {
			return fmt.Errorf("갈래 처분 %q 미정의", v.Verdict)
		}
		if v.Reason == "" {
			return fmt.Errorf("갈래 판정에는 사유 필수 — 조용한 우회 금지(§14-4 4b)")
		}
	case WideningExecuted:
		if !ValidWideningStep(v.Step) {
			return fmt.Errorf("widening 칸 %q 미정의", v.Step)
		}
		if v.Tool == "" {
			return fmt.Errorf("widening 호출에는 도구 이름 필수")
		}
		// 관측 키 계약은 **약해지지 않는다**: 키를 비울 수 있는 것은 애초에
		// index 레코드를 만들 수 없는 술어 불가 도구뿐이고(§5.1), 그때도
		// 봉투 지목은 반드시 있어야 한다. 사상표에 행이 있는 원천의 호출이
		// 키 없이 실리는 문은 그대로 닫혀 있다.
		if v.ObservationKey == "" {
			if !evidence.IsPredicateFreeTool(v.Tool) {
				return fmt.Errorf("widening 호출 %s에 관측 키 없음 — 술어 불가 도구가 아니면 필수다", v.Tool)
			}
			if v.EnvelopeRef == "" && v.Outcome == ProbeDone {
				return fmt.Errorf("관측 키 없는 widening 호출(%s)에는 봉투 지목 필수 — 재생 감사의 유일한 지목이다", v.Tool)
			}
		}
		if v.Outcome != ProbeDone && v.Outcome != ProbeInfeasible {
			return fmt.Errorf("widening 결과 %q 미정의", v.Outcome)
		}
		if v.Reason == "" {
			return fmt.Errorf("widening 호출에는 선택 사유 필수 — 사다리 규칙의 발행")
		}
	}
	return nil
}

// applyLoopV2는 상태 전이다 — Prior 하향만 상태를 바꾼다(갈래 판정은
// history-only).
func (l *Ledger) applyLoopV2(p Payload) {
	if v, ok := p.(PriorLowered); ok {
		if h := l.hypos[v.Hypothesis]; h != nil {
			h.Entry.Prior = v.To
		}
	}
}
