// [4] 입력 직렬화 — evidence index → 프롬프트 payload
// (docs/spec-agent-structure.md §5.3 "입력 예산 25k — 절단 우선순위").
//
// 종전 [4] 입력은 [3]의 Finding{Observation string} 목록이었고 예산 개념이
// 없었다. 산출이 index로 바뀌면서 레코드 수가 대상 수에 선형으로 늘어나므로
// (계층 A만 M+2N+2D+S+ceil(N/10) 호출), 예산과 절단 우선순위가 필요해졌다.
//
// **조용한 절단 금지**: 접거나 축약한 것은 반드시 payload의 truncation 절에
// 수로 남는다 — "절단 사실은 절단하지 않는다"(§5.3-6)를 index 자신에게도
// 적용한 것이다. 축약은 삭제가 아니라 EID+Headline 한 줄로 접는 것이라
// [5]가 fetch·재조회로 회수할 수 있다.
package llm

import (
	"encoding/json"
	"math"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// indexBudgetChars는 §5.3의 입력 예산이다(문자 수). 스펙 문면 "25k"를
// 그대로 문자 수로 읽는다 — 토큰 수는 서빙 토크나이저에 딸려 있어 하네스가
// 결정론으로 셈할 수 없다(판단 지점).
const indexBudgetChars = 25000

// recordPayload는 레코드 한 건의 [4] 표현이다. index 스키마(§5.1) 중
// **가설 생성에 쓰이는 열만** 옮긴다 — LineageID·ParamsDigest 같은 감사
// 열은 프롬프트 예산을 먹고 [4]는 읽지 않는다(감사는 산출물 덤프 몫).
type recordPayload struct {
	EID      string `json:"eid"` // [4][5]의 인용 키(§5.1)
	TargetID string `json:"target_id"`
	Domain   string `json:"domain,omitempty"`
	Aspect   string `json:"aspect,omitempty"`
	// Source — 관측 원천. **합성 원천 change가 여기 그대로 보인다**(B-15):
	// 실행 채널(list_changes)이 아니라 원천 이름이 실린다.
	Source string `json:"source"`
	// Class — 사상표 행 키(§5.1 FindingClass). 어떤 종류의 관측인지.
	Class string `json:"class,omitempty"`
	// EntityKey — 개체 정본 키(§5.6 low-level 정체성 보존).
	EntityKey string `json:"entity_key,omitempty"`
	Headline  string `json:"headline"`
	// Quiet — 증상 멤버가 아닌 대상의 관측인가. 종전 Finding.Quiet의 자리 —
	// 규칙이 계산한다(§4 역할 문단이 "조용한 이웃"을 요구한다).
	Quiet bool `json:"quiet"`

	Status       string   `json:"status"`
	NoDataReason string   `json:"no_data_reason,omitempty"`
	Availability string   `json:"availability"`
	Confidence   string   `json:"confidence"`
	Effect       *effectP `json:"effect,omitempty"`
	Window       windowP  `json:"window"`
	// Truncated·OmittedN — 절단 사실(§5.1 Truncation). normal인데 OmittedN>0
	// 이면 이미 Confidence=low로 강등돼 있다(§5.4-1).
	Truncated bool `json:"truncated,omitempty"`
	OmittedN  int  `json:"omitted_n,omitempty"`
}

type effectP struct {
	Kind      string   `json:"kind"`
	Metric    string   `json:"metric,omitempty"`
	Observed  *float64 `json:"observed,omitempty"`
	Baseline  *float64 `json:"baseline,omitempty"`
	Magnitude *float64 `json:"magnitude,omitempty"`
	Direction string   `json:"direction,omitempty"`
}

type windowP struct {
	From string `json:"from"`
	To   string `json:"to"`
	// ChangeFrom/ChangeTo — 변화 구간(§10의 유일한 입력). 없으면 생략.
	ChangeFrom string `json:"change_from,omitempty"`
	ChangeTo   string `json:"change_to,omitempty"`
	Class      string `json:"class,omitempty"`
}

// abbrevPayload는 §5.3-6의 축약형이다 — EID가 남으므로 [5]가 회수 가능하다.
//
// **결박 재료 포함(§14-7 ④ 라이브 실측·사용자 결정 2026-08-12)**: 종전
// EID+Headline만으로는 후보가 술어의 관측 키(§6.2 결박 검사의 selector
// 완전 일치)를 재현할 재료가 없어, 대형 인시던트(축약 비중 높음)에서
// 유효 인용조차 selector 불일치로 전멸했다(N=11 라이브 — 26B는 payload
// 안 EID를 정확히 인용·eid_hint 결박까지 했는데 entity_key·metric을
// 못 맞춤). 축약은 서사(Findings·Effect)를 접는 것이지 정체(키)를 접는
// 것이 아니다.
type abbrevPayload struct {
	EID      string `json:"eid"`
	TargetID string `json:"target_id"`
	Source   string `json:"source"`
	Aspect   string `json:"aspect,omitempty"`
	// Metric은 빈 값도 싣는다(omitempty 금지 — ④ 2차 재주행 실측: 숨기면
	// 후보가 headline에서 metric을 지어내 그 한 축이 selector 불일치가
	// 된다. 빈 metric도 키의 사실이다).
	Metric    string `json:"metric"`
	EntityKey string `json:"entity_key,omitempty"`
	Window    string `json:"window,omitempty"`
	Headline  string `json:"headline"`
}

// rollupPayload는 §5.2 대상별 롤업 행이다. 접힌 레코드는 여기 카운트로만
// 남는다(§5.3-4).
type rollupPayload struct {
	TargetID    string   `json:"target_id"`
	Anomalous   int      `json:"anomalous"`
	Weak        int      `json:"weak"`
	NoDataViews []string `json:"no_data_views,omitempty"`
}

// truncationNote는 절단 사실의 기록이다(§5.3-5·6 "접은 수를 index 말미에 명시").
type truncationNote struct {
	BudgetChars int `json:"budget_chars"`
	UsedChars   int `json:"used_chars"`
	// FoldedNormalOK — §5.3-4로 항상 접히는 normal+ok 레코드 수.
	FoldedNormalOK int `json:"folded_normal_ok"`
	// FoldedOther — 우선순위 밖(unknown·not_collected 등) 접힌 레코드 수.
	FoldedOther int `json:"folded_other"`
	// FoldedNormalLow — 예산 초과로 접힌 normal+low 레코드 수(§5.3-5, 4→3 순서).
	FoldedNormalLow int `json:"folded_normal_low"`
	// AbbreviatedNoData·AbbreviatedAnomalous — 축약(EID+Headline)된 수.
	AbbreviatedNoData    int `json:"abbreviated_no_data"`
	AbbreviatedAnomalous int `json:"abbreviated_anomalous"`
}

// indexPayload는 [4] 프롬프트에 실리는 index 표현 전체다.
type indexPayload struct {
	Records     []recordPayload `json:"records"`
	Abbreviated []abbrevPayload `json:"abbreviated,omitempty"`
	Rollups     []rollupPayload `json:"rollups"`
	Truncation  truncationNote  `json:"truncation"`
}

// buildIndexPayload는 §5.3의 절단 우선순위를 기계 규칙으로 적용한다.
//
//	1 anomalous 전량 유지
//	2 no_data 중 collector_gap·ttl_expired·backend_error + observed_zero 전량
//	3 normal + Confidence=low
//	4 normal + ok는 레코드를 접고 롤업 카운트로만 (**항상**)
//	5 그래도 초과면 4→3 순서로 접는다
//	6 anomalous만으로 초과면 2차 정렬 후 초과분을 **축약**(EID+Headline)
//
// 판단 지점 ①: 4는 예산과 무관하게 항상 접는다 — 스펙 문면이 "레코드를
// 접고 롤업 카운트로만"이라 조건절이 없다.
// 판단 지점 ②: 2가 남아도 초과인 경우는 스펙에 없다. 삭제하면 최강 배제
// 근거가 증발하므로 6과 같은 축약을 2에 먼저 적용하고(그다음이 1이다)
// 그 수를 기록한다 — 우선순위 역전 없이 정보 손실만 최소화한다.
// 판단 지점 ③: 우선순위 표에 없는 no_data(unknown·not_collected)는 4와 같이
// 접는다 — 2의 열거가 닫힌 목록이기 때문이다. 수는 FoldedOther로 남는다.
func buildIndexPayload(ix *evidence.Index, symptomMembers map[string]bool) indexPayload {
	return buildIndexPayloadWhere(ix, symptomMembers, nil)
}

// buildIndexPayloadWhere는 keep이 참인 finding 레코드만 싣는 판본이다 —
// 출처별 시야 분리(3c 실측: member·change 출처가 색인을 아예 못 받아
// SupportEIDs 인용이 원천 불가였다. 전량을 세 출처에 중복 전송하면 §5.3
// 예산 문제가 3배가 되므로, 출처 역할에 맞는 부분 색인을 준다 — change
// 출처는 change 레코드, member 출처는 증상 멤버 레코드). keep=nil이면 전량.
// **필터는 레코드에만 건다** — 롤업은 전 대상 유지(어느 대상이 이상한지의
// 맥락은 시야가 좁아도 필요하다), 절단 기록은 필터 후 집합 기준.
func buildIndexPayloadWhere(ix *evidence.Index, symptomMembers map[string]bool,
	keep func(evidence.EvidenceIndexRecord) bool) indexPayload {
	out := indexPayload{Truncation: truncationNote{BudgetChars: indexBudgetChars}}
	if ix == nil {
		return out
	}
	for _, r := range ix.Rollups() {
		out.Rollups = append(out.Rollups, rollupPayload{
			TargetID: r.TargetID, Anomalous: r.Anomalous, Weak: r.Weak,
			NoDataViews: r.NoDataViews,
		})
	}

	var t1, t2, t3 []evidence.EvidenceIndexRecord
	for _, r := range ix.Active() {
		if r.RecordKind != evidence.KindFinding {
			// query_scope는 조회 범위 계약, meta는 감사 재료 — 가설 재료가
			// 아니다. 판단 지점(§14-7 ③): guard 경로의 backend_error는
			// KindMeta 합성 레코드(안 A)라 이 필터에 걸려 payload에 안
			// 실린다 — §5.3-2 "backend_error 전량 유지"는 finding·scope
			// 형태의 backend_error에만 닿는다. 결손 자체는 BackendGaps
			// (§15.4-2)·재생성 입력 ⑤가 kind 무관하게 읽으므로 t2 편입은
			// 유보(스펙 문면 재조정 대기).
			continue
		}
		if keep != nil && !keep(r) {
			continue // 시야 밖 — 절단 통계에도 세지 않는다(이 payload의 세계가 아니다)
		}
		switch {
		case r.Quality.Status == evidence.StatusAnomalous:
			t1 = append(t1, r)
		case r.Quality.Availability == evidence.AvailObservedZero,
			r.Quality.Status == evidence.StatusNoData && isRetainedNoData(r.Quality.NoDataReason):
			t2 = append(t2, r)
		case r.Quality.Status == evidence.StatusNormal && r.Quality.Confidence == evidence.ConfLow:
			t3 = append(t3, r)
		case r.Quality.Status == evidence.StatusNormal:
			out.Truncation.FoldedNormalOK++
		default:
			out.Truncation.FoldedOther++
		}
	}
	sortByEffectRank(t1, symptomMembers)

	// ① 1+2+3 전량으로 시작한다.
	keepLow, abbrevT2, keepT1 := true, false, len(t1)
	for {
		out.Records, out.Abbreviated = assemble(t1, t2, t3, symptomMembers, keepLow, abbrevT2, keepT1)
		out.Truncation.FoldedNormalLow = 0
		out.Truncation.AbbreviatedNoData = 0
		out.Truncation.AbbreviatedAnomalous = 0
		if !keepLow {
			out.Truncation.FoldedNormalLow = len(t3)
		}
		if abbrevT2 {
			out.Truncation.AbbreviatedNoData = len(t2)
		}
		out.Truncation.AbbreviatedAnomalous = len(t1) - keepT1
		out.Truncation.UsedChars = payloadSize(out)
		if out.Truncation.UsedChars <= indexBudgetChars {
			return out
		}
		switch {
		case keepLow && len(t3) > 0: // ⑤ 4→3 순서
			keepLow = false
		case keepLow:
			keepLow = false // 3이 비어도 상태는 진행시킨다(무한 루프 차단)
		case !abbrevT2 && len(t2) > 0: // 판단 지점 ②
			abbrevT2 = true
		case keepT1 > 0: // ⑥ anomalous 축약 — 2차 정렬의 꼬리부터
			keepT1--
		default:
			return out // 더 줄일 것이 없다 — 예산 초과 사실은 UsedChars가 말한다
		}
	}
}

// noteTrunc는 payload 한 건의 예산 축약을 run 누적기에 남긴다(§14-7 계측
// — 한계 블록 IndexReduction의 생산자). 누적기 미배선(nil)이면 무동작 —
// 리포트가 not_measured로 정직하게 남는 fail-closed다.
func noteTrunc(in pipeline.GenerateInput, p indexPayload) indexPayload {
	if in.IndexTrunc != nil {
		t := p.Truncation
		in.IndexTrunc.Note(t.AbbreviatedNoData+t.AbbreviatedAnomalous, t.FoldedNormalLow, t.UsedChars)
	}
	return p
}

// isRetainedNoData는 §5.3-2가 전량 유지로 지목한 no_data 사유다.
func isRetainedNoData(r evidence.NoDataReason) bool {
	switch r {
	case evidence.NoDataCollectorGap, evidence.NoDataTTLExpired, evidence.NoDataBackendError:
		return true
	}
	return false
}

// assemble은 현재 절단 상태로 레코드·축약 목록을 만든다.
func assemble(t1, t2, t3 []evidence.EvidenceIndexRecord, members map[string]bool,
	keepLow, abbrevT2 bool, keepT1 int) ([]recordPayload, []abbrevPayload) {

	var recs []recordPayload
	var abbr []abbrevPayload
	for i, r := range t1 {
		if i < keepT1 {
			recs = append(recs, toRecordPayload(r, members))
		} else {
			abbr = append(abbr, toAbbrev(r))
		}
	}
	for _, r := range t2 {
		if abbrevT2 {
			abbr = append(abbr, toAbbrev(r))
		} else {
			recs = append(recs, toRecordPayload(r, members))
		}
	}
	if keepLow {
		for _, r := range t3 {
			recs = append(recs, toRecordPayload(r, members))
		}
	}
	return recs, abbr
}

func payloadSize(p indexPayload) int {
	b, err := json.Marshal(p)
	if err != nil {
		return 0
	}
	return len([]rune(string(b)))
}

// sortByEffectRank는 §5.3-6의 2차 정렬이다 — ① 증상 멤버 대상 우선
// ② Effect 서열(KindRank + |Magnitude|, 이종 Kind 수치 직접 비교 금지·C-10).
// 마지막 갈래는 EID 오름차순이다(정렬 결정론 — index 적재 순서와 같다).
func sortByEffectRank(recs []evidence.EvidenceIndexRecord, members map[string]bool) {
	sort.SliceStable(recs, func(i, j int) bool {
		a, b := recs[i], recs[j]
		if am, bm := members[a.TargetID], members[b.TargetID]; am != bm {
			return am
		}
		ar, _ := evidence.KindRank(a.Effect.Kind)
		br, _ := evidence.KindRank(b.Effect.Kind)
		if ar != br {
			return ar > br
		}
		am, bm := a.Effect.Magnitude, b.Effect.Magnitude
		if (am != nil) != (bm != nil) {
			return am != nil // Magnitude 없는 레코드는 후순위(B-11)
		}
		if am != nil && math.Abs(*am) != math.Abs(*bm) {
			return math.Abs(*am) > math.Abs(*bm)
		}
		return a.EID < b.EID
	})
}

func toAbbrev(r evidence.EvidenceIndexRecord) abbrevPayload {
	return abbrevPayload{
		EID: r.EID, TargetID: r.TargetID,
		Source: string(r.Provenance.Source), Aspect: string(r.Aspect),
		Metric: r.Effect.Metric, EntityKey: r.EntityKey,
		Window:   string(r.Window.Class),
		Headline: r.Headline,
	}
}

func toRecordPayload(r evidence.EvidenceIndexRecord, members map[string]bool) recordPayload {
	p := recordPayload{
		EID: r.EID, TargetID: r.TargetID, Domain: r.Domain,
		Aspect: string(r.Aspect), Source: string(r.Provenance.Source),
		Class: r.FindingClass, EntityKey: r.EntityKey, Headline: r.Headline,
		Quiet:        !members[r.TargetID],
		Status:       string(r.Quality.Status),
		NoDataReason: string(r.Quality.NoDataReason),
		Availability: string(r.Quality.Availability),
		Confidence:   string(r.Quality.Confidence),
		Window:       toWindowPayload(r.Window),
		Truncated:    r.Truncation.Truncated,
		OmittedN:     r.Truncation.OmittedN,
	}
	if r.Effect.Kind != "" {
		p.Effect = &effectP{
			Kind: string(r.Effect.Kind), Metric: r.Effect.Metric,
			Observed: r.Effect.Observed, Baseline: r.Effect.Baseline,
			Magnitude: r.Effect.Magnitude, Direction: string(r.Effect.Direction),
		}
	}
	return p
}

func toWindowPayload(w evidence.TimeWindow) windowP {
	out := windowP{Class: string(w.Class)}
	if !w.From.IsZero() {
		out.From = w.From.UTC().Format(time.RFC3339)
	}
	if !w.To.IsZero() {
		out.To = w.To.UTC().Format(time.RFC3339)
	}
	if w.ChangeFrom != nil {
		out.ChangeFrom = w.ChangeFrom.UTC().Format(time.RFC3339)
	}
	if w.ChangeTo != nil {
		out.ChangeTo = w.ChangeTo.UTC().Format(time.RFC3339)
	}
	return out
}
