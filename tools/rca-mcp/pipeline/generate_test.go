package pipeline

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

type sourceFunc func(GenerateInput) ([]Candidate, error)

func (f sourceFunc) Propose(ctx context.Context, in GenerateInput) ([]Candidate, error) { return f(in) }

type grouperFunc func([]Candidate) ([][]int, error)

func (f grouperFunc) Group(ctx context.Context, c []Candidate) ([][]int, error) { return f(c) }

// cand는 등록 규칙(§6.2)을 통과하는 후보다 — 신 계약에서는 술어·정체성·
// 개설 근거가 없으면 기계 관문에서 반려되므로 깔때기 시험의 재료도 그것을
// 갖춰야 한다.
func cand(target string, src ledger.HypoSource, prior ledger.Prior) Candidate {
	return Candidate{
		TargetID: target,
		Chain: []ledger.ChainStep{
			{Entity: target, Effect: "원인 발생"},
			{Entity: "app-1", Effect: "응답 지연"},
		},
		Source: src, Prior: prior, PriorRationale: string(src) + " 근거",
		IdentityKey: ledger.HypoIdentity{
			CauseEntity: target, Mechanism: "포화", TemporalClaim: ledger.TemporalPrecedes,
		},
		PredictedSignals: []ledger.SignalPred{
			// 기실측 근거를 지목하는 술어(개설 근거 결박) — 회고이므로
			// corroborating이다.
			admPred(func(p *ledger.SignalPred) {
				p.TargetID, p.EIDHint, p.Role = target, genEID(target), ledger.RoleCorroborating
			}),
			// 미실측 necessary — 등록 규칙 3이 요구하는 "죽일 수 있는 예측".
			admPred(func(p *ledger.SignalPred) { p.TargetID, p.Metric = target, "mem.util" }),
		},
		SupportEIDs: []string{genEID(target)},
	}
}

// genFixture는 깔때기 시험의 배선이다 — 대상마다 관측 레코드 하나(개설
// 근거)를 index에 앉히고, 위상 어휘와 명제 장부를 그 index에서 만든다.
var genEIDs = map[string]string{}

func genEID(target string) string { return genEIDs[target] }

func genInput(t *testing.T, targets ...string) GenerateInput {
	t.Helper()
	ix := evidence.NewIndex()
	genEIDs = map[string]string{}
	for _, tg := range targets {
		eid, err := ix.Append(admRec(tg, "cpu.util", evidence.WindowFull))
		if err != nil {
			t.Fatalf("index 적재: %v", err)
		}
		genEIDs[tg] = eid
	}
	return GenerateInput{Evidence: ix, Vocab: NewTopologyVocab(targets...),
		Propositions: evidence.NewLedger(ix)}
}

