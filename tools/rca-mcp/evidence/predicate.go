// 술어 어휘 — docs/spec-agent-structure.md §6(SignalPred.Predicate)이 정본.
//
// **왜 evidence에 있는가**(1a 타입 이동, 사유 기록): 사상표(§5.1)의 "허용
// Predicate" 열이 등록·판정 게이트의 기계 입력이고, 사상표는 레코드 스키마와
// 같은 자리에 있어야 한다(행 키·EntityKey 정본 열과 한 몸이다). ledger는
// evidence를 import하지만 반대 방향은 순환이라 불가능하므로, 어휘를 여기 두고
// ledger가 별칭으로 승계한다 — 기존 `ledger.Predicate`·`ledger.PredPresent`
// 사용처는 한 줄도 바뀌지 않는다(별칭이라 동일 타입).
package evidence

// Predicate는 닫힌 6종이다.
type Predicate string

const (
	PredPresent       Predicate = "present"
	PredAbsent        Predicate = "absent"
	PredMagnitudeGE   Predicate = "magnitude_ge"
	PredMagnitudeLT   Predicate = "magnitude_lt"
	PredDirectionUp   Predicate = "direction_up"
	PredDirectionDown Predicate = "direction_down"
)

func ValidPredicate(p Predicate) bool {
	switch p {
	case PredPresent, PredAbsent, PredMagnitudeGE, PredMagnitudeLT,
		PredDirectionUp, PredDirectionDown:
		return true
	}
	return false
}

// allPredicates는 사상표 "전부" 열의 실체다 — 표에 문자열 "전부"를 적으면
// 나중에 술어가 늘 때 표와 코드가 갈린다.
func allPredicates() []Predicate {
	return []Predicate{PredPresent, PredAbsent, PredMagnitudeGE, PredMagnitudeLT,
		PredDirectionUp, PredDirectionDown}
}

// existencePredicates — 존재 여부만 파생 가능한 class(값이 없거나 1 고정).
func existencePredicates() []Predicate {
	return []Predicate{PredPresent, PredAbsent}
}

// existenceDirectionPredicates — compare_peers처럼 값 크기는 못 쓰고
// 방향은 원천에 있는 class.
func existenceDirectionPredicates() []Predicate {
	return []Predicate{PredPresent, PredAbsent, PredDirectionUp, PredDirectionDown}
}
