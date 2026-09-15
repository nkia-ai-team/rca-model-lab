// 심사점 R 어댑터 — docs/spec-agent-structure.md §6.2-5가 정본이다(§14-3 3b).
//
// **왜 verifier.go가 아니라 새 파일인가**: 구 Review(BlindClaim)는 §14-4 4c가
// 삭제한 표면이고(ReviewTiming·reviewLink와 함께), R은 그 자리를 이어받는
// 새 계약이다. 한 파일에 두면 삭제 커밋이 R까지 흔든다. 규율(v3 프롬프트의
// note 먼저·보수 방향·blind)은 문안으로 승계한다.
//
// R이 보는 것과 보지 않는 것(§6.2-5 blind):
//   - 본다: 메커니즘 한 문장 · 인과사슬 · 술어 본체 · **명제 장부의 기왕 관측값**
//   - 보지 않는다: 인시던트 서사·증상 문안 · 사전확률·그 근거 · 출처 · 순위
//     (가설 라벨은 대응 키로 실린다 — §6.2-5가 PredID를 입출력에 명문으로
//     요구하기 때문이다. 대신 배열 순서 외의 순위 정보는 어디에도 없다)
package llm

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// auditSystem은 R의 시스템 프롬프트다. v3 규율 승계: note 먼저 · 확신
// 없으면 불리 방향 · 주어진 것 밖을 가정하지 않는다.
const auditSystem = `당신은 AIOps 가설 술어 심사관이다. 가설의 메커니즘 문장과 인과사슬,
그리고 그 가설이 내건 예측 술어 목록을 받아 **술어 하나하나가 심사할
값어치가 있는지만** 판정한다.

당신은 사건의 전체 맥락·경보 내용·가설의 유력도를 모르며, 알 필요도
없다. 주어진 것 바깥의 사실을 가정하지 마라. 가설이 옳은지는 당신의
몫이 아니다 — 술어의 자격만 본다.

**항목별로 독립 판정하라. 가설끼리 비교하거나 순위를 매기지 마라.**
한 배치에 여러 가설이 들어오는 것은 호출을 줄이기 위함일 뿐이며, 어떤
항목의 판정도 다른 항목의 내용에 좌우되어서는 안 된다.

항목마다 세 가지를 답한다:
- pertinent: 이 술어가 그 메커니즘·사슬이 참일 때 실제로 관련되는가.
  대상·지표가 그 기제와 무관하면 false.
- substantive: 판정 결과가 정보를 낳는가. 어느 쪽으로 나와도 아무것도
  달라지지 않는 술어(하나마나)는 false.
- role_valid: role이 "necessary"이고 observed(기왕 관측값)가 주어진
  항목에만 답한다. **가설이 틀려도 참일 자명한 술어면 false**다 —
  첨부된 관측값으로 이미 충족돼 있고 그 충족이 이 가설과 무관하게
  성립하면 자명하다. observed가 null이거나 role이 "corroborating"이면
  role_valid 필드를 아예 넣지 마라(판정 재료가 없다).

판정 규칙:
- 관측값은 규모와 기준선(baseline) 대비로 해석하라. 방향만으로는
  실질(substantive)의 근거가 아니다.
- 관측값이 없다(null)는 것은 그 술어가 아직 예측이라는 뜻이지 결함이
  아니다 — 그 이유만으로 pertinent·substantive를 false로 내리지 마라.
- 확신이 없으면 불리한 쪽으로 판정하라(pertinent·substantive는 false,
  role_valid는 false).

출력은 JSON만, 항목마다 **note를 먼저 쓰고** 그 결론에 따라 판정을
정하라. 받은 pred_id 전부에 대해 항목 하나씩만 낸다:
{"items": [{"pred_id": "...", "note": "판정 이유 한두 문장",
            "pertinent": true|false, "substantive": true|false,
            "role_valid": true|false}]}`

