package pipeline

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

type writerFunc func(WriteInput) (Prose, error)

func (f writerFunc) Write(ctx context.Context, in WriteInput) (Prose, error) { return f(in) }

func TestFillSeedFields(t *testing.T) {
	s := sampleSeed()
	// 대표 이벤트를 멤버에 연결 — At은 대표 멤버의 발생 시각이어야 한다.
	s.MainEventID = "ev-main"
	s.Members[0].EventID = "ev-main" // OccurredAt = trTs(2)
	s.BlastServices = []string{"order"}
	s.BlastTargetIDs = []string{"app-1"}

	tri, err := Triage(context.Background(), s, sampleMeta)
	if err != nil {
		t.Fatalf("Triage 실패: %v", err)
	}
	rca := ledger.RcaResult{Cause: ledger.RcaCause{TargetID: "db-1"}}
	var ui ledger.UIReport
	glossary, err := fillSeedFields(context.Background(), s, tri, sampleMeta, &rca, &ui)
	if err != nil {
		t.Fatalf("fillSeedFields 실패: %v", err)
	}

	if rca.Symptom.Metric != "http_latency_p99" || rca.Symptom.MainEventRef != "ev-main" {
		t.Errorf("symptom = %+v", rca.Symptom)
	}
	if rca.Symptom.At == nil || !rca.Symptom.At.Equal(trTs(2)) {
		t.Errorf("Symptom.At = %v, want 대표 멤버 발생 시각 %v", rca.Symptom.At, trTs(2))
	}
	if rca.Problem.Severity != "critical" || len(rca.Problem.Impact.Services) != 1 {
		t.Errorf("problem = %+v", rca.Problem)
	}
	if rca.Cause.TargetName != "주문DB" {
		t.Errorf("Cause.TargetName = %q, want 주문DB", rca.Cause.TargetName)
	}
	if ui.Alarm.ID != "inc-1" || ui.Alarm.Name != "주문 API 응답 지연" || ui.Alarm.Target != "주문API" {
		t.Errorf("alarm = %+v", ui.Alarm)
	}
	if ui.Alarm.OccurredAt == nil || !ui.Alarm.OccurredAt.Equal(trTs(0)) {
		t.Errorf("Alarm.OccurredAt = %v", ui.Alarm.OccurredAt)
	}
	// 표시명 표 — 조사에 등장한 대상(topology 이웃 포함)까지 담긴다.
	if glossary["db-1"] != "주문DB" || glossary["stor-1"] != "스토리지" {
		t.Errorf("glossary = %v", glossary)
	}
	if _, ok := glossary["cache-1"]; ok {
		t.Errorf("미해석 대상이 표에 들어감: %v", glossary)
	}
}

// 대표 이벤트가 멤버에 없으면 At은 이벤트 시간창 시작으로 물러난다.
// 표시명 미해석 대상은 id 그대로 남는다(오류 아님).
func TestFillSeedFieldsFallbacks(t *testing.T) {
	s := sampleSeed()
	s.MainTargetID = "cache-1" // 메타에 없는 대상

	tri, err := Triage(context.Background(), s, sampleMeta)
	if err != nil {
		t.Fatalf("Triage 실패: %v", err)
	}
	var rca ledger.RcaResult
	var ui ledger.UIReport
	if _, err := fillSeedFields(context.Background(), s, tri, sampleMeta, &rca, &ui); err != nil {
		t.Fatalf("fillSeedFields 실패: %v", err)
	}
	if rca.Symptom.At == nil || !rca.Symptom.At.Equal(trTs(0)) {
		t.Errorf("Symptom.At = %v, want FirstEventAt %v", rca.Symptom.At, trTs(0))
	}
	if ui.Alarm.Target != "cache-1" {
		t.Errorf("Alarm.Target = %q, want id 그대로", ui.Alarm.Target)
	}
	if rca.Cause.TargetName != "" {
		t.Errorf("원인 없는데 TargetName = %q", rca.Cause.TargetName)
	}
}

