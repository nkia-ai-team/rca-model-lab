// 파이프라인 [4] 가설 생성 — 네 단계 깔때기 (설계 §4, 2026-07-13 확정):
// ①출처별 후보 생성(LLM) ②스키마 검증(규칙) ③중복 그룹핑(LLM은 인덱스
// 묶음만, 병합은 규칙) ④사전확률 상위 K 개설(규칙). 가설 생성이 곧 장부
// 개설이라 결과는 반환값이 아니라 장부 이벤트다 — 반려·탈락도 전부
// hypothesis_rejected_at_creation으로 감사 기록에 남는다.
package pipeline

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// GenerateInput은 출처 모듈들의 책상이다 — [1]~[3] 산출물 묶음. 각 출처는
// 자기 몫만 읽는다(변경 이력→Changes, 멤버 이벤트→Triage, 조사 관측→
// Evidence, 과거 사례→구현체가 검색 인터페이스를 내장).
type GenerateInput struct {
	Triage  TriageResult
	Changes ChangeScanResult
	// Evidence — [3]의 산출 본체인 evidence index다(스펙 §4·§5).
	// **슬라이스가 아니라 index를 넘기는 이유**: §5.3 절단 우선순위가
	// active 뷰(§5.7)뿐 아니라 §5.2 롤업 카운트("normal+ok는 레코드를 접고
	// 롤업 카운트로만")를 요구하는데, 롤업은 index가 계산한다. 레코드
	// 슬라이스만 주면 소비자마다 롤업을 다시 셈해 셈법이 갈린다.
	Evidence *evidence.Index

	// Vocab — 위상 어휘(§6.2-1ⓐ, append-only). 원천은 [3] ScreenResult.
	// Targets(동결 조사 범위 = seed ∪ A0 신규)이고 W3 확장은 Extend가 진다.
	// 소비자: 등록 규칙 1ⓐ(admission.go) · 후보 프롬프트의 어휘 열거(llm).
	// **nil은 공집합이다**(fail-closed) — 어휘 미배선이 무검사 통과가 되지
	// 않는다.
	Vocab *TopologyVocab
	// Propositions — 명제 장부(§6.0, run 스코프 전역). 소비자: 등록 규칙 2
	// (pre_satisfied)·3(미판정 ≥1)의 기실측 조회. 창 축 무시 조회
	// (MeasuredAnyWindow)가 창 변형 재등록 우회를 막는다(4차 A-3).
	Propositions *evidence.Ledger

	// DeltaKeys — 직전 widening이 만든 material delta의 관측 키(§7.4).
	// **재생성 경로에서만 비지 않는다**(§14-4 4d): 등록 규칙 3의 delta 문맥
	// 해석이 읽는 유일한 입력이며, nil이면 규칙 3은 종전 그대로다 —
	// 첫 생성 경로의 판정은 이 필드가 생기기 전과 한 글자도 다르지 않다.
	DeltaKeys map[evidence.ObservationKey]bool

	// Feedback — 직전 라운드의 반려 사유(§6.2-6 "반려 사유를 피드백해
	// 재생성 1회"). 첫 라운드는 비어 있다. 소비자: 후보 프롬프트
	// (llm/candidates.go) — 같은 위반을 반복하지 말라는 재요구다.
	Feedback []string

	// IndexTrunc — §5.3 payload 축약 누적기(§14-7 계측). 생산자는 llm의
	// payload 빌더, 소비자는 리포트 한계 블록. 재생성은 base 복사로
	// 같은 포인터를 물려받는다. nil이면 계측 없이 돈다(리포트는
	// not_measured로 정직).
	IndexTrunc *ledger.IndexTruncStats
}

