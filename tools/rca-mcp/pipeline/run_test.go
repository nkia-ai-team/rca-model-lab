package pipeline

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// runClock — 호출마다 1초 흐르는 가짜 시계 (이벤트 시각 결정론).
func runClock() func() time.Time {
	t := tr0.Add(time.Hour)
	return func() time.Time { t = t.Add(time.Second); return t }
}

// 구 [5] 대역(scriptedInv·passVerifier)은 §14-4 4c에서 삭제됐다 — 주입 자리가
// VerifyLoop 하나로 바뀌었고 그 대역은 stubVerify다.

func rstep(i int) *int { return &i }

// runIdentity·runPreds — 신 가설 계약(§6)의 후보 서식을 mock 출처가 갖추는
// 자리다. 등록 규칙(§6.2)이 실제로 걸리므로 walking skeleton도 어휘 안의
// 술어·결박된 개설 근거를 내야 한다.
func runIdentity(cause string) ledger.HypoIdentity {
	return ledger.HypoIdentity{CauseEntity: cause, Mechanism: "포화",
		TemporalClaim: ledger.TemporalPrecedes}
}

// runPreds는 그 대상의 index 레코드 하나를 개설 근거로 결박하고(selector
// 일치), 미실측 necessary 술어 하나를 덧붙인다(등록 규칙 3).
func runPreds(t *testing.T, in GenerateInput, target string) ([]ledger.SignalPred, []string) {
	t.Helper()
	for _, r := range in.Evidence.Active() {
		// scan_metrics 레코드로 좁힌다 — [2] 합성 봉투의 change 레코드는
		// 허용 Predicate가 존재형뿐이라 direction_up 술어가 규칙 1ⓓ에서 죽는다.
		if r.TargetID != target || r.RecordKind != evidence.KindFinding ||
			r.Provenance.Source != evidence.SrcScanMetrics {
			continue
		}
		k := r.Key()
		return []ledger.SignalPred{
			{Tool: k.Source, TargetID: k.TargetID, Aspect: k.Aspect, Metric: k.Metric,
				EntityKey: k.EntityKey, Window: k.Window, EIDHint: r.EID,
				Predicate: evidence.PredDirectionUp, Expectation: evidence.ExpectMustHold,
				Role: ledger.RoleCorroborating},
			{Tool: evidence.SrcScanMetrics, TargetID: target, Aspect: evidence.AspectMetric,
				Metric: "mem.util", Window: evidence.WindowFull,
				Predicate: evidence.PredDirectionUp, Expectation: evidence.ExpectMustHold,
				Role: ledger.RoleNecessary},
		}, []string{r.EID}
	}
	t.Fatalf("%s의 index 레코드가 없다 — 개설 근거를 결박할 수 없다", target)
	return nil, nil
}

