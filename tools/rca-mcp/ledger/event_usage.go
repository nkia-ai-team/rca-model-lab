// llm_usage 이벤트 — LLM 호출당 토큰 사용량의 내구 기록(§14-6 6a,
// §14-1 판단 지점 ④ 처분 2026-08-11: journal 부착 공사와 같은 자리라
// 지금 신설).
//
// **소비자는 §14-7 §13 계측까지 부재하다** — 이 이벤트는 수사 상태를
// 바꾸지 않는 계측 문장이며, apply는 no-op이다. journal에 실려 크래시한
// run의 토큰 지출도 감사 가능해진다(run-meta.json의 tokens 절은 성공
// 경로에만 남는다).
package ledger

// EvLLMUsage — 계측 문장. 수첩 상태 전이는 없다(history-only).
const EvLLMUsage EventType = "llm_usage"

// LLMUsage는 호출 하나의 usage 절이다. 값은 서빙 응답의 usage를 그대로
// 승계한다(llm.UsageMeter.record — total 생략 시 prompt+completion 합).
type LLMUsage struct {
	Model string
	// Purpose — 호출처 딱지(§14-7 ③, additive). llm.WithPurpose 컨텍스트
	// 승계 — 어느 부품의 호출인가(candidates_change · candidates_member ·
	// candidates_investigation · grouper · audit_r · audit_r_revise ·
	// audit_a · probe_select · writer, 재생성 경로는 "regen/" 접두 —
	// 어휘는 열린 집합이고 생산자(llm 어댑터)의 딱지가 정본이다).
	// §11 예산표 대조·R/A 토큰 귀속(지표 6·9)의 귀속 키. 구 journal에는
	// 없던 필드라 빈 값이 "딱지 이전 기록"이다.
	Purpose          string
	PromptTokens     int
	CompletionTokens int
	TotalTokens      int
}

func (LLMUsage) eventType() EventType { return EvLLMUsage }

func init() {
	allowedActors[EvLLMUsage] = ActorPipeline
	payloadDecoders[EvLLMUsage] = decodeAs[LLMUsage]
}
