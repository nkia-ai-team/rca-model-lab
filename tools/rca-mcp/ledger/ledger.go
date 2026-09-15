// 수첩(가설 장부) 본체 — append-only history가 정본이고, 현재 상태는
// history를 읽은 결과다 (장부 스펙 §1 원칙 3, event sourcing).
//
// 상태를 바꾸는 경로는 Append 하나뿐이다. 실시간 기록도 Append, 재생
// (Replay)도 내부적으로 같은 적용 함수를 지나므로 "실시간 상태"와
// "재생한 상태"는 구조적으로 어긋날 수 없다.
package ledger

import (
	"fmt"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// Lifecycle은 가설의 생애 상태다 (장부 스펙 §2).
type Lifecycle string

const (
	LifeActive   Lifecycle = "active"
	LifeRefuted  Lifecycle = "refuted"
	LifeAdopted  Lifecycle = "adopted"
	LifeInferior Lifecycle = "inferior"
)

// ProbeState(계획된 조사의 진행 상태)는 §14-4 4c에서 폐기됐다 — 계획
// 슬롯(ProbeSpec)이 사라지면서 "몇 번째 조사가 pending인가"라는 상태도
// 함께 없어졌다. 남은 조사 여지의 자리는 §7.2 후보 열거(loop/candidates.go)다.

// LinkVerdict는 링크(관측→가설 주장)의 심사 상태다.
type LinkVerdict string

const (
	VerdictProposed LinkVerdict = "proposed"
	VerdictPassed   LinkVerdict = "passed"
	VerdictRejected LinkVerdict = "rejected"
)

// HypothesisRecord는 가설 하나의 현재 상태다 — history의 projection.
// Append 밖에서 수정하지 말 것.
type HypothesisRecord struct {
	Entry       HypothesisEntry
	CreatedSeq  int
	Lifecycle   Lifecycle
	StateReason string
	Dimension   *Dimension // EvHypothesisDimensionSpecified로만 채워짐
}

// EvidenceRecord는 근거 하나의 현재 상태다. Verdicts는 Entry.Links와
// 같은 인덱스로 대응한다.
type EvidenceRecord struct {
	Entry    EvidenceEntry
	Verdicts []LinkVerdict
}

// Ledger는 수첩이다. 이벤트 목록이 정본, 나머지 필드는 파생 캐시다.
type Ledger struct {
	events []Event

	hypos      map[string]*HypothesisRecord
	hypoOrder  []string // 생성 순서 — 순회 결정론
	evidence   map[string]*EvidenceRecord
	evOrder    []string
	terminated bool

	// 사슬 구간(§7.3)의 정체성 추적 — 키는 "<가설>\x00<ClaimID>".
	// ClaimID는 가설 내 유일하고, 정밀화는 교체가 아니라 append이므로
	// 한 구간이 둘에게 대체되는 fork는 거부한다(§5.7과 같은 규율).
	claims          map[string]bool
	claimSuperseded map[string]bool

	// journal — 내구 기록(§15.5). nil이면 구 동작 그대로다(additive).
	journal *Journal
}

// **동시성 전제(6a 검증 소견)**: Ledger는 무잠금이다 — run 하나의 파이프
// 라인이 전 구간 단일 goroutine이라는 전제 위에 서 있다(usage.OnCall→
// Append 배선 포함, go func 0건 실측 2026-08-12). 어댑터를 병렬화하려면
// 이 타입에 잠금을 먼저 넣어야 한다.

// New는 빈 수첩을 만든다.
func New() *Ledger {
	return &Ledger{
		hypos:           map[string]*HypothesisRecord{},
		evidence:        map[string]*EvidenceRecord{},
		claims:          map[string]bool{},
		claimSuperseded: map[string]bool{},
	}
}

// Append는 문장 한 줄을 수첩에 추가한다 — 상태를 바꾸는 유일한 경로.
// 작성 주체가 틀리거나 내용이 현재 상태와 모순이면 거부하고, 수첩에는
// 아무것도 남지 않는다.
func (l *Ledger) Append(actor Actor, t time.Time, p Payload) (Event, error) {
	ev := Event{Seq: len(l.events) + 1, Time: t, Actor: actor, Type: p.eventType(), Payload: p}
	if err := l.validate(ev); err != nil {
		return Event{}, fmt.Errorf("seq %d %s: %w", ev.Seq, ev.Type, err)
	}
	// 내구 기록이 상태 변경보다 **먼저**다(§15.5 크래시 계약 — journal.go의
	// 순서 계약). 기록에 실패하면 상태를 바꾸지 않고 오류를 돌려주므로,
	// "journal에 없는데 상태에는 반영된 이벤트"가 존재할 수 없다.
	// 호출자는 이 오류를 store_failure로 사상해 run을 끝낸다(IsStoreFailure).
	if l.journal != nil {
		if err := l.journal.Append(ev); err != nil {
			return Event{}, fmt.Errorf("seq %d %s 내구 기록: %w", ev.Seq, ev.Type, err)
		}
	}
	l.apply(ev)
	l.events = append(l.events, ev)
	return ev, nil
}

// Events는 history의 복사본을 돌려준다.
func (l *Ledger) Events() []Event {
	out := make([]Event, len(l.events))
	copy(out, l.events)
	return out
}

// Hypothesis와 Evidence, Obligation, Terminated는 projection 읽기다.
func (l *Ledger) Hypothesis(id string) (HypothesisRecord, bool) {
	h, ok := l.hypos[id]
	if !ok {
		return HypothesisRecord{}, false
	}
	return *h, true
}

// ActiveHypotheses는 생성 순서대로 활성 가설의 복사본을 돌려준다 —
// 루프·실행기가 보는 시야다. (구 ledger/loop.go에 있던 것을 그 파일이
// 삭제되면서 옮겨 왔다 — §14-4 4c.)
func (l *Ledger) ActiveHypotheses() []HypothesisRecord {
	out := make([]HypothesisRecord, 0, len(l.hypoOrder))
	for _, id := range l.hypoOrder {
		if h := l.hypos[id]; h.Lifecycle == LifeActive {
			out = append(out, *h)
		}
	}
	return out
}

func (l *Ledger) Evidence(id string) (EvidenceRecord, bool) {
	e, ok := l.evidence[id]
	if !ok {
		return EvidenceRecord{}, false
	}
	return *e, true
}

func (l *Ledger) Terminated() bool { return l.terminated }

// Replay는 history만으로 수첩을 복원한다. Append와 같은 검증·적용
// 경로를 지나므로, 재생 결과는 실시간 상태와 정의상 같다.
func Replay(events []Event) (*Ledger, error) {
	l := New()
	for _, ev := range events {
		if ev.Seq != len(l.events)+1 {
			return nil, fmt.Errorf("replay: seq %d 위치가 어긋남 (기대 %d)", ev.Seq, len(l.events)+1)
		}
		if err := l.validate(ev); err != nil {
			return nil, fmt.Errorf("replay: seq %d %s: %w", ev.Seq, ev.Type, err)
		}
		l.apply(ev)
		l.events = append(l.events, ev)
	}
	return l, nil
}

// ── 검증 — 거부된 문장은 수첩에 남지 않는다 ─────────────────────

func (l *Ledger) validate(ev Event) error {
	if want := allowedActors[ev.Type]; ev.Actor != want {
		return fmt.Errorf("actor %s는 이 문장을 쓸 수 없음 (허용: %s)", ev.Actor, want)
	}
	// llm_usage는 계측 문장이라 종료 후에도 허용한다 — [6] 작문(Writer)의
	// 호출이 loop_terminated 뒤에 온다(§14-6 6a).
	if l.terminated && ev.Type != EvReportAssembled && ev.Type != EvLLMUsage {
		return fmt.Errorf("종료된 수사의 수첩에는 리포트 추출·llm_usage만 기록 가능")
	}

	switch p := ev.Payload.(type) {
	case HypothesisCreated:
		if p.Entry.ID == "" {
			return fmt.Errorf("가설 id 없음")
		}
		if _, dup := l.hypos[p.Entry.ID]; dup {
			return fmt.Errorf("가설 %s 중복", p.Entry.ID)
		}
		// **"반증 조건 없으면 생성 불가"는 폐기됐다**(§14-3, 3차 C-13).
		// 반증력의 관문은 등록 규칙 3(미실측 necessary 술어 ≥1)이며 그
		// 판정은 [4]의 기계층(pipeline/admission.go)이 진다 — 장부는 자기
		// 힘으로 지킬 수 있는 것(형식·PredID 유일성)만 검사한다.
		if len(p.Entry.Sources) == 0 {
			return fmt.Errorf("출처 없는 가설은 생성 불가 — 출처 기여도 측정 재료")
		}
		if len(p.Entry.Chain) == 0 {
			return fmt.Errorf("인과사슬 없는 가설은 생성 불가 (생성 규칙)")
		}
		if p.Entry.Chain[0].Entity != p.Entry.TargetID {
			return fmt.Errorf("첫 구간 entity %q ≠ 의심 대상 %q — 의심 대상은 사슬의 뿌리여야 함",
				p.Entry.Chain[0].Entity, p.Entry.TargetID)
		}
		// v2 additive 필드는 **쓰였을 때만** 검사한다 — 구 경로(빈 필드)는
		// 그대로 통과한다. 등록 규칙 전체(§6.2)는 §14-3 몫이고, 여기서는
		// 장부가 자기 힘으로 지킬 수 있는 것(형식·PredID 유일성)만 진다.
		if len(p.Entry.PredictedSignals) > 0 {
			if err := ValidatePredIDs(p.Entry.PredictedSignals); err != nil {
				return fmt.Errorf("가설 %s 예측 술어: %w", p.Entry.ID, err)
			}
			for _, sp := range p.Entry.PredictedSignals {
				if err := sp.ValidateForm(); err != nil {
					return fmt.Errorf("가설 %s 술어 %s: %w", p.Entry.ID, sp.PredID, err)
				}
			}
		}
		if p.Entry.IdentityKey.TemporalClaim != "" && !ValidTemporalClaim(p.Entry.IdentityKey.TemporalClaim) {
			return fmt.Errorf("temporal_claim %q 미정의", p.Entry.IdentityKey.TemporalClaim)
		}
	case HypothesisRejectedAtCreation:
		// 상태 무관 — history에만 남는 줄.
	case ProbePredicted:
		if err := l.validateProbeTarget(p.ObservationKey, p.PredIDs); err != nil {
			return err
		}
		// 예측은 살아있는 가설 전부를 정확히 한 번씩 덮어야 한다 —
		// 빠진 가설의 예측 없는 실행은 사후 아전인수와 구분 불가.
		covered := map[string]bool{}
		for _, pr := range p.Predictions {
			if pr.ExpectedIfTrue == "" {
				return fmt.Errorf("가설 %s의 예측이 빈 문장", pr.Hypothesis)
			}
			if covered[pr.Hypothesis] {
				return fmt.Errorf("가설 %s의 예측 중복", pr.Hypothesis)
			}
			if hh, ok := l.hypos[pr.Hypothesis]; !ok || hh.Lifecycle != LifeActive {
				return fmt.Errorf("예측 대상 가설 %s가 활성이 아님", pr.Hypothesis)
			}
			covered[pr.Hypothesis] = true
		}
		for _, id := range l.hypoOrder {
			if l.hypos[id].Lifecycle == LifeActive && !covered[id] {
				return fmt.Errorf("활성 가설 %s의 예측 누락 — 예측표는 살아있는 가설 전부", id)
			}
		}
	case DiscriminationExhausted:
		if p.Note == "" {
			return fmt.Errorf("감별 소진에는 사유 필수 — 무엇이 있으면 갈리는지")
		}
	case ProbeExecuted:
		if err := l.validateProbeTarget(p.ObservationKey, p.PredIDs); err != nil {
			return err
		}
		if p.Outcome != ProbeDone && p.Outcome != ProbeInfeasible {
			return fmt.Errorf("probe 결과 %q 미정의", p.Outcome)
		}
	case EvidenceProposed:
		if p.Entry.ID == "" {
			return fmt.Errorf("근거 id 없음")
		}
		if _, dup := l.evidence[p.Entry.ID]; dup {
			return fmt.Errorf("근거 %s 중복", p.Entry.ID)
		}
		if len(p.Entry.Links) == 0 {
			return fmt.Errorf("링크 없는 근거는 기록 불가 — 어느 가설에 대한 주장인지 필요")
		}
		// 링크는 (근거, 가설) 쌍으로 식별·심사·전이된다 — 같은 가설로의
		// 링크가 둘이면 심사 상태가 충돌한다(실 주행 2026-07-16 관찰).
		linked := map[string]bool{}
		for _, lk := range p.Entry.Links {
			if linked[lk.Hypothesis] {
				return fmt.Errorf("같은 가설(%s)로의 링크는 근거당 1개 — 다른 구간을 덮으려면 근거를 나눠 제안하라", lk.Hypothesis)
			}
			linked[lk.Hypothesis] = true
		}
		for _, lk := range p.Entry.Links {
			h, ok := l.hypos[lk.Hypothesis]
			if !ok {
				return fmt.Errorf("링크 대상 가설 %s 없음", lk.Hypothesis)
			}
			if lk.ChainStep != nil {
				if lk.Direction != DirSupports {
					return fmt.Errorf("구간 지목은 supports 링크만 가능 (가설 %s)", lk.Hypothesis)
				}
				if *lk.ChainStep < 0 || *lk.ChainStep >= len(h.Entry.Chain) {
					return fmt.Errorf("가설 %s에 사슬 구간 %d 없음", lk.Hypothesis, *lk.ChainStep)
				}
			}
		}
	case EvidenceLinkPassed:
		if _, err := l.proposedLink(p.Evidence, p.Hypothesis); err != nil {
			return err
		}
	case EvidenceLinkRejected:
		if _, err := l.proposedLink(p.Evidence, p.Hypothesis); err != nil {
			return err
		}
	case HypothesisRefuted:
		if _, err := l.activeHypo(p.Hypothesis); err != nil {
			return err
		}
		lk, err := l.passedLink(p.ByEvidence, p.Hypothesis)
		if err != nil {
			return err
		}
		if lk.Direction != DirRefutes {
			return fmt.Errorf("근거 %s의 링크는 refutes가 아님 — 반증 전이 불가", p.ByEvidence)
		}
	case HypothesisDimensionSpecified:
		if _, err := l.activeHypo(p.Hypothesis); err != nil {
			return err
		}
		lk, err := l.passedLink(p.ByEvidence, p.Hypothesis)
		if err != nil {
			return err
		}
		if lk.DimensionClaim == nil {
			return fmt.Errorf("근거 %s의 링크에 차원 주장 없음 — 차원 확정 불가", p.ByEvidence)
		}
	case ObligationUpdated:
		if !validAxis(p.Axis) {
			return fmt.Errorf("의무 축 %q 미정의", p.Axis)
		}
		if p.Status != ObligationPending && p.Status != ObligationSatisfied && p.Status != ObligationInfeasible {
			return fmt.Errorf("의무 상태 %q 미정의", p.Status)
		}
	case HypothesisAdopted:
		if _, err := l.activeHypo(p.Hypothesis); err != nil {
			return err
		}
	case HypothesisMarkedInferior:
		if _, err := l.activeHypo(p.Hypothesis); err != nil {
			return err
		}
	case LoopTerminated:
		if !p.Decision.Terminate {
			return fmt.Errorf("Terminate=false인 Decision은 종료 기록이 아님")
		}
	case ReportAssembled:
		if p.AsOfSeq < 1 || p.AsOfSeq > len(l.events) {
			return fmt.Errorf("as_of_seq %d가 수첩 범위 밖", p.AsOfSeq)
		}

	// ── 신 문장 (구조 설계 §14-1) ────────────────────────────────
	case PredicateAudited:
		// **가설 실재를 검사하지 않는다** — 심사점 R은 등록 과정에서
		// 돌고(§6.2-5), 그 결과로 반려된 후보는 hypothesis_created가
		// 아예 없다. 실재를 요구하면 "왜 반려했나"의 감사 기록이
		// 기록 불능이 된다(hypothesis_rejected_at_creation이 상태
		// 무관인 것과 같은 이유).
		if p.PredID == "" {
			return fmt.Errorf("pred_id 없는 심사 결과")
		}
		if p.Note == "" {
			return fmt.Errorf("심사에는 note 필수 (§6.2-5 note 먼저)")
		}
	case ChainClaimAsserted:
		if _, err := l.activeHypo(p.Hypothesis); err != nil {
			return err
		}
		if err := p.Claim.Validate(); err != nil {
			return err
		}
		key := p.Hypothesis + "\x00" + p.Claim.ClaimID
		if l.claims[key] {
			return fmt.Errorf("가설 %s에 사슬 구간 %s 중복 — ClaimID는 가설 내 유일",
				p.Hypothesis, p.Claim.ClaimID)
		}
		if s := p.Claim.Supersedes; s != "" {
			if s == p.Claim.ClaimID {
				return fmt.Errorf("사슬 구간 %s가 자기를 대체할 수 없음", s)
			}
			prev := p.Hypothesis + "\x00" + s
			if !l.claims[prev] {
				return fmt.Errorf("대체 대상 사슬 구간 %s가 가설 %s에 없음", s, p.Hypothesis)
			}
			if l.claimSuperseded[prev] {
				return fmt.Errorf("사슬 구간 %s는 이미 대체됨 — fork 반려(§5.7과 같은 규율)", s)
			}
		}
	case AdoptionAudited:
		if _, ok := l.hypos[p.Hypothesis]; !ok {
			return fmt.Errorf("채택 검수 대상 가설 %s 없음", p.Hypothesis)
		}
		if p.ClaimID != "" && p.Note == "" {
			return fmt.Errorf("구간 판정에는 note 필수 (§7.7 규율 — note 먼저)")
		}
	case TemporalJudged:
		if _, ok := l.hypos[p.Hypothesis]; !ok {
			return fmt.Errorf("시간 판정 대상 가설 %s 없음", p.Hypothesis)
		}
		if p.CauseEID == "" || p.SymptomEID == "" {
			return fmt.Errorf("시간 판정에는 원인·증상 양쪽 EID 필수 (§10 피연산자)")
		}
		if !ValidTemporalClaim(p.Claim) {
			return fmt.Errorf("temporal_claim %q 미정의", p.Claim)
		}
		if !ValidTemporalVerdict(p.Verdict) {
			return fmt.Errorf("시간 판정 %q 미정의", p.Verdict)
		}
	case RequiredViewsExtended:
		if !ValidViewPhase(p.Phase) {
			return fmt.Errorf("확정 단계 %q 미정의", p.Phase)
		}
		if len(p.Keys) == 0 {
			return fmt.Errorf("빈 필수 관점 확장 — 분모 재생 불가")
		}
		for _, k := range p.Keys {
			if err := validViewKey(k); err != nil {
				return err
			}
		}
	case RequiredViewNotApplicable:
		if err := validViewKey(p.Key); err != nil {
			return err
		}
		if p.Reason == "" {
			return fmt.Errorf("not_applicable 판정에는 사유 필수 — 분모 축소의 근거")
		}

	// ── 신 Loop 문장 (§14-4 4b) ──────────────────────────────────
	case PriorLowered, LoopStepDecided, WideningExecuted:
		return l.validateLoopV2(ev.Payload)

	case LLMUsage:
		// 계측 문장 — 형식 외 검사 없음(§14-6 6a).

	case ProjectorSpotChecked:
		if p.EID == "" {
			return fmt.Errorf("spot check 기록에 EID 필수 — 무엇을 재대조했는지 없는 검사는 검사가 아니다")
		}

	default:
		return fmt.Errorf("payload 타입 %T 미정의", ev.Payload)
	}
	return nil
}

func (l *Ledger) activeHypo(id string) (*HypothesisRecord, error) {
	h, ok := l.hypos[id]
	if !ok {
		return nil, fmt.Errorf("가설 %s 없음", id)
	}
	if h.Lifecycle != LifeActive {
		return nil, fmt.Errorf("가설 %s는 %s — active 아님", id, h.Lifecycle)
	}
	return h, nil
}

// validateProbeTarget은 probe 문장 둘(예측 선언·실행)의 공통 계약이다
// (§14-4 4c): 지목은 **심사 통과 술어의 PredID**이고, 관측 키가 함께
// 실린다. 자유문 슬롯 시절의 "가설의 몇 번째 probe" 검사가 있던 자리다.
//
// 술어의 무효 딱지(Undecidable·AuditedOut)까지 여기서 막지는 않는다 —
// 그 검사는 실행 입구(probe.Executor.Admit)가 정본이고, 장부가 같은 규칙을
// 두 번 들면 한쪽만 고쳐졌을 때 계약이 갈린다.
func (l *Ledger) validateProbeTarget(key string, predIDs []string) error {
	if key == "" {
		return fmt.Errorf("probe 문장에는 관측 키 필수 — 무엇을 봤는지가 없으면 재생 불가")
	}
	if len(predIDs) == 0 {
		return fmt.Errorf("probe 문장에는 pred_id 지목 필수 — 어느 명제를 해소했는지가 계약이다")
	}
	seen := map[string]bool{}
	for _, id := range predIDs {
		if id == "" {
			return fmt.Errorf("빈 pred_id")
		}
		if seen[id] {
			return fmt.Errorf("pred_id %s 중복 지목", id)
		}
		seen[id] = true
		found := false
		for _, hid := range l.hypoOrder {
			h := l.hypos[hid]
			if h.Lifecycle != LifeActive {
				continue
			}
			for _, p := range h.Entry.PredictedSignals {
				if p.PredID == id {
					found = true
					break
				}
			}
			if found {
				break
			}
		}
		if !found {
			return fmt.Errorf("pred_id %s가 활성 가설의 예측 술어에 없음", id)
		}
	}
	return nil
}

// proposedLink는 심사 대기 중인 링크를, passedLink는 심사 통과한 링크를
// 찾는다. 링크는 (근거, 가설) 쌍으로 특정된다.
func (l *Ledger) proposedLink(evidence, hypo string) (LinkClaim, error) {
	return l.findLink(evidence, hypo, VerdictProposed)
}

func (l *Ledger) passedLink(evidence, hypo string) (LinkClaim, error) {
	return l.findLink(evidence, hypo, VerdictPassed)
}

func (l *Ledger) findLink(evidence, hypo string, want LinkVerdict) (LinkClaim, error) {
	e, ok := l.evidence[evidence]
	if !ok {
		return LinkClaim{}, fmt.Errorf("근거 %s 없음", evidence)
	}
	for i, lk := range e.Entry.Links {
		if lk.Hypothesis == hypo {
			if e.Verdicts[i] != want {
				return LinkClaim{}, fmt.Errorf("근거 %s→가설 %s 링크는 %s (기대: %s)",
					evidence, hypo, e.Verdicts[i], want)
			}
			return lk, nil
		}
	}
	return LinkClaim{}, fmt.Errorf("근거 %s에 가설 %s 링크 없음", evidence, hypo)
}

// validViewKey — 필수 관점 키의 형식(§4 4차 A-5). Metric은
// compare_peers에서만 비지 않는다: 나머지 도구는 (TargetID, Tool)이
// 호출 단위 그대로이므로 Metric이 붙으면 분모가 호출 계획보다 잘게 쪼개진다.
func validViewKey(k RequiredViewKey) error {
	if k.TargetID == "" {
		return fmt.Errorf("필수 관점 키에 target_id 없음")
	}
	if !evidence.ValidSource(k.Tool) {
		return fmt.Errorf("필수 관점 키의 도구 %q는 사상표에 행이 없음", k.Tool)
	}
	if k.Tool == evidence.SrcComparePeers && k.Metric == "" {
		return fmt.Errorf("compare_peers 관점 키에는 metric 필수 (대상×지표당 1회)")
	}
	if k.Tool != evidence.SrcComparePeers && k.Metric != "" {
		return fmt.Errorf("도구 %s의 관점 키에는 metric을 쓰지 않음 (호출 단위가 대상)", k.Tool)
	}
	return nil
}

func validAxis(a ObligationAxis) bool {
	switch a {
	case AxisBreadth, AxisDepth, AxisAlternatives, AxisSelfRefutation, AxisTemporal, AxisEvidenceQuality:
		return true
	}
	return false
}

// ── 적용 — validate를 통과한 문장만 도달한다 ────────────────────

func (l *Ledger) apply(ev Event) {
	switch p := ev.Payload.(type) {
	case HypothesisCreated:
		l.hypos[p.Entry.ID] = &HypothesisRecord{
			Entry: p.Entry, CreatedSeq: ev.Seq, Lifecycle: LifeActive,
		}
		l.hypoOrder = append(l.hypoOrder, p.Entry.ID)
	case HypothesisRejectedAtCreation:
		// 상태 변화 없음 — "왜 고려하지 않았나"의 기록.
	case LLMUsage:
		// 상태 변화 없음 — 호출당 토큰 계측(§14-6 6a). 소비자는 §14-7 §13.
	case ProjectorSpotChecked:
		// 상태 변화 없음 — §5.8-2 재대조 계측(지표 7). 강등은 게이트
		// 판정(DecisionV2) 몫이다.
	case ProbePredicted:
		// 상태 변화 없음 — 실행 전 선언의 history-only 기록 (§15.3).
	case DiscriminationExhausted:
		// 상태 변화 없음 — 감별 소진의 계측 기록이다. 소비자는 §8
		// inferior 진리표 ⑤(status_v2)이며, 의무 6축은 폐기됐다(§14-5 5a).
	case ProbeExecuted:
		// 상태 변화 없음 — 실행의 산출은 index 레코드(ProducedEIDs)이고,
		// 술어의 판정은 진리표가 그 레코드에서 낸다(§7.2-3). 장부가 따로
		// 들고 있을 "probe 진행 상태"는 없다(§14-4 4c).
	case EvidenceProposed:
		verdicts := make([]LinkVerdict, len(p.Entry.Links))
		for i := range verdicts {
			verdicts[i] = VerdictProposed
		}
		l.evidence[p.Entry.ID] = &EvidenceRecord{Entry: p.Entry, Verdicts: verdicts}
		l.evOrder = append(l.evOrder, p.Entry.ID)
	case EvidenceLinkPassed:
		l.setVerdict(p.Evidence, p.Hypothesis, VerdictPassed)
	case EvidenceLinkRejected:
		l.setVerdict(p.Evidence, p.Hypothesis, VerdictRejected)
	case HypothesisRefuted:
		h := l.hypos[p.Hypothesis]
		h.Lifecycle = LifeRefuted
		h.StateReason = p.Reason
	case HypothesisDimensionSpecified:
		dim := p.Dimension
		l.hypos[p.Hypothesis].Dimension = &dim
	case ObligationUpdated:
		// 6축 판정 표면은 폐기됐다(§8·§14-5 5a) — 문장은 과거 journal
		// 재생을 위해 디코드되지만 상태를 바꾸지 않는다.
	case HypothesisAdopted:
		h := l.hypos[p.Hypothesis]
		h.Lifecycle = LifeAdopted
		h.StateReason = p.Reason
	case HypothesisMarkedInferior:
		h := l.hypos[p.Hypothesis]
		h.Lifecycle = LifeInferior
		h.StateReason = p.Reason
	case LoopTerminated:
		l.terminated = true
	case ReportAssembled:
		// 상태 변화 없음 — 감사용.
	case PredicateAudited:
		// 상태 변화 없음 — 술어 딱지(audited_out)는 등록 절차(§14-3)가
		// 이 기록을 읽어 계산한다.
	case ChainClaimAsserted:
		l.claims[p.Hypothesis+"\x00"+p.Claim.ClaimID] = true
		if p.Claim.Supersedes != "" {
			l.claimSuperseded[p.Hypothesis+"\x00"+p.Claim.Supersedes] = true
		}
	case TemporalJudged:
		// 상태 변화 없음 — §8 게이트 재생의 재료.
	case RequiredViewsExtended:
		// 상태 변화 없음 — 분모 projection은 §14-1 1e의 새 status
		// projection(§8 게이트)이 이벤트를 읽어 계산한다.
	case RequiredViewNotApplicable:
		// 상태 변화 없음 — 위와 같다.
	default:
		// 신 Loop 문장(§14-4 4b) — Prior 하향만 상태를 바꾼다.
		l.applyLoopV2(ev.Payload)
	}
}

func (l *Ledger) setVerdict(evidence, hypo string, v LinkVerdict) {
	e := l.evidence[evidence]
	for i, lk := range e.Entry.Links {
		if lk.Hypothesis == hypo {
			e.Verdicts[i] = v
			return
		}
	}
}

// ── 독해기 — 수첩을 집계한다(세거나 찾을 뿐, 판단하지 않는다) ──


