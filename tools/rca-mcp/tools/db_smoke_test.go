// 실 스모크 — CH 필수, PG는 본문 사전 검증에 사용(없으면 본문 생략 경로).
//
// db_blocking 쪽 스모크는 두 창을 쓴다(§13.13 재작성):
//   - 최근 창: engine별 판별식이 실제로 해석되는지 + "블로킹 없음"이
//     no_data가 아니라 normal인지(§13.9).
//   - 블로킹 실재 창: 원천에서 블로킹 행이 있는 시각을 찾아 그 창을
//     겨냥 — 사건 접기·루트 표시·두 방향 결손이 실물에서 나오는지.
//     희박(PG 1.9%·Oracle 0.03%)해서 최근 창만으로는 도달할 수 없다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"testing"
	"time"
)

func TestDBToolsSmoke(t *testing.T) {
	ch := chFromEnv(t)
	var pg *sql.DB
	if dsn := os.Getenv("RCA_PG_DSN"); dsn != "" {
		var err error
		if pg, err = OpenPG(dsn); err != nil {
			t.Fatal(err)
		}
		defer pg.Close()
	}

	// 3 engine 각각의 최근 세션 스냅샷으로 검증 — engine별 키 매핑이
	// 실제로 해석되는지가 이 스모크의 핵심이다.
	engines, _, err := ch.Query(context.Background(), `
		SELECT engine, target_id, max(timestamp) AS at FROM dpm_session_local
		GROUP BY engine, target_id ORDER BY at DESC LIMIT 10`, nil)
	if err != nil || len(engines) == 0 {
		t.Fatalf("실 세션 대상 발굴: %v", err)
	}
	seen := map[string]bool{}
	slow := NewDBSlowQueriesTool(ch, pg, time.Time{}, time.Time{}, nil)
	for _, e := range engines {
		eng := e["engine"].(string)
		if seen[eng] {
			continue
		}
		seen[eng] = true
		target := e["target_id"].(string)
		at, _ := time.Parse("2006-01-02 15:04:05.999999999", e["at"].(string))
		from := at.Add(-10 * time.Minute).Format(time.RFC3339)
		to := at.Add(time.Minute).Format(time.RFC3339)

		args, _ := json.Marshal(map[string]string{"db": target, "from": from, "to": to})

		blk := NewDBBlockingTool(ch, time.Time{}, time.Time{}, nil)
		out, err := blk.Call(context.Background(), args)
		if err != nil {
			t.Fatalf("[%s] db_blocking: %v", eng, err)
		}
		env := out.(Envelope)
		if _, err := json.Marshal(env); err != nil {
			t.Fatalf("[%s] 봉투 직렬화: %v", eng, err)
		}
		// 스냅샷을 창에 두고 no_data면 회귀다 — "블로킹 없음"은 normal이고
		// no_data는 행 0 또는 축 부재 전용이다(§13.9).
		if env.Status == "no_data" {
			t.Fatalf("[%s] 스냅샷을 창에 두고도 no_data: reason=%s %s", eng, env.NoDataReason, env.Summary)
		}
		if env.AssessmentBasis == "" {
			t.Fatalf("[%s] assessment_basis 없음", eng)
		}
		t.Logf("[%s] blocking(최근 창): status=%s %s", eng, env.Status, env.Summary)

		// db_slow_queries — 세션 대상과 topsql 대상은 같은 target_id다
		// (같은 DPM 컬렉터). 창은 위와 같이 마지막 스냅샷 주변 10분.
		out, err = slow.Call(context.Background(), args)
		if err != nil {
			t.Fatalf("[%s] db_slow_queries: %v", eng, err)
		}
		env = out.(Envelope)
		if _, err := json.Marshal(env); err != nil {
			t.Fatalf("[%s] slow 봉투 직렬화: %v", eng, err)
		}
		if env.Status == "no_data" {
			t.Logf("[%s] slow: no_data(%s) — %s", eng, env.NoDataReason, env.Summary)
			continue
		}
		var sqlRows int
		for _, f := range env.Findings {
			if f["kind"] != "sql" {
				continue
			}
			sqlRows++
			// 필드 계약 회귀 가드 — 선언한 필드가 실물에서 채워지는지.
			for _, k := range []string{"sql_key", "engine", "mode", "latency_ms_p50",
				"latency_ms_max", "latency_ms_last", "latency_unit_source", "polls_seen",
				"polls_in_window", "presence", "first_seen", "last_seen", "verdict",
				"rank_among_collected", "baseline_polls", "total_ms_source"} {
				if _, ok := f[k]; !ok {
					t.Fatalf("[%s] slow finding에 %s 없음: %+v", eng, k, f)
				}
			}
			// 단위 정규화 회귀 가드(§14.1 정정 1) — Oracle은 μs 원천이므로
			// unit_source가 us여야 하고, 값은 이미 ms로 나눠져 있어야 한다.
			want := map[string]string{"postgresql": "ms", "mysql": "ms", "mariadb": "ms",
				"oracle": "us", "tibero": "us", "mssql": "us"}[eng]
			if got := f["latency_unit_source"]; want != "" && got != want {
				t.Fatalf("[%s] latency_unit_source=%v, 계약은 %s", eng, got, want)
			}
		}
		if sqlRows == 0 {
			t.Fatalf("[%s] status=%s인데 sql 행이 0건: %s", eng, env.Status, env.Summary)
		}
		var cov Finding
		for _, f := range env.Findings {
			if f["kind"] == "coverage" {
				cov = f
			}
		}
		if cov == nil {
			t.Fatalf("[%s] coverage finding 없음", eng)
		}
		// 절단 고지가 빠지면 조사자가 "이 창의 모든 SQL"로 오독한다(§14.4).
		if cov["population"] != "top_n_union_per_poll" || cov["selection_axis_per_row"] != "unavailable" {
			t.Fatalf("[%s] coverage 절단 고지 누락: %+v", eng, cov)
		}
		t.Logf("[%s] slow: status=%s 행 %d개 / %s", eng, env.Status, sqlRows, env.Summary)
	}
	if len(seen) < 2 {
		t.Fatalf("engine 다양성 부족: %v", seen)
	}
}

