// projector_spot_checked 이벤트 — §5.8-2 런타임 스팟 체크의 기계 기록
// (§14-7 ③). [6] 게이트가 confirmed 채택 근거(SupportEIDs) 1~2건을 봉투
// 원문에서 재투영해 레코드 Effect와 재대조한 결과다.
//
// 일치도 기록한다 — §13 지표 7(정합 불일치율)의 분모가 "수행한 검사 수"
// 이기 때문이다. 불일치의 상태 전이(confirmed→probable 강등)는 이벤트가
// 아니라 게이트 판정(DecisionV2)에 실린다 — 이 이벤트는 계측 문장이며
// apply는 no-op이다(llm_usage와 같은 계열, history-only).
package ledger

// EvProjectorSpotChecked — 계측 문장. 수첩 상태 전이는 없다.
const EvProjectorSpotChecked EventType = "projector_spot_checked"

// ProjectorSpotChecked는 검사 한 건의 결과다. Note는 기계 조립 토큰만
// 싣는다 — 봉투 원문·오류 원문은 싣지 않는다(§15.3-2).
type ProjectorSpotChecked struct {
	EID         string
	EnvelopeRef string
	Consistent  bool
	Note        string
}

func (ProjectorSpotChecked) eventType() EventType { return EvProjectorSpotChecked }

func init() {
	allowedActors[EvProjectorSpotChecked] = ActorRule
	payloadDecoders[EvProjectorSpotChecked] = decodeAs[ProjectorSpotChecked]
}