// Candidate는 스키마 검증 전의 가설 제안이다 — HypothesisEntry에서 ID를
// 뺀 꼴(ID는 개설 시 규칙이 부여). Source는 제안한 출처 하나 — 합집합
// 꼬리표(Sources)는 병합 규칙이 만든다.
//
// **RefutationCondition은 폐기됐다**(스펙 §14-3, 3차 검토 C-13): 반증은 가설
// 이름에 붙은 자유문이 아니라 typed 술어에 붙는다. 반증력의 자리는 등록 규칙 3의
// "미실측 necessary 술어 ≥1" 요건이 계승한다(§6.2-3 — 필수 술어 = 이 가설을
// 죽일 수 있는 예측).
type Candidate struct {
	TargetID       string
	Chain          []ledger.ChainStep
	Source         ledger.HypoSource
	Prior          ledger.Prior
	PriorRationale string

	// ── 가설 계약 v2의 3필드(§6). 등록 규칙의 판정 대상이다.

	// IdentityKey — 가설의 정체성. CauseEntity가 다양성 소프트의 계수 축이고
	// (sentinel 제외), Mechanism은 닫힌 enum이 아니다(§6.1).
	IdentityKey ledger.HypoIdentity
	// PredictedSignals — 참이면 보여야/안 보여야 하는 신호. 소비자: 등록
	// 규칙 1~4 · §7.2 식별력 정렬 · §14-5 판정 게이트.
	PredictedSignals []ledger.SignalPred
	// SupportEIDs — 개설 근거 레코드(active). 결박·selector 검사는 Admit이
	// 하고, 미결박은 불산입된다(§6 C-4).
	SupportEIDs []string
}

// CandidateSource는 출처 모듈(LLM)의 자리다 — §5의 출처 4개 각각의
// 구현체. 켜고 끄기는 호출자가 슬라이스에 넣고 빼는 것(ablation 스위치).
type CandidateSource interface {
	Propose(ctx context.Context, in GenerateInput) ([]Candidate, error)
}

// Grouper는 깔때기 3단계의 LLM 판단 자리다 — 후보 목록을 받아 "같은
// 의심 대상 + 같은 메커니즘 계열" 묶음을 인덱스로만 돌려준다(내용
// 다시쓰기 금지, spike ⑤). 완전 분할 검증과 병합은 규칙이 한다.
type Grouper interface {
	Group(ctx context.Context, cands []Candidate) ([][]int, error)
}

// GenerateConfig — TopK 0이면 5 (설계 §4: K=5면 루프 예산 probe 10~15회).
type GenerateConfig struct {
	TopK int
	Now  func() time.Time // nil = time.Now

	// Auditor — 심사점 R(§6.2-5). **필수다**(nil이면 Generate가 오류):
	// R은 등록 규칙의 한 겹이지 선택 사양이 아니고, nil을 "심사 생략"으로
	// 관용하면 배선을 빠뜨린 run이 조용히 무심사로 개설된다(Grouper·명제
	// 장부와 같은 fail-closed 규약).
	Auditor Auditor
	// Reviser — 규칙 5의 재요구 1회(§6.2-5 "위반 피드백 재요구 1회").
	// **선택이다**: nil이면 재요구 없이 그 가설이 반려된다 — 없는 쪽이
	// 보수 방향이므로 fail-closed가 깨지지 않는다.
	Reviser PredicateReviser

	// AffordDiversityRerequest — 다양성 재요구(소프트 규칙)를 지금 지출해도
	// 되는가의 판정. **§7.6의 "지출 전 잔여 검사"다**: 재요구는 3원 전체
	// 재호출이라 한 라운드가 통째로 나가고, 그 지출이 [5] 루프를 굶기면
	// 다양성이 노리던 경쟁 요건은 오히려 영구 미충족이 된다(라이브 회귀
	// 2026-08-04: 재요구 후 누적 317k > 상한 200k, 바퀴 0·probe 0).
	//
	// **nil은 "재요구 안 함"이다**(fail-closed). 계정·상한은 loop 패키지가
	// 들고 있고 pipeline은 그것을 import할 수 없어(순환), 검사식의 배선은
	// 호출자(cmd/rca) 몫이다 — 배선을 빠뜨린 run이 상한을 모른 채 지출하는
	// 쪽으로 흐르지 않게 없는 쪽을 보수로 둔다.
	AffordDiversityRerequest func() bool
}

