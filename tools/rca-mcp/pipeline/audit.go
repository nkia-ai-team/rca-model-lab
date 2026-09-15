// 심사점 R(§6.2-5)과 그 뒤의 기계 재검·전멸 경로(§6.2-6) —
// docs/spec-agent-structure.md §6.2가 정본이다(§14-3 3b).
//
// **층 나누기는 3a와 같다**: 여기 있는 것은 (ⓐ) R의 입출력 계약 typed 타입,
// (ⓑ) 배치 대조의 fail-closed 규율(누락·중복·미지 PredID), (ⓒ) 심사 결과를
// 술어에 복원한 뒤의 기계 재검이다. **LLM 호출 자체는 여기 없다** — Auditor
// 인터페이스의 구현체가 llm/audit.go에 있다(pipeline은 llm을 import할 수
// 없다: 방향은 llm→pipeline이다).
//
// 여기 없는 것(침범 금지):
//   - 경쟁 공집합의 **판정 본체**(confirmed 차단·probable 상한) — §14-5.
//     이 파일은 competitor_scarcity 딱지 **기록까지**만 한다.
//   - ProbeRequest 루프·Verifier.Review(BlindClaim) 폐기 — §14-4(4b·4c 완료).
package pipeline

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// ── R 심사의 입출력 계약 (§6.2-5) ───────────────────────────────

// PredicateView는 R에게 보이는 술어 하나다.
//
// **blind의 실물이 이 구조체다**(§6.2-5): 인시던트 서사·증상 문안·사전확률·
// 출처·순위가 여기 없다. R이 받는 것은 술어 본체와 **명제 장부(§6.0)의 기왕
// 관측값**뿐이다 — 관측값 없이는 자명성(RoleValid)을 판정할 재료가 R에게
// 없다는 3차 검토 지적의 해소다.
type PredicateView struct {
	// Pred — 술어 본체. 기계 딱지(PreSatisfied·Undecidable·AuditedOut)는
	// **어댑터가 서식에서 뺀다**(llm/audit.go predView) — 딱지가 LLM 입력에
	// 없어야 주입이 원천적으로 불가능하다(3a가 pred_id에 쓴 방식과 동일).
	Pred ledger.SignalPred
	// Observed — 기왕 관측값. **nil = 미실측**이다(C-2 데드락의 열쇠):
	// 미실측 necessary 술어는 자명성 심사의 재료가 없으므로 RoleValid
	// 심사 대상에서 빠진다(collateAudit이 기계로 nil을 강제한다).
	Observed *evidence.ObservationValue
}

// AuditSubject는 심사 단위 하나(가설 하나)다 — §6.2-5의 입력
// {Mechanism, Chain, []PredicateView}.
type AuditSubject struct {
	// Hypothesis — 대응 키. 배치 결과를 술어에 복원하는 축이다.
	Hypothesis string
	Mechanism  string
	// Chain — 인과사슬. 구 ChainStep이다(ChainClaim 이관은 §14-4).
	Chain      []ledger.ChainStep
	Predicates []PredicateView
}

// AuditItem은 R의 항목별 출력이다(§6.2-5).
//
// **RoleValid는 *bool이다** — nil = "자명성 심사 대상 아님"(ledger.
// PredicateVerdict와 같은 계약, C-2).
type AuditItem struct {
	PredID      string
	Note        string
	Pertinent   bool
	Substantive bool
	RoleValid   *bool
}

// Auditor는 심사점 R이다(Verifier의 새 자리 ①). **배치 1콜**이 계약이다 —
// 전 가설의 전 술어를 한 번에 넘기고, 구현체는 "항목별 독립 판정·가설 간
// 비교 금지"를 지시문으로 건다(배치 오염 가드, §6.2-5).
type Auditor interface {
	AuditPredicates(ctx context.Context, subjects []AuditSubject) ([]AuditItem, error)
}

// PredicateReviser는 규칙 5의 **재요구 1회** 경로다 — "위반 피드백 재요구
// 1회(그 가설의 술어 재생성 요청)".
//
// **선택 배선이다**: nil이면 재요구 없이 그 가설이 바로 반려된다. 없는 것이
// 보수 방향이므로(재요구는 반려를 뒤집는 유일한 통로다) fail-closed가 깨지지
// 않는다.
type PredicateReviser interface {
	RevisePredicates(ctx context.Context, subject AuditSubject, feedback []AdmissionReject) ([]ledger.SignalPred, error)
}