// TestDBSlowQueriesRegressionSmoke는 원천에서 "자기 기준선 대비 3배"가
// 실재하는 창을 찾아 겨냥한다. slower 판정·chronic 분리는 회귀가 있어야
// 밟히는 경로라 최근 창 스모크로는 도달하지 않는다(라이브 배경 발화율이
// 0에 가깝다는 것이 §14.3의 문턱 근거이므로, 최근 창은 정의상 normal이다).
func TestDBSlowQueriesRegressionSmoke(t *testing.T) {
	ch := chFromEnv(t)
	dsn := os.Getenv("RCA_PG_DSN")
	if dsn == "" {
		t.Skip("RCA_PG_DSN 미설정 — engine 레지스트리·본문 사전 없이는 이 스모크가 반쪽이다")
	}
	pg, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()

	targets, err := pg.Query(`SELECT target_id, engine FROM collector_dpm_engine_version ORDER BY engine`)
	if err != nil {
		t.Fatalf("engine 레지스트리 조회: %v", err)
	}
	defer targets.Close()
	type tgt struct{ id, engine string }
	var list []tgt
	for targets.Next() {
		var v tgt
		if targets.Scan(&v.id, &v.engine) == nil {
			list = append(list, v)
		}
	}
	if len(list) == 0 {
		t.Skip("레지스트리에 DPM 대상이 없다")
	}

	tool := NewDBSlowQueriesTool(ch, pg, time.Time{}, time.Time{}, nil)
	fired := 0
	for _, v := range list {
		spec, ok := sqEngines[v.engine]
		if !ok {
			continue
		}
		// 도구가 쓰는 것과 같은 식으로 회귀 창을 찾는다 — 시험이 코드의
		// 계약을 따라가게 한다(시각 후보만 얻고 판정은 도구가 한다).
		hot, _, err := ch.Query(context.Background(), fmt.Sprintf(`
			WITH w AS (
			  SELECT sql_key, toStartOfInterval(parseDateTime64BestEffort(ts, 9), INTERVAL 30 MINUTE) AS win,
			         quantileExact(0.5)(lat_poll) AS m
			  FROM (SELECT sql_key, ts, if(sum(execs) > 0, sum(lat_ms * execs) / sum(execs), avg(lat_ms)) AS lat_poll
			        FROM (SELECT %s FROM dpm_topsql_local
			              WHERE target_id = {target:String} AND engine = {engine:String}
			                AND timestamp > now() - INTERVAL 4 DAY)
			        GROUP BY sql_key, ts)
			  GROUP BY sql_key, win HAVING count() >= %d)
			SELECT toString(win) AS win, m / nullIf(base, 0) AS ratio FROM (
			  SELECT sql_key, win, m,
			         quantileExact(0.5)(m) OVER (PARTITION BY sql_key ORDER BY win ROWS BETWEEN 48 PRECEDING AND 1 PRECEDING) AS base,
			         count(m) OVER (PARTITION BY sql_key ORDER BY win ROWS BETWEEN 48 PRECEDING AND 1 PRECEDING) AS bw
			  FROM w)
			WHERE bw >= 20 AND m / nullIf(base, 0) >= %v
			ORDER BY ratio DESC LIMIT 1`, sqSelectExprs(v.engine, spec), sqMinPolls, sqRatioThreshold),
			map[string]string{"target": v.id, "engine": v.engine})
		if err != nil {
			t.Fatalf("[%s] 회귀 창 발굴: %v", v.engine, err)
		}
		if len(hot) == 0 {
			t.Logf("[%s] 최근 4일에 3배 회귀가 없다 — 이 engine은 건너뜀(배경 발화율이 0에 가깝다는 §14.3과 정합)", v.engine)
			continue
		}
		win, _ := time.Parse("2006-01-02 15:04:05.999999999", fmt.Sprint(hot[0]["win"]))
		args, _ := json.Marshal(map[string]any{
			"db": v.id, "from": win.UTC().Format(time.RFC3339),
			"to":   win.Add(30 * time.Minute).UTC().Format(time.RFC3339),
			"sort": "ratio_p50", "top_n": 5,
		})
		out, err := tool.Call(context.Background(), args)
		if err != nil {
			t.Fatalf("[%s] db_slow_queries: %v", v.engine, err)
		}
		env := out.(Envelope)
		var slower Finding
		for _, f := range env.Findings {
			if f["kind"] == "sql" && f["verdict"] == "slower" {
				slower = f
				break
			}
		}
		if slower == nil {
			t.Fatalf("[%s] 원천에서 3배가 실재하는 창(%s)인데 slower 판정이 없다: %s",
				v.engine, win.Format(time.RFC3339), env.Summary)
		}
		for _, k := range []string{"ratio_p50", "slower_basis", "baseline_latency_ms_p50", "baseline_polls"} {
			if _, ok := slower[k]; !ok {
				t.Fatalf("[%s] slower 행에 %s 없음: %+v", v.engine, k, slower)
			}
		}
		// 자기관측 행은 status를 올리지 못한다(§14.8) — 실물에서 컬렉터
		// 쿼리가 3배로 발화하는 것을 관측했으므로 이 규율이 회귀하면
		// 조사자가 모니터링 쿼리를 원인으로 끌고 간다.
		if self, _ := slower["self_monitoring"].(bool); self && env.Status == "anomalous" {
			onlySelf := true
			for _, f := range env.Findings {
				if f["kind"] == "sql" && f["verdict"] == "slower" {
					if s, _ := f["self_monitoring"].(bool); !s {
						onlySelf = false
					}
				}
			}
			if onlySelf {
				t.Fatalf("[%s] slower가 자기관측뿐인데 status=anomalous: %s", v.engine, env.Summary)
			}
		}
		fired++
		t.Logf("[%s] %s 창: status=%s / %v %v배(%v) p50 %v→%v / plan=%v",
			v.engine, win.Format(time.RFC3339), env.Status, slower["sql_key"], slower["ratio_p50"],
			slower["slower_basis"], slower["baseline_latency_ms_p50"], slower["latency_ms_p50"],
			slower["plan_records"])
	}
	if fired == 0 {
		t.Skip("어느 engine에도 최근 4일 회귀 창이 없다 — 판정 경로 스모크 불가")
	}
}

