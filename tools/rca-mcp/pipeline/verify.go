// [5] 검증 루프의 **주입 계약** — docs/spec-agent-structure.md §7이 정본이다
// (§14-4 4b).
//
// 여기 있는 것은 계약뿐이다. 루프 본체는 loop 패키지, 조사자 어댑터는 llm
// 패키지에 있다 — **pipeline은 둘 다 import할 수 없다**(방향은 llm→pipeline,
// probe→llm). Screener·Auditor와 같은 주입 규약을 [5]에도 적용한 것이며,
// 그래서 이 파일은 인터페이스와 typed 시야만 정의한다.
//
// 여기 없는 것(침범 금지):
//   - 판정 3갈래·걸음 규율·종료 조건 — loop 패키지(§7.2·§7.6)
//   - LLM 서식 — llm 패키지(§7.1: 도구별 인자 union을 서식에 노출하지 않는다)
package pipeline

import (
	"context"
	"fmt"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/seed"
)

// ── [5] 자리 ────────────────────────────────────────────────────

// VerifyInput은 루프가 받는 책상이다 — 전부 [1]~[4]가 이미 만든 것이다.
type VerifyInput struct {
	Ledger       *ledger.Ledger
	Index        *evidence.Index
	Propositions *evidence.Ledger
	// Vocab — 위상 어휘(§6.2-1ⓐ). W3 확장(§7.4)이 여기에 append한다 — 4d.
	Vocab *TopologyVocab
	// From·To — 인시던트 창(WindowClass=full의 실 창, §7.1 조립기 입력).
	From, To time.Time
	// Onset — onset_narrow 창의 중심(첫 증상 시각).
	Onset time.Time
	// GateWindow — 자격 게이트가 요구하는 시간 범위(§8 창 커버). **정본은
	// GateRequiredWindow 하나다**(1e 인계 ③): 요구 창이 배터리 조회 창보다
	// 넓으면 전 레코드가 창 커버 실패로 weak_support가 된다. nil이면 게이트가
	// unknown = 미충족으로 문다(fail-closed).
	GateWindow *ledger.TimeRange
	// TargetDomains — 투영 레코드의 Domain(§5.1).
	TargetDomains map[string]string
	// SymptomTargets — 증상 멤버 대상(Triage 정렬 순, 주 증상 먼저 —
	// SymptomTargetsOf). §10 증상 앵커(피연산자 B)의 후보 집합이다.
	// 비면 시간 판정이 전부 불명이 되어 confirmed 시간 요건이 영구
	// 미충족이다(fail-closed).
	SymptomTargets []string
	// Topology — seed 박제(incidents.topology). §10 위상 방향 판정
	// (ReqTopologyDirection)의 재료다 — 호출류 간선(apm_call·apm_db)만
	// 쓰며, 비면 전 가설이 무경로 = confirmed 차단이다(fail-closed).
	Topology seed.Graph
	// AdoptAuditor — §7.7 심사점 A의 주입 자리(5c). nil이면 A는 돌지 않고
	// AuditAPassed는 영구 false = confirmed 불가(fail-closed — 심사 없는
	// confirmed는 없다).
	AdoptAuditor AdoptAuditor
	// LayerAComplete — 계층 A 미조사 증상 멤버 없음(§8 요건). **fail-closed
	// 기본값은 false**다 — 판정 주체는 [3]이고 루프는 받아 쓰기만 한다.
	LayerAComplete bool
	// Regenerate — §7.5 재생성의 주입 자리(§14-4 4d). **nil이면 재생성을
	// 하지 않는다**(fail-closed): 승인 판정은 그대로 나고, 실행 자리가
	// 비어 있다는 사실이 생략 사유로 장부에 남는다 — 조용한 우회가 아니다.
	Regenerate Regenerator
	// TargetDomainsOf — W3가 편입한 **신규** 대상의 도메인 해석(§7.4).
	// TargetDomains는 [1]이 확정한 map이라 신규 대상이 없고, 도메인을
	// 모르면 유형별 필수 관점(DB 대상 ≤7콜)을 고를 수 없다. nil이면 신규
	// 대상은 전부 일반 대상으로 취급한다(없는 도메인을 지어내 db_* 호출을
	// 흘리지 않는다 — battery.freeze의 "Triage 해석만 믿는다"와 같은 규율).
	TargetDomainsOf func(ctx context.Context, targetID string) string
}

// ── [4] 재생성 계약 (§7.5 — §14-4 4d) ───────────────────────────

// RefutedBrief는 반증된 가설 하나의 사후 기록이다(§7.5 입력 ③).
type RefutedBrief struct {
	// Signature — 가설의 정체성(§6.1 IdentityKey의 표기). 재생성이 같은
	// 가설을 다시 내지 않게 하는 지목이다.
	Signature string
	// EIDs — 반증 근거 레코드. 사유만 주고 근거를 빼면 생성기가 "왜
	// 틀렸는지"를 관측이 아니라 문장으로만 받는다.
	EIDs   []string
	Reason string
}

