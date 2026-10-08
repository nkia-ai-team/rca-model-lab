// db_blocking 단일 DB 경로(db를 준 호출) 응답 동결 가드 — 전체 개관(db 생략, 2026-10-06)을
// 넣으면서 db를 준 호출의 봉투가 한 바이트도 바뀌지 않아야 한다(기존 교사 궤적이 이 바이너리로
// 재생된다). 골든은 전체 개관 이전 소스로 만들었다:
//
//	go test -overlay <개관 이전 소스 overlay> -run TestDBBlockingSingleDBGolden ./tools/  (DBBLK_GOLDEN_UPDATE=1)
//
// overlay 빌드가 당시 bin/rca-mcp와 sha256까지 같음을 확인한 뒤 생성했다. 시나리오는 사건 2건
// (정렬·대표 폴·사건 상한 절단), 세션 구획+클라이언트 해석, 블로킹 없음, 판단 축 없음, 행 0+명부
// 힌트, 창 오류다. 인자 형식 오류·UUID 아님 오류 문구는 db 선택화로 의도적으로 바꿨으므로 제외한다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"testing"
	"time"
)

const dbBlkGoldenPath = "testdata/dbblocking_single_golden.json"

func dbBlkGoldenPG(t *testing.T) *sql.DB {
	t.Helper()
	const cl = "cccccccc-0000-4000-8000-000000000001"
	p := newDgFakePG(t)
	p.script("FROM kcm_resources p", []string{"cluster", "namespace", "name", "ip", "created", "ok", "owner", "rk", "rsowner"},
		[][]string{{cl, "ns", "order-5c4d-mq4xm", "10.0.0.5", "2026-08-12T07:44:34Z", "ReplicaSet", "order-5c4d", "Deployment", "order"}})
	p.script("FROM targets WHERE name = ANY", []string{"id", "name", "type"}, [][]string{
		{"aaaaaaaa-0000-4000-8000-0000000000a1", "commerce-order", "application"},
	})
	p.script("FROM db_sql_text", []string{"sql_key", "sql_text"}, [][]string{{"1132536200", "select 1 for update"}})
	p.script("host_target_id", []string{"type", "host"}, [][]string{{"db_resource", "cf97076f-e72d-4c9e-b731-649592cfc5bf"}})
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// dbBlkGoldenBlockingCH — 사건 2건(규모 8·3, 사이에 수집된 조용한 폴) + 세션 구획 원천.
func dbBlkGoldenBlockingCH(t *testing.T) *CH {
	t.Helper()
	poll := func(ts string, blk, blkd int) map[string]any {
		return map[string]any{"ts": ts, "engine": "postgresql", "rows": 12, "blocking_n": blk, "blocked_n": blkd, "axis_n": 12}
	}
	row := func(ts string, sid, blocker int, isblk string) map[string]any {
		return map[string]any{"ts": ts, "engine": "postgresql", "sid": sid, "blocker": blocker, "isblk": isblk,
			"wait": "transactionid", "wait_class": "", "rel": "inventory, inventory_pkey", "sql_key": "1132536200"}
	}
	return dgCH(t, []dgCHScript{
		{"AS blocking_n", []map[string]any{
			poll("2026-08-31 19:50:01.100", 1, 3), poll("2026-08-31 19:51:02.200", 0, 0),
			poll("2026-08-31 19:52:03.300", 2, 8), poll("2026-08-31 19:53:04.400", 2, 8), poll("2026-08-31 19:56:05.500", 1, 6),
		}},
		{"AS isblk", []map[string]any{
			row("2026-08-31 19:50:01.100", 501, 0, "1"), row("2026-08-31 19:50:01.100", 502, 501, "0"),
			row("2026-08-31 19:52:03.300", 63197, 0, "1"), row("2026-08-31 19:52:03.300", 62953, 63197, "1"),
			row("2026-08-31 19:53:04.400", 63197, 0, "1"), row("2026-08-31 19:53:04.400", 62953, 63197, "1"),
			row("2026-08-31 19:53:04.400", 62961, 62953, "0"), row("2026-08-31 19:53:04.400", 62999, 70000, "0"),
		}},
		{"AS sess_n", []map[string]any{
			{"ts": "2026-08-31 19:52:03.300", "client": "10.0.0.5", "state": "idle in transaction", "stype": "blocking",
				"wait_class": "Client", "wait": "ClientRead", "sess_n": 1, "max_age": 52245.9, "users": []string{"commerce"}},
			{"ts": "2026-08-31 19:53:04.400", "client": "10.0.0.7", "state": "active", "stype": "blocked",
				"wait_class": "Lock", "wait": "transactionid", "sess_n": 7, "max_age": 3000.5, "users": []string{"commerce"}},
		}},
		{"AS bg FROM", []map[string]any{{"bg": 3}}},
		{"AS first_ts", []map[string]any{
			{"sid": "63197", "client": "10.0.0.5", "max_age": 52245.9, "max_at": "2026-08-31 19:53:04.400", "state": "idle in transaction",
				"stype": "blocking", "wait_class": "Client", "wait": "ClientRead", "sql_key": "1132536200", "usr": "commerce",
				"first_ts": "2026-08-31 19:52:03.300", "last_ts": "2026-08-31 19:56:05.500", "polls": 3},
			{"sid": "62953", "client": "10.0.0.7", "max_age": 3000.5, "max_at": "2026-08-31 19:53:04.400", "state": "active",
				"stype": "blocked", "wait_class": "Lock", "wait": "transactionid", "sql_key": "-820851744", "usr": "commerce",
				"first_ts": "2026-08-31 19:52:03.300", "last_ts": "2026-08-31 19:53:04.400", "polls": 2},
		}},
		{"FROM otel_traces_local", []map[string]any{{"pod": "order-5c4d-mq4xm", "svc": "commerce-order", "n": 10}}},
	})
}

type dbBlkGoldenCase struct {
	ch   func(t *testing.T) *CH
	pg   bool
	args string
}

func dbBlkGoldenCases() map[string]dbBlkGoldenCase {
	const db = `"db":"cf97076f-e72d-4c9e-b731-649592cfc5bf"`
	quiet := func(t *testing.T) *CH {
		return dgCH(t, []dgCHScript{
			{"AS blocking_n", []map[string]any{{"ts": "2026-08-31 19:52:03.300", "engine": "postgresql", "rows": 4, "blocking_n": 0, "blocked_n": 0, "axis_n": 4}}},
			{"AS sess_n", []map[string]any{{"ts": "2026-08-31 19:52:03.300", "client": "10.0.0.9", "state": "active", "stype": "active",
				"wait_class": "CPU", "wait": "CPU", "sess_n": 1, "max_age": 2.0, "users": []string{"mon"}}}},
		})
	}
	noAxis := func(t *testing.T) *CH {
		return dgCH(t, []dgCHScript{
			{"AS blocking_n", []map[string]any{{"ts": "2026-08-31 19:52:03.300", "engine": "clickhouse", "rows": 4, "blocking_n": 0, "blocked_n": 0, "axis_n": 0}}},
		})
	}
	empty := func(t *testing.T) *CH { return dgCH(t, nil) }
	return map[string]dbBlkGoldenCase{
		"blocking_with_sessions":    {dbBlkGoldenBlockingCH, true, `{` + db + `}`},
		"blocking_without_pg":       {dbBlkGoldenBlockingCH, false, `{` + db + `,"from":"2026-08-31T19:45:00Z","to":"2026-08-31T19:57:00Z"}`},
		"blocking_max_events_1":     {dbBlkGoldenBlockingCH, true, `{` + db + `,"max_events":1}`},
		"no_blocking":               {quiet, true, `{` + db + `}`},
		"no_axis":                   {noAxis, false, `{` + db + `}`},
		"no_rows_hint":              {empty, true, `{` + db + `,"from":"now-30m","to":"now"}`},
		"window_error":              {empty, false, `{` + db + `,"from":"2026-08-31T19:57:00Z","to":"2026-08-31T19:45:00Z"}`},
		"window_from_parse_error":   {empty, false, `{` + db + `,"from":"yesterday"}`},
		"window_default_no_seed_ok": {quiet, false, `{` + db + `}`},
	}
}

func dbBlkGoldenRun(t *testing.T, name string, c dbBlkGoldenCase) string {
	t.Helper()
	first := time.Date(2026, 8, 31, 19, 49, 0, 0, time.UTC)
	last := time.Date(2026, 8, 31, 19, 57, 0, 0, time.UTC)
	if name == "window_default_no_seed_ok" {
		first, last = time.Time{}, time.Time{}
	}
	var db *sql.DB
	if c.pg {
		db = dbBlkGoldenPG(t)
	}
	tool := NewDBBlockingTool(c.ch(t), db, first, last, func() time.Time { return last })
	out, err := tool.Call(context.Background(), json.RawMessage(c.args))
	if err != nil {
		return "error: " + err.Error()
	}
	b, err := json.Marshal(out)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestDBBlockingSingleDBGolden(t *testing.T) {
	cases := dbBlkGoldenCases()
	names := make([]string, 0, len(cases))
	for n := range cases {
		names = append(names, n)
	}
	sort.Strings(names)
	got := map[string]string{}
	for _, n := range names {
		got[n] = dbBlkGoldenRun(t, n, cases[n])
	}
	if os.Getenv("DBBLK_GOLDEN_UPDATE") == "1" {
		b, _ := json.MarshalIndent(got, "", " ")
		if err := os.MkdirAll(filepath.Dir(dbBlkGoldenPath), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(dbBlkGoldenPath, append(b, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
		t.Logf("golden written: %d cases", len(got))
		return
	}
	raw, err := os.ReadFile(dbBlkGoldenPath)
	if err != nil {
		t.Fatalf("골든 없음: %v", err)
	}
	var want map[string]string
	if err := json.Unmarshal(raw, &want); err != nil {
		t.Fatal(err)
	}
	if len(want) != len(got) {
		t.Fatalf("시나리오 수 불일치: golden %d, now %d", len(want), len(got))
	}
	for _, n := range names {
		if got[n] != want[n] {
			t.Errorf("%s: db를 준 호출의 응답이 바뀌었다\nwant %s\n got %s", n, want[n], got[n])
		}
	}
}