// walking skeleton 전체 보행: 예시 사건(주문 API 지연, 진범 db-1 인덱스
// 정책 배포)을 seed→[1]~[6]→리포트까지 mock으로 걷는다. 단계 간 배선
// (산출물이 다음 단계 책상에 오르는가)과 최종 confirmed 리포트를 본다.
func TestRunWalkingSkeleton(t *testing.T) {
	now := runClock()

	getChanges := func(ctx context.Context, target string, from, to time.Time) ([]ChangeEvent, error) {
		if target == "db-1" {
			return []ChangeEvent{{Kind: "policy_deploy", At: trTs(-20), TargetID: "db-1", Detail: "인덱스 정책 배포"}}, nil
		}
		return nil, nil
	}

	// [3] mock 배터리 — 산출은 index다(스펙 §4). stor-1의 이상 관측 하나를
	// 실제로 index에 싣는다: [4] 책상·의무 계산·잔여 계측이 전부 이 레코드를
	// 읽으므로, 종전 ExamineResult{Findings:…} 한 건과 같은 자리다.
	screener := screenerFunc(func(tri TriageResult, d ScreenDeps) (ScreenResult, error) {
		rec := findingRec("stor-1", evidence.StatusAnomalous, "io_wait")
		rec.Window.From, rec.Window.To = trTs(0), trTs(10)
		if _, err := d.Index.Append(rec); err != nil {
			t.Fatalf("index 적재: %v", err)
		}
		for _, id := range []string{"app-1", "db-1", "host-9", "net-1"} {
			ok := findingRec(id, evidence.StatusNormal, "cpu")
			if _, err := d.Index.Append(ok); err != nil {
				t.Fatalf("index 적재: %v", err)
			}
		}
		return ScreenResult{Targets: []string{"app-1", "db-1", "host-9", "net-1", "stor-1"}}, nil
	})

	// 출처 2개 — 각자 자기 몫의 책상([2]·[3] 산출물)이 배선됐는지도 확인.
	changeSrc := sourceFunc(func(in GenerateInput) ([]Candidate, error) {
		if len(in.Changes.Changes) != 1 {
			t.Errorf("변경 이력 출처의 책상에 [2] 산출물이 없음: %+v", in.Changes)
		}
		preds, eids := runPreds(t, in, "db-1")
		return []Candidate{{
			TargetID: "db-1",
			Chain: []ledger.ChainStep{
				{Entity: "db-1", Effect: "인덱스 정책 배포 후 실행 계획 악화"},
				{Entity: "app-1", Effect: "응답 지연"},
			},
			Source: ledger.SourceChange, Prior: ledger.PriorHigh,
			PriorRationale:   "시간창 직전 배포",
			IdentityKey:      runIdentity("db-1"),
			PredictedSignals: preds, SupportEIDs: eids,
		}}, nil
	})
	investSrc := sourceFunc(func(in GenerateInput) ([]Candidate, error) {
		// [3] 산출물이 책상에 오르는가 — 이제 index다.
		if in.Evidence == nil {
			t.Fatal("조사 출처의 책상에 [3] index가 없음")
		}
		bySource := map[evidence.ObservationSource]int{}
		for _, rec := range in.Evidence.Active() {
			if rec.RecordKind == evidence.KindFinding &&
				rec.Quality.Status == evidence.StatusAnomalous {
				bySource[rec.Provenance.Source]++
			}
		}
		// [3] 스크리닝의 이상 관측 1건(stor-1).
		if bySource[evidence.SrcScanMetrics] != 1 {
			t.Errorf("스크리닝 anomalous = %d건, want 1건", bySource[evidence.SrcScanMetrics])
		}
		// **B-15 실물**: [2] 합성 봉투의 변경 레코드도 같은 index에 앉는다 —
		// 원천은 change이고(실행 채널 list_changes가 아니다) [4]는 이것을
		// 스크리닝 관측과 같은 모양으로 본다.
		if bySource[evidence.SrcChange] != 1 {
			t.Errorf("change 레코드 = %d건, want 1건(db-1 정책 배포)", bySource[evidence.SrcChange])
		}
		preds, eids := runPreds(t, in, "stor-1")
		return []Candidate{{
			TargetID: "stor-1",
			Chain: []ledger.ChainStep{
				{Entity: "stor-1", Effect: "IO 지연"},
				{Entity: "app-1", Effect: "응답 지연"},
			},
			Source: ledger.SourceInvestigation, Prior: ledger.PriorMedium,
			PriorRationale:   "조용한 이웃에서 이상 발견",
			IdentityKey:      runIdentity("stor-1"),
			PredictedSignals: preds, SupportEIDs: eids,
		}}, nil
	})
	grouper := grouperFunc(func(c []Candidate) ([][]int, error) {
		return [][]int{{0}, {1}}, nil
	})

	// [5] 자리 — 배선 대역. 신 루프의 행동은 loop 패키지 시험이 지고,
	// 여기서는 책상이 넘어오는가와 산출이 [6]에 오르는가만 본다(§14-4 4b).
	verify := &stubVerify{adopt: "H1"}

	// 작문 자리 — 책상에 조립 완료 리포트가 올라오는지 배선을 본다.
	writer := writerFunc(func(in WriteInput) (Prose, error) {
		if in.Title != "주문 API 응답 지연" || in.Rca.Status != "confirmed" || in.Rca.Cause.TargetName != "주문DB" {
			t.Errorf("작문가의 책상이 비었음: title=%q status=%q cause=%+v", in.Title, in.Rca.Status, in.Rca.Cause)
		}
		if in.Targets["db-1"] != "주문DB" {
			t.Errorf("표시명 표 미배선: %v", in.Targets)
		}
		return Prose{Headline: "주문DB 인덱스 정책 배포로 주문 API 지연", SymptomText: "s",
			ProblemText: "p", DiagnosisSummary: "d", ConclusionSummary: "c"}, nil
	})

	deps := screenDeps(t)
	r, err := Run(context.Background(), sampleSeed(), Config{
		Meta: sampleMeta, GetChanges: getChanges, Lookback: time.Hour,
		Store: deps.Store, Index: deps.Index,
		Screener: screener,
		Sources:  []CandidateSource{changeSrc, investSrc}, Grouper: grouper,
		Gen:         GenerateConfig{Now: now, Auditor: passAuditor{}},
		Verify:      verify,
		Writer:      writer,
		RequestedAt: trTs(15),
	})
	if err != nil {
		t.Fatalf("Run 실패: %v", err)
	}

	// [4]까지의 배선: 개설 2건, H1=변경 출처 db-1.
	if len(r.Opened) != 2 || r.Opened[0].TargetID != "db-1" || r.Opened[1].TargetID != "stor-1" {
		t.Fatalf("opened = %+v", r.Opened)
	}
	// [5] 판정: confirmed, H1 채택, 경쟁자 반증.
	if r.Decision.Status != ledger.StatusConfirmed || r.Decision.AdoptedID != "H1" {
		t.Fatalf("confirmed/H1 기대, got %s/%s (%s)", r.Decision.Status, r.Decision.AdoptedID, r.Decision.Reason)
	}
	// [6] 리포트: 원인 = db-1, 차원까지 채워진 채 추출.
	if r.Rca.Cause.TargetID != "db-1" {
		t.Errorf("Rca.Cause.TargetID = %q, want db-1", r.Rca.Cause.TargetID)
	}
	if !r.Ledger.Terminated() {
		t.Error("수첩이 종료 상태가 아님")
	}
	// [6] 작문: 산문이 리포트 자리에 앉고, seed 원천 필드도 채워졌다.
	if r.Rca.Headline == "" || r.UI.ConclusionSummary != "c" {
		t.Errorf("산문 미적용: headline=%q conclusion=%q", r.Rca.Headline, r.UI.ConclusionSummary)
	}
	if r.Rca.Symptom.Metric != "http_latency_p99" || r.UI.Alarm.Target != "주문API" {
		t.Errorf("seed 필드 미전사: symptom=%+v alarm=%+v", r.Rca.Symptom, r.UI.Alarm)
	}
	// 이상 대상(app-1·db-1·stor-1)이 전부 가설에 등장 — 잔여 없음(§14 ①).
	if len(r.UnexplainedAnomalies) != 0 {
		t.Errorf("UnexplainedAnomalies = %v, want 없음", r.UnexplainedAnomalies)
	}
	// 장부 정본: 전 과정이 재생 가능해야 한다.
	if _, err := ledger.Replay(r.Ledger.Events()); err != nil {
		t.Errorf("history 재생 실패: %v", err)
	}
}

