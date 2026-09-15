// read_timeseries 실 VM 스모크 — RCA_VM_URL 없으면 skip.
// 실행: RCA_VM_URL='http://192.168.230.119:18428' go test ./tools -run TestReadTimeseriesSmoke -v
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

func TestReadTimeseriesSmoke(t *testing.T) {
	vm := vmFromEnv(t)
	now := time.Now().UTC().Truncate(time.Minute)
	firstEvent := now.Add(-15 * time.Minute)

	targets, err := vm.LabelValues(context.Background(), "target_id", `{target_id!=""}`, firstEvent, now)
	if err != nil || len(targets) == 0 {
		t.Fatalf("실 대상 발굴: %v (%d개)", err, len(targets))
	}
	var db *sql.DB
	if dsn := os.Getenv("RCA_PG_DSN"); dsn != "" {
		if db, err = OpenPG(dsn); err != nil {
			t.Fatal(err)
		}
		defer db.Close()
	}
	// scan으로 지표 이름 발굴(깔때기 그대로) → read로 정독.
	scan := NewScanMetricsTool(vm, db, firstEvent)
	args, _ := json.Marshal(map[string]string{"target": targets[0],
		"from": firstEvent.Format(time.RFC3339), "to": now.Format(time.RFC3339)})
	out, err := scan.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	metric := ""
	for _, f := range out.(Envelope).Findings {
		// disappeared는 현재 창에 없는 지표라 read 대상이 아님.
		if c, _ := f["class"].(string); c != "shifted" && c != "appeared" {
			continue
		}
		if m, _ := f["metric"].(string); m != "" {
			metric = m
			break
		}
	}
	if metric == "" {
		// 조용한 라이브 — 창 내 실제 방출 중인 지표로 대체. raw rollup만
		// __name__을 유지한다(집계는 count by(__name__)조차 이름을 안
		// 돌려줌 — §5.6.1 함정의 변종, 이 스모크에서 실측).
		raw, err := vm.RangeQuery(context.Background(), `last_over_time({target_id="`+targets[0]+`"}[5m])`,
			now.Add(-5*time.Minute), now, 5*time.Minute)
		if err != nil || len(raw) == 0 {
			t.Skipf("현재 창 지표 없음: %v", err)
		}
		for _, s := range raw {
			n := s.Labels["__name__"]
			if n != "" && !strings.Contains(n, ".bands") && !strings.HasSuffix(n, ".raw") {
				metric = n
				break
			}
		}
	}

	t.Logf("선택 지표: %q (target %s)", metric, targets[0])
	read := NewReadTimeseriesTool(vm, db, firstEvent, nil)
	args, _ = json.Marshal(map[string]any{
		"targets": []string{targets[0]}, "metrics": []string{metric},
		"from": "now-15m", "to": "now"}) // 현재 창 상대 표기 경로
	out, err = read.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화 실패: %v", err)
	}
	if len(env.Findings) == 0 || len(env.Refs) == 0 {
		t.Fatalf("봉투 이상: %+v", env)
	}
	f := env.Findings[0]
	if _, ok := f["values"]; !ok {
		if note, _ := f["note"].(string); !strings.Contains(note, "관측") {
			t.Fatalf("버킷 배열 없음: %+v", f)
		}
	}
	t.Logf("read(%s/%s): status=%s %s", targets[0][:8], metric, env.Status, env.Summary)

	// 조합 상한 오류 경로 — 복구 안내 포함.
	big := make([]string, 9)
	for i := range big {
		big[i] = metric
	}
	args, _ = json.Marshal(map[string]any{
		"targets": []string{targets[0]}, "metrics": big,
		"from": "now-15m", "to": "now"})
	if _, err = read.Call(context.Background(), args); err == nil || !strings.Contains(err.Error(), "쪼개서") {
		t.Fatalf("조합 상한 오류에 복구 안내 없음: %v", err)
	}
}
