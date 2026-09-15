package llm

import (
	"context"
	"fmt"
	"sort"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// 스모크 — spike 5-2의 함정 6후보 이식(문서 §5.5). 기대 분할:
// {0,2} {1,5} {3} {4}.
func TestGrouperSmoke(t *testing.T) {
	g := &Grouper{Client: smokeClient(t)}
	cs := func(steps ...[2]string) []ledger.ChainStep {
		out := make([]ledger.ChainStep, len(steps))
		for i, s := range steps {
			out[i] = ledger.ChainStep{Entity: s[0], Effect: s[1]}
		}
		return out
	}
	cands := []pipeline.Candidate{
		{TargetID: "payment-db-01", Source: ledger.SourceChange,
			Chain: cs([2]string{"payment-db-01", "max_connections 200→50 설정 변경으로 커넥션 풀 고갈"},
				[2]string{"order-api-01", "커넥션 대기에 걸려 주문 처리 지연"})},
		{TargetID: "order-api-01", Source: ledger.SourceMember,
			Chain: cs([2]string{"order-api-01", "인스턴스 CPU 급증"},
				[2]string{"order-api-01", "요청 처리 지연"})},
		{TargetID: "payment-db-01", Source: ledger.SourceInvestigation,
			Chain: cs([2]string{"payment-db-01", "커넥션 사용률 100% 포화로 대기 세션 누적"},
				[2]string{"order-api-01", "상류 API 응답 지연"})},
		{TargetID: "payment-db-01", Source: ledger.SourceInvestigation,
			Chain: cs([2]string{"payment-db-01", "특정 SQL 풀스캔으로 쿼리 응답 지연"},
				[2]string{"order-api-01", "주문 API 응답 지연"})},
		{TargetID: "network-sw-3", Source: ledger.SourcePastCase,
			Chain: cs([2]string{"network-sw-3", "스위치 포트 에러로 패킷 손실"},
				[2]string{"order-api-01", "구간 지연으로 응답 지연"})},
		{TargetID: "order-api-01", Source: ledger.SourceMember,
			Chain: cs([2]string{"order-api-01", "CPU 포화로 워커 스레드 스케줄링 실패"},
				[2]string{"order-api-01", "처리량 저하"})},
	}

	groups, err := g.Group(context.Background(), cands)
	if err != nil {
		t.Fatalf("Group: %v", err)
	}
	got := map[string]bool{}
	for _, grp := range groups {
		sort.Ints(grp)
		got[fmt.Sprint(grp)] = true
	}
	for _, want := range []string{"[0 2]", "[1 5]", "[3]", "[4]"} {
		if !got[want] {
			t.Errorf("기대 묶음 %s 없음 — got %v", want, groups)
		}
	}
	if len(groups) != 4 {
		t.Errorf("묶음 수 %d, want 4 — got %v", len(groups), groups)
	}
}