// 후보가 하나도 안 나오면 **run이 실패한다**(§6.2-6, 3b): 반려 사유
// 피드백으로 재생성 1회를 돌리고 그래도 0이면 hypothesis_admission_failure다
// — 가설 없이 루프는 정의되지 않는다. 종전 시험은 "가설 0개면 루프가
// insufficient로 종료"를 단언했는데, 그 경로가 계약에서 사라졌다.
func TestRunNoCandidates(t *testing.T) {
	now := runClock()
	empty := sourceFunc(func(GenerateInput) ([]Candidate, error) { return nil, nil })
	deps := screenDeps(t)
	r, err := Run(context.Background(), sampleSeed(), Config{
		Meta:       sampleMeta,
		GetChanges: func(context.Context, string, time.Time, time.Time) ([]ChangeEvent, error) { return nil, nil },
		Lookback:   time.Hour,
		Store:      deps.Store, Index: deps.Index,
		Screener: screenerFunc(func(TriageResult, ScreenDeps) (ScreenResult, error) {
			return ScreenResult{}, nil
		}),
		Sources: []CandidateSource{empty},
		Grouper: grouperFunc(func([]Candidate) ([][]int, error) { return nil, nil }),
		Gen:     GenerateConfig{Now: now, Auditor: passAuditor{}},
		Verify:  &stubVerify{},
	})
	var fail *AdmissionFailureError
	if !errors.As(err, &fail) {
		t.Fatalf("admission failure 기대, got err=%v opened=%d", err, len(r.Opened))
	}
	if fail.Reason() != ledger.ReasonHypothesisAdmissionFailure {
		t.Errorf("§15.5 enum = %s", fail.Reason())
	}
	if fail.Rounds != 2 {
		t.Errorf("라운드 = %d, want 2 (재생성 1회)", fail.Rounds)
	}
}

