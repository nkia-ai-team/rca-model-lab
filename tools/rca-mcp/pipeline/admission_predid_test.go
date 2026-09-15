// B-5 승계 시험 — §14-4 4b 완료 기준 ④. 가설 **간** PredID 유일성은
// ledger.ValidatePredIDs(가설 내부)가 지지 않는다(4a 실측).
package pipeline

import (
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

func predIDCand(hypoID string, ids ...string) AdmittedCandidate {
	var preds []ledger.SignalPred
	for _, id := range ids {
		preds = append(preds, ledger.SignalPred{
			PredID: id, Tool: evidence.SrcScanMetrics, TargetID: "t1",
			Aspect: evidence.AspectMetric, Metric: "cpu",
			Predicate: evidence.PredPresent, Window: evidence.WindowFull,
			Expectation: evidence.ExpectMustHold, Role: ledger.RoleNecessary,
		})
	}
	return AdmittedCandidate{
		Candidate: Candidate{PredictedSignals: preds}, hypoID: hypoID,
	}
}

// 기계 부여(AssignPredIDs)는 가설 ID 접두사로 유일성을 구조적으로 보장한다 —
// 정상 경로에서는 아무것도 걸리지 않아야 한다.
func TestCrossHypoPredIDNoFalsePositive(t *testing.T) {
	got := CrossHypoPredIDConflicts([]AdmittedCandidate{
		predIDCand("H1", "H1-P1", "H1-P2"),
		predIDCand("H2", "H2-P1"),
	})
	if len(got) != 0 {
		t.Fatalf("정상 부여인데 반려: %v", got)
	}
}

// 충돌은 **뒤쪽 후보를 반려**한다 — 조용히 첫 항목을 고르면 한 가설의 R
// 심사 결과가 다른 가설 술어에 박힌다(세탁 경로).
func TestCrossHypoPredIDConflictRejectsLater(t *testing.T) {
	got := CrossHypoPredIDConflicts([]AdmittedCandidate{
		predIDCand("H1", "H1-P1"),
		predIDCand("H2", "H1-P1", "H2-P2"),
	})
	if len(got) != 1 {
		t.Fatalf("반려 대상 = %v, 기대 후보 1건", got)
	}
	rj, ok := got[1]
	if !ok || len(rj) != 1 || rj[0].PredID != "H1-P1" || rj[0].Rule != "B-5" {
		t.Fatalf("반려 사유 = %+v", got)
	}
}

// 빈 PredID는 여기서 세지 않는다 — 형식 위반은 규칙 1ⓒ의 몫이고, 두 곳이
// 같은 위반을 세면 반려 사유가 중복된다.
func TestCrossHypoPredIDIgnoresEmpty(t *testing.T) {
	if got := CrossHypoPredIDConflicts([]AdmittedCandidate{
		predIDCand("H1", ""), predIDCand("H2", ""),
	}); len(got) != 0 {
		t.Fatalf("빈 pred_id를 B-5로 셈: %v", got)
	}
}