// merged는 병합된 후보다 — order는 그룹 첫 후보의 생성 순서로,
// 사전확률 동률의 타이브레이크다.
type merged struct {
	Candidate
	Sources []ledger.HypoSource
	order   int
	// mergeAudit — 병합의 정보 유실 기록(§6.2 병합 규칙 C-14). 단일 후보
	// 그룹이면 nil이다(유실이 없으므로 기록도 없다).
	mergeAudit *ledger.MergeAudit
}

// Generate는 깔때기를 돌려 장부를 개설한다. 반환값은 개설된 엔트리
// (장부 기록과 동일 내용, 편의용)다.
//
// **규칙 6의 라운드 루프가 여기 있다**(§6.2-6): 한 라운드가 유효 가설 0개로
// 끝나면 반려 사유를 피드백해 **재생성 1회**를 돌리고, 그래도 0이면
// AdmissionFailureError(=§15.5 hypothesis_admission_failure)로 run을 끝낸다 —
// 가설 없이 루프는 정의되지 않는다.
// maxHypoSeq는 장부에 이미 쓰인 가설 번호("H<n>")의 최댓값이다. 개설
// (hypothesis_created)만이 아니라 심사 기록(predicate_audited의 PredID
// "H<n>-P<m>")도 센다 — R 반려분은 개설 이벤트가 없지만 번호는 심사
// 기록에 남았고, 그 번호의 재사용도 감사 복원을 깨기는 마찬가지다.
func maxHypoSeq(l *ledger.Ledger) int {
	maxN := 0
	scan := func(id string) {
		var n int
		if _, err := fmt.Sscanf(id, "H%d", &n); err == nil && n > maxN {
			maxN = n
		}
	}
	for _, ev := range l.Events() {
		switch p := ev.Payload.(type) {
		case ledger.HypothesisCreated:
			scan(p.Entry.ID)
		case ledger.PredicateAudited:
			scan(p.PredID) // "H3-P1" — Sscanf가 H3까지 읽는다
		}
	}
	return maxN
}

func Generate(ctx context.Context, in GenerateInput, sources []CandidateSource, g Grouper, l *ledger.Ledger, cfg GenerateConfig) ([]ledger.HypothesisEntry, AdmissionReport, error) {
	var rep AdmissionReport
	if l == nil || g == nil {
		return nil, rep, fmt.Errorf("generate: 장부·Grouper 필수")
	}
	// 명제 장부 없이는 등록 규칙 2·3이 판정 불능이다 — 조용히 "전부
	// 미실측"으로 흐르면 회고 가설이 그대로 통과한다(fail-closed, §6.2-3).
	if in.Propositions == nil {
		return nil, rep, fmt.Errorf("generate: 명제 장부(§6.0) 필수 — 등록 규칙 2·3의 기실측 조회")
	}
	// 심사점 R도 같은 규약이다 — 등록 규칙의 한 겹이라 배선 누락이
	// "무심사 개설"로 흘러서는 안 된다(§6.2-5).
	if cfg.Auditor == nil {
		return nil, rep, fmt.Errorf("generate: 심사점 R 어댑터(§6.2-5) 필수")
	}
	// hypoSeq는 라운드를 넘어 이어지는 가설 번호다 — 재생성 라운드가 1라운드
	// 반려분과 같은 ID를 재사용하면 predicate_audited 감사 기록이 두 후보를
	// 가리켜 복원이 불가능해진다. **호출 경계도 넘는다**(5b 라이브 실측):
	// 재생성(§7.5)은 Generate를 새로 호출하므로 0에서 재시작하면 기존
	// 가설과 "H2 중복"으로 장부 검증에 걸려 run이 죽는다 — 장부의 기존
	// 최대 번호에서 이어 매긴다.
	hypoSeq := maxHypoSeq(l)
	rerequested := false
	for round := 1; ; round++ {
		opened, r, err := generateRound(ctx, in, sources, g, l, cfg, &hypoSeq, round == 1)
		if errors.Is(err, errDiversityUnmet) {
			// 다양성 재요구(§6.2 소프트 규칙, §14-3 이월). **개설 전에** 끊고
			// 돌린다 — 개설 후에 다시 돌리면 같은 가설이 장부에 두 번 선다.
			// 재요구는 1회뿐이고(round 2는 retry 인자가 false), 그래도
			// 미충족이면 diversity_unmet을 단 채 그대로 진행한다.
			in.Feedback = append(feedbackLines(r), diversityGuidance)
			rerequested = true
			continue
		}
		if err != nil {
			return nil, r, err
		}
		if rerequested {
			r.RunTags = append(r.RunTags, TagDiversityRerequested)
		}
		if len(opened) > 0 {
			// 경쟁 공집합 보정(§6.2-6) — 1~2개면 진행하되 기록한다.
			// **판정 본체(confirmed 차단·probable 상한)는 §14-5 몫이다.**
			if len(opened) <= 2 {
				r.RunTags = append(r.RunTags, TagCompetitorScarcity)
			}
			return opened, r, nil
		}
		if round >= 2 {
			return nil, r, &AdmissionFailureError{Rounds: round, Rejects: allRejects(r)}
		}
		in.Feedback = feedbackLines(r)
	}
}

