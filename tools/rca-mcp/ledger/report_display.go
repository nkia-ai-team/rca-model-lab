// §15.4 판정 신뢰성 표시 (§14-6 6d) — 전제·한계 블록, 등급 고정 문구,
// 푸터. 전부 **기계 조립·하네스 상수**다: 게이트가 누락을 막고, 문장화
// 재량이 없다(§15.4-1·3 — LLM 문장화 대상이 아니다).
package ledger

import (
	"fmt"
	"sort"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// ReportFooter — §15.4-4 고정 푸터. 제품 차원의 책임 경계 표현은 이 한
// 줄이고, 실질은 전제·한계 블록이 채운다.
const ReportFooter = "본 리포트는 자동 분석 결과이며 조치 결정의 단독 근거로 사용하지 않는다."

// ConfidenceNote — §8.2-5 고정 문구(§14-5 이월 고아의 해소 자리).
const ConfidenceNote = "confidence 수치는 판정 게이트 충족도이며 정답 확률이 아니다."

// gradeMeanings — §15.4-3 등급 의미 고정 문구(공개 status 3값).
// provisional은 내부 probable의 공개 매핑이다(§8).
var gradeMeanings = map[Status]string{
	StatusConfirmed:    "관측이 이 원인을 지지하고 경쟁 가설이 반증됨 — 조치 전 현장 확인 권고",
	StatusProvisional:  "우세하나 감별 미완",
	StatusInsufficient: "결론 유보 — 미조사 영역 참조",
}

// GradeMeaning은 공개 status의 고정 문구다. 미정의 값은 빈 문자열 —
// 조용한 문구 발명 금지.
func GradeMeaning(s Status) string { return gradeMeanings[s] }

// ReportPremises는 §15.4-1 전제 블록(의무)이다.
type ReportPremises struct {
	// ClockSkewStatus — observed | unknown | exceeded (기계 필드 §5.1).
	ClockSkewStatus string `json:"clock_skew_status"`
	ClockSkewBoundS int    `json:"clock_skew_bound_s,omitempty"`
	// TemporalJudgement — 시간 판정 사용 여부의 정직 표기. unknown·
	// exceeded면 §10이 시간 판정을 금지하므로 "미사용"이 사실이다
	// (4차 A-13 — 폐기된 "≤2s 가정" 면책 문구의 재발 방지).
	TemporalJudgement string `json:"temporal_judgement"` // used | unused_clock_unknown | unused_clock_exceeded
	// BaselineWindow — 기준선 창 정의(실제 시각, 기계 조립).
	BaselineWindow string `json:"baseline_window,omitempty"`
	// ReplaySource — replay run이면 재생 입력의 표기(§12-6).
	ReplaySource   string `json:"replay_source,omitempty"`
	ConfidenceNote string `json:"confidence_note"`
}

// ReportLimitations는 §15.4-2 한계 블록(의무)이다 — confirmed여도
// 숨기지 않는다. §8의 "미조사" 정의가 느슨한 만큼 이 블록이 보상한다.
type ReportLimitations struct {
	// LayerBTruncated — 계층 B 상한으로 잘린 대상(§4).
	LayerBTruncated []string `json:"layer_b_truncated,omitempty"`
	// UnobservedViews — §5.2 롤업의 no_data·미조회 관점 목록.
	UnobservedViews []string `json:"unobserved_views,omitempty"`
	// WideningStage — widening 사다리 도달 단계(마지막 실행 칸).
	WideningStage string `json:"widening_stage,omitempty"`
	// BackendGaps — 일시 백엔드 장애로 인한 결손(§15.2-3) — 진짜 결손
	// (미수집·0건)과 구분해 표기한다.
	BackendGaps []string `json:"backend_gaps,omitempty"`
	// IndexReduction — §5.3-6 페이로드 축약 계측(§14-7 배선). 누적기가
	// 안 꽂힌 경로에서는 not_measured가 정직하다(6d 판단 지점 승계).
	IndexReduction string `json:"index_reduction"`
}

// IndexTruncStats는 run 한 번 동안의 §5.3 payload 축약 누적기다(§14-7).
// 생산자는 llm의 payload 빌더(출처별 부분 색인 포함 — build 1회 = Note
// 1회), 소비자는 한계 블록(IndexReduction)이다. 예산 무관 상시 접힘
// (normal+ok 롤업화 §5.3-4)은 세지 않는다 — 여기 실리는 것은 예산이
// 실제로 깎은 몫(축약·normal+low 접힘)뿐이다.
type IndexTruncStats struct {
	Builds         int // payload 조립 횟수(출처별·재생성 포함)
	ReducedBuilds  int // 예산 축약이 실제 발생한 조립 수
	MaxAbbreviated int // 한 조립 기준 축약(EID+Headline) 최대 수
	MaxFoldedLow   int // 한 조립 기준 normal+low 접힘 최대 수
	MaxUsedChars   int // 한 조립 기준 최대 사용 문자 수(캡 역산 재료 §14-7)
}

// Note는 payload 조립 한 번의 절단 기록을 누적한다.
func (s *IndexTruncStats) Note(abbreviated, foldedLow, usedChars int) {
	s.Builds++
	if abbreviated > 0 || foldedLow > 0 {
		s.ReducedBuilds++
	}
	if abbreviated > s.MaxAbbreviated {
		s.MaxAbbreviated = abbreviated
	}
	if foldedLow > s.MaxFoldedLow {
		s.MaxFoldedLow = foldedLow
	}
	if usedChars > s.MaxUsedChars {
		s.MaxUsedChars = usedChars
	}
}

// Summary는 한계 블록 IndexReduction의 표기다 — 측정했고 축약 0이면
// "none"이라고 말할 자격이 생긴다(not_measured와의 구분이 이 계측의 목적).
func (s *IndexTruncStats) Summary() string {
	if s == nil || s.Builds == 0 {
		return "not_measured"
	}
	if s.ReducedBuilds == 0 {
		return "none"
	}
	return fmt.Sprintf("reduced %d/%d builds (max_abbreviated=%d, max_folded_low=%d)",
		s.ReducedBuilds, s.Builds, s.MaxAbbreviated, s.MaxFoldedLow)
}

// assemblePremises — 전제 블록 기계 조립.
func assemblePremises(rc ReportContext) ReportPremises {
	p := ReportPremises{
		ClockSkewStatus: string(rc.ClockSkew.Status),
		ClockSkewBoundS: rc.ClockSkew.BoundS,
		ReplaySource:    rc.ReplaySource,
		BaselineWindow:  rc.BaselineWindow,
		ConfidenceNote:  ConfidenceNote,
	}
	switch rc.ClockSkew.Status {
	case evidence.TagObserved:
		p.TemporalJudgement = "used"
	case evidence.TagExceeded:
		p.TemporalJudgement = "unused_clock_exceeded"
	default:
		p.TemporalJudgement = "unused_clock_unknown"
	}
	return p
}

// assembleLimitations — 한계 블록 중 수첩·index에서 나오는 몫.
// 계층 B 절단·미조회 목록은 ScreenResult가 원천이라 pipeline이 채운다.
func assembleLimitations(l *Ledger, rc ReportContext) ReportLimitations {
	lim := ReportLimitations{IndexReduction: rc.IndexTrunc.Summary()}
	// widening 도달 단계 — 마지막 실행 칸(이벤트 순서가 사다리 순서다).
	for _, ev := range l.events {
		if w, ok := ev.Payload.(WideningExecuted); ok {
			lim.WideningStage = string(w.Step)
		}
	}
	// 일시 백엔드 결손 — backend_error 레코드의 (backend, 대상) 전수.
	if rc.Index != nil {
		seen := map[string]bool{}
		for _, rec := range rc.Index.Active() {
			if rec.Quality.NoDataReason != evidence.NoDataBackendError {
				continue
			}
			k := fmt.Sprintf("%s:%s", rec.Provenance.Source, rec.TargetID)
			if !seen[k] {
				seen[k] = true
				lim.BackendGaps = append(lim.BackendGaps, k)
			}
		}
		sort.Strings(lim.BackendGaps)
	}
	return lim
}
