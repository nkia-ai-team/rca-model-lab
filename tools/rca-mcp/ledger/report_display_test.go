package ledger

import (
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// §15.4-1 전제 블록 — clock unknown·exceeded면 "시간 판정 미사용"이
// 사실대로 실리고(4차 A-13 — 폐기된 "≤2s 가정" 면책 문구 재발 방지),
// §8.2-5 고정 문구가 항상 실린다.
func TestAssemblePremisesClockHonesty(t *testing.T) {
	cases := []struct {
		skew evidence.ClockSkew
		want string
	}{
		{evidence.ClockSkew{Status: evidence.TagObserved, BoundS: 2}, "used"},
		{evidence.ClockSkew{Status: evidence.TagUnknown}, "unused_clock_unknown"},
		{evidence.ClockSkew{Status: evidence.TagExceeded, BoundS: 99}, "unused_clock_exceeded"},
	}
	for _, c := range cases {
		p := assemblePremises(ReportContext{ClockSkew: c.skew, BaselineWindow: "w", ReplaySource: "seed_file:x"})
		if p.TemporalJudgement != c.want {
			t.Errorf("skew %s → %s (기대 %s)", c.skew.Status, p.TemporalJudgement, c.want)
		}
		if p.ConfidenceNote != ConfidenceNote || !strings.Contains(p.ConfidenceNote, "정답 확률이 아니다") {
			t.Errorf("§8.2-5 고정 문구 누락: %q", p.ConfidenceNote)
		}
	}
	// 빈 재료는 unknown으로 정직하다(fail-closed).
	if p := assemblePremises(ReportContext{}); p.TemporalJudgement != "unused_clock_unknown" {
		t.Errorf("빈 ClockSkew가 %s — unknown이어야 함", p.TemporalJudgement)
	}
}

// §15.4-2 한계 블록 — widening 도달 단계는 마지막 실행 칸, 백엔드 결손은
// backend_error 레코드에서 기계 산출, index 축약은 not_measured 정직 표기.
func TestAssembleLimitations(t *testing.T) {
	l := &Ledger{events: []Event{
		{Payload: WideningExecuted{Step: WideningW1}},
		{Payload: WideningExecuted{Step: WideningW2}},
	}}
	ix := evidence.NewIndex()
	rec := displayRecord()
	rec.Effect = evidence.Effect{Kind: evidence.EffectCategorical, Direction: evidence.DirUp}
	rec.Quality = evidence.Quality{Status: evidence.StatusNoData,
		NoDataReason: evidence.NoDataBackendError, Availability: evidence.AvailMissing,
		Confidence: evidence.ConfLow}
	if _, err := ix.Append(rec); err != nil {
		t.Fatalf("append: %v", err)
	}
	// guard 경로의 공허 backend_error는 KindMeta 합성 레코드로 온다
	// (§14-7 ③ 안 A) — BackendGaps는 kind 무관하게 사유로 긁어야 한다.
	meta := evidence.EvidenceIndexRecord{
		TargetID: "svc-2", Domain: "application",
		Aspect: evidence.AspectDB, RecordKind: evidence.KindMeta,
		Headline: "db_slow_queries backend_error(ch/http_5xx) — 관측 백엔드 결손",
		Quality: evidence.Quality{Status: evidence.StatusNoData,
			NoDataReason: evidence.NoDataBackendError, Availability: evidence.AvailMissing,
			Confidence: evidence.ConfOK},
		Provenance: evidence.Provenance{Source: evidence.SrcDBSlowQueries, EnvelopeRef: "ref-2"},
	}
	if _, err := ix.Append(meta); err != nil {
		t.Fatalf("meta append: %v", err)
	}
	lim := assembleLimitations(l, ReportContext{Index: ix})
	if lim.WideningStage != string(WideningW2) {
		t.Errorf("widening 단계 %q — 마지막 실행 칸 W2여야 함", lim.WideningStage)
	}
	if len(lim.BackendGaps) != 2 || !strings.Contains(lim.BackendGaps[0], "svc-1") ||
		!strings.Contains(lim.BackendGaps[1], "svc-2") {
		t.Errorf("백엔드 결손 %v — finding·meta 양쪽이 실려야 한다", lim.BackendGaps)
	}
	if lim.IndexReduction != "not_measured" {
		t.Errorf("index 축약 %q — 누적기 미배선(nil)이면 not_measured여야 함", lim.IndexReduction)
	}
}

// §5.3 payload 축약 계측(§14-7) — nil/무조립=not_measured, 축약 0=none,
// 발생 시 조립 수·최대치가 문구에 실린다.
func TestIndexTruncStats(t *testing.T) {
	var nilStats *IndexTruncStats
	if s := nilStats.Summary(); s != "not_measured" {
		t.Errorf("nil 누적기 %q — not_measured여야 함", s)
	}
	st := &IndexTruncStats{}
	if s := st.Summary(); s != "not_measured" {
		t.Errorf("조립 0회 %q — not_measured여야 함(계측이 실제로 돌았다는 증거가 없다)", s)
	}
	st.Note(0, 0, 1200)
	if s := st.Summary(); s != "none" {
		t.Errorf("축약 0 %q — none이어야 함(측정했고 축약 없음의 자격)", s)
	}
	st.Note(3, 2, 26000)
	st.Note(1, 5, 24000)
	if st.Builds != 3 || st.ReducedBuilds != 2 || st.MaxAbbreviated != 3 ||
		st.MaxFoldedLow != 5 || st.MaxUsedChars != 26000 {
		t.Errorf("누적 오류: %+v", st)
	}
	want := "reduced 2/3 builds (max_abbreviated=3, max_folded_low=5)"
	if s := st.Summary(); s != want {
		t.Errorf("문구 %q — %q여야 함", s, want)
	}
	// 한계 블록 합류 — 누적기가 꽂히면 not_measured가 실측으로 교체된다.
	lim := assembleLimitations(&Ledger{}, ReportContext{IndexTrunc: st})
	if lim.IndexReduction != want {
		t.Errorf("한계 블록 %q — 누적기 문구가 실려야 함", lim.IndexReduction)
	}
}

// §15.4-3·4 — 등급 문구는 공개 status 3값 전수 + 미정의 값 무발명, 푸터 불변.
func TestGradeMeaningAndFooter(t *testing.T) {
	for _, s := range []Status{StatusConfirmed, StatusProvisional, StatusInsufficient} {
		if GradeMeaning(s) == "" {
			t.Errorf("status %s의 고정 문구 없음", s)
		}
	}
	if GradeMeaning(Status("nonsense")) != "" {
		t.Error("미정의 status에 문구 발명")
	}
	if !strings.Contains(ReportFooter, "단독 근거로 사용하지 않는다") {
		t.Errorf("푸터 문면: %q", ReportFooter)
	}
}

// displayRecord — 이 시험 전용의 유효 레코드(evidence 패키지 시험의
// okRecord와 같은 모양 — 패키지가 달라 재정의).
func displayRecord() evidence.EvidenceIndexRecord {
	return evidence.EvidenceIndexRecord{
		TargetID: "svc-1", Domain: "application",
		Aspect: evidence.AspectDB, RecordKind: evidence.KindFinding,
		FindingClass: "db_slow_queries", EntityKey: "sql_key:abc",
		Headline: "관측 없음",
		Effect:   evidence.Effect{Kind: evidence.EffectCategorical},
		Window:   evidence.TimeWindow{Class: evidence.WindowFull},
		Quality: evidence.Quality{Status: evidence.StatusAnomalous,
			Availability: evidence.AvailObserved, Confidence: evidence.ConfOK},
		Provenance: evidence.Provenance{Source: evidence.SrcDBSlowQueries, EnvelopeRef: "ref-1"},
	}
}
