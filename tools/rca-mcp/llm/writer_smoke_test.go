package llm

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/seed"
)

// Writer 스모크 — 문서 §7.5. 케이스는 실 인시던트 run(food-delivery
// c098ddb6, 2026-07-20)의 result.json을 압축한 것: provisional의 추정
// 어투와 insufficient의 미확정 명시를 본다. 어투는 기계 단정이 어려워
// 핵심 신호만 검사하고 전문은 -v 로그로 남긴다(눈 점검).
func TestWriterSmoke(t *testing.T) {
	w := &Writer{Client: smokeClient(t)}

	// 실 run처럼 리포트 구조는 UUID로 말한다 — 표시명 표 치환을 본다.
	host := "7946cf87-60e2-47de-a7be-89b1bd0977ad"
	provisional := pipeline.WriteInput{
		Title: "[food-delivery] payment 에러율 급증",
		Targets: map[string]string{
			host: "호스트 192.168.200.137",
		},
		Rca: ledger.RcaResult{
			Status: "provisional",
			Symptom: ledger.RcaSymptom{Metric: "error_rate", Text: ""},
			Problem: ledger.RcaProblem{Severity: "critical"},
			Cause: ledger.RcaCause{
				Text:     host + ": 패킷 손실·디스크 I/O 급감 → dispatch: kafka producer I/O wait 증가 → payment: 에러율 급증",
				TargetID: host, TargetName: "호스트 192.168.200.137", Confidence: 0.5,
			},
			CausalChain: []ledger.RcaChainStep{
				{Entity: host, Note: "패킷 손실·디스크 I/O 급감 — 근거: sms.disk.read_counts 평균 0 (기준선 대비 -100%)"},
				{Entity: "dispatch", Note: "kafka producer I/O wait 증가"},
				{Entity: "payment", Note: "에러율 급증"},
			},
			Evidence: []ledger.RcaEvidence{
				{Ref: "vm:sms.disk.read_counts", Label: "sms.disk.read_counts 평균 0 — 기준선 0.008876 대비 -100%"},
			},
			Alternatives: []ledger.RcaAlternative{
				{Text: "dispatch: JVM heap 객체 해제 실패 → GC 빈도 증가 → 응답 지연", Confidence: 0},
			},
			MissingEvidence: []string{"조회 불가: jvm.memory.used_after_last_gc (no_data)"},
		},
		UI: ledger.UIReport{
			Diagnosis: ledger.UIDiagnosis{Verdict: "INCONCLUSIVE", Confidence: 0.5},
			DataCoverage: ledger.UIDataCoverage{
				RuledOut: []string{"sms.disk.busy_time 기준선 대비 -6% — 정상 범위"},
			},
		},
	}

	insufficient := pipeline.WriteInput{
		Title: "[core-banking] jvm.memory.used 이상",
		Rca: ledger.RcaResult{
			Status:  "insufficient",
			Symptom: ledger.RcaSymptom{Metric: "jvm.memory.used"},
			Problem: ledger.RcaProblem{Severity: "warning"},
			Alternatives: []ledger.RcaAlternative{
				{Text: "commerce-pg: 커넥션 풀 포화 → 대기 증가", Confidence: 0},
			},
			MissingEvidence: []string{"조회 불가: 트레이스 (TTL 초과)"},
		},
		UI: ledger.UIReport{
			Diagnosis: ledger.UIDiagnosis{Verdict: "INCONCLUSIVE"},
			DataCoverage: ledger.UIDataCoverage{
				Gaps: []string{"트레이스 데이터 없음 (보존 기간 초과)"},
			},
		},
	}

	cases := []struct {
		name string
		in   pipeline.WriteInput
		// headline·결론에 나타나면 안 되는 단정 신호 (status 어투 검사의
		// 기계 검사 가능한 최소치)
		forbidHeadline []string
	}{
		{"provisional", provisional, []string{"확정", "결론적으로 확인"}},
		{"insufficient", insufficient, nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			p, err := w.Write(context.Background(), c.in)
			if err != nil {
				t.Fatalf("Write 실패: %v", err)
			}
			t.Logf("headline: %s", p.Headline)
			t.Logf("symptom: %s", p.SymptomText)
			t.Logf("problem: %s", p.ProblemText)
			t.Logf("diagnosis: %s", p.DiagnosisSummary)
			t.Logf("conclusion: %s", p.ConclusionSummary)
			for _, bad := range c.forbidHeadline {
				if strings.Contains(p.Headline, bad) {
					t.Errorf("headline에 단정 어휘 %q — status=%s와 부정합", bad, c.in.Rca.Status)
				}
			}
			// 표시명 표에 있는 UUID가 산문에 남으면 치환 실패.
			all := p.Headline + p.SymptomText + p.ProblemText + p.DiagnosisSummary + p.ConclusionSummary
			for id := range c.in.Targets {
				if strings.Contains(all, id[:8]) {
					t.Errorf("산문에 UUID %s… 잔존 — 표시명 치환 안 됨", id[:8])
				}
			}
			if c.name == "insufficient" && !strings.Contains(p.DiagnosisSummary+p.ConclusionSummary, "확") {
				// "미확정"·"확인되지 않" 류의 명시가 있어야 한다 — 약한 검사.
				t.Errorf("insufficient인데 미확정 언급이 안 보임: %s / %s", p.DiagnosisSummary, p.ConclusionSummary)
			}
		})
	}
}

// 실 run 산출물 전문으로 작문 — RCA_WRITER_RUN=<run 디렉터리> 지정 시.
// 압축 픽스처가 아닌 실물 크기 입력의 거동 확인용(문서 §7.5).
func TestWriterSmokeRealRun(t *testing.T) {
	w := &Writer{Client: smokeClient(t)}
	dir := os.Getenv("RCA_WRITER_RUN")
	if dir == "" {
		t.Skip("RCA_WRITER_RUN 미설정 — 실 run 작문 스모크 skip")
	}

	var res struct {
		Rca ledger.RcaResult `json:"rca"`
		UI  ledger.UIReport  `json:"ui"`
	}
	b, err := os.ReadFile(filepath.Join(dir, "result.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &res); err != nil {
		t.Fatal(err)
	}
	var s seed.IncidentSeed
	if b, err = os.ReadFile(filepath.Join(dir, "seed.json")); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(b, &s); err != nil {
		t.Fatal(err)
	}

	p, err := w.Write(context.Background(), pipeline.WriteInput{Title: s.Title, Rca: res.Rca, UI: res.UI})
	if err != nil {
		t.Fatalf("Write 실패: %v", err)
	}
	t.Logf("headline: %s", p.Headline)
	t.Logf("symptom: %s", p.SymptomText)
	t.Logf("problem: %s", p.ProblemText)
	t.Logf("diagnosis: %s", p.DiagnosisSummary)
	t.Logf("conclusion: %s", p.ConclusionSummary)
}
