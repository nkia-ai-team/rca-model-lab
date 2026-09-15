package llm

import (
	"context"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// 스모크 — spike 5-3의 3시나리오 이식(문서 §4.5). 생성 계열은 temp 0.2.
func candClient(t *testing.T) *Client {
	c := smokeClient(t)
	c.Temperature = 0.2
	return c
}

// toolClient — 도구 루프 스모크용. 최종 정리 JSON이 길어(6대상 발견)
// Verifier용 1200 토큰으로는 잘린다.
func toolClient(t *testing.T) *Client {
	c := candClient(t)
	c.MaxTokens = 4000
	return c
}

func f64(v float64) *float64 { return &v }

func seedATriage(t *testing.T) pipeline.TriageResult {
	t.Helper()
	from, _ := time.Parse(time.RFC3339, "2026-07-09T05:40:00Z")
	to, _ := time.Parse(time.RFC3339, "2026-07-09T05:55:00Z")
	return pipeline.TriageResult{
		Symptom: pipeline.Symptom{TargetID: "svc:commerce-payment",
			Metric: "http.server.duration.p95", Observed: f64(2400), Baseline: f64(180),
			Direction: "up", Severity: "critical"},
		From: from, To: to,
		MemberGroups: []pipeline.MemberGroup{
			{TargetID: "svc:commerce-payment", Metric: "http.server.duration.p95",
				Count: 1, FirstAt: from, LastAt: to, MaxSeverity: "critical", HasAnomaly: true},
			{TargetID: "svc:commerce-order", Metric: "http.server.duration.p95",
				Count: 1, FirstAt: from, LastAt: to, MaxSeverity: "warning", HasAnomaly: true},
		},
		Candidates: []pipeline.CandidateTarget{
			{TargetID: "svc:core-banking-transfer", Via: pipeline.ViaTopology1Hop},
			{TargetID: "db:commerce-pg-01", Via: pipeline.ViaTopology1Hop},
			{TargetID: "mock:external-pg", Via: pipeline.ViaTopology1Hop},
			{TargetID: "db:banking-mariadb-01", Via: pipeline.ViaTopology2Hop},
		},
	}
}

// lintCandidates — 깔때기 스키마 검증 거울 + 신 계약(§6) 서식 검사 +
// 끝 구간=증상. 종전 "반증 조건 평서형" lint는 RefutationCondition 폐기로
// 사라졌고(§14-3), 그 자리를 술어 서식·반증형(necessary) 요건이 대신한다.
func lintCandidates(t *testing.T, cands []pipeline.Candidate, symptomTarget string) {
	t.Helper()
	for i, c := range cands {
		// probes 검사는 삭제됐다(§14-4 4c) — 후보 서식에서 probes가 빠졌고,
		// "무엇을 볼 것인가"의 자리는 아래 predicted_signals 검사가 진다.
		if c.TargetID == "" || len(c.Chain) == 0 {
			t.Errorf("[%d] 필수 필드 누락: %+v", i, c)
			continue
		}
		if c.Chain[0].Entity != c.TargetID {
			t.Errorf("[%d] 첫 구간 entity %q ≠ 의심 대상 %q", i, c.Chain[0].Entity, c.TargetID)
		}
		if last := c.Chain[len(c.Chain)-1].Entity; last != symptomTarget {
			t.Errorf("[%d] 끝 구간 %q ≠ 증상 %q", i, last, symptomTarget)
		}
		if c.IdentityKey.CauseEntity == "" || c.IdentityKey.Mechanism == "" ||
			!ledger.ValidTemporalClaim(c.IdentityKey.TemporalClaim) {
			t.Errorf("[%d] identity 미비: %+v", i, c.IdentityKey)
		}
		if len(c.PredictedSignals) < 2 {
			t.Errorf("[%d] 술어 %d개 — 2개 이상이어야", i, len(c.PredictedSignals))
		}
		var necessary int
		for _, p := range ledger.AssignPredIDs("Lint", c.PredictedSignals) {
			if err := p.ValidateForm(); err != nil {
				t.Errorf("[%d] 술어 서식 위반: %v", i, err)
			}
			if p.Role == ledger.RoleNecessary && p.EIDHint == "" {
				necessary++
			}
		}
		if necessary == 0 {
			t.Errorf("[%d] 미관측 necessary 술어 없음 — 죽지 않는 가설", i)
		}
		if len(c.SupportEIDs) == 0 {
			t.Errorf("[%d] support_eids 없음 — 개설 근거 부재", i)
		}
	}
}

// smokeVocab은 위상 어휘다(§6.2-1ⓐ) — 실 파이프라인에서는 [3]의 동결
// 조사 범위가 원천이고, 스모크에서는 triage 범위가 그 자리를 대신한다.
func smokeVocab(tri pipeline.TriageResult) *pipeline.TopologyVocab {
	ids := []string{tri.Symptom.TargetID}
	for _, g := range tri.MemberGroups {
		ids = append(ids, g.TargetID)
	}
	for _, c := range tri.Candidates {
		ids = append(ids, c.TargetID)
	}
	return pipeline.NewTopologyVocab(ids...)
}

func hasTarget(cands []pipeline.Candidate, id string) bool {
	for _, c := range cands {
		if c.TargetID == id {
			return true
		}
	}
	return false
}

func TestInvestigationSourceSmoke(t *testing.T) {
	src := NewInvestigationSource(candClient(t))
	// [3] 산출은 index다(스펙 §4) — 종전 Finding 4건과 같은 관측을 레코드로.
	ix := evidence.NewIndex()
	for _, r := range []evidence.EvidenceIndexRecord{
		smokeRecord("svc:core-banking-transfer", evidence.StatusAnomalous,
			"transfer API p95가 05:41부터 90ms→2100ms로 급증 (알람 없음)"),
		smokeRecord("db:banking-mariadb-01", evidence.StatusAnomalous,
			"lock wait 평균 0.01s→2.1s, blocking session 1건이 32세션 차단"),
		smokeRecord("db:commerce-pg-01", evidence.StatusNormal,
			"활성 세션·slow query·CPU 모두 기준선 범위"),
		smokeRecord("mock:external-pg", evidence.StatusNoData,
			"시간창 내 외부 PG 응답 지표 수집 안 됨"),
	} {
		if _, err := ix.Append(r); err != nil {
			t.Fatalf("index 적재: %v", err)
		}
	}
	in := pipeline.GenerateInput{Triage: seedATriage(t), Evidence: ix,
		Vocab: smokeVocab(seedATriage(t))}
	cands, err := src.Propose(context.Background(), in)
	if err != nil {
		t.Fatalf("Propose: %v", err)
	}
	lintCandidates(t, cands, "svc:commerce-payment")
	if !hasTarget(cands, "svc:core-banking-transfer") && !hasTarget(cands, "db:banking-mariadb-01") {
		t.Error("조용한 상류 후보 없음")
	}
	if hasTarget(cands, "db:commerce-pg-01") || hasTarget(cands, "mock:external-pg") {
		t.Error("normal/no_data 대상을 뿌리로 한 후보 존재")
	}
	for _, c := range cands {
		t.Logf("%s (prior=%d) %s", c.TargetID, c.Prior, c.IdentityKey.Mechanism)
	}
}

func TestChangeSourceSmoke(t *testing.T) {
	src := NewChangeSource(candClient(t))
	at1, _ := time.Parse(time.RFC3339, "2026-07-09T05:30:00Z")
	at2, _ := time.Parse(time.RFC3339, "2026-07-09T05:20:00Z")
	in := pipeline.GenerateInput{Triage: seedATriage(t), Vocab: smokeVocab(seedATriage(t)),
		Changes: pipeline.ChangeScanResult{
			Changes: []pipeline.ChangeEvent{
				{Kind: "audit", At: at1, TargetID: "db:banking-mariadb-01", Detail: "max_connections 200→50 적용"},
				{Kind: "audit", At: at2, TargetID: "host:node-77", Detail: "OS 보안 패치 적용 (증상 경로와 무관한 배치 서버)"},
			}}}
	in.Vocab.Extend("host:node-77")
	cands, err := src.Propose(context.Background(), in)
	if err != nil {
		t.Fatalf("Propose: %v", err)
	}
	lintCandidates(t, cands, "svc:commerce-payment")
	if !hasTarget(cands, "db:banking-mariadb-01") {
		t.Error("변경 대상(banking-mariadb-01) 후보 없음")
	}
	if hasTarget(cands, "host:node-77") {
		t.Error("무관 변경(node-77) 후보 존재")
	}
	for _, c := range cands {
		t.Logf("%s (prior=%d) %s", c.TargetID, c.Prior, c.IdentityKey.Mechanism)
	}
}

func TestMemberSourceSmoke_음성(t *testing.T) {
	src := NewMemberSource(candClient(t))
	from, _ := time.Parse(time.RFC3339, "2026-07-09T05:49:00Z")
	to, _ := time.Parse(time.RFC3339, "2026-07-09T05:55:00Z")
	in := pipeline.GenerateInput{Triage: pipeline.TriageResult{
		Symptom: pipeline.Symptom{TargetID: "host:node-09", Metric: "system.cpu.utilization",
			Observed: f64(72), Baseline: f64(55), Direction: "up", Severity: "warning"},
		From: from, To: to,
		MemberGroups: []pipeline.MemberGroup{
			{TargetID: "host:node-09", Metric: "system.cpu.utilization",
				Count: 1, FirstAt: from, LastAt: to, MaxSeverity: "warning", HasAnomaly: true}},
	}}
	cands, err := src.Propose(context.Background(), in)
	if err != nil {
		t.Fatalf("Propose: %v", err)
	}
	if len(cands) > 1 {
		t.Errorf("음성 seed에서 후보 %d개 — 과확신", len(cands))
	}
	for _, c := range cands {
		if c.Prior != ledger.PriorLow {
			t.Errorf("음성 seed 후보의 prior=%d — low여야", c.Prior)
		}
	}
	lintCandidates(t, cands, "host:node-09")
	t.Logf("후보 %d개", len(cands))
}
