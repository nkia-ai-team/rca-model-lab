// 봉투 ↔ projector 입력의 계약 왕복 검사.
//
// projector(evidence 패키지)는 tools.Envelope를 직접 쓰지 않는다 — 의존 방향이
// tools → pipeline → ledger → evidence라 순환이기 때문이고, 실제 입력도
// evidence store에 적재된 **봉투 원문 JSON**이기 때문이다. 그래서 evidence에
// 거울 타입(RawEnvelope)이 있고, 두 타입이 조용히 갈릴 위험이 생겼다.
//
// 이 시험이 그 위험을 막는다: tools는 evidence를 import할 수 있으므로 여기서
// 왕복을 건다. 봉투에 새 필드를 추가하고 거울에 안 넣으면 여기서 죽는다.
package tools

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

func TestEnvelopeMirrorsProjectorInput(t *testing.T) {
	src := Envelope{
		Status:          "anomalous",
		NoDataReason:    NoDataZeroObservations,
		AssessmentBasis: "판정 근거",
		Summary:         "요약",
		Findings: []Finding{
			{"class": "shifted", "metric": "m1", "current_median": 2.0, "baseline_median": 1.0,
				"refs": []string{"vm:m1{target_id=t}:A/B"}},
		},
		ObservedRange: &TimeRange{From: time.Unix(0, 0).UTC(), To: time.Unix(60, 0).UTC()},
		Truncated:     true,
		Refs:          []string{"vm:scan{target_id=t}:A/B"},
		Scopes:        []QueryScope{qscope("shifted", 30, 20), qscopeMetric("peer", "m1", 5, 5)},
	}
	b, err := json.Marshal(src)
	if err != nil {
		t.Fatal(err)
	}
	got, err := evidence.DecodeEnvelope(b)
	if err != nil {
		t.Fatal(err)
	}

	if got.Status != src.Status || got.NoDataReason != src.NoDataReason ||
		got.AssessmentBasis != src.AssessmentBasis || got.Summary != src.Summary ||
		got.Truncated != src.Truncated {
		t.Errorf("봉투 머리 필드가 거울에서 빠졌다: %+v", got)
	}
	if len(got.Findings) != 1 || got.Findings[0]["metric"] != "m1" {
		t.Errorf("findings 손실: %+v", got.Findings)
	}
	if got.ObservedRange == nil || !got.ObservedRange.To.Equal(src.ObservedRange.To) {
		t.Errorf("observed_range 손실: %+v", got.ObservedRange)
	}
	if len(got.Refs) != 1 {
		t.Errorf("refs 손실: %+v", got.Refs)
	}
	if len(got.Scopes) != 2 {
		t.Fatalf("scopes 손실: %+v", got.Scopes)
	}
	if got.Scopes[0].Class != "shifted" || got.Scopes[0].Total != 30 ||
		got.Scopes[0].Returned != 20 || got.Scopes[0].Omitted != 10 {
		t.Errorf("절단 계약이 거울에서 어긋났다: %+v", got.Scopes[0])
	}
	if got.Scopes[1].Metric != "m1" {
		t.Errorf("지표축 있는 조회 단위가 손실됐다: %+v", got.Scopes[1])
	}

	// 필드 수 자체를 센다 — 봉투에 필드를 더하고 거울을 안 고치면 여기서 죽는다.
	var raw map[string]json.RawMessage
	if err := json.Unmarshal(b, &raw); err != nil {
		t.Fatal(err)
	}
	mirror, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var mraw map[string]json.RawMessage
	if err := json.Unmarshal(mirror, &mraw); err != nil {
		t.Fatal(err)
	}
	for k := range raw {
		if _, ok := mraw[k]; !ok {
			t.Errorf("봉투 필드 %q가 projector 입력 타입에 없다 — 투영에서 조용히 사라진다", k)
		}
	}
}

// 절단 계약의 산술: omitted = total - returned이고 음수는 0으로 접힌다.
func TestQueryScopeArithmetic(t *testing.T) {
	if s := qscope("c", 10, 3); s.Omitted != 7 {
		t.Errorf("%+v", s)
	}
	if s := qscope("c", 0, 0); s.Omitted != 0 {
		t.Errorf("0건 조회가 절단으로 보인다: %+v", s)
	}
	if s := qscope("c", 2, 5); s.Omitted != 0 {
		t.Errorf("음수 누락이 실렸다: %+v", s)
	}
}
