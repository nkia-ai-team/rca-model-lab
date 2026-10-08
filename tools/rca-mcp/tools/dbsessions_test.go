// db_blocking 세션 상태 구획 단위 가드 — 실 DB 없이(fake CH·PG) 구조 결정들이 지켜지는지.
// 실측 형상을 줄여 넣는다: 커넥션을 잡고 노는 클라이언트(idle in transaction / ClientRead가
// 폴마다 여럿), 스냅샷에 없는 클라이언트(삭제된 pod), 같은 키 세션 묶음.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestDBSessStateKey(t *testing.T) {
	cases := []struct{ state, stype, wc, wev, want string }{
		{"idle in transaction", "idle in transaction", "Client", "ClientRead", "idle in transaction | Client:ClientRead"},
		{"active", "blocking", "Lock", "tuple", "active (blocking) | Lock:tuple"},
		{"active", "active", "CPU", "CPU", "active | CPU"}, // 클래스=이벤트면 한 번만
		{"idle in transaction", "", "", "", "idle in transaction | -"},
		{"INACTIVE", "", "Idle", "SQL*Net message from client", "INACTIVE | Idle:SQL*Net message from client"},
	}
	for _, c := range cases {
		if got := dbSessStateKey(c.state, c.stype, c.wc, c.wev); got != c.want {
			t.Errorf("%+v: got %q", c, got)
		}
	}
}

func TestDBSessClientAddr(t *testing.T) {
	cases := map[string][2]string{
		"10.244.3.167":            {"10.244.3.167", ""},
		"10.244.3.167:51234":      {"10.244.3.167", ""}, // MySQL hostname = 호스트:포트
		"[fe80::1]:5432":          {"fe80::1", ""},
		"testbed-oracle-0":        {"", "testbed-oracle-0"}, // Oracle machine = 호스트 이름
		"testbed-transfer-58ff-x": {"", "testbed-transfer-58ff-x"},
		"":                        {"", ""},
	}
	for in, want := range cases {
		ip, host := dbSessClientAddr(in)
		if ip != want[0] || host != want[1] {
			t.Errorf("%q: got (%q,%q) want %v", in, ip, host, want)
		}
	}
}

func TestDBSessBuckets(t *testing.T) {
	from := time.Date(2026, 8, 21, 10, 40, 0, 0, time.UTC)
	// 폴이 상한 이하 — 폴 하나가 버킷 하나.
	polls := []time.Time{from.Add(52 * time.Second), from.Add(114 * time.Second), from.Add(175 * time.Second)}
	b := dbSessMakeBuckets(polls, from, from.Add(6*time.Minute))
	if len(b.labels) != 3 || b.widthS != 0 || b.of(polls[2]) != 2 {
		t.Fatalf("per-poll buckets: %+v", b.labels)
	}
	peaks := dbSessPeaks(map[time.Time]int{polls[0]: 4, polls[2]: 6}, b)
	if peaks[0] != 4 || peaks[1] != 0 || peaks[2] != 6 {
		t.Fatalf("peaks %v", peaks)
	}
	// 폴이 상한 초과 — 같은 폭 8개, 버킷 값은 그 안 폴별 값의 최대(합이 아니다).
	var many []time.Time
	per := map[time.Time]int{}
	for i := 0; i < 20; i++ {
		ts := from.Add(time.Duration(i) * time.Minute)
		many = append(many, ts)
		per[ts] = i % 3
	}
	b = dbSessMakeBuckets(many, from, from.Add(20*time.Minute))
	if len(b.labels) != dbSessMaxBuckets || b.widthS != 150 {
		t.Fatalf("equal-width buckets: n=%d width=%d", len(b.labels), b.widthS)
	}
	if b.of(from.Add(-time.Minute)) != 0 || b.of(from.Add(time.Hour)) != dbSessMaxBuckets-1 {
		t.Fatal("bucket index must clamp to [0, n)")
	}
	for i, v := range dbSessPeaks(per, b) {
		if v > 2 {
			t.Fatalf("bucket %d = %d — peak must not sum polls", i, v)
		}
	}
}