// TestDBBlockingRealEventSmoke는 원천에 블로킹이 실재하는 창을 찾아
// 겨냥한다. 사건 접기·루트 표시·결손 명단은 블로킹이 있어야 나오는
// 경로라 최근 창 스모크로는 한 번도 밟히지 않는다.
func TestDBBlockingRealEventSmoke(t *testing.T) {
	ch := chFromEnv(t)

	// 블로킹 행이 가장 많은 시각대를 engine별로 하나씩 고른다.
	hot, _, err := ch.Query(context.Background(), `
		SELECT engine, target_id, toString(min(timestamp)) AS t0, toString(max(timestamp)) AS t1, count() AS n
		FROM dpm_session_local
		WHERE log_attributes['is_blocking'] = '1' OR `+dbBlkBlockerExpr+` > 0
		GROUP BY engine, target_id, toStartOfHour(timestamp)
		ORDER BY n DESC LIMIT 20`, nil)
	if err != nil {
		t.Fatalf("블로킹 실재 창 발굴: %v", err)
	}
	if len(hot) == 0 {
		t.Skip("이 환경의 원천에 블로킹 관측이 없다 — 사건 경로 스모크 불가")
	}

	blk := NewDBBlockingTool(ch, time.Time{}, time.Time{}, nil)
	seen := map[string]bool{}
	events := 0
	for _, h := range hot {
		eng := h["engine"].(string)
		if seen[eng] {
			continue
		}
		seen[eng] = true
		t0, _ := time.Parse("2006-01-02 15:04:05.999999999", h["t0"].(string))
		t1, _ := time.Parse("2006-01-02 15:04:05.999999999", h["t1"].(string))
		args, _ := json.Marshal(map[string]string{
			"db":   h["target_id"].(string),
			"from": t0.Add(-2 * time.Minute).Format(time.RFC3339),
			"to":   t1.Add(2 * time.Minute).Format(time.RFC3339),
		})
		out, err := blk.Call(context.Background(), args)
		if err != nil {
			t.Fatalf("[%s] db_blocking: %v", eng, err)
		}
		env := out.(Envelope)
		if len(env.Findings) == 0 {
			t.Fatalf("[%s] 블로킹 행이 실재하는 창인데 사건 0건: %s", eng, env.Summary)
		}
		f := env.Findings[0]
		// 필드 계약 회귀 가드 — 선언한 필드가 실물에서 채워지는지.
		for _, k := range []string{"engine", "first_seen", "last_seen", "polls",
			"root_timeline", "root_migrated", "blocked_timeline", "blocked_peak",
			"blocked_last", "roles", "representative_poll_ts", "edges",
			"unpaired_blockers", "orphan_blocked", "chain_depth_min_observed",
			"blocked_predicate", "pair_linking"} {
			if _, ok := f[k]; !ok {
				t.Fatalf("[%s] finding에 %s 없음: %+v", eng, k, f)
			}
		}
		// asInt는 CH JSON 값(float64)용이라 finding의 Go int를 0으로 읽는다.
		peak, _ := f["blocked_peak"].(int)
		if peak == 0 && len(f["unpaired_blockers"].([]string)) == 0 {
			t.Fatalf("[%s] 막힌 세션도 0이고 결손 명단도 비었다 — 판별식 회귀 의심: %+v", eng, f)
		}
		// contended_objects는 PG만 — 없는 engine에서 빈 배열로 새면 회귀다.
		if _, ok := f["contended_objects"]; ok && eng != "postgresql" {
			t.Fatalf("[%s] relNames 없는 engine인데 contended_objects가 있다: %+v", eng, f["contended_objects"])
		}
		if _, err := json.Marshal(env); err != nil {
			t.Fatalf("[%s] 봉투 직렬화: %v", eng, err)
		}
		events += len(env.Findings)
		t.Logf("[%s] status=%s 사건 %d건 / 최대 %v세션·%v폴 / 루트이동=%v / 깊이하한=%v / 경합=%v / 결손 unpaired=%v orphan=%v",
			eng, env.Status, len(env.Findings), f["blocked_peak"], f["polls"], f["root_migrated"],
			f["chain_depth_min_observed"], f["contended_objects"], f["unpaired_blockers"], f["orphan_blocked"])
	}
	if events == 0 {
		t.Fatal("블로킹 실재 창에서 사건이 하나도 접히지 않았다")
	}
}
