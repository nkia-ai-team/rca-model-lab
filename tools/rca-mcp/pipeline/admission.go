// 가설 등록 규칙의 기계층 — docs/spec-agent-structure.md §6.2(등록 규칙)가
// 정본이다(§14-3 3a).
//
// **이 파일은 순수 함수 층이다**: 입력은 후보 + 위상 어휘 + 사상표 + 명제
// 장부이고, 출력은 통과 후보 + 기계 딱지 + 반려 사유 목록이다. LLM을 부르지
// 않고 장부에 쓰지도 않는다 — 기록은 Generate 본체가 한다. 이렇게 나눈 이유는
// 3b(심사점 R·재요구 루프)가 같은 판정을 **재검**해야 하기 때문이다(§6.2-3:
// "규칙 5의 audited_out 이후 이 규칙을 기계가 재검한다").
//
// 여기 없는 것(침범 금지):
//   - 규칙 5 R 심사(LLM 술어 의미 검수) — 3b
//   - 규칙 6 전멸·과소 경로(재생성 1회·hypothesis_admission_failure) — 3b
//   - Grouper 병합 규칙 확장(PredictedSignals dedup 합집합 등) — 3b
package pipeline

import (
	"fmt"
	"sort"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// ── 위상 어휘 ───────────────────────────────────────────────────

// TopologyVocab는 등록 규칙 1ⓐ의 위상 어휘다 — **append-only**([1] ∪ A0 ∪
// W3 확장분, §6.2-1ⓐ 3차 검토 C-1).
//
// **왜 슬라이스가 아니라 타입인가**: 종전 "[1]+A0 고정"은 W3가 실호출로 만든
// 신규 대상을 지목한 술어를 전부 반려시켰다. 확장 통로(Extend)와 버전이 한
// 자리에 있어야 §15.1-3 대상 allowlist가 같은 뷰를 읽는다. 확장분은 LLM 산출이
// 아니라 하네스 계산값이라 주입 방어가 유지된다.
type TopologyVocab struct {
	ids     map[string]bool
	order   []string
	version int
}

// NewTopologyVocab은 동결 어휘로 시작한다 — 원천은 [3] ScreenResult.Targets
// (동결된 조사 범위 = seed ∪ A0 신규, pipeline/screen.go).
func NewTopologyVocab(ids ...string) *TopologyVocab {
	v := &TopologyVocab{ids: map[string]bool{}}
	v.add(ids)
	v.version = 1 // 동결 어휘가 버전 1이다 — 확장분부터 버전이 오른다.
	return v
}

// Extend는 W3 확장 통로다 — 신규 대상이 실제로 늘었을 때만 버전을 올린다.
// **삭제는 없다**(append-only): 지운 어휘를 지목한 과거 술어가 소급 반려되면
// 판정이 시간에 따라 뒤집힌다.
func (v *TopologyVocab) Extend(ids ...string) {
	if v == nil {
		return
	}
	if v.add(ids) {
		v.version++
	}
}

// add는 편입 본체다 — 실제로 늘었는지를 돌려준다(버전 상승 판정).
func (v *TopologyVocab) add(ids []string) bool {
	added := false
	for _, id := range ids {
		if id == "" || v.ids[id] {
			continue
		}
		v.ids[id] = true
		v.order = append(v.order, id)
		added = true
	}
	return added
}

// Has는 어휘 소속 판정이다. **nil 어휘는 공집합이다**(fail-closed) — 어휘를
// 배선하지 않은 호출자가 전 술어를 무검사 통과시키는 문을 막는다.
func (v *TopologyVocab) Has(id string) bool {
	if v == nil {
		return false
	}
	return v.ids[id]
}

// IDs는 어휘 전량이다(편입 순). 소비자: 후보 프롬프트의 어휘 열거(llm).
func (v *TopologyVocab) IDs() []string {
	if v == nil {
		return nil
	}
	return append([]string(nil), v.order...)
}

// Version은 어휘 버전이다 — 소비자: §15.1-3 대상 allowlist(§14-6).
func (v *TopologyVocab) Version() int {
	if v == nil {
		return 0
	}
	return v.version
}

// ── 딱지·반려 사유 ──────────────────────────────────────────────

// AdmissionTag는 등록 시 기계가 계산하는 딱지다 — **LLM 선언이 아니다**(§6).
type AdmissionTag string

const (
	// TagPreSatisfied — 규칙 2. 선언 시점에 이미 충족(회고). supported로 셀
	// 수는 있으나 §8 신규 관측 요건에 불산입한다(소비자: §14-5 판정 게이트).
	TagPreSatisfied AdmissionTag = "pre_satisfied"
	// TagUndecidable — 규칙 1 위반. 그 가설은 반려된다.
	TagUndecidable AdmissionTag = "undecidable"
	// TagNonDiscriminating — 규칙 4. **반려가 아니다**(원인 후보가 하나뿐인
	// 인시던트 존중). 소비자: probe 정렬 최후순위(§14-4).
	TagNonDiscriminating AdmissionTag = "non_discriminating"
	// TagDiversityUnmet — 다양성 소프트 미충족(CauseEntity ≥2종, sentinel
	// 제외). 재요구 1회는 Generate의 라운드 루프가 돌리고(§14-4 4c), 그래도
	// 미충족이면 이 딱지를 단 채 진행한다.
	TagDiversityUnmet AdmissionTag = "diversity_unmet"
	// TagCountBelowMin — 개설 수 3 미만(§6.2 "개수 3~5"). 전멸·과소 경로의
	// 처분(재생성·probable 상한)은 규칙 6 = 3b 몫이라 여기서는 기록만 한다.
	TagCountBelowMin AdmissionTag = "count_below_min"

	// ── 3b(규칙 5·6)가 추가한 딱지 ─────────────────────────────

	// TagAuditedOut — 심사점 R 탈락(무효). 셈·정렬에서 제외된다(§6.2-5).
	// 소비자: §14-5 판정 게이트(유효 술어만 센다) · §7.2 식별력 정렬.
	TagAuditedOut AdmissionTag = "audited_out"
	// TagRoleDemoted — RoleValid=false로 necessary→corroborating 강등
	// (§6.2-5). 반려가 아니다 — 강등 후 규칙 3·5를 기계가 재검한다.
	TagRoleDemoted AdmissionTag = "role_demoted"
	// TagTopKDropped — 상위 K 탈락. **개설분과 구분하는 표식이다**(3a 인계
	// ② — 종전 rep.Admitted는 탈락자와 개설분이 무표식으로 섞여 보고서
	// 소비자가 오독할 여지가 있었다). 기계 관문은 통과했으므로 Rejected가
	// 아니라 Admitted에 남되 이 딱지를 단다.
	TagTopKDropped AdmissionTag = "topk_dropped"
	// TagAuditRejected — 기계 관문은 통과했으나 R 심사·재검에서 반려됐다.
	// Admitted 목록의 그 후보에 달리고, 반려 사유는 Rejected에 따로 실린다.
	TagAuditRejected AdmissionTag = "audit_rejected"
	// TagCompetitorScarcity — 개설 유효 가설이 1~2개(§6.2-6 경쟁 공집합
	// 보정). **여기는 기록까지다** — confirmed 차단·probable 상한의 판정
	// 본체는 §14-5 몫이다.
	TagCompetitorScarcity AdmissionTag = "competitor_scarcity"

	// TagDiversityRerequested — 다양성 미충족으로 **재요구 1회를 돌렸다**
	// (§14-4 4c, §14-3 이월분). 개설된 집합이 재요구 이후의 것임을 감사에
	// 남긴다 — 이 딱지 없이 diversity_unmet만 보면 재요구가 있었는지
	// 없었는지 산출물에서 구분되지 않는다.
	TagDiversityRerequested AdmissionTag = "diversity_rerequested"

	// TagDiversityRerequestUnaffordable — 다양성이 미충족인데 **잔여 예산이
	// 없어** 재요구를 생략했다(§7.6 지출 전 잔여 검사). diversity_unmet만으로는
	// "재요구했는데도 안 됐다"와 "예산이 없어 시도조차 못 했다"가 구분되지
	// 않는다 — 라이브 회귀(2026-08-04, 재요구가 루프를 굶겨 바퀴 0)의 기록처다.
	TagDiversityRerequestUnaffordable AdmissionTag = "diversity_rerequest_unaffordable"
)

// PredTag는 술어 하나에 붙은 기계 딱지 기록이다(감사 — 3b의 R 심사 입력).
type PredTag struct {
	CandidateIndex int
	PredID         string
	Tag            AdmissionTag
	Detail         string
}

// AdmissionReject는 반려 사유 하나다. **감사 가능한 구조체로 남긴다** —
// 3b의 재요구 루프가 이 목록을 그대로 LLM 피드백으로 쓴다(§6.2-5·6).
type AdmissionReject struct {
	// Rule — §6.2의 규칙 지목("규칙 1ⓐ" 등). 재요구 피드백의 머리말이다.
	Rule string
	// PredID — 술어 단위 위반이면 그 술어. 가설 단위 위반이면 빈 값.
	PredID string
	Reason string
}

func (r AdmissionReject) String() string {
	if r.PredID != "" {
		return fmt.Sprintf("%s [%s] %s", r.Rule, r.PredID, r.Reason)
	}
	return fmt.Sprintf("%s %s", r.Rule, r.Reason)
}

// UnboundEID는 개설 근거로 세지 않은 SupportEID다(§6 C-4). 반려가 아니라
// **불산입**이며, 유효 0이 되면 그때 개설 반려다.
type UnboundEID struct {
	CandidateIndex int
	EID            string
	Reason         string
}

// AdmittedCandidate는 기계 관문을 통과한 후보다.
type AdmittedCandidate struct {
	Candidate
	// Index — Admit에 넘긴 슬라이스에서의 원본 위치. 호출자가 병합 메타
	// (Sources·order)를 되찾는 통로다.
	Index int
	// Tags — 가설 수준 기계 딱지(non_discriminating·topk_dropped·
	// audit_rejected). 반려가 아니다.
	Tags []AdmissionTag
	// hypoID — 개설 예정 가설 ID. **비공개다**: 부여 주체는 Generate 하나이고
	// (§6.2-5의 R 심사가 개설 **전에** 돌아야 해서 개설 시점보다 앞당겨졌다),
	// 밖에서 쓰면 두 곳이 ID를 부여하는 길이 열린다.
	hypoID string
}

// HypoID는 개설 예정 가설 ID다(감사·시험용 읽기 창).
func (c AdmittedCandidate) HypoID() string { return c.hypoID }

// RejectedCandidate는 반려된 후보와 그 사유 전부다.
type RejectedCandidate struct {
	Candidate Candidate
	Index     int
	Rejects   []AdmissionReject
}

// AdmissionReport는 등록 규칙 한 패스의 산출이다. **1패스 기계 판정만**이며
// LLM 재요구 루프는 3b가 이 보고서를 소비해 돈다.
type AdmissionReport struct {
	Admitted []AdmittedCandidate
	Rejected []RejectedCandidate
	PredTags []PredTag
	Unbound  []UnboundEID
	// RunTags — 집단 규칙의 run 수준 기록(diversity_unmet·count_below_min).
	RunTags []AdmissionTag
}

// RejectReasons는 반려 사유를 한 줄로 잇는다(장부 ViolatedRule 기록용).
func (r RejectedCandidate) RejectReasons() string {
	s := ""
	for i, rj := range r.Rejects {
		if i > 0 {
			s += " / "
		}
		s += rj.String()
	}
	return s
}

// ── 규칙 1~3 + SupportEIDs 결박 (후보 단위) ─────────────────────

// Admit은 후보 단위 기계 관문이다 — 규칙 1(형식·어휘)·2(pre_satisfied)·
// 3(미판정 ≥1 + 미실측 necessary ≥1) + SupportEIDs 결박 검사(§6).
//
// 집단 규칙(규칙 4 식별력·다양성·개수)은 개설 확정 집합에 걸리므로
// AdmitCohort가 따로 진다.
// **명제 장부 nil은 전원 반려다**(fail-closed, 3a 인계 ②): Generate 본체는
// 진입에서 nil을 막지만 3b의 재검 경로가 Admit을 **직접** 부르므로 관문이
// 여기에도 있어야 한다. 없으면 규칙 2·3의 기실측 조회가 조용히 "전부
// 미실측"으로 흘러 회고 가설이 재검을 통과한다.
func Admit(cands []Candidate, in GenerateInput) AdmissionReport {
	var rep AdmissionReport
	if in.Propositions == nil {
		for i, c := range cands {
			rep.Rejected = append(rep.Rejected, RejectedCandidate{Candidate: c, Index: i,
				Rejects: []AdmissionReject{{Rule: "규칙 2·3",
					Reason: "명제 장부(§6.0) 미배선 — 기실측 조회 불능(fail-closed)"}}})
		}
		return rep
	}
	for i, c := range cands {
		// PredID는 **기계가 부여한다**(§6 B-5 — LLM이 쓰면 같은 ID를 두 번 써
		// 미심사 술어를 유효 술어 셈에 넣는 세탁이 가능하다). 여기서는 후보
		// 임시 ID이고, 개설 시 Generate가 가설 ID로 다시 부여한다.
		c.PredictedSignals = ledger.AssignPredIDs(fmt.Sprintf("C%d", i+1), c.PredictedSignals)
		preds := c.PredictedSignals
		var rejects []AdmissionReject

		// 규칙 1 (기계, 형식·어휘) — 위반 = undecidable 딱지 → 그 가설 반려.
		for j := range preds {
			for _, v := range formViolations(preds[j], in.Vocab) {
				preds[j].Undecidable = true
				rep.PredTags = append(rep.PredTags, PredTag{
					CandidateIndex: i, PredID: preds[j].PredID,
					Tag: TagUndecidable, Detail: v.Reason,
				})
				rejects = append(rejects, v)
			}
		}
		if len(rejects) > 0 {
			// 형식이 깨진 술어로 명제 장부를 조회하면 근거 없는 기실측·
			// 미실측 판정이 나온다 — 규칙 2·3은 건너뛴다.
			rep.Rejected = append(rep.Rejected, RejectedCandidate{Candidate: c, Index: i, Rejects: rejects})
			continue
		}

		// 규칙 2 (기계, pre_satisfied) — 명제 장부에 그 관측 키가 이미
		// 실측돼 있고 관측값에서 파생한 truth가 술어와 일치하면 회고 딱지.
		// **Window 축은 무시한다**(MeasuredAnyWindow, 4차 A-3).
		measured := make([]bool, len(preds))
		for j := range preds {
			m := in.Propositions.MeasuredAnyWindow(preds[j].ObservationKey(), preds[j].Predicate, preds[j].Threshold)
			measured[j] = m.Measured
			if m.Measured && evidence.Holds(m.Truth, preds[j].Expectation) {
				preds[j].PreSatisfied = true
				rep.PredTags = append(rep.PredTags, PredTag{
					CandidateIndex: i, PredID: preds[j].PredID, Tag: TagPreSatisfied,
					Detail: fmt.Sprintf("창 %s에서 기실측 — §8 신규 관측 요건 불산입", m.Window),
				})
			}
		}

		// SupportEIDs 결박 검사(§6, 3차 검토 C-4) — 미결박은 개설 근거
		// 불산입, 유효 0이면 개설 반려.
		bound, drops := bindSupportEIDs(c, in.Evidence, i)
		rep.Unbound = append(rep.Unbound, drops...)
		c.SupportEIDs = bound
		if len(bound) == 0 {
			rejects = append(rejects, AdmissionReject{
				Rule:   "SupportEIDs",
				Reason: "결박된 active 개설 근거 EID가 0개 — §6 '없으면 개설 반려'",
			})
		}

		// 규칙 3 (기계, 미판정 ≥1 + 미실측 necessary ≥1).
		rejects = append(rejects, rule3Violations(preds, measured, in.DeltaKeys)...)

		if len(rejects) > 0 {
			rep.Rejected = append(rep.Rejected, RejectedCandidate{Candidate: c, Index: i, Rejects: rejects})
			continue
		}
		rep.Admitted = append(rep.Admitted, AdmittedCandidate{Candidate: c, Index: i})
	}
	return rep
}

// formViolations는 등록 규칙 1 ⓐ~ⓓ다.
//
// **Metric·EntityKey의 실재는 검사하지 않는다**(§6.2-1 명문) — prospective
// 술어(아직 조회 안 한 개체에 대한 예측)와 양립해야 하므로, 실재는 판정
// 시점에 해소 불능=inconclusive로 드러난다.
func formViolations(p ledger.SignalPred, vocab *TopologyVocab) []AdmissionReject {
	var out []AdmissionReject
	// ⓒ Predicate·Threshold·Expectation·Role 조합 유효(+ 어휘 형식).
	if err := p.ValidateForm(); err != nil {
		out = append(out, AdmissionReject{Rule: "규칙 1ⓒ", PredID: p.PredID, Reason: err.Error()})
	}
	// ⓐ TargetID ∈ 위상 어휘(append-only: 동결 어휘 + A0·W3 확장 통로).
	if !vocab.Has(p.TargetID) {
		out = append(out, AdmissionReject{Rule: "규칙 1ⓐ", PredID: p.PredID,
			Reason: fmt.Sprintf("target_id %q가 위상 어휘 밖", p.TargetID)})
	}
	if !evidence.ValidSource(p.Tool) {
		// ⓑⓓ는 사상표 행이 있어야 판정된다 — 원천 자체가 어휘 밖이면 ⓒ가
		// 이미 반려했고, 여기서 또 세면 같은 위반이 두 번 계수된다.
		return out
	}
	// ⓑ (Tool, Aspect) ∈ 사상표(§5.1).
	if !hasAspect(p.Tool, p.Aspect) {
		out = append(out, AdmissionReject{Rule: "규칙 1ⓑ", PredID: p.PredID,
			Reason: fmt.Sprintf("(%s, %s) 조합이 사상표에 없음", p.Tool, p.Aspect)})
	}
	// ⓓ Predicate ∈ 그 Tool의 사상표 행 허용 Predicate 합집합
	// (class 층은 판정 시점 몫 — §5.1 2층 강제 중 ①).
	if evidence.ValidPredicate(p.Predicate) && !inPredicates(evidence.AllowedUnion(p.Tool), p.Predicate) {
		out = append(out, AdmissionReject{Rule: "규칙 1ⓓ", PredID: p.PredID,
			Reason: fmt.Sprintf("predicate %s는 %s의 허용 합집합 밖", p.Predicate, p.Tool)})
	}
	return out
}

func hasAspect(src evidence.ObservationSource, a evidence.Aspect) bool {
	for _, got := range evidence.AspectOf(src) {
		if got == a {
			return true
		}
	}
	return false
}

func inPredicates(list []evidence.Predicate, p evidence.Predicate) bool {
	for _, got := range list {
		if got == p {
			return true
		}
	}
	return false
}

// rule3Violations는 등록 규칙 3이다.
//
//	① 예측표 전체가 기실측이면 "예측이 아니라 회고"라 반려.
//	② Role=necessary 중 미실측(pre_satisfied 아님) ≥1을 요구 — 없으면
//	   **죽지 않는 가설**이다(3차 검토 양 red team 수렴: 답을 알고 베낀 것은
//	   예측이 아니고, 오답 confirmed까지 실증됐다).
//
// 이 규칙이 반증 재제안 차단을 겸한다(§6.1) — Predicate·Threshold 변형은
// 관측 키에서 이미 빠져 있고(§6.0), 창 변형은 규칙 2·3의 조회가 무시한다.
//
// # delta 문맥 해석 (§7.5 — §14-4 4d)
//
// 재생성 경로에서는 ②를 "material delta로 **새로 판정 가능해졌거나** 미실측인
// necessary ≥1"로 읽는다. 근거: widening이 뒤집은 관측(§7.4 anomaly_superseded·
// no_data_resolved 등)에 걸린 necessary 술어는 pre_satisfied 딱지를 달고 있어도
// 그 delta 때문에 다시 판정 대상이 된다 — 종전 문면대로 읽으면 관측 키 정규화가
// 넓혀 놓은 기실측 집합에 재생성 가설이 통째로 반려당하는 경로가 실증됐다(C-6).
//
// **①(전부 기실측 = 회고)는 좁히지 않는다**: 예측표 전량이 이미 실측된 가설은
// delta가 있든 없든 회고이며, 스펙 §7.5가 문맥 해석을 명시한 것은 ②뿐이다.
//
// deltaKeys가 nil이면(첫 생성 경로) 판정은 종전과 완전히 같다.
func rule3Violations(preds []ledger.SignalPred, measured []bool,
	deltaKeys map[evidence.ObservationKey]bool) []AdmissionReject {

	var out []AdmissionReject
	unmeasured, liveNecessary := 0, 0
	for j, p := range preds {
		if !measured[j] {
			unmeasured++
		}
		if p.Role != ledger.RoleNecessary {
			continue
		}
		if !p.PreSatisfied || deltaKeys[p.ObservationKey().Canonical()] {
			liveNecessary++
		}
	}
	if unmeasured == 0 {
		out = append(out, AdmissionReject{Rule: "규칙 3",
			Reason: fmt.Sprintf("예측표 %d개가 전부 기실측 — 예측이 아니라 회고", len(preds))})
	}
	if liveNecessary == 0 {
		out = append(out, AdmissionReject{Rule: "규칙 3",
			Reason: "미실측 necessary 술어가 0개 — 죽지 않는 가설(반증력 없음)"})
	}
	return out
}

// bindSupportEIDs는 개설 근거 EID의 결박 검사다(§6 SupportEIDs 주석).
//
// 세 관문을 통과해야 근거로 센다: ① index에 있고 active(superseded 아님)
// ② 어느 술어의 EIDHint에 결박 ③ 그 술어의 관측 키와 레코드 selector 일치.
// 임의 active EID + 무관 술어로 개설을 통과하는 경로를 닫는다(3차 검토 C-4).
//
// **ChainClaim 결박 통로는 여기 없다**: Candidate.Chain이 아직 구 ChainStep
// (EID 링크가 없는 타입)이라 결박할 자리가 없다. 사슬이 ChainClaim으로
// 옮겨지는 §14-4에서 두 번째 통로가 붙는다.
func bindSupportEIDs(c Candidate, ix *evidence.Index, idx int) ([]string, []UnboundEID) {
	var bound []string
	var drops []UnboundEID
	drop := func(eid, why string) {
		drops = append(drops, UnboundEID{CandidateIndex: idx, EID: eid, Reason: why})
	}
	for _, eid := range c.SupportEIDs {
		if ix == nil {
			drop(eid, "evidence index 미배선 — 결박 검사 불능")
			continue
		}
		rec, ok := ix.Get(eid)
		if !ok {
			drop(eid, "index에 없는 EID")
			continue
		}
		if heir, err := ix.ActiveHeir(eid); err != nil || heir != eid {
			drop(eid, "active가 아님(superseded) — §5.7")
			continue
		}
		matched := false
		for _, p := range c.PredictedSignals {
			if p.EIDHint != eid {
				continue
			}
			if rec.Key() != p.ObservationKey() {
				drop(eid, fmt.Sprintf("술어 %s가 지목했으나 selector 불일치(%s ≠ %s)",
					p.PredID, rec.Key(), p.ObservationKey()))
				matched = true // 사유를 이미 남겼다 — "결박 없음"으로 또 세지 않는다
				break
			}
			matched = true
			bound = append(bound, eid)
			break
		}
		if !matched {
			drop(eid, "어느 술어의 eid_hint에도 결박되지 않음")
		}
	}
	return bound, drops
}

// ── 집단 규칙 (규칙 4·다양성·개수) ──────────────────────────────

// AdmitCohort는 **개설 확정 집합**에 거는 집단 규칙이다 — 규칙 4(식별력
// 행렬)·다양성 소프트·개수. 셋 다 반려하지 않는다(기록만).
//
// 후보 단위가 아니라 확정 집합에 거는 이유: 규칙 4는 "활성 가설 전체에서"의
// 계산이고, 다양성은 개설된 CauseEntity의 성질이다. 상위 K 탈락 전 집합에
// 걸면 탈락할 후보가 식별력을 만들어 준 것으로 세어진다.
func AdmitCohort(admitted []AdmittedCandidate) ([]AdmittedCandidate, []AdmissionTag) {
	out := make([]AdmittedCandidate, len(admitted))
	copy(out, admitted)

	// 규칙 4 — 같은 **명제 키**(§6.0)의 Expectation이 갈리는 쌍을 계산.
	// 어느 경쟁과도 안 갈리면 non_discriminating(반려 아님, probe 정렬
	// 최후순위).
	type keyExp struct {
		key evidence.ObservationKey
		exp evidence.Expectation
	}
	holders := map[keyExp]map[int]bool{}
	for i, c := range out {
		for _, p := range c.PredictedSignals {
			k := keyExp{p.ObservationKey(), p.Expectation}
			if holders[k] == nil {
				holders[k] = map[int]bool{}
			}
			holders[k][i] = true
		}
	}
	discriminating := map[int]bool{}
	for k, mine := range holders {
		for other, theirs := range holders {
			if other.key != k.key || other.exp == k.exp {
				continue
			}
			for i := range mine {
				for j := range theirs {
					if i != j {
						discriminating[i] = true
						discriminating[j] = true
					}
				}
			}
		}
	}
	for i := range out {
		if !discriminating[i] {
			out[i].Tags = append(out[i].Tags, TagNonDiscriminating)
		}
	}

	var runTags []AdmissionTag
	// 다양성(소프트) — CauseEntity ≥2종. **sentinel(`unknown:*`)은 계수에서
	// 제외한다**(대항 가설은 다양성 충족 수단이 아니다, §6.2). 재요구 1회는
	// Generate가 돌리고, 여기서는 판정과 기록만 한다.
	if !DiverseCauses(out) {
		runTags = append(runTags, TagDiversityUnmet)
	}
	// 개수 3~5 — 상한은 Generate의 TopK가 지고, 하한 미달은 여기 기록만
	// 한다(처분은 규칙 6 = 3b).
	if len(out) < 3 {
		runTags = append(runTags, TagCountBelowMin)
	}
	return out, runTags
}

// DiverseCauses는 다양성 소프트 규칙의 판정식이다 — 서로 다른 CauseEntity
// ≥2종(sentinel `unknown:*` 제외). 재요구 판정(generate.go)과 딱지 계산이
// 같은 식을 써야 "재요구했는데 같은 이유로 또 걸린다"가 재생 가능해진다.
func DiverseCauses(cands []AdmittedCandidate) bool {
	causes := map[string]bool{}
	for _, c := range cands {
		e := c.IdentityKey.CauseEntity
		if e == "" || ledger.IsSentinelCause(e) {
			continue
		}
		causes[e] = true
	}
	return len(causes) >= 2
}

// ── B-5: 가설 간 PredID 유일성 ──────────────────────────────────

// CrossHypoPredIDConflicts는 **개설 확정 집합 전체**에서 PredID 충돌을 찾는다
// (§14-4 4b, B-5 승계).
//
// **왜 ledger.ValidatePredIDs로는 부족한가**(4a 실측): 그 함수는 가설
// **하나 안에서만** 유일성을 강제한다. 가설을 건너뛴 충돌은 검사가 없어
// 두 곳에서 새어 나온다:
//
//	① R 배치 심사 — collateAudit은 PredID를 **전 가설 공통 키**로 판정을
//	   복원한다(audit.go). 두 가설이 같은 PredID를 쓰면 한쪽의 심사 결과가
//	   다른 쪽 술어에 박힌다 — 미심사 술어가 유효로 세탁되는 바로 그 문이다.
//	② ProbeRequest — 실행기가 PredID로 관측 키를 해소하는데 충돌이면 해소
//	   불능이라 RejectAmbiguousPred로 반려된다(probe/exec.go). 조사자가
//	   무엇을 고르든 반려당하는 후보가 목록에 남는다.
//
// 기계 부여(AssignPredIDs)를 쓰면 가설 ID 접두사가 유일성을 구조적으로
// 보장하므로 여기서 걸리는 일은 없어야 한다 — 걸린다면 ID 부여가 두 곳에서
// 일어났다는 신호이고, 그때는 **반려가 옳다**(조용히 첫 항목을 고르면 위 ①이
// 그대로 실현된다). 처분은 fail-closed: 먼저 나온 가설이 그 ID를 갖고,
// 충돌한 뒤쪽 후보가 반려된다.
func CrossHypoPredIDConflicts(cands []AdmittedCandidate) map[int][]AdmissionReject {
	owner := map[string]string{}
	out := map[int][]AdmissionReject{}
	for i, c := range cands {
		for _, p := range c.PredictedSignals {
			if p.PredID == "" {
				continue // 형식 위반은 규칙 1ⓒ의 몫이다
			}
			prev, dup := owner[p.PredID]
			if !dup {
				owner[p.PredID] = c.hypoID
				continue
			}
			out[i] = append(out[i], AdmissionReject{
				Rule: "B-5", PredID: p.PredID,
				Reason: fmt.Sprintf("pred_id가 가설 %s와 충돌 — 가설 간 유일성 위반(R 심사 결과 복원·probe 해소 불능)", prev),
			})
		}
	}
	return out
}

// SortedPredTags는 감사 출력의 결정론을 위한 정렬본이다.
func SortedPredTags(tags []PredTag) []PredTag {
	out := append([]PredTag(nil), tags...)
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].PredID != out[j].PredID {
			return out[i].PredID < out[j].PredID
		}
		return out[i].Tag < out[j].Tag
	})
	return out
}
