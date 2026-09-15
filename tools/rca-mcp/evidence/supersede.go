// supersede 계약 — docs/spec-agent-structure.md §5.7이 정본.
//
// 재조회(W1 정밀화·§7.3 드릴다운·W2 고해상도)는 같은 대상·관점의 레코드를
// 다시 만든다. 그때 무엇이 무엇을 대체하는가의 규칙이 여기 있다.
//
// 계약 4줄 요약:
//
//	· EID 불변 · 레코드 append-only — 대체는 삭제가 아니라 새 레코드다.
//	· 단일 active head: supersede는 "동일 selector의 active head 하나 →
//	  새 head 하나"인 **함수**다. selector = 관측 키(§6.0)이며 창 클래스를
//	  포함한다 — full과 onset_narrow는 서로 다른 head로 공존한다(4차 A-3:
//	  W2 재조회가 full 창의 반증 근거를 조용히 지우던 오답 경로의 봉쇄).
//	· 도구가 다른 정밀 재조회는 supersede가 아니라 Refines다 — 원본은
//	  active로 남고 정밀본은 자기 키의 head가 된다.
//	· append와 동시에 영향 명제(§6.0)를 후계 관측값으로 원자적 재계산한다
//	  (미실측으로 돌아가지 않는다).
package evidence

import "fmt"

// checkLinksLocked는 append 직전의 supersede·Refines 검사다(ix.mu 보유 전제).
// 반려는 전부 여기서 나며, 통과한 뒤에야 레코드가 index에 들어간다 —
// 계약 위반 레코드가 실린 뒤 사후 정리하는 경로는 만들지 않는다(append-only
// 장부에서 "실렸다가 무효"는 감사 추적을 흐린다).
func (ix *Index) checkLinksLocked(r EvidenceIndexRecord, key ObservationKey) error {
	if r.Supersedes != "" {
		prev, ok := ix.recordLocked(r.Supersedes)
		if !ok {
			return fmt.Errorf("%s가 대체한다는 %s가 index에 없음(§5.7 실재 검사)", r.EID, r.Supersedes)
		}
		if by, dead := ix.supersededBy[r.Supersedes]; dead {
			return fmt.Errorf("%s는 이미 %s가 대체함 — fork 반려(§5.7). 한 레코드를 둘이 대체하면 같은 키에 active head가 둘이 되어 진리표 행 1로 영구 inconclusive다", r.Supersedes, by)
		}
		if prev.RecordKind != r.RecordKind {
			return fmt.Errorf("%s(%s)가 %s(%s)를 대체할 수 없음 — 종류가 다른 레코드는 같은 selector의 head가 아니다",
				r.EID, r.RecordKind, prev.EID, prev.RecordKind)
		}
		if pk := prev.Key(); pk != key {
			return fmt.Errorf("selector 불일치 — %s의 관측 키 %s ≠ %s의 %s(§5.7: supersede는 동일 selector 안의 함수다. 창 클래스가 다르면 두 head로 공존한다)",
				r.EID, key, prev.EID, pk)
		}
		// 순환은 구조적으로 불가능하다(대상은 반드시 기존 레코드이고 각
		// 레코드는 최대 하나만 대체하므로 사슬은 삽입 순서로 향한다).
		// 그래도 후계 추적을 한 번 돌려 불변식을 실증한다 — 이 검사가
		// 실패하면 index 내부 상태가 이미 깨진 것이라 append를 막는다.
		if _, err := ix.activeHeirLocked(r.Supersedes); err != nil {
			return fmt.Errorf("%s의 supersede 사슬이 성립하지 않음: %w", r.Supersedes, err)
		}
	}
	if r.Refines != "" {
		base, ok := ix.recordLocked(r.Refines)
		if !ok {
			return fmt.Errorf("%s가 정밀화한다는 %s가 index에 없음(§5.7)", r.EID, r.Refines)
		}
		if bk := base.Key(); bk == key {
			return fmt.Errorf("%s와 %s는 관측 키가 같다(%s) — 같은 selector의 재조회는 Refines가 아니라 supersede다(§5.7)",
				r.EID, base.EID, key)
		}
	}
	return nil
}

// linkLocked는 검사를 통과한 레코드의 head·후계 색인을 갱신한다.
// Refines는 **head를 건드리지 않는다** — 원본 head를 지우면 그 레코드에
// 결박된 반증·지지가 증발한다(4차 A-3).
func (ix *Index) linkLocked(r EvidenceIndexRecord, key ObservationKey) {
	if r.Supersedes != "" {
		ix.supersededBy[r.Supersedes] = r.EID
		ix.heads[key] = removeString(ix.heads[key], r.Supersedes)
	}
	if r.RecordKind != KindMeta {
		ix.heads[key] = append(ix.heads[key], r.EID)
	}
}

// Heads는 그 관측 키의 active head EID들이다.
//
// **둘 이상일 수 있다** — 반려하지 않는 것이 계약이다. 실물 봉투에서 같은
// 관측 키의 조회 범위 행이 여럿 나오고(1c 실측: scan_metrics의 shifted·
// appeared·disappeared 세 구획이 한 키로 접힌다), 그것은 계약 위반이 아니라
// 독립된 논리 조회 단위 셋이다. supersede만이 head를 함수로 제한한다 —
// 대체 없이 실린 동일 키 레코드가 여럿이면 §6 진리표 행 1(복수 해소)이
// 판정하며, 그 사실을 여기서 감추지 않는다.
func (ix *Index) Heads(k ObservationKey) []string {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	return append([]string(nil), ix.heads[k.Canonical()]...)
}

func removeString(in []string, s string) []string {
	out := in[:0]
	for _, v := range in {
		if v != s {
			out = append(out, v)
		}
	}
	return out
}
