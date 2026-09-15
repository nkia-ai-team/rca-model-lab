package pipeline

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// ── 시험 재료 ───────────────────────────────────────────────────

// auditorFunc는 심사점 R의 시험 대역이다.
type auditorFunc func([]AuditSubject) ([]AuditItem, error)

func (f auditorFunc) AuditPredicates(_ context.Context, s []AuditSubject) ([]AuditItem, error) {
	return f(s)
}

// passAuditor는 전 항목을 유효로 통과시킨다 — R 이외의 축을 보는 시험의 배선.
type passAuditor struct{}

func (passAuditor) AuditPredicates(_ context.Context, subjects []AuditSubject) ([]AuditItem, error) {
	return passItems(subjects), nil
}

func passItems(subjects []AuditSubject) []AuditItem {
	var out []AuditItem
	for _, s := range subjects {
		for _, v := range s.Predicates {
			out = append(out, AuditItem{PredID: v.Pred.PredID, Note: "관련·실질 모두 충족",
				Pertinent: true, Substantive: true})
		}
	}
	return out
}

// reviserFunc는 규칙 5 재요구의 시험 대역이다.
type reviserFunc func(AuditSubject, []AdmissionReject) ([]ledger.SignalPred, error)

func (f reviserFunc) RevisePredicates(_ context.Context, s AuditSubject, fb []AdmissionReject) ([]ledger.SignalPred, error) {
	return f(s, fb)
}

// genCfg는 R이 배선된 기본 설정이다(§6.2-5는 필수 배선).
func genCfg() GenerateConfig { return GenerateConfig{Auditor: passAuditor{}} }

// auditedEvents는 장부의 predicate_audited 기록을 PredID→payload로 모은다.
func auditedEvents(l *ledger.Ledger) map[string]ledger.PredicateAudited {
	out := map[string]ledger.PredicateAudited{}
	for _, ev := range l.Events() {
		if p, ok := ev.Payload.(ledger.PredicateAudited); ok {
			out[p.PredID] = p
		}
	}
	return out
}

// ── R 어댑터 계약: blind 입력 (§6.2-5) ──────────────────────────

// R에게 가는 것은 메커니즘·사슬·술어·기왕 관측값뿐이다 — 인시던트 서사·
// 사전확률·출처·순위는 입력에 없다.
func TestAuditInputIsBlind(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	in.Triage = TriageResult{Symptom: Symptom{TargetID: "app-1", Metric: "http.latency"}}
	c := admCand("db-1", "db-1",
		admPred(func(p *ledger.SignalPred) {
			p.EIDHint, p.Role = eids[0], ledger.RoleCorroborating
		}),
		admPred(func(p *ledger.SignalPred) { p.Metric = "mem.util" }),
	)
	c.SupportEIDs = []string{eids[0]}
	c.PriorRationale = "변경 직후라 유력함" // 순위 근거 — R에게 새면 안 된다
	c.IdentityKey.Mechanism = "커넥션 포화"

	var seen []AuditSubject
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		seen = s
		return passItems(s), nil
	})
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) { return []Candidate{c}, nil })
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0}}, nil })
	if _, _, err := Generate(context.Background(), in, []CandidateSource{src}, g, ledger.New(),
		GenerateConfig{Auditor: aud}); err != nil {
		t.Fatalf("Generate: %v", err)
	}

	if len(seen) != 1 || len(seen[0].Predicates) != 2 {
		t.Fatalf("심사 배치 = %+v", seen)
	}
	s := seen[0]
	if s.Mechanism != "커넥션 포화" || len(s.Chain) == 0 {
		t.Errorf("메커니즘·사슬이 R에게 안 갔다: %+v", s)
	}
	// 서사·순위 부재 단언 — 구조체에 그 자리가 아예 없다.
	blob := fmt.Sprintf("%+v", s)
	for _, leak := range []string{"http.latency", "변경 직후라 유력함", "investigation"} {
		if strings.Contains(blob, leak) {
			t.Errorf("blind 위반: 심사 입력에 %q가 실렸다 — %s", leak, blob)
		}
	}
	// 기왕 관측값이 첨부된다(자명성 판정의 재료) — 미실측 항목은 nil.
	var measured, unmeasured int
	for _, v := range s.Predicates {
		if v.Observed != nil {
			measured++
			if v.Observed.Direction != evidence.DirUp {
				t.Errorf("관측값이 장부 값이 아니다: %+v", v.Observed)
			}
			continue
		}
		unmeasured++
	}
	if measured != 1 || unmeasured != 1 {
		t.Errorf("관측값 첨부 = 실측 %d·미실측 %d, want 1·1", measured, unmeasured)
	}
	// PredID는 최종(가설 ID) 키다 — 심사 결과를 개설 술어에 복원하는 축.
	// (원인 개체 1종이지만 다양성 재요구는 AffordDiversityRerequest 미주입 =
	// 미발동이라 1라운드다 — §14-4 4c 정정의 fail-closed.)
	if !strings.HasPrefix(s.Predicates[0].Pred.PredID, "H1-P") {
		t.Errorf("PredID = %q, want H1-P*", s.Predicates[0].Pred.PredID)
	}
}

