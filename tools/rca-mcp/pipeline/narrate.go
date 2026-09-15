// 파이프라인 [6] 작문 단계 — 리포트 조립(ledger.AssembleReport)이 빈
// 값으로 남긴 두 종류를 채운다 (프롬프트 문서 §7).
//
//   - seed 원천 필드(symptom 지표·값·시각, problem 영향, alarm 헤더,
//     대상 표시명): seed에서 결정론 전사. LLM 없음.
//   - 산문 필드(headline·symptom/problem text·diagnosis summary·
//     conclusion): Writer(LLM 자리 6번째)가 조립된 리포트를 사람
//     문장으로 옮겨 적는다. 판단하지 않는다 — 입력이 이미 심사 통과
//     근거와 정직 표기만 담은 최종 리포트라는 것 자체가 가드다.
package pipeline

import (
	"context"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/seed"
)

// Prose는 Writer의 산출물 — 리포트의 산문 필드 5개다. 필드 순서와
// 이름은 프롬프트 문서 §7.2의 출력 스키마와 같다.
type Prose struct {
	Headline          string `json:"headline"`
	SymptomText       string `json:"symptom_text"`
	ProblemText       string `json:"problem_text"`
	DiagnosisSummary  string `json:"diagnosis_summary"`
	ConclusionSummary string `json:"conclusion_summary"`
}

// WriteInput은 Writer의 책상이다 — 조립이 끝난 리포트 둘과 인시던트
// 제목, 그리고 대상 표시명 표가 전부. 원시 도구 응답·기각된 관측은
// 오르지 않는다.
type WriteInput struct {
	Title string
	Rca   ledger.RcaResult
	UI    ledger.UIReport

	// Targets — target_id → 표시명. 리포트 구조는 canonical id(UUID)로
	// 말하지만 산문의 독자는 이름으로 읽는다(실 run 스모크에서 UUID
	// headline 실측, 2026-07-20). 결정론 전사(get_target_meta) — Writer는
	// 이 표에 있는 치환만 한다.
	Targets map[string]string
	// Feedback — §8.1-4 인용 무결성 위반의 재시도 피드백(1회). 비면 초회다.
	Feedback string
}

// Writer는 산문 작문의 LLM 자리다. nil이면 산문은 빈 값으로 남는다
// (walking skeleton·ablation).
type Writer interface {
	Write(ctx context.Context, in WriteInput) (Prose, error)
}

// fillSeedFields는 seed가 원천인 리포트 필드를 결정론으로 전사하고,
// 산문 작문용 대상 표시명 표(target_id → 이름)를 돌려준다. meta는
// 대상 표시명 해석(get_target_meta와 같은 targets 조회)이고, tri는
// 조사에 등장한 대상의 목록 원천이다.
func fillSeedFields(ctx context.Context, s seed.IncidentSeed, tri TriageResult, meta TargetMetaFunc, rca *ledger.RcaResult, ui *ledger.UIReport) (map[string]string, error) {
	rca.Symptom.Metric = s.MainMetric
	rca.Symptom.Observed = s.MainObserved
	rca.Symptom.Baseline = s.MainBaseline
	rca.Symptom.MainEventRef = s.MainEventID
	at := s.FirstEventAt
	for _, m := range s.Members {
		if m.EventID != "" && m.EventID == s.MainEventID {
			at = m.OccurredAt
			break
		}
	}
	if !at.IsZero() {
		rca.Symptom.At = &at
	}

	rca.Problem.Severity = s.Severity
	rca.Problem.Impact.Services = s.BlastServices
	rca.Problem.Impact.TargetIDs = s.BlastTargetIDs

	ui.Alarm.ID = s.IncidentID
	// seed 제목·표시명은 taint(§15.1)이자 리포트로 나가는 바이트다 —
	// 시크릿 스캔(§15.3-3, 6c. 라이브 canary가 잡은 유출 경로: 산문이
	// 아니라 결정론 전사가 제목을 리포트에 실었다).
	ui.Alarm.Name, _ = evidence.MaskSecrets(s.Title)
	ui.Alarm.Severity = s.Severity
	if !s.FirstEventAt.IsZero() {
		occurred := s.FirstEventAt
		ui.Alarm.OccurredAt = &occurred
	}

	// 표시명 — 조사에 등장한 대상 전부(도메인 해석된 대상 + 대표·원인
	// 대상)를 한 번에 조회한다. 못 찾으면 id 그대로(계약상 조회 결과에
	// 없는 id는 오류가 아니다 — triage와 같은 결).
	idSet := map[string]bool{}
	for id := range tri.TargetDomains {
		idSet[id] = true
	}
	if s.MainTargetID != "" {
		idSet[s.MainTargetID] = true
	}
	if rca.Cause.TargetID != "" {
		idSet[rca.Cause.TargetID] = true
	}
	ids := make([]string, 0, len(idSet))
	for id := range idSet {
		ids = append(ids, id)
	}
	names := map[string]TargetMeta{}
	if len(ids) > 0 {
		var err error
		if names, err = meta(ctx, ids); err != nil {
			return nil, err
		}
	}
	glossary := map[string]string{}
	for id, m := range names {
		if m.Name != "" && m.Name != id {
			// 표시명도 같은 계약(§15.3-3) — 리포트·writer 프롬프트 양쪽에
			// 실리는 값이다.
			glossary[id], _ = evidence.MaskSecrets(m.Name)
		}
	}

	ui.Alarm.Target = s.MainTargetID
	if n, ok := glossary[s.MainTargetID]; ok {
		ui.Alarm.Target = n
	}
	if n, ok := glossary[rca.Cause.TargetID]; ok {
		rca.Cause.TargetName = n
	}
	return glossary, nil
}

// applyProse는 Writer 산출물을 리포트의 산문 자리에 앉힌다.
func applyProse(p Prose, rca *ledger.RcaResult, ui *ledger.UIReport) {
	rca.Headline = p.Headline
	rca.Symptom.Text = p.SymptomText
	rca.Problem.Text = p.ProblemText
	ui.Diagnosis.Summary = p.DiagnosisSummary
	ui.ConclusionSummary = p.ConclusionSummary
}
