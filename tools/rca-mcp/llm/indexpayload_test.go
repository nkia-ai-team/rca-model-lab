package llm

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// smokeRecord는 유효한 finding 레코드 하나다(§5.1 Validate 통과 최소 형태).
func smokeRecord(target string, status evidence.Status, headline string) evidence.EvidenceIndexRecord {
	r := evidence.EvidenceIndexRecord{
		TargetID: target, RecordKind: evidence.KindFinding,
		Aspect: evidence.AspectMetric, FindingClass: "shifted",
		Headline: headline,
		Effect: evidence.Effect{
			Kind: evidence.EffectRatio, Metric: "m", Direction: evidence.DirUp,
		},
		Window:     evidence.TimeWindow{Class: evidence.WindowFull},
		Quality:    evidence.Quality{Status: status, Confidence: evidence.ConfOK},
		Provenance: evidence.Provenance{Source: evidence.SrcScanMetrics, EnvelopeRef: "EST-0001:scan_metrics"},
	}
	r.Quality.Availability = evidence.AvailObserved
	if status == evidence.StatusNoData {
		// 수집 공백 — §5.3-2가 "전량 유지"로 지목한 사유다.
		r.Quality.NoDataReason = evidence.NoDataCollectorGap
		r.Quality.Availability = evidence.AvailMissing
	}
	return r
}

func mustIndex(t *testing.T, recs ...evidence.EvidenceIndexRecord) *evidence.Index {
	t.Helper()
	ix := evidence.NewIndex()
	for _, r := range recs {
		if _, err := ix.Append(r); err != nil {
			t.Fatalf("index 적재: %v", err)
		}
	}
	return ix
}

// §5.3의 계층 분류 — 1·2·3은 레코드로 실리고 4(normal+ok)는 **항상** 접혀
// 롤업 카운트로만 남는다. 조용한 이웃 딱지는 규칙이 계산한다.
func TestIndexPayloadTiers(t *testing.T) {
	low := smokeRecord("t-low", evidence.StatusNormal, "정상이나 표본 부족")
	low.Quality.Confidence = evidence.ConfLow
	zero := smokeRecord("t-zero", evidence.StatusNoData, "창 내 0건")
	zero.Quality.NoDataReason = evidence.NoDataZeroObservations
	zero.Quality.Availability = evidence.AvailObservedZero
	na := smokeRecord("t-na", evidence.StatusNoData, "이 대상엔 미적용")
	na.Quality.NoDataReason = evidence.NoDataNotCollected
	na.Quality.Availability = evidence.AvailNotApplicable

	ix := mustIndex(t,
		smokeRecord("t-anom", evidence.StatusAnomalous, "이상"),
		smokeRecord("t-gap", evidence.StatusNoData, "수집 공백"),
		zero, low, na,
		smokeRecord("t-ok", evidence.StatusNormal, "정상"),
	)
	p := buildIndexPayload(ix, map[string]bool{"t-anom": true})

	got := map[string]recordPayload{}
	for _, r := range p.Records {
		got[r.TargetID] = r
	}
	for _, want := range []string{"t-anom", "t-gap", "t-zero", "t-low"} {
		if _, ok := got[want]; !ok {
			t.Errorf("%s 레코드가 실리지 않았다(§5.3 1~3 전량 유지)", want)
		}
	}
	if _, ok := got["t-ok"]; ok {
		t.Error("normal+ok가 레코드로 실렸다 — §5.3-4는 롤업 카운트로만")
	}
	if _, ok := got["t-na"]; ok {
		t.Error("not_applicable이 우선순위 표에 없는데 레코드로 실렸다")
	}
	if p.Truncation.FoldedNormalOK != 1 || p.Truncation.FoldedOther != 1 {
		t.Errorf("접힌 수 기록 어긋남: %+v", p.Truncation)
	}
	// 조용한 이웃 = 증상 멤버가 아닌 대상(종전 Finding.Quiet의 자리).
	if got["t-anom"].Quiet {
		t.Error("증상 멤버가 quiet=true")
	}
	if !got["t-gap"].Quiet {
		t.Error("증상 밖 대상이 quiet=false")
	}
	// 접힌 레코드도 롤업에는 남는다 — "접혔다 = 없다"가 아니다.
	if len(p.Rollups) != 6 {
		t.Errorf("롤업 행 %d개, want 6(대상 전수)", len(p.Rollups))
	}
}

