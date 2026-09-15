package pipeline

import (
	"strings"
	"testing"
)

// 3c 라이브 실측 회귀 — 반려 사유가 "무엇을 하라"로 번역돼 사유보다 앞에
// 실리는지. 같은 규칙이 여러 번 나와도 지침은 한 번만 나온다.
func TestFeedbackLines_행동지침(t *testing.T) {
	rep := AdmissionReport{Rejected: []RejectedCandidate{
		{Index: 0, Rejects: []AdmissionReject{
			{Rule: "SupportEIDs", Reason: "결박된 active 개설 근거 EID가 0개 — §6 '없으면 개설 반려'"},
			{Rule: "규칙 3", Reason: "미실측 necessary 술어가 0개 — 죽지 않는 가설(반증력 없음)"},
		}},
		{Index: 1, Rejects: []AdmissionReject{
			{Rule: "규칙 3", Reason: "예측표 3개가 전부 기실측 — 예측이 아니라 회고"},
			{Rule: "규칙 1ⓐ", PredID: "H1-P2", Reason: `target_id "db:ghost"가 위상 어휘 밖`},
			{Rule: "SupportEIDs", Reason: "결박된 active 개설 근거 EID가 0개 — §6 '없으면 개설 반려'"},
		}},
	}}
	lines := feedbackLines(rep)

	// 지침 4종이 사유보다 먼저, 중복 없이.
	guides := []string{"support_eids에는", "role=necessary로 최소 1개", "아직 아무도 재지 않은 관측", "후보 어휘"}
	firstReason := -1
	for i, l := range lines {
		if !strings.HasPrefix(l, "지침:") {
			firstReason = i
			break
		}
	}
	if firstReason != len(guides) {
		t.Fatalf("지침 %d줄(기대 %d) — %v", firstReason, len(guides), lines)
	}
	for _, g := range guides {
		n := 0
		for _, l := range lines {
			if strings.HasPrefix(l, "지침:") && strings.Contains(l, g) {
				n++
			}
		}
		if n != 1 {
			t.Errorf("지침 %q가 %d번 — 정확히 1번이어야", g, n)
		}
	}
	// 원 사유 나열은 그대로 보존(5건).
	if got := len(lines) - firstReason; got != 5 {
		t.Errorf("사유 %d줄 — 5줄이어야: %v", got, lines[firstReason:])
	}
	if !strings.Contains(lines[len(lines)-1], "SupportEIDs") {
		t.Errorf("마지막 사유 줄 이상: %q", lines[len(lines)-1])
	}
}

// 모르는 규칙은 지침 없이 사유만 남는다.
func TestFeedbackLines_미지규칙(t *testing.T) {
	lines := feedbackLines(AdmissionReport{Rejected: []RejectedCandidate{
		{Rejects: []AdmissionReject{{Rule: "규칙 9", Reason: "미래 규칙"}}},
	}})
	if len(lines) != 1 || strings.HasPrefix(lines[0], "지침:") {
		t.Errorf("미지 규칙에 지침이 붙었다: %v", lines)
	}
}