// ProbeLogEntry는 실행된 probe 하나의 기록이다(§7.5 입력 ④).
type ProbeLogEntry struct {
	ObservationKey string
	PredIDs        []string
	Outcome        string
	ProducedEIDs   []string
}

// UnexploredArea는 미조사·저신뢰 영역 하나다(§7.5 입력 ⑤).
type UnexploredArea struct {
	// Kind — unscreened | no_data | low_confidence. 사유가 달라 회수
	// 수단도 다르다(no_data의 collector_gap은 재조회로 안 풀린다).
	Kind     string
	TargetID string
	Tool     string
	Detail   string
}

// UndecidedProposition은 아직 판정되지 않은 명제 하나다(§7.5 입력 ⑥).
//
// **C-6의 자리다**: 이것 없이는 생성기가 자신을 반려시킬 제약(등록 규칙 3)을
// 볼 수 없다 — 관측 키 정규화가 기실측 집합을 넓혀 놓아, 재생성 가설이
// 규칙 3에 전멸하고 run이 hypothesis_admission_failure로 떨어지는 경로가
// 실증됐다.
type UndecidedProposition struct {
	Key string
	// NewlyDecidable — material delta(§7.4)로 **새로 판정 가능해진** 키인가.
	// 규칙 3의 delta 문맥 해석이 이 축을 읽는다.
	NewlyDecidable bool
}

// RegenerateInput은 §7.5의 **필수 6요소**다 — 구조체로 강제한다.
//
// 6요소를 하나라도 빼고 부를 수 있게 두면 배선 누락이 "정보가 적은 재생성"
// 으로 조용히 흘러간다. Validate가 그 문을 닫는다.
type RegenerateInput struct {
	// ① 갱신된 index(active 뷰).
	Index *evidence.Index
	// ② ledger projection 요약.
	Projection []HypoBrief
	// ③ 반증 가설 signature + 근거 EID + 사유.
	Refuted []RefutedBrief
	// ④ probe 로그(관측 키·PredIDs·outcome·낳은 EID).
	ProbeLog []ProbeLogEntry
	// ⑤ 미조사·저신뢰 영역.
	Unexplored []UnexploredArea
	// ⑥ 미판정 명제(명제 장부 §6.0에서 **기계 추출**).
	Undecided []UndecidedProposition
	// DeltaKeys — 이번 widening이 만든 material delta의 관측 키(§7.4).
	// 등록 규칙 3의 delta 문맥 해석이 읽는다(§7.5 — 반려 전멸 방지).
	DeltaKeys []evidence.ObservationKey
	// Round — 이 run에서 몇 번째 재생성인가(1-based, ≤2).
	Round int
}

// Validate는 6요소 강제다.
//
// **빈 슬라이스와 nil을 구분한다**: ③④⑤⑥은 정직하게 계산한 결과가 빈
// 목록일 수 있으므로 len==0을 반려하면 정상 경로가 막힌다. 반면 nil은
// "계산하지 않았다"이며 그것이 배선 누락의 모양이다 — 계산한 빈 목록은
// 통과하고 안 계산한 것은 반려된다.
func (in RegenerateInput) Validate() error {
	switch {
	case in.Index == nil:
		return fmt.Errorf("재생성 입력 ①: 갱신된 index 없음")
	case in.Projection == nil:
		return fmt.Errorf("재생성 입력 ②: ledger projection 요약 없음")
	case in.Refuted == nil:
		return fmt.Errorf("재생성 입력 ③: 반증 가설 목록 미계산(빈 목록은 nil이 아니라 길이 0이다)")
	case in.ProbeLog == nil:
		return fmt.Errorf("재생성 입력 ④: probe 로그 미계산")
	case in.Unexplored == nil:
		return fmt.Errorf("재생성 입력 ⑤: 미조사·저신뢰 영역 목록 미계산")
	case in.Undecided == nil:
		return fmt.Errorf("재생성 입력 ⑥: 미판정 명제 목록 미계산 — C-6(규칙 3 반려 전멸)의 방어")
	case in.Round < 1:
		return fmt.Errorf("재생성 회차는 1 이상")
	}
	return nil
}

// Regenerator는 §7.5 재생성의 주입 자리다. 구현은 [4]와 같은 깔때기를
// 다시 도는 것이며(pipeline.RegenerateWith), 재호출 대상은 **index 소비
// 출처 + Grouper뿐**이다(§7.6 43k 모양 — 변경·멤버 출처는 기존 후보를
// 재사용한다).
type Regenerator interface {
	Regenerate(ctx context.Context, in RegenerateInput) ([]ledger.HypothesisEntry, error)
}

// VerifyResult는 루프의 산출이다.
type VerifyResult struct {
	// Decision — 구 공개 계약의 거울([6] 리포트 조립이 읽는다). **판정
	// 본체가 아니다** — 본체는 DecisionV2이고 이것은 그 사영이다.
	Decision ledger.Decision
	// DecisionV2 — §8 판정 게이트의 산출(status_v2).
	DecisionV2 ledger.DecisionV2
	// Ranked — 종료 시점의 §8 순위(RankV2). 구 stages.ranked(심사 통과 링크
	// 수 기준)의 자리를 잇는다 — 결정론 채점의 cause_hit@k·MRR가 읽는다.
	Ranked []ledger.RankedHypo
	// Wheels·Probes·ToolCalls — 파생 상한의 실적(§7.6 감사).
	Wheels, Probes, ToolCalls int
	// Notes — 이음새·생략의 사람 읽을 기록(장부에도 남는다).
	Notes []string
}