// 깔때기 전체: 출처 2개 → 스키마 반려 1 → 그룹핑 병합 → 사전확률 순
// 개설. 병합은 먼저 나온 후보를 남기고 출처·probe 합집합, 사전확률은
// 높은 쪽.
func TestGenerate(t *testing.T) {
	change := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		return []Candidate{cand("db-1", ledger.SourceChange, ledger.PriorMedium)}, nil
	})
	invest := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		bad := cand("net-1", ledger.SourceInvestigation, ledger.PriorHigh)
		bad.Chain = nil // 스키마 반려 대상(인과사슬 없음)
		return []Candidate{
			cand("db-1", ledger.SourceInvestigation, ledger.PriorHigh),
			cand("stor-1", ledger.SourceInvestigation, ledger.PriorLow),
			bad,
		}, nil
	})
	var grouped []Candidate
	g := grouperFunc(func(c []Candidate) ([][]int, error) {
		grouped = c
		return [][]int{{1, 0}, {2}}, nil // db-1 둘 병합(순서 뒤집어 전달해도 규칙이 정렬)
	})

	l := ledger.New()
	in := genInput(t, "db-1", "stor-1", "net-1")
	opened, rep, err := Generate(context.Background(), in, []CandidateSource{change, invest}, g, l, genCfg())
	if err != nil {
		t.Fatalf("Generate 실패: %v", err)
	}

	// 그룹핑은 스키마 통과분(3개)만 본다 — 반려분은 이미 걸러짐.
	if len(grouped) != 3 {
		t.Fatalf("Grouper 입력 = %d개, want 3", len(grouped))
	}

	// 개설 = 사전확률 순: H1=db-1(high, 병합), H2=stor-1(low).
	if len(opened) != 2 || opened[0].ID != "H1" || opened[1].ID != "H2" {
		t.Fatalf("opened = %+v", opened)
	}
	h1 := opened[0]
	if h1.TargetID != "db-1" || h1.Prior != ledger.PriorHigh {
		t.Errorf("H1 어긋남: %+v", h1)
	}
	// 먼저 나온 후보(change)가 남고, 출처는 합집합·사전확률 근거는 높은 쪽.
	if want := []ledger.HypoSource{ledger.SourceChange, ledger.SourceInvestigation}; !reflect.DeepEqual(h1.Sources, want) {
		t.Errorf("H1.Sources = %v, want %v", h1.Sources, want)
	}
	if h1.PriorRationale != "investigation 근거" {
		t.Errorf("H1 사전확률 근거 = %q, want 높은 쪽(investigation)", h1.PriorRationale)
	}
	if want := []ledger.HypoSource{ledger.SourceInvestigation}; !reflect.DeepEqual(opened[1].Sources, want) {
		t.Errorf("H2.Sources = %v, want %v", opened[1].Sources, want)
	}

	// 장부 감사 기록: 반려 1(스키마) + 개설 2.
	var rejects, creates int
	for _, ev := range l.Events() {
		switch ev.Type {
		case ledger.EvHypothesisRejectedAtCreation:
			rejects++
		case ledger.EvHypothesisCreated:
			creates++
		}
	}
	if rejects != 1 || creates != 2 {
		t.Errorf("장부 기록 = 반려 %d·개설 %d, want 1·2", rejects, creates)
	}
	if _, ok := l.Hypothesis("H1"); !ok {
		t.Error("H1이 장부에 없음")
	}

	// 신 계약 3필드가 개설 엔트리에 실린다 — PredID는 개설 시 **가설 ID로**
	// 다시 부여된다(§6 B-5 기계 부여).
	if h1.IdentityKey.CauseEntity != "db-1" || h1.IdentityKey.TemporalClaim != ledger.TemporalPrecedes {
		t.Errorf("IdentityKey 유실: %+v", h1.IdentityKey)
	}
	if len(h1.PredictedSignals) != 2 || h1.PredictedSignals[0].PredID != "H1-P1" {
		t.Errorf("PredictedSignals = %+v", h1.PredictedSignals)
	}
	if len(h1.SupportEIDs) != 1 || h1.SupportEIDs[0] != genEID("db-1") {
		t.Errorf("SupportEIDs = %v", h1.SupportEIDs)
	}
	// 등록 보고서가 감사 가능한 구조로 남는다(3b의 R 심사·재요구 입력).
	if len(rep.Admitted) != 2 || len(rep.PredTags) == 0 {
		t.Errorf("등록 보고서 = admitted %d·딱지 %d", len(rep.Admitted), len(rep.PredTags))
	}
}

// 상위 K 탈락도 감사 기록으로 남는다.
func TestGenerateTopK(t *testing.T) {
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		return []Candidate{
			cand("a", ledger.SourceMember, ledger.PriorLow),
			cand("b", ledger.SourceMember, ledger.PriorHigh),
		}, nil
	})
	g := grouperFunc(func(c []Candidate) ([][]int, error) {
		return [][]int{{0}, {1}}, nil
	})
	l := ledger.New()
	opened, _, err := Generate(context.Background(), genInput(t, "a", "b"), []CandidateSource{src}, g, l, GenerateConfig{TopK: 1, Auditor: passAuditor{}})
	if err != nil {
		t.Fatal(err)
	}
	if len(opened) != 1 || opened[0].TargetID != "b" { // high가 남는다
		t.Fatalf("opened = %+v, want b 1개", opened)
	}
	var rejects int
	for _, ev := range l.Events() {
		if ev.Type == ledger.EvHypothesisRejectedAtCreation {
			rejects++
		}
	}
	if rejects != 1 {
		t.Errorf("K 탈락 감사 기록 = %d, want 1", rejects)
	}
}

