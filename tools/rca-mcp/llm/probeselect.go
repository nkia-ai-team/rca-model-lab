// 조사자 어댑터(신) — docs/spec-agent-structure.md §7.1이 정본이다(§14-4 4b).
//
// **왜 investigator.go가 아니라 새 파일인가**: 구 어댑터는 ChatTools로 LLM이
// 도구를 직접 호출하고 봉투 원문이 대화에 유입되던 경로이며(§7.4에서 컨텍스트를
// 태운 바로 그 경로), §14-4 4c가 삭제할 표면이다. 한 파일에 두면 삭제 커밋이
// 새 계약까지 흔든다 — R 어댑터를 verifier.go 밖에 세운 것과 같은 규율이다.
//
// 이 어댑터가 지키는 계약 셋(§7.1):
//
//	① 무상태  — 대화 이력이 없다. 입력은 매 호출 ledger projection + index
//	            발췌에서 기계 재구성된 ProbeChoiceInput 하나다.
//	② 선택    — 도구별 인자 union이 서식에 없다. 후보는 기계가 열거한
//	            PredID 묶음이고 LLM이 하는 일은 그중 하나를 고르는 것뿐이다.
//	③ strict  — 출력은 DecodeProbeRequest(DisallowUnknownFields)를 통과한
//	            ProbeRequest 하나다. 모르는 필드·누락은 조용히 흐르지 않는다.
package llm

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// probeSelectSystem은 §7.1의 시스템 프롬프트다.
//
// 문안 규율은 §14-3 선례를 승계한다: **등록(선택) 계약을 먼저 못박고**,
// 서식 예시를 하나 보이고, 반려는 사유가 아니라 **행동 지침**으로 되돌린다
// (3c 라이브 실측 — 사유만 주면 26B는 같은 위반을 되풀이한다).
const probeSelectSystem = `당신은 AIOps RCA 검증 루프의 조사자다. 이번 걸음에서 **어느 명제를 실측할지
하나만** 고른다.

당신은 도구를 부르지 않는다. 도구·대상·시간창·인자는 이미 명제가 정해 두었고
하네스가 조립해 실행한다 — 당신이 쓰는 인자는 하나도 없다. 당신의 일은
**후보 목록(candidates)에서 항목 하나를 고르는 것**뿐이다.

입력:
- hypotheses: 활성 가설의 기계 요약(메커니즘·사슬·현재 지지/판정 수).
  refuted=true인 가설은 이미 죽었다.
- candidates: **우선순위 순으로 정렬된** 후보. 각 항목은 관측 키 하나이며
  pred_ids(그 키를 지목한 술어 전량)·priority(왜 앞에 있는가)·
  expectations(가설별 예측)를 갖는다.
  priority의 뜻:
    temporal_requirement  1순위 가설의 시간 요건이 비어 있다 — 이것부터 채운다
    necessary_allocation  아직 필수 술어가 한 번도 판정되지 않은 경쟁 가설
    discriminating        가설들의 예측이 갈린다 — 결과가 어느 쪽이든 목록이 준다
    corroborating         예측이 갈리지 않는 확증용(후순위)
- recent: 직전 실행이 낳은 관측 요약.
- feedback: 직전 요청이 반려된 사유와 행동 지침. 있으면 반드시 따르라.

고르는 규칙:
- 위쪽 후보가 규율상 먼저다. 아래쪽을 고르려면 reason에 그 이유를 써라.
- pred_ids는 고른 후보의 목록을 **그대로 전부** 복사한다. 항목을 빼거나,
  다른 후보의 id를 섞거나, 목록에 없는 id를 지어내면 반려된다.
- 이미 판정된 명제는 후보에 없다. 없는 것을 요구하지 마라.

출력은 JSON만, 필드 둘뿐이다(다른 필드를 넣으면 반려된다). reason을 먼저 쓰고
그 판단에 맞는 후보를 골라라:
{"reason": "왜 이 명제를 지금 재는가 한 문장",
 "pred_ids": ["H1-P2", "H3-P1"]}`

// ProbeSelector는 pipeline.ProbeRequester의 실 LLM 구현체다.
type ProbeSelector struct {
	Client *Client
}

// ── 서식 (LLM이 보는 전부) ──────────────────────────────────────

