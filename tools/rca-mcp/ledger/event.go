// 이벤트(수첩 문장) 16종의 타입 정의 — 장부 스펙 §5의 payload 상세
// 스키마 정본이다. 겉면(Event)은 공통, 내용물(Payload)은 문장 종류별
// 타입으로 고정된다. 문장 종류마다 쓸 수 있는 주체가 하나로 정해져
// 있어(allowedActors) "조사자는 상태를 바꾸지 못한다"(장부 원칙 1)가
// 타입 수준에서 강제된다(예외였던 probe_added는 §14-4 4c에서 폐기).
package ledger

import "time"

// Actor는 수첩에 문장을 쓰는 주체다.
type Actor string

const (
	ActorInvestigator Actor = "investigator"
	ActorVerifier     Actor = "verifier"
	ActorRule         Actor = "rule"
	ActorPipeline     Actor = "pipeline"
)

// EventType은 문장 종류다 — 아래 구 16종 + 구조 설계 §14-1이 추가한
// 신 5종(event_v2.go). 구 16종은 소비자가 §14-4·§14-5에서 옮겨갈 때까지
// 삭제·변경하지 않는다.
type EventType string

const (
	EvHypothesisCreated            EventType = "hypothesis_created"
	EvHypothesisRejectedAtCreation EventType = "hypothesis_rejected_at_creation"
	EvProbePredicted               EventType = "probe_predicted"
	EvProbeExecuted                EventType = "probe_executed"
	EvEvidenceProposed             EventType = "evidence_proposed"
	EvEvidenceLinkPassed           EventType = "evidence_link_passed"
	EvEvidenceLinkRejected         EventType = "evidence_link_rejected"
	EvHypothesisRefuted            EventType = "hypothesis_refuted"
	EvHypothesisDimensionSpecified EventType = "hypothesis_dimension_specified"
	EvObligationUpdated            EventType = "obligation_updated"
	EvHypothesisAdopted            EventType = "hypothesis_adopted"
	EvHypothesisMarkedInferior     EventType = "hypothesis_marked_inferior"
	EvLoopTerminated               EventType = "loop_terminated"
	EvReportAssembled              EventType = "report_assembled"
	EvDiscriminationExhausted      EventType = "discrimination_exhausted"
)

// allowedActors — 문장 종류별 유일한 작성 주체. 심사는 확인자만,
// 상태 전이·판정은 규칙만 쓸 수 있다. 예외였던 probe_added(출처별 주체)는
// §14-4 4c에서 폐기됐다 — 이제 전 종류가 이 표 하나로 검증된다.
var allowedActors = map[EventType]Actor{
	EvHypothesisCreated:            ActorRule,
	EvHypothesisRejectedAtCreation: ActorRule,
	EvProbePredicted:               ActorInvestigator,
	EvProbeExecuted:                ActorInvestigator,
	EvEvidenceProposed:             ActorInvestigator,
	EvEvidenceLinkPassed:           ActorVerifier,
	EvEvidenceLinkRejected:         ActorVerifier,
	EvHypothesisRefuted:            ActorRule,
	EvHypothesisDimensionSpecified: ActorRule,
	EvObligationUpdated:            ActorRule,
	EvHypothesisAdopted:            ActorRule,
	EvHypothesisMarkedInferior:     ActorRule,
	EvLoopTerminated:               ActorRule,
	EvReportAssembled:              ActorPipeline,
	EvDiscriminationExhausted:      ActorInvestigator,
}

// Event는 수첩 한 줄의 겉면이다. Seq는 Append가 부여하는 단조 증가
// 번호이고, 내용물은 Payload 타입이 문장 종류를 결정한다.
type Event struct {
	Seq     int
	Time    time.Time
	Actor   Actor
	Type    EventType
	Payload Payload
}

// Payload는 문장 종류별 내용물이다. 기준: 그 줄만 읽고도 상태를 그대로
// 따라갈 수 있을 만큼 — 딱 그만큼만.
type Payload interface {
	eventType() EventType
}

// ── 공용 값 타입 ────────────────────────────────────────────────

// Dimension은 범인 차원이다 (예: {sql_hash, abc123}).
type Dimension struct {
	Label string
	Value string
}

// Window는 관측이 가리키는 시간 범위다 — 시간 정합 의무의 결정론 입력.
type Window struct {
	From time.Time
	To   time.Time
}

// SourceStatus는 도구 응답 봉투에서 승계한 관측 상태다.
type SourceStatus string

const (
	SourceNormal    SourceStatus = "normal"
	SourceAnomalous SourceStatus = "anomalous"
	SourceNoData    SourceStatus = "no_data"
)

// LinkDirection은 관측이 가설에 작용하는 방향이다.
type LinkDirection string

const (
	DirSupports LinkDirection = "supports"
	DirRefutes  LinkDirection = "refutes"
)

