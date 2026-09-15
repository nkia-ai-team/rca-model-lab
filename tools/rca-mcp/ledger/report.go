// 리포트 조립([6]) — 종료된 수첩에서 출력 계약(RcaResult/UIReport)을
// 추출한다. 여기 있는 출력 타입은 lucida-next 계약의 거울이다(정본:
// docs/ref-rca-io-contract.md — 이 프로젝트는 계약을 정의하지 않고
// 추적만 한다). 추출은 전부 결정론이다 — LLM이 지어내는 필드가 없다.
//
// 채우는 범위(걷는 뼈대):
//   - 장부가 원천인 필드(status·cause·alternatives·evidence·missing·
//     가설 카드·data_coverage.gaps/ruled_out)는 여기서 채운다.
//   - seed가 원천인 필드(symptom·problem·alarm)와 산문 필드(headline·
//     summary 등 사람용 문장)는 빈 값으로 둔다 — 파이프라인 상위([6]의
//     작문 단계)의 몫이다. 구조가 먼저, 문장은 나중.
package ledger

import (
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// ── 출력 계약 거울 ① RcaResult (drawer 요약, camelCase) ─────────

type RcaResult struct {
	Status string `json:"status"`
	// StopReason은 계약 확장 필드다(lucida-next 원 계약엔 없음 — 평가
	// 스펙 §3.4 d3·ADR D7 요구, §10 이행). 왜 조사를 멈췄는가의 typed 기록.
	StopReason      string           `json:"stopReason,omitempty"`
	Headline        string           `json:"headline"`
	Symptom         RcaSymptom       `json:"symptom"`
	Problem         RcaProblem       `json:"problem"`
	Cause           RcaCause         `json:"cause"`
	CausalChain     []RcaChainStep   `json:"causalChain"`
	Evidence        []RcaEvidence    `json:"evidence"`
	Alternatives    []RcaAlternative `json:"alternatives"`
	MissingEvidence []string         `json:"missingEvidence"`
}

type RcaSymptom struct {
	Text         string     `json:"text"`
	Metric       string     `json:"metric"`
	Observed     *float64   `json:"observed"`
	Baseline     *float64   `json:"baseline"`
	At           *time.Time `json:"at"`
	MainEventRef string     `json:"mainEventRef"`
}

type RcaProblem struct {
	Text     string    `json:"text"`
	Severity string    `json:"severity"`
	Impact   RcaImpact `json:"impact"`
}

type RcaImpact struct {
	Services  []string `json:"services"`
	TargetIDs []string `json:"targetIds"`
}

type RcaCause struct {
	Text       string        `json:"text"`
	TargetID   string        `json:"targetId"`
	TargetName string        `json:"targetName"`
	Metric     string        `json:"metric"`
	Confidence float64       `json:"confidence"`
	ChangeRef  *RcaChangeRef `json:"changeRef,omitempty"`
}

type RcaChangeRef struct {
	Kind string    `json:"kind"`
	At   time.Time `json:"at"`
}

type RcaChainStep struct {
	Entity string `json:"entity"`
	Metric string `json:"metric"`
	Note   string `json:"note"`
}

type RcaEvidence struct {
	Kind  string `json:"kind"`
	Ref   string `json:"ref"`
	Label string `json:"label"`
}

type RcaAlternative struct {
	Text       string  `json:"text"`
	Confidence float64 `json:"confidence"`
	// TargetID는 계약 확장 필드다(평가 스펙 §3.1 — 채점은 공개 리포트
	// 계약만 읽는다. 내부 stage 덤프 채점 금지의 전제).
	TargetID string `json:"targetId,omitempty"`
}

// ── 출력 계약 거울 ② UIReport (상세 보고서, snake_case) ─────────

type UIReport struct {
	RequestedAt       time.Time      `json:"requested_at"`
	GeneratedAt       time.Time      `json:"generated_at"`
	Alarm             UIAlarm        `json:"alarm"`
	Diagnosis         UIDiagnosis    `json:"diagnosis"`
	Hypotheses        []UIHypothesis `json:"hypotheses"`
	ConclusionSummary string         `json:"conclusion_summary"`
	NextSteps         []UINextStep   `json:"next_steps"`
	SimilarCases      []UISimilar    `json:"similar_cases"`
	DataCoverage      UIDataCoverage `json:"data_coverage"`
	// ConfidenceBreakdown·ConfidenceContract — §8.2-3(계약 확장, 기존 float
	// 소비자 비파괴). 산문 수치 표기 금지(§8.1)와 양립하는 유일한 공식
	// 전달 경로다. breakdown은 채택 가설이 있을 때만 실린다.
	ConfidenceBreakdown *ConfidenceBreakdown `json:"confidence_breakdown,omitempty"`
	ConfidenceContract  string               `json:"confidence_contract"`

	// §15.4 표기층(6d) — 전제·한계 블록(전 등급 의무), 등급 고정 문구,
	// 푸터. 전부 기계 조립·하네스 상수.
	Premises     ReportPremises    `json:"premises"`
	Limitations  ReportLimitations `json:"limitations"`
	GradeMeaning string            `json:"grade_meaning"`
	Footer       string            `json:"footer"`
}

type UIAlarm struct {
	ID         string     `json:"id"`
	Name       string     `json:"name"`
	Severity   string     `json:"severity"`
	Target     string     `json:"target"`
	OccurredAt *time.Time `json:"occurred_at"`
}

type UIDiagnosis struct {
	Verdict    string   `json:"verdict"` // CONCLUSIVE | INCONCLUSIVE | CONTRADICTED
	Confidence float64  `json:"confidence"`
	Guards     []string `json:"guards"`
	Summary    string   `json:"summary"`
}

type UIHypothesis struct {
	Rank        int          `json:"rank"`
	IsAdopted   bool         `json:"is_adopted"`
	Title       string       `json:"title"`
	Description string       `json:"description"`
	Grounds     []string     `json:"grounds"`
	Culprits    []UICulprit  `json:"culprits"`
	Evaluation  UIEvaluation `json:"evaluation"`
}

type UICulprit struct {
	Label  string `json:"label"`
	Value  string `json:"value"`
	Detail string `json:"detail"`
}

type UIEvaluation struct {
	Verdict     string   `json:"verdict"` // PROVEN | WEAKENED | CONTRADICTED
	Confidence  float64  `json:"confidence"`
	Supports    []string `json:"supports"`
	Contradicts []string `json:"contradicts"`
	Missing     []string `json:"missing"`
	Guards      []string `json:"guards"`
}

type UINextStep struct {
	Kind           string `json:"kind"` // verify | mitigate | monitor
	Priority       int    `json:"priority"`
	Title          string `json:"title"`
	TargetResource string `json:"target_resource"`
}

type UISimilar struct {
	ReportID          string  `json:"report_id"`
	Similarity        float64 `json:"similarity"`
	AdoptedCause      string  `json:"adopted_cause"`
	ResolutionSummary string  `json:"resolution_summary"`
}

type UIDataCoverage struct {
	Targets    int      `json:"targets"`
	Metrics    int      `json:"metrics"`
	Events     int      `json:"events"`
	Logs       int      `json:"logs"`
	Traces     int      `json:"traces"`
	Changes    int      `json:"changes"`
	Dimensions int      `json:"dimensions"`
	Gaps       []string `json:"gaps"`
	RuledOut   []string `json:"ruled_out"`
}

// ── 조립 ────────────────────────────────────────────────────────

// ReportContext는 [6]이 next_steps·missing을 **기계 조립**하는 원천이다
// (§14-4 4c). 종전 원천은 ProbeSpec.Purpose 자유문이었고, 그 필드가 폐기되면서
// "이어서 볼 것"의 정의도 바뀌었다:
//
//	미판정 명제        — 활성 가설의 술어 중 진리표가 아직 못 가른 것(§6.0·§7.2-3)
//	미조사 필수 관점   — required_views_extended 집합에서 관측이 없는 키(§4)
//
// 빈 값이어도 조립은 돈다 — 그때는 두 원천이 각각 공집합이고, 리포트는
// 지어낸 문장 대신 비어 있는 목록을 낸다(fail-quiet가 아니라 fail-empty).
type ReportContext struct {
	// Propositions — 명제 장부(§6.0). nil이면 술어는 전부 미판정으로 읽힌다
	// (JudgePredicate의 nil 계약 — inconclusive).
	Propositions *evidence.Ledger
	// Index — 필수 관점의 관측 유무 판정과 자격 게이트의 재료(§5.7).
	Index *evidence.Index
	// Gate — 술어 판정 문맥(요구 창·기준 시각). 호출자가 [5]와 같은 값을 준다.
	Gate GateContext

	// §15.4 전제 블록 재료(6d) — 배포 파라미터·재생 표기·기준선 창.
	// 호출자(pipeline←cmd)가 채운다. 빈 값이면 unknown 쪽으로 정직하게
	// 조립된다(fail-closed).
	ClockSkew      evidence.ClockSkew
	ReplaySource   string
	BaselineWindow string

	// IndexTrunc — §5.3 payload 축약 누적기(§14-7 계측). nil이면 한계
	// 블록은 not_measured로 정직하게 남는다(fail-closed).
	IndexTrunc *IndexTruncStats
}

// AssembleReport는 종료된 수첩에서 두 출력을 추출하고, 감사용
// report_assembled 문장을 수첩에 남긴다. 종료 전 수첩은 거부한다.
func AssembleReport(l *Ledger, rc ReportContext, requestedAt, generatedAt time.Time) (RcaResult, UIReport, error) {
	if !l.terminated {
		return RcaResult{}, UIReport{}, errors.New("종료되지 않은 수첩에서는 리포트를 추출하지 않는다")
	}
	dec, ok := lastDecision(l)
	if !ok {
		return RcaResult{}, UIReport{}, errors.New("종료 선언(loop_terminated)이 수첩에 없음")
	}
	asOf := len(l.events)

	gaps := newGapSet(l, rc)
	missing := missingEvidence(l, gaps)
	// 순위는 §8 게이트와 **같은 사영**에서 나온다(§14-5 5a — 구 Rank+Snapshot
	// 표면 폐기). 리포트가 따로 세면 루프가 채택한 순서와 갈린다.
	ranked := RankV2(ProjectV2Terminal(l, rc.Propositions, rc.Gate).Hypotheses)
	unex, requiredTotal := requiredViewTally(l, rc.Index)
	conf := confidenceTable(dec, ranked, len(unex), requiredTotal)
	result := assembleResult(l, dec, ranked, missing, conf)
	ui := assembleUI(l, dec, ranked, gaps, conf, requestedAt, generatedAt)
	// §15.4 전제·한계 블록(6d) — rc 재료의 기계 조립. 전 등급 의무.
	ui.Premises = assemblePremises(rc)
	ui.Limitations = assembleLimitations(l, rc)

	if _, err := l.Append(ActorPipeline, generatedAt, ReportAssembled{AsOfSeq: asOf}); err != nil {
		return RcaResult{}, UIReport{}, err
	}
	return result, ui, nil
}

func lastDecision(l *Ledger) (Decision, bool) {
	for i := len(l.events) - 1; i >= 0; i-- {
		if p, ok := l.events[i].Payload.(LoopTerminated); ok {
			return p.Decision, true
		}
	}
	return Decision{}, false
}

// reportConfidence는 이 리포트의 신뢰도 표다 — 채택은 §8.2 rubric,
// 미채택은 상수(unresolved 0.30 / inferior 0.15), 반증·무채택은 0
// (byID 미등재 = zero value). 임시 산식 passed/(passed+1)의 자리를
// rubric v1이 계승했다(§14-5 5c — 5a의 confidenceUnimplemented 소멸).
type reportConfidence struct {
	Adopted ConfidenceBreakdown
	byID    map[string]float64
}

func confidenceTable(dec Decision, ranked []HypothesisV2, unexamined, requiredTotal int) reportConfidence {
	rc := reportConfidence{byID: map[string]float64{}}
	inferior := map[string]bool{}
	for _, id := range dec.InferiorIDs {
		inferior[id] = true
	}
	for _, h := range ranked {
		switch {
		case dec.AdoptedID != "" && h.ID == dec.AdoptedID:
			st := IntProbable
			if dec.Status == StatusConfirmed {
				st = IntConfirmed
			}
			rc.Adopted = RubricConfidence(st, h, h.Count(), unexamined, requiredTotal)
			rc.byID[h.ID] = rc.Adopted.Value
		case inferior[h.ID]:
			rc.byID[h.ID] = ConfInferiorCompetitor
		default:
			rc.byID[h.ID] = ConfUnresolvedCompetitor
		}
	}
	return rc
}

func assembleResult(l *Ledger, dec Decision, ranked []HypothesisV2, missing []string, conf reportConfidence) RcaResult {
	r := RcaResult{
		Status:          string(dec.Status),
		StopReason:      string(dec.StopReason),
		MissingEvidence: missing,
	}

	// evidence — 심사 통과 링크의 근거 참조 전부(제안 순서, 근거 단위 중복 제거).
	seen := map[string]bool{}
	for _, evID := range l.evOrder {
		e := l.evidence[evID]
		for i := range e.Entry.Links {
			if e.Verdicts[i] != VerdictPassed || seen[evID] {
				continue
			}
			seen[evID] = true
			ref := evID
			if len(e.Entry.Refs) > 0 {
				ref = e.Entry.Refs[0]
			}
			r.Evidence = append(r.Evidence, RcaEvidence{Ref: ref, Label: e.Entry.Observation})
		}
	}

	if dec.AdoptedID != "" {
		h := l.hypos[dec.AdoptedID]
		r.Cause = RcaCause{
			Text:       h.Entry.MechanismText(),
			TargetID:   h.Entry.TargetID,
			Confidence: conf.Adopted.Value, // §8.2 rubric v1
		}
		// causalChain — 채택 가설의 사슬 구간 그대로. 구간을 지목한
		// 통과 관측이 있으면 함께 싣는다.
		for i, st := range h.Entry.Chain {
			step := RcaChainStep{Entity: st.Entity, Note: st.Effect}
			if obs := stepObservations(l, dec.AdoptedID, i); len(obs) > 0 {
				step.Note += " — 근거: " + strings.Join(obs, "; ")
			}
			r.CausalChain = append(r.CausalChain, step)
		}
	}

	// alternatives — 채택 제외 생존 가설 순위순 (열세 + 미해소).
	for _, h := range ranked {
		if h.ID == dec.AdoptedID {
			continue
		}
		rec := l.hypos[h.ID]
		r.Alternatives = append(r.Alternatives, RcaAlternative{
			Text:       rec.Entry.MechanismText(),
			Confidence: conf.byID[h.ID], // 미채택 상수(§8.2-2)
			TargetID:   rec.Entry.TargetID,
		})
	}
	return r
}

func assembleUI(l *Ledger, dec Decision, ranked []HypothesisV2, gaps gapSet, conf reportConfidence, requestedAt, generatedAt time.Time) UIReport {
	ui := UIReport{
		RequestedAt: requestedAt,
		GeneratedAt: generatedAt,
		Diagnosis: UIDiagnosis{
			Verdict: diagnosisVerdict(l, dec),
			// Guards — §8 미충족 요건 이름(5c 배선). 종전 원천(의무 6축)은
			// 5a에서 폐기됐고, 이 목록이 "왜 confirmed가 아닌가"의 기계
			// 원천이자 §13 계측(topology_direction 단독 차단 계수)의 표면이다.
			Guards: append([]string(nil), dec.Guards...),
		},
		DataCoverage: UIDataCoverage{
			Gaps:     probeGaps(l),
			RuledOut: ruledOut(l),
		},
	}
	// §8.2-3 — 수치 내역의 유일한 공식 전달 경로. contract는 항상 싣고
	// breakdown은 채택이 있을 때만 실린다.
	ui.ConfidenceContract = ConfidenceContract
	if dec.AdoptedID != "" {
		ui.Diagnosis.Confidence = conf.Adopted.Value
		b := conf.Adopted
		ui.ConfidenceBreakdown = &b
	}
	// §15.4 표기층(6d) — 등급 고정 문구·푸터(하네스 상수). 전제·한계
	// 블록은 rc가 필요해 AssembleReport가 채운다.
	ui.GradeMeaning = GradeMeaning(dec.Status)
	ui.Footer = ReportFooter

	// 가설 카드 — 생존 가설은 순위순, 반증 가설은 그 뒤 생성 순서.
	rank := 0
	for _, h := range ranked {
		rank++
		// PROVEN/WEAKENED의 입력은 §8 계수다 — 구 심사 통과 링크 수가 아니라
		// 진리표가 낸 지지(자격+weak+회고) 전량이다(§14-5 5a 교체).
		ui.Hypotheses = append(ui.Hypotheses,
			hypothesisCard(l, gaps, h.ID, rank, h.ID == dec.AdoptedID, h.Count().PassedAny(), conf.byID[h.ID]))
	}
	for _, id := range l.hypoOrder {
		if l.hypos[id].Lifecycle != LifeRefuted {
			continue
		}
		rank++
		// 반증 가설의 신뢰도는 0이다(§8.2-2).
		ui.Hypotheses = append(ui.Hypotheses, hypothesisCard(l, gaps, id, rank, false, 0, 0))
	}

	// next_steps — **기계 조립이다**(§14-4 4c). 대상 가설은 채택 가설,
	// 없으면 순위 1위다: insufficient로 끝난 run이야말로 "이어서 볼 것"이
	// 필요한데 채택만 보면 그때 목록이 통째로 빈다.
	focus := dec.AdoptedID
	if focus == "" {
		if len(ranked) > 0 {
			focus = ranked[0].ID
		}
	}
	for _, g := range gaps.undecided {
		if g.hypothesis != focus {
			continue
		}
		ui.NextSteps = append(ui.NextSteps, UINextStep{
			Kind: "verify", Priority: len(ui.NextSteps) + 1,
			Title: "미판정 명제 해소: " + g.text, TargetResource: g.targetID,
		})
	}
	for _, k := range gaps.unexamined {
		ui.NextSteps = append(ui.NextSteps, UINextStep{
			Kind: "verify", Priority: len(ui.NextSteps) + 1,
			Title: "필수 관점 미조사: " + string(k.Tool), TargetResource: k.TargetID,
		})
	}
	return ui
}

// ── next_steps·missing의 기계 원천 (§14-4 4c) ───────────────────

// predGap은 미판정 술어 하나다 — 진리표가 supported도 invalidated도 내지
// 못한 명제(§7.2-3 inconclusive)가 곧 "아직 확인 못 한 것"이다.
type predGap struct {
	hypothesis string
	targetID   string
	text       string
}

// gapSet은 두 기계 원천의 산출이다. 순서는 결정론이다 — 가설 생성 순서 ×
// 술어 선언 순서, 그리고 필수 관점은 이벤트 기록 순서.
type gapSet struct {
	undecided  []predGap
	unexamined []RequiredViewKey
}

func newGapSet(l *Ledger, rc ReportContext) gapSet {
	var gs gapSet
	for _, id := range l.hypoOrder {
		h := l.hypos[id]
		if h.Lifecycle == LifeRefuted {
			continue // 반증된 가설의 미판정 술어는 이어서 볼 것이 아니다
		}
		for _, p := range h.Entry.PredictedSignals {
			// 무효 딱지가 붙은 술어는 조사 대상이 아니다 — 심사가 이미
			// "판정할 수 없다"고 끝낸 것이라 미해소로 세면 영구 잔여가 된다.
			if p.Undecidable || p.AuditedOut {
				continue
			}
			if JudgePredicate(p, rc.Propositions, rc.Gate).Decided() {
				continue
			}
			gs.undecided = append(gs.undecided, predGap{
				hypothesis: id,
				targetID:   p.ObservationKey().TargetID,
				text: fmt.Sprintf("%s %s (%s, key=%s)",
					p.Expectation, p.Predicate, p.PredID, p.ObservationKey().String()),
			})
		}
	}
	gs.unexamined = unexaminedViews(l, rc.Index)
	return gs
}

// byHypothesis는 가설 카드의 missing이다.
func (g gapSet) byHypothesis(id string) []string {
	var out []string
	for _, p := range g.undecided {
		if p.hypothesis == id {
			out = append(out, "미판정 명제: "+p.text)
		}
	}
	return out
}

// lines는 missingEvidence 몫의 전량이다(가설 구분 없이).
func (g gapSet) lines() []string {
	var out []string
	for _, p := range g.undecided {
		out = append(out, fmt.Sprintf("미판정 명제(%s): %s", p.hypothesis, p.text))
	}
	for _, k := range g.unexamined {
		out = append(out, fmt.Sprintf("미조사 필수 관점: %s@%s", k.Tool, k.TargetID))
	}
	return out
}

// unexaminedViews — 필수 관점 집합(§4) 중 관측이 없는 키. 집합은 append-only
// 이므로 이벤트를 앞에서부터 훑어 모으고, not_applicable 딱지가 붙은 키는
// 분모에서 빠진다(C-12 — 제거가 아니라 딱지).
//
// "관측이 있다"의 판정은 index의 active 레코드에 같은 (대상, 도구[, metric])
// 조합이 있는가다 — [3]의 ScreenResult.Unobserved와 같은 기준이되, 리포트는
// 루프가 추가로 남긴 관측까지 봐야 하므로 index를 직접 읽는다.
func unexaminedViews(l *Ledger, ix *evidence.Index) []RequiredViewKey {
	out, _ := requiredViewTally(l, ix)
	return out
}

// requiredViewTally는 §4 필수 관점의 (미조회 목록, 적용 분모)다 —
// §8.2-1 rubric의 미조사 항이 같은 분모를 쓴다(세 소비자 공유 계약).
func requiredViewTally(l *Ledger, ix *evidence.Index) ([]RequiredViewKey, int) {
	var keys []RequiredViewKey
	seen := map[RequiredViewKey]bool{}
	na := map[RequiredViewKey]bool{}
	for _, ev := range l.events {
		switch p := ev.Payload.(type) {
		case RequiredViewsExtended:
			for _, k := range p.Keys {
				if !seen[k] {
					seen[k] = true
					keys = append(keys, k)
				}
			}
		case RequiredViewNotApplicable:
			na[p.Key] = true
		}
	}
	observed := map[RequiredViewKey]bool{}
	if ix != nil {
		for _, rec := range ix.Active() {
			if rec.RecordKind != evidence.KindFinding {
				continue
			}
			observed[RequiredViewKey{TargetID: rec.TargetID, Tool: rec.Provenance.Source}] = true
			// compare_peers만 키에 metric이 있다(§4 A-5).
			observed[RequiredViewKey{TargetID: rec.TargetID, Tool: rec.Provenance.Source,
				Metric: rec.Effect.Metric}] = true
		}
	}
	var out []RequiredViewKey
	total := 0
	for _, k := range keys {
		if na[k] {
			continue
		}
		total++
		if observed[k] {
			continue
		}
		out = append(out, k)
	}
	return out, total
}

// diagnosisVerdict — status의 UI 배너 등급 매핑. 모든 가설이 반증으로
// 끝난 insufficient는 CONTRADICTED(적극적으로 기각됨), 그 외
// insufficient·provisional은 INCONCLUSIVE.
func diagnosisVerdict(l *Ledger, dec Decision) string {
	switch dec.Status {
	case StatusConfirmed:
		return "CONCLUSIVE"
	case StatusInsufficient:
		allRefuted := len(l.hypoOrder) > 0
		for _, id := range l.hypoOrder {
			if l.hypos[id].Lifecycle != LifeRefuted {
				allRefuted = false
				break
			}
		}
		if allRefuted {
			return "CONTRADICTED"
		}
	}
	return "INCONCLUSIVE"
}

func hypothesisCard(l *Ledger, gaps gapSet, id string, rank int, adopted bool, passedSupports int, confidence float64) UIHypothesis {
	h := l.hypos[id]
	card := UIHypothesis{
		Rank: rank, IsAdopted: adopted,
		Title:       h.Entry.TargetID,
		Description: h.Entry.MechanismText(),
		Grounds:     passedObservations(l, id, DirSupports),
	}
	if h.Dimension != nil {
		card.Culprits = []UICulprit{{Label: h.Dimension.Label, Value: h.Dimension.Value}}
	}

	verdict := "WEAKENED"
	switch {
	case h.Lifecycle == LifeRefuted:
		verdict = "CONTRADICTED"
	case passedSupports > 0:
		verdict = "PROVEN"
	}
	card.Evaluation = UIEvaluation{
		Verdict:     verdict,
		Confidence:  confidence,
		Supports:    card.Grounds,
		Contradicts: passedObservations(l, id, DirRefutes),
		Missing:     gaps.byHypothesis(id),
	}
	return card
}

// ── 집계 헬퍼 — 세거나 찾을 뿐, 판단하지 않는다 ─────────────────

// passedObservations는 해당 가설에 해당 방향으로 심사 통과한 관측을
// 제안 순서대로 모은다.
func passedObservations(l *Ledger, hypo string, dir LinkDirection) []string {
	var out []string
	for _, evID := range l.evOrder {
		e := l.evidence[evID]
		for i, lk := range e.Entry.Links {
			if lk.Hypothesis == hypo && lk.Direction == dir && e.Verdicts[i] == VerdictPassed {
				out = append(out, e.Entry.Observation)
			}
		}
	}
	return out
}

// stepObservations는 사슬 구간 i를 지목해 심사 통과한 관측들이다.
func stepObservations(l *Ledger, hypo string, step int) []string {
	var out []string
	for _, evID := range l.evOrder {
		e := l.evidence[evID]
		for i, lk := range e.Entry.Links {
			if lk.Hypothesis == hypo && lk.Direction == DirSupports &&
				lk.ChainStep != nil && *lk.ChainStep == step && e.Verdicts[i] == VerdictPassed {
				out = append(out, e.Entry.Observation)
			}
		}
	}
	return out
}

// missingEvidence — 출력 missingEvidence의 원천 셋: 조회 불가 실행 +
// **미판정 명제 + 미조사 필수 관점**(§14-4 4c, next_steps와 같은 기계
// 원천이다 — 한쪽에만 나타나는 결손은 없다).
//
// 넷째였던 "이행 불가 의무"는 사라졌다 — 의무 6축 자체가 폐기됐고(§8),
// 그 기능은 판정 게이트의 미충족 요건(DecisionV2.MissingRequirements)이
// 계승한다. 리포트 배선은 5c 몫이다.
func missingEvidence(l *Ledger, gaps gapSet) []string {
	out := probeGaps(l)
	out = append(out, gaps.lines()...)
	return out
}

// probeGaps — probe_executed(outcome=infeasible) 줄들. "조회했으나
// 데이터 없음"의 기록이다.
func probeGaps(l *Ledger) []string {
	var out []string
	for _, ev := range l.events {
		p, ok := ev.Payload.(ProbeExecuted)
		if !ok || p.Outcome != ProbeInfeasible {
			continue
		}
		out = append(out, fmt.Sprintf("조회 불가: %s [%s] (%s)",
			p.ObservationKey, strings.Join(p.PredIDs, ","), p.Reason))
	}
	return out
}

// ruledOut — 정상 관측(source_status=normal)이 배제(refutes 통과)에
// 쓰인 것. negative evidence의 정직 표기.
func ruledOut(l *Ledger) []string {
	var out []string
	for _, evID := range l.evOrder {
		e := l.evidence[evID]
		if e.Entry.SourceStatus != SourceNormal {
			continue
		}
		for i, lk := range e.Entry.Links {
			if lk.Direction == DirRefutes && e.Verdicts[i] == VerdictPassed {
				out = append(out, e.Entry.Observation)
				break
			}
		}
	}
	return out
}