// ── 규칙 5 fail-closed (§6.2-5) ─────────────────────────────────

// 반환 누락 항목은 audited_out이다 — 그리고 그 사실이 장부에 남는다.
func TestRule5MissingItemIsAuditedOut(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	c := auditCand(eids[0])
	// 3개 중 하나(P2)만 빼고 답한다.
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		var out []AuditItem
		for _, it := range passItems(s) {
			if strings.HasSuffix(it.PredID, "-P2") {
				continue
			}
			out = append(out, it)
		}
		return out, nil
	})
	l := ledger.New()
	opened, rep, err := generateOne(t, in, c, l, GenerateConfig{Auditor: aud})
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	if len(opened) != 1 {
		t.Fatalf("개설 = %d, want 1 (유효 2개가 남아 재검 통과)", len(opened))
	}
	var out []string
	for _, p := range opened[0].PredictedSignals {
		if p.AuditedOut {
			out = append(out, p.PredID)
		}
	}
	miss := opened[0].ID + "-P2"
	if len(out) != 1 || out[0] != miss {
		t.Fatalf("audited_out = %v, want [%s]", out, miss)
	}
	if !hasPredTag(rep, miss, TagAuditedOut) {
		t.Errorf("audited_out 딱지가 보고서에 없다: %+v", rep.PredTags)
	}
	// 누락도 기록된다 — 무효의 사유가 감사에서 사라지면 안 된다.
	evs := auditedEvents(l)
	if len(evs) != 3 {
		t.Fatalf("predicate_audited = %d건, want 3(전 항목)", len(evs))
	}
	if got := evs[miss]; got.Verdict.Valid() || got.Note == "" {
		t.Errorf("누락 항목 기록 = %+v", got)
	}
}

// 중복 반환은 첫 항목만 산다 — 뒤 항목이 판정을 뒤집지 못한다.
func TestRule5DuplicateKeepsFirst(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	var first string
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		items := passItems(s)
		// 첫 술어를 무효로 한 번 더 보낸다(뒤집기 시도). PredID는 배치에서
		// 끌어온다 — 라운드가 늘면 가설 번호가 달라지는데(다양성 재요구 등),
		// 하드코딩하면 그때 "미지 PredID"가 돼 시험이 다른 것을 재게 된다.
		first = s[0].Predicates[0].Pred.PredID
		items = append(items, AuditItem{PredID: first, Note: "번복", Pertinent: false, Substantive: false})
		return items, nil
	})
	l := ledger.New()
	opened, _, err := generateOne(t, in, auditCand(eids[0]), l, GenerateConfig{Auditor: aud})
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	if len(opened) != 1 || opened[0].PredictedSignals[0].AuditedOut {
		t.Fatalf("첫 항목(유효)이 뒤집혔다: %+v", opened)
	}
	if got := auditedEvents(l)[first]; got.Note == "번복" {
		t.Errorf("장부에 뒤 항목이 실렸다: %+v", got)
	}
}