// ── 배치 대조 (fail-closed, §6.2-5) ─────────────────────────────

// auditResult는 술어 하나에 복원된 심사 결과다.
type auditResult struct {
	verdict ledger.PredicateVerdict
	note    string
	// missing — R이 반환하지 않은 항목. audited_out이다(fail-closed).
	missing bool
}

// valid는 유효 술어 판정이다 — Pertinent∧Substantive(§6.2-5). 누락은 무효다.
func (r auditResult) valid() bool { return !r.missing && r.verdict.Valid() }

// errUnknownPredID는 "미지 PredID = 배치 파싱 실패"다(§6.2-5). 재시도 1회의
// 방아쇠이며, 재실패 시 그 배치의 전 가설이 반려된다.
type errUnknownPredID struct{ PredID string }

func (e *errUnknownPredID) Error() string {
	return fmt.Sprintf("R 배치에 미지 pred_id %q — 파싱 실패", e.PredID)
}

// missingNote는 누락 항목의 기계 사유다. note는 이벤트 필수 필드이고
// (ledger.go PredicateAudited 검증), 누락에 note가 없으면 "왜 무효인가"가
// 감사에서 사라진다.
const missingNote = "R이 반환하지 않은 항목 — fail-closed audited_out(§6.2-5)"

// collateAudit은 배치 응답을 술어에 복원한다. 규율 셋이 전부 여기 있다:
//
//	① 반환 누락 항목 = audited_out
//	② 중복은 **첫 항목만** (뒤 항목은 조용히 버린다 — 뒤집기 금지)
//	③ 미지 PredID = 배치 파싱 실패(errUnknownPredID)
//
// 그리고 **기계 강제 하나**: 첨부 관측값이 없는 술어(Observed=nil)의
// RoleValid는 R이 무엇을 답했든 nil로 덮는다(C-2). 미실측 술어의 자명성은
// R이 판정할 재료 자체가 없는데 "불확실은 불리 방향" 규율을 그대로 먹이면
// necessary가 전원 강등돼 등록 규칙 3이 전 가설을 죽인다.
func collateAudit(subjects []AuditSubject, items []AuditItem) (map[string]auditResult, error) {
	known := map[string]bool{}
	measured := map[string]bool{}
	for _, s := range subjects {
		for _, v := range s.Predicates {
			known[v.Pred.PredID] = true
			measured[v.Pred.PredID] = v.Observed != nil
		}
	}
	out := map[string]auditResult{}
	for _, it := range items {
		if !known[it.PredID] {
			return nil, &errUnknownPredID{PredID: it.PredID}
		}
		if _, dup := out[it.PredID]; dup {
			continue // ② 첫 항목만
		}
		rv := it.RoleValid
		if !measured[it.PredID] {
			rv = nil // 기계 강제(C-2)
		}
		out[it.PredID] = auditResult{
			verdict: ledger.PredicateVerdict{
				Pertinent: it.Pertinent, Substantive: it.Substantive, RoleValid: rv,
			},
			note: it.Note,
		}
	}
	for id := range known { // ① 누락 = audited_out
		if _, ok := out[id]; !ok {
			out[id] = auditResult{missing: true, note: missingNote}
		}
	}
	return out, nil
}

// ── 규칙 5의 기계 재검 ──────────────────────────────────────────

// applyVerdicts는 심사 결과를 술어에 박는다 — audited_out 딱지와
// RoleValid=false의 **corroborating 강등**이다(§6.2-5).
//
// 강등이 반려가 아닌 이유: 자명한 necessary는 "죽지 않는 가설"을 만들 뿐
// 술어 자체가 무의미한 것은 아니다. 강등 후 규칙 3·5를 기계가 재검해
// 반증력이 남았는지를 본다.
func applyVerdicts(preds []ledger.SignalPred, res map[string]auditResult) ([]ledger.SignalPred, []PredTag) {
	out := make([]ledger.SignalPred, len(preds))
	copy(out, preds)
	var tags []PredTag
	for i := range out {
		r, ok := res[out[i].PredID]
		if !ok || !r.valid() {
			out[i].AuditedOut = true
			detail := "R 심사 무효(pertinent·substantive 미충족)"
			if !ok || r.missing {
				detail = missingNote
			}
			tags = append(tags, PredTag{PredID: out[i].PredID, Tag: TagAuditedOut, Detail: detail})
			continue
		}
		if out[i].Role == ledger.RoleNecessary && r.verdict.RoleValid != nil && !*r.verdict.RoleValid {
			out[i].Role = ledger.RoleCorroborating
			tags = append(tags, PredTag{PredID: out[i].PredID, Tag: TagRoleDemoted,
				Detail: "RoleValid=false — 자명 술어라 corroborating 강등(§6.2-5)"})
		}
	}
	return out, tags
}

