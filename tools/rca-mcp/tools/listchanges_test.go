// list_changes 순수 단위 — DB 없이 §10의 판정 규칙을 고정한다.
// 특히 lcIsReadOnly는 3자 검토가 생산자 소스에서 찾아낸 은폐 경로의
// 회귀 가드다(SESSION_KILL이 allowlist에 들어가는 순간 파괴 작업이
// 기본 화면에서 사라진다 — 실측 데이터로는 잡을 수 없는 함정).
package tools

import (
	"testing"
	"time"
)

func TestListChangesReadOnlyAllowlist(t *testing.T) {
	// 조회 전용으로 검증된 것만 감춘다.
	for _, op := range []string{"CURRENT_SESSION_AND_LOCK", "PARAMETER", "ORACLE_SCHEMA"} {
		if !lcIsReadOnly("데이터베이스 Live-SQL", op) {
			t.Errorf("조회 전용인데 노출됨: %s", op)
		}
	}
	// 파괴 작업은 같은 카테고리·같은 빈 action이어도 반드시 보인다.
	for _, op := range []string{"SESSION_KILL", "", "FUTURE_UNKNOWN_OP"} {
		if lcIsReadOnly("데이터베이스 Live-SQL", op) {
			t.Errorf("allowlist 밖인데 감춰짐(은폐 경로 부활): %q", op)
		}
	}
	// 미지의 카테고리 전체는 감춤 대상이 아니다.
	if lcIsReadOnly("새로운 카테고리", "PARAMETER") {
		t.Error("미등재 카테고리가 감춰짐 — allowlist는 category×operation 짝이어야 한다")
	}
}

func TestListChangesScope(t *testing.T) {
	cases := map[string]string{
		"사용자": "control_plane", "자산 트리": "global",
		"구성 대상": "target", "수집기": "target", "데이터베이스 Live-SQL": "target",
	}
	for cat, want := range cases {
		if got := lcScope(lcRawCH{Category: cat}); got != want {
			t.Errorf("scope(%s) = %s, want %s", cat, got, want)
		}
	}
}

// lcPair는 억지 1:1을 만들지 않는다 — 후보가 2개 이상이면 ambiguous로
// 후보를 노출하고 아무것도 소비하지 않는다(§10.5-1, 검토 지적 반영).
func TestListChangesPairing(t *testing.T) {
	base := time.Date(2026, 7, 14, 0, 35, 39, 0, time.UTC)
	mk := func(id string, offset time.Duration, name string) lcRawCH {
		return lcRawCH{ID: id, At: base.Add(offset), Category: "수집기", Name: name}
	}
	always := func(e *lcEvent, r lcRawCH) bool { return r.Name == e.MatchKey }

	t.Run("유일 후보만 잇는다", func(t *testing.T) {
		e := &lcEvent{At: base, MatchKey: "polestar_apm"}
		rows := []lcRawCH{mk("a", 3*time.Millisecond, "polestar_apm"), mk("b", time.Hour, "polestar_apm")}
		consumed := map[string]bool{}
		lcPair([]*lcEvent{e}, rows, "수집기", always, consumed)
		if e.Correlation != "unique_inferred" {
			t.Fatalf("correlation = %s, want unique_inferred", e.Correlation)
		}
		if e.DeltaMs == nil || *e.DeltaMs != 3 {
			t.Fatalf("delta_ms = %v, want 3", e.DeltaMs)
		}
		if !consumed["a"] || consumed["b"] {
			t.Fatalf("소비 오류: %v", consumed)
		}
	})

	t.Run("후보 다수는 ambiguous — 소비하지 않는다", func(t *testing.T) {
		e := &lcEvent{At: base, MatchKey: "polestar_apm"}
		rows := []lcRawCH{
			mk("a", 100*time.Millisecond, "polestar_apm"),
			mk("b", 200*time.Millisecond, "polestar_apm"),
		}
		consumed := map[string]bool{}
		lcPair([]*lcEvent{e}, rows, "수집기", always, consumed)
		if e.Correlation != "ambiguous" {
			t.Fatalf("correlation = %s, want ambiguous", e.Correlation)
		}
		if len(e.Candidates) != 2 {
			t.Fatalf("후보 노출 안 됨: %v", e.Candidates)
		}
		if len(consumed) != 0 {
			t.Fatalf("ambiguous인데 소비함(임의 선택 금지): %v", consumed)
		}
	})

	t.Run("후보 없음은 unmatched — 누락이 아니라 사실", func(t *testing.T) {
		e := &lcEvent{At: base, MatchKey: "otel_recv"}
		lcPair([]*lcEvent{e}, []lcRawCH{mk("a", 0, "polestar_apm")}, "수집기", always, map[string]bool{})
		if e.Correlation != "unmatched" {
			t.Fatalf("correlation = %s, want unmatched", e.Correlation)
		}
	})

	t.Run("창 밖은 후보가 아니다", func(t *testing.T) {
		e := &lcEvent{At: base, MatchKey: "polestar_apm"}
		lcPair([]*lcEvent{e}, []lcRawCH{mk("a", 3*time.Second, "polestar_apm")}, "수집기", always, map[string]bool{})
		if e.Correlation != "unmatched" {
			t.Fatalf("2초 밖인데 이어짐: %s", e.Correlation)
		}
	})
}

