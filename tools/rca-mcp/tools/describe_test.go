// limitTimeline(§9 결정 3 판정) hermetic 단위 — 변화 감지(다중·원복)·
// 희박 표식·관측 표본 한정 문구.
package tools

import (
	"testing"
	"time"
)

func mkFolded(name string, labels map[string]string, buckets map[int64]float64) *foldedSeries {
	return &foldedSeries{Name: name, Labels: labels, Buckets: buckets}
}

func TestLimitTimeline(t *testing.T) {
	from := time.Unix(0, 0).UTC()
	to := from.Add(10 * time.Minute)
	step := time.Minute
	pair := &capacityPair{Limit: "l", JoinLabels: []string{"pool"}}

	// ① 다중 변경·원복(100→200→100) — 시작·끝 비교가 아니라 전 표본.
	b := map[int64]float64{}
	for i := int64(0); i < 10; i++ {
		v := 100.0
		if i >= 3 && i < 7 {
			v = 200
		}
		b[i*60] = v
	}
	rows := limitTimeline([]*foldedSeries{mkFolded("l", map[string]string{"pool": "p1"}, b)}, pair, from, to, step)
	if len(rows) != 1 {
		t.Fatalf("행 수 = %d", len(rows))
	}
	if rows[0]["stability"] != "changed" {
		t.Fatalf("원복 변화를 놓침: %v", rows[0]["stability"])
	}
	if ch := rows[0]["changes"].([]Finding); len(ch) != 2 {
		t.Fatalf("변경 2건(100→200, 200→100) 기대, 실제 %d", len(ch))
	}

	// ② 희박(10버킷 중 1표본) — 불변 단정 금지.
	rows = limitTimeline([]*foldedSeries{mkFolded("l", map[string]string{"pool": "p2"},
		map[int64]float64{60: 50})}, pair, from, to, step)
	if rows[0]["stability"] != "unknown_sparse" {
		t.Fatalf("1표본이 불변으로 단정됨: %v", rows[0]["stability"])
	}

	// ③ 충분 관측·불변 — "관측 N표본 동일"(한정 문구).
	b3 := map[int64]float64{}
	for i := int64(0); i < 10; i++ {
		b3[i*60] = 10
	}
	rows = limitTimeline([]*foldedSeries{mkFolded("l", map[string]string{"pool": "p3"}, b3)}, pair, from, to, step)
	if rows[0]["stability"] != "observed_constant" {
		t.Fatalf("불변 판정 실패: %v", rows[0]["stability"])
	}
	if rows[0]["sample_count"] != 10 {
		t.Fatalf("표본 수 = %v", rows[0]["sample_count"])
	}

	// ④ 조인 키 두 개는 합산 없이 별도 행 — 합치면 가짜 변화.
	rows = limitTimeline([]*foldedSeries{
		mkFolded("l", map[string]string{"pool": "a"}, b3),
		mkFolded("l", map[string]string{"pool": "b"}, map[int64]float64{0: 20, 60: 20, 120: 20, 180: 20}),
	}, pair, from, to, step)
	if len(rows) != 2 {
		t.Fatalf("조인 키별 행 분리 실패: %d행", len(rows))
	}
	for _, r := range rows {
		if r["stability"] == "changed" {
			t.Fatalf("별도 풀 합산으로 가짜 변화 발생: %v", r)
		}
	}
}
