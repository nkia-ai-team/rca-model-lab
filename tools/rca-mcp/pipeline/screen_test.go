package pipeline

import (
	"context"
	"errors"
	"reflect"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// screenerFunc는 [3] 자리의 mock이다 — 종전 examinerFunc의 자리.
type screenerFunc func(TriageResult, ScreenDeps) (ScreenResult, error)

func (f screenerFunc) Screen(_ context.Context, tri TriageResult, d ScreenDeps) (ScreenResult, error) {
	return f(tri, d)
}

// screenDeps는 시험용 적재 경로다.
func screenDeps(t *testing.T) ScreenDeps {
	t.Helper()
	st, err := evidence.NewStore(t.TempDir())
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	return ScreenDeps{Store: st, Index: evidence.NewIndex(), Ledger: ledger.New()}
}

// findingRec는 유효한 finding 레코드 하나다(§5.1 Validate 통과 최소 형태).
func findingRec(target string, status evidence.Status, metric string) evidence.EvidenceIndexRecord {
	avail := evidence.AvailObserved
	if status == evidence.StatusNoData {
		avail = evidence.AvailMissing
	}
	return evidence.EvidenceIndexRecord{
		TargetID: target, RecordKind: evidence.KindFinding,
		// FindingClass는 사상표 행 키다 — "<원천>/<class>"이어야 RowFor가
		// 행을 찾는다(§5.1). 종전 "shifted"는 판정 시점 class 층 검사를
		// 통과하지 못하는 값이었다.
		Aspect: evidence.AspectMetric, FindingClass: "scan_metrics/shifted",
		Headline: target + " " + metric + " 관측",
		Effect: evidence.Effect{
			Kind: evidence.EffectRatio, Metric: metric, Direction: evidence.DirUp,
		},
		Window: evidence.TimeWindow{Class: evidence.WindowFull},
		Quality: evidence.Quality{
			Status: status, Availability: avail, Confidence: evidence.ConfOK,
			NoDataReason: noDataReasonFor(status),
		},
		Provenance: evidence.Provenance{Source: evidence.SrcScanMetrics, EnvelopeRef: "EST-0001:scan_metrics"},
	}
}

func noDataReasonFor(s evidence.Status) evidence.NoDataReason {
	if s == evidence.StatusNoData {
		return evidence.NoDataCollectorGap
	}
	return ""
}

// 격자 scope는 **배터리가 동결한 조사 범위**다 — A0가 찾아낸 신규 대상도
// 계층 A 호출 대상이므로 격자에 들어온다(§4 "A0 신규 대상은 편입 시점에
// append"). 종전 RunExamine은 Triage 3원천만 scope로 썼다.
func TestRunScreenGridScopeUsesFrozenTargets(t *testing.T) {
	tri, err := Triage(context.Background(), sampleSeed(), sampleMeta)
	if err != nil {
		t.Fatal(err)
	}
	s := screenerFunc(func(got TriageResult, d ScreenDeps) (ScreenResult, error) {
		if got.Symptom.TargetID != tri.Symptom.TargetID {
			t.Errorf("배터리 입력이 Triage 산출물이 아님: %+v", got.Symptom)
		}
		if d.Store == nil || d.Index == nil || d.Ledger == nil {
			t.Errorf("적재 경로 미배선: %+v", d)
		}
		return ScreenResult{
			Targets:           []string{"app-1", "db-1", "new-9"},
			DiscoveredTargets: []string{"new-9"},
			Calls: []ToolCallRecord{
				{Name: "scan_metrics", Args: []byte(`{"target":"app-1"}`)},
				{Name: "list_events", Args: []byte(`{"target":"new-9"}`)},
			},
		}, nil
	})

	r, err := RunScreen(context.Background(), tri, s, screenDeps(t))
	if err != nil {
		t.Fatalf("RunScreen: %v", err)
	}
	if want := []string{"app-1", "db-1", "new-9"}; !reflect.DeepEqual(r.Grid.Targets, want) {
		t.Fatalf("격자 대상 = %v, want %v (A0 신규 포함)", r.Grid.Targets, want)
	}
	// 칸 배정: app-1은 지표, new-9은 이벤트가 채워지고 나머지는 빈 칸.
	if got := r.Grid.Filled["app-1"]; !reflect.DeepEqual(got, []Viewpoint{ViewMetrics}) {
		t.Errorf("app-1 채움 = %v", got)
	}
	if got := r.Grid.Filled["new-9"]; !reflect.DeepEqual(got, []Viewpoint{ViewEvents}) {
		t.Errorf("new-9 채움 = %v", got)
	}
	if len(r.Grid.Empty) != 3*3-2 {
		t.Errorf("빈 칸 %d개 — 대상 3 × 관점 3 − 채움 2와 다름", len(r.Grid.Empty))
	}
}

// 조사 범위를 못 준 구현체(mock·ablation)에서는 Triage 범위로 물러선다 —
// 격자 대상이 통째로 비면 커버리지 계측 자체가 사라진다.
func TestRunScreenGridFallsBackToTriageScope(t *testing.T) {
	tri, err := Triage(context.Background(), sampleSeed(), sampleMeta)
	if err != nil {
		t.Fatal(err)
	}
	s := screenerFunc(func(TriageResult, ScreenDeps) (ScreenResult, error) {
		return ScreenResult{}, nil
	})
	r, err := RunScreen(context.Background(), tri, s, screenDeps(t))
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Grid.Targets) == 0 {
		t.Fatal("격자 대상이 비었다 — Triage 범위 폴백 미작동")
	}
	for _, want := range []string{"app-1", "db-1"} {
		found := false
		for _, got := range r.Grid.Targets {
			if got == want {
				found = true
			}
		}
		if !found {
			t.Errorf("격자에 %s 없음: %v", want, r.Grid.Targets)
		}
	}
}