func TestRunRequiresLoopRoles(t *testing.T) {
	if _, err := Run(context.Background(), sampleSeed(), Config{Meta: sampleMeta}); err == nil {
		t.Fatal("[5] 루프 없이 수락함")
	}
}

// [3] 산출이 index이므로 적재 경로 없이는 run이 성립하지 않는다 —
// 조용히 "관측 0건"으로 완주하면 "아무 이상 없다"와 구분되지 않는다.
func TestRunRequiresEvidenceStoreAndIndex(t *testing.T) {
	_, err := Run(context.Background(), sampleSeed(), Config{
		Meta: sampleMeta, Verify: &stubVerify{},
	})
	if err == nil {
		t.Fatal("store·index 없이 수락함")
	}
}

// unexplainedAnomalies는 change 레코드를 이상 관측으로 세지 않는다(2c
// 독립 검증 Gap 3) — 변경 이벤트는 [2]의 관측이지 [3]의 이상 관측이
// 아니다(구 계약: triage anomaly + [3] anomalous 발견).
func TestUnexplainedAnomaliesIgnoreChangeRecords(t *testing.T) {
	ix := evidence.NewIndex()
	rec := findingRec("db-1", evidence.StatusAnomalous, "change")
	rec.Aspect = evidence.AspectChange
	rec.FindingClass = "change"
	rec.Effect = evidence.Effect{Kind: evidence.EffectCount, Metric: "change", Direction: evidence.DirNA}
	rec.Provenance.Source = evidence.SrcChange
	if _, err := ix.Append(rec); err != nil {
		t.Fatalf("change 레코드 적재: %v", err)
	}
	scan := findingRec("app-1", evidence.StatusAnomalous, "cpu")
	if _, err := ix.Append(scan); err != nil {
		t.Fatalf("scan 레코드 적재: %v", err)
	}
	got := unexplainedAnomalies(TriageResult{}, ix, nil)
	if len(got) != 1 || got[0] != "app-1" {
		t.Fatalf("[3] anomalous만 남아야 함(app-1): %v", got)
	}
}

// D-1(6c 검증): 리포트 전문 스캔 — 산문 5필드 밖의 LLM 자유 서술
// (Cause.Text 등)도 걸린다.
func TestMaskReportCoversFreeTextBeyondProse(t *testing.T) {
	rca := ledger.RcaResult{}
	rca.Cause.Text = "덤프 password=LeakMe1 경유"
	rca.Alternatives = []ledger.RcaAlternative{{Text: `token: "sk-leak2"`}}
	ui := ledger.UIReport{}
	ui.Hypotheses = []ledger.UIHypothesis{{Description: "정상 서술"}}
	masked := maskReport(&rca, &ui)
	if len(masked) != 2 {
		t.Fatalf("마스킹 필드 %v — cause_text·alternatives[0].text여야 함", masked)
	}
	if rca.Cause.Text != "덤프 password=[MASKED] 경유" {
		t.Fatalf("cause_text: %q", rca.Cause.Text)
	}
	if ui.Hypotheses[0].Description != "정상 서술" {
		t.Fatal("무관 필드가 변형됨")
	}
}
