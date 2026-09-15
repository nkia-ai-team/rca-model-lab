// CandidateSource 어댑터 — 문서 §4 (v1, spike 5-3에서 3/3 통과).
// 공통 골격 프롬프트 + 출처별 역할 문단(§4.4). past_case는 피드백(b)
// 설계 대기라 여기 없다. 생성 계열은 temperature 0.2를 쓴다 — 호출자가
// Client를 그렇게 구성해 넘긴다.
package llm

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// candidateSkeleton은 문서 §4.3의 공통 시스템 프롬프트 v1 그대로다.
// {source_role} 자리에 출처별 역할 문단이 들어간다.
const candidateSkeleton = `당신은 AIOps RCA(근본 원인 분석) 시스템의 가설 후보 생성기다.

{source_role}

각 후보의 규칙:
- chain은 원인→증상 인과사슬이다. 각 구간은 {"entity": "target_id",
  "effect": "이 지점에서 일어나는 일"}이며, 첫 구간의 entity는 반드시
  의심 대상(target_id)과 같아야 하고, 끝 구간은 주어진 증상이어야
  한다.
- identity는 가설의 정체성이다. cause_entity는 원인 대상의 target_id
  (후보 어휘에서 고른다. 특정할 수 없으면 "unknown:<증상 target_id>"),
  mechanism은 어떤 기제로 증상이 생겼는지 한 문장(자유 서술),
  impact_scope는 영향받는 대상 target_id 목록,
  temporal_claim은 precedes | coincides | unknown 중 하나.
- predicted_signals는 이 가설이 참이면 보여야/안 보여야 하는 신호다.
  **작문이 아니라 선택이다** — tool·target_id·aspect·metric은 아래
  "후보 어휘"에서 그대로 골라 쓴다. 지어내면 그 가설은 반려된다.
  항목 필드(아래 11개 외의 필드를 절대 추가하지 않는다):
  - eid_hint: 이미 관측된 index 레코드를 가리키는 예측이면 그 eid,
    아니면 ""
  - tool / target_id / aspect / metric: 후보 어휘에서 선택
  - entity_key: 개체 키(sql_key·template_id·episode_id 등). 없으면 ""
  - predicate: present | absent | magnitude_ge | magnitude_lt |
    direction_up | direction_down (그 tool의 허용 목록 안에서)
  - threshold: predicate가 magnitude_ge·magnitude_lt일 때만 숫자로
    넣는다. 그 외에는 threshold 필드 자체를 넣지 않는다
  - window: full | onset_narrow
  - expectation: must_hold | must_not_hold
  - role: necessary | corroborating
- support_eids는 이 후보를 세운 근거 레코드의 eid 목록이며 **1개 이상**
  이어야 한다. 인용할 수 있는 것은 입력 evidence의 레코드에 적힌
  eid 필드(EIX-로 시작하는 값)뿐이다 — target_id·UUID·알람 ID는 eid가 아니다.
  **결박 요건**: 인용한 eid마다 그 eid를 eid_hint로 지목하는 술어가
  predicted_signals에 있어야 하고, 그 술어의 tool·target_id·aspect·
  metric·entity_key·window는 그 레코드에 적힌 값을 **글자 그대로 복사**
  해야 한다(tool ← 레코드의 source, metric ← effect.metric, window ←
  window.class). 한 자라도 다르면 그 eid는 근거로 세지 않으며, 남는
  근거가 0이면 후보는 반려된다.
- 각 후보는 술어 2개 이상을 내고, 그중 **색인에 없는 새 관측**을 예측하는
  role="necessary" 술어를 1개 이상 포함해야 한다. "새 관측"이란 주어진
  레코드 어느 것과도 (tool, target_id, metric, entity_key)가 겹치지 않는
  관측이다 — 같은 관측의 window만 바꾼 것은 새 관측이 아니다.
  "이 가설이 참이면 반드시 보여야(또는 안 보여야) 하는데 아직 아무도 재지
  않은 것"을 예측하라 — 예: 원인 대상의 db_blocking present, 특정 로그
  템플릿 present, 아직 안 잰 지표의 magnitude_ge. 이 술어의 eid_hint는
  ""로 비운다. 이미 관측된 사실을 necessary로 베끼면 "예측이 아니라
  회고"로 반려된다.

두 요건을 함께 만족하는 서식 예시(인용 술어 1 + 신규 necessary 1):
  "predicted_signals": [
    {"eid_hint": "EIX-0007", "tool": "scan_metrics",
     "target_id": "db:banking-mariadb-01", "aspect": "db",
     "metric": "lock_wait_seconds", "entity_key": "",
     "predicate": "direction_up", "window": "full",
     "expectation": "must_hold", "role": "corroborating"},
    {"eid_hint": "", "tool": "db_blocking",
     "target_id": "db:banking-mariadb-01", "aspect": "db",
     "metric": "blocked_sessions", "entity_key": "",
     "predicate": "present", "window": "onset_narrow",
     "expectation": "must_hold", "role": "necessary"}
  ],
  "support_eids": ["EIX-0007"]
  — 첫 술어는 EIX-0007 레코드의 값을 그대로 베낀 인용이고(그래서 근거로
  결박된다), 둘째는 색인에 없는 관측이라 이 가설을 죽일 수 있다.
- prior_rationale을 먼저 쓰고, 그 근거에 맞는 prior(low|medium|high)를
  정하라. 근거가 약하면 정직하게 low로 — 후보를 부풀리지 마라.
- 확실한 후보가 없으면 빈 목록을 내라. 억지 후보 금지.
- 입력에 feedback이 있으면 직전 시도가 등록 규칙에 걸려 전부 반려됐다는
  뜻이다(§6.2-6 재생성). 같은 위반을 되풀이하지 마라.

출력은 JSON만:
{"candidates": [
  {"target_id": "...",
   "chain": [{"entity": "...", "effect": "..."}, ...],
   "identity": {"cause_entity": "...", "mechanism": "...",
                "impact_scope": ["..."], "temporal_claim": "precedes"},
   "predicted_signals": [
     {"eid_hint": "", "tool": "...", "target_id": "...", "aspect": "...",
      "metric": "...", "entity_key": "", "predicate": "direction_up",
      "window": "onset_narrow", "expectation": "must_hold",
      "role": "necessary"}
   ],
   "support_eids": ["..."],
   "prior_rationale": "...",
   "prior": "low|medium|high"}
]}`

