// §7.7 심사점 A(채택 검수)의 주입 계약 — Verifier의 새 자리 ②(§14-5 5c,
// 재작성 ②). 발동·예산·소비는 loop가 지고(3단: pre-A 기계 선정 → A 심사
// → 결과 반영 재평가), 여기는 인터페이스와 typed 입력뿐이다 — [5] 주입
// 규약(verify.go)과 같은 이유로 pipeline은 llm을 import하지 않는다.
package pipeline

import (
	"context"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// AdoptClaimRecord는 인용 레코드의 기계 직렬화다(§7.7 입력 — Headline·
// Effect·Window·Quality·RawExcerpt. 원문 봉투 아님).
type AdoptClaimRecord struct {
	EID        string `json:"eid"`
	Headline   string `json:"headline"`
	Effect     string `json:"effect,omitempty"`
	Window     string `json:"window,omitempty"`
	Quality    string `json:"quality,omitempty"`
	RawExcerpt string `json:"raw_excerpt,omitempty"`
}

// AdoptClaim은 심사 대상 구간 하나다.
type AdoptClaim struct {
	ClaimID   string             `json:"claim_id"`
	Kind      string             `json:"kind"`
	EntityKey string             `json:"entity_key,omitempty"`
	Effect    string             `json:"effect"`
	// ExemptSupport — projector 생성 구간은 질문 ①(지지 실질)을 면제한다
	// (§7.7 — 필드 조립엔 의미 비약이 없다). ②(말단 깊이)는 말단 구간이면
	// 항상 심사.
	ExemptSupport bool               `json:"exempt_support,omitempty"`
	Terminal      bool               `json:"terminal,omitempty"`
	Records       []AdoptClaimRecord `json:"records,omitempty"`
}

// AdoptAuditInput은 구간당 1콜의 책상이다 — 가설의 Mechanism과 active 사슬
// 전체(맥락) + 심사 대상 구간. Mechanism 없이는 질문 ②를 판정할 재료가
// 없다(§7.7).
type AdoptAuditInput struct {
	Hypothesis string       `json:"hypothesis"`
	Mechanism  string       `json:"mechanism"`
	Chain      []AdoptClaim `json:"chain"`
	Subject    AdoptClaim   `json:"subject"`
}

// AdoptVerdict는 구간 판정이다. note-first 규율(§7.7 — R과 같은 결).
type AdoptVerdict struct {
	Note   string `json:"note"`
	Passed bool   `json:"passed"`
}

// AdoptAuditor는 심사점 A의 LLM 자리다. 오류·서식 위반의 처분은 호출자
// (loop)가 fail-closed로 문다 — 응답이 없으면 그 구간은 탈락이다.
type AdoptAuditor interface {
	AuditAdoption(ctx context.Context, in AdoptAuditInput) (AdoptVerdict, error)
}

// AdoptClaimOf는 ledger.ChainClaim의 심사 서식이다.
func AdoptClaimOf(c ledger.ChainClaim) AdoptClaim {
	return AdoptClaim{
		ClaimID: c.ClaimID, Kind: string(c.Kind), EntityKey: c.EntityKey,
		Effect:        c.Effect,
		ExemptSupport: c.Origin == ledger.OriginProjector,
		Terminal:      c.Kind == ledger.ChainTerminalEntity,
	}
}
