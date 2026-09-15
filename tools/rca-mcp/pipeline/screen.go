// 파이프라인 [3] 스크리닝 — **LLM이 서 있던 자리를 기계가 대신한다**
// (docs/spec-agent-structure.md §4). 종전 examine.go의 Examiner·Finding·
// ExamineResult는 이 파일로 대체됐다: Finding{Observation, Refs}에는 EID·
// Effect·Quality·Truncation을 담을 자리가 없어 산출 타입째 교체된다는 것이
// §4의 판단이고, 그래서 **[3]의 산출 알맹이는 evidence index**다.
//
// 여기 남는 것은 index에 담기지 않는 축뿐이다 — 호출 계획(조사 범위)·
// 커버리지 격자·필수 관점 분모·실행 대응. 관측 자체는 전부 index에 있다.
//
// 배터리 구현체는 battery 패키지다(battery.Screener). 이 자리를 인터페이스로
// 남기는 이유는 §11.1 ablation("조사 단계 대체")과 mock 보행이며, 방향은
// pipeline ← battery다(battery가 pipeline.TriageResult를 읽는다).
package pipeline

import (
	"context"
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// ScreenDeps는 [3]이 소비하는 적재 경로다 — 호출자(pipeline.Run)가 만들어
// 넘긴다. store·index는 [2] 합성 봉투와 **같은 것**이어야 한다: 변경 레코드와
// 스크리닝 레코드가 다른 index에 앉으면 [4] 입력이 둘로 갈린다.
type ScreenDeps struct {
	// Store — 봉투 원문 적재(§5). ref 발급의 원천.
	Store *evidence.Store
	// Index — 투영 레코드의 append-only 장부. **[3]의 산출 본체**다.
	Index *evidence.Index
	// Ledger — required_views_extended·required_view_not_applicable의
	// 기록처(§4 분모 재생 가능성).
	Ledger *ledger.Ledger
}

// ScreenCall은 계획된 호출 하나의 실행 대응이다(battery.CallOutcome의
// 파이프라인 쪽 표현). 실패한 호출도 남는다 — "무엇을 부르기로 했고 무엇이
// 됐는가"가 §15.4 한계 블록과 산출물 감사의 재료다.
type ScreenCall struct {
	Tool     string
	TargetID string
	Targets  []string // A6(get_data_coverage) 청크만 참
	// ParamsDigest·EnvelopeRef — 감사·재현(§5.1 Provenance).
	ParamsDigest string
	EnvelopeRef  string
	// EIDs — 이 봉투에서 index에 실린 레코드. 술어 불가 도구(A0·A6)는 빈다.
	EIDs []string
	// Err — 비어 있지 않으면 그 키는 미조회다(§4 분모에 남는다).
	Err string
	// ErrClass — 실패 분류 토큰(§14-7 ③ S-3 — battery.CallOutcome 승계).
	// 지표 3(스크리닝 결손 귀속)이 원문 파싱 없이 층화하는 축이다.
	ErrClass string
}

// ScreenResult는 [3]의 산출 **요약**이다 — 관측 레코드는 여기 없다(index다).
//
// 필드마다 소비자를 지목한다(이 레포 규율):
type ScreenResult struct {
	// Targets — 동결된 조사 범위(§15.2-7 ④의 N₁ = seed ∪ A0 신규).
	// 소비자: 격자 scope · 산출물 감사.
	Targets []string
	// DiscoveredTargets — A0가 [1] 위상 박제 밖에서 찾아낸 신규 대상.
	// 소비자: 리포트("조사 범위가 왜 넓어졌는가").
	DiscoveredTargets []string
	// Calls — **성공 호출만**의 로그(grid.go 계약). 소비자: BuildGrid.
	Calls []ToolCallRecord
	// Executions — 계획 순서 그대로의 실행 대응(실패 포함). 소비자: 감사.
	Executions []ScreenCall
	// RequiredViews — 필수 관점 집합(계층 A + 계층 B, 절단 전)(§4).
	// 소비자: §5.2 롤업 분모 · §8.2-1 rubric 분모(§14-5).
	RequiredViews []ledger.RequiredViewKey
	// Unobserved — 그중 관측을 못 남긴 키. 소비자: §5.2 롤업 · §15.4.
	Unobserved []ledger.RequiredViewKey
	// NotApplicable — not_applicable 딱지가 붙은 키(C-12). 집합은 그대로
	// 두고 **분모 계산에서만** 뺀다. 소비자: 롤업·rubric 분모(§14-5).
	NotApplicable []ledger.RequiredViewKey
	// TruncatedLayerB — 계층 B 상한 10으로 잘린 대상. 소비자: §15.4 한계 블록.
	TruncatedLayerB []string
	// Grid — 대상×관점 커버리지 격자(§15.2). **규칙이 계산한다**(RunScreen) —
	// 구현체가 아니라 하네스의 몫인 것은 RunExamine 시절과 같다.
	Grid CoverageGrid
}

// Screener는 [3] 스크리닝 배터리의 자리다(§4 "Examiner 인터페이스에서 LLM
// 구현체를 빼고 기계 구현체 BatteryExaminer를 꽂는다"). 실 구현체는
// battery.Screener이고, mock 보행·ablation은 다른 구현체를 꽂는다.
type Screener interface {
	Screen(ctx context.Context, tri TriageResult, deps ScreenDeps) (ScreenResult, error)
}

// RunScreen은 배터리를 부르고 규칙 몫의 뒷정리를 한다 — 지금 규칙 몫은
// 격자 계산 하나다(RunExamine의 ref 반려·Quiet은 Finding 필드에 결박돼
// 있었으므로 함께 소멸했다. "조용한 이웃"은 증상 멤버 여부로 [4]가 직접
// 판정한다). **EnvelopeRef는 어디서도 강제되지 않는다**(2c 독립 검증
// Gap 2 — Validate·projector 모두 비어 있음을 반려하지 않는다): 실 생산자
// 2곳(battery·StoreChanges)이 store.Put ref를 항상 실어 구성상 보장될
// 뿐이고, 대체 Screener/mock은 ref 없는 레코드를 앉힐 수 있다. 인용
// 시점의 강제는 §8.1 게이트(§14-5)의 몫이다.
func RunScreen(ctx context.Context, tri TriageResult, s Screener, deps ScreenDeps) (ScreenResult, error) {
	if s == nil {
		return ScreenResult{}, fmt.Errorf("screen: Screener 필수")
	}
	if deps.Store == nil || deps.Index == nil || deps.Ledger == nil {
		return ScreenResult{}, fmt.Errorf("screen: store·index·ledger 필수")
	}
	r, err := s.Screen(ctx, tri, deps)
	if err != nil {
		return ScreenResult{}, fmt.Errorf("screen: %w", err)
	}
	// 격자 scope는 **동결된 조사 범위**다(§4). A0 신규 대상도 계층 A의
	// 무절단 호출 대상이고 RequiredViewKeys에 편입되므로(§4 "A0 신규 대상은
	// 편입 시점에 append") 격자에서만 빼면 분모가 두 벌이 된다.
	// 구현체가 범위를 못 주면(mock) Triage 범위로 물러선다.
	scope := r.Targets
	if len(scope) == 0 {
		scope = triageScope(tri)
	}
	r.Grid = BuildGrid(scope, r.Calls)
	return r, nil
}

// triageScope는 [1]이 준 조사 의무 범위다 — 증상 대표 + 멤버 + 후보.
func triageScope(tri TriageResult) []string {
	scope := []string{tri.Symptom.TargetID}
	for _, g := range tri.MemberGroups {
		scope = append(scope, g.TargetID)
	}
	for _, c := range tri.Candidates {
		scope = append(scope, c.TargetID)
	}
	return scope
}

// GateRequiredWindow는 자격 게이트(§5.5 counterevidence 자격)의 요구 창을
// 만든다 — **인시던트 창 전체**다.
//
// 1e 인계의 배선 자리가 여기다: ledger.GateContext.RequiredWindow가 nil이면
// 창 커버 절이 weak_support로 강등되므로(ledger/verdict_v2.go:147), 게이트를
// 부르는 쪽(§14-5의 [6] 판정·§14-3의 심사점 R)은 반드시 이 함수의 값을
// GateContext에 실어야 한다. 값이 [3] 배터리의 호출 창과 같은 것이 계약이다
// — 배터리는 계층 A·B 전부를 WindowClass=full, From/To=tri.From/To로 부른다
// (battery/exec.go project·argsFor). 요구 창이 그보다 넓으면 전 레코드가
// 자동 강등되고, 좁으면 실제로 못 본 구간이 커버된 것으로 통과한다.
func GateRequiredWindow(tri TriageResult) *ledger.TimeRange {
	if tri.From.IsZero() || tri.To.IsZero() {
		return nil // 창을 모르면 nil — 모르는 것을 지어내지 않는다(fail-closed)
	}
	return &ledger.TimeRange{From: tri.From, To: tri.To}
}