// 출처별 역할 문단 — 문서 §4.4 v1 그대로.
const (
	roleChange = `당신의 시야는 변경 이력이다. 사건 시간창 직전과 도중의 배포·설정
변경 목록을 받는다. 변경이 증상을 일으킬 수 있는 인과 경로가 있는
변경만 후보로 만들어라 — 변경의 대상이 의심 대상이다. 변경과
증상 사이에 시간·경로상 연결이 없으면 후보로 만들지 마라.`

	roleMember = `당신의 시야는 인시던트 멤버 이벤트다. 가장 이르거나 가장 심한
anomaly 멤버가 가리키는 대상을 의심 대상으로 후보를 만들어라.
증상 자체(대표 현상의 대상)를 원인으로 되풀이하는 후보는 만들지
마라 — 증상보다 앞서거나 더 깊은 지점을 찾아라. 단서가 약하면
후보 수를 줄이고 prior를 낮게 잡아라.`

	roleInvestigation = `당신의 시야는 [3] 스크리닝 배터리가 남긴 관측 레코드(evidence
index)다. 레코드 하나는 개체 하나의 관측이며 eid·target_id·
status(anomalous|normal|no_data)·availability·headline·effect
(kind/metric/observed/baseline/magnitude/direction)·window로
주어진다. anomalous 레코드, 특히 증상 멤버가 아닌 조용한 이웃
(quiet=true)의 레코드를 놓치지 마라 — 직접 알람이 없는 상류가
진짜 원인인 사건이 많다. normal/no_data 레코드는 후보 근거가
아니다(배제·반증 재료다). rollups는 대상별 요약이고, abbreviated는
예산 때문에 한 줄로 접힌 레코드다 — 관측 키(source·aspect·metric·
entity_key·window)는 남아 있으니 술어의 selector와 eid_hint 결박에
**글자 그대로** 쓰라. metric이 빈 문자열이면 술어의 metric도 빈
문자열이어야 한다 — headline에서 지어내면 결박이 깨진다. truncation은
접힌 수다 — 접혔다는 것이 "없다"는 뜻이 아니다.`
)

// SourceAdapter는 pipeline.CandidateSource의 실 LLM 구현체 하나다 —
// 출처(§5)마다 역할 문단과 payload 빌더가 다르다.
type SourceAdapter struct {
	Client *Client
	source ledger.HypoSource
	role   string
	build  func(in pipeline.GenerateInput) any
}

