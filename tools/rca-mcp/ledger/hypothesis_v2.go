// 가설 계약 v2의 타입 — docs/spec-agent-structure.md §6(가설 계약)·
// §7.1(ProbeRequest)·§7.3(ChainClaim)이 정본이다.
//
// 이 파일은 **추가만** 한다. 구 타입 중 ProbeSpec·PredictionDecl 계열은
// §14-4 4c에서 소비자와 함께 삭제됐고, 남은 구 필드는 §14-5가 옮길 때까지
// 공존한다.
package ledger

import (
	"bytes"
	"encoding/json"
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// Prior는 가설 생성 시 부여된 사전확률이다(구 status.go에서 이관 — 판정
// 표면이 아니라 가설 계약의 필드다. 하향 계산은 LowerPrior).
type Prior int

const (
	PriorLow Prior = iota
	PriorMedium
	PriorHigh
)

// ── 술어 어휘 ───────────────────────────────────────────────────

// Predicate는 닫힌 6종이다(§6 SignalPred.Predicate).
//
// 정의는 evidence 패키지로 옮겼다(§14-1 1b) — 사상표의 "허용 Predicate" 열이
// 사상표와 같은 자리에 있어야 하는데 evidence는 ledger를 import할 수 없다.
// **별칭이라 동일 타입이다** — 기존 사용처(ledger.PredPresent 등)는 그대로다.
type Predicate = evidence.Predicate

const (
	PredPresent       = evidence.PredPresent
	PredAbsent        = evidence.PredAbsent
	PredMagnitudeGE   = evidence.PredMagnitudeGE
	PredMagnitudeLT   = evidence.PredMagnitudeLT
	PredDirectionUp   = evidence.PredDirectionUp
	PredDirectionDown = evidence.PredDirectionDown
)

func ValidPredicate(p Predicate) bool { return evidence.ValidPredicate(p) }

// needsThreshold — magnitude_* 에서만 Threshold가 허용·필수다(§6).
func needsThreshold(p Predicate) bool {
	return p == PredMagnitudeGE || p == PredMagnitudeLT
}

// Expectation은 must_hold | must_not_hold다. **bool이 아니다** — JSON
// 누락이 false로 떨어져 우발 반증을 만드는 위험 때문에 3값(빈 값 포함)이
// 구분되는 문자열로 둔다(§6 재검토 실측).
//
// **정의는 evidence에 있고 여기는 별칭이다**(1c 이동, Predicate와 같은 사유):
// truth 파생(evidence.DeriveTruth·Holds)이 이 값과 짝을 이루는데 import는
// ledger→evidence 방향으로만 가능하다. 별칭이라 사용처는 한 줄도 바뀌지 않는다.
type Expectation = evidence.Expectation

const (
	ExpectMustHold    = evidence.ExpectMustHold
	ExpectMustNotHold = evidence.ExpectMustNotHold
)

func ValidExpectation(e Expectation) bool { return evidence.ValidExpectation(e) }

// PredRole은 술어의 반증력이다. **Role=necessary만 불일치가 invalidated를
// 낳는다** — corroborating 불일치는 inconclusive(지지 실패일 뿐)이며,
// 보조 상관 하나가 정답 가설을 죽이는 것을 막는다(§6).
type PredRole string

const (
	RoleNecessary     PredRole = "necessary"
	RoleCorroborating PredRole = "corroborating"
)

func ValidPredRole(r PredRole) bool {
	return r == RoleNecessary || r == RoleCorroborating
}

// SignalPred는 기계 비교 가능한 예측 술어다(§6).
// **시간 선후는 여기 없다** — HypoIdentity.TemporalClaim 단독이고 판정은 §10.
type SignalPred struct {
	// PredID — R 심사·판정·장부의 대응 키. **기계가 부여한다**
	// (AssignPredIDs) — LLM이 쓰면 같은 가설이 "H1-P1"을 두 번 써서
	// 심사받지 않은 술어를 유효 술어 셈에 넣는 세탁이 가능하다(B-5).
	PredID string `json:"pred_id"`
	// EIDHint — 기존 index 레코드를 지목하는 예측이면 그 EID.
	EIDHint string `json:"eid_hint,omitempty"`
	// Tool — 사상표(§5.1)에 행이 있는 원천만. 실행 채널 해소는
	// evidence.ExecutableTool(§7.1 하네스 조립).
	Tool evidence.ObservationSource `json:"tool"`
	// TargetID — 위상 어휘(§6.2-1ⓐ [1]∪A0∪W3, append-only)에서만.
	TargetID string          `json:"target_id"`
	Aspect   evidence.Aspect `json:"aspect"`
	// Metric — 판정 축. 이것이 없으면 한 대상의 레코드 20개 중 무엇을
	// 비교할지가 미정이다(재검토 실측).
	Metric string `json:"metric,omitempty"`
	// EntityKey — 사상표 EntityKey 정본 열의 어휘만(sql_key·사건 ID·
	// episode_id·템플릿·진입점·pid). **세션은 키가 아니다**(db_blocking
	// 행). 있으면 그 개체 레코드로, 없으면 (Target,Aspect,Metric) 해소.
	EntityKey   string               `json:"entity_key,omitempty"`
	Predicate   Predicate            `json:"predicate"`
	Threshold   *float64             `json:"threshold,omitempty"`
	Window      evidence.WindowClass `json:"window"`
	Expectation Expectation          `json:"expectation"`
	Role        PredRole             `json:"role"`

	// ── 기계 딱지 — 등록 시 규칙이 계산한다. LLM 선언이 아니다(§6).
	//
	// **직렬화된다(3b 결정, 3a 인계 ①)**: 종전 `json:"-"`는 journal 재생
	// (§15.5 크래시 계약)에서 딱지를 통째로 잃었다 — 크래시 후 재개하면
	// audited_out 술어가 유효로 되살아나고 pre_satisfied가 §8 신규 관측
	// 요건에 산입돼, **재생이 판정을 바꾼다**. 내구성과 주입 방어는 충돌하지
	// 않는다: 주입 방어의 정본은 "디코드 시 무시"가 아니라 **LLM 입력 서식에
	// 애초에 없음**이며(3a가 pred_id에 쓴 방식과 동일), 실물이 그렇다 —
	// llm/candidates.go predJSON(후보 서식)·llm/audit.go predView(R 심사
	// 서식) 어느 쪽에도 이 세 필드가 없어 LLM은 값을 쓸 수단이 없다.
	// omitempty라 거짓 딱지는 서식에 나타나지도 않는다.
	//
	// PreSatisfied: 선언 시점에 이미 충족(회고) — §8 신규 관측 요건 불산입.
	PreSatisfied bool `json:"pre_satisfied,omitempty"`
	// Undecidable: 형식·어휘 위반으로 판정 불능 — 등록 반려 사유.
	Undecidable bool `json:"undecidable,omitempty"`
	// AuditedOut: 심사점 R 탈락(무효) — 셈·정렬 제외(§6.2-5).
	AuditedOut bool `json:"audited_out,omitempty"`
}

// ObservationKey는 이 술어가 지목하는 명제 키다(§6.0).
// **Predicate·Threshold는 키에 들어가지 않는다** — 연산자만 바꾼 변형이
// 새 명제가 되어 반증 가설이 무한 재등록되는 우회를 봉쇄한다.
func (p SignalPred) ObservationKey() evidence.ObservationKey {
	return evidence.ObservationKey{
		Source:    p.Tool,
		TargetID:  p.TargetID,
		Aspect:    p.Aspect,
		Metric:    p.Metric,
		EntityKey: p.EntityKey,
		Window:    p.Window,
	}.Canonical()
}

// ValidateForm은 등록 규칙 1ⓒ(조합 유효)와 어휘 검사를 수행한다.
// 사상표 (Tool,Aspect) 대조와 허용 Predicate 합집합 검사(ⓑ·ⓓ)는 사상표가
// 실물로 확정된 뒤(1b) 붙는다 — 표 없이 여기서 판정하면 근거 없는 반려가 된다.
// **Metric·EntityKey의 실재는 검사하지 않는다** — prospective 술어와
// 양립해야 하므로 실재는 판정 시점에 해소 불능=inconclusive로 드러난다(§6.2-1).
func (p SignalPred) ValidateForm() error {
	if p.PredID == "" {
		return fmt.Errorf("pred_id 없음")
	}
	if !evidence.ValidSource(p.Tool) {
		return fmt.Errorf("tool %q는 사상표에 행이 없음", p.Tool)
	}
	if p.TargetID == "" {
		return fmt.Errorf("target_id 없음")
	}
	if !evidence.ValidAspect(p.Aspect) {
		return fmt.Errorf("aspect %q 미정의", p.Aspect)
	}
	if !ValidPredicate(p.Predicate) {
		return fmt.Errorf("predicate %q 미정의", p.Predicate)
	}
	if needsThreshold(p.Predicate) && p.Threshold == nil {
		return fmt.Errorf("%s에는 threshold 필수", p.Predicate)
	}
	if !needsThreshold(p.Predicate) && p.Threshold != nil {
		return fmt.Errorf("%s에는 threshold를 쓸 수 없음", p.Predicate)
	}
	if !evidence.ValidWindowClass(p.Window) {
		return fmt.Errorf("window %q 미정의", p.Window)
	}
	if !ValidExpectation(p.Expectation) {
		return fmt.Errorf("expectation %q 미정의 — bool 폐기(§6)", p.Expectation)
	}
	if !ValidPredRole(p.Role) {
		return fmt.Errorf("role %q 미정의", p.Role)
	}
	return nil
}

// AssignPredIDs는 술어에 기계 부여 ID를 박는다 — 서식은 "<가설ID>-P<n>"
// (§6). 기계 부여이므로 유일성이 구조적으로 보장되고, 등록 규칙에 별도
// 유일성 검사 조항이 필요 없어진다(B-5의 권고안 채택).
func AssignPredIDs(hypoID string, preds []SignalPred) []SignalPred {
	out := make([]SignalPred, len(preds))
	copy(out, preds)
	for i := range out {
		out[i].PredID = fmt.Sprintf("%s-P%d", hypoID, i+1)
	}
	return out
}

// ValidatePredIDs는 유일성과 비어있지 않음을 검사한다. 기계 부여를 쓰면
// 항상 통과하지만, LLM 산출이나 재생 경로로 들어온 술어에는 이 관문이
// 필요하다 — 같은 PredID 둘이면 R의 배치 심사 결과를 술어에 복원할 수
// 없고("중복은 첫 항목만" 규율이 미심사 술어를 유효로 세탁한다),
// ProbeRequest의 PredID도 어느 관측 키를 실행할지 결정 불능이 된다(B-5).
func ValidatePredIDs(preds []SignalPred) error {
	seen := map[string]bool{}
	for _, p := range preds {
		if p.PredID == "" {
			return fmt.Errorf("pred_id 없는 술어")
		}
		if seen[p.PredID] {
			return fmt.Errorf("pred_id %s 중복 — 심사 결과를 술어에 복원 불가", p.PredID)
		}
		seen[p.PredID] = true
	}
	return nil
}

// ── 가설 정체성 ─────────────────────────────────────────────────

// TemporalClaim은 시간 선후 주장의 정본이다(§6 HypoIdentity·§10).
type TemporalClaim string

const (
	TemporalPrecedes  TemporalClaim = "precedes"
	TemporalCoincides TemporalClaim = "coincides"
	TemporalUnknown   TemporalClaim = "unknown"
)

func ValidTemporalClaim(t TemporalClaim) bool {
	return t == TemporalPrecedes || t == TemporalCoincides || t == TemporalUnknown
}

// HypoIdentity는 가설의 정체성이다(§6).
type HypoIdentity struct {
	// CauseEntity — canonical 대상 ID. 미지면 sentinel "unknown:<증상엔티티>".
	// sentinel은 다양성 계수에서 제외된다(§6.2 — 대항 가설은 다양성 충족
	// 수단이 아니다).
	CauseEntity string
	// Mechanism — 자유 서술. **닫힌 enum이 아니다**(§6.1, 사용자 결정
	// 07-31 — 닫힌 목록은 세계를 목록에 맞추는 설계다).
	Mechanism     string
	ImpactScope   []string
	TemporalClaim TemporalClaim
}

// UnknownCause는 대항 가설의 sentinel CauseEntity를 만든다(§6.2 권장 구성).
func UnknownCause(symptomEntity string) string { return "unknown:" + symptomEntity }

// IsSentinelCause는 다양성 계수 제외 대상인지다.
func IsSentinelCause(cause string) bool { return len(cause) >= 8 && cause[:8] == "unknown:" }

// ── 인과사슬 ────────────────────────────────────────────────────

// ChainKind는 사슬 구간의 종류다(§7.3).
type ChainKind string

const (
	ChainRootMechanism   ChainKind = "root_mechanism"
	ChainPropagationEdge ChainKind = "propagation_edge"
	ChainTerminalEntity  ChainKind = "terminal_entity"
)

func ValidChainKind(k ChainKind) bool {
	return k == ChainRootMechanism || k == ChainPropagationEdge || k == ChainTerminalEntity
}

// ClaimOrigin은 사슬 구간 문장을 누가 썼는지다 — §7.7 검수 면제 판정의
// 근거다. 말단 문장은 projector가 typed 레코드에서 기계 조립하므로
// (엉뚱한 개체를 말로 포장할 자유가 애초에 없다) 검수 대상이 아니다.
type ClaimOrigin string

const (
	OriginLLM       ClaimOrigin = "llm"
	OriginProjector ClaimOrigin = "projector"
)

func ValidClaimOrigin(o ClaimOrigin) bool { return o == OriginLLM || o == OriginProjector }

// ChainClaim은 인과사슬의 한 구간이다(§7.3). 구 ChainStep의 `chain_step
// int` 근거 링크를 ClaimID 지목으로 교체한다 — 정밀화로 배열이 바뀌면
// 과거 링크의 의미가 변해 append-only와 충돌했다(재검토 실측).
type ChainClaim struct {
	// ClaimID — 가설 내 유일, revision을 넘어 불변 지시 불가. 새 revision은
	// 새 ClaimID + Supersedes다.
	ClaimID string
	Kind    ChainKind
	// EntityKey — terminal_entity면 필수. §5.1 사상표 EntityKey 정본 열과
	// 같은 어휘·정규화를 쓴다 — index 레코드 필드와의 typed 동치 비교가
	// §8 말단 게이트의 판정식이기 때문이다.
	EntityKey string
	Effect    string
	Origin    ClaimOrigin
	// Supersedes — 정밀화가 대체한 이전 ClaimID. 교체가 아니라 append다.
	Supersedes string
}

// Validate는 사슬 구간의 형식 계약이다.
func (c ChainClaim) Validate() error {
	if c.ClaimID == "" {
		return fmt.Errorf("claim_id 없음")
	}
	if !ValidChainKind(c.Kind) {
		return fmt.Errorf("chain kind %q 미정의", c.Kind)
	}
	if !ValidClaimOrigin(c.Origin) {
		return fmt.Errorf("claim origin %q 미정의 — §7.7 면제 판정 불가", c.Origin)
	}
	if c.Kind == ChainTerminalEntity && c.EntityKey == "" {
		return fmt.Errorf("terminal_entity에는 entity_key 필수 — §8 말단 게이트의 판정 입력")
	}
	if c.Effect == "" {
		return fmt.Errorf("effect 없음")
	}
	return nil
}

// ── probe 요청 ──────────────────────────────────────────────────

// ProbeRequest는 조사자 LLM의 유일한 출력이다(§7.1).
// **핵심 파라미터(도구·대상·창)는 LLM이 쓰지 않는다** — 조사자는 심사된
// PredID를 지목하고, 하네스가 그 명제의 관측 키(§6.0)에서 도구·인자를
// 조립한다. WindowClass·ResolutionHint를 두지 않는 이유도 같다(4차 A-4):
// 창은 명제 키의 성분이라 PredID가 이미 결정하고, 해상도는 실물
// read_timeseries에 파라미터 자체가 없다(scanStep 30s 고정).
//
// **이 타입은 comparable이 아니다**(PredIDs []string) — 구 코드의
// `map[ProbeSpec]bool` 식 dedup을 그대로 옮길 수 없었다(B-10, §14-4 4c에서
// 폐기 완료: pipeline/generate.go의 병합 dedup이 그 자리다). 중복 판정은
// 요청이 아니라 **명제 키 기준**이다: evidence.ObservationKey는 비교
// 가능한 값 타입이라 그대로 map 키로 쓴다.
type ProbeRequest struct {
	// PredIDs — 심사 통과 술어만. 미지·중복·빈 목록은 반려.
	// **전 항목이 동일 canonical 관측 키여야 한다**(4차 A-4 — 키가 섞이면
	// 단일 도구 호출로 인자를 결정할 수 없고, 한 요청이 여러 도구 호출로
	// fan-out해 probe 상한을 우회한다). 예산 차감은 요청 수가 아니라
	// 실제 도구 호출 수다.
	PredIDs []string `json:"pred_ids"`
	Reason  string   `json:"reason"`
}

// DecodeProbeRequest는 **strict** 디코드다 — 모르는 필드를 거부하고
// 필수 누락을 반려한다(§7.1). 현행 어댑터는 json_object + 일반
// Unmarshal이라 누락이 zero value로, 오타 필드가 무시로 조용히 흘렀다.
func DecodeProbeRequest(data []byte) (ProbeRequest, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var r ProbeRequest
	if err := dec.Decode(&r); err != nil {
		return ProbeRequest{}, fmt.Errorf("probe_request 디코드: %w", err)
	}
	if dec.More() {
		return ProbeRequest{}, fmt.Errorf("probe_request 뒤에 잉여 토큰")
	}
	if err := r.Validate(); err != nil {
		return ProbeRequest{}, err
	}
	return r, nil
}

// Validate는 형식 계약이다. PredID의 실재·심사 통과 여부와 관측 키
// 동일성은 명제 장부가 있어야 판정되므로 하네스(1c 이후)가
// SameObservationKey로 이어서 검사한다.
func (r ProbeRequest) Validate() error {
	if len(r.PredIDs) == 0 {
		return fmt.Errorf("pred_ids 비어 있음 — 지목 없는 요청은 반려")
	}
	seen := map[string]bool{}
	for _, id := range r.PredIDs {
		if id == "" {
			return fmt.Errorf("빈 pred_id")
		}
		if seen[id] {
			return fmt.Errorf("pred_id %s 중복", id)
		}
		seen[id] = true
	}
	if r.Reason == "" {
		return fmt.Errorf("reason 필수 — ledger 기록용")
	}
	return nil
}

// SameObservationKey는 §7.1의 "전 항목이 동일 canonical 관측 키" 계약을
// 검사한다. 하네스가 PredID → 명제 키로 해소한 결과를 넘긴다.
func SameObservationKey(keys []evidence.ObservationKey) error {
	if len(keys) == 0 {
		return fmt.Errorf("해소된 관측 키 없음")
	}
	first := keys[0].Canonical()
	for _, k := range keys[1:] {
		if k.Canonical() != first {
			return fmt.Errorf("관측 키가 섞임(%s ≠ %s) — 단일 도구 호출로 인자 결정 불가",
				first, k)
		}
	}
	return nil
}
