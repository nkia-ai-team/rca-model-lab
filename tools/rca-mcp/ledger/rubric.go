// §8.2 신뢰도 rubric v1 — 장부에서 셀 수 있는 것만, 결정론(§14-5 5c).
//
// 값의 정본은 스펙 §8.2의 표다. 등급 역전 불가는 산술이 보장한다: 가감
// 합산 ±0.10 < 밴드 간격 0.30의 절반 — 실효 confirmed 0.75~0.90 > 최고
// probable 0.60(C-17 재계산: confirmed 게이트의 시간 요건이 시간 항을
// 항상 참으로 만들어 실효 최저는 0.75다). run 단위 동적 조정 금지 —
// 가중치 개정은 릴리스 단위 rubric 버전으로(§8.2-4).
package ledger

import "math"

const (
	// ConfidenceContract — §8.2-3의 confidence_contract 값.
	ConfidenceContract = "gate_rubric_v1"

	confBandConfirmed = 0.80
	confBandProbable  = 0.50

	// 미채택 경쟁 상수(§8.2-2) — 서열은 rank 필드가 이미 지므로 수치는
	// 차등 없는 상수다. 반증 가설은 0(별도 카드).
	ConfUnresolvedCompetitor = 0.30
	ConfInferiorCompetitor   = 0.15
)

// ConfidenceItem은 breakdown 한 줄이다 — 항목 이름과 가감.
type ConfidenceItem struct {
	Item  string  `json:"item"`
	Delta float64 `json:"delta"`
}

// ConfidenceBreakdown은 §8.2-3의 구조 필드다. 산문 수치 표기는 금지(§8.1
// — 신뢰도는 슬롯 치환에서도 제외)이므로 이 구조가 수치 내역의 유일한
// 공식 전달 경로다.
type ConfidenceBreakdown struct {
	Contract string           `json:"contract"`
	Band     float64          `json:"band"`
	Items    []ConfidenceItem `json:"items,omitempty"`
	Value    float64          `json:"value"`
}

// RubricConfidence는 채택 가설의 §8.2 수치다. unexamined·requiredTotal은
// §4 필수 관점의 미조회 분자·분모(requiredViewTally — §8.2-1 "분모 =
// RequiredViewKeys"). 채택 없는 등급(insufficient)은 0이다 — insufficient
// 밴드는 없다(§8.2-2).
func RubricConfidence(st InternalStatus, h HypothesisV2, t Tally, unexamined, requiredTotal int) ConfidenceBreakdown {
	var band float64
	switch st {
	case IntConfirmed:
		band = confBandConfirmed
	case IntProbable:
		band = confBandProbable
	default:
		return ConfidenceBreakdown{Contract: ConfidenceContract}
	}
	b := ConfidenceBreakdown{Contract: ConfidenceContract, Band: band}
	v := band
	apply := func(item string, delta float64) {
		b.Items = append(b.Items, ConfidenceItem{Item: item, Delta: delta})
		v += delta
	}
	// 계보 다수 — 자격 지지의 원천 계보 ≥3종(Tally.Lineages는 자격 지지
	// 판정에서만 모인다).
	if len(t.Lineages) >= 3 {
		apply("lineage_ge3", 0.05)
	}
	// 시간 확인 — 선행 지지(coincides는 겹침 확인, §10 소비 규칙 그대로).
	if h.TemporalSatisfied() {
		apply("temporal_confirmed", 0.05)
	}
	// weak 비중 — weak_support가 자격 지지 수를 넘으면 감점.
	if t.WeakSupports > t.QualifiedSupports {
		apply("weak_majority", -0.05)
	}
	// 미조사 — 미조회 비율 > 1/3. 분모 0(필수 관점 기록 없음)이면 비율이
	// 정의되지 않아 항을 적용하지 않는다.
	if requiredTotal > 0 && 3*unexamined > requiredTotal {
		apply("unexamined_over_third", -0.05)
	}
	if lo := band - 0.10; v < lo {
		v = lo
	}
	if hi := band + 0.10; v > hi {
		v = hi
	}
	b.Value = math.Round(v*100) / 100
	return b
}