// 미지 PredID = 배치 파싱 실패 → 재시도 1회 → 재실패면 그 배치 전 가설 반려.
func TestRule5UnknownPredIDRetriesThenRejectsBatch(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	calls := 0
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		calls++
		items := passItems(s)
		items[0].PredID = "H9-P9" // 미지
		return items, nil
	})
	l := ledger.New()
	_, rep, err := generateOne(t, in, auditCand(eids[0]), l, GenerateConfig{Auditor: aud})
	var fail *AdmissionFailureError
	if !errors.As(err, &fail) {
		t.Fatalf("전멸 실패 기대, got %v", err)
	}
	// 라운드마다 배치 1콜 + 재시도 1회 = 2콜, 재생성 1회까지 4콜.
	if calls != 4 {
		t.Errorf("R 호출 = %d, want 4 (배치+재시도, 2라운드)", calls)
	}
	if !hasRule(rep, "규칙 5") {
		t.Errorf("배치 반려 사유가 없다: %+v", rep.Rejected)
	}
	if len(auditedEvents(l)) != 0 {
		t.Error("파싱 실패한 배치의 판정이 기록됐다 — 심사 결과가 없는데 기록은 위조다")
	}
}

// 재시도 1회가 성공하면 그 결과를 쓴다(첫 응답만 보고 반려하지 않는다).
func TestRule5RetrySucceeds(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	calls := 0
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		calls++
		items := passItems(s)
		if calls == 1 {
			items[0].PredID = "없는놈"
		}
		return items, nil
	})
	opened, _, err := generateOne(t, in, auditCand(eids[0]), ledger.New(), GenerateConfig{Auditor: aud})
	if err != nil || len(opened) != 1 {
		t.Fatalf("재시도 성공 경로: opened=%d err=%v (호출 %d)", len(opened), err, calls)
	}
}

// 유효 술어 미달(≥2·necessary ≥1) → 재요구 1회 → 그래도면 **그 가설만** 반려.
func TestRule5ReRequestOnceThenRejectHypothesis(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	// necessary(P2·P3)를 전부 무효로 — 유효 반증형 0개.
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		var out []AuditItem
		for _, v := range s[0].Predicates {
			ok := v.Pred.Role == ledger.RoleCorroborating
			out = append(out, AuditItem{PredID: v.Pred.PredID, Note: "무관",
				Pertinent: ok, Substantive: ok})
		}
		return out, nil
	})
	revised := 0
	rev := reviserFunc(func(s AuditSubject, fb []AdmissionReject) ([]ledger.SignalPred, error) {
		revised++
		if len(fb) == 0 {
			t.Error("재요구에 위반 피드백이 안 갔다")
		}
		return s2preds(s), nil // 같은 술어를 다시 냄 — 재심사도 같은 결과
	})
	l := ledger.New()
	_, rep, err := generateOne(t, in, auditCand(eids[0]), l, GenerateConfig{Auditor: aud, Reviser: rev})
	var fail *AdmissionFailureError
	if !errors.As(err, &fail) {
		t.Fatalf("전 가설 반려 → 규칙 6 전멸 기대, got %v", err)
	}
	if revised != 2 { // 라운드마다 정확히 1회
		t.Errorf("재요구 = %d회, want 라운드당 1회(총 2)", revised)
	}
	if !hasRule(rep, "규칙 5") {
		t.Errorf("규칙 5 반려 사유 없음: %+v", rep.Rejected)
	}
}

