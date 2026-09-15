// 실 스모크 — 메타 도구 2종 (CH+PG+VM 전부 필요).
package tools

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func TestMetaToolsSmoke(t *testing.T) {
	dsn, vmURL, chURL := os.Getenv("RCA_PG_DSN"), os.Getenv("RCA_VM_URL"), os.Getenv("RCA_CH_URL")
	if dsn == "" || vmURL == "" || chURL == "" {
		t.Skip("RCA_PG_DSN·RCA_VM_URL·RCA_CH_URL 미설정 — 생략")
	}
	pg, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()
	vm := &VM{BaseURL: vmURL}
	ch := chFromEnv(t)

	var host string
	if err := pg.QueryRow(`SELECT id::text FROM targets WHERE type='server' LIMIT 1`).Scan(&host); err != nil {
		t.Fatalf("server 대상: %v", err)
	}
	now := time.Now().UTC()
	from, to := now.Add(-15*time.Minute).Format(time.RFC3339), now.Format(time.RFC3339)

	// (get_runtime_connections 스모크 제거 — 소켓 원천은 앱 프로세스
	// 0건이라 '계측 밖 의존 후보'를 낼 수 없었고, 그 몫은
	// expand_topology의 apm_client_peer가 승계했다, §12.4)

	// data_coverage: 수집기 있는 대상 + 없는 대상(가짜 UUID는 인자
	// 검증에 걸리므로 등록 대상 중 수집기 없는 것을 찾되, 없으면 정상
	// 대상 2개로 대조 자체를 검증).
	cov := NewDataCoverageTool(pg, ch, vm)
	var second string
	_ = pg.QueryRow(`SELECT t.id::text FROM targets t
		LEFT JOIN collectors c ON c.target_id = t.id
		WHERE c.id IS NULL LIMIT 1`).Scan(&second)
	targets := []string{host}
	if second != "" {
		targets = append(targets, second)
	}
	args, _ := json.Marshal(map[string]any{"targets": targets, "from": from, "to": to})
	out, err := cov.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if len(env.Findings) != len(targets) {
		t.Fatalf("대상 수 불일치: %+v", env)
	}
	f0 := env.Findings[0]
	if _, ok := f0["window_observations"]; !ok {
		t.Fatalf("관측 대조 없음: %+v", f0)
	}
	note, _ := f0["coverage_note"].(string)
	if note == "" {
		t.Fatalf("coverage_note 없음: %+v", f0)
	}
	if second != "" {
		n2, _ := env.Findings[1]["coverage_note"].(string)
		if !strings.Contains(n2, "not_collected") {
			t.Fatalf("수집기 없는 대상의 note 이상: %s", n2)
		}
	}
	t.Logf("coverage: %s / note[0]=%s", env.Summary, note)
}