// VerifyLoop는 [5]의 주입 자리다. 구 (Investigator, Verifier) 쌍을 대체한다 —
// 실행 주체가 LLM에서 하네스로 옮겨졌으므로(§7.1) 루프는 더 이상 "조사자를
// 부르는 규칙"이 아니라 그 자체가 한 부품이다.
type VerifyLoop interface {
	Verify(ctx context.Context, in VerifyInput) (VerifyResult, error)
}

// ── 조사자 어댑터 계약 (§7.1) ───────────────────────────────────

// ProbeCandidate는 기계가 열거한 후보 하나다 — **한 후보 = 한 관측 키**다.
//
// §7.1의 계약이 이 구조체에 그대로 있다: 조사자는 도구도 인자도 창도 쓰지
// 않고 **PredIDs 하나를 고른다**. 도구·대상·창은 이미 명제 키가 정했고
// (하네스가 조립한다), LLM에게 남는 것은 작문이 아니라 선택이다.
type ProbeCandidate struct {
	// PredIDs — 이 관측 키를 지목하는 술어 전량(가설을 넘나든다). 요청은
	// 이 목록을 **그대로** 복사해야 한다 — 키가 섞이면 입구가 반려한다.
	PredIDs []string
	// Key — canonical 관측 키의 표기(§6.0). 사람·LLM이 읽는 식별자다.
	Key string
	// Tool·TargetID·Aspect·Metric·EntityKey·Window — 키의 성분. **인자가
	// 아니라 명제의 좌표다**(도구별 인자 union은 서식에 없다).
	Tool, TargetID, Aspect, Metric, EntityKey, Window string
	// Expectations — 가설별 예측("H1: must_hold necessary"). 식별력의 근거가
	// 서식에 보여야 조사자가 정렬 이유를 이해한다.
	Expectations []string
	// Priority — 이 후보가 왜 앞에 있는가(§7.2 정렬 축의 이름).
	Priority string
	// Note — 정렬 근거의 한 줄 설명.
	Note string
}

// 우선순위 어휘(§7.2) — 정렬 축의 이름이자 서식에 실리는 라벨이다.
const (
	// PriorityTemporal — §7.2-4 시간 요건 probe 우선 배치. **C-7의 해소**:
	// 종전에는 이 규칙을 발행할 채널이 실물에 없었다.
	PriorityTemporal = "temporal_requirement"
	// PriorityNecessary — §7.2-5 경쟁 necessary 선배분(4차 A-8).
	PriorityNecessary = "necessary_allocation"
	// PriorityDiscriminating — §7.2-2 식별력 정렬.
	PriorityDiscriminating = "discriminating"
	// PriorityRoutine — 예측이 갈리지 않는 확증 probe(후순위 강등, 금지 아님).
	PriorityRoutine = "corroborating"
)

// HypoBrief는 가설 하나의 기계 요약이다 — **대화 이력이 아니다**(§7.1
// 무상태): 바퀴마다 장부 projection에서 새로 조립된다.
type HypoBrief struct {
	ID        string
	Mechanism string
	Chain     string
	// Qualified·Weak·Inconclusive·NecessaryUnresolved — Tally 사영(§8).
	Qualified, Weak, Inconclusive, NecessaryUnresolved int
	// Refuted — 진리표가 반증으로 판정한 가설(§7.2-3). 후보 열거에서 빠진다.
	Refuted bool
}

// ObservationBrief는 직전 실행이 낳은 레코드의 기계 요약이다.
// **봉투 원문이 아니다**(§7.1 "다음 바퀴 입력에는 기계 요약·발췌만").
type ObservationBrief struct {
	EID      string
	TargetID string
	Headline string
	Status   string
}

// ProbeChoiceInput은 조사자가 보는 전부다.
type ProbeChoiceInput struct {
	Wheel       int
	ProbesUsed  int
	ProbeBudget int
	Hypotheses  []HypoBrief
	// Candidates — **우선순위 순**이다(§7.2). 순서 자체가 걸음 규율의 발행이다.
	Candidates []ProbeCandidate
	Recent     []ObservationBrief
	// Feedback — 직전 요청의 반려 사유(probe.RejectError의 Kind별 행동 지침).
	// §14-3 선례(admissionGuidance)를 승계한다 — 사유만 돌려주면 26B는 같은
	// 위반을 되풀이한다.
	Feedback []string
}

// ProbeRequester는 §7.1의 조사자 어댑터다 — **무상태**다. 출력은
// ProbeRequest 하나이며, 어댑터는 strict 디코드(DecodeProbeRequest)를 통과한
// 값만 돌려준다.
type ProbeRequester interface {
	NextProbe(ctx context.Context, in ProbeChoiceInput) (ledger.ProbeRequest, error)
}
