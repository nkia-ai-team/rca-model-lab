// 사슬 말단 문장 기계 생성 시험 — §7.3(§14-4 4d).
package evidence

import (
	"strings"
	"testing"
)

func TestTerminalStatementIsAssembledFromTypedFields(t *testing.T) {
	mag := 7.0
	r := EvidenceIndexRecord{
		TargetID: "orders-db", Aspect: AspectDB, RecordKind: KindFinding,
		EntityKey: "blocking_event:04:28:10",
		Headline:  "블로킹 사건 04:28:10~ 대기 최대 7세션",
		RawExcerpt: "UPDATE order_items SET qty=? WHERE id=?", // **문장에 실리면 안 된다**
		Effect:     Effect{Kind: EffectCount, Metric: "waiters", Magnitude: &mag},
	}
	got := TerminalStatement(r)
	for _, want := range []string{"blocking_event:04:28:10", "orders-db", "db", "대기 최대 7세션", "waiters=7"} {
		if !strings.Contains(got, want) {
			t.Fatalf("말단 문장에 %q가 없다: %q", want, got)
		}
	}
	// 원문 발췌는 사슬 문장에 들지 않는다 — §8 말단 게이트가 읽는 것은
	// EntityKey 하나라 원문이 있을 이유가 없고, 있으면 §15.1 이스케이프
	// 규율이 사슬까지 따라와야 한다.
	if strings.Contains(got, "UPDATE order_items") {
		t.Fatalf("원문 발췌가 사슬 문장에 실렸다: %q", got)
	}
}

// 개체가 없으면 문장도 없다 — **없는 값을 만들지 않는다**.
func TestTerminalStatementEmptyWithoutEntity(t *testing.T) {
	if got := TerminalStatement(EvidenceIndexRecord{TargetID: "t1", Headline: "무언가"}); got != "" {
		t.Fatalf("EntityKey 없는 레코드에서 문장이 나왔다: %q", got)
	}
}

// 없는 항은 빠질 뿐 채워지지 않는다.
func TestTerminalStatementOmitsMissingParts(t *testing.T) {
	got := TerminalStatement(EvidenceIndexRecord{EntityKey: "sql_key:abc"})
	if got != "sql_key:abc" {
		t.Fatalf("빈 항이 채워졌다: %q", got)
	}
}
