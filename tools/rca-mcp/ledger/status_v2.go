// 가설 status projection (신) — docs/spec-agent-structure.md §8(판정 게이트)이
// 정본이다. 술어 단위 판정은 verdict_v2.go(§6 진리표)가 낸다.
//
// **구 판정 표면(status.go의 ComputeStatus·의무 6축)은 폐기됐다**(§14-5 5a) —
// 루프·리포트·CLI가 전부 이 파일의 판정을 읽는다.
//
// **여기 없는 것**: rubric §8.2(신뢰도 수치)·§15.4 리포트 문구·§8.1 인용
// 무결성 게이트·§7.7 심사점 A의 수행. 심사점 A와 계층 A 완주, 회수 의무,
// 감별 소진은 **입력 플래그**로 받는다 — 그 판정 주체가 이 단계에 없기
// 때문이고, 전부 fail-closed 기본값(false = 미충족)이다.
package ledger

import (
	"fmt"
	"sort"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// ── 공개 계약의 잔존 타입 (구 status.go에서 이관, §14-5 5a) ──────
//
// 구 판정 표면(ComputeStatus·6축 의무·Rank·IsInferior)은 폐기됐다 — §8
// 게이트가 기능을 계승했고 그 계승표는 §8 본문에 있다. 아래 셋만 남는 이유는
// **판정이 아니라 계약**이기 때문이다: 리포트가 내보내는 enum(Status),
// 왜 멈췄는가의 typed 기록(StopReason), 그리고 그 둘을 담아 장부에 박히는
// 종료 선언(Decision — loop가 DecisionV2에서 사영한다).

// Status는 종료 시 리포트에 실리는 조사 결과 등급이다(RcaResult.status).
type Status string

const (
	StatusConfirmed    Status = "confirmed"
	StatusProvisional  Status = "provisional"
	StatusInsufficient Status = "insufficient"
)

// StopReason은 typed 종료 사유다(재설계 ADR D7 "멈춤 ≠ 확정", 평가 스펙
// §3.4 게이트 d3의 입력). 판정 결과(왜 이 status인가)와 별개로 "왜 조사를
// 멈췄는가"를 기록한다.
type StopReason string

const (
	// StopCauseSufficient — 원인 충분: 확정 문턱을 통과해 멈춤.
	StopCauseSufficient StopReason = "cause_sufficient"
	// StopBudgetExhausted — 예산 소진으로 강제 종료.
	StopBudgetExhausted StopReason = "budget_exhausted"
	// StopNoNewInformation — 수확 체감: 최근 조회가 새 근거를 못 붙임.
	StopNoNewInformation StopReason = "no_new_information"
	// StopMeansExhausted — 조사 수단 소진(감별 소진·미지원 도메인 포함).
	StopMeansExhausted StopReason = "means_exhausted"
	// StopBelowThreshold — 조사는 끝냈으나 확정 문턱 미달(경쟁 미해소 등).
	StopBelowThreshold StopReason = "completed_below_threshold"
)

// Decision은 종료 선언의 내용이다 — **판정 본체가 아니다**(본체는 DecisionV2).
// loop/loop.go의 projectDecision이 DecisionV2에서 이 값을 사영하고,
// loop_terminated 문장과 리포트 조립이 그것을 읽는다.
type Decision struct {
	Terminate     bool
	Status        Status     // Terminate일 때만 유효
	AdoptedID     string     // 채택 가설 (insufficient면 빈 값)
	InferiorIDs   []string   // 명백 열세로 접힌 가설 → alternatives
	UnresolvedIDs []string   // 배제도 열세도 아닌 생존 경쟁자 → alternatives
	Reason        string     // 판정 사유 — 궤적 로그용
	StopReason    StopReason // typed 종료 사유 (Terminate일 때만 유효)
	// Guards — §8 미충족 요건 이름(DecisionV2.MissingRequirements의 사영,
	// 5c). UIDiagnosis.Guards의 새 원천이다 — 구 원천(의무 6축)은 5a에서
	// 폐기됐다.
	Guards []string
}

// InternalStatus는 §8의 내부 명칭이다. 공개 계약(RcaResult·status.go)의
// enum은 confirmed|provisional|insufficient이고 **probable의 직렬화는
// provisional**이다(§8 "공개 계약 매핑" — 계약 파괴 금지).
type InternalStatus string

const (
	IntConfirmed    InternalStatus = "confirmed"
	IntProbable     InternalStatus = "probable"
	IntInsufficient InternalStatus = "insufficient"
	IntRefuted      InternalStatus = "refuted"
)

// Public은 공개 enum으로의 사상이다.
func (s InternalStatus) Public() Status {
	switch s {
	case IntConfirmed:
		return StatusConfirmed
	case IntProbable:
		return StatusProvisional
	}
	return StatusInsufficient
}

// CompetitorState는 경쟁 가설의 3치다(§8 경쟁 서열).
// **결손·inconclusive·감별 수단 부재는 unresolved다** — inferior로 접지
// 않는다. "안 본 것"과 "봤는데 진 것"의 구분이 그 게이트의 전부다.
type CompetitorState string

const (
	CompRefuted    CompetitorState = "refuted"
	CompInferior   CompetitorState = "inferior"
	CompUnresolved CompetitorState = "unresolved"
)

// HypothesisV2는 판정에 필요한 가설 하나의 projection이다.
type HypothesisV2 struct {
	ID         string
	Identity   HypoIdentity
	CreatedSeq int

	// Judgments — 이 가설 술어들의 판정(JudgePredicate 산출). 무효 술어
	// (Undecidable·AuditedOut)는 호출자가 빼고 넘기거나, 아래 Tally가
	// 딱지로 거른다.
	Judgments []PredJudgment
	// Preds — 딱지 판정용 원본. Judgments와 PredID로 짝짓는다.
	Preds []SignalPred
	Chain []ChainClaim

	// NewObservationEIDs — **선언 이후 실행이 낳은 레코드**의 EID 집합
	// (probe_executed.produced_eids, §7.1). §8 "신규 관측 ≥1"의 정본
	// 입력이다 — C-9 대조: "진전"을 verdict 발생으로 세면 이미 supported인
	// 술어를 재보고하는 것만으로 요건이 충족된다. 실행이 실제로 낳은
	// 레코드에 결박해야 재보고가 셈에서 빠진다.
	NewObservationEIDs map[string]bool

	// Temporal — §10 판정 결과(temporal_judged 이벤트).
	Temporal []TemporalJudged

	// ── 외부 판정 입력(§14-2·§14-5 몫). 전부 false가 fail-closed 기본값.
	// AuditAPassed — §7.7 심사점 A 전 구간 통과.
	AuditAPassed bool
	// TopologyAligned — §10 전파 일관성의 위상 절반(부록 A-3 엄격 유지, 5b).
	// 생산자는 loop(alignTopology — 호출 그래프는 장부 밖 seed 박제라 여기서
	// 판정할 수 없다). 불부합·무경로·판정 불능 전부 false = confirmed 차단이며,
	// 미충족 목록의 topology_direction 표기가 §13 계측(단독 차단 계수)의
	// 원천이다.
	TopologyAligned bool
	// RecoveryDone — §8 축약·절단 회수 의무 이행(C-8, 4차 A-11).
	RecoveryDone bool
	// DiscriminationExhausted — 식별력 있는 잔여 probe 없음(inferior ⑤).
	DiscriminationExhausted bool
}

// Tally는 가설 하나의 기계 계수다 — §8 요건이 읽는 모든 수는 여기서 나온다.
type Tally struct {
	// QualifiedSupports — 자격 지지 수. **pre_satisfied는 빠진다**(C-4·1b
	// 인계 ②). Qualified 등급을 받았어도 회고 술어는 무상 충족의 통로이므로
	// confirmed 계수에 넣지 않는다 — probable의 "passed ≥1"에는 든다.
	QualifiedSupports int
	// PreSatisfiedSupports — 위에서 뺀 회고 지지 수(§15.4 표기용).
	PreSatisfiedSupports int
	WeakSupports         int
	Invalidated          int
	Inconclusive         int

	NecessaryTotal      int
	NecessaryDecided    int // 실제 판정됨(supported|invalidated)
	NecessaryQualified  int // 자격 지지로 해소됨
	NecessaryUnresolved int // inconclusive로 남음

	// DiversityClasses — §8 "서로 다른 Aspect ≥2"의 축. **Aspect가 아니라
	// FindingClass(사상표 행 = 도구×class)를 쓴다** — C-5 대조 참조.
	DiversityClasses []string
	Lineages         []string
	// NewObservations — 자격 지지 중 신규 관측에 결박된 수.
	NewObservations int
	// SupportEntityKeys — 말단 게이트 대조용.
	SupportEntityKeys []string
}

// PassedAny는 probable의 "passed ≥1(weak 포함)"이다.
func (t Tally) PassedAny() int {
	return t.QualifiedSupports + t.WeakSupports + t.PreSatisfiedSupports
}

// Count는 §8 계수를 돈다.
//
// **무효 술어는 세지 않는다**(§6 기계 딱지): Undecidable은 등록 반려 사유,
// AuditedOut은 심사점 R 탈락으로 "셈·정렬 제외"다.
func (h HypothesisV2) Count() Tally {
	tagOf := map[string]SignalPred{}
	for _, p := range h.Preds {
		tagOf[p.PredID] = p
	}
	var t Tally
	for _, j := range h.Judgments {
		if p, ok := tagOf[j.PredID]; ok && (p.Undecidable || p.AuditedOut) {
			continue
		}
		necessary := j.Role == RoleNecessary
		if necessary {
			t.NecessaryTotal++
			if j.Decided() {
				t.NecessaryDecided++
			} else {
				t.NecessaryUnresolved++
			}
		}
		switch j.Verdict {
		case VerdictSupportedQualified:
			if j.PreSatisfied {
				t.PreSatisfiedSupports++
				break
			}
			t.QualifiedSupports++
			if necessary {
				t.NecessaryQualified++
			}
			t.DiversityClasses = append(t.DiversityClasses, j.FindingClasses...)
			t.Lineages = append(t.Lineages, j.LineageIDs...)
			t.SupportEntityKeys = append(t.SupportEntityKeys, j.EntityKeys...)
			if h.hasNewObservation(j) {
				t.NewObservations++
			}
		case VerdictSupportedWeak:
			t.WeakSupports++
		case VerdictInvalidated:
			t.Invalidated++
		default:
			t.Inconclusive++
		}
	}
	t.DiversityClasses = uniq(t.DiversityClasses)
	t.Lineages = uniq(t.Lineages)
	t.SupportEntityKeys = uniq(t.SupportEntityKeys)
	return t
}

// hasNewObservation은 그 지지가 **선언 이후 실행이 낳은** 레코드에
// 결박됐는지다(§8 "신규 관측 ≥1", C-9).
func (h HypothesisV2) hasNewObservation(j PredJudgment) bool {
	if j.PreSatisfied || len(h.NewObservationEIDs) == 0 {
		return false
	}
	for _, eid := range j.EIDs {
		if h.NewObservationEIDs[eid] {
			return true
		}
	}
	return false
}

// Refuted는 반증된 가설인가다 — necessary 술어가 자격 있는 반증으로
// 불일치했다(진리표 행 5).
func (h HypothesisV2) Refuted() bool { return h.Count().Invalidated > 0 }

// TemporalSatisfied는 §8 시간 요건이다.
//
//	precedes  — "선행" 판정 ≥1이 supports.
//	coincides — 구간 겹침이 **확인 판정**이다(§10 TemporalClaim별 소비 —
//	            "선행 지지 ≥1"이 이 가설에선 "겹침 확인 ≥1"로 대체된다).
//	unknown   — 시간 근거 없이 probable까지. confirmed 불가.
//
// 판정은 **현행 앵커에 결박**된다(5b): 증상 앵커(§10 B)는 재조회가 더 이른
// onset을 찾으면 이동하고, 구 앵커 대비 "선행"이 신 앵커 대비로는 성립하지
// 않을 수 있다(앵커 = onset_lo 최소 = 선행이 가장 어려운 기준점). 이벤트는
// append-only이므로 마지막 판정의 SymptomEID가 현행 앵커다 — 그 앵커의
// 판정만 센다(fail-closed).
func (h HypothesisV2) TemporalSatisfied() bool {
	want := TemporalPrecedesVerdict
	switch h.Identity.TemporalClaim {
	case TemporalPrecedes:
		want = TemporalPrecedesVerdict
	case TemporalCoincides:
		want = TemporalOverlapVerdict
	default:
		return false
	}
	if len(h.Temporal) == 0 {
		return false
	}
	anchor := h.Temporal[len(h.Temporal)-1].SymptomEID
	for _, tj := range h.Temporal {
		if tj.SymptomEID == anchor && tj.Verdict == want {
			return true
		}
	}
	return false
}

// TerminalDepthSatisfied는 §8 말단 깊이 게이트다 — 사슬 말단(terminal_entity)의
// EntityKey가 자격 지지 레코드의 EntityKey와 **typed 동치**여야 한다
// (§7.3 일반 규칙. 메커니즘별 적정 깊이는 §7.7-② 의미 심사 몫).
func (h HypothesisV2) TerminalDepthSatisfied(t Tally) bool {
	have := map[string]bool{}
	for _, k := range t.SupportEntityKeys {
		have[k] = true
	}
	for _, c := range h.Chain {
		if c.Kind != ChainTerminalEntity {
			continue
		}
		if have[CanonEntityKey(c.EntityKey)] {
			return true
		}
	}
	return false
}

// ── 가설 단위 요건 ──────────────────────────────────────────────

// 요건 이름 — 미충족 목록이 리포트(§15.4)와 감사에 그대로 실린다.
const (
	ReqQualifiedSupports = "qualified_supports_ge2"
	ReqDiversity         = "diversity_ge2"
	ReqLineage           = "lineage_ge2"
	ReqNewObservation    = "new_observation_ge1"
	ReqTemporal          = "temporal_support"
	ReqTopologyDirection = "topology_direction"
	ReqTerminalDepth     = "terminal_depth"
	ReqNecessaryResolved = "necessary_resolved"
	ReqAuditA            = "audit_a_passed"
	ReqRecovery          = "truncation_recovery"
	ReqLayerAComplete    = "layer_a_complete"
	ReqNoUnresolvedComp  = "no_unresolved_competitor"
	ReqNotRefuted        = "not_refuted"
)

// ConfirmedRequirements는 채택 후보의 confirmed 요건을 전수 판정한다.
// 반환은 (충족, 미충족 요건 목록)이다 — bool 하나로 접으면 "왜 probable
// 인가"를 리포트가 말할 수 없다.
//
// # C-1: necessary 술어 미해결이어도 confirmed인가 — 스펙 원문 판정
//
// §8 confirmed 항목 원문에는 necessary 술어의 **해소·일치** 조건이 없다.
// 있는 것은 6축 계승 매핑의 "SelfRefutation→반증형(necessary) 술어 ≥1
// (§6.2-5)"인데, 이는 **등록 시점에 존재해야 한다**는 요건이지 실행됐다는
// 요건이 아니다. 반면 같은 절의 inferior 진리표 ②는 경쟁 가설에게
// "necessary 술어 ≥1회 **실제 판정됨**(미실측이면 자격 없음)"을 요구한다.
// 즉 스펙은 **경쟁을 접는 데는 necessary 실측을 요구하면서 채택에는 요구하지
// 않는다** — C-1의 지적은 원문상 유효하며 반박할 근거가 없다.
//
// 그래서 checklist의 최소선을 그대로 게이트에 넣는다: **"등록 후 실행된
// qualified necessary ≥1 그리고 unresolved necessary = 0"**. C-2(반대편)와
// 충돌하지 않는 이유는 미충족이 반려가 아니라 **probable 강등**이기 때문이다
// — 미실측 necessary가 전 가설을 죽이는 데드락은 생기지 않는다.
// 스펙 §8 confirmed 항목에 이 조항을 추가하는 것을 §14-5에 넘긴다.
func (h HypothesisV2) ConfirmedRequirements(t Tally, comp map[string]CompetitorState, layerAComplete bool) (bool, []string) {
	var missing []string
	need := func(ok bool, name string) {
		if !ok {
			missing = append(missing, name)
		}
	}
	need(!h.Refuted(), ReqNotRefuted)
	need(t.QualifiedSupports >= 2, ReqQualifiedSupports)
	need(len(t.DiversityClasses) >= 2, ReqDiversity)
	need(len(t.Lineages) >= 2, ReqLineage)
	need(t.NewObservations >= 1, ReqNewObservation)
	need(h.TemporalSatisfied(), ReqTemporal)
	need(h.TopologyAligned, ReqTopologyDirection)
	need(h.TerminalDepthSatisfied(t), ReqTerminalDepth)
	need(t.NecessaryQualified >= 1 && t.NecessaryUnresolved == 0, ReqNecessaryResolved)
	need(h.AuditAPassed, ReqAuditA)
	need(h.RecoveryDone, ReqRecovery)
	need(layerAComplete, ReqLayerAComplete)
	unresolved := 0
	for _, st := range comp {
		if st == CompUnresolved {
			unresolved++
		}
	}
	need(unresolved == 0, ReqNoUnresolvedComp)
	return len(missing) == 0, missing
}

// ── 경쟁 서열 (§8) ──────────────────────────────────────────────

// IsInferiorV2는 §8 inferior 진리표다 — **전부 충족**해야 inferior다.
// 하나라도 어긋나면 unresolved이며, 그것이 confirmed를 막는다.
//
// 구 IsInferior(폐기된 status.go)의 "PassedSupports=0 → inferior"를 승계하지
// 않은 이유가 4차 A-8이다: 예산 소진 순간 경쟁 전원이 inferior가 되어
// 검증 없는 오답 confirmed가 난다.
func IsInferiorV2(c HypothesisV2, ct Tally, at Tally) bool {
	if c.Refuted() {
		return false // refuted는 별도 상태다
	}
	// ② necessary 술어가 실제 판정됐는가 — 미실측이면 자격 없음.
	if ct.NecessaryDecided < 1 {
		return false
	}
	// ③ qualified 지지 0.
	if ct.QualifiedSupports > 0 {
		return false
	}
	// ④ 채택 후보 대비 명시적 열세 — qualified contradiction 보유(반증
	//    자격을 갖춘 불일치가 실제로 있었다) 또는 지지 수 열세.
	explicit := ct.Invalidated > 0 || at.QualifiedSupports > ct.QualifiedSupports
	if !explicit {
		return false
	}
	// ⑤ 식별력 있는 잔여 probe 없음(감별 소진).
	return c.DiscriminationExhausted
}

// RankV2는 §8 Rank 총순서다: qualified 지지 수 → weak 지지 수 → 미반증
// necessary 비율 → 시간·말단 요건 충족 수 → 등록순.
//
// pre-A 후보 선정(§7.7)과 probable의 "경쟁 대비 우위"가 같은 순서를 본다.
func RankV2(hs []HypothesisV2) []HypothesisV2 {
	type keyed struct {
		h HypothesisV2
		t Tally
	}
	ks := make([]keyed, 0, len(hs))
	for _, h := range hs {
		if h.Refuted() {
			continue
		}
		ks = append(ks, keyed{h, h.Count()})
	}
	ratio := func(t Tally) float64 {
		if t.NecessaryTotal == 0 {
			return 0
		}
		return float64(t.NecessaryTotal-t.Invalidated) / float64(t.NecessaryTotal)
	}
	extra := func(k keyed) int {
		n := 0
		if k.h.TemporalSatisfied() {
			n++
		}
		if k.h.TerminalDepthSatisfied(k.t) {
			n++
		}
		return n
	}
	sort.SliceStable(ks, func(i, j int) bool {
		a, b := ks[i], ks[j]
		if a.t.QualifiedSupports != b.t.QualifiedSupports {
			return a.t.QualifiedSupports > b.t.QualifiedSupports
		}
		if a.t.WeakSupports != b.t.WeakSupports {
			return a.t.WeakSupports > b.t.WeakSupports
		}
		if ra, rb := ratio(a.t), ratio(b.t); ra != rb {
			return ra > rb
		}
		if ea, eb := extra(a), extra(b); ea != eb {
			return ea > eb
		}
		return a.h.CreatedSeq < b.h.CreatedSeq
	})
	out := make([]HypothesisV2, 0, len(ks))
	for _, k := range ks {
		out = append(out, k.h)
	}
	return out
}

// ── 판정 ────────────────────────────────────────────────────────

// ViewV2는 판정이 읽는 스냅샷이다.
type ViewV2 struct {
	Hypotheses []HypothesisV2
	// LayerAComplete — 계층 A 미조사 증상 멤버 없음(§4 대상 단위).
	// **§14-2 몫이라 여기서는 입력**이고 기본값 false는 fail-closed다.
	LayerAComplete bool
}

// DecisionV2는 §8 판정의 결과다.
type DecisionV2 struct {
	Internal InternalStatus
	Status   Status // 공개 enum 사상
	AdoptedID string
	// Competitors — 채택 외 가설의 3치 상태.
	Competitors map[string]CompetitorState
	// MissingRequirements — 채택 후보가 confirmed에 못 간 사유(요건 이름).
	MissingRequirements []string
	Tally               Tally
	Reason              string
}

// RankedHypo는 최종 순위 1건의 감사 기록이다(구 stages.ranked 승계 —
// 결정론 채점의 원천이라 리포트 계약 밖에서도 남긴다).
type RankedHypo struct {
	ID                string `json:"id"`
	QualifiedSupports int    `json:"qualified_supports"`
	WeakSupports      int    `json:"weak_supports"`
	PreSatisfied      int    `json:"pre_satisfied_supports"`
}

// RankedOf는 순위 목록을 감사 기록으로 접는다.
func RankedOf(hs []HypothesisV2) []RankedHypo {
	out := make([]RankedHypo, 0, len(hs))
	for _, h := range hs {
		t := h.Count()
		out = append(out, RankedHypo{ID: h.ID, QualifiedSupports: t.QualifiedSupports,
			WeakSupports: t.WeakSupports, PreSatisfied: t.PreSatisfiedSupports})
	}
	return out
}

// ProjectStatus는 §8 판정 게이트의 기계 계산이다.
//
// **채택 후보 선정과 confirmed 판정은 다른 일이다**(§8): 순위 1위는 어떤
// 장부에서도 정해지지만, 그것이 confirmed를 뜻하지는 않는다.
func ProjectStatus(v ViewV2) DecisionV2 {
	ranked := RankV2(v.Hypotheses)
	d := DecisionV2{Competitors: map[string]CompetitorState{}}
	for _, h := range v.Hypotheses {
		if h.Refuted() {
			d.Competitors[h.ID] = CompRefuted
		}
	}
	if len(ranked) == 0 {
		d.Internal, d.Status = IntInsufficient, StatusInsufficient
		d.Reason = "생존 가설 없음"
		return d
	}
	adopted := ranked[0]
	at := adopted.Count()
	d.AdoptedID, d.Tally = adopted.ID, at

	for _, c := range ranked[1:] {
		ct := c.Count()
		if IsInferiorV2(c, ct, at) {
			d.Competitors[c.ID] = CompInferior
			continue
		}
		d.Competitors[c.ID] = CompUnresolved
	}

	ok, missing := adopted.ConfirmedRequirements(at, d.Competitors, v.LayerAComplete)
	d.MissingRequirements = missing
	switch {
	case ok:
		d.Internal, d.Reason = IntConfirmed, "§8 confirmed 요건 전수 충족"
	case at.PassedAny() >= 1:
		// probable — passed ≥1(weak 포함) · 경쟁 대비 우위(Rank 1위).
		// 시간 "불명"까지 허용하고 A는 돌지 않는다(§7.7 발동 조건).
		d.Internal = IntProbable
		d.Reason = fmt.Sprintf("confirmed 미충족 %d건: %v", len(missing), missing)
	default:
		// insufficient — 전 가설 inconclusive·widening 소진.
		d.Internal = IntInsufficient
		d.Reason = "지지 0건(weak 포함)"
	}
	d.Status = d.Internal.Public()
	return d
}

// ── C-9: 바퀴 간 진전의 계산 가능성 ─────────────────────────────

// LedgerSnapshot은 명제 장부의 한 시점 사영이다 — 관측 키 → 근거 EID 목록.
//
// C-9 대조의 재료다: "진전"을 verdict 발생으로 세면 이미 supported인 술어를
// 차례로 재보고하는 것만으로 매 바퀴 supported가 생겨 W3(progressive
// widening)가 영원히 발동하지 않는다. 진전은 **관측 키의 상태 변화**여야
// 하며, 그 delta가 계산 가능함을 이 두 함수가 실물로 보인다.
// **무진전 판정 규칙 본체(§7.4 발동 조건)는 §14-5 몫**이다 — 여기 있는 것은
// 그 규칙이 읽을 수 있는 형태가 실재한다는 증명까지다.
func LedgerSnapshot(l *evidence.Ledger) map[evidence.ObservationKey][]string {
	out := map[evidence.ObservationKey][]string{}
	if l == nil {
		return out
	}
	for _, p := range l.Propositions() {
		out[p.Key] = append([]string(nil), p.EIDs...)
	}
	return out
}

// ChangedKeys는 두 사영 사이에 상태가 바뀐 관측 키다(신규 키 + 근거가 바뀐
// 키). 같은 결론의 재보고는 근거 EID가 그대로이므로 여기 들지 않는다.
func ChangedKeys(prev, cur map[evidence.ObservationKey][]string) []evidence.ObservationKey {
	var out []evidence.ObservationKey
	for k, now := range cur {
		before, had := prev[k]
		if !had || !sameStrings(before, now) {
			out = append(out, k)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].String() < out[j].String() })
	return out
}

func sameStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