// ConsumesIndex는 pipeline.IndexConsumingSource의 구현이다 — **재생성(§7.5)이
// 이 출처를 다시 부를 것인가**.
//
// investigation 출처만 참이다: 그 시야가 evidence index 자체라(buildIndexPayload)
// widening이 index를 갱신하면 입력이 실제로 달라진다. change·member는 부분
// 색인을 곁들이지만 시야의 본체가 [2] 변경 목록·[1] 멤버 그룹이고 둘 다
// 인시던트 창의 함수라 index가 갱신돼도 같은 값이다 — 다시 물으면 같은 답에
// 43k를 두 번 낸다(§7.6 재생성 43k 모양).
func (a *SourceAdapter) ConsumesIndex() bool { return a.source == ledger.SourceInvestigation }

// NewChangeSource — 시야: [2] 변경 조회 결과.
func NewChangeSource(c *Client) *SourceAdapter {
	return &SourceAdapter{Client: c, source: ledger.SourceChange, role: roleChange,
		build: func(in pipeline.GenerateInput) any {
			return map[string]any{
				"symptom":   symptomPayload(in.Triage),
				"neighbors": neighborIDs(in.Triage),
				"changes":   in.Changes.Changes,
				// evidence — change 레코드만의 부분 색인(3c 실측: 색인 없이는
				// SupportEIDs 인용이 원천 불가라 이 출처가 전멸했다. §6.2
				// "deploy 계열 가설의 SupportEIDs·술어가 change 행을 쓴다").
				"evidence": noteTrunc(in, buildIndexPayloadWhere(in.Evidence, symptomMemberSet(in.Triage),
					func(r evidence.EvidenceIndexRecord) bool {
						return r.Provenance.Source == evidence.SrcChange
					})),
				"vocabulary": vocabPayload(in),
				"feedback":   in.Feedback,
			}
		}}
}

// NewMemberSource — 시야: Triage 멤버 그룹.
func NewMemberSource(c *Client) *SourceAdapter {
	return &SourceAdapter{Client: c, source: ledger.SourceMember, role: roleMember,
		build: func(in pipeline.GenerateInput) any {
			members := make([]map[string]any, len(in.Triage.MemberGroups))
			for i, m := range in.Triage.MemberGroups {
				members[i] = map[string]any{
					"target_id": m.TargetID, "metric": m.Metric, "count": m.Count,
					"first_at": m.FirstAt.Format(time.RFC3339), "last_at": m.LastAt.Format(time.RFC3339),
					"max_severity": m.MaxSeverity, "has_anomaly": m.HasAnomaly,
				}
			}
			// evidence — 증상 멤버 대상 레코드만의 부분 색인(3c 실측: 색인
			// 없이는 SupportEIDs 인용이 원천 불가라 이 출처가 전멸했다).
			memberSet := symptomMemberSet(in.Triage)
			return map[string]any{
				"symptom":   symptomPayload(in.Triage),
				"neighbors": neighborIDs(in.Triage),
				"members":   members,
				"evidence": noteTrunc(in, buildIndexPayloadWhere(in.Evidence, memberSet,
					func(r evidence.EvidenceIndexRecord) bool {
						return memberSet[r.TargetID]
					})),
				"vocabulary": vocabPayload(in),
				"feedback":   in.Feedback,
			}
		}}
}

// NewInvestigationSource — 시야: [3] evidence index (+증상 맥락).
//
// 종전 시야는 pipeline.Finding 목록이었다. [3] 산출이 index로 교체되면서
// (스펙 §4) 이 출처의 입력도 index 레코드가 되고, 직렬화는 §5.3의 입력
// 예산 25k·절단 우선순위를 따른다(indexpayload.go).
func NewInvestigationSource(c *Client) *SourceAdapter {
	return &SourceAdapter{Client: c, source: ledger.SourceInvestigation, role: roleInvestigation,
		build: func(in pipeline.GenerateInput) any {
			return map[string]any{
				"symptom":    symptomPayload(in.Triage),
				"evidence":   noteTrunc(in, buildIndexPayload(in.Evidence, symptomMemberSet(in.Triage))),
				"vocabulary": vocabPayload(in),
				"feedback":   in.Feedback,
			}
		}}
}

