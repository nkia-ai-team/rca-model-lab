// §8.1 인용 무결성 게이트 시험 — 5b 완료 기준 ③과 1:1 대응한다.
// 검사 3종(EID 실재·active / ref 실재 / 슬롯 닫힌 목록·값 부재)과
// "위반 시 부분 치환 없음" 계약.
package pipeline

import (
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// citeWorld는 store에 실물 봉투가 있고 그 ref를 문 레코드 하나가 active인
// 세계다. 반환된 EID가 유일한 정당 인용처다.
func citeWorld(t *testing.T) (CiteGate, string) {
	t.Helper()
	store, err := evidence.NewStore(t.TempDir())
	if err != nil {
		t.Fatalf("store: %v", err)
	}
	ref, err := store.Put("scan_metrics", []byte(`{"ok":true}`))
	if err != nil {
		t.Fatalf("put: %v", err)
	}
	ix := evidence.NewIndex()
	r := findingRec("web-1", evidence.StatusAnomalous, "latency_p99")
	mag := 3.5
	r.Effect.Magnitude = &mag
	r.Provenance.EnvelopeRef = ref
	eid, err := ix.Append(r)
	if err != nil {
		t.Fatalf("적재: %v", err)
	}
	return CiteGate{Index: ix, Store: store}, eid
}

func onlyKind(t *testing.T, vs []CiteViolation, want CiteViolationKind) {
	t.Helper()
	if len(vs) == 0 {
		t.Fatalf("위반이 없다, want %s", want)
	}
	for _, v := range vs {
		if v.Kind != want {
			t.Fatalf("위반 %v, want 전부 %s", vs, want)
		}
	}
}

func TestCiteGateValidSubstitution(t *testing.T) {
	g, eid := citeWorld(t)
	p := Prose{DiagnosisSummary: "근거 [" + eid + "] — 배율 {" + eid + ".Effect.Magnitude}배 상승"}
	out, vs := g.Apply(p)
	if len(vs) != 0 {
		t.Fatalf("위반: %v", vs)
	}
	if !strings.Contains(out.DiagnosisSummary, "배율 3.5배") {
		t.Fatalf("치환 실패: %s", out.DiagnosisSummary)
	}
}

func TestCiteGateUnknownEID(t *testing.T) {
	g, _ := citeWorld(t)
	_, vs := g.Apply(Prose{Headline: "원인은 EIX-9999다"})
	onlyKind(t, vs, CiteUnknownEID)
}

func TestCiteGateInactiveEID(t *testing.T) {
	g, eid := citeWorld(t)
	// 같은 관측 키의 후계가 head를 넘겨받으면 구 EID 인용은 위반이다 —
	// 문장화 입력이 active 사영이므로(§8.1-1) 여기 걸리면 지어낸 것이다.
	old, _ := g.Index.Get(eid)
	heir := old
	heir.EID = ""
	heir.Supersedes = eid
	if _, err := g.Index.Append(heir); err != nil {
		t.Fatalf("supersede 적재: %v", err)
	}
	_, vs := g.Apply(Prose{Headline: "근거 [" + eid + "]"})
	onlyKind(t, vs, CiteInactiveEID)
}

func TestCiteGateMissingRef(t *testing.T) {
	g, _ := citeWorld(t)
	r := findingRec("web-2", evidence.StatusAnomalous, "err_rate")
	r.Provenance.EnvelopeRef = "EST-9999:ghost"
	eid, err := g.Index.Append(r)
	if err != nil {
		t.Fatalf("적재: %v", err)
	}
	_, vs := g.Apply(Prose{Headline: "근거 [" + eid + "]"})
	onlyKind(t, vs, CiteMissingRef)
}

// 슬롯 경로는 닫힌 목록이다 — 신뢰도류 경로는 목록에 없어 자동 반려된다
// (§8.1 "신뢰도 수치는 슬롯 제외").
func TestCiteGateBadSlotPathClosed(t *testing.T) {
	g, eid := citeWorld(t)
	_, vs := g.Apply(Prose{Headline: "신뢰도 {" + eid + ".Confidence}%"})
	onlyKind(t, vs, CiteBadSlot)
}

func TestCiteGateValueUnavailable(t *testing.T) {
	g, eid := citeWorld(t)
	// Baseline이 없는 레코드에서 Baseline 슬롯 — 없는 값을 문장이 주장하게
	// 두지 않는다.
	_, vs := g.Apply(Prose{Headline: "기준 {" + eid + ".Effect.Baseline}"})
	onlyKind(t, vs, CiteValueUnavailable)
}

// 위반이 있으면 산문은 원문 그대로 돌아온다 — 부분 치환본을 내보내면
// 재시도 피드백의 좌표(원문 토큰)가 사라진다.
func TestCiteGateNoPartialSubstitutionOnViolation(t *testing.T) {
	g, eid := citeWorld(t)
	p := Prose{
		Headline:         "배율 {" + eid + ".Effect.Magnitude}배",
		DiagnosisSummary: "원인은 EIX-9999다",
	}
	out, vs := g.Apply(p)
	if len(vs) == 0 {
		t.Fatal("위반이 있어야 한다")
	}
	if out != p {
		t.Fatalf("위반인데 산문이 변형됐다: %+v", out)
	}
}