// HypoSource는 가설이 어느 단계에서 나왔는지다 (구조 설계 §5).
type HypoSource string

const (
	SourceChange        HypoSource = "change"
	SourceMember        HypoSource = "member"
	SourceInvestigation HypoSource = "investigation"
	SourcePastCase      HypoSource = "past_case"
)

// ChainStep은 인과사슬의 한 구간이다 — "이 지점에서 이런 일이 일어나
// 다음 구간으로 전파된다". 첫 구간은 의심 대상(범인), 끝 구간은 증상.
type ChainStep struct {
	Entity string // target_id
	Effect string // 이 지점에서 일어나는 일
}

// ProbeOutcome은 조사 실행 결과다.
type ProbeOutcome string

const (
	ProbeDone       ProbeOutcome = "done"
	ProbeInfeasible ProbeOutcome = "infeasible"
)

// ObligationStatus는 완결성 의무 한 축의 상태다. **판정 표면은 폐기됐다**
// (§8 — 6축은 §14-5 5a에서 코드째 사라졌고 기능은 판정 게이트가 계승했다).
// 타입이 남는 이유는 과거 journal의 obligation_updated 문장을 디코드할 수
// 있어야 하기 때문이다 — 이벤트 어휘는 append-only다.
type ObligationStatus string

const (
	ObligationPending    ObligationStatus = "pending"
	ObligationSatisfied  ObligationStatus = "satisfied"
	ObligationInfeasible ObligationStatus = "infeasible"
)

// ObligationAxis는 완결성 의무 6축의 식별자다.
type ObligationAxis string

const (
	AxisBreadth         ObligationAxis = "breadth"
	AxisDepth           ObligationAxis = "depth"
	AxisAlternatives    ObligationAxis = "alternatives"
	AxisSelfRefutation  ObligationAxis = "self_refutation"
	AxisTemporal        ObligationAxis = "temporal"
	AxisEvidenceQuality ObligationAxis = "evidence_quality"
)

// ── 묶음 A: 가설의 탄생 ─────────────────────────────────────────

// HypothesisEntry는 가설의 출생신고서다. suspect.dimension은 여기 없다 —
// 차원은 EvHypothesisDimensionSpecified로만 채워진다.
type HypothesisEntry struct {
	ID       string
	TargetID string      // canonical target_id
	Chain    []ChainStep // 원인→증상 인과사슬. 첫 구간 entity = TargetID 강제
	// Sources — 첫 원소가 대표 출처(먼저 나온 후보의 것), 나머지는 깔때기
	// 병합으로 합류한 출처들(설계 §4 "출처 꼬리표는 합집합"). 출처별
	// 기여도 ablation(§11.1)의 측정 재료.
	Sources        []HypoSource
	Prior          Prior
	PriorRationale string
	// RefutationCondition은 폐기됐다(§14-3, 3차 검토 C-13) — 반증은 가설
	// 이름에 붙은 자유문이 아니라 typed 술어(Role=necessary)에 붙는다.
	// 반증력의 자리는 등록 규칙 3의 "미실측 necessary 술어 ≥1" 요건이
	// 계승한다(pipeline/admission.go rule3Violations).
	//
	// **Probes(ProbeSpec 자유문 계획)도 폐기됐다**(§14-4 4c): 실행 주체가
	// 하네스로 옮겨진 뒤(§7.1) 조사 계획의 자리는 PredictedSignals 하나다 —
	// 무엇을 볼지는 술어의 관측 키가 정하고, 도구·인자는 기계가 조립한다.

	// ── 가설 계약 v2의 additive 3필드 (구조 설계 §6). 구 필드와 공존하며,
	// 소비자 재배선은 §14-3·§14-4 몫이다. 비어 있으면 구 경로 그대로다.

	// IdentityKey — 가설의 정체성(CauseEntity·Mechanism·ImpactScope·
	// TemporalClaim). Mechanism은 닫힌 enum이 아니다(§6.1).
	IdentityKey HypoIdentity
	// PredictedSignals — 참이면 보여야/안 보여야 하는 신호. 개설 시
	// 선언과 걸음 시 예측표(PredictionDecl)가 같은 SignalPred를 가리켜야
	// §7.2의 식별력 정렬이 기계 계산이 된다(§6.0).
	PredictedSignals []SignalPred
	// SupportEIDs — 개설 근거 레코드(active). 각 EID는 술어(EIDHint 경유)
	// 또는 ChainClaim에 결박되어야 하며 — 미결박 EID는 개설 근거로 세지
	// 않는다(§6, C-4: 임의 active EID + 무관 술어로 개설 통과하는 경로
	// 차단). 결박·active 검사는 명제 장부와 index가 붙는 §14-1 1c 이후다.
	SupportEIDs []string

	// Merge — 깔때기 3단계 병합의 정보 유실 기록(§6.2 병합 규칙, C-14).
	// 단일 후보면 nil이다. **기존 감사 이벤트(hypothesis_created)의 additive
	// 확장**이며, 병합 사실 자체는 종전대로 Sources 합집합이 진다.
	Merge *MergeAudit `json:",omitempty"`
}