// 재요구가 반증형을 되살리면 그 가설은 산다 — 재생성분도 기계 관문을 다시 거친다.
func TestRule5ReRequestRecovers(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	round := 0
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		round++
		var out []AuditItem
		for _, v := range s[0].Predicates {
			ok := round > 1 || v.Pred.Role == ledger.RoleCorroborating
			out = append(out, AuditItem{PredID: v.Pred.PredID, Note: "판정", Pertinent: ok, Substantive: ok})
		}
		return out, nil
	})
	rev := reviserFunc(func(s AuditSubject, _ []AdmissionReject) ([]ledger.SignalPred, error) {
		return s2preds(s), nil
	})
	opened, _, err := generateOne(t, in, auditCand(eids[0]), ledger.New(),
		GenerateConfig{Auditor: aud, Reviser: rev})
	if err != nil || len(opened) != 1 {
		t.Fatalf("재요구 회복 경로: opened=%d err=%v", len(opened), err)
	}
}

// 재요구가 어휘 밖 술어를 들여오면 기계 관문(Admit)이 다시 막는다.
func TestRule5RevisionRunsMachineGatesAgain(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		var out []AuditItem
		for _, v := range s[0].Predicates {
			ok := v.Pred.Role == ledger.RoleCorroborating
			out = append(out, AuditItem{PredID: v.Pred.PredID, Note: "무관", Pertinent: ok, Substantive: ok})
		}
		return out, nil
	})
	rev := reviserFunc(func(s AuditSubject, _ []AdmissionReject) ([]ledger.SignalPred, error) {
		preds := s2preds(s)
		preds[1].TargetID = "어휘밖-1" // 규칙 1ⓐ 위반
		return preds, nil
	})
	_, rep, err := generateOne(t, in, auditCand(eids[0]), ledger.New(),
		GenerateConfig{Auditor: aud, Reviser: rev})
	if err == nil {
		t.Fatal("전멸 실패 기대")
	}
	if !hasRule(rep, "규칙 1ⓐ") {
		t.Errorf("재생성분이 기계 관문을 안 거쳤다: %+v", rep.Rejected)
	}
}

// ── RoleValid 강등 재검 (§6.2-5) ────────────────────────────────

// RoleValid=false → corroborating 강등 → 규칙 5·3 재검 탈락 = 반려.
// 그리고 전 가설이 이 경로로 죽으면 규칙 6이 받는다(C-2 데드락 대조).
func TestRoleValidDemotionRecheckAndC2Deadlock(t *testing.T) {
	rec := admRec("db-1", "cpu.util", evidence.WindowFull)
	in, eids := admInput(t, []string{"db-1"}, rec)
	// 기실측 necessary 하나 + 미실측 necessary 하나 + 회고 corroborating.
	c := admCand("db-1", "db-1",
		admPred(func(p *ledger.SignalPred) {
			p.EIDHint, p.Role = eids[0], ledger.RoleCorroborating
		}),
		admPred(func(p *ledger.SignalPred) { p.Metric = "mem.util" }), // 미실측 necessary
	)
	c.SupportEIDs = []string{eids[0]}
	// R이 미실측 necessary의 RoleValid=false를 답해도 기계가 nil로 덮는다
	// (C-2) — 관측값이 없으면 자명성 심사 대상이 아니다.
	no := false
	aud := auditorFunc(func(s []AuditSubject) ([]AuditItem, error) {
		var out []AuditItem
		for _, v := range s[0].Predicates {
			out = append(out, AuditItem{PredID: v.Pred.PredID, Note: "자명",
				Pertinent: true, Substantive: true, RoleValid: &no})
		}
		return out, nil
	})
	l := ledger.New()
	opened, _, err := generateOne(t, in, c, l, GenerateConfig{Auditor: aud})
	if err != nil || len(opened) != 1 {
		t.Fatalf("미실측 necessary는 강등 대상이 아니다(C-2): opened=%d err=%v", len(opened), err)
	}
	if got := opened[0].PredictedSignals[1].Role; got != ledger.RoleNecessary {
		t.Errorf("미실측 necessary가 강등됐다: %s", got)
	}
	if v := auditedEvents(l)["H1-P2"].Verdict.RoleValid; v != nil {
		t.Errorf("미실측 항목의 RoleValid가 기록됐다: %v", *v)
	}

	// 이번엔 necessary가 **기실측**이라 R이 자명성을 판정할 수 있다 —
	// 강등되면 반증형이 0이 되어 재검 탈락 → 전 가설 반려 → 규칙 6.
	c2 := admCand("db-1", "db-1",
		admPred(func(p *ledger.SignalPred) {
			p.EIDHint, p.Role = eids[0], ledger.RoleCorroborating
		}),
		admPred(func(p *ledger.SignalPred) { p.Metric = "mem.util" }),
		admPred(func(p *ledger.SignalPred) { p.EIDHint = eids[0] }), // 기실측 necessary
	)
	c2.SupportEIDs = []string{eids[0]}
	l2 := ledger.New()
	_, rep, err := generateOne(t, in, c2, l2, GenerateConfig{Auditor: auditorFunc(
		func(s []AuditSubject) ([]AuditItem, error) {
			var out []AuditItem
			for _, v := range s[0].Predicates {
				// 미실측 necessary(P2)는 무효로, 기실측 necessary(P3)는 자명으로.
				// (라운드가 바뀌면 가설 번호가 올라가므로 접미로 짚는다.)
				valid := !strings.HasSuffix(v.Pred.PredID, "-P2")
				out = append(out, AuditItem{PredID: v.Pred.PredID, Note: "판정",
					Pertinent: valid, Substantive: valid, RoleValid: &no})
			}
			return out, nil
		})})
	var fail *AdmissionFailureError
	if !errors.As(err, &fail) {
		t.Fatalf("강등 재검 반려 → 규칙 6 기대, got %v", err)
	}
	if fail.Reason() != ledger.ReasonHypothesisAdmissionFailure {
		t.Errorf("실패 enum = %s", fail.Reason())
	}
	if !hasPredTagSuffix(rep, "-P3", TagRoleDemoted) {
		t.Errorf("강등 딱지가 없다: %+v", rep.PredTags)
	}
	if !hasRule(rep, "규칙 5") {
		t.Errorf("재검 반려 사유 없음: %+v", rep.Rejected)
	}
}

