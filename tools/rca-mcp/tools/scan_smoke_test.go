// scan_metrics 실 VM 스모크 — RCA_VM_URL 없으면 skip. PG(RCA_PG_DSN)가
// 있으면 counter 판별·단위 결합까지.
// 실행: RCA_VM_URL='http://192.168.230.119:18428' go test ./tools -run ScanSmoke -v
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestScanMetricsSmoke(t *testing.T) {
	vm := vmFromEnv(t)
	now := time.Now().UTC().Truncate(time.Minute)
	// 라이브 세계엔 인시던트가 없으니 first_event를 15분 전으로 가장 —
	// 현재 창 [now-15m, now), 기준선 자동 = [now-75m, now-15m).
	firstEvent := now.Add(-15 * time.Minute)

	targets, err := vm.LabelValues(context.Background(), "target_id", `{target_id!=""}`, firstEvent, now)
	if err != nil || len(targets) == 0 {
		t.Fatalf("실 대상 발굴: %v (%d개)", err, len(targets))
	}
	var db *sql.DB
	if dsn := os.Getenv("RCA_PG_DSN"); dsn != "" {
		db, err = OpenPG(dsn)
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
	}

	scan := NewScanMetricsTool(vm, db, firstEvent)
	args, _ := json.Marshal(map[string]string{
		"target": targets[0],
		"from":   firstEvent.Format(time.RFC3339), "to": now.Format(time.RFC3339)})
	out, err := scan.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if env.Status == "no_data" {
		t.Fatalf("방출 중 대상이 no_data: %+v", env)
	}
	counts := env.Findings[len(env.Findings)-1]
	if counts["class"] != "counts" {
		t.Fatalf("counts finding 없음: %+v", counts)
	}
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화 실패: %v", err)
	}
	if len(env.Refs) == 0 {
		t.Fatal("refs 없음")
	}
	t.Logf("scan(%s): status=%s %s", targets[0], env.Status, env.Summary)
	t.Logf("counts: %+v", counts)

	// baseline override 경로.
	args, _ = json.Marshal(map[string]string{
		"target": targets[0],
		"from":   firstEvent.Format(time.RFC3339), "to": now.Format(time.RFC3339),
		"baseline_from": now.Add(-75 * time.Minute).Format(time.RFC3339),
		"baseline_to":   now.Add(-30 * time.Minute).Format(time.RFC3339)})
	if _, err := scan.Call(context.Background(), args); err != nil {
		t.Fatalf("baseline override 실패: %v", err)
	}

	// 존재하지 않는 대상 → no_data + coverage 안내.
	args, _ = json.Marshal(map[string]string{
		"target": "00000000-0000-0000-0000-000000000000",
		"from":   firstEvent.Format(time.RFC3339), "to": now.Format(time.RFC3339)})
	out, err = scan.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	if env := out.(Envelope); env.Status != "no_data" {
		t.Fatalf("없는 대상 봉투 이상: %+v", env)
	}
}