// auditRecheck는 강등·무효 반영 후의 기계 재검이다(§6.2-3 "규칙 5의
// audited_out 이후 이 규칙을 기계가 재검한다" + §6.2-5의 유효 술어 요건).
//
//	규칙 5: 유효(audited_out 아님) ≥2 · 그중 Role=necessary ≥1
//	규칙 3: 유효 술어만으로 미판정 ≥1 · 미실측 necessary ≥1 (rule3Violations 재사용)
//
// 재검 탈락 = 반려다. C-2(강등이 necessary를 소진해 전 가설이 죽는 경우)는
// 규칙 6이 받는다 — 재생성 1회 → 그래도 0이면 run 실패.
//
// deltaKeys는 §7.5 재생성 경로의 delta 문맥이다 — Admit의 규칙 3과 **같은
// 값**을 받아야 한다(§14-4 4d). 여기만 빼면 delta로 되살아난 necessary가
// 개설 직전 재검에서 다시 죽어 반려 전멸 경로가 그대로 남는다.
func auditRecheck(preds []ledger.SignalPred, props *evidence.Ledger,
	deltaKeys map[evidence.ObservationKey]bool) []AdmissionReject {
	var live []ledger.SignalPred
	necessary := 0
	for _, p := range preds {
		if p.AuditedOut {
			continue
		}
		live = append(live, p)
		if p.Role == ledger.RoleNecessary {
			necessary++
		}
	}
	var out []AdmissionReject
	if len(live) < 2 {
		out = append(out, AdmissionReject{Rule: "규칙 5",
			Reason: fmt.Sprintf("R 심사 통과 유효 술어 %d개 — 2개 미만", len(live))})
	}
	if necessary == 0 {
		out = append(out, AdmissionReject{Rule: "규칙 5",
			Reason: "유효 술어 중 반증형(necessary) 0개 — 강등·무효 후 반증력 소멸"})
	}
	if len(live) == 0 {
		return out // 규칙 3 재검은 술어가 있어야 의미가 있다
	}
	out = append(out, rule3Violations(live, measuredMask(live, props), deltaKeys)...)
	return out
}

// measuredMask는 술어별 기실측 여부다(§6.0 창 축 무시 조회) — Admit의
// 규칙 2·3이 쓰는 것과 **같은 조회**여야 재검이 같은 판정을 낸다.
func measuredMask(preds []ledger.SignalPred, props *evidence.Ledger) []bool {
	out := make([]bool, len(preds))
	if props == nil {
		return out
	}
	for i, p := range preds {
		out[i] = props.MeasuredAnyWindow(p.ObservationKey(), p.Predicate, p.Threshold).Measured
	}
	return out
}

// ── 규칙 5의 경로 (배치 1콜 → 재검 → 재요구 1회) ────────────────

// auditOutcome은 R 단계의 부산물이다(보고서·감사용).
type auditOutcome struct {
	predTags []PredTag
	rejected []RejectedCandidate
}