// hasTag — run 딱지 조회.
func hasTag(tags []AdmissionTag, want AdmissionTag) bool {
	for _, t := range tags {
		if t == want {
			return true
		}
	}
	return false
}

// allRejects는 보고서의 반려 사유 전량이다(실패 선언·감사용).
func allRejects(rep AdmissionReport) []AdmissionReject {
	var out []AdmissionReject
	for _, rj := range rep.Rejected {
		out = append(out, rj.Rejects...)
	}
	return out
}

// errDiversityUnmet은 "이 라운드는 다양성 미충족이니 재요구하라"는 내부
// 신호다 — 실패가 아니라 라운드 루프의 갈래다.
var errDiversityUnmet = errors.New("다양성 미충족 — 재요구")

// diversityGuidance는 재요구에 실리는 행동 지침이다. 반려 사유 피드백과
// 같은 통로(GenerateInput.Feedback)로 간다 — LLM 쪽에 새 입구를 내지 않는다.
const diversityGuidance = "지침: 원인 개체(cause_entity)가 서로 다른 가설을 최소 2종 내라 — " +
	"같은 대상의 변형만 늘리지 말고, 다른 계층·다른 대상에서 증상을 설명할 수 있는 경로를 제시하라. " +
	"확신이 낮으면 prior=low로 정직하게 달되, 경쟁 가설 없이 한 대상만 내지 마라."