// 그룹핑이 완전 분할이 아니면(누락·중복) 오류다 — spike ⑤ 기준.
func TestGenerateBadPartition(t *testing.T) {
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		return []Candidate{
			cand("a", ledger.SourceMember, ledger.PriorLow),
			cand("b", ledger.SourceMember, ledger.PriorLow),
		}, nil
	})
	for _, groups := range [][][]int{
		{{0}},       // 누락
		{{0, 0, 1}}, // 중복
		{{0, 2}},    // 범위 밖
	} {
		g := grouperFunc(func([]Candidate) ([][]int, error) { return groups, nil })
		if _, _, err := Generate(context.Background(), genInput(t, "a", "b"), []CandidateSource{src}, g, ledger.New(), genCfg()); err == nil {
			t.Errorf("분할 %v 수락함", groups)
		}
	}
}

// 후보가 하나면 LLM 그룹핑을 부르지 않는다.
func TestGenerateSingleSkipsGrouper(t *testing.T) {
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		return []Candidate{cand("a", ledger.SourceMember, ledger.PriorLow)}, nil
	})
	g := grouperFunc(func([]Candidate) ([][]int, error) {
		t.Fatal("후보 1개인데 Grouper 호출됨")
		return nil, nil
	})
	opened, _, err := Generate(context.Background(), genInput(t, "a"), []CandidateSource{src}, g, ledger.New(), genCfg())
	if err != nil || len(opened) != 1 {
		t.Fatalf("opened = %+v, err = %v", opened, err)
	}
}

// 출처 오류는 전파된다. 후보 0개는 **더 이상 성립이 아니다** — §6.2-6이
// "유효 가설 0개 → 재생성 1회 → run 실패"로 계약을 바꿨다(3b. 종전 시험은
// "개설 0개로 성립한다"를 단언했는데, 가설 없이 루프는 정의되지 않는다).
func TestGenerateSourceErrorAndEmpty(t *testing.T) {
	boom := errors.New("LLM 죽음")
	bad := sourceFunc(func(GenerateInput) ([]Candidate, error) { return nil, boom })
	g := grouperFunc(func([]Candidate) ([][]int, error) { return nil, nil })
	in := genInput(t, "a")
	if _, _, err := Generate(context.Background(), in, []CandidateSource{bad}, g, ledger.New(), genCfg()); !errors.Is(err, boom) {
		t.Fatalf("출처 오류 전파 안 됨: %v", err)
	}
	opened, _, err := Generate(context.Background(), in, nil, g, ledger.New(), genCfg())
	var fail *AdmissionFailureError
	if !errors.As(err, &fail) || len(opened) != 0 {
		t.Fatalf("빈 출처: opened = %+v, err = %v (admission failure 기대)", opened, err)
	}
	if fail.Reason() != ledger.ReasonHypothesisAdmissionFailure {
		t.Errorf("실패 enum = %s", fail.Reason())
	}
}

// ── 병합 규칙 (§6.2, 3b) ────────────────────────────────────────