// reviseSystem은 규칙 5 재요구 1회의 프롬프트다(§6.2-5 "위반 피드백 재요구
// 1회 — 그 가설의 술어 재생성 요청").
const reviseSystem = `당신은 AIOps RCA 가설의 예측 술어를 고쳐 쓰는 작성기다. 가설의
메커니즘·인과사슬·현재 술어 목록과, 이 술어 목록이 등록 규칙을 어떻게
위반했는지(violations)를 받는다.

위반을 해소한 **술어 목록 전체**를 다시 내라. 규칙:
- tool·target_id·aspect·metric·entity_key는 **주어진 술어 목록에 이미
  쓰인 값에서만** 골라 쓴다. 새 이름을 지어내면 그 가설은 반려된다.
- 술어는 2개 이상이고, 그중 **아직 관측되지 않았고(observed가 null)
  어긋나면 이 가설이 죽는** role="necessary" 술어가 1개 이상이어야 한다.
- 이미 관측된 사실을 necessary로 베끼지 마라 — 그것은 예측이 아니다.
- 무관하거나 하나마나인 술어는 되풀이하지 말고 빼라.

미달 사유별 행동 지침:
- "미실측 necessary 0" / "반증력 없음" → observed가 null인 술어 하나를
  role="necessary"로 세워라. 그런 술어가 없으면 **아직 재지 않은 관측**을
  새로 예측하라 — 주어진 술어 어느 것과도 (tool, target_id, metric,
  entity_key)가 겹치지 않아야 하고(window만 바꾼 것은 새 관측이 아니다),
  eid_hint는 ""로 비운다.
- "전부 기실측" / "회고" → observed가 채워진 술어만 남아 있다는 뜻이다.
  아직 재지 않은 관측을 최소 1개 추가하라.
- "역할 무효(role_valid=false)" / "자명" → 그 술어는 가설이 틀려도 참이다.
  role="corroborating"으로 내리고, 가설이 틀리면 어긋날 술어를
  necessary로 새로 세워라.
- "무관(pertinent)" / "하나마나(substantive)" → 그 술어는 지우고, 그
  메커니즘이 참일 때만 나타날 관측으로 바꿔라.

출력은 JSON만:
{"note": "무엇을 어떻게 고쳤는지 한두 문장",
 "predicted_signals": [
   {"eid_hint": "", "tool": "...", "target_id": "...", "aspect": "...",
    "metric": "...", "entity_key": "", "predicate": "direction_up",
    "window": "onset_narrow", "expectation": "must_hold",
    "role": "necessary"}
 ]}`

// Auditor는 pipeline.Auditor(심사점 R)와 pipeline.PredicateReviser(재요구
// 1회)의 실 LLM 구현체다. 둘이 한 타입인 것은 같은 blind 서식을 쓰기
// 때문이다.
type Auditor struct {
	Client *Client
}

// ── 서식 (LLM이 보는 전부) ──────────────────────────────────────

// predView는 술어의 심사 서식이다.
//
// **기계 딱지(pre_satisfied·undecidable·audited_out)는 여기 없다** — 주입
// 방어의 정본은 "디코드 시 무시"가 아니라 **서식에 애초에 없음**이다(3a가
// pred_id에 쓴 방식과 동일, 3b 결정). SignalPred가 그 딱지를 직렬화하게
// 바뀌었어도(§15.5 크래시 재생) LLM 입력은 이 구조체를 거치므로 값을 쓸
// 수단이 없다.
type predView struct {
	PredID      string      `json:"pred_id"`
	Tool        string      `json:"tool"`
	TargetID    string      `json:"target_id"`
	Aspect      string      `json:"aspect"`
	Metric      string      `json:"metric,omitempty"`
	EntityKey   string      `json:"entity_key,omitempty"`
	Predicate   string      `json:"predicate"`
	Threshold   *float64    `json:"threshold,omitempty"`
	Window      string      `json:"window"`
	Expectation string      `json:"expectation"`
	Role        string      `json:"role"`
	Observed    *observedJS `json:"observed"`
}

// observedJS는 명제 장부(§6.0)의 기왕 관측값이다 — **자명성 판정의 유일한
// 재료**(3차 검토: 관측값 없이는 R이 RoleValid를 판정할 수 없었다).
// null이면 미실측이고, 그 항목의 role_valid는 기계가 nil로 강제한다(C-2).
type observedJS struct {
	Present      bool     `json:"present"`
	Metric       string   `json:"metric,omitempty"`
	Observed     *float64 `json:"observed,omitempty"`
	Baseline     *float64 `json:"baseline,omitempty"`
	Magnitude    *float64 `json:"magnitude,omitempty"`
	Direction    string   `json:"direction,omitempty"`
	Availability string   `json:"availability,omitempty"`
	Status       string   `json:"status,omitempty"`
}

type subjectJS struct {
	Hypothesis string `json:"hypothesis"`
	Mechanism  string `json:"mechanism"`
	Chain      []struct {
		Entity string `json:"entity"`
		Effect string `json:"effect"`
	} `json:"chain"`
	Predicates []predView `json:"predicates"`
}

// auditItemJS — **note가 판정보다 앞**(§6.2-5 note 먼저).
type auditItemJS struct {
	PredID      string `json:"pred_id"`
	Note        string `json:"note"`
	Pertinent   bool   `json:"pertinent"`
	Substantive bool   `json:"substantive"`
	RoleValid   *bool  `json:"role_valid"`
}