type probeHypoJS struct {
	ID        string `json:"id"`
	Mechanism string `json:"mechanism"`
	Chain     string `json:"chain"`
	// 판정 계수 — 어느 가설이 서 있고 어느 쪽이 비었는지의 기계 요약.
	Qualified           int  `json:"qualified_supports"`
	Weak                int  `json:"weak_supports"`
	Inconclusive        int  `json:"inconclusive"`
	NecessaryUnresolved int  `json:"necessary_unresolved"`
	Refuted             bool `json:"refuted"`
}

// probeCandJS — **도구 인자가 없다**. 실리는 것은 명제의 좌표뿐이다(§7.1).
type probeCandJS struct {
	PredIDs      []string `json:"pred_ids"`
	Key          string   `json:"key"`
	Tool         string   `json:"tool"`
	TargetID     string   `json:"target_id"`
	Aspect       string   `json:"aspect,omitempty"`
	Metric       string   `json:"metric,omitempty"`
	EntityKey    string   `json:"entity_key,omitempty"`
	Window       string   `json:"window"`
	Expectations []string `json:"expectations"`
	Priority     string   `json:"priority"`
	Note         string   `json:"note,omitempty"`
}

type probeObsJS struct {
	EID      string `json:"eid"`
	TargetID string `json:"target_id"`
	Headline string `json:"headline"`
	Status   string `json:"status"`
}

// NextProbe는 걸음 하나의 선택을 받는다.
//
// **반환 전에 strict 디코드를 통과한다** — CompleteJSON의 일반 Unmarshal은
// 누락을 zero value로, 오타 필드를 무시로 흘린다(3차 검토 실측). 그래서 응답을
// RawMessage로 받아 ledger.DecodeProbeRequest에 그대로 넘긴다.
func (s *ProbeSelector) NextProbe(ctx context.Context, in pipeline.ProbeChoiceInput) (ledger.ProbeRequest, error) {
	if len(in.Candidates) == 0 {
		return ledger.ProbeRequest{}, fmt.Errorf("probe 선택: 후보 0건 — 부를 이유가 없다")
	}
	body, err := json.MarshalIndent(payloadOf(in), "", " ")
	if err != nil {
		return ledger.ProbeRequest{}, fmt.Errorf("probe 선택: 직렬화: %w", err)
	}
	var raw json.RawMessage
	if err := s.Client.CompleteJSON(WithPurpose(ctx, "probe_select"), probeSelectSystem, "이번 걸음:\n"+string(body), &raw); err != nil {
		return ledger.ProbeRequest{}, fmt.Errorf("probe 선택: %w", err)
	}
	req, err := ledger.DecodeProbeRequest(raw)
	if err != nil {
		return ledger.ProbeRequest{}, fmt.Errorf("probe 선택: %w", err)
	}
	return req, nil
}

func payloadOf(in pipeline.ProbeChoiceInput) map[string]any {
	hyps := make([]probeHypoJS, 0, len(in.Hypotheses))
	for _, h := range in.Hypotheses {
		hyps = append(hyps, probeHypoJS{
			ID: h.ID, Mechanism: h.Mechanism, Chain: h.Chain,
			Qualified: h.Qualified, Weak: h.Weak, Inconclusive: h.Inconclusive,
			NecessaryUnresolved: h.NecessaryUnresolved, Refuted: h.Refuted,
		})
	}
	cands := make([]probeCandJS, 0, len(in.Candidates))
	for _, c := range in.Candidates {
		cands = append(cands, probeCandJS{
			PredIDs: c.PredIDs, Key: c.Key, Tool: c.Tool, TargetID: c.TargetID,
			Aspect: c.Aspect, Metric: c.Metric, EntityKey: c.EntityKey, Window: c.Window,
			Expectations: c.Expectations, Priority: c.Priority, Note: c.Note,
		})
	}
	obs := make([]probeObsJS, 0, len(in.Recent))
	for _, o := range in.Recent {
		obs = append(obs, probeObsJS{EID: o.EID, TargetID: o.TargetID,
			Headline: o.Headline, Status: o.Status})
	}
	out := map[string]any{
		"wheel": in.Wheel, "probes_used": in.ProbesUsed, "probe_budget": in.ProbeBudget,
		"hypotheses": hyps, "candidates": cands, "recent": obs,
	}
	if len(in.Feedback) > 0 {
		out["feedback"] = in.Feedback
	}
	return out
}