// vocabPayload는 후보 어휘 열거다 — **작문이 아니라 선택**으로 만드는 재료다
// (§6.2-1 등록 규칙 1의 어휘가 그대로 프롬프트에 실린다). 이 방식은
// cmd/gate26b가 26B 유효율 실측(술어 96.9%·재시도 후 98.8%)으로 검증한 선례다.
//
// 어휘 4종의 원천이 각각 다르다:
//   - target_id — 위상 어휘(§6.2-1ⓐ, [3] ScreenResult.Targets ∪ 확장)
//   - tool·aspect·predicate — 사상표(§5.1)가 코드 정본
//   - metric — 이미 관측된 레코드의 판정 축(실재 검사는 하지 않지만, 실물
//     지표 이름을 보여주면 지어내기가 줄어든다. Metric 실재는 규칙 1이
//     검사하지 않는다 — prospective 술어와 양립해야 하므로)
func vocabPayload(in pipeline.GenerateInput) map[string]any {
	tools := make([]map[string]any, 0, 12)
	seenSrc := map[evidence.ObservationSource]bool{}
	for _, row := range evidence.Rows() { // 사상표 행이 있는 원천만(§5.1 "술어 불가 도구" 제외)
		src := row.Source
		if seenSrc[src] {
			continue
		}
		seenSrc[src] = true
		aspects := evidence.AspectOf(src)
		as := make([]string, len(aspects))
		for i, a := range aspects {
			as[i] = string(a)
		}
		preds := evidence.AllowedUnion(src)
		ps := make([]string, len(preds))
		for i, p := range preds {
			ps[i] = string(p)
		}
		tools = append(tools, map[string]any{
			"tool": string(src), "aspects": as, "allowed_predicates": ps,
		})
	}
	return map[string]any{
		"target_ids": in.Vocab.IDs(),
		"tools":      tools,
		"metrics":    observedMetrics(in.Evidence),
		"windows":    []string{string(evidence.WindowFull), string(evidence.WindowOnsetNarrow)},
	}
}

// observedMetrics는 index active 레코드가 실제로 쓴 판정 축 이름이다(정렬·중복 제거).
func observedMetrics(ix *evidence.Index) []string {
	if ix == nil {
		return nil
	}
	seen := map[string]bool{}
	var out []string
	for _, r := range ix.Active() {
		if r.Effect.Metric == "" || seen[r.Effect.Metric] {
			continue
		}
		seen[r.Effect.Metric] = true
		out = append(out, r.Effect.Metric)
	}
	sort.Strings(out)
	return out
}

// symptomMemberSet은 증상권 대상이다 — 레코드의 quiet 딱지(조용한 이웃)를
// 규칙이 계산하는 근거다(종전 RunExamine이 Finding.Quiet에 하던 계산).
func symptomMemberSet(tri pipeline.TriageResult) map[string]bool {
	m := map[string]bool{}
	if tri.Symptom.TargetID != "" {
		m[tri.Symptom.TargetID] = true
	}
	for _, g := range tri.MemberGroups {
		m[g.TargetID] = true
	}
	return m
}

func symptomPayload(tri pipeline.TriageResult) map[string]any {
	return map[string]any{
		"target_id": tri.Symptom.TargetID, "metric": tri.Symptom.Metric,
		"observed": tri.Symptom.Observed, "baseline": tri.Symptom.Baseline,
		"direction": tri.Symptom.Direction, "severity": tri.Symptom.Severity,
		"window": map[string]string{
			"from": tri.From.Format(time.RFC3339), "to": tri.To.Format(time.RFC3339),
		},
	}
}

// neighborIDs는 증상 인접 후보(Triage 이웃 확장)의 ID 목록이다 —
// 변경·멤버 출처가 "경로상 연결"을 판단하는 재료.
func neighborIDs(tri pipeline.TriageResult) []string {
	out := make([]string, len(tri.Candidates))
	for i, c := range tri.Candidates {
		out[i] = c.TargetID
	}
	return out
}

// candJSON은 후보 서식이다(§6 가설 계약). refutation_condition은 폐기됐고
// (§14-3, 3차 C-13) 그 자리를 typed 술어 3필드가 대신한다.
//
// **pred_id는 서식에 없다** — 기계가 부여한다(§6 B-5: LLM이 쓰면 같은 ID를
// 두 번 써서 미심사 술어를 유효 술어 셈에 넣는 세탁이 가능하다).
type candJSON struct {
	TargetID string `json:"target_id"`
	Chain    []struct {
		Entity string `json:"entity"`
		Effect string `json:"effect"`
	} `json:"chain"`
	Identity struct {
		CauseEntity   string   `json:"cause_entity"`
		Mechanism     string   `json:"mechanism"`
		ImpactScope   []string `json:"impact_scope"`
		TemporalClaim string   `json:"temporal_claim"`
	} `json:"identity"`
	PredictedSignals []predJSON `json:"predicted_signals"`
	SupportEIDs      []string   `json:"support_eids"`
	PriorRationale string `json:"prior_rationale"`
	Prior          string `json:"prior"`
}