// ── 규칙 6 (§6.2-6) ─────────────────────────────────────────────

// 유효 가설 0개 → 반려 사유 피드백으로 재생성 1회 → 그래도 0이면 run 실패.
func TestRule6RegeneratesOnceThenFails(t *testing.T) {
	in, _ := admInput(t, []string{"db-1"})
	rounds := 0
	var feedbacks [][]string
	src := sourceFunc(func(got GenerateInput) ([]Candidate, error) {
		rounds++
		feedbacks = append(feedbacks, got.Feedback)
		bad := admCand("db-1", "db-1", admPred(func(p *ledger.SignalPred) { p.TargetID = "어휘밖-1" }))
		return []Candidate{bad}, nil
	})
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0}}, nil })
	_, _, err := Generate(context.Background(), in, []CandidateSource{src}, g, ledger.New(), genCfg())
	var fail *AdmissionFailureError
	if !errors.As(err, &fail) {
		t.Fatalf("admission failure 기대, got %v", err)
	}
	if rounds != 2 {
		t.Fatalf("생성 라운드 = %d, want 2 (재생성 1회)", rounds)
	}
	if len(feedbacks[0]) != 0 || len(feedbacks[1]) == 0 {
		t.Errorf("피드백 배선: 1라운드 %v · 2라운드 %v", feedbacks[0], feedbacks[1])
	}
	if fail.Reason() != ledger.ReasonHypothesisAdmissionFailure {
		t.Errorf("§15.5 enum = %s", fail.Reason())
	}
	if !ledger.ValidFailureReason(fail.Reason()) {
		t.Error("enum이 §15.5 목록 밖")
	}
}