func TestDBSessFoldAndGroup(t *testing.T) {
	t1 := time.Date(2026, 8, 21, 10, 41, 52, 0, time.UTC)
	t2 := t1.Add(time.Minute)
	aggs := []dbSessAgg{
		{ts: t1, client: "10.0.0.5", state: "idle in transaction", stype: "idle in transaction", wc: "Client", wev: "ClientRead", sessions: 4, maxAge: 300},
		{ts: t2, client: "10.0.0.5", state: "idle in transaction", stype: "idle in transaction", wc: "Client", wev: "ClientRead", sessions: 6, maxAge: 367},
		{ts: t2, client: "10.0.0.5", state: "active", stype: "active", wc: "IO", wev: "WALSync", sessions: 1, maxAge: 388},
		{ts: t1, client: "10.0.0.9", state: "active", stype: "active", wc: "CPU", wev: "CPU", sessions: 1, maxAge: 2},
	}
	clients, overall := dbSessFold(aggs)
	if len(clients) != 2 || clients[0].client != "10.0.0.5" || clients[0].sessionPolls != 11 {
		t.Fatalf("clients ranked by session-polls: %+v", clients[0])
	}
	if got := dbSessMaxPoll(clients[0].perPoll); got != 7 {
		t.Fatalf("max sessions per poll = %d (6 iit + 1 active at t2)", got)
	}
	if overall[0].key != "idle in transaction | Client:ClientRead" || overall[0].sessionPolls != 10 {
		t.Fatalf("overall top: %+v", overall[0])
	}
	hs := []dbSessHolder{ // 경과 내림차순
		{sid: "63197", client: "10.0.0.77", state: "idle in transaction", stype: "blocking", wc: "Client", wev: "ClientRead", sqlKey: "1", maxAge: 235200},
		{sid: "62953", client: "10.0.0.5", state: "active", stype: "blocking", wc: "Lock", wev: "tuple", sqlKey: "2", maxAge: 229000},
		{sid: "62938", client: "10.0.0.5", state: "active", stype: "blocking", wc: "Lock", wev: "tuple", sqlKey: "2", maxAge: 218000},
		{sid: "62961", client: "10.0.0.5", state: "active", stype: "blocking", wc: "Lock", wev: "tuple", sqlKey: "2", maxAge: 217000},
	}
	gs := dbSessGroupHolders(hs)
	if len(gs) != 2 || gs[0].rep.sid != "63197" || gs[1].rep.sid != "62953" || gs[1].n != 3 || len(gs[1].others) != 2 {
		t.Fatalf("groups: %+v %+v", gs[0], gs[1])
	}
}