// 병합은 술어를 명제 키 기준 dedup 합집합으로, 개설 근거를 합집합으로 잇고,
// **말단이 더 깊은 후보의 사슬**을 대표로 삼는다. 비대표 후보의 유실은
// 병합 감사(hypothesis_created.Merge)에 남는다(C-14).
func TestMergeGroupUnionsPredicatesAndKeepsDeeperChain(t *testing.T) {
	genInput(t, "db-1", "stor-1") // index·어휘 배선(cand가 EID를 여기서 얻는다)
	a := cand("db-1", ledger.SourceChange, ledger.PriorMedium)
	b := cand("db-1", ledger.SourceInvestigation, ledger.PriorHigh)
	// b는 사슬이 한 구간 더 깊고, 술어 하나가 a와 같은 명제 키다.
	b.Chain = append(b.Chain, ledger.ChainStep{Entity: "stor-1", Effect: "IO 지연"})
	b.PredictedSignals = append(b.PredictedSignals,
		admPred(func(p *ledger.SignalPred) { p.TargetID, p.Metric = "db-1", "disk.util" }))
	b.SupportEIDs = []string{genEID("db-1"), genEID("stor-1")}

	m := mergeGroup([]Candidate{a, b}, []int{0, 1})
	// ① 술어 dedup 합집합 — 같은 명제 키 2쌍은 접히고 새 키 하나만 붙는다.
	if len(m.PredictedSignals) != 3 {
		t.Fatalf("술어 = %d개, want 3(합집합·dedup)", len(m.PredictedSignals))
	}
	keys := map[evidence.ObservationKey]int{}
	for _, p := range m.PredictedSignals {
		keys[p.ObservationKey()]++
	}
	for k, n := range keys {
		if n > 1 {
			t.Errorf("명제 키 %s가 %d번 — dedup 실패", k, n)
		}
	}
	// ② 개설 근거 합집합.
	if len(m.SupportEIDs) != 2 {
		t.Errorf("SupportEIDs = %v, want 합집합 2건", m.SupportEIDs)
	}
	// ③ 더 깊은 사슬이 대표.
	if len(m.Chain) != 3 {
		t.Errorf("대표 사슬 깊이 = %d, want 3(더 깊은 쪽)", len(m.Chain))
	}
	// ④ 병합 감사 — 비대표 사슬의 구간 지목과 접힌 술어 수가 남는다.
	if m.mergeAudit == nil {
		t.Fatal("병합 감사가 없다")
	}
	if !m.mergeAudit.ChainReplaced || m.mergeAudit.DedupedPreds != 2 ||
		len(m.mergeAudit.DroppedChains) != 1 || len(m.mergeAudit.DroppedChains[0].ClaimIDs) != 2 {
		t.Errorf("병합 감사 = %+v", m.mergeAudit)
	}
	// 단일 후보 그룹은 유실이 없으므로 감사도 없다.
	if mergeGroup([]Candidate{a}, []int{0}).mergeAudit != nil {
		t.Error("단일 그룹에 병합 감사가 붙었다")
	}
}

// 병합 감사는 개설 이벤트에 실려 장부에 남는다(기존 감사 이벤트의 확장).
func TestMergeAuditIsRecordedOnCreation(t *testing.T) {
	in := genInput(t, "db-1")
	a := cand("db-1", ledger.SourceChange, ledger.PriorMedium)
	b := cand("db-1", ledger.SourceInvestigation, ledger.PriorHigh)
	b.Chain = append(b.Chain, ledger.ChainStep{Entity: "app-2", Effect: "타임아웃"})
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) { return []Candidate{a, b}, nil })
	g := grouperFunc(func([]Candidate) ([][]int, error) { return [][]int{{0, 1}}, nil })
	l := ledger.New()
	opened, _, err := Generate(context.Background(), in, []CandidateSource{src}, g, l, genCfg())
	if err != nil || len(opened) != 1 {
		t.Fatalf("opened=%d err=%v", len(opened), err)
	}
	if opened[0].Merge == nil || opened[0].Merge.MergedCount != 2 {
		t.Fatalf("개설 엔트리의 병합 감사 = %+v", opened[0].Merge)
	}
	// 장부 재생으로도 살아남는다.
	rl, err := ledger.Replay(l.Events())
	if err != nil {
		t.Fatal(err)
	}
	h, _ := rl.Hypothesis(opened[0].ID)
	if h.Entry.Merge == nil || len(h.Entry.Merge.DroppedChains) != 1 {
		t.Errorf("재생 후 병합 감사 = %+v", h.Entry.Merge)
	}
}

