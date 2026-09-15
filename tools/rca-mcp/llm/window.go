// LLM 응답의 관측 시간창 파싱 — [5] Investigator가 근거 엔트리의
// observed_window를 채우는 경로다(llm/investigator.go).
//
// 이 두 조각은 §14-2 전까지 llm/examiner.go에 있었다. 그 파일은 [3]이 기계
// 배터리로 교체되면서 통째로 폐기됐고(스펙 §4), 남은 유일 소비자인
// Investigator를 위해 여기로 옮겨 왔다 — 폐기 파일에 남의 부품을 얹어 두면
// 다음 폐기 때 같이 사라진다.
package llm

import (
	"fmt"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

type windowJSON struct {
	From string `json:"from"`
	To   string `json:"to"`
}

// parseWindow — 창이 없으면 nil, 있으면 RFC3339 두 끝을 엄격히 판다
// (조용한 기본값 금지: 못 읽은 시각을 zero time으로 흘리면 §10 선후 판정이
// 거짓 입력을 받는다).
func parseWindow(w *windowJSON) (*ledger.Window, error) {
	if w == nil {
		return nil, nil
	}
	from, err := time.Parse(time.RFC3339, w.From)
	if err != nil {
		return nil, fmt.Errorf("observed_window.from %q: %w", w.From, err)
	}
	to, err := time.Parse(time.RFC3339, w.To)
	if err != nil {
		return nil, fmt.Errorf("observed_window.to %q: %w", w.To, err)
	}
	return &ledger.Window{From: from, To: to}, nil
}
