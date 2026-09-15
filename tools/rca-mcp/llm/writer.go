// Writer 어댑터 — 문서 §7. 유일하게 판단하지 않는 자리: 조립이 끝난
// 리포트를 사람 문장으로 옮겨 적는다. temperature 0(§7.4 — 작문도
// 재현 대상이다).
package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// writerSystem은 문서 §7.2의 시스템 프롬프트 v1 그대로다.
const writerSystem = `당신은 AIOps RCA 보고서의 작문가다. 수사는 이미 끝났다 — 입력으로
주어진 구조화 리포트(판정 status, 원인, 인과 사슬, 근거, 대안 가설,
누락 근거)를 운영 조직이 읽는 한국어 문장으로 옮겨 적는다.

당신은 판단하지 않는다:
- 입력에 없는 사실·수치·대상·원인을 만들지 마라. 모든 문장의 내용은
  입력의 어느 필드에서 왔는지 지목할 수 있어야 한다.
- 입력의 결론을 바꾸거나 강화하지 마라. 어투는 status를 따른다:
  - confirmed: 단정형으로 서술한다.
  - provisional: "가장 유력하다" 수준의 추정 어투 — 단정 금지, 남은
    확인 사항(missing)을 결론 문단에 함께 적는다. 잠정 원인을
    지목하되 추정임을 밝힌다 — "원인 미확정"이라는 표현은 쓰지 마라
    (그것은 insufficient의 몫이다).
  - insufficient: 원인 미확정을 명시한다. 유력 후보를 원인처럼 쓰지
    마라. 배제된 것(ruled_out·반증 가설)과 데이터 공백(gaps)을 적는다.

독자와 문체:
- 1차 독자는 24/7 관제(tier 1)다. headline 한 문장만 읽고 어느 팀에
  에스컬레이션할지 판단할 수 있어야 한다 — 원인 대상(무엇이)과 증상
  (어디에 어떤 영향)을 담아라.
- 전문 용어는 입력에 있는 것만. 지표명·대상명은 입력 표기 그대로 쓴다.
- 대상 표기: "대상 표시명 표"에 있는 target_id는 표시명으로 바꿔
  쓴다. 표에 없는 id는 그대로 둔다 — 표에 없는 이름을 지어내지 마라.
- 간결한 완결 문장. 감탄·과장·사과 없음.

각 필드의 몫:
- headline: 한 문장 요약 (원인 → 증상 방향, 120자 이내).
- symptom_text: 대표 현상 서술 — 어느 대상의 어느 지표가 기준 대비
  어떻게 튀었는가 (한두 문장).
- problem_text: 영향 서술 — 어느 서비스·대상이 어떤 심각도로 영향을
  받았는가 (한두 문장).
- diagnosis_summary: 진단 배너 한두 문장 — 판정과 원인(또는 미확정)의
  핵심만.
- conclusion_summary: 결론 문단 — 원인 경로(인과 사슬), 근거 요지,
  대안 가설의 처리(반증/열세), 남은 확인 사항 순으로 3~6문장.

인용·수치 규율 (§8.1 — 기계 검사를 통과해야 리포트가 된다):
- 관측 번호(EIX-…)는 입력에 등장하는 것만 쓸 수 있다. 지어낸 번호는
  기계 검사가 잡아 재작성된다.
- 수치(배율·건수·시각)는 직접 쓰지 말고 슬롯으로 쓴다:
  {EIX-0007.Effect.Magnitude} 처럼 — 하네스가 원본 값으로 치환한다.
  허용 경로: Effect.Magnitude / Effect.Observed / Effect.Baseline /
  Window.ChangeFrom / Window.ChangeTo / Quality.SampleN.
  입력 JSON에 이미 적힌 수치를 문장에 옮겨 적는 것도 금지다 — 슬롯이
  있으면 슬롯, 없으면 수치 없이 서술하라.
- 신뢰도는 수치로 쓰지 마라 — 등급 문구(단정/유력/미확정)로만 말한다.

출력은 JSON만:
{"headline": "...", "symptom_text": "...", "problem_text": "...",
 "diagnosis_summary": "...", "conclusion_summary": "..."}`

// Writer는 pipeline.Writer의 실 LLM 구현체다.
type Writer struct {
	Client *Client
}

// headlineMaxRunes — §7.4 가드 ②. 프롬프트는 120자를 요구하지만 가드는
// 여유를 둔다(한 줄 자리에 문단이 들어오는 실패 모드만 차단).
const headlineMaxRunes = 200

func (w *Writer) Write(ctx context.Context, in pipeline.WriteInput) (pipeline.Prose, error) {
	rcaJSON, err := json.MarshalIndent(in.Rca, "", "  ")
	if err != nil {
		return pipeline.Prose{}, fmt.Errorf("writer: RcaResult 직렬화: %w", err)
	}
	uiJSON, err := json.MarshalIndent(in.UI, "", "  ")
	if err != nil {
		return pipeline.Prose{}, fmt.Errorf("writer: UIReport 직렬화: %w", err)
	}
	// 표시명 표 — id 정렬로 고정(재현, §7.4 가드 ③과 같은 결).
	glossary := "없음"
	if len(in.Targets) > 0 {
		ids := make([]string, 0, len(in.Targets))
		for id := range in.Targets {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		var b strings.Builder
		for _, id := range ids {
			// 표시명은 taint다(§15.1 — 종전 직삽) — ②③ 한 줄 정화.
			fmt.Fprintf(&b, "%s → %s\n", id, SanitizeLine(in.Targets[id]))
		}
		glossary = strings.TrimRight(b.String(), "\n")
	}
	// 제목은 taint다(§15.1 — 제목 지시문 주입 우회가 시연된 표면). ①②③
	// 프레이밍 블록으로만 반입한다(6c).
	user := fmt.Sprintf("인시던트 제목(관측 데이터):\n%s\n\n대상 표시명 표(target_id → 이름 — 이름은 관측 데이터):\n%s\n\n구조화 리포트(RcaResult):\n%s\n\n상세 보고서(UIReport):\n%s",
		FrameData("TITLE", in.Title), glossary, rcaJSON, uiJSON)
	// §8.1-4 재시도 — 위반 목록과 행동 지침이 입력 앞에 붙는다.
	if in.Feedback != "" {
		user = "직전 작문이 인용 무결성 검사에 걸렸다. 아래 위반을 고쳐 전체를 다시 써라.\n" +
			in.Feedback + "\n" + user
	}

	var p pipeline.Prose
	if err := w.Client.CompleteJSON(WithPurpose(ctx, "writer"), writerSystem, user, &p); err != nil {
		return pipeline.Prose{}, fmt.Errorf("writer: %w", err)
	}

	// §7.4 가드 ① — 빈 필드는 오류(조용한 기본값 금지).
	for name, v := range map[string]string{
		"headline": p.Headline, "symptom_text": p.SymptomText,
		"problem_text": p.ProblemText, "diagnosis_summary": p.DiagnosisSummary,
		"conclusion_summary": p.ConclusionSummary,
	} {
		if v == "" {
			return pipeline.Prose{}, fmt.Errorf("writer: 산문 필드 %s가 빈 값", name)
		}
	}
	if n := utf8.RuneCountInString(p.Headline); n > headlineMaxRunes {
		return pipeline.Prose{}, fmt.Errorf("writer: headline %d자 — 한 줄 요약 상한(%d) 초과", n, headlineMaxRunes)
	}
	return p, nil
}