// runAudit은 규칙 5의 전 경로다(§6.2-5). cut의 원소를 **제자리에서** 고치고
// (술어 딱지·강등·TagAuditRejected) 통과분만 돌려준다.
//
// 순서:
//
//	① 배치 1콜 — 전 가설의 전 술어를 한 번에(항목별 독립 판정 지시는 어댑터).
//	   미지 PredID면 재시도 1회 → 재실패면 **그 배치의 전 가설 반려**.
//	② 판정 복원 — 누락=audited_out · 중복=첫 항목 · RoleValid=false는 강등.
//	   전 항목을 predicate_audited 이벤트로 장부에 기록한다.
//	③ 기계 재검(규칙 5의 유효 ≥2·necessary ≥1 + 규칙 3) — 탈락이면
//	   재요구 1회(Reviser) → 재심사 → 그래도 탈락이면 그 가설만 반려.
func runAudit(ctx context.Context, cut []AdmittedCandidate, in GenerateInput, cfg GenerateConfig, l *ledger.Ledger, now func() time.Time) ([]AdmittedCandidate, auditOutcome, error) {
	var out auditOutcome
	if len(cut) == 0 {
		return nil, out, nil
	}
	subjects := make([]AuditSubject, len(cut))
	for i := range cut {
		subjects[i] = subjectOf(cut[i], in.Propositions)
	}
	res, err := auditWithRetry(ctx, cfg.Auditor, subjects)
	if err != nil {
		var unknown *errUnknownPredID
		if !errors.As(err, &unknown) {
			return nil, out, fmt.Errorf("generate: 심사점 R: %w", err)
		}
		// 배치 파싱 실패 — 그 배치의 전 가설 반려(§6.2-5 fail-closed).
		// 전멸이면 규칙 6이 받는다(재생성 1회 → admission_failure).
		for i := range cut {
			cut[i].Tags = append(cut[i].Tags, TagAuditRejected)
			out.rejected = append(out.rejected, RejectedCandidate{
				Candidate: cut[i].Candidate, Index: cut[i].Index,
				Rejects: []AdmissionReject{{Rule: "규칙 5",
					Reason: "R 배치 파싱 실패(미지 pred_id) — 재시도 1회 후에도 실패, 배치 전 가설 반려"}},
			})
		}
		return nil, out, nil
	}

	var kept []AdmittedCandidate
	for i := range cut {
		rejects, tags, err := auditOne(ctx, &cut[i], subjects[i], res, in, cfg, l, now)
		if err != nil {
			return nil, out, err
		}
		out.predTags = append(out.predTags, tags...)
		if len(rejects) > 0 {
			cut[i].Tags = append(cut[i].Tags, TagAuditRejected)
			out.rejected = append(out.rejected, RejectedCandidate{
				Candidate: cut[i].Candidate, Index: cut[i].Index, Rejects: rejects,
			})
			continue
		}
		kept = append(kept, cut[i])
	}
	return kept, out, nil
}

// auditOne은 후보 하나의 판정 복원 → 재검 → 재요구 1회다.
func auditOne(ctx context.Context, c *AdmittedCandidate, subj AuditSubject, res map[string]auditResult,
	in GenerateInput, cfg GenerateConfig, l *ledger.Ledger, now func() time.Time) ([]AdmissionReject, []PredTag, error) {

	preds, tags, err := applyAndRecord(c.hypoID, c.Index, c.PredictedSignals, res, l, now)
	if err != nil {
		return nil, nil, err
	}
	c.PredictedSignals = preds
	rejects := auditRecheck(preds, in.Propositions, in.DeltaKeys)
	if len(rejects) == 0 {
		return nil, tags, nil
	}
	// 재요구 1회 — 그 가설의 술어 재생성(§6.2-5). 배선이 없으면 여기서 끝.
	if cfg.Reviser == nil {
		return rejects, tags, nil
	}
	revised, err := cfg.Reviser.RevisePredicates(ctx, subj, rejects)
	if err != nil {
		return nil, nil, fmt.Errorf("generate: 규칙 5 재요구: %w", err)
	}
	if len(revised) == 0 {
		return rejects, tags, nil
	}
	// 재생성분은 **기계 관문부터 다시 통과해야 한다**(규칙 1·2·3 + 결박) —
	// 재요구가 어휘 밖 술어를 들여오는 문을 열어서는 안 된다. Admit을 그대로
	// 부르므로 명제 장부 nil 관문도 같이 걸린다.
	retry := c.Candidate
	retry.PredictedSignals = revised
	rep2 := Admit([]Candidate{retry}, in)
	if len(rep2.Admitted) == 0 {
		out := append([]AdmissionReject(nil), rejects...)
		for _, rj := range rep2.Rejected {
			out = append(out, rj.Rejects...)
		}
		return out, tags, nil
	}
	retry = rep2.Admitted[0].Candidate
	// PredID는 다시 가설 ID 기준으로(Admit이 "C1-P<n>"을 박았다).
	retry.PredictedSignals = ledger.AssignPredIDs(c.hypoID, retry.PredictedSignals)
	subj2 := subjectOf(AdmittedCandidate{Candidate: retry, Index: c.Index}, in.Propositions)
	res2, err := auditWithRetry(ctx, cfg.Auditor, []AuditSubject{subj2})
	if err != nil {
		var unknown *errUnknownPredID
		if !errors.As(err, &unknown) {
			return nil, nil, fmt.Errorf("generate: 심사점 R(재요구): %w", err)
		}
		return append(rejects, AdmissionReject{Rule: "규칙 5",
			Reason: "재요구 배치도 파싱 실패(미지 pred_id) — 그 가설 반려"}), tags, nil
	}
	preds2, tags2, err := applyAndRecord(c.hypoID, c.Index, retry.PredictedSignals, res2, l, now)
	if err != nil {
		return nil, nil, err
	}
	tags = append(tags, tags2...)
	if again := auditRecheck(preds2, in.Propositions, in.DeltaKeys); len(again) > 0 {
		return append(rejects, again...), tags, nil
	}
	// 재요구가 통과했다 — 그 가설의 술어는 재생성분으로 교체된다.
	c.Candidate = retry
	c.PredictedSignals = preds2
	return nil, tags, nil
}

