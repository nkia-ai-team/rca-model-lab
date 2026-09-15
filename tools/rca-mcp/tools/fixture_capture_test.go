// 봉투 골든 fixture 캡처 — §5.8-1 "도구별 실측 봉투 → 기대 레코드의 골든
// 쌍"의 왼쪽(실측 봉투)을 만드는 도구다. projector 골든 테스트는 라이브
// 백엔드 없이 돌아야 하므로, 실 봉투를 파일로 박제해 두고 그것을 읽는다.
//
// 갱신 방법(라이브 필요):
//
//	RCA_CAPTURE_DIR=$PWD/evidence/testdata \
//	RCA_VM_URL='http://192.168.230.119:18428' \
//	RCA_CH_URL='http://lucida:lucida123@192.168.230.119:18123' \
//	RCA_PG_DSN='postgres://lucida:lucida123@192.168.230.119:15432/lucida?sslmode=disable' \
//	go test ./tools -run FixtureCapture -v
//
// RCA_CAPTURE_DIR가 없으면 통째로 skip한다 — 평상시 CI는 박제된 파일만 쓴다.
// 대상 발굴은 best-effort다: 원천에 데이터가 없는 도구는 그 도구만 건너뛰고
// 나머지를 캡처한다(라이브 세계에 인시던트가 없는 것은 결함이 아니다).
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestFixtureCapture(t *testing.T) {
	dir := os.Getenv("RCA_CAPTURE_DIR")
	if dir == "" {
		t.Skip("RCA_CAPTURE_DIR 미설정 — 봉투 캡처 생략(박제된 fixture로 충분)")
	}
	vmURL, chURL, dsn := os.Getenv("RCA_VM_URL"), os.Getenv("RCA_CH_URL"), os.Getenv("RCA_PG_DSN")
	if vmURL == "" || chURL == "" || dsn == "" {
		t.Fatal("RCA_VM_URL·RCA_CH_URL·RCA_PG_DSN 셋 다 필요")
	}
	vm := &VM{BaseURL: vmURL}
	ch := chFromEnv(t)
	_ = chURL
	pg, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()
	ctx := context.Background()
	now := time.Now().UTC().Truncate(time.Minute)
	from, to := now.Add(-30*time.Minute), now
	fs, ts := from.Format(time.RFC3339), to.Format(time.RFC3339)

	save := func(name string, r capres) {
		out, err := r.out, r.err
		if err != nil {
			t.Logf("  [%s] 호출 실패 — 건너뜀: %v", name, err)
			return
		}
		env, ok := out.(Envelope)
		if !ok {
			t.Logf("  [%s] 봉투가 아님 — 건너뜀", name)
			return
		}
		b, err := json.MarshalIndent(env, "", " ")
		if err != nil {
			t.Fatalf("[%s] 직렬화: %v", name, err)
		}
		p := filepath.Join(dir, "envelope_"+name+".json")
		if err := os.WriteFile(p, b, 0o600); err != nil {
			t.Fatalf("[%s] 기록: %v", name, err)
		}
		t.Logf("  [%s] status=%s findings=%d scopes=%d → %s",
			name, env.Status, len(env.Findings), len(env.Scopes), p)
	}
	args := func(m map[string]any) json.RawMessage { b, _ := json.Marshal(m); return b }
	one := func(q string) string {
		var s string
		if err := pg.QueryRow(q).Scan(&s); err != nil {
			return ""
		}
		return s
	}
	chOne := func(q string) map[string]any {
		rows, _, err := ch.Query(ctx, q, nil)
		if err != nil || len(rows) == 0 {
			return nil
		}
		return rows[0]
	}

	// ── VM 계열 — 지표가 있는 아무 대상.
	if names, err := vm.LabelValues(ctx, "target_id", `{target_id!=""}`, from, to); err == nil && len(names) > 0 {
		tgt := names[0]
		scanOut, scanErr := NewScanMetricsTool(vm, pg, from).Call(ctx,
			args(map[string]any{"target": tgt, "from": fs, "to": ts}))
		save("scan_metrics", cap2(scanOut, scanErr))
		// read_timeseries·compare_peers의 지표는 scan이 실제로 본 것에서
		// 고른다 — 라벨 목록의 첫 지표는 그 창에 표본이 없을 수 있다.
		metric := ""
		if env, ok := scanOut.(Envelope); ok {
			for _, f := range env.Findings {
				if f["class"] == "shifted" || f["class"] == "appeared" {
					metric = fmt.Sprint(f["metric"])
					break
				}
			}
		}
		if metric != "" {
			save("read_timeseries", cap2(NewReadTimeseriesTool(vm, pg, from, nil).Call(ctx,
				args(map[string]any{"targets": []string{tgt}, "metrics": []string{metric}, "from": fs, "to": ts}))))
			// compare_peers는 또래 명단이 잡히는 대상이어야 판정 행이 나온다 —
			// 앞에서부터 훑어 첫 성공을 쓴다.
			cp := NewComparePeersTool(pg, vm, from, nil)
			for i, cand := range names {
				if i >= 12 {
					break
				}
				out, err := cp.Call(ctx, args(map[string]any{"target": cand, "metric": metric, "from": fs, "to": ts}))
				if env, ok := out.(Envelope); err == nil && ok && len(env.Findings) > 0 {
					save("compare_peers", cap2(out, err))
					break
				}
			}
		}
	}
	// get_processes는 sms.process.* 가 실제로 붙은 호스트여야 한다.
	if hosts, err := vm.LabelValues(ctx, "target_id", `{__name__="sms.process.cpu_utilization"}`, from, to); err == nil && len(hosts) > 0 {
		save("get_processes", cap2(NewProcessesTool(vm).Call(ctx,
			args(map[string]any{"host": hosts[0], "from": fs, "to": ts, "sort_by": "cpu", "top_n": 3}))))
	}
	if cl := one(`SELECT id::text FROM targets WHERE type='kubernetes' LIMIT 1`); cl != "" {
		save("get_k8s_state", cap2(NewK8sStateTool(vm, pg).Call(ctx,
			args(map[string]any{"target": cl, "from": fs, "to": ts}))))
	}
	if dev := one(`SELECT id::text FROM targets WHERE type='network' LIMIT 1`); dev != "" {
		save("get_snmp_traps", cap2(NewSnmpTrapsTool(ch).Call(ctx,
			args(map[string]any{"device": dev, "from": fs, "to": ts}))))
	}

	// ── CH 계열 — 원천에 행이 있는 대상·창을 원천에서 찾는다.
	if r := chOne(`SELECT toString(target_id) AS t, toString(max(occurred_at)) AS at
		FROM lucida_events_local GROUP BY target_id ORDER BY at DESC LIMIT 1`); r != nil {
		at := chParseTime(fmt.Sprint(r["at"]))
		save("list_events", cap2(NewListEventsTool(ch, pg, nil).Call(ctx,
			args(map[string]any{"target": fmt.Sprint(r["t"]),
				"from": at.Add(-30 * time.Minute).Format(time.RFC3339),
				"to":   at.Add(time.Minute).Format(time.RFC3339)}))))
	}
	if r := chOne(`SELECT toString(target_id) AS t, toString(max(timestamp)) AS at
		FROM lucida_logs_local GROUP BY target_id ORDER BY count() DESC LIMIT 1`); r != nil {
		tgt, at := fmt.Sprint(r["t"]), chParseTime(fmt.Sprint(r["at"]))
		lf := at.Add(-30 * time.Minute).Format(time.RFC3339)
		lt := at.Add(time.Minute).Format(time.RFC3339)
		logs := NewSampleLogsTool(ch, at.Add(-30*time.Minute))
		save("sample_logs", cap2(logs.Call(ctx,
			args(map[string]any{"target": tgt, "from": lf, "to": lt, "mode": "map"}))))
		save("sample_logs_grep", cap2(logs.Call(ctx,
			args(map[string]any{"target": tgt, "from": lf, "to": lt, "mode": "grep", "query": "e"}))))
	}
	if r := chOne(`SELECT toString(target_id) AS t, toString(max(timestamp)) AS at
		FROM dpm_topsql_local GROUP BY target_id ORDER BY at DESC LIMIT 1`); r != nil {
		tgt, at := fmt.Sprint(r["t"]), chParseTime(fmt.Sprint(r["at"]))
		save("db_slow_queries", cap2(NewDBSlowQueriesTool(ch, pg, at.Add(-30*time.Minute), at, nil).Call(ctx,
			args(map[string]any{"db": tgt, "from": at.Add(-30 * time.Minute).Format(time.RFC3339),
				"to": at.Add(time.Minute).Format(time.RFC3339)}))))
	}
	// db_blocking은 블로킹이 실재한 창을 겨눠야 한다(희박 — 최근 창은 0건).
	if r := chOne(`SELECT toString(target_id) AS t, toString(max(timestamp)) AS at
		FROM dpm_session_local
		WHERE toInt64OrZero(log_attributes['blockingPid']) > 0
		   OR toInt64OrZero(log_attributes['blockingSession']) > 0
		   OR toInt64OrZero(log_attributes['blockingTid']) > 0
		GROUP BY target_id ORDER BY at DESC LIMIT 1`); r != nil {
		tgt, at := fmt.Sprint(r["t"]), chParseTime(fmt.Sprint(r["at"]))
		save("db_blocking", cap2(NewDBBlockingTool(ch, at.Add(-30*time.Minute), at, nil).Call(ctx,
			args(map[string]any{"db": tgt, "from": at.Add(-30 * time.Minute).Format(time.RFC3339),
				"to": at.Add(time.Minute).Format(time.RFC3339)}))))
	} else if r := chOne(`SELECT toString(target_id) AS t, toString(max(timestamp)) AS at
		FROM dpm_session_local GROUP BY target_id ORDER BY at DESC LIMIT 1`); r != nil {
		// 블로킹 0건 경로도 골든 재료다 — normal + query_scope(0/0).
		tgt, at := fmt.Sprint(r["t"]), chParseTime(fmt.Sprint(r["at"]))
		save("db_blocking", cap2(NewDBBlockingTool(ch, at.Add(-30*time.Minute), at, nil).Call(ctx,
			args(map[string]any{"db": tgt, "from": at.Add(-30 * time.Minute).Format(time.RFC3339),
				"to": at.Add(time.Minute).Format(time.RFC3339)}))))
	}
	if r := chOne(`SELECT service_name AS s, toString(max(timestamp)) AS at
		FROM otel_traces_local GROUP BY service_name ORDER BY count() DESC LIMIT 1`); r != nil {
		svc, at := fmt.Sprint(r["s"]), chParseTime(fmt.Sprint(r["at"]))
		save("breakdown_endpoints", cap2(NewBreakdownEndpointsTool(ch, at.Add(-30*time.Minute), at).Call(ctx,
			args(map[string]any{"target": svc, "from": at.Add(-30 * time.Minute).Format(time.RFC3339),
				"to": at.Add(time.Minute).Format(time.RFC3339)}))))
	}

	// ── 술어 불가 3종 + 변경 — 사상표 행은 없지만 store 적재 경로는 있다.
	if tgt := one(`SELECT id::text FROM targets LIMIT 1`); tgt != "" {
		save("describe_target", cap2(NewDescribeTargetTool(pg, vm, from).Call(ctx,
			args(map[string]any{"target": tgt}))))
		save("get_data_coverage", cap2(NewDataCoverageTool(pg, ch, vm).Call(ctx,
			args(map[string]any{"targets": []string{tgt}, "from": fs, "to": ts}))))
		save("expand_topology", cap2(NewExpandTopologyTool(pg, ch, from, to, nil).Call(ctx,
			args(map[string]any{"target": tgt, "from": fs, "to": ts}))))
		save("list_changes", cap2(NewListChangesTool(pg, from, to, nil).Call(ctx,
			args(map[string]any{"from": fs, "to": ts}))))
	}
}

// must는 (any, error) 쌍을 save에 그대로 넘기기 위한 통과 함수다.
func cap2(out any, err error) capres { return capres{out, err} }

// capres는 (any, error) 쌍을 인자 하나로 접는다 — Go는 다중 반환을 다른
// 인자와 섞어 전개하지 못한다.
type capres struct {
	out any
	err error
}

// chParseTime은 CH DateTime64 문자열을 UTC 시각으로 읽는다.
func chParseTime(s string) time.Time {
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02 15:04:05", time.RFC3339} {
		if t, err := time.Parse(layout, s); err == nil {
			return t.UTC()
		}
	}
	return time.Now().UTC()
}

var _ = sql.ErrNoRows
