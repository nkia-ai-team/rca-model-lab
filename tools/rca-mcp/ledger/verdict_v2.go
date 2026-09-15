// 술어 판정 진리표 + 자격 게이트 — docs/spec-agent-structure.md §6(판정
// 진리표 행 0~7, first-match)·§8(지지 자격 게이트)·§5.5(반증 자격 게이트)가
// 정본이다.
//
// **경계**: 이 파일은 술어 하나의 판정까지다. 가설 단위 집계(confirmed·
// probable·경쟁 서열)는 status_v2.go이고, rubric(§8.2)·리포트 문구(§15.4)·
// 심사점 A(§7.7)는 §14-5 몫이다.
//
// **구 판정 표면은 폐기됐다**(§14-5 5a): ledger/status.go(ComputeStatus)와
// pipeline/obligations.go(의무 6축)가 삭제되어 판정 체계는 이 계열 하나다.
package ledger

import (
	"fmt"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// ── 판정 결과 ───────────────────────────────────────────────────

// PredVerdict는 술어 하나의 판정이다(§6 진리표의 판정 열).
type PredVerdict string

const (
	// VerdictSupportedQualified — 행 3. 자격 지지.
	VerdictSupportedQualified PredVerdict = "supported_qualified"
	// VerdictSupportedWeak — 행 4. **inconclusive가 아니다**(§7.2-3·§8) —
	// probable의 "passed ≥1"에는 들되 confirmed 계수에서 빠진다.
	VerdictSupportedWeak PredVerdict = "supported_weak"
	// VerdictInvalidated — 행 5. necessary 불일치 + 반증 자격 충족.
	VerdictInvalidated PredVerdict = "invalidated"
	// VerdictInconclusive — 행 1·2·6·7.
	VerdictInconclusive PredVerdict = "inconclusive"
)

// PredJudgment는 술어 판정의 전량이다 — 판정과 **그 판정이 어느 행에서
// 나왔는지**를 함께 싣는다. 행 번호가 없으면 "왜 supported인가"를 사후에
// 재구성할 수 없고, 진리표가 first-match라는 계약도 검사할 수 없다.
type PredJudgment struct {
	PredID  string
	Verdict PredVerdict
	// RowsFired — 발화한 행 번호를 순서대로. 행 0은 전용 경로라 뒤에 행
	// 3~7이 따라붙는다(§6 "truth 파생 후 행 3 이하로 진행"). 그 외에는 한 개다.
	RowsFired []int
	// Row — 판정을 낳은 마지막 행(감사 표기의 대표값).
	Row int

	Truth       bool
	Holds       bool // truth와 Expectation의 일치
	Derive      evidence.DeriveStatus
	Resolution  evidence.ResolveStatus
	EIDs        []string
	SupportGate GateResult
	CounterGate GateResult

	// 가설 단위 집계(status_v2.go)가 index를 다시 뒤지지 않도록 판정 시점에
	// 뽑아 둔 사실들이다.
	FindingClasses []string // §8 다양성 축 — C-5 대조 참조
	LineageIDs     []string // §8 "서로 다른 원천 계보 ≥2"
	EntityKeys     []string // §7.3 말단 깊이 게이트의 대조 대상

	// PreSatisfied — 선언 시점에 이미 충족된 회고 술어(§6 기계 딱지).
	// **자격 지지 계수에서 빠진다** — C-4·1b 인계 ②.
	PreSatisfied bool
	// Role — 반증력. 행 5·6·7의 분기 축.
	Role PredRole
	Note string
}

// Supported는 행 3·4의 통칭이다.
func (j PredJudgment) Supported() bool {
	return j.Verdict == VerdictSupportedQualified || j.Verdict == VerdictSupportedWeak
}

// Decided는 그 술어가 실제로 판정됐는가다 — §8 inferior 진리표 ②
// ("necessary 술어 ≥1회 실제 판정됨 — 미실측이면 자격 없음")의 입력.
// inconclusive는 판정이 아니다.
func (j PredJudgment) Decided() bool {
	return j.Supported() || j.Verdict == VerdictInvalidated
}

// ── 자격 게이트 ─────────────────────────────────────────────────

// ClauseState는 자격 조항 하나의 상태다. **unknown을 pass와 구분한다** —
// "검사할 재료가 없었다"를 통과로 접으면 fail-closed 규율이 스키마 층에서
// 새어 나간다(4차 A-2가 CollectLag에서 닫은 것과 같은 문).
type ClauseState string

const (
	ClausePass ClauseState = "pass"
	ClauseFail ClauseState = "fail"
	// ClauseUnknown — 판정 재료가 없다. **미충족으로 센다**(fail-closed).
	ClauseUnknown ClauseState = "unknown"
	// ClauseNoSource — 스펙 조항에 대응하는 실물 원천이 없다. 미충족으로
	// 세지 **않는다** — 아래 clauseSample의 주석이 이 예외의 유일한 근거다.
	ClauseNoSource ClauseState = "no_source"
)

// GateClause는 자격 조항 하나의 판정이다.
type GateClause struct {
	Name  string
	State ClauseState
	Note  string
}

// GateResult는 §5.5·§8 게이트의 결과다.
type GateResult struct {
	Qualified bool
	Clauses   []GateClause
}

// Failed는 미충족 조항 이름들이다(§15.4 한계 블록의 재료).
func (g GateResult) Failed() []string {
	var out []string
	for _, c := range g.Clauses {
		if c.State == ClauseFail || c.State == ClauseUnknown {
			out = append(out, c.Name)
		}
	}
	return out
}

// 조항 이름 — §8 대응표의 행 이름과 1:1이다.
const (
	ClauseActive       = "active"
	ClauseAvailability = "availability"
	ClauseConfidence   = "confidence"
	ClauseTruncation   = "truncation"
	ClauseWindowCover  = "window_cover"
	ClauseCollectLag   = "collect_lag_cutoff"
	ClauseSample       = "sample_sensitivity"
)

// TimeRange는 자격이 요구하는 시간 범위다(§5.5 "필요 시간 범위 수집됨" /
// §8 "창 커버").
type TimeRange struct{ From, To time.Time }

// GateContext는 자격 판정의 외부 입력이다.
type GateContext struct {
	// Index — active 판정과 레코드 조회(§5.7).
	Index *evidence.Index
	// Now — 수집 지연 컷오프의 기준 시각.
	Now time.Time
	// RequiredWindow — 자격이 요구하는 시간 범위. **nil이면 창 커버는
	// unknown = 미충족**이다(fail-closed). 술어에는 창 클래스(full |
	// onset_narrow)만 있고 실 시각이 없어 이 값은 하네스가 준다 —
	// 배선은 §14-2이고, 잊은 호출자는 confirmed가 아니라 weak_support를
	// 얻는다(조용한 통과가 아니라 눈에 보이는 강등).
	RequiredWindow *TimeRange
	// MinSampleN — 표본 하한. nil이면 표본 조항을 판정하지 않는다
	// (clauseSample 주석 참조).
	MinSampleN *int
}

type gateMode string

const (
	gateSupport gateMode = "support" // §8
	gateCounter gateMode = "counter" // §5.5
)

// claimKind는 그 근거가 **무엇을 주장하는 데** 쓰이는가다 — §8 창 커버
// 조항의 분기 축이다(사용자 결정 2026-08-04).
type claimKind string

const (
	// claimExistential — "있었다"(present류). 목격 하나가 곧 증거다.
	claimExistential claimKind = "existential"
	// claimUniversal — "없었다"(absent류). 창 전체를 봐야 말할 수 있다.
	claimUniversal claimKind = "universal"
)

// claimKindOf는 (Predicate, Expectation) 짝이 존재 주장인지 부재 주장인지를
// 판정한다.
//
// **Predicate 단독으로는 정해지지 않는다**: `absent must_not_hold`는 "뭔가
// 있었다"는 존재 주장이고 `present must_not_hold`는 "없었다"는 부재 주장이다.
// Expectation을 빼고 술어 이름만 보면 절반이 반대로 분류된다.
//
// 존재형 술어(present·magnitude_ge·direction_*)는 전부 "그런 관측을 목격했다"는
// 주장이고, 부재형(absent·magnitude_lt)은 "창 안 어디에도 그런 것이 없었다"는
// 전칭 주장이다 — magnitude_lt를 전칭에 넣는 이유가 이것이다("전부 임계 미만"은
// 창의 절반만 보고 말할 수 없다). must_not_hold는 그 방향을 뒤집는다.
func claimKindOf(p Predicate, exp Expectation) claimKind {
	existential := true
	switch p {
	case PredAbsent, PredMagnitudeLT:
		existential = false
	}
	if exp == ExpectMustNotHold {
		existential = !existential
	}
	if existential {
		return claimExistential
	}
	return claimUniversal
}

// evaluateGate는 근거 레코드 하나가 자격을 갖췄는지 판정한다.
//
// **두 게이트는 "거울"이다**(§8 대응표). 현행 4값 Availability enum에서는
// 두 조건이 실제로 같은 집합이다 — 지지의 화이트리스트 {observed,
// observed_zero}와 반증의 블랙리스트 ∉{missing, not_applicable}은 여집합
// 관계다. 그래도 코드에서 둘을 따로 쓰는 이유는 enum이 늘어나는 날
// (예: partial) 두 규칙이 갈리기 때문이다 — 지금 하나로 접으면 그때
// 조용히 같은 값을 쓴다.
func evaluateGate(r evidence.EvidenceIndexRecord, gc GateContext, mode gateMode, claim claimKind) GateResult {
	var cs []GateClause
	add := func(name string, st ClauseState, note string) {
		cs = append(cs, GateClause{Name: name, State: st, Note: note})
	}

	// ① active(§5.7) — 판정 게이트의 입력 EID는 전부 active여야 한다.
	switch {
	case gc.Index == nil:
		add(ClauseActive, ClauseUnknown, "index 미주입 — active 판정 불가")
	default:
		heir, err := gc.Index.ActiveHeir(r.EID)
		switch {
		case err != nil:
			add(ClauseActive, ClauseFail, err.Error())
		case heir != r.EID:
			add(ClauseActive, ClauseFail, "superseded — active 후계는 "+heir)
		default:
			add(ClauseActive, ClausePass, "")
		}
	}

	// ② Availability.
	av := r.Quality.Availability
	if mode == gateSupport {
		if av == evidence.AvailObserved || av == evidence.AvailObservedZero {
			add(ClauseAvailability, ClausePass, string(av))
		} else {
			add(ClauseAvailability, ClauseFail, string(av)+" — 화이트리스트 밖(§8)")
		}
	} else {
		if av == evidence.AvailMissing || av == evidence.AvailNotApplicable {
			add(ClauseAvailability, ClauseFail, string(av)+" — 반증 불가(§5.5)")
		} else {
			add(ClauseAvailability, ClausePass, string(av))
		}
	}

	// ③ 신뢰 딱지 — 양쪽 다 Confidence=low면 미충족.
	if r.Quality.Confidence == evidence.ConfOK {
		add(ClauseConfidence, ClausePass, "")
	} else {
		add(ClauseConfidence, ClauseFail, "confidence=low")
	}

	// ④ 절단 — Truncated=false 또는 OmittedN=0(구획 확인).
	// query_scope 레코드는 Scope.Omitted가 정본이다.
	switch {
	case r.Scope != nil && !r.Scope.Complete():
		add(ClauseTruncation, ClauseFail, fmt.Sprintf("omitted=%d", r.Scope.Omitted))
	case r.Truncation.OmittedN > 0:
		add(ClauseTruncation, ClauseFail, fmt.Sprintf("omitted_n=%d", r.Truncation.OmittedN))
	case r.Truncation.Truncated:
		// 절단 표시인데 누락 수 0 — 구획 확인 해소이거나 **질의 계층
		// 절단**(봉투 QueryTruncated 승계 — Total 미상이라 OmittedN이
		// 없다, §14-7 D-1)이다. 후자는 ②(availability=missing)·③
		// (confidence=low)이 앞서 막으므로 여기 도달하면 전자다 — 이
		// 분기 단독으로 절단 무해를 판정하지 말 것.
		add(ClauseTruncation, ClausePass, "truncated이나 omitted_n=0(구획 확인)")
	default:
		add(ClauseTruncation, ClausePass, "")
	}

	// ⑤ 창 커버 — **술어 방향에 의존한다**(§8, 사용자 결정 2026-08-04).
	cs = append(cs, clauseWindow(r, gc, mode, claim))

	// ⑥ 수집 지연 컷오프 — "그 창의 데이터가 도착할 시간이 지났는가".
	// 판정식: Now ≥ Window.To + CollectLag. 지연이 unknown이면 판정 불가 =
	// 미충족(§15.2-3 fail-closed — 지연이 미상이면 컷오프를 말할 수 없다).
	lag := r.Window.CollectLag
	switch {
	case !lag.Usable():
		add(ClauseCollectLag, ClauseUnknown, "collect_lag="+string(lag.Status))
	case r.Window.To.IsZero():
		add(ClauseCollectLag, ClauseUnknown, "레코드 창 끝 없음 — 컷오프 계산 불가")
	case gc.Now.IsZero():
		add(ClauseCollectLag, ClauseUnknown, "기준 시각 미주입")
	case gc.Now.Before(r.Window.To.Add(time.Duration(lag.ValueS) * time.Second)):
		add(ClauseCollectLag, ClauseFail, fmt.Sprintf("컷오프 미도달(지연 %ds)", lag.ValueS))
	default:
		add(ClauseCollectLag, ClausePass, fmt.Sprintf("지연 %ds", lag.ValueS))
	}

	// ⑦ 표본/민감도.
	cs = append(cs, clauseSample(r, gc))

	q := true
	for _, c := range cs {
		if c.State == ClauseFail || c.State == ClauseUnknown {
			q = false
		}
	}
	return GateResult{Qualified: q, Clauses: cs}
}

// clauseWindow는 §8 대응표의 "시간 범위" 행이다 — **술어 방향에 의존한다**
// (사용자 결정 2026-08-04, §8 개정 블록).
//
//	absent류 근거(claimUniversal)  전체 커버 요구 유지(fail-closed).
//	                              창의 절반만 보고 "없었다"고 말할 수 없다.
//	present류 근거(claimExistential) 목격 시각이 요구 창 안이면 충족.
//	                              관측 범위가 창보다 좁아도 목격 자체가 증거다.
//
// **반증 자격(§5.5)은 분기하지 않는다** — 개정 블록이 "필요 범위 수집됨"은
// 전체 커버 그대로라고 못박았다. 반증은 "그 시간대에 그것이 없었다/정상이었다"를
// 주장하므로 방향과 무관하게 전 범위가 필요하다.
//
// 근거는 실측이다(§14-2): 창 커버율 56%(384/680) — 방향 구분 없는 전체 커버
// 요구가 멀쩡한 목격 44%를 weak로 강등해 confirmed를 구조적으로 압박했다.
//
// # 판단 지점 — "목격 시각"이 무엇인가
//
// 레코드에 목격의 점 시각을 실은 필드는 없다. 있는 것은 두 구간이다:
// 변화구간(ChangeFrom/To — 산출 가능한 도구에서만)과 그 레코드가 실제로 본
// 관측 범위(Window.From/To). 그래서 목격을 **구간**으로 읽고 요구 창과
// 겹치는지를 본다 — 변화구간이 있으면 그쪽이 정확하므로 우선한다. 겹침이
// 없으면 그 목격은 요구 창 밖의 사건이다(미달).
func clauseWindow(r evidence.EvidenceIndexRecord, gc GateContext, mode gateMode, claim claimKind) GateClause {
	fail := func(note string) GateClause {
		return GateClause{Name: ClauseWindowCover, State: ClauseFail, Note: note}
	}
	unknown := func(note string) GateClause {
		return GateClause{Name: ClauseWindowCover, State: ClauseUnknown, Note: note}
	}
	if gc.RequiredWindow == nil {
		return unknown("요구 창 미주입(§14-2 배선)")
	}
	if r.Window.From.IsZero() || r.Window.To.IsZero() {
		return unknown("레코드 조회 창 없음")
	}
	covers := !r.Window.From.After(gc.RequiredWindow.From) && !r.Window.To.Before(gc.RequiredWindow.To)
	if covers {
		return GateClause{Name: ClauseWindowCover, State: ClausePass}
	}
	partial := fmt.Sprintf("레코드 창 %s~%s가 요구 %s~%s를 못 덮음",
		r.Window.From.Format(time.RFC3339), r.Window.To.Format(time.RFC3339),
		gc.RequiredWindow.From.Format(time.RFC3339), gc.RequiredWindow.To.Format(time.RFC3339))
	if mode == gateCounter || claim == claimUniversal {
		return fail(partial)
	}
	// present류 — 목격 구간이 요구 창과 겹치는가.
	from, to := r.Window.From, r.Window.To
	if r.Window.ChangeFrom != nil {
		from = *r.Window.ChangeFrom
		to = from
		if r.Window.ChangeTo != nil {
			to = *r.Window.ChangeTo
		}
	}
	if to.Before(gc.RequiredWindow.From) || from.After(gc.RequiredWindow.To) {
		return fail(fmt.Sprintf("목격 구간 %s~%s가 요구 창 밖 — %s",
			from.Format(time.RFC3339), to.Format(time.RFC3339), partial))
	}
	// 부분 커버 사실은 기록으로 남는다(결정 문면: "부분 커버 기록 유지").
	return GateClause{Name: ClauseWindowCover, State: ClausePass,
		Note: "present류 — 목격 시각이 요구 창 안(부분 커버: " + partial + ")"}
}

// clauseSample은 §8 대응표의 "표본/민감도" 행이다.
//
// **이 조항만 unknown을 미충족으로 세지 않는다 — 원천이 없기 때문이다.**
// 1b 실측: 표본 수를 봉투에 싣는 도구는 compare_peers 하나뿐이고
// (comparepeers.go의 `samples`), 그래서 Quality.SampleN은 *int이며 나머지
// 15종은 전부 nil이다. 이 조항을 fail-closed로 걸면 **모든 도구의 모든
// 레코드가 영구 미충족**이 되어 confirmed가 구조적으로 불가능해진다 —
// §8이 경쟁 서열 논의(4차 A-8)에서 명시적으로 거부한 실패 모드다.
// 원천 없는 조항을 강제하는 대신 상태를 no_source로 기록해 리포트가 그
// 사실을 말하게 한다. 원천이 생기는 날(도구가 표본 수를 실으면) 이 함수만
// 고치면 자동으로 강제된다.
func clauseSample(r evidence.EvidenceIndexRecord, gc GateContext) GateClause {
	switch {
	case r.Quality.SampleN == nil:
		return GateClause{Name: ClauseSample, State: ClauseNoSource,
			Note: "봉투에 표본 수 없음 — 강제 대상 아님(1b 실측: compare_peers만 원천 보유)"}
	case gc.MinSampleN == nil:
		return GateClause{Name: ClauseSample, State: ClauseNoSource,
			Note: fmt.Sprintf("표본 %d — 하한 미설정으로 미판정", *r.Quality.SampleN)}
	case *r.Quality.SampleN < *gc.MinSampleN:
		return GateClause{Name: ClauseSample, State: ClauseFail,
			Note: fmt.Sprintf("표본 %d < 하한 %d", *r.Quality.SampleN, *gc.MinSampleN)}
	default:
		return GateClause{Name: ClauseSample, State: ClausePass, Note: ""}
	}
}

// gateAll은 해소에 쓰인 근거 **전부**가 자격을 갖췄는지다.
//
// 범위 해소(행 0)는 근거가 여럿일 수 있고, 그때 "하나만 통과하면 자격"은
// 1c가 absent 해소에서 이미 기각한 논리다 — 절단된 형제가 감춘 개체를
// 완전한 형제 하나로 덮어쓰는 것과 같은 오류가 자격 층에서 반복된다.
func gateAll(eids []string, gc GateContext, mode gateMode, claim claimKind) GateResult {
	if len(eids) == 0 {
		return GateResult{Qualified: false, Clauses: []GateClause{
			{Name: ClauseActive, State: ClauseUnknown, Note: "근거 EID 없음"}}}
	}
	if gc.Index == nil {
		return GateResult{Qualified: false, Clauses: []GateClause{
			{Name: ClauseActive, State: ClauseUnknown, Note: "index 미주입"}}}
	}
	out := GateResult{Qualified: true}
	for _, eid := range eids {
		r, ok := gc.Index.Get(eid)
		if !ok {
			out.Qualified = false
			out.Clauses = append(out.Clauses, GateClause{Name: ClauseActive,
				State: ClauseFail, Note: eid + " index에 없음"})
			continue
		}
		g := evaluateGate(r, gc, mode, claim)
		if !g.Qualified {
			out.Qualified = false
		}
		for _, c := range g.Clauses {
			c.Name = eid + ":" + c.Name
			out.Clauses = append(out.Clauses, c)
		}
	}
	return out
}

// ── 진리표 (§6 행 0~7, first-match) ─────────────────────────────

// JudgePredicate는 §6 판정 진리표를 기계로 돈다. **행 순서가 계약이다** —
// 위에서부터 첫 일치 행에서 판정이 확정된다.
//
// 행 0만 예외적으로 "해소 경로"이고 판정 자체는 행 3 이하에서 난다
// (§6 행 0: "truth 파생 후 행 3 이하로 진행. 행 1·2를 면제하는 전용 경로").
//
// # 행 0의 observed_zero 인정 범위 (1c 인계 ③ — 1e의 결정)
//
// 스펙 행 0의 괄호는 "(Omitted=0, Availability=observed_zero)"이지만,
// **이 구현은 완전 조회이기만 하면 observed도 행 0으로 인정한다.** 근거:
//
//	① 장부가 두 값을 실어 둔 것은 갈래가 실재하기 때문이다(1c): 덮는 범위
//	   행이 전부 0건이면 observed_zero, 다른 개체는 나왔는데 이 개체만
//	   없으면 observed다. 둘 다 **완전 열거**이므로 부재는 똑같이 실측이다.
//	② 후자가 오히려 강한 근거다 — 다른 개체가 나왔다는 것은 그 조회가 실제
//	   데이터에 대해 돌았다는 증거다. 전부 0건인 쪽이야말로 "질의가 엉뚱한
//	   곳을 봤을 가능성"이 남는다. 따라서 observed를 빼는 것은 보수가 아니라
//	   더 약한 근거만 남기는 역전이다.
//	③ §8 지지 자격 게이트의 Availability 화이트리스트가 {observed,
//	   observed_zero} **둘 다**를 명시한다 — 행 0을 observed_zero로 좁히면
//	   범위 해소 근거가 §8을 통과할 수 없는 값만 남아 두 조항이 어긋난다.
//
// 보수성은 다른 자리가 진다: 덮는 범위 행이 **하나라도 절단됐으면** 해소
// 자체가 없고(1c resolveByScopeLocked), 범위 관측에서 파생되는 술어는
// present/absent뿐이며(magnitude·direction은 DeriveNoMagnitude·
// NoDirection으로 막힌다), 자격은 근거 **전부**가 통과해야 한다.
// **스펙 §6 행 0의 문안 정정 후보로 §14-5에 넘긴다.**
func JudgePredicate(p SignalPred, l *evidence.Ledger, gc GateContext) PredJudgment {
	j := PredJudgment{PredID: p.PredID, Role: p.Role, PreSatisfied: p.PreSatisfied}
	if l == nil {
		j.Verdict, j.Row, j.RowsFired = VerdictInconclusive, 1, []int{1}
		j.Note = "명제 장부 없음"
		return j
	}
	res := l.Resolve(p.ObservationKey())
	j.Resolution, j.EIDs = res.Status, res.EIDs

	switch res.Status {
	case evidence.ResolvedScope:
		// 행 0 — 완전 조회 범위로 해소. 인정 범위는 위 주석의 결정.
		j.RowsFired = append(j.RowsFired, 0)
	case evidence.ResolvedEntity:
		// 개체 해소 — 행 0을 거치지 않는다.
	default:
		// 행 1 — 해소 불능(0건·복수·절단된 범위).
		j.Verdict, j.Row, j.RowsFired = VerdictInconclusive, 1, append(j.RowsFired, 1)
		j.Derive = evidence.DeriveUnresolved
		j.Note = "해소 " + string(res.Status)
		return j
	}

	truth, ds := evidence.DeriveTruth(res.Value, p.Predicate, p.Threshold, res.Status)
	j.Truth, j.Derive = truth, ds
	if ds != evidence.DeriveOK {
		// 행 2 — 파생 불능은 관측 부재와 같다(§5.1). Availability·class만이
		// 아니라 magnitude·threshold·direction 결손도 여기다: 행 2의 근거
		// 문안이 "파생 불능은 관측 부재와 같다"이고, 그 외에 이 결과를 받을
		// 행이 진리표에 없다. 지지·반증 **양쪽** 금지.
		j.Verdict, j.Row, j.RowsFired = VerdictInconclusive, 2, append(j.RowsFired, 2)
		j.Note = "파생 불능: " + string(ds)
		return j
	}

	j.collectFacts(res.EIDs, gc)
	j.Holds = evidence.Holds(truth, p.Expectation)

	if j.Holds {
		j.SupportGate = gateAll(res.EIDs, gc, gateSupport, claimKindOf(p.Predicate, p.Expectation))
		if j.SupportGate.Qualified {
			j.Verdict, j.Row, j.RowsFired = VerdictSupportedQualified, 3, append(j.RowsFired, 3)
			return j
		}
		// 행 4 — 자격 미달 지지. inconclusive가 아니다.
		j.Verdict, j.Row, j.RowsFired = VerdictSupportedWeak, 4, append(j.RowsFired, 4)
		j.Note = "자격 미달: " + fmt.Sprint(j.SupportGate.Failed())
		return j
	}

	if p.Role != RoleNecessary {
		// 행 7 — corroborating 불일치는 지지 실패일 뿐이다.
		j.Verdict, j.Row, j.RowsFired = VerdictInconclusive, 7, append(j.RowsFired, 7)
		j.Note = "corroborating 불일치"
		return j
	}
	// 반증 자격은 방향을 보지 않는다 — 전체 커버 그대로다(§5.5 개정 블록).
	j.CounterGate = gateAll(res.EIDs, gc, gateCounter, claimUniversal)
	if j.CounterGate.Qualified {
		j.Verdict, j.Row, j.RowsFired = VerdictInvalidated, 5, append(j.RowsFired, 5)
		return j
	}
	// 행 6 — 그 "정상"은 반증 불가. W1 재검 후보다.
	j.Verdict, j.Row, j.RowsFired = VerdictInconclusive, 6, append(j.RowsFired, 6)
	j.Note = "반증 자격 미달: " + fmt.Sprint(j.CounterGate.Failed())
	return j
}

// collectFacts는 가설 단위 집계가 쓸 사실을 판정 시점에 뽑는다.
func (j *PredJudgment) collectFacts(eids []string, gc GateContext) {
	if gc.Index == nil {
		return
	}
	for _, eid := range eids {
		r, ok := gc.Index.Get(eid)
		if !ok {
			continue
		}
		if r.FindingClass != "" {
			j.FindingClasses = append(j.FindingClasses, r.FindingClass)
		}
		if r.Provenance.LineageID != "" {
			j.LineageIDs = append(j.LineageIDs, r.Provenance.LineageID)
		}
		if r.EntityKey != "" {
			j.EntityKeys = append(j.EntityKeys, CanonEntityKey(r.EntityKey))
		}
	}
	j.FindingClasses = uniq(j.FindingClasses)
	j.LineageIDs = uniq(j.LineageIDs)
	j.EntityKeys = uniq(j.EntityKeys)
}

// CanonEntityKey는 §5.6 EntityKey의 정규화다 — ChainClaim.EntityKey와
// 레코드 EntityKey의 **typed 동치**(§8 말단 게이트 판정식)가 표기 흔들림으로
// 어긋나지 않게 관측 키와 같은 규칙을 쓴다.
func CanonEntityKey(s string) string {
	return evidence.ObservationKey{EntityKey: s}.Canonical().EntityKey
}

func uniq(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	sort.Strings(in)
	out := in[:1]
	for _, s := range in[1:] {
		if s != out[len(out)-1] {
			out = append(out, s)
		}
	}
	return out
}