// applyAndRecord는 판정을 술어에 박고 **전 항목을 장부에 기록한다** —
// predicate_audited{PredID, verdict, note}(§6.2-5 "판정은 ledger에 기록").
// 누락 항목도 기록된다: 무효의 사유가 감사에서 사라지면 안 된다.
func applyAndRecord(hypoID string, candIdx int, preds []ledger.SignalPred, res map[string]auditResult,
	l *ledger.Ledger, now func() time.Time) ([]ledger.SignalPred, []PredTag, error) {

	out, tags := applyVerdicts(preds, res)
	for i := range tags {
		tags[i].CandidateIndex = candIdx
	}
	for _, p := range preds {
		r := res[p.PredID]
		note := r.note
		if note == "" {
			note = missingNote
		}
		if _, err := l.Append(ledger.ActorVerifier, now(), ledger.PredicateAudited{
			Hypothesis: hypoID, PredID: p.PredID, Verdict: r.verdict, Note: note,
		}); err != nil {
			return nil, nil, err
		}
	}
	return out, tags, nil
}

// auditWithRetry는 배치 1콜 + **재시도 1회**다(§6.2-5). 재시도는 미지
// PredID(파싱 실패)에만 건다 — 전송·디코드 오류는 그대로 올린다(run 실패의
// 원인은 llm_error이지 admission_failure가 아니다).
func auditWithRetry(ctx context.Context, aud Auditor, subjects []AuditSubject) (map[string]auditResult, error) {
	var lastErr error
	for attempt := 0; attempt < 2; attempt++ {
		items, err := aud.AuditPredicates(ctx, subjects)
		if err != nil {
			return nil, err
		}
		res, err := collateAudit(subjects, items)
		if err == nil {
			return res, nil
		}
		lastErr = err
	}
	return nil, lastErr
}

// subjectOf는 후보 하나를 R의 심사 단위로 편다 — **여기가 blind의 경계다**:
// 넘어가는 것은 Mechanism·Chain·술어·기왕 관측값뿐이고, 사전확률·출처·
// 순위·증상 서사는 넘어가지 않는다(§6.2-5).
func subjectOf(c AdmittedCandidate, props *evidence.Ledger) AuditSubject {
	s := AuditSubject{
		Hypothesis: c.hypoID,
		Mechanism:  c.IdentityKey.Mechanism,
		Chain:      c.Chain,
	}
	for _, p := range c.PredictedSignals {
		v := PredicateView{Pred: p}
		if props != nil {
			v.Observed = props.Observation(p.ObservationKey())
		}
		s.Predicates = append(s.Predicates, v)
	}
	return s
}

// ── 규칙 6의 실패 (§15.5 enum) ──────────────────────────────────

// AdmissionFailureError는 "R까지 거친 유효 가설 0개 + 재생성 1회도 0개"다 —
// **run 실패**이며 §15.5 reason은 hypothesis_admission_failure다(가설 없이
// 루프는 정의되지 않는다, §6.2-6).
//
// Reason()을 가진 것은 battery의 실패 오류들과 같은 규약이다 — 상위(§14-6)가
// 이 인터페이스로 실패 선언을 조립한다.
type AdmissionFailureError struct {
	// Rounds — 실제로 돈 생성 라운드 수(재생성 1회를 썼으면 2).
	Rounds int
	// Rejects — 마지막 라운드의 반려 사유 전량(운영자 안내·감사).
	Rejects []AdmissionReject
}