// ── 다양성 재요구 1회 (§6.2 소프트 규칙, §14-4 4c) ───────────────

// afford는 §7.6 잔여 검사 주입 자리의 대역이다.
func afford(ok bool) func() bool { return func() bool { return ok } }

// 재요구는 조건부 지출이라 잔여 검사를 먼저 받는다(§7.6). 미충족이면
// **시도조차 하지 않고** 그 라운드를 그대로 개설한다 — 라이브 회귀
// (2026-08-04: 재요구 지출로 누적 317k > 상한 200k, 바퀴 0·probe 0)의 봉쇄다.
func TestDiversityRerequestSkippedWhenUnaffordable(t *testing.T) {
	for _, tc := range []struct {
		name   string
		afford func() bool
	}{
		{"잔여 부족", afford(false)},
		{"검사 미배선(fail-closed)", nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			rounds := 0
			src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
				rounds++
				return []Candidate{
					cand("a", ledger.SourceMember, ledger.PriorHigh),
					cand("a", ledger.SourceChange, ledger.PriorLow),
				}, nil
			})
			g := grouperFunc(func(c []Candidate) ([][]int, error) { return [][]int{{0}, {1}}, nil })
			opened, rep, err := Generate(context.Background(), genInput(t, "a"), []CandidateSource{src}, g,
				ledger.New(), GenerateConfig{Auditor: passAuditor{}, AffordDiversityRerequest: tc.afford})
			if err != nil {
				t.Fatalf("Generate: %v", err)
			}
			if rounds != 1 || len(opened) != 2 {
				t.Fatalf("rounds=%d opened=%d — 재요구가 돌았다", rounds, len(opened))
			}
			// 못 한 것과 안 걸린 것은 다른 사실이다.
			if !hasTag(rep.RunTags, TagDiversityUnmet) ||
				!hasTag(rep.RunTags, TagDiversityRerequestUnaffordable) ||
				hasTag(rep.RunTags, TagDiversityRerequested) {
				t.Errorf("run 딱지 = %v, want diversity_unmet + diversity_rerequest_unaffordable", rep.RunTags)
			}
		})
	}
}

// 원인 개체가 1종뿐이면 반려 사유 피드백과 **같은 통로**로 재요구 1회를
// 돌린다. 그래도 미충족이면 diversity_unmet을 단 채 진행한다(반려 아님).
func TestDiversityRerequestOnceThenProceed(t *testing.T) {
	rounds := 0
	var feedback []string
	src := sourceFunc(func(in GenerateInput) ([]Candidate, error) {
		rounds++
		feedback = in.Feedback
		// 두 라운드 모두 같은 원인 개체(a) — 재요구해도 미충족이다.
		return []Candidate{
			cand("a", ledger.SourceMember, ledger.PriorHigh),
			cand("a", ledger.SourceChange, ledger.PriorLow),
		}, nil
	})
	g := grouperFunc(func(c []Candidate) ([][]int, error) { return [][]int{{0}, {1}}, nil })
	l := ledger.New()
	opened, rep, err := Generate(context.Background(), genInput(t, "a"), []CandidateSource{src}, g, l,
		GenerateConfig{Auditor: passAuditor{}, AffordDiversityRerequest: afford(true)})
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	if rounds != 2 {
		t.Fatalf("생성 라운드 = %d, want 2 (재요구 1회)", rounds)
	}
	// 재요구는 같은 통로다 — 2라운드 입력에 지침이 실린다.
	var guided bool
	for _, f := range feedback {
		if strings.Contains(f, "cause_entity") {
			guided = true
		}
	}
	if !guided {
		t.Errorf("재요구 지침이 피드백에 없다: %v", feedback)
	}
	// 그래도 미충족 → 기록 후 진행. 개설은 2라운드 것 하나뿐이다(중복 개설 금지).
	if !hasTag(rep.RunTags, TagDiversityUnmet) || !hasTag(rep.RunTags, TagDiversityRerequested) {
		t.Errorf("run 딱지 = %v, want diversity_unmet + diversity_rerequested", rep.RunTags)
	}
	if len(opened) != 2 {
		t.Fatalf("opened = %d, want 2", len(opened))
	}
	var created int
	for _, ev := range l.Events() {
		if ev.Type == ledger.EvHypothesisCreated {
			created++
		}
	}
	if created != 2 {
		t.Errorf("hypothesis_created = %d건, want 2 — 재요구 라운드가 가설을 두 번 열었다", created)
	}
}

