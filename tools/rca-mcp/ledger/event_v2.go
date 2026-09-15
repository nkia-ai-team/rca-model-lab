// 신 이벤트 타입 — docs/spec-agent-structure.md가 §14-1에서 요구하는
// 5종 + C-12 해소분 1종이다. 구 16종은 손대지 않는다(공존 — 소비자
// 재배선은 §14-4·§14-5 몫).
//
//	predicate_audited            §6.2-5 심사점 R의 항목별 판정
//	chain_claim_asserted         §7.3 사슬 구간 주장(정밀화 포함)
//	temporal_judged              §10 선후 판정에 쓴 쌍과 결과
//	required_views_extended      §4 필수 관점 집합의 확정·확장
//	required_view_not_applicable §4·§5.1 분모 제외의 재생 가능성(C-12)
//
// probe_executed의 produced_eids(§7.1)는 새 타입이 아니라 기존 payload의
// additive 필드다 — event.go 참조.
package ledger

import "github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"

const (
	EvPredicateAudited          EventType = "predicate_audited"
	EvChainClaimAsserted        EventType = "chain_claim_asserted"
	EvTemporalJudged            EventType = "temporal_judged"
	EvRequiredViewsExtended     EventType = "required_views_extended"
	EvRequiredViewNotApplicable EventType = "required_view_not_applicable"
	// EvAdoptionAudited — §7.7 심사점 A의 판정 기록(§14-5 5c). 구간 판정
	// (ClaimID 지정)과 라운드 요약(ClaimID="")의 두 모양이 한 타입이다 —
	// 게이트 재생 가능성(AuditAPassed·탈락 구간 강등)이 이 이벤트에 달려
	// 있다.
	EvAdoptionAudited EventType = "adoption_audited"
)

// allowedActorsV2 — 신 문장의 작성 주체. init에서 구 표에 합류시킨다
// (구 표의 리터럴을 건드리지 않고 추가만 한다).
//
// predicate_audited만 확인자다 — 심사점 R은 Verifier의 새 자리 ①(§6.2-5).
// 나머지는 전부 규칙 계산이다: 사슬 주장은 LLM이 제안해도 장부에 쓰는
// 주체는 규칙이고(구 hypothesis_created와 같은 패턴), 시간 판정·필수 관점
// 집합은 정의상 기계 산출이다.
var allowedActorsV2 = map[EventType]Actor{
	EvPredicateAudited:          ActorVerifier,
	EvChainClaimAsserted:        ActorRule,
	EvTemporalJudged:            ActorRule,
	EvRequiredViewsExtended:     ActorRule,
	EvRequiredViewNotApplicable: ActorRule,
	// adoption_audited도 확인자다 — 심사점 A는 Verifier의 새 자리 ②(§7.7).
	EvAdoptionAudited: ActorVerifier,
}

func init() {
	for t, a := range allowedActorsV2 {
		allowedActors[t] = a
	}
}

// ── predicate_audited (§6.2-5) ──────────────────────────────────

// PredicateVerdict는 R의 항목별 출력이다(§6.2-5).
// **RoleValid는 *bool이다** — nil = "자명성 심사 대상 아님". 미실측 술어는
// 장부 첨부 관측값이 정의상 비어 있어 R에게 판정 재료가 없는데, R 규율
// ("불확실은 불리 방향")을 그대로 적용하면 necessary가 전원 corroborating
// 으로 강등돼 등록 규칙 3이 전 가설을 반려하는 데드락이 생긴다(C-2).
// nil을 표현할 수 있어야 어댑터가 그 항목을 심사 대상에서 뺄 수 있다.
type PredicateVerdict struct {
	Pertinent   bool
	Substantive bool
	RoleValid   *bool
}

// Valid는 유효 술어 판정이다 — Pertinent·Substantive 둘 다 참(§6.2-5).
// RoleValid=false는 무효가 아니라 corroborating 강등 사유다.
func (v PredicateVerdict) Valid() bool { return v.Pertinent && v.Substantive }

// PredicateAudited — 술어 하나의 심사 결과. 누락 항목은 audited_out으로
// 기록되므로(fail-closed) 이 이벤트가 없는 술어는 무효 취급이다.
type PredicateAudited struct {
	Hypothesis string
	PredID     string
	Verdict    PredicateVerdict
	Note       string // R 규율 "note 먼저" — 필수
}

// ── chain_claim_asserted (§7.3) ─────────────────────────────────

// ChainClaimAsserted — 사슬 구간의 주장. 정밀화는 교체가 아니라 새
// ClaimID + Supersedes의 append다(§7.3).
type ChainClaimAsserted struct {
	Hypothesis string
	Claim      ChainClaim
}

// ── temporal_judged (§10) ───────────────────────────────────────

// TemporalVerdict는 §10 판정표의 결과다.
type TemporalVerdict string

const (
	// TemporalPrecedesVerdict — A.onset_hi < B.onset_lo (원인측 선행).
	TemporalPrecedesVerdict TemporalVerdict = "precedes"
	// TemporalFollowsVerdict — B.onset_hi < A.onset_lo (후행).
	TemporalFollowsVerdict TemporalVerdict = "follows"
	// TemporalOverlapVerdict — 겹침. precedes 가설에는 불명이지만
	// coincides 가설에는 확인 판정이다 — 그래서 payload에 Claim이 실린다.
	TemporalOverlapVerdict TemporalVerdict = "overlap"
	// TemporalUndeterminedVerdict — 산식 호출 불가(ChangeFrom/To nil,
	// 또는 CollectLag·ClockSkew가 observed 아님, §10 전제).
	TemporalUndeterminedVerdict TemporalVerdict = "undetermined"
)