func (e *AdmissionFailureError) Error() string {
	// 사유를 문자열에 싣는다(3c 라이브 실측 — run 실패 시 장부가 덤프되지
	// 않아(2c Gap 6, §14-6 몫) 이 메시지가 반려 사유의 유일한 생존 통로였다).
	msg := fmt.Sprintf("가설 등록 전멸(§6.2-6): %d라운드 후 유효 가설 0개 (반려 사유 %d건)",
		e.Rounds, len(e.Rejects))
	for i, r := range e.Rejects {
		if i >= 8 {
			msg += fmt.Sprintf("; …외 %d건", len(e.Rejects)-i)
			break
		}
		msg += fmt.Sprintf("; [%s/%s] %s", r.Rule, r.PredID, r.Reason)
	}
	return msg
}

func (e *AdmissionFailureError) Reason() ledger.FailureReason {
	return ledger.ReasonHypothesisAdmissionFailure
}

// feedbackLines는 반려 사유를 재생성 피드백 문장으로 편다(§6.2-6 "반려
// 사유를 피드백해 재생성 1회"). 소비자: GenerateInput.Feedback → 후보
// 프롬프트(llm/candidates.go).
//
// **지침이 사유보다 앞에 온다**(3c 라이브 실측): 사유만 돌려주면 26B는
// 같은 위반을 되풀이했다 — 반려는 "무엇이 틀렸나"이지 "무엇을 하라"가
// 아니기 때문이다. 규칙별 행동 지침을 중복 없이 먼저 싣고, 원 사유
// 나열은 그대로 뒤에 붙인다(감사·디버깅 통로 보존).
func feedbackLines(rep AdmissionReport) []string {
	var guides, reasons []string
	seen := map[string]bool{}
	for _, rj := range rep.Rejected {
		for _, r := range rj.Rejects {
			if g := admissionGuidance(r); g != "" && !seen[g] {
				seen[g] = true
				guides = append(guides, g)
			}
			reasons = append(reasons, r.String())
		}
	}
	return append(guides, reasons...)
}

// admissionGuidance는 반려 사유 하나를 행동 지침 한 문장으로 번역한다.
// 아는 규칙만 번역하고, 모르는 규칙은 ""(사유 나열만 남는다).
func admissionGuidance(r AdmissionReject) string {
	switch {
	case r.Rule == "SupportEIDs":
		return "지침: support_eids에는 입력 evidence 레코드에 적힌 eid(EIX-로 시작하는 값)만 인용하라 — " +
			"target_id·UUID는 eid가 아니다. 인용한 eid마다 그 eid를 eid_hint로 지목하는 술어를 두고, " +
			"그 술어의 tool·target_id·aspect·metric·entity_key·window를 레코드 값 그대로 복사하라(불일치하면 근거로 세지 않는다)."
	case r.Rule == "규칙 3" && strings.Contains(r.Reason, "necessary"):
		return "지침: 색인에 없는 새 관측을 role=necessary로 최소 1개 예측하라 — " +
			"주어진 레코드 어느 것과도 (tool, target_id, metric, entity_key)가 겹치지 않는 관측이어야 하고(창만 바꾼 것은 새 관측이 아니다), " +
			"그 술어의 eid_hint는 비워 둔다. 이 가설이 참이면 반드시 보여야/안 보여야 하는데 아직 아무도 재지 않은 것을 골라라."
	case r.Rule == "규칙 3":
		return "지침: 예측표가 전부 이미 관측된 사실이다. 아직 아무도 재지 않은 관측을 최소 1개 술어로 넣어라 " +
			"(같은 관측의 window만 바꾼 것은 새 관측이 아니다)."
	case strings.HasPrefix(r.Rule, "규칙 1"):
		return "지침: tool·aspect·predicate는 후보 어휘(vocabulary.tools)의 조합에서, target_id는 vocabulary.target_ids에서만 골라라 — " +
			"지어낸 이름은 그 술어를 반려시킨다."
	}
	return ""
}