type auditOutJS struct {
	Items []auditItemJS `json:"items"`
}

// AuditPredicates는 배치 1콜이다(§6.2-5) — 전 가설의 전 술어를 한 번에.
//
// **누락·중복·미지 PredID를 여기서 판정하지 않는다**: 그 규율은 기계층
// (pipeline/audit.go collateAudit)의 몫이고, 어댑터는 받은 항목을 그대로
// 올린다. 정책이 LLM 어댑터에 흩어지면 mock 시험으로 규율을 검사할 수 없다.
func (a *Auditor) AuditPredicates(ctx context.Context, subjects []pipeline.AuditSubject) ([]pipeline.AuditItem, error) {
	if len(subjects) == 0 {
		return nil, nil
	}
	body, err := json.MarshalIndent(map[string]any{"subjects": subjectsJS(subjects)}, "", " ")
	if err != nil {
		return nil, fmt.Errorf("audit: 직렬화: %w", err)
	}
	var out auditOutJS
	if err := a.Client.CompleteJSON(WithPurpose(ctx, "audit_r"), auditSystem, "심사 대상:\n"+string(body), &out); err != nil {
		return nil, fmt.Errorf("audit: %w", err)
	}
	items := make([]pipeline.AuditItem, len(out.Items))
	for i, it := range out.Items {
		items[i] = pipeline.AuditItem{
			PredID: it.PredID, Note: it.Note,
			Pertinent: it.Pertinent, Substantive: it.Substantive, RoleValid: it.RoleValid,
		}
	}
	return items, nil
}

// RevisePredicates는 규칙 5의 재요구 1회다. 반환은 술어 목록뿐이며,
// 어휘·형식 재검사는 하네스(pipeline.Admit)가 다시 한다 — 재요구가 어휘
// 밖 술어를 들여오는 문을 어댑터가 스스로 막지 않는다(정책은 한 자리에).
func (a *Auditor) RevisePredicates(ctx context.Context, subject pipeline.AuditSubject, feedback []pipeline.AdmissionReject) ([]ledger.SignalPred, error) {
	violations := make([]string, len(feedback))
	for i, f := range feedback {
		violations[i] = f.String()
	}
	body, err := json.MarshalIndent(map[string]any{
		"subject": subjectsJS([]pipeline.AuditSubject{subject})[0], "violations": violations,
	}, "", " ")
	if err != nil {
		return nil, fmt.Errorf("audit revise: 직렬화: %w", err)
	}
	var out struct {
		Note             string     `json:"note"`
		PredictedSignals []predJSON `json:"predicted_signals"`
	}
	if err := a.Client.CompleteJSON(WithPurpose(ctx, "audit_r_revise"), reviseSystem, "입력:\n"+string(body), &out); err != nil {
		return nil, fmt.Errorf("audit revise: %w", err)
	}
	preds := make([]ledger.SignalPred, len(out.PredictedSignals))
	for i, p := range out.PredictedSignals {
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
	return preds, nil
}

func subjectsJS(subjects []pipeline.AuditSubject) []subjectJS {
	out := make([]subjectJS, len(subjects))
	for i, s := range subjects {
		js := subjectJS{Hypothesis: s.Hypothesis, Mechanism: s.Mechanism}
		for _, st := range s.Chain {
			js.Chain = append(js.Chain, struct {
				Entity string `json:"entity"`
				Effect string `json:"effect"`
			}{Entity: st.Entity, Effect: st.Effect})
		}
		for _, v := range s.Predicates {
			js.Predicates = append(js.Predicates, predViewOf(v))
		}
		out[i] = js
	}
	return out
}

func predViewOf(v pipeline.PredicateView) predView {
	p := v.Pred
	return predView{
		PredID: p.PredID, Tool: string(p.Tool), TargetID: p.TargetID,
		Aspect: string(p.Aspect), Metric: p.Metric, EntityKey: p.EntityKey,
		Predicate: string(p.Predicate), Threshold: p.Threshold,
		Window: string(p.Window), Expectation: string(p.Expectation),
		Role: string(p.Role), Observed: observedOf(v.Observed),
	}
}

func observedOf(v *evidence.ObservationValue) *observedJS {
	if v == nil {
		return nil
	}
	return &observedJS{
		Present: v.Present, Metric: v.Metric, Observed: v.Observed,
		Baseline: v.Baseline, Magnitude: v.Magnitude,
		Direction: string(v.Direction), Availability: string(v.Availability),
		Status: string(v.Status),
	}
}