// 재생성이 성공하면 run은 계속된다.
func TestRule6RegenerationRecovers(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	rounds := 0
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		rounds++
		if rounds == 1 {
			return []Candidate{admCand("db-1", "db-1", admPred(func(p *ledger.SignalPred) { p.TargetID = "어휘밖-1" }))}, nil
		}
		return []Candidate{auditCand(eids[0])}, nil
	})
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0}}, nil })
	opened, rep, err := Generate(context.Background(), in, []CandidateSource{src}, g, ledger.New(), genCfg())
	if err != nil || len(opened) != 1 {
		t.Fatalf("재생성 회복: opened=%d err=%v", len(opened), err)
	}
	// 가설 번호는 라운드를 넘어 이어진다 — 1라운드 반려분과 ID가 겹치면
	// predicate_audited 감사 기록의 복원이 불가능해진다.
	if opened[0].ID != "H1" {
		t.Errorf("ID = %s (1라운드는 기계 관문에서 죽어 번호를 쓰지 않았다)", opened[0].ID)
	}
	if !hasRunTag(rep, TagCompetitorScarcity) {
		t.Errorf("경쟁 공집합 보정 기록 없음: %v", rep.RunTags)
	}
}

// 개설 1~2개면 경쟁 공집합 보정을 **기록**한다(판정 본체는 §14-5).
func TestRule6CompetitorScarcityRecorded(t *testing.T) {
	in, eids := admInput(t, []string{"db-1", "stor-1"},
		admRec("db-1", "cpu.util", evidence.WindowFull), admRec("stor-1", "cpu.util", evidence.WindowFull))
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		a := auditCand(eids[0])
		b := auditCand(eids[1])
		b.TargetID, b.IdentityKey.CauseEntity = "stor-1", "stor-1"
		b.Chain[0].Entity = "stor-1"
		for i := range b.PredictedSignals {
			b.PredictedSignals[i].TargetID = "stor-1"
		}
		b.PredictedSignals[0].EIDHint = eids[1]
		b.PredictedSignals[2].EIDHint = eids[1]
		return []Candidate{a, b}, nil
	})
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0}, {1}}, nil })
	opened, rep, err := Generate(context.Background(), in, []CandidateSource{src}, g, ledger.New(), genCfg())
	if err != nil || len(opened) != 2 {
		t.Fatalf("opened=%d err=%v", len(opened), err)
	}
	if !hasRunTag(rep, TagCompetitorScarcity) {
		t.Errorf("2개 개설인데 competitor_scarcity 없음: %v", rep.RunTags)
	}
}

// ── 3a 인계 ② — TopK 탈락자 표식·Admit nil 관문 ─────────────────

func TestAdmitRejectsWhenPropositionsNil(t *testing.T) {
	in, _ := admInput(t, []string{"db-1"})
	in.Propositions = nil
	rep := Admit([]Candidate{admCand("db-1", "db-1", admPred())}, in)
	if len(rep.Admitted) != 0 || len(rep.Rejected) != 1 {
		t.Fatalf("명제 장부 nil은 전원 반려여야 한다: %+v", rep)
	}
}

func TestTopKDroppedIsTagged(t *testing.T) {
	in, eids := admInput(t, []string{"db-1", "stor-1"},
		admRec("db-1", "cpu.util", evidence.WindowFull), admRec("stor-1", "cpu.util", evidence.WindowFull))
	a := auditCand(eids[0])
	a.Prior = ledger.PriorHigh
	b := auditCand(eids[1])
	b.TargetID, b.IdentityKey.CauseEntity, b.Prior = "stor-1", "stor-1", ledger.PriorLow
	b.Chain[0].Entity = "stor-1"
	for i := range b.PredictedSignals {
		b.PredictedSignals[i].TargetID = "stor-1"
	}
	b.PredictedSignals[0].EIDHint, b.PredictedSignals[2].EIDHint = eids[1], eids[1]
	b.SupportEIDs = []string{eids[1]}
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) { return []Candidate{a, b}, nil })
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0}, {1}}, nil })
	cfg := genCfg()
	cfg.TopK = 1
	opened, rep, err := Generate(context.Background(), in, []CandidateSource{src}, g, ledger.New(), cfg)
	if err != nil || len(opened) != 1 {
		t.Fatalf("opened=%d err=%v", len(opened), err)
	}
	var dropped, kept int
	for _, c := range rep.Admitted {
		if hasTag(c.Tags, TagTopKDropped) {
			dropped++
			continue
		}
		kept++
	}
	if dropped != 1 || kept != 1 {
		t.Fatalf("탈락 표식 = 탈락 %d·개설 %d, want 1·1", dropped, kept)
	}
}