// generateRound는 깔때기 한 바퀴다 — ①생성 ②스키마 ③그룹핑·병합
// ④기계 관문(규칙 1·2·3) ⑤상위 K ⑥집단 규칙 ⑦심사점 R+재검 ⑧개설.
//
// retryDiversity가 참이면 다양성 미충족(⑦ 통과 집합 기준)을 만났을 때
// **개설 전에** errDiversityUnmet으로 끊는다 — 재요구는 호출자의 라운드
// 루프가 돌린다.
func generateRound(ctx context.Context, in GenerateInput, sources []CandidateSource, g Grouper, l *ledger.Ledger, cfg GenerateConfig, hypoSeq *int, retryDiversity bool) ([]ledger.HypothesisEntry, AdmissionReport, error) {
	var rep AdmissionReport
	k := cfg.TopK
	if k == 0 {
		k = 5
	}
	now := cfg.Now
	if now == nil {
		now = time.Now
	}
	reject := func(c Candidate, rule string) error {
		_, err := l.Append(ledger.ActorRule, now(), ledger.HypothesisRejectedAtCreation{
			ProposalSummary: summarize(c), ViolatedRule: rule,
		})
		return err
	}

	// ① 출처별 후보 생성 — 출처 순서가 곧 생성 순서(순위 타이브레이크).
	var cands []Candidate
	for _, s := range sources {
		got, err := s.Propose(ctx, in)
		if err != nil {
			return nil, rep, fmt.Errorf("generate: 출처 제안: %w", err)
		}
		cands = append(cands, got...)
	}

	// ② 스키마 검증(규칙) — 장부 개설 검증과 같은 규칙을 앞당겨,
	// 위반을 감사 기록으로 남기고 거른다.
	var valid []Candidate
	for _, c := range cands {
		if rule := schemaViolation(c); rule != "" {
			if err := reject(c, rule); err != nil {
				return nil, rep, err
			}
			continue
		}
		valid = append(valid, c)
	}

	// ③ 중복 그룹핑 — LLM은 묶음만, 병합은 규칙이 기계적으로. 후보가
	// 하나 이하면 LLM을 부르지 않는다.
	var groups [][]int
	switch {
	case len(valid) > 1:
		var err error
		groups, err = g.Group(ctx, valid)
		if err != nil {
			return nil, rep, fmt.Errorf("generate: 그룹핑: %w", err)
		}
		if err := checkPartition(groups, len(valid)); err != nil {
			return nil, rep, fmt.Errorf("generate: 그룹핑: %w", err)
		}
	case len(valid) == 1:
		groups = [][]int{{0}}
	}
	ms := make([]merged, 0, len(groups))
	for _, grp := range groups {
		ms = append(ms, mergeGroup(valid, grp))
	}

	// ④ 등록 규칙 기계 4겹 중 후보 단위(규칙 1·2·3 + SupportEIDs 결박,
	// §6.2). 순수 함수 층(admission.go)이 판정하고 여기서는 기록만 한다 —
	// 3b의 R 심사·재요구 루프가 같은 보고서를 소비한다.
	mcands := make([]Candidate, len(ms))
	for i, m := range ms {
		mcands[i] = m.Candidate
	}
	rep = Admit(mcands, in)
	for _, rj := range rep.Rejected {
		if err := reject(rj.Candidate, rj.RejectReasons()); err != nil {
			return nil, rep, err
		}
	}

	// ⑤ 사전확률 상위 K 개설 — 동률은 생성 순서. 탈락도 감사 기록.
	adm := rep.Admitted
	sort.SliceStable(adm, func(i, j int) bool {
		a, b := ms[adm[i].Index], ms[adm[j].Index]
		if a.Prior != b.Prior {
			return a.Prior > b.Prior
		}
		return a.order < b.order
	})
	cut := adm
	if len(cut) > k {
		for i := k; i < len(adm); i++ {
			// **탈락자에 표식을 단다**(3a 인계 ②): 기계 관문은 통과했으므로
			// Rejected가 아니라 Admitted에 남는데, 무표식이면 보고서 소비자가
			// 개설분과 구분할 수 없다.
			adm[i].Tags = append(adm[i].Tags, TagTopKDropped)
			if err := reject(adm[i].Candidate, fmt.Sprintf("상위 K=%d 탈락 (사전확률 순)", k)); err != nil {
				return nil, rep, err
			}
		}
		cut = adm[:k]
	}

	// ⑥ 집단 규칙(규칙 4 식별력·다양성 소프트·개수)은 **개설 확정 집합**에
	// 건다 — 탈락할 후보가 식별력을 만들어 준 것으로 세어지지 않게.
	cut, rep.RunTags = AdmitCohort(cut)

	// ⑦ 규칙 5 — 심사점 R(LLM) + 강등·무효 반영 후의 기계 재검(§6.2-5).
	//    **개설 전에 돈다**: 반려된 후보는 hypothesis_created가 아예 없어야
	//    하고(장부 검증이 그렇게 설계됐다 — ledger.go의 predicate_audited는
	//    가설 실재를 검사하지 않는다), 그래서 가설 ID는 여기서 미리 부여한다.
	//    PredID도 이 시점에 가설 ID로 다시 부여된다(Admit의 "C<n>-P<m>"은
	//    후보 단위 임시 ID였다) — R 배치의 대응 키가 곧 최종 키여야 심사
	//    결과를 개설 엔트리의 술어에 복원할 수 있다(B-5).
	for i := range cut {
		*hypoSeq++
		cut[i].hypoID = fmt.Sprintf("H%d", *hypoSeq)
		cut[i].PredictedSignals = ledger.AssignPredIDs(cut[i].hypoID, cut[i].PredictedSignals)
	}
	// B-5 — 가설 간 PredID 유일성(§14-4 4b). **R 배치보다 먼저** 건다:
	// collateAudit이 PredID를 전 가설 공통 키로 쓰므로, 충돌을 안고 배치에
	// 들어가면 한 가설의 심사 결과가 다른 가설 술어에 박힌다.
	if conflicts := CrossHypoPredIDConflicts(cut); len(conflicts) > 0 {
		// **제자리 축약이 아니다**: cut은 정렬된 adm의 앞부분을 공유하는
		// 슬라이스라, cut[:0]에 되쓰면 반려된 후보의 자리가 덮여 보고서가
		// 거짓이 된다.
		keep := make([]AdmittedCandidate, 0, len(cut))
		for i := range cut {
			rj, bad := conflicts[i]
			if !bad {
				keep = append(keep, cut[i])
				continue
			}
			if err := reject(cut[i].Candidate, RejectedCandidate{Rejects: rj}.RejectReasons()); err != nil {
				return nil, rep, err
			}
			rep.Rejected = append(rep.Rejected, RejectedCandidate{
				Candidate: cut[i].Candidate, Index: cut[i].Index, Rejects: rj,
			})
		}
		cut = keep
	}

	// runAudit은 cut의 원소를 **제자리에서** 고친다(술어 딱지·강등·표식) —
	// 그래야 아래에서 보고서에 되돌리는 값이 개설된 실물과 같다.
	kept, audited, err := runAudit(ctx, cut, in, cfg, l, now)
	if err != nil {
		return nil, rep, err
	}
	rep.PredTags = append(rep.PredTags, audited.predTags...)
	for _, rj := range audited.rejected {
		if err := reject(rj.Candidate, rj.RejectReasons()); err != nil {
			return nil, rep, err
		}
		rep.Rejected = append(rep.Rejected, rj)
	}
	// 집단 딱지·R 심사 결과를 보고서에 되돌린다(cut은 정렬된 adm의 앞부분이다)
	// — non_discriminating·audit_rejected는 반려가 아니라 기록이므로 감사에
	// 남아야 한다.
	rep.Admitted = adm
	copy(rep.Admitted, cut)

	// 다양성 재요구(§6.2 소프트 규칙, §14-3 이월) — **개설 직전, 심사를
	// 통과한 집합에** 건다. 앞(⑥)에서 걸면 R 심사가 어차피 전부 반려할
	// 라운드에도 재생성 비용을 물고, 뒤(⑧ 이후)에서 걸면 같은 가설이 장부에
	// 두 번 선다. 판정 대상이 kept인 이유도 같다 — 실제로 열릴 집합의
	// 다양성이 규칙이 말하는 다양성이다.
	//
	// K<2면 재요구하지 않는다: 상위 K가 1이면 어떤 후보가 와도 2종을 채울
	// 수 없어 재생성이 구조적으로 무익하다(딱지는 그대로 남는다).
	if retryDiversity && len(kept) > 0 && k >= 2 && !DiverseCauses(kept) {
		// §7.6 지출 전 잔여 검사 — 미충족이면 **재요구를 생략하고 그대로
		// 개설한다**(기존 fallback: diversity_unmet 기록 후 진행). 생략 사유는
		// 딱지로 남는다 — 안 걸린 것과 못 한 것은 다른 사실이다.
		if cfg.AffordDiversityRerequest != nil && cfg.AffordDiversityRerequest() {
			return nil, rep, errDiversityUnmet
		}
		rep.RunTags = append(rep.RunTags, TagDiversityRerequestUnaffordable)
	}

	// ⑧ 개설.
	var opened []ledger.HypothesisEntry
	for _, m := range kept {
		e := ledger.HypothesisEntry{
			ID:       m.hypoID,
			TargetID: m.TargetID, Chain: m.Chain, Sources: ms[m.Index].Sources,
			Prior: ms[m.Index].Prior, PriorRationale: ms[m.Index].PriorRationale,
			// v2 3필드 — 술어는 R 심사를 거친 것(audited_out 딱지·강등 반영)
			// 그대로다. 딱지는 직렬화된다(§15.5 크래시 재생 — hypothesis_v2.go).
			IdentityKey:      m.IdentityKey,
			PredictedSignals: m.PredictedSignals,
			SupportEIDs:      m.SupportEIDs,
			// 병합 감사 — 비대표 후보의 유실 기록(§6.2 병합 규칙 C-14).
			Merge: ms[m.Index].mergeAudit,
		}
		if _, err := l.Append(ledger.ActorRule, now(), ledger.HypothesisCreated{Entry: e}); err != nil {
			return nil, rep, err
		}
		opened = append(opened, e)
	}
	return opened, rep, nil
}