// dbSessE2E는 fake CH·PG로 db_blocking 전 경로를 돈다(블로킹 없음 + 세션 상태 구획).
func dbSessE2E(t *testing.T, withPG bool) Envelope {
	t.Helper()
	const cl = "cccccccc-0000-4000-8000-000000000001"
	ch := dgCH(t, []dgCHScript{
		{"AS blocking_n", []map[string]any{
			{"ts": "2026-08-21 10:41:52.080", "engine": "postgresql", "rows": 6, "blocking_n": 0, "blocked_n": 0, "axis_n": 6},
			{"ts": "2026-08-21 10:42:54.090", "engine": "postgresql", "rows": 7, "blocking_n": 0, "blocked_n": 0, "axis_n": 7},
		}},
		{"AS sess_n", []map[string]any{
			{"ts": "2026-08-21 10:41:52.080", "client": "10.0.0.5", "state": "idle in transaction", "stype": "idle in transaction",
				"wait_class": "Client", "wait": "ClientRead", "sess_n": 3, "max_age": 300.5, "users": []string{"commerce"}},
			{"ts": "2026-08-21 10:42:54.090", "client": "10.0.0.5", "state": "idle in transaction", "stype": "idle in transaction",
				"wait_class": "Client", "wait": "ClientRead", "sess_n": 4, "max_age": 367.1, "users": []string{"commerce"}},
			{"ts": "2026-08-21 10:41:52.080", "client": "10.0.0.9", "state": "active", "stype": "active",
				"wait_class": "CPU", "wait": "CPU", "sess_n": 1, "max_age": 2.0, "users": []string{"mon"}},
			{"ts": "2026-08-21 10:42:54.090", "client": "10.0.0.77", "state": "active", "stype": "active",
				"wait_class": "LWLock", "wait": "WALWrite", "sess_n": 2, "max_age": 50.0, "users": []string{"commerce"}},
		}},
		{"AS bg FROM", []map[string]any{{"bg": 2}}},
		{"AS first_ts", []map[string]any{
			{"sid": "101", "client": "10.0.0.5", "max_age": 367.1, "max_at": "2026-08-21 10:42:54.090", "state": "idle in transaction",
				"stype": "idle in transaction", "wait_class": "Client", "wait": "ClientRead", "sql_key": "-1430586695", "usr": "commerce",
				"first_ts": "2026-08-21 10:41:52.080", "last_ts": "2026-08-21 10:42:54.090", "polls": 2},
			{"sid": "102", "client": "10.0.0.5", "max_age": 300.5, "max_at": "2026-08-21 10:41:52.080", "state": "idle in transaction",
				"stype": "idle in transaction", "wait_class": "Client", "wait": "ClientRead", "sql_key": "-1430586695", "usr": "commerce",
				"first_ts": "2026-08-21 10:41:52.080", "last_ts": "2026-08-21 10:41:52.080", "polls": 1},
			{"sid": "103", "client": "10.0.0.77", "max_age": 50.0, "max_at": "2026-08-21 10:42:54.090", "state": "active",
				"stype": "active", "wait_class": "LWLock", "wait": "WALWrite", "sql_key": "0", "usr": "commerce",
				"first_ts": "2026-08-21 10:42:54.090", "last_ts": "2026-08-21 10:42:54.090", "polls": 1},
		}},
		{"FROM otel_traces_local", []map[string]any{{"pod": "order-5c4d-mq4xm", "svc": "commerce-order", "n": 10}}},
	})
	var db *sql.DB
	if withPG {
		p := newDgFakePG(t)
		p.script("FROM kcm_resources p", []string{"cluster", "namespace", "name", "ip", "created", "ok", "owner", "rk", "rsowner"},
			[][]string{
				{cl, "ns", "order-5c4d-mq4xm", "10.0.0.5", "2026-08-12T07:44:34Z", "ReplicaSet", "order-5c4d", "Deployment", "order"},
				// 창 뒤 생성 pod — 그 IP의 당시 주인일 수 없다.
				{cl, "ns", "late-pod-aaaaa", "10.0.0.9", "2026-08-22T00:00:00Z", "ReplicaSet", "late-pod", "Deployment", "late"},
				// 같은 IP 두 pod(hostNetwork 류) — 고르지 않는다.
				{cl, "ns", "ds-a-11111", "10.0.0.77", "2026-08-01T00:00:00Z", "DaemonSet", "ds-a", "", ""},
				{cl, "ns", "ds-b-22222", "10.0.0.77", "2026-08-01T00:00:00Z", "DaemonSet", "ds-b", "", ""},
			})
		p.script("FROM targets WHERE name = ANY", []string{"id", "name", "type"}, [][]string{
			{"aaaaaaaa-0000-4000-8000-0000000000a1", "commerce-order", "application"},
			{"aaaaaaaa-0000-4000-8000-0000000000d1", cl + ":deployment:ns/order", "kubernetes_resource"},
		})
		p.script("FROM db_sql_text", []string{"sql_key", "sql_text"}, [][]string{
			{"-1430586695", "insert into order_schema.outbox_events (aggregate_id) values ($1)"},
		})
		var err error
		db, err = sql.Open("pgx", p.dsn())
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { db.Close() })
	}
	from := time.Date(2026, 8, 21, 10, 40, 38, 0, time.UTC)
	to := time.Date(2026, 8, 21, 10, 46, 48, 0, time.UTC)
	out, err := NewDBBlockingTool(ch, db, from, to, func() time.Time { return to }).Call(context.Background(),
		json.RawMessage(`{"db":"dddddddd-0000-4000-8000-000000000001"}`))
	if err != nil {
		t.Fatal(err)
	}
	return out.(Envelope)
}

func dbSessSection(env Envelope, name string) Finding {
	for _, f := range env.Findings {
		if f["section"] == name {
			return f
		}
	}
	return nil
}

