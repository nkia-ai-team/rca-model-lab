// 사슬 말단 문장의 **기계 생성** — docs/spec-agent-structure.md §7.3이
// 정본이다(§14-4 4d).
//
// §7.3의 문장 하나가 이 파일의 전부다: **"말단 문장은 LLM이 쓰지 않는다 —
// projector가 typed 레코드의 개체 필드를 조립해 기계 생성한다."** 그래서
// 엉뚱한 개체를 말로 포장할 자유가 애초에 없고, 이렇게 만든 구간은
// Origin=projector로 §7.7 검수 면제 대상이 된다.
//
// **여기서 값을 만들지 않는다**: 문장에 드는 것은 전부 레코드에 이미 있는
// 필드(EntityKey·TargetID·Aspect·Headline·Effect)다. 없는 항은 문장에서
// 빠질 뿐 채워지지 않는다 — 채우면 그 순간 이것도 작문이다.
package evidence

import (
	"fmt"
	"strconv"
	"strings"
)

// TerminalStatement는 개체 레코드 하나를 사슬 말단 문장으로 조립한다.
//
// 서식: `<EntityKey>(<TargetID>/<Aspect>): <Headline>[, <Metric> <Magnitude>]`
//
// Headline 자체가 이미 기계 서술이고(§15.1-2 원문 직삽 금지) 개체 정체성은
// EntityKey가 진다 — 둘을 붙이는 것이 조립의 전부다. RawExcerpt는 **쓰지
// 않는다**: 원문 발췌를 사슬 문장에 실으면 §15.1의 이스케이프 규율이
// 사슬까지 따라와야 하는데, 말단 판정(§8)이 읽는 것은 EntityKey 하나라
// 원문이 문장에 있을 이유가 없다.
func TerminalStatement(r EvidenceIndexRecord) string {
	if r.EntityKey == "" {
		return ""
	}
	var b strings.Builder
	b.WriteString(r.EntityKey)
	switch {
	case r.TargetID != "" && r.Aspect != "":
		fmt.Fprintf(&b, "(%s/%s)", r.TargetID, r.Aspect)
	case r.TargetID != "":
		fmt.Fprintf(&b, "(%s)", r.TargetID)
	}
	if r.Headline != "" {
		b.WriteString(": ")
		b.WriteString(r.Headline)
	}
	// 판정 축은 Headline에 없을 수 있다(도구별 추출기마다 서식이 다르다) —
	// 있으면 붙이고 없으면 만들지 않는다.
	if r.Effect.Metric != "" && r.Effect.Magnitude != nil {
		fmt.Fprintf(&b, " [%s=%s]", r.Effect.Metric,
			strconv.FormatFloat(*r.Effect.Magnitude, 'g', 4, 64))
	}
	return b.String()
}