// schemaViolation은 깔때기 2단계의 검증이다 — 장부 개설 검증(생성 규칙)
// 과 같은 내용. 통과면 빈 문자열.
//
// **"반증 조건 없으면 반려"는 여기서 삭제됐다**(§14-3, 3차 C-13): 반증력의
// 판정은 자유문 유무가 아니라 등록 규칙 3의 "미실측 necessary 술어 ≥1"이
// 진다(Admit → rule3Violations).
func schemaViolation(c Candidate) string {
	switch {
	case c.TargetID == "":
		return "의심 대상 없음"
	case len(c.Chain) == 0:
		return "인과사슬 없음"
	case c.Chain[0].Entity != c.TargetID:
		return "첫 구간 entity ≠ 의심 대상 — 의심 대상은 사슬의 뿌리"
	}
	return ""
}

// mergeGroup은 병합 규칙이다 (설계 §4 3단계 + **§6.2 병합 규칙**): 먼저 나온
// 후보의 IdentityKey를 대표로 유지하고, 출처 꼬리표는 합집합, 사전확률
// (과 그 근거)은 높은 쪽.
//
// **probe 합집합은 폐기됐다**(§14-4 4c): 합칠 자유문 슬롯이 없어졌고, 그
// dedup이 하던 일은 ① 명제 키 dedup이 그대로 진다 — 두 후보가 같은 관측을
// 겨냥했다는 사실은 이제 술어의 관측 키 하나로만 표현된다(§6.2 Grouper 정합).
//
// **§6.2가 추가한 세 합집합(3b)** — 종전에는 두 번째 후보의 계보·반증형
// 술어가 조용히 유실돼 R 심사 결과와 §8 계보 계수가 갈렸다:
//
//	① PredictedSignals — **명제 키(§6.0) 기준 dedup 합집합**. 키에는 Role도
//	   Predicate도 없으므로(관측 키 6성분뿐) 같은 관측을 겨냥한 술어는 Role이
//	   달라도 하나로 접힌다 — 먼저 나온 쪽이 남는다. 접힌 수는 감사에 남는다.
//	② SupportEIDs — 합집합(결박 검사는 Admit이 다시 한다).
//	③ Chain — **말단이 더 깊은 후보의 사슬을 대표로**(C-14). 사슬 말단은 §8
//	   confirmed의 기계 판정 입력이라, 첫 후보 사슬만 남기면 병합 순서가
//	   등급을 좌우했다. 깊이 척도는 현행 타입에서 **구간 수**다 — ChainClaim
//	   (terminal_entity의 EntityKey)로 옮겨지는 §14-4에서 말단 개체 깊이로
//	   정밀해진다. 비대표 사슬은 병합 감사에 통째로 남는다.
func mergeGroup(cands []Candidate, grp []int) merged {
	sort.Ints(grp)
	m := merged{Candidate: cands[grp[0]], order: grp[0]}
	// 합집합은 **사본에** 쌓는다 — append가 원본 후보의 배열을 덮으면
	// (용량이 남는 경우) 반려 기록의 후보가 조용히 변조된다.
	m.PredictedSignals = append([]ledger.SignalPred(nil), m.PredictedSignals...)
	m.SupportEIDs = append([]string(nil), m.SupportEIDs...)
	m.Sources = []ledger.HypoSource{m.Source}
	srcSeen := map[ledger.HypoSource]bool{m.Source: true}
	predSeen := map[evidence.ObservationKey]bool{}
	for _, p := range m.PredictedSignals {
		predSeen[p.ObservationKey()] = true
	}
	eidSeen := map[string]bool{}
	for _, e := range m.SupportEIDs {
		eidSeen[e] = true
	}
	audit := ledger.MergeAudit{MergedCount: len(grp), ChainDepth: len(m.Chain)}
	for _, i := range grp[1:] {
		c := cands[i]
		if !srcSeen[c.Source] {
			srcSeen[c.Source] = true
			m.Sources = append(m.Sources, c.Source)
		}
		for _, p := range c.PredictedSignals { // ① 명제 키 dedup 합집합
			k := p.ObservationKey()
			if predSeen[k] {
				audit.DedupedPreds++
				continue
			}
			predSeen[k] = true
			m.PredictedSignals = append(m.PredictedSignals, p)
		}
		for _, e := range c.SupportEIDs { // ② 합집합
			if e == "" || eidSeen[e] {
				continue
			}
			eidSeen[e] = true
			m.SupportEIDs = append(m.SupportEIDs, e)
		}
		// ③ 사슬 — 더 깊은 쪽이 대표, 밀려난 쪽은 감사로.
		dropped := c.Chain
		if len(c.Chain) > len(m.Chain) {
			dropped, m.Chain = m.Chain, c.Chain
			// 대표 사슬이 바뀌면 뿌리도 바뀐다 — 첫 구간 entity = TargetID는
			// 장부 개설 검증의 불변식이다(ledger.go HypothesisCreated).
			m.TargetID = c.TargetID
			audit.ChainReplaced = true
		}
		audit.ChainDepth = len(m.Chain)
		audit.DroppedChains = append(audit.DroppedChains, ledger.DroppedChain{
			Source: c.Source, Depth: len(dropped), ClaimIDs: chainClaimIDs(dropped),
		})
		if c.Prior > m.Prior {
			m.Prior, m.PriorRationale = c.Prior, c.PriorRationale
		}
	}
	if len(grp) > 1 {
		m.mergeAudit = &audit
	}
	return m
}