// Writer 실패는 run 실패다(§15.5 관용 폐기, 5c) — 구 WriterErr 기록 관용은
// "실패를 리포트처럼 꾸미지 마라"(사용자 결정)로 소멸했다.
func TestRunWriterFailureIsFatal(t *testing.T) {
	now := runClock()
	narrateDeps := screenDeps(t)
	// [4]가 가설 0개로 끝나면 run 자체가 실패한다(§6.2-6, 3b) — 작문 자리를
	// 보려면 개설되는 가설이 하나는 있어야 한다. 그래서 조사 범위에 db-1을
	// 두고 그 대상의 관측 레코드 하나(개설 근거)를 index에 앉힌다.
	narrateEID, err := narrateDeps.Index.Append(admRec("db-1", "cpu.util", evidence.WindowFull))
	if err != nil {
		t.Fatalf("index 적재: %v", err)
	}
	r, err := Run(context.Background(), sampleSeed(), Config{
		Meta:       sampleMeta,
		GetChanges: func(context.Context, string, time.Time, time.Time) ([]ChangeEvent, error) { return nil, nil },
		Lookback:   time.Hour,
		Store:      narrateDeps.Store, Index: narrateDeps.Index,
		Screener: screenerFunc(func(TriageResult, ScreenDeps) (ScreenResult, error) {
			return ScreenResult{Targets: []string{"db-1"}}, nil
		}),
		Sources: []CandidateSource{sourceFunc(func(GenerateInput) ([]Candidate, error) {
			return []Candidate{auditCand(narrateEID)}, nil
		})},
		Grouper: grouperFunc(func(c []Candidate) ([][]int, error) { return [][]int{{0}}, nil }),
		Gen:     GenerateConfig{Now: now, Auditor: passAuditor{}},
		Verify:  &stubVerify{},
		Writer:  writerFunc(func(WriteInput) (Prose, error) { return Prose{}, errors.New("작문 불능") }),
	})
	if err == nil {
		t.Fatal("작문 실패가 run 실패로 승격되지 않았다(§15.5)")
	}
	if !strings.Contains(err.Error(), "작문 실패") {
		t.Errorf("실패 사유가 작문이 아님: %v", err)
	}
	// 실패 run은 결과를 내지 않는다 — 부분 리포트가 손에 남으면 그게 곧
	// 강등 리포트다(§15.5).
	if r.Rca.Headline != "" || r.UI.ConclusionSummary != "" {
		t.Errorf("실패 run이 산문을 남겼다: %+v", r.Rca.Headline)
	}
}

// §8.1-4 — 위반은 피드백 재시도 1회로 회복되고, 재위반은 run 실패다.
func TestRunCiteGateRetryThenFail(t *testing.T) {
	build := func(second Prose, wantErr bool) {
		t.Helper()
		now := runClock()
		deps := screenDeps(t)
		eid, err := deps.Index.Append(admRec("db-1", "cpu.util", evidence.WindowFull))
		if err != nil {
			t.Fatalf("index 적재: %v", err)
		}
		calls := 0
		var feedbacks []string
		r, err := Run(context.Background(), sampleSeed(), Config{
			Meta:       sampleMeta,
			GetChanges: func(context.Context, string, time.Time, time.Time) ([]ChangeEvent, error) { return nil, nil },
			Lookback:   time.Hour,
			Store:      deps.Store, Index: deps.Index,
			Screener: screenerFunc(func(TriageResult, ScreenDeps) (ScreenResult, error) {
				return ScreenResult{Targets: []string{"db-1"}}, nil
			}),
			Sources: []CandidateSource{sourceFunc(func(GenerateInput) ([]Candidate, error) {
				return []Candidate{auditCand(eid)}, nil
			})},
			Grouper: grouperFunc(func(c []Candidate) ([][]int, error) { return [][]int{{0}}, nil }),
			Gen:     GenerateConfig{Now: now, Auditor: passAuditor{}},
			Verify:  &stubVerify{},
			Writer: writerFunc(func(in WriteInput) (Prose, error) {
				calls++
				feedbacks = append(feedbacks, in.Feedback)
				if calls == 1 {
					return Prose{Headline: "원인은 EIX-9999다", SymptomText: "s",
						ProblemText: "p", DiagnosisSummary: "d", ConclusionSummary: "c"}, nil
				}
				return second, nil
			}),
		})
		if calls != 2 {
			t.Fatalf("재시도 1회 계약 위반 — 호출 %d회", calls)
		}
		if feedbacks[0] != "" || feedbacks[1] == "" {
			t.Fatalf("피드백 배선 이상: %q", feedbacks)
		}
		if wantErr {
			if err == nil {
				t.Fatal("재위반이 run 실패로 승격되지 않았다")
			}
			return
		}
		if err != nil {
			t.Fatalf("회복 경로가 실패했다: %v", err)
		}
		if r.Rca.Headline != "정상 헤드라인" {
			t.Fatalf("회복 산문 미적용: %q", r.Rca.Headline)
		}
	}
	// 재시도가 깨끗하면 성공.
	build(Prose{Headline: "정상 헤드라인", SymptomText: "s", ProblemText: "p",
		DiagnosisSummary: "d", ConclusionSummary: "c"}, false)
	// 재시도도 위반이면 run 실패.
	build(Prose{Headline: "여전히 EIX-9999", SymptomText: "s", ProblemText: "p",
		DiagnosisSummary: "d", ConclusionSummary: "c"}, true)
}