// MergeAudit는 병합에서 대표가 되지 못한 후보의 유실 기록이다(§6.2 C-14).
//
// **왜 기록이 필요한가**: 사슬 말단은 §8 confirmed의 기계 판정 입력이라
// 병합 순서가 등급을 좌우했다. 대표 선택 규칙(말단이 더 깊은 쪽)만으로는
// 밀려난 사슬이 어디로 갔는지 감사에서 사라진다.
type MergeAudit struct {
	// MergedCount — 병합에 든 후보 수(대표 포함).
	MergedCount int
	// ChainDepth — 대표 사슬의 깊이(구간 수. ChainClaim 이관 후에는 말단
	// 개체 깊이, §14-4).
	ChainDepth int
	// ChainReplaced — 대표 사슬이 첫 후보의 것이 아니다(더 깊은 쪽으로 교체).
	ChainReplaced bool
	// DedupedPreds — 명제 키(§6.0) 중복으로 접힌 술어 수.
	DedupedPreds int
	// DroppedChains — 대표가 되지 못한 사슬들.
	DroppedChains []DroppedChain
}

// DroppedChain은 비대표 후보의 사슬 하나다.
type DroppedChain struct {
	Source HypoSource
	Depth  int
	// ClaimIDs — 구간 지목 목록. 구 ChainStep에는 ClaimID가 없어 하네스가
	// `<i>:<entity>` 표기를 만든다(§14-4에서 실 ClaimID로 교체).
	ClaimIDs []string
}

// MechanismText는 사슬을 사람이 읽는 한 문장으로 잇는다 — 리포트·심사
// 프롬프트용 표시값이며 판정에는 쓰지 않는다.
func (e HypothesisEntry) MechanismText() string {
	s := ""
	for i, st := range e.Chain {
		if i > 0 {
			s += " → "
		}
		s += st.Entity + ": " + st.Effect
	}
	return s
}

type HypothesisCreated struct {
	Entry HypothesisEntry
}

type HypothesisRejectedAtCreation struct {
	ProposalSummary string
	ViolatedRule    string
}

// ── 묶음 B: 조사 ────────────────────────────────────────────────

// probe_added(14번째 문장)는 §14-4 4c에서 폐기됐다. 그 문장이 실어 나르던
// 것은 ProbeSpec 자유문 슬롯 하나였고, 자유문이 사라지자 남는 payload가
// 없다 — 두 출처도 함께 소멸했다: depth_obligation은 쓰는 규칙이 없었고
// (의무 6축은 §8 게이트가 계승, §14-5), investigator_followup은 하네스가
// 실행마다 슬롯을 새로 열던 4b의 과도기 계약이었다.

// HypoPrediction은 예측표의 한 줄이다 — "이 가설이 참이라면 이번
// probe의 결과는 이렇게 나온다".
type HypoPrediction struct {
	Hypothesis     string
	ExpectedIfTrue string
}

// ProbePredicted — 15번째 문장(탐색 정책 §15.3, 2026-07-21). probe
// 실행 전의 예측 선언이다 — probe_executed보다 먼저 기록되어야 사후
// 아전인수 해석과 장부에서 구분된다. 상태 전이는 없다(history-only):
// 확인자의 예측 vs 실제 대조(유보)와 리포트의 감사 재료.
//
// **지목은 PredID다**(§14-4 4c): 종전의 (가설, Probes 인덱스) 결박은 자유문
// 슬롯과 함께 폐기됐다. 선언의 대상은 이번 실행이 해소하려는 명제이며,
// 그 명제에 술어를 건 가설이 곧 소속 가설이다.
type ProbePredicted struct {
	// ObservationKey — 이번 실행이 겨냥한 관측 키(canonical, §6.0).
	ObservationKey string
	// PredIDs — 해소를 시도하는 술어들. 활성 가설의 술어여야 한다.
	PredIDs     []string
	Predictions []HypoPrediction
	Note        string // 선택 이유 (분별력·비용 판단)
}

// DiscriminationExhausted — 16번째 문장. 살아있는 가설들을 갈라주는
// probe를 더 제안하지 못함(감별 소진, §15.3의 원리적 종료 신호).
// Note = 어떤 정보가 있으면 갈리는지 — missing_evidence 재료.
type DiscriminationExhausted struct {
	Note string
}