func TestRunScreenRequiresScreenerAndDeps(t *testing.T) {
	if _, err := RunScreen(context.Background(), TriageResult{}, nil, screenDeps(t)); err == nil {
		t.Fatal("Screener nil인데 수락함")
	}
	s := screenerFunc(func(TriageResult, ScreenDeps) (ScreenResult, error) { return ScreenResult{}, nil })
	if _, err := RunScreen(context.Background(), TriageResult{}, s, ScreenDeps{}); err == nil {
		t.Fatal("store·index·ledger 없이 수락함 — [3] 산출이 앉을 자리가 없다")
	}
}

func TestRunScreenPropagatesError(t *testing.T) {
	boom := errors.New("백엔드 죽음")
	_, err := RunScreen(context.Background(), TriageResult{},
		screenerFunc(func(TriageResult, ScreenDeps) (ScreenResult, error) { return ScreenResult{}, boom }),
		screenDeps(t))
	if !errors.Is(err, boom) {
		t.Fatalf("배터리 오류 전파 안 됨: %v", err)
	}
}

// 1e 인계 — GateContext.RequiredWindow 주입 자리(checklist 6). 요구 창은
// [1]이 확정한 인시던트 창이며, 배터리가 도구를 부른 창과 같아야 한다.
func TestGateRequiredWindowIsIncidentWindow(t *testing.T) {
	tri, err := Triage(context.Background(), sampleSeed(), sampleMeta)
	if err != nil {
		t.Fatal(err)
	}
	w := GateRequiredWindow(tri)
	if w == nil {
		t.Fatal("요구 창이 nil — 전 레코드가 weak_support로 강등된다(1e)")
	}
	if !w.From.Equal(tri.From) || !w.To.Equal(tri.To) {
		t.Fatalf("요구 창 %v~%v ≠ 인시던트 창 %v~%v", w.From, w.To, tri.From, tri.To)
	}
	// 게이트에 실리는 경로의 계약: GateContext.RequiredWindow 자리에 이
	// 값이 그대로 들어간다(실 호출은 §14-5 몫이라 자리와 값만 고정한다).
	gc := ledger.GateContext{RequiredWindow: w}
	if gc.RequiredWindow == nil || !gc.RequiredWindow.From.Equal(tri.From) {
		t.Fatalf("GateContext 주입 값이 인시던트 창과 다름: %+v", gc.RequiredWindow)
	}
	// 배터리 레코드는 이 창 전체로 조회된다(WindowClass=full) — 레코드 창이
	// 요구 창을 못 덮으면 게이트가 강등하므로 둘이 같아야 한다.
	rec := findingRec("app-1", evidence.StatusAnomalous, "cpu")
	rec.Window.From, rec.Window.To = tri.From, tri.To
	if rec.Window.From.After(w.From) || rec.Window.To.Before(w.To) {
		t.Fatal("배터리 조회 창이 요구 창을 못 덮는다 — 전 레코드가 창 커버 실패한다")
	}
	// 창을 모르면 nil이다 — 모르는 것을 지어내지 않는다(fail-closed).
	if GateRequiredWindow(TriageResult{}) != nil {
		t.Fatal("창 없는 Triage에서 요구 창을 지어냄")
	}
}
