package ledger

import (
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// 실물 봉투 위에서 자격 게이트를 돌린 결과 — **수집 지연 원천 교체의 대조**다
// (§5.5 게이트 조항의 원천, 사용자 결정 2026-08-04).
//
// 종전 원천은 collectors.last_collected_at이었고 라이브 119에서 50개 대상 중
// 32개가 NULL·나머지도 최대 14일 낡아 지연이 통째로 unknown이었다. 그 상태로
// fail-closed를 돌리면 자격 지지가 한 건도 나오지 않는다 = confirmed 구조적
// 불가. 이 시험은 그 사실(왼쪽)과, 관측 데이터 자체를 원천으로 바꾸면 같은
// 봉투에서 자격이 살아난다는 것(오른쪽)을 한자리에서 고정한다.
func TestGateOverLiveEnvelopeCollectLagSourceSwap(t *testing.T) {
	recs := projectLive(t, "../evidence/testdata/envelope_scan_metrics.json",
		evidence.SrcScanMetrics, "scan_metrics")
	if len(recs) == 0 {
		t.Fatal("실물 레코드 0건 — 시험이 아무것도 안 걸고 있다")
	}

	// failed는 미달 조항의 히스토그램이다 — "왜 weak인가"를 조항 단위로
	// 말할 수 있어야 §15.4 한계 블록이 성립한다.
	failed := map[string]int{}
	run := func(recs []evidence.EvidenceIndexRecord) (qualified, weak, other int) {
		ix := evidence.NewIndex()
		l := evidence.NewLedger(ix)
		if _, err := ix.AppendAll(recs); err != nil {
			t.Fatalf("실물 레코드가 index 계약에 걸렸다: %v", err)
		}
		gc := GateContext{Index: ix, Now: liveNow,
			RequiredWindow: &TimeRange{From: liveFrom, To: liveTo}}
		for _, r := range recs {
			if r.RecordKind != evidence.KindFinding {
				continue
			}
			k := r.Key()
			p := SignalPred{PredID: "L-P1", Tool: k.Source, TargetID: k.TargetID,
				Aspect: k.Aspect, Metric: k.Metric, EntityKey: k.EntityKey,
				Predicate: evidence.PredPresent, Window: k.Window,
				Expectation: ExpectMustHold, Role: RoleCorroborating}
			j := JudgePredicate(p, l, gc)
			switch j.Verdict {
			case VerdictSupportedQualified:
				qualified++
			case VerdictSupportedWeak:
				weak++
				for _, name := range j.SupportGate.Failed() {
					failed[clauseTail(name)]++
				}
			default:
				other++
			}
		}
		return
	}

	// ① 종전 원천 재현 — 메타데이터가 죽어 지연이 unknown인 세계.
	//    레코드는 같고 지연 딱지만 되돌린다.
	dead := make([]evidence.EvidenceIndexRecord, len(recs))
	copy(dead, recs)
	for i := range dead {
		dead[i].Window.CollectLag = evidence.CollectLag{Status: evidence.TagUnknown}
	}
	q, w, o := run(dead)
	t.Logf("구 원천(CollectLag=unknown): 자격 %d · weak %d · 그 외 %d · 미달 조항 %v", q, w, o, failed)
	if q != 0 {
		t.Fatalf("지연이 미상인데 자격 지지가 %d건 — fail-closed가 새고 있다", q)
	}
	if w == 0 {
		t.Fatal("weak도 0건 — 지연 조항 말고 다른 곳에서 먼저 막혔다(측정이 무의미)")
	}

	// ② 새 원천 — 같은 봉투에서 지연이 실측된다(창 끝까지 데이터가 왔으므로 0초).
	//    투영이 붙인 값 그대로이며 시험이 주입하는 값이 아니다.
	for _, r := range recs {
		if !r.Window.CollectLag.Usable() {
			t.Fatalf("%s: 새 원천인데 지연이 %s — 봉투에서 파생되지 않았다",
				r.EID, r.Window.CollectLag.Status)
		}
	}
	failed = map[string]int{}
	q2, w2, _ := run(recs)
	t.Logf("새 원천(봉투 관측 데이터): 자격 %d · weak %d · 남은 미달 조항 %v", q2, w2, failed)
	if q2 == 0 {
		t.Fatalf("원천을 바꿔도 자격 0건 — 다른 조항도 함께 막고 있다(weak %d)", w2)
	}
}

var (
	// 봉투 fixture의 실 조회 창(observed_range)에 맞춘 값.
	liveFrom = time.Date(2026, 8, 3, 2, 44, 0, 0, time.UTC)
	liveTo   = time.Date(2026, 8, 3, 3, 14, 0, 0, time.UTC)
	liveNow  = time.Date(2026, 8, 3, 4, 0, 0, 0, time.UTC)
)

// clauseTail은 "EIX-0001:availability" 꼴 이름에서 조항 부분만 뗀다.
func clauseTail(name string) string {
	for i := len(name) - 1; i >= 0; i-- {
		if name[i] == ':' {
			return name[i+1:]
		}
	}
	return name
}

func projectLive(t *testing.T, path string, src evidence.ObservationSource, tool string) []evidence.EvidenceIndexRecord {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var env evidence.RawEnvelope
	if err := json.Unmarshal(b, &env); err != nil {
		t.Fatal(err)
	}
	res, err := evidence.Project(evidence.ProjectRequest{
		Source: src, Tool: tool, TargetID: "11111111-2222-3333-4444-555555555555",
		Domain: "commerce", WindowClass: evidence.WindowFull,
		From: liveFrom, To: liveTo, ResolutionS: 30,
		ClockSkew: evidence.ClockSkew{BoundS: 1, Status: evidence.TagObserved},
	}, env)
	if err != nil {
		t.Fatalf("투영: %v", err)
	}
	return res.Records
}