// ProbeExecuted — 실행 결과. **지목은 PredID다**(§14-4 4c): 실행 주체가
// 하네스로 옮겨진 뒤(§7.1) "가설이 계획한 몇 번째 조사"라는 자리는 없고,
// 한 실행이 여러 가설의 술어를 동시에 해소한다.
type ProbeExecuted struct {
	// ObservationKey — 이 실행이 해소한 관측 키(canonical, §6.0).
	ObservationKey string
	// PredIDs — 해소를 시도한 술어들. 활성 가설의 술어여야 한다.
	PredIDs []string
	Outcome ProbeOutcome
	Reason  string // infeasible일 때 사유 (not_collected 등)
	Note    string
	// ProducedEIDs — 이 실행이 낳은 index 레코드의 EID 목록(구조 설계
	// §7.1, additive). 이 연결이 없으면 §8의 "신규 관측 ≥1"(선언 이후
	// 실행이 낳은 레코드)을 재생으로 증명할 수 없다. 하네스가 도구를
	// 부르고 projector가 레코드를 만드는 경로(§14-1 1b)가 채운다.
	ProducedEIDs []string
}

// LinkClaim은 "이 관측이 이 가설을 지지/반증한다"는 조사자의 주장이다.
// DimensionClaim은 부수 주장 — 이 링크가 심사를 통과해도 차원은
// 규칙의 확정 이벤트가 있어야 채워진다. ChainStep은 supports 링크가
// 인과사슬의 몇 번 구간을 지지하는지다(선택) — 인과사슬 채움 판정은
// 구간을 지목한 통과 링크만 센다.
type LinkClaim struct {
	Hypothesis     string
	Direction      LinkDirection
	DimensionClaim *Dimension
	ChainStep      *int // supports 전용. Chain의 인덱스
}

type EvidenceEntry struct {
	ID             string
	Observation    string
	ObservedWindow *Window
	Refs           []string // 도구 응답의 evidence ref (원본 추적)
	SourceStatus   SourceStatus
	Links          []LinkClaim
}

type EvidenceProposed struct {
	Entry EvidenceEntry
}

// ── 묶음 C: 심사 ────────────────────────────────────────────────

type EvidenceLinkPassed struct {
	Evidence   string
	Hypothesis string
	Note       string
}

type EvidenceLinkRejected struct {
	Evidence   string
	Hypothesis string
	Note       string
}

type HypothesisRefuted struct {
	Hypothesis string
	ByEvidence string // 반증 조건 충족을 심사 통과한 링크의 근거 id
	Reason     string
}

// ── 묶음 D: 판정과 종료 ─────────────────────────────────────────

// HypothesisDimensionSpecified — 13번째 문장. 차원 주장을 담은 링크가
// 심사를 통과한 뒤, 규칙이 가설의 차원을 확정하는 별도 상태 전이 줄이다
// (반증의 2단계 패턴과 대칭: 도장 줄 → 전이 줄).
type HypothesisDimensionSpecified struct {
	Hypothesis string
	Dimension  Dimension
	ByEvidence string
}

type ObligationUpdated struct {
	Axis   ObligationAxis
	Status ObligationStatus
	Note   string
	Refs   []string
}

type HypothesisAdopted struct {
	Hypothesis string
	Reason     string // 진리표 행 식별자
}

type HypothesisMarkedInferior struct {
	Hypothesis string
	Reason     string
}

type LoopTerminated struct {
	Decision Decision // 종료 선언 — §8 판정(DecisionV2)의 공개 계약 사영
}

type ReportAssembled struct {
	AsOfSeq int // 어느 seq 시점의 장부에서 추출했는지
}

func (HypothesisCreated) eventType() EventType            { return EvHypothesisCreated }
func (HypothesisRejectedAtCreation) eventType() EventType { return EvHypothesisRejectedAtCreation }
func (ProbePredicted) eventType() EventType               { return EvProbePredicted }
func (ProbeExecuted) eventType() EventType                { return EvProbeExecuted }
func (DiscriminationExhausted) eventType() EventType      { return EvDiscriminationExhausted }
func (EvidenceProposed) eventType() EventType             { return EvEvidenceProposed }
func (EvidenceLinkPassed) eventType() EventType           { return EvEvidenceLinkPassed }
func (EvidenceLinkRejected) eventType() EventType         { return EvEvidenceLinkRejected }
func (HypothesisRefuted) eventType() EventType            { return EvHypothesisRefuted }
func (HypothesisDimensionSpecified) eventType() EventType { return EvHypothesisDimensionSpecified }
func (ObligationUpdated) eventType() EventType            { return EvObligationUpdated }
func (HypothesisAdopted) eventType() EventType            { return EvHypothesisAdopted }
func (HypothesisMarkedInferior) eventType() EventType     { return EvHypothesisMarkedInferior }
func (LoopTerminated) eventType() EventType               { return EvLoopTerminated }
func (ReportAssembled) eventType() EventType              { return EvReportAssembled }
