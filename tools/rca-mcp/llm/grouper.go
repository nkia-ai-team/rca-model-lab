// Grouper 어댑터 — 문서 §5 (v1, spike 5-2에서 5/5 통과).
package llm

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// grouperSystem은 문서 §5.3의 시스템 프롬프트 v1 그대로다.
const grouperSystem = `당신은 장애 원인 분석(RCA)의 가설 후보 정리 담당이다. 가설 후보
목록에서 중복 묶음을 찾는다.

중복 기준 (둘 다 만족해야 같은 묶음):
1. 의심 대상(suspect)이 같다
2. 인과사슬(chain)이 같은 계열이다 — 표현이 달라도 같은 인과
   이야기면 같은 계열, 대상이 같아도 인과 이야기가 다르면 다른 계열

규칙:
- 모든 후보 인덱스가 정확히 한 번씩 나타나야 한다 (중복이 없으면
  혼자 묶음)
- 후보 내용을 다시 쓰거나 요약하지 마라 — 출력은 인덱스 묶음뿐이다
- 출력은 JSON 하나만, note를 먼저 쓰고 그에 따라 groups를 정하라:
  {"note": "묶음 판단 이유 한두 문장", "groups": [[0, 2], [1], ...]}`

// Grouper는 pipeline.Grouper의 실 LLM 구현체다. 완전 분할 검증은
// 호출자(Generate의 checkPartition) 몫 — 어댑터는 묶음만 나른다.
type Grouper struct {
	Client *Client
}

// grouperCand는 §5.4의 직렬화 형태다 — index/suspect/source/chain만.
// Prior·반증 조건·probe는 묶음 판단에 불필요(컨텍스트 절약 + 판단
// 오염 방지).
type grouperCand struct {
	Index   int      `json:"index"`
	Suspect string   `json:"suspect"`
	Source  string   `json:"source"`
	Chain   []string `json:"chain"`
}

// grouperOut — note가 groups보다 앞(문서 §1).
type grouperOut struct {
	Note   string  `json:"note"`
	Groups [][]int `json:"groups"`
}

func (g *Grouper) Group(ctx context.Context, cands []pipeline.Candidate) ([][]int, error) {
	items := make([]grouperCand, len(cands))
	for i, c := range cands {
		chain := make([]string, len(c.Chain))
		for j, st := range c.Chain {
			chain[j] = st.Entity + ": " + st.Effect
		}
		items[i] = grouperCand{Index: i, Suspect: c.TargetID, Source: string(c.Source), Chain: chain}
	}
	body, err := json.MarshalIndent(items, "", " ")
	if err != nil {
		return nil, fmt.Errorf("grouper: 직렬화: %w", err)
	}

	var out grouperOut
	if err := g.Client.CompleteJSON(WithPurpose(ctx, "grouper"), grouperSystem, "가설 후보:\n"+string(body), &out); err != nil {
		return nil, fmt.Errorf("grouper: %w", err)
	}
	return out.Groups, nil
}