// chainClaimIDs는 비대표 사슬의 구간 지목이다(§6.2 "비대표 후보의 ClaimID
// 목록"). 구 ChainStep에는 ClaimID가 없으므로 `<i>:<entity>` 표기를 쓴다 —
// ChainClaim 이관(§14-4)에서 실 ClaimID로 교체된다.
func chainClaimIDs(chain []ledger.ChainStep) []string {
	out := make([]string, len(chain))
	for i, st := range chain {
		out[i] = fmt.Sprintf("%d:%s", i, st.Entity)
	}
	return out
}

// checkPartition — 그룹핑 출력이 0..n-1의 완전 분할인지 (spike ⑤의
// 기준: ID 누락·중복 없음).
func checkPartition(groups [][]int, n int) error {
	seen := make([]bool, n)
	total := 0
	for _, grp := range groups {
		for _, i := range grp {
			if i < 0 || i >= n {
				return fmt.Errorf("인덱스 %d 범위 밖 (후보 %d개)", i, n)
			}
			if seen[i] {
				return fmt.Errorf("인덱스 %d 중복", i)
			}
			seen[i] = true
			total++
		}
	}
	if total != n {
		return fmt.Errorf("후보 %d개 중 %d개만 묶임 — 완전 분할 아님", n, total)
	}
	return nil
}

// summarize는 반려 기록용 한 줄 요약이다.
func summarize(c Candidate) string {
	s := fmt.Sprintf("[%s] %s", c.Source, c.TargetID)
	for _, st := range c.Chain {
		s += " → " + st.Entity + ": " + st.Effect
	}
	return s
}