// **B-15 실물 기록**: [2] 합성 봉투의 change 레코드가 [4] 직렬화에서 어떻게
// 보이는가. 판정(등록 규칙에서 change 술어를 기실측 전용으로 볼지)은 §14-3
// 몫이고, 여기서는 사실만 고정한다 —
//
//	source="change"로 보인다(실행 채널 list_changes는 payload에 없다).
//	나머지 열은 스크리닝 레코드와 **완전히 같은 모양**이다.
func TestIndexPayloadChangeRecordShape(t *testing.T) {
	ch := smokeRecord("db-1", evidence.StatusAnomalous, "policy_deploy — 인덱스 정책 배포")
	ch.Aspect = evidence.AspectChange
	ch.FindingClass = "change"
	ch.Effect = evidence.Effect{Kind: evidence.EffectCount, Metric: "change", Direction: evidence.DirNA}
	ch.Provenance.Source = evidence.SrcChange
	ch.EntityKey = "policy_deploy|db-1|2026-08-03T02:00:00Z"

	p := buildIndexPayload(mustIndex(t, ch), map[string]bool{})
	if len(p.Records) != 1 {
		t.Fatalf("레코드 %d건", len(p.Records))
	}
	r := p.Records[0]
	if r.Source != string(evidence.SrcChange) {
		t.Fatalf("source = %q, want change", r.Source)
	}
	b, err := json.Marshal(r)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(b), "list_changes") {
		t.Fatalf("실행 채널 이름이 [4] 입력에 새어 나왔다: %s", b)
	}
	// 기록: 이 모양이 §14-3 판정의 입력이다.
	t.Logf("B-15 change 레코드의 [4] 직렬화: %s", b)
}

// §5.3-5·6 — 예산 초과 시 4→3 순서로 접고, anomalous만으로도 초과하면
// 2차 정렬 후 꼬리를 **축약**한다(삭제가 아니다). 절단 사실은 수로 남는다.
func TestIndexPayloadBudgetTruncation(t *testing.T) {
	fat := strings.Repeat("가", 110) // Headline 상한 120자 안
	var recs []evidence.EvidenceIndexRecord
	for i := 0; i < 80; i++ {
		r := smokeRecord(fmt.Sprintf("t-%03d", i), evidence.StatusAnomalous, fat)
		mag := float64(80 - i)
		r.Effect.Magnitude = &mag
		recs = append(recs, r)
	}
	// 증상 멤버 하나를 **가장 작은 magnitude**로 꼬리에 둔다 — 2차 정렬 ①이
	// 이기면 축약되지 않아야 한다.
	member := smokeRecord("t-member", evidence.StatusAnomalous, fat)
	small := 0.1
	member.Effect.Magnitude = &small
	recs = append(recs, member)
	// normal+low도 넣는다 — 예산이 모자라면 이쪽이 먼저 접힌다(4→3 순서).
	lowRec := smokeRecord("t-low", evidence.StatusNormal, fat)
	lowRec.Quality.Confidence = evidence.ConfLow
	recs = append(recs, lowRec)

	p := buildIndexPayload(mustIndex(t, recs...), map[string]bool{"t-member": true})

	if p.Truncation.UsedChars > indexBudgetChars {
		t.Fatalf("예산 %d자를 %d자로 넘겼다", indexBudgetChars, p.Truncation.UsedChars)
	}
	if p.Truncation.FoldedNormalLow != 1 {
		t.Errorf("normal+low가 먼저 접혔어야 한다: %+v", p.Truncation)
	}
	if p.Truncation.AbbreviatedAnomalous == 0 {
		t.Fatal("anomalous 축약이 일어나지 않았다 — 예산을 어떻게 맞췄나")
	}
	if len(p.Records)+len(p.Abbreviated) != 81 {
		t.Fatalf("레코드가 사라졌다: 본문 %d + 축약 %d ≠ 81(anomalous 전수)",
			len(p.Records), len(p.Abbreviated))
	}
	// ① 증상 멤버 우선 — 본문 첫 줄이 t-member다.
	if p.Records[0].TargetID != "t-member" {
		t.Errorf("2차 정렬 ① 위반: 첫 레코드 = %s", p.Records[0].TargetID)
	}
	// ② Effect 서열 — 본문에 남은 나머지는 magnitude 큰 순.
	for i := 2; i < len(p.Records); i++ {
		a, b := p.Records[i-1], p.Records[i]
		if a.Effect == nil || b.Effect == nil || a.Effect.Magnitude == nil || b.Effect.Magnitude == nil {
			continue
		}
		if *a.Effect.Magnitude < *b.Effect.Magnitude {
			t.Fatalf("서열 역전: %s(%v) 뒤에 %s(%v)",
				a.TargetID, *a.Effect.Magnitude, b.TargetID, *b.Effect.Magnitude)
		}
	}
	// 축약분은 EID가 남는다 — [5]가 fetch·재조회로 회수 가능해야 한다.
	for _, a := range p.Abbreviated {
		if a.EID == "" {
			t.Fatal("축약 레코드에 EID가 없다 — 회수 불가")
		}
	}
}

