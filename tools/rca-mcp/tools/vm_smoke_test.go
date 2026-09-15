// 실 VM 스모크 — RCA_VM_URL 없으면 skip. PG(RCA_PG_DSN)가 있으면
// discover_signals의 단위 결합까지 검증.
// 실행: RCA_VM_URL='http://192.168.230.119:18428' go test ./tools -run Smoke -v
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

func vmFromEnv(t *testing.T) *VM {
	raw := os.Getenv("RCA_VM_URL")
	if raw == "" {
		t.Skip("RCA_VM_URL 미설정 — 실 VM 스모크 생략")
	}
	return &VM{BaseURL: raw}
}

func TestMetricsSmoke(t *testing.T) {
	vm := vmFromEnv(t)
	now := time.Now().UTC().Truncate(time.Minute)
	from, to := now.Add(-15*time.Minute), now

	// 최근 15분에 지표가 있는 대상 하나 발굴.
	targets, err := vm.LabelValues(context.Background(), "target_id", `{target_id!=""}`, from, to)
	if err != nil || len(targets) == 0 {
		t.Fatalf("실 대상 발굴: %v (%d개)", err, len(targets))
	}
	target := targets[0]

	var db *sql.DB
	if dsn := os.Getenv("RCA_PG_DSN"); dsn != "" {
		db, err = OpenPG(dsn)
		if err != nil {
			t.Fatal(err)
		}
		defer db.Close()
	}

	// discover_signals: 인벤토리 + 이상 상위 N.
	disc := NewDiscoverSignalsTool(vm, db)
	args, _ := json.Marshal(map[string]any{
		"target": target, "from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339), "top_n": 5})
	out, err := disc.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if len(env.Findings) == 0 {
		t.Fatalf("discover 봉투 이상: %+v", env)
	}
	inv := env.Findings[len(env.Findings)-1]
	if _, ok := inv["inventory_total"]; !ok {
		t.Fatalf("인벤토리 finding 없음: %+v", inv)
	}
	metric, _ := env.Findings[0]["metric"].(string)
	if metric == "" {
		t.Fatalf("이상 상위 finding에 metric 없음: %+v", env.Findings[0])
	}
	t.Logf("discover: target=%s status=%s %s", target, env.Status, env.Summary)

	// get_metric_series: discover가 준 지표로 기준선 비교.
	series := NewMetricSeriesTool(vm)
	args, _ = json.Marshal(map[string]string{
		"target": target, "metric": metric,
		"from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339)})
	out, err = series.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env = out.(Envelope)
	if env.Status == "no_data" {
		t.Fatalf("discover가 준 지표가 series에서 no_data: %s", metric)
	}
	if _, ok := env.Findings[0]["current"]; !ok {
		t.Fatalf("current 통계 없음: %+v", env.Findings[0])
	}
	// 봉투는 도구 루프에서 그대로 JSON이 된다 — Inf/NaN이 섞이면 안 된다.
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화 실패: %v", err)
	}
	t.Logf("series(%s): status=%s %s", metric, env.Status, env.Summary)

	// 없는 지표 → no_data + discover 안내.
	args, _ = json.Marshal(map[string]string{
		"target": target, "metric": "no.such.metric",
		"from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339)})
	out, err = series.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	if env := out.(Envelope); env.Status != "no_data" || !strings.Contains(env.Summary, "discover_signals") {
		t.Fatalf("없는 지표 봉투 이상: %+v", env)
	}

	// get_metric_dimensions: 프로세스 지표로 차원 분해(sms 대상일 때만
	// 성립하므로, 지표에서 process 라벨 유무에 따라 두 경로 중 하나를 검증).
	dims := NewMetricDimensionsTool(vm)
	args, _ = json.Marshal(map[string]any{
		"target": target, "metric": metric, "dim_label": "process_executable_name",
		"from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339), "top_n": 3})
	out, err = dims.Call(context.Background(), args)
	if err != nil {
		// 차원 없는 지표 경로: 복구 정보(라벨 목록)가 있어야 한다.
		if !strings.Contains(err.Error(), "라벨") {
			t.Fatalf("차원 오류에 복구 정보 없음: %v", err)
		}
		t.Logf("dims: 차원 없음 경로 통과 — %v", err)
	} else {
		env = out.(Envelope)
		if len(env.Findings) == 0 {
			t.Fatalf("dims 봉투 이상: %+v", env)
		}
		t.Logf("dims: status=%s %s", env.Status, env.Summary)
	}
}