func TestDBBlockingSessionSectionEndToEnd(t *testing.T) {
	env := dbSessE2E(t, true)
	if env.Status != "normal" {
		t.Fatalf("세션 구획은 블로킹 판정(status)을 바꾸지 않는다: %s", env.Status)
	}
	if len(env.Findings) != 2 || env.Findings[0]["section"] != "session_holders" || env.Findings[1]["section"] != "session_clients" {
		t.Fatalf("블로킹 0건이어도 세션 구획 2개(보유 → 클라이언트 순): %+v", env.Findings)
	}
	h := dbSessSection(env, "session_holders")
	hs := h["holders"].([]Finding)
	if len(hs) != 2 || hs[0]["session"] != "101" || hs[0]["sessions_same_key"] != 2 || hs[0]["max_tran_ms"] != 367.1 {
		t.Fatalf("보유 묶음: %+v", hs)
	}
	if hs[0]["client_label"] != "commerce-order" || hs[0]["client_resolution"] != "resolved" || hs[0]["wait"] != "Client:ClientRead" {
		t.Fatalf("보유 세션 클라이언트 해석: %+v", hs[0])
	}
	if hs[1]["client_resolution"] != "ambiguous" || hs[1]["sql_key"] != nil {
		t.Fatalf("같은 IP 두 pod는 고르지 않고, sql_key 0은 싣지 않는다: %+v", hs[1])
	}
	if !strings.HasPrefix(hs[0]["sql_text"].(string), "insert into order_schema.outbox_events") {
		t.Fatalf("db_sql_text 본문(보유 세션 항목에 직접): %v", hs[0]["sql_text"])
	}
	c := dbSessSection(env, "session_clients")
	cs := c["clients"].([]Finding)
	if cs[0]["client"] != "10.0.0.5" || cs[0]["session_polls"] != 7 || cs[0]["max_sessions_per_poll"] != 4 {
		t.Fatalf("클라이언트 순위·수치: %+v", cs[0])
	}
	st := cs[0]["states"].(map[string]any)["idle in transaction | Client:ClientRead"].(map[string]any)
	if pb := st["per_bucket"].([]int); len(pb) != 2 || pb[0] != 3 || pb[1] != 4 {
		t.Fatalf("시간 추이(per_bucket): %v", st["per_bucket"])
	}
	byClient := map[string]Finding{}
	for _, x := range cs {
		byClient[x["client"].(string)] = x
	}
	if byClient["10.0.0.9"]["client_resolution"] != "unresolved" {
		t.Fatalf("창 뒤 생성 pod는 후보가 아니다: %+v", byClient["10.0.0.9"])
	}
	det := c["resolution_detail"].(map[string]any)["10.0.0.5"].(map[string]any)
	if det["workload"] != "Deployment/order" || det["app_target_id"] != "aaaaaaaa-0000-4000-8000-0000000000a1" ||
		det["workload_target_id"] != "aaaaaaaa-0000-4000-8000-0000000000d1" || det["pod"] != "ns/order-5c4d-mq4xm" {
		t.Fatalf("해석 상세: %+v", det)
	}
	if c["background_rows_excluded"] != 2 || c["clients_total"] != 3 {
		t.Fatalf("배경 제외·총수: %v %v", c["background_rows_excluded"], c["clients_total"])
	}
	if _, bad := c["source_errors"]; bad {
		t.Fatalf("정상 조회인데 source_errors: %v", c["source_errors"])
	}
	for _, want := range []string{"세션 101", "10.0.0.5→commerce-order", "insert into order_schema.outbox_events", "세션 수 상위 클라이언트"} {
		if !strings.Contains(env.Summary, want) {
			t.Fatalf("요약에 %q 없음: %s", want, env.Summary)
		}
	}
	refs := strings.Join(env.Refs, " ")
	for _, want := range []string{":session:101", "pg:db_sql_text:dddddddd-0000-4000-8000-000000000001:-1430586695",
		"pg:kcm_resources:" + "cccccccc-0000-4000-8000-000000000001:pod:ns/order-5c4d-mq4xm", "ch:otel_traces_local:host_name->service_name:"} {
		if !strings.Contains(refs, want) {
			t.Fatalf("봉투 refs에 %q 없음: %v", want, env.Refs)
		}
	}
}

// PG 미연결 — 세션 관측은 그대로, 해석·본문만 not_queried(결손 오류 아님).
func TestDBBlockingSessionWithoutPG(t *testing.T) {
	env := dbSessE2E(t, false)
	h := dbSessSection(env, "session_holders")
	hs := h["holders"].([]Finding)
	if hs[0]["client_resolution"] != "not_queried" || hs[0]["client_label"] != nil {
		t.Fatalf("PG 없으면 미해석: %+v", hs[0])
	}
	if hs[0]["sql_text"] != nil {
		t.Fatalf("PG 없으면 본문 없음: %v", hs[0]["sql_text"])
	}
	if !strings.Contains(env.Summary, "10.0.0.5(not_queried)") {
		t.Fatalf("요약: %s", env.Summary)
	}
}

