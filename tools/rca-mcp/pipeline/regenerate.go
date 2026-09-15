// [4] 재생성 — docs/spec-agent-structure.md §7.5(입력 6요소)·§7.6(43k
// 모양)이 정본이다(§14-4 4d).
//
// 재생성은 새 파이프라인이 아니라 **깔때기 재호출**이다: 같은 Generate가
// 갱신된 index 위에서 한 번 더 돈다. 다른 것은 둘뿐이다:
//
//	재호출 범위  index 소비 출처 + Grouper만(§7.6). 변경·멤버 출처는
//	             인시던트 창의 함수라 index가 갱신돼도 입력이 같다 —
//	             다시 물으면 같은 값에 43k를 두 번 내는 것이다.
//	규칙 3 문맥  material delta로 새로 판정 가능해진 necessary는 "미실측"
//	             과 같이 센다(§7.5 — 반려 전멸 방지).
//
// 6요소는 **Feedback 채널로 간다**(§6.2-6 반려 피드백·§14-3 다양성 재요구와
// 같은 통로). LLM 쪽에 새 입구를 내지 않는 것이 이 레포의 규약이다.
package pipeline

import (
	"context"
	"fmt"
	"strings"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// IndexConsumingSource는 "이 출처의 시야가 evidence index인가"의 표식이다.
//
// **표식을 출처 쪽에 두는 이유**: 재생성이 출처 목록을 이름·타입으로
// 골라내면 새 출처가 추가될 때마다 이 파일이 그 사실을 몰라 조용히 빠진다.
// 표식이 없는 출처는 재호출되지 않는다(fail-closed — 모르는 출처에 43k를
// 쓰지 않는 쪽이 보수 방향이다).
type IndexConsumingSource interface {
	ConsumesIndex() bool
}

// indexConsuming은 재호출 대상 출처만 남긴다.
func indexConsuming(sources []CandidateSource) []CandidateSource {
	var out []CandidateSource
	for _, s := range sources {
		if m, ok := s.(IndexConsumingSource); ok && m.ConsumesIndex() {
			out = append(out, s)
		}
	}
	return out
}

// regenerator는 Regenerator의 실 구현이다 — [4]가 쓴 것과 **같은 부품**을
// 들고 있다. 다른 설정으로 돌면 재생성 가설만 다른 잣대로 심사된다.
type regenerator struct {
	base    GenerateInput
	sources []CandidateSource
	grouper Grouper
	ledger  *ledger.Ledger
	cfg     GenerateConfig
}

// NewRegenerator는 §7.5 재생성의 실 구현을 만든다. **index 소비 출처가
// 하나도 없으면 nil을 돌려준다** — 재호출할 것이 없는 재생성은 같은 입력의
// 반복이라 정의상 무의미하고, 빈 출처로 Generate를 돌리면 후보 0개 →
// admission 실패로 run이 죽는다.
func NewRegenerator(base GenerateInput, sources []CandidateSource, g Grouper,
	l *ledger.Ledger, cfg GenerateConfig) Regenerator {

	live := indexConsuming(sources)
	if len(live) == 0 || g == nil || l == nil {
		return nil
	}
	return &regenerator{base: base, sources: live, grouper: g, ledger: l, cfg: cfg}
}

// Regenerate는 깔때기를 한 번 더 돈다.
//
// **다양성 재요구는 끈다**: 재생성 자체가 이미 조건부 지출(§7.6 reserve
// 게이트를 통과한 43k)이고, 그 안에서 또 3원 재호출을 물면 4c 정정이 막은
// 회귀(누적 317k)가 재생성 경로로 되돌아온다. AffordDiversityRerequest를
// nil로 두는 것이 그 차단이며 nil=재요구 안 함이 기존 fail-closed 계약이다.
func (r *regenerator) Regenerate(ctx context.Context, in RegenerateInput) ([]ledger.HypothesisEntry, error) {
	if err := in.Validate(); err != nil {
		return nil, err
	}
	gi := r.base
	gi.Evidence = in.Index
	gi.Feedback = regenFeedback(in)
	gi.DeltaKeys = deltaKeySet(in.DeltaKeys)

	cfg := r.cfg
	cfg.AffordDiversityRerequest = nil

	opened, _, err := Generate(ctx, gi, r.sources, r.grouper, r.ledger, cfg)
	if err != nil {
		return nil, err
	}
	return opened, nil
}

func deltaKeySet(keys []evidence.ObservationKey) map[evidence.ObservationKey]bool {
	if len(keys) == 0 {
		return nil
	}
	out := map[evidence.ObservationKey]bool{}
	for _, k := range keys {
		out[k.Canonical()] = true
	}
	return out
}

// regenFeedbackLimit은 6요소 각 항목의 서식 상한이다 — 재생성 프롬프트가
// 입력 예산(§5.3)을 넘기면 index 발췌가 먼저 잘려 정작 봐야 할 관측이 빠진다.
const regenFeedbackLimit = 12

// regenFeedback은 6요소를 **기계 조립**해 후보 프롬프트에 싣는다.
// 자유문 요약을 LLM에게 시키지 않는다 — 요약이 곧 해석이고, 해석은 재생성의
// 입력이 아니라 출력이어야 한다.
func regenFeedback(in RegenerateInput) []string {
	out := []string{fmt.Sprintf(
		"재생성 라운드 %d(§7.5) — 아래는 직전 검증 루프의 결과다. 같은 가설을 다시 내지 말고, "+
			"미판정 명제와 미조사 영역을 설명하는 경로를 제시하라.", in.Round)}

	// ② projection 요약.
	for i, h := range in.Projection {
		if i >= regenFeedbackLimit {
			break
		}
		out = append(out, fmt.Sprintf("[기존 가설] %s(%s) 자격지지 %d·약지지 %d·미판정 %d·necessary 미해소 %d%s",
			h.ID, h.Mechanism, h.Qualified, h.Weak, h.Inconclusive, h.NecessaryUnresolved,
			map[bool]string{true: " — 반증됨", false: ""}[h.Refuted]))
	}
	// ③ 반증 가설.
	for i, rf := range in.Refuted {
		if i >= regenFeedbackLimit {
			break
		}
		out = append(out, fmt.Sprintf("[반증] %s — %s (근거 %s)",
			rf.Signature, rf.Reason, strings.Join(rf.EIDs, ",")))
	}
	// ④ probe 로그.
	for i, p := range in.ProbeLog {
		if i >= regenFeedbackLimit {
			break
		}
		out = append(out, fmt.Sprintf("[probe] %s → %s (%d건 적재)",
			p.ObservationKey, p.Outcome, len(p.ProducedEIDs)))
	}
	// ⑤ 미조사·저신뢰.
	for i, u := range in.Unexplored {
		if i >= regenFeedbackLimit {
			break
		}
		out = append(out, fmt.Sprintf("[미조사/%s] %s %s %s", u.Kind, u.TargetID, u.Tool, u.Detail))
	}
	// ⑥ 미판정 명제 — 등록 규칙 3이 요구하는 "죽을 수 있는 예측"의 재료다.
	for i, u := range in.Undecided {
		if i >= regenFeedbackLimit {
			break
		}
		tag := ""
		if u.NewlyDecidable {
			tag = " (widening delta로 새로 판정 가능)"
		}
		out = append(out, "[미판정 명제] "+u.Key+tag)
	}
	out = append(out, "지침: 예측 술어에는 **아직 판정되지 않은 명제**를 최소 하나 걸어라 — "+
		"전부 기실측인 예측표는 회고이며 등록 규칙 3이 반려한다.")
	return out
}