// predJSON은 SignalPred의 LLM 서식이다 — 기계 딱지(pre_satisfied 등)는
// 등록 규칙이 계산하므로 서식에 없다(§6).
type predJSON struct {
	EIDHint     string   `json:"eid_hint"`
	Tool        string   `json:"tool"`
	TargetID    string   `json:"target_id"`
	Aspect      string   `json:"aspect"`
	Metric      string   `json:"metric"`
	EntityKey   string   `json:"entity_key"`
	Predicate   string   `json:"predicate"`
	Threshold   *float64 `json:"threshold"`
	Window      string   `json:"window"`
	Expectation string   `json:"expectation"`
	Role        string   `json:"role"`
}

type candOut struct {
	Candidates []candJSON `json:"candidates"`
}

func (s *SourceAdapter) Propose(ctx context.Context, in pipeline.GenerateInput) ([]pipeline.Candidate, error) {
	body, err := json.MarshalIndent(s.build(in), "", " ")
	if err != nil {
		return nil, fmt.Errorf("candidates(%s): 직렬화: %w", s.source, err)
	}
	system := strings.Replace(candidateSkeleton, "{source_role}", s.role, 1)
	// 호출처 딱지(§14-7 ③) — 출처별 예산행(§11)의 귀속 키.
	ctx = WithPurpose(ctx, "candidates_"+string(s.source))
	var out candOut
	if err := s.Client.CompleteJSON(ctx, system, "입력:\n"+string(body), &out); err != nil {
		return nil, fmt.Errorf("candidates(%s): %w", s.source, err)
	}

	cands := make([]pipeline.Candidate, 0, len(out.Candidates))
	for _, c := range out.Candidates {
		prior, err := parsePrior(c.Prior)
		if err != nil {
			return nil, fmt.Errorf("candidates(%s): %w", s.source, err)
		}
		chain := make([]ledger.ChainStep, len(c.Chain))
		for i, st := range c.Chain {
			chain[i] = ledger.ChainStep{Entity: st.Entity, Effect: st.Effect}
		}
		preds := make([]ledger.SignalPred, len(c.PredictedSignals))
		for i, p := range c.PredictedSignals {
			// 어휘 위반은 여기서 거르지 않는다 — 등록 규칙 1(§6.2)이
			// 판정하고 반려 사유를 감사 기록으로 남긴다. 여기서 미리
			// 삼키면 그 사유가 3b의 재요구 피드백에서 사라진다.
			preds[i] = ledger.SignalPred{
				EIDHint: p.EIDHint, Tool: evidence.ObservationSource(p.Tool),
				TargetID: p.TargetID, Aspect: evidence.Aspect(p.Aspect),
				Metric: p.Metric, EntityKey: p.EntityKey,
				Predicate: evidence.Predicate(p.Predicate), Threshold: p.Threshold,
				Window:      evidence.WindowClass(p.Window),
				Expectation: evidence.Expectation(p.Expectation),
				Role:        ledger.PredRole(p.Role),
			}
		}
		cands = append(cands, pipeline.Candidate{
			TargetID: c.TargetID, Chain: chain, Source: s.source,
			Prior: prior, PriorRationale: c.PriorRationale,
			IdentityKey: ledger.HypoIdentity{
				CauseEntity: c.Identity.CauseEntity, Mechanism: c.Identity.Mechanism,
				ImpactScope:   c.Identity.ImpactScope,
				TemporalClaim: ledger.TemporalClaim(c.Identity.TemporalClaim),
			},
			PredictedSignals: preds,
			SupportEIDs:      c.SupportEIDs,
		})
	}
	return cands, nil
}

// parsePrior — 모르는 값은 오류다(조용한 기본값 금지, 문서 §1).
func parsePrior(s string) (ledger.Prior, error) {
	switch s {
	case "low":
		return ledger.PriorLow, nil
	case "medium":
		return ledger.PriorMedium, nil
	case "high":
		return ledger.PriorHigh, nil
	}
	return 0, fmt.Errorf("prior 값 이상: %q", s)
}
