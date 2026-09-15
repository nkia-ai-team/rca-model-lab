// 실 스모크 — processes(VM)·snmp_traps(CH). traps는 테스트베드에
// 아직 0건이라 '미발생=normal' 경로를 검증한다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func TestServerNetworkSmoke(t *testing.T) {
	vmURL, dsn := os.Getenv("RCA_VM_URL"), os.Getenv("RCA_PG_DSN")
	if vmURL == "" || dsn == "" {
		t.Skip("RCA_VM_URL·RCA_PG_DSN 미설정 — 생략")
	}
	vm := &VM{BaseURL: vmURL}
	var pg *sql.DB
	pg, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()

	var host string
	if err := pg.QueryRow(`SELECT id::text FROM targets WHERE type='server' LIMIT 1`).Scan(&host); err != nil {
		t.Fatalf("server 대상: %v", err)
	}
	now := time.Now().UTC()
	from, to := now.Add(-15*time.Minute).Format(time.RFC3339), now.Format(time.RFC3339)

	proc := NewProcessesTool(vm)
	args, _ := json.Marshal(map[string]any{"host": host, "from": from, "to": to, "sort_by": "cpu", "top_n": 3})
	out, err := proc.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if env.Status == "no_data" {
		t.Fatalf("SMS server인데 프로세스 no_data: %+v", env)
	}
	if env.Findings[0]["process"] == "" || env.Findings[0]["pid"] == "" {
		t.Fatalf("프로세스 차원 없음: %+v", env.Findings[0])
	}
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화: %v", err)
	}
	t.Logf("processes: status=%s %s", env.Status, env.Summary)

	// sort_by 오류 → 유효 값 목록.
	bad, _ := json.Marshal(map[string]string{"host": host, "from": from, "to": to, "sort_by": "disk"})
	if _, err := proc.Call(context.Background(), bad); err == nil || !strings.Contains(err.Error(), "cpu") {
		t.Fatalf("sort_by 오류에 유효 값 없음: %v", err)
	}

	if chURL := os.Getenv("RCA_CH_URL"); chURL != "" {
		ch := chFromEnv(t)
		traps := NewSnmpTrapsTool(ch)
		args, _ = json.Marshal(map[string]string{"device": host, "from": from, "to": to})
		out, err = traps.Call(context.Background(), args)
		if err != nil {
			t.Fatal(err)
		}
		env = out.(Envelope)
		if env.Status != "anomalous" && !strings.Contains(env.Summary, "trap 없음") {
			t.Fatalf("traps 봉투 이상: %+v", env)
		}
		t.Logf("traps: status=%s %s", env.Status, env.Summary)
	}
}