// ── 시험 보조 ───────────────────────────────────────────────────

// auditCand는 R 시험용 기준 후보다 — 술어 3개(회고 corroborating 1 +
// 미실측 necessary 2)라 하나가 무효가 돼도 재검이 통과한다.
func auditCand(eid string) Candidate {
	c := admCand("db-1", "db-1",
		admPred(func(p *ledger.SignalPred) {
			p.EIDHint, p.Role = eid, ledger.RoleCorroborating
		}),
		admPred(func(p *ledger.SignalPred) { p.Metric = "mem.util" }),
		admPred(func(p *ledger.SignalPred) { p.Metric = "disk.util" }),
	)
	c.SupportEIDs = []string{eid}
	return c
}

// s2preds는 심사 단위의 술어를 그대로 돌려준다(재요구 대역용).
func s2preds(s AuditSubject) []ledger.SignalPred {
	out := make([]ledger.SignalPred, len(s.Predicates))
	for i, v := range s.Predicates {
		out[i] = v.Pred
	}
	return out
}

// generateOne은 후보 하나짜리 깔때기다.
func generateOne(t *testing.T, in GenerateInput, c Candidate, l *ledger.Ledger, cfg GenerateConfig) ([]ledger.HypothesisEntry, AdmissionReport, error) {
	t.Helper()
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) { return []Candidate{c}, nil })
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0}}, nil })
	return Generate(context.Background(), in, []CandidateSource{src}, g, l, cfg)
}

func hasPredTag(rep AdmissionReport, predID string, tag AdmissionTag) bool {
	for _, t := range rep.PredTags {
		if t.PredID == predID && t.Tag == tag {
			return true
		}
	}
	return false
}

func hasPredTagSuffix(rep AdmissionReport, suffix string, tag AdmissionTag) bool {
	for _, t := range rep.PredTags {
		if strings.HasSuffix(t.PredID, suffix) && t.Tag == tag {
			return true
		}
	}
	return false
}

func hasRunTag(rep AdmissionReport, tag AdmissionTag) bool { return hasTag(rep.RunTags, tag) }

// Auditor 전송 오류는 admission_failure가 아니라 그대로 전파된다(3b 독립
// 검증 시험 공백 지적) — §15.5 원인이 다르다: 심사가 죽은 것(llm_error)과
// 가설이 전멸한 것(admission_failure)을 섞으면 실패 선언이 거짓이 된다.
func TestAuditorTransportErrorPropagates(t *testing.T) {
	in, eids := admInput(t, []string{"db-1"}, admRec("db-1", "cpu.util", evidence.WindowFull))
	c := admCand("db-1", "db-1",
		admPred(func(p *ledger.SignalPred) {
			p.EIDHint, p.Role = eids[0], ledger.RoleCorroborating
		}),
		admPred(func(p *ledger.SignalPred) { p.Metric = "mem.util" }),
	)
	c.SupportEIDs = []string{eids[0]}

	sentinel := fmt.Errorf("LLM 전송 실패")
	aud := auditorFunc(func([]AuditSubject) ([]AuditItem, error) { return nil, sentinel })
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) { return []Candidate{c}, nil })
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0}}, nil })
	_, _, err := Generate(context.Background(), in, []CandidateSource{src}, g, ledger.New(),
		GenerateConfig{Auditor: aud})
	if !errors.Is(err, sentinel) {
		t.Fatalf("전송 오류가 전파돼야 함: %v", err)
	}
	var af *AdmissionFailureError
	if errors.As(err, &af) {
		t.Fatalf("전송 오류가 admission_failure로 둔갑함: %v", err)
	}
}