// 재요구가 먹히면 그대로 개설한다 — 딱지는 재요구 사실만 남는다.
func TestDiversityRerequestSatisfied(t *testing.T) {
	rounds := 0
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		rounds++
		if rounds == 1 {
			return []Candidate{
				cand("a", ledger.SourceMember, ledger.PriorHigh),
				cand("a", ledger.SourceChange, ledger.PriorLow),
			}, nil
		}
		return []Candidate{
			cand("a", ledger.SourceMember, ledger.PriorHigh),
			cand("b", ledger.SourceChange, ledger.PriorLow),
		}, nil
	})
	g := grouperFunc(func(c []Candidate) ([][]int, error) { return [][]int{{0}, {1}}, nil })
	opened, rep, err := Generate(context.Background(), genInput(t, "a", "b"), []CandidateSource{src}, g,
		ledger.New(), GenerateConfig{Auditor: passAuditor{}, AffordDiversityRerequest: afford(true)})
	if err != nil {
		t.Fatalf("Generate: %v", err)
	}
	if rounds != 2 || len(opened) != 2 {
		t.Fatalf("rounds=%d opened=%d", rounds, len(opened))
	}
	if hasTag(rep.RunTags, TagDiversityUnmet) || !hasTag(rep.RunTags, TagDiversityRerequested) {
		t.Errorf("run 딱지 = %v, want diversity_rerequested만", rep.RunTags)
	}
}

// 재생성(§7.5)은 Generate를 새로 호출한다 — 가설 번호는 호출 경계를 넘어
// 이어져야 한다. 5b 라이브 실측: 0 재시작이 기존 가설과 "가설 H2 중복"으로
// 장부 검증에 걸려 run을 죽였다(fe8dd38b, 2026-08-10).
func TestGenerateHypoSeqContinuesAcrossCalls(t *testing.T) {
	src := sourceFunc(func(GenerateInput) ([]Candidate, error) {
		return []Candidate{
			cand("a", ledger.SourceMember, ledger.PriorHigh),
			cand("b", ledger.SourceChange, ledger.PriorLow),
		}, nil
	})
	g := grouperFunc(func(c []Candidate) ([][]int, error) { return [][]int{{0}, {1}}, nil })
	l := ledger.New()
	cfg := GenerateConfig{Auditor: passAuditor{}}
	first, _, err := Generate(context.Background(), genInput(t, "a", "b"), []CandidateSource{src}, g, l, cfg)
	if err != nil {
		t.Fatalf("1차 Generate: %v", err)
	}
	if len(first) != 2 || first[0].ID != "H1" || first[1].ID != "H2" {
		t.Fatalf("1차 opened = %+v", first)
	}
	second, _, err := Generate(context.Background(), genInput(t, "a", "b"), []CandidateSource{src}, g, l, cfg)
	if err != nil {
		t.Fatalf("2차 Generate(재생성 경로): %v", err)
	}
	if len(second) != 2 || second[0].ID != "H3" || second[1].ID != "H4" {
		t.Fatalf("2차 opened = %+v — 번호가 이어지지 않았다", second)
	}
}