// 요약이 다르면 "내용 동일"이라 하지 않고 variants로 변화를 보인다
// (§10.5-5 — 자산 트리 members 0→3 실측이 근거).
func TestListChangesFoldVariants(t *testing.T) {
	base := time.Date(2026, 7, 14, 1, 15, 36, 0, time.UTC)
	rows := []lcRawCH{
		{ID: "1", At: base, Category: "자산 트리", Operation: "저장", Name: "unified", Action: "execute", Detail: `{"members": 0}`},
		{ID: "2", At: base.Add(time.Minute), Category: "자산 트리", Operation: "저장", Name: "unified", Action: "execute", Detail: `{"members": 1}`},
		{ID: "3", At: base.Add(2 * time.Minute), Category: "자산 트리", Operation: "저장", Name: "unified", Action: "execute", Detail: `{"members": 1}`},
	}
	inv := lcInventory{byID: map[string]lcTargetMeta{}, byName: map[string]string{}}
	evs, excluded, exMeta := lcFoldChangeHistory(rows, map[string]bool{}, inv, false)
	if excluded != 0 || len(exMeta) != 0 {
		t.Fatalf("변경인데 제외됨: %d %v", excluded, exMeta)
	}
	if len(evs) != 1 {
		t.Fatalf("같은 계열이 %d건으로 갈림", len(evs))
	}
	e := evs[0]
	if e.Count != 3 {
		t.Fatalf("count = %d, want 3", e.Count)
	}
	if len(e.Variants) != 2 {
		t.Fatalf("variants = %d, want 2(요약 2종)", len(e.Variants))
	}
	if e.Scope != "global" {
		t.Fatalf("scope = %s, want global(대상 개념 없음)", e.Scope)
	}
	if !lcHasNote(e, "요약 2종으로 접음") {
		t.Fatalf("요약 다름 고지가 없다: %v", e.Notes)
	}
	if !e.FirstAt.Equal(base) || !e.LastAt.Equal(base.Add(2*time.Minute)) {
		t.Fatalf("구간 보존 실패: %v ~ %v", e.FirstAt, e.LastAt)
	}
}

// action이 비어 있으면 감추는 게 아니라 표식을 달아 노출한다(§10.4).
func TestListChangesActionMissingExposed(t *testing.T) {
	rows := []lcRawCH{{
		ID: "k1", At: time.Now().UTC(), Category: "데이터베이스 Live-SQL",
		Operation: "SESSION_KILL", Action: "", Name: "some-db",
		Detail: `{"killed": 3}`,
	}}
	inv := lcInventory{byID: map[string]lcTargetMeta{}, byName: map[string]string{}}
	evs, excluded, _ := lcFoldChangeHistory(rows, map[string]bool{}, inv, false)
	if excluded != 0 {
		t.Fatalf("SESSION_KILL이 제외됐다 — 은폐 경로 부활")
	}
	if len(evs) != 1 {
		t.Fatalf("사건 %d건", len(evs))
	}
	if !lcHasNote(evs[0], "action_missing") {
		t.Fatalf("action 비었음 표식이 없다: %v", evs[0].Notes)
	}
}

// 정렬 구획: 힌트 귀속 → 대상 → 전역 → 관제면(§10.7).
func TestListChangesBucket(t *testing.T) {
	hint := "11111111-1111-4111-8111-111111111111"
	hinted := &lcEvent{Scope: "target", Targets: []lcTarget{{ID: hint}}}
	other := &lcEvent{Scope: "target", Targets: []lcTarget{{ID: "22222222-2222-4222-8222-222222222222"}}}
	global := &lcEvent{Scope: "global"}
	ctrl := &lcEvent{Scope: "control_plane"}
	for i, e := range []*lcEvent{hinted, other, global, ctrl} {
		if got := lcBucket(e, hint); got != i {
			t.Errorf("bucket[%d] = %d", i, got)
		}
	}
	// 힌트가 없으면 첫 구획은 비고 나머지 순서는 유지된다.
	if lcBucket(hinted, "") != 1 || lcBucket(ctrl, "") != 3 {
		t.Error("힌트 없을 때 구획이 무너짐")
	}
}
