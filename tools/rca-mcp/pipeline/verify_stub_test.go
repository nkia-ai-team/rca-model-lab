package pipeline

import (
	"context"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// stubVerify는 [5] 자리의 시험용 대역이다 — **배선만 본다**. 루프의 행동
// (판정 3갈래·걸음 규율·종료 조건)은 loop 패키지의 시험이 지고, 여기서는
// "책상이 넘어오는가 / 산출이 [6]에 오르는가"만 확인한다.
//
// 종료 기록(채택·종료 선언)은 실물 루프와 같은 문장을 남긴다 — 그래야 [6]
// 리포트 조립이 실제와 같은 장부를 읽는다.
type stubVerify struct {
	adopt string
	// seen — Verify가 받은 책상(배선 검사용).
	seen VerifyInput
}

func (s *stubVerify) Verify(_ context.Context, in VerifyInput) (VerifyResult, error) {
	s.seen = in
	dec := ledger.Decision{Terminate: true, Status: ledger.StatusInsufficient,
		Reason: "stub: 루프 미수행"}
	if s.adopt != "" {
		if _, ok := in.Ledger.Hypothesis(s.adopt); ok {
			dec.Status, dec.AdoptedID = ledger.StatusConfirmed, s.adopt
			dec.Reason = "stub: 채택"
			if _, err := in.Ledger.Append(ledger.ActorRule, in.To,
				ledger.HypothesisAdopted{Hypothesis: s.adopt, Reason: dec.Reason}); err != nil {
				return VerifyResult{}, err
			}
		}
	}
	if _, err := in.Ledger.Append(ledger.ActorRule, in.To, ledger.LoopTerminated{Decision: dec}); err != nil {
		return VerifyResult{}, err
	}
	return VerifyResult{Decision: dec}, nil
}
