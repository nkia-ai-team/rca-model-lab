package llm

import (
	"strings"
	"testing"
)

// 3c 라이브 실측 회귀 — 등록 계약(§6.2 SupportEIDs 결박·규칙 3)을 3원
// 프롬프트가 전부 가르치는지. LLM 호출 없이 시스템 프롬프트 문자열만 본다.
func TestCandidateSkeleton_등록계약지침(t *testing.T) {
	want := []string{
		"EIX-",          // eid 형식 명시
		"글자 그대로 복사",     // 결박 요건
		"eid_hint",      // 결박 통로
		"색인에 없는 새 관측",   // 미실측 necessary 요건
		"window만 바꾼 것은", // 창 변형 봉쇄
		"서식 예시",         // 26B가 베낄 본
		`"support_eids": ["EIX-0007"]`,
	}
	for _, src := range []*SourceAdapter{
		NewChangeSource(nil), NewMemberSource(nil), NewInvestigationSource(nil),
	} {
		system := strings.Replace(candidateSkeleton, "{source_role}", src.role, 1)
		if strings.Contains(system, "{source_role}") {
			t.Fatalf("%s: 역할 문단 미치환", src.source)
		}
		for _, w := range want {
			if !strings.Contains(system, w) {
				t.Errorf("%s 프롬프트에 %q 없음", src.source, w)
			}
		}
	}
}

// 재요구 프롬프트도 같은 계약을 미달 사유별 행동 지침으로 가르쳐야 한다.
func TestReviseSystem_미달사유별지침(t *testing.T) {
	for _, w := range []string{"미달 사유별 행동 지침", "미실측 necessary 0", "전부 기실측",
		"role_valid=false", "아직 재지 않은 관측"} {
		if !strings.Contains(reviseSystem, w) {
			t.Errorf("reviseSystem에 %q 없음", w)
		}
	}
}