func ValidTemporalVerdict(v TemporalVerdict) bool {
	switch v {
	case TemporalPrecedesVerdict, TemporalFollowsVerdict,
		TemporalOverlapVerdict, TemporalUndeterminedVerdict:
		return true
	}
	return false
}

// TemporalJudged — 판정에 쓴 쌍과 결과(§10 "게이트 재생 가능성").
// **Claim이 payload에 있어야** 판정을 TemporalClaim 축으로 분기할 수 있다
// (C-6 — §10 판정표에는 TemporalClaim 축이 없어 first-match로 읽으면
// coincides 가설이 겹침에서 영구 confirmed 불가가 된다. 표의 문안 정정은
// 스펙 몫이지만, 이벤트에 축이 없으면 나중에 분기 자체가 불가능하다).
type TemporalJudged struct {
	Hypothesis string
	CauseEID   string
	SymptomEID string
	Claim      TemporalClaim
	Verdict    TemporalVerdict
	Note       string
}

// ── adoption_audited (§7.7 심사점 A) ────────────────────────────

// AdoptionAudited — 채택 검수의 판정 기록.
//
//	ClaimID != "" — 구간 판정: 그 사슬 구간이 질문 ①(지지 실질)·②(말단
//	                깊이)를 통과했는가. **탈락(Passed=false)은 그 ClaimID가
//	                active인 동안 지지 강등으로 소비된다**(projection) —
//	                정밀화가 구간을 supersede하면 새 ClaimID는 미심사로
//	                돌아가고 구 탈락의 효력은 함께 접힌다.
//	ClaimID == "" — 라운드 요약: 이번 라운드 전 구간 통과 여부.
//	                AuditAPassed의 정본 원천이다(마지막 요약이 이긴다).
type AdoptionAudited struct {
	Hypothesis string
	ClaimID    string
	Passed     bool
	Note       string // A 규율 "note 먼저" — 구간 판정에는 필수
}

// ── required_views_extended (§4) ────────────────────────────────

// RequiredViewKey는 "봤어야 할 것"의 키다(§4, 4차 A-5).
// **키 = (TargetID, Tool) — B2(compare_peers)만 (TargetID, Tool, Metric)**.
// 종전 (target, aspect)는 호출 계획보다 거칠었다: db_slow_queries와
// db_blocking이 둘 다 Aspect=db라 한쪽만 실패해도 "조회됨"이 됐다.
//
// 비교 가능한 값 타입이다 — 분모 집합을 map으로 든다.
type RequiredViewKey struct {
	TargetID string
	Tool     evidence.ObservationSource
	Metric   string // compare_peers만 비지 않는다
}

// ViewPhase는 필수 관점 집합의 확정 단계다(§4 — 확정은 2단이고, 계층 B
// 계획은 계층 A의 anomalous 결과의 함수라 실행 전에 존재할 수 없다).
type ViewPhase string

const (
	PhaseLayerA ViewPhase = "layer_a" // admission으로 N이 동결되는 시점
	PhaseLayerB ViewPhase = "layer_b" // 계층 A 완료 직후
	// PhaseTargetAdded — W3·A0가 만든 신규 대상의 편입(§4 4차 A-12).
	// 넓히는 순간 해야 할 검사도 같이 정의되므로 "조사를 넓힐수록 감점"의
	// 역인센티브가 생기지 않는다.
	PhaseTargetAdded ViewPhase = "target_added"
)

func ValidViewPhase(p ViewPhase) bool {
	return p == PhaseLayerA || p == PhaseLayerB || p == PhaseTargetAdded
}

// RequiredViewsExtended — 집합의 확정·확장. 세 소비자(§5.2 롤업 분모·
// 계층 B 절단 기록·§8.2-1 rubric 분모)가 같은 집합을 공유하므로 분모
// 재생 가능성이 이 이벤트에 달려 있다.
type RequiredViewsExtended struct {
	Phase  ViewPhase
	Keys   []RequiredViewKey
	Reason string
}

// RequiredViewNotApplicable — 그 대상엔 원래 미적용이라 분모에서 빼는
// 키(§5.1 not_applicable).
//
// **집합은 append-only이므로 제거 이벤트를 두지 않는다**(C-12): §4는
// 집합을 append-only로 규정하는데 not_applicable은 호출 결과로만 판정되는
// "실행 후 분모 축소"라 두 규정이 모순이었다. 해소는 제거가 아니라
// **딱지**다 — 키는 집합에 남고 분모 계산에서만 빠지며, 이 이벤트가 그
// 딱지의 재생 근거다.
type RequiredViewNotApplicable struct {
	Key    RequiredViewKey
	Reason string // no_data_reason=not_collected 등 판정 근거
}

func (PredicateAudited) eventType() EventType          { return EvPredicateAudited }
func (ChainClaimAsserted) eventType() EventType        { return EvChainClaimAsserted }
func (TemporalJudged) eventType() EventType            { return EvTemporalJudged }
func (AdoptionAudited) eventType() EventType           { return EvAdoptionAudited }
func (RequiredViewsExtended) eventType() EventType     { return EvRequiredViewsExtended }
func (RequiredViewNotApplicable) eventType() EventType { return EvRequiredViewNotApplicable }