// 절단 규칙은 기계다 — 같은 index에서 두 번 지으면 바이트까지 같다.
func TestIndexPayloadDeterministic(t *testing.T) {
	var recs []evidence.EvidenceIndexRecord
	for i := 0; i < 50; i++ {
		r := smokeRecord(fmt.Sprintf("t-%02d", i), evidence.StatusAnomalous, "이상")
		mag := float64(i % 5) // 동률을 일부러 만든다 — 마지막 갈래가 EID다
		r.Effect.Magnitude = &mag
		recs = append(recs, r)
	}
	ix := mustIndex(t, recs...)
	a, err := json.Marshal(buildIndexPayload(ix, map[string]bool{}))
	if err != nil {
		t.Fatal(err)
	}
	b, err := json.Marshal(buildIndexPayload(ix, map[string]bool{}))
	if err != nil {
		t.Fatal(err)
	}
	if string(a) != string(b) {
		t.Fatal("같은 index에서 payload가 갈렸다 — 절단 규칙이 비결정론")
	}
}

func TestIndexPayloadNilIndex(t *testing.T) {
	p := buildIndexPayload(nil, nil)
	if len(p.Records) != 0 || p.Truncation.BudgetChars != indexBudgetChars {
		t.Fatalf("nil index에서 %+v", p)
	}
}

// 출처별 부분 색인(3c) — keep 필터가 레코드에만 걸리고 롤업은 전 대상 유지.
func TestIndexPayloadWhereFilters(t *testing.T) {
	ix := evidence.NewIndex()
	a := smokeRecord("app-1", evidence.StatusAnomalous, "app-1 cpu 급등")
	a.Provenance.Source = evidence.SrcScanMetrics
	b := smokeRecord("db-1", evidence.StatusAnomalous, "db-1 정책 배포")
	b.Aspect, b.FindingClass = evidence.AspectChange, "change"
	b.Effect = evidence.Effect{Kind: evidence.EffectCount, Metric: "change", Direction: evidence.DirNA}
	b.Provenance.Source = evidence.SrcChange
	for _, r := range []evidence.EvidenceIndexRecord{a, b} {
		if _, err := ix.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	p := buildIndexPayloadWhere(ix, nil, func(r evidence.EvidenceIndexRecord) bool {
		return r.Provenance.Source == evidence.SrcChange
	})
	if len(p.Records) != 1 || p.Records[0].Source != string(evidence.SrcChange) {
		t.Fatalf("change 레코드만 남아야 함: %+v", p.Records)
	}
	// 시야 밖 레코드는 절단 통계에도 들지 않는다.
	if p.Truncation.FoldedOther != 0 || p.Truncation.FoldedNormalOK != 0 {
		t.Fatalf("시야 밖이 절단 통계에 섞임: %+v", p.Truncation)
	}
	if len(p.Rollups) != 2 {
		t.Fatalf("롤업은 전 대상 유지여야 함: %d", len(p.Rollups))
	}
}