// 행 0 + 명부상 DB 하위 리소스 — 소속 대상과 찾는 경로를 밝힌다.
func TestDBBlockingNoRowsTargetHint(t *testing.T) {
	ch := dgCH(t, nil)
	p := newDgFakePG(t)
	p.script("host_target_id", []string{"type", "host"}, [][]string{{"db_resource", "cf97076f-e72d-4c9e-b731-649592cfc5bf"}})
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	at := time.Date(2026, 8, 31, 19, 56, 0, 0, time.UTC)
	out, err := NewDBBlockingTool(ch, db, at.Add(-5*time.Minute), at, nil).Call(context.Background(),
		json.RawMessage(`{"db":"525d172b-c70a-4092-b612-1754539ed7f9"}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if env.Status != "no_data" || !strings.Contains(env.Summary, "cf97076f-e72d-4c9e-b731-649592cfc5bf") ||
		!strings.Contains(env.Summary, "search_targets type=database") {
		t.Fatalf("하위 리소스 안내: %s", env.Summary)
	}
}

// 블로킹 사건이 있어도 세션 구획이 findings 맨 앞이다(-blind 키 재정렬로 summary가 뒤로 가는
// 응답 앞부분 소비자 대비). 사건 행은 그 뒤에 그대로, status는 사건 규모로만 정한다.
func TestDBBlockingSessionSectionPrecedesEvents(t *testing.T) {
	ch := dgCH(t, []dgCHScript{
		{"AS blocking_n", []map[string]any{
			{"ts": "2026-08-31 19:53:02.351", "engine": "postgresql", "rows": 12, "blocking_n": 10, "blocked_n": 10, "axis_n": 12},
		}},
		{"AS isblk", []map[string]any{
			{"ts": "2026-08-31 19:53:02.351", "engine": "postgresql", "sid": 63197, "blocker": 0, "isblk": "1", "wait": "ClientRead", "wait_class": "", "rel": "", "sql_key": "1132536200"},
			{"ts": "2026-08-31 19:53:02.351", "engine": "postgresql", "sid": 62953, "blocker": 63197, "isblk": "1", "wait": "transactionid", "wait_class": "", "rel": "inventory", "sql_key": "-820851744"},
		}},
		{"AS sess_n", []map[string]any{
			{"ts": "2026-08-31 19:53:02.351", "client": "10.244.2.247", "state": "idle in transaction", "stype": "blocking",
				"wait_class": "Client", "wait": "ClientRead", "sess_n": 1, "max_age": 52245.9, "users": []string{"commerce"}},
		}},
		{"AS bg FROM", []map[string]any{{"bg": 0}}},
		{"AS first_ts", []map[string]any{
			{"sid": "63197", "client": "10.244.2.247", "max_age": 52245.9, "max_at": "2026-08-31 19:53:02.351", "state": "idle in transaction",
				"stype": "blocking", "wait_class": "Client", "wait": "ClientRead", "sql_key": "1132536200", "usr": "commerce",
				"first_ts": "2026-08-31 19:53:02.351", "last_ts": "2026-08-31 19:53:02.351", "polls": 1},
		}},
	})
	at := time.Date(2026, 8, 31, 19, 56, 16, 0, time.UTC)
	out, err := NewDBBlockingTool(ch, nil, at.Add(-5*time.Minute), at, nil).Call(context.Background(),
		json.RawMessage(`{"db":"cf97076f-e72d-4c9e-b731-649592cfc5bf"}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if env.Status != "anomalous" {
		t.Fatalf("status는 사건 규모(blocked_peak 10)로: %s", env.Status)
	}
	if len(env.Findings) != 3 || env.Findings[0]["section"] != "session_holders" || env.Findings[1]["section"] != "session_clients" ||
		env.Findings[2]["blocked_peak"] != 10 {
		t.Fatalf("순서: 보유 → 클라이언트 → 사건: %+v", env.Findings)
	}
	if hs := env.Findings[0]["holders"].([]Finding); hs[0]["session"] != "63197" || hs[0]["session_type"] != "blocking" {
		t.Fatalf("루트 세션이 보유 상위에: %+v", hs[0])
	}
}
