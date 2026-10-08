// db_blocking 전체 개관(db 생략) 단위 가드 — 실 DB 없이(대상별 fake CH·fake PG) 구조 결정들이
// 지켜지는지: db 생략 분기, 증거 강도 순위, 상한·예산·절단 표시, 명부 결손 대체, ref 관례.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

const (
	fltPG     = "aaaaaaaa-0000-4000-8000-00000000000a"
	fltOra    = "bbbbbbbb-0000-4000-8000-00000000000b"
	fltMy     = "cccccccc-0000-4000-8000-00000000000c"
	fltIdle   = "dddddddd-0000-4000-8000-00000000000d"
	fltSecond = "2026-08-31 19:53:04.400"
)

var (
	fltFirst = time.Date(2026, 8, 31, 19, 49, 0, 0, time.UTC)
	fltLast  = time.Date(2026, 8, 31, 19, 57, 0, 0, time.UTC)
)

// fltCH — 대상별 대본 CH fake(param_target으로 대본을 고른다). 대상 인자가 없는 조회는 common.
func fltCH(t *testing.T, byTarget map[string][]dgCHScript, common []dgCHScript) *CH {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		scripts := common
		if tg := r.URL.Query().Get("param_target"); tg != "" {
			scripts = byTarget[tg]
		}
		for _, sc := range scripts {
			if strings.Contains(string(body), sc.contains) {
				for _, row := range sc.rows {
					b, _ := json.Marshal(row)
					w.Write(append(b, '\n'))
				}
				return
			}
		}
	}))
	t.Cleanup(srv.Close)
	return &CH{BaseURL: srv.URL, Database: "lucida"}
}

func fltPoll(ts, engine string, blk, blkd int) map[string]any {
	return map[string]any{"ts": ts, "engine": engine, "rows": 10, "blocking_n": blk, "blocked_n": blkd, "axis_n": 10}
}

func fltHolder(sid, client, state, stype, wc, wev string, age float64) map[string]any {
	return map[string]any{"sid": sid, "client": client, "max_age": age, "max_at": fltSecond, "state": state, "stype": stype,
		"wait_class": wc, "wait": wev, "sql_key": "1", "usr": "app", "first_ts": fltSecond, "last_ts": fltSecond, "polls": 1}
}

// fltScripts — PG(블로킹 최대 5), Oracle(블로킹 최대 9), MySQL(블로킹 없음·세션만), 행 없는 대상 1개.
func fltScripts() map[string][]dgCHScript {
	return map[string][]dgCHScript{
		fltPG: {
			{"AS blocking_n", []map[string]any{fltPoll("2026-08-31 19:52:03.300", "postgresql", 1, 5), fltPoll(fltSecond, "postgresql", 1, 5)}},
			{"AS first_ts", []map[string]any{
				fltHolder("63197", "10.0.0.5", "idle in transaction", "blocking", "Client", "ClientRead", 52245.9),
				fltHolder("62953", "10.0.0.7", "active", "blocked", "Lock", "transactionid", 3000.5),
				fltHolder("62961", "10.0.0.7", "active", "blocked", "Lock", "transactionid", 2000.5),
			}},
		},
		fltOra: {
			{"AS blocking_n", []map[string]any{fltPoll("2026-08-31 19:52:21.000", "oracle", 1, 9), fltPoll("2026-08-31 19:55:28.000", "oracle", 1, 9)}},
			{"AS first_ts", []map[string]any{fltHolder("228", "testbed-oracle-0", "INACTIVE", "", "Idle", "SQL*Net message from client", 197506.4)}},
		},
		fltMy: {
			{"AS blocking_n", []map[string]any{fltPoll(fltSecond, "mysql", 0, 0)}},
			{"AS first_ts", []map[string]any{fltHolder("35574", "10.244.1.0:5123", "Query", "active", "", "executing", 12.62)}},
		},
	}
}

func fltInventoryPG(t *testing.T) *sql.DB {
	t.Helper()
	p := newDgFakePG(t)
	p.script("coalesce(address,'')", []string{"id", "name", "display", "type", "address"}, [][]string{
		{fltPG, "PostgreSQL-commerce", "PostgreSQL-commerce", "database", "10.0.0.36"},
		{fltOra, "Oracle-corebanking", "Oracle-corebanking", "database", "10.0.0.37"},
		{fltMy, "MySQL-food", "MySQL-food", "database", "10.0.0.38"},
		{fltIdle, "Idle-DB", "Idle-DB", "database", "10.0.0.39"},
	})
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func fltCall(t *testing.T, ch *CH, pg *sql.DB, args string) Envelope {
	t.Helper()
	out, err := NewDBBlockingTool(ch, pg, fltFirst, fltLast, func() time.Time { return fltLast }).Call(context.Background(), json.RawMessage(args))
	if err != nil {
		t.Fatalf("args %q: %v", args, err)
	}
	return out.(Envelope)
}

// db 생략(빈 값·공백·null·인자 없음 포함) → 전체 개관. 명부 순서가 아니라 증거 강도 순.
func TestDBFleetOmittedDBDispatch(t *testing.T) {
	ch := fltCH(t, fltScripts(), nil)
	pg := fltInventoryPG(t)
	for _, args := range []string{`{}`, `{"db":""}`, `{"db":"  "}`, `null`, ``} {
		env := fltCall(t, ch, pg, args)
		if env.Status != "anomalous" || len(env.Findings) != 4 {
			t.Fatalf("args %q: status=%s findings=%d", args, env.Status, len(env.Findings))
		}
		order := []string{fltOra, fltPG, fltMy}
		for i, want := range order {
			if f := env.Findings[i]; f["section"] != dbFleetSection || f["target_id"] != want {
				t.Fatalf("args %q: 순위 %d = %v(%v), want %s", args, i, f["target_id"], f["section"], want)
			}
		}
		more := env.Findings[3]
		if more["section"] != dbFleetMoreSection || more["no_session_rows_total"] != 1 ||
			!strings.HasPrefix(more["no_session_rows"].([]string)[0], fltIdle) {
			t.Fatalf("행 없는 대상은 짧은 명단으로만: %+v", more)
		}
		if env.Truncated || env.Scopes[0].Total != 4 || env.Scopes[0].Returned != 4 {
			t.Fatalf("4개 전부 실렸으면 절단 아님: truncated=%v scopes=%+v", env.Truncated, env.Scopes)
		}
	}
}

func TestDBFleetDetailRowsAndRefs(t *testing.T) {
	env := fltCall(t, fltCH(t, fltScripts(), nil), fltInventoryPG(t), `{}`)
	ora, pg, my := env.Findings[0], env.Findings[1], env.Findings[2]
	if ora["blocked_peak"] != 9 || ora["status"] != "anomalous" || ora["engine"] != "oracle" || ora["name"] != "Oracle-corebanking" ||
		ora["first_seen"] != "2026-08-31T19:52:21Z" || ora["last_seen"] != "2026-08-31T19:55:28Z" {
		t.Fatalf("Oracle 행: %+v", ora)
	}
	if hs := ora["top_sessions"].([]Finding); len(hs) != 1 || hs[0]["max_wait_ms"] != 197506.4 || hs[0]["session"] != "228" {
		t.Fatalf("Oracle 경과 키는 wait_ms: %+v", hs)
	}
	// PG: 같은 (클라이언트, 상태 키, sql_key) 세션 둘은 한 묶음.
	hs := pg["top_sessions"].([]Finding)
	if len(hs) != 2 || hs[0]["state"] != "idle in transaction (blocking) | Client:ClientRead" || hs[1]["sessions_same_key"] != 2 {
		t.Fatalf("PG 경과 상위: %+v", hs)
	}
	if my["blocking_events"] != 0 || my["blocked_peak"] != nil || my["top_sessions"].([]Finding)[0]["max_tran"] != 12.62 {
		t.Fatalf("MySQL 세션 행: %+v", my)
	}
	// ref 관례 — 단일 DB 봉투와 같은 좌표 문자열(사건 대표 폴·보유 세션·폴 수).
	wantRefs := []string{
		"ch:dpm_session_local:" + fltOra + ":2026-08-31T19:55:28Z", // 동수면 가장 늦은 폴이 대표
		"ch:dpm_session_local:" + fltOra + ":2026-08-31T19:53:04Z:session:228",
		"ch:dpm_session_local:" + fltPG + ":2026-08-31T19:53:04Z:session:63197",
		"ch:dpm_session_local:" + fltMy + ":polls:1",
	}
	var nested []string
	for _, f := range env.Findings {
		nested = append(nested, f["refs"].([]string)...)
	}
	all := strings.Join(nested, " ")
	for _, w := range wantRefs {
		if !strings.Contains(all, w) {
			t.Fatalf("행 refs에 %s 없음: %v", w, nested)
		}
	}
	// 봉투 refs는 실린 대상마다 대표 관측 좌표 하나(사건 대표 폴 또는 폴 수).
	if len(env.Refs) != 3 || env.Refs[0] != wantRefs[0] || env.Refs[2] != wantRefs[3] {
		t.Fatalf("봉투 refs: %v", env.Refs)
	}
	for _, f := range env.Findings[:3] {
		if refs, _ := f["refs"].([]string); len(refs) == 0 {
			t.Fatalf("행마다 refs: %+v", f)
		}
	}
	// 요약 — 순위대로 target_id가 나오고 드릴다운 경로를 밝힌다. 응답 앞부분(6000자)에 요약이 있다.
	oi, pi, mi := strings.Index(env.Summary, fltOra), strings.Index(env.Summary, fltPG), strings.Index(env.Summary, fltMy)
	if oi < 0 || pi < oi || mi < pi || !strings.Contains(env.Summary, "target_id를 db로 다시 부른다") ||
		!strings.Contains(env.Summary, "블로킹 관측 2개(막힌 세션 3개 이상 2개)") || !strings.Contains(env.Summary, "창 내 세션 행 0 1개") {
		t.Fatalf("요약: %s", env.Summary)
	}
	b, _ := json.Marshal(env)
	if s := string(b); strings.Index(s, `"summary"`) > 1500 || len([]rune(s)) > dbFleetCharBudget || strings.Contains(s, `\u003`) {
		t.Fatalf("요약 위치 %d, 전체 %d자", strings.Index(s, `"summary"`), len([]rune(s)))
	}
}

func fltSynth(id string, class dbFleetClass, peak, blkPolls int, ageUnit string, age float64) dbFleetDB {
	d := dbFleetDB{t: inventoryTarget{ID: id, Name: "db-" + id[:4]}, class: class, peak: peak, blkPolls: blkPolls, engines: "postgresql"}
	if class != dbFleetQuiet && class != dbFleetFailed {
		d.polls = 4
	}
	if age > 0 {
		k := dbSessKeys{ageField: "tran_ms", ageUnit: ageUnit}
		d.holders = []dbFleetHolder{{g: &dbSessGroup{rep: dbSessHolder{sid: "1", maxAge: age, maxAt: fltLast}, n: 1}, k: k}}
	}
	if class == dbFleetBlocking {
		ps := []dbBlkPoll{{ts: fltFirst, blockedN: peak}, {ts: fltLast, blockedN: peak}}
		d.events = []dbBlkEvent{{engine: "postgresql", polls: ps, peak: peak}}
		d.first, d.last = fltFirst, fltLast
	}
	return d
}

func fltID(n int) string { return fmt.Sprintf("%08x-0000-4000-8000-000000000000", n) }

// 순위 — 구획 → 최대 막힌 세션 → 블로킹 폴 수 → 최장 세션 경과(ms 확인 engine 우선) → target_id.
func TestDBFleetRanking(t *testing.T) {
	dbs := []dbFleetDB{
		fltSynth(fltID(1), dbFleetQuiet, 0, 0, "", 0),
		fltSynth(fltID(2), dbFleetBlocking, 3, 2, "ms", 10),
		fltSynth(fltID(3), dbFleetSessions, 0, 0, "ms", 1000),
		fltSynth(fltID(4), dbFleetBlocking, 3, 5, "ms", 10),
		fltSynth(fltID(5), dbFleetSessions, 0, 0, "단위 미확인", 99999),
		fltSynth(fltID(6), dbFleetBlocking, 10, 1, "ms", 1),
		fltSynth(fltID(7), dbFleetSessions, 0, 0, "ms", 5000),
		fltSynth(fltID(8), dbFleetFailed, 0, 0, "", 0),
		fltSynth(fltID(9), dbFleetPlain, 0, 0, "", 0),
		fltSynth(fltID(10), dbFleetNoAxis, 0, 0, "", 0),
	}
	dbFleetRank(dbs)
	want := []int{6, 4, 2, 7, 3, 5, 9, 10, 8, 1}
	for i, n := range want {
		if dbs[i].t.ID != fltID(n) {
			got := make([]string, len(dbs))
			for j, d := range dbs {
				got[j] = d.t.ID[:8]
			}
			t.Fatalf("순위 %d: got %v, want %v", i, got, want)
		}
	}
}

// 상한·예산 — 수십 개 대상이어도 응답 전체가 예산 안이고, 잘렸으면 truncated·scopes가 밝힌다.
func TestDBFleetCapsAndBudget(t *testing.T) {
	var dbs []dbFleetDB
	for i := 0; i < 40; i++ {
		class, peak := dbFleetSessions, 0
		switch {
		case i < 3:
			class, peak = dbFleetBlocking, 20-i
		case i >= 28:
			class = dbFleetQuiet
		}
		d := fltSynth(fltID(100+i), class, peak, 4, "ms", float64(1000*(40-i)))
		d.t.Name = strings.Repeat("long-database-name-", 3) + fmt.Sprint(i)
		if len(d.holders) > 0 {
			d.holders[0].g.rep.client = "testbed-transfer-58ffd457c7-vk8pq"
			d.holders[0].g.rep.state = "ACTIVE"
			d.holders[0].g.rep.wc, d.holders[0].g.rep.wev = "Application", "enq: TX - row lock contention"
			d.holders = append(d.holders, d.holders[0])
		}
		dbs = append(dbs, d)
	}
	dbFleetRank(dbs)
	env := dbFleetBuild(dbs, dbFleetInv{}, &TimeRange{From: fltFirst, To: fltLast}, "from=인자, to=인자", false)
	b, _ := json.Marshal(env)
	if n := len([]rune(string(b))); n > dbFleetCharBudget {
		t.Fatalf("예산 초과: %d자", n)
	}
	if !env.Truncated || env.Scopes[0].Total != 40 || env.Scopes[0].Returned >= 40 {
		t.Fatalf("잘렸으면 truncated·scopes: %v %+v", env.Truncated, env.Scopes)
	}
	if env.Findings[0]["target_id"] != fltID(100) {
		t.Fatalf("가장 큰 블로킹이 첫 행: %v", env.Findings[0]["target_id"])
	}
	// 블로킹 대상은 상세 행이든 짧은 줄이든 반드시 이름(target_id)이 실린다.
	s := string(b)
	for i := 0; i < 3; i++ {
		if !strings.Contains(s, fltID(100+i)) {
			t.Fatalf("블로킹 대상 %s가 응답에 없음", fltID(100+i))
		}
	}
	more := env.Findings[len(env.Findings)-1]
	if more["section"] != dbFleetMoreSection || more["no_session_rows_total"] != 12 || more["omitted"] == nil {
		t.Fatalf("나머지 구획: %+v", more)
	}
	// 단계 축소가 실제로 일어났다(상세 5행이면 예산을 넘는 입력).
	detail := strings.Count(s, `"section":"fleet_db"`)
	if detail >= dbFleetCaps[0][0] || detail < 1 {
		t.Fatalf("상세 행 %d", detail)
	}
	t.Logf("대상 40개: 응답 %d자, 상세 행 %d, scopes %+v, 요약 위치 %d", len([]rune(s)), detail, env.Scopes[0], strings.Index(s, `"summary"`))
}

// 명부를 못 보면(PG 미연결·연결 실패) 세션 원천의 창 내 target_id로 대체 발견하고 결손을 밝힌다.
func TestDBFleetInventoryFallback(t *testing.T) {
	common := []dgCHScript{{"AS fleet_rows", []map[string]any{{"target": fltOra, "fleet_rows": 20}, {"target": fltPG, "fleet_rows": 20}}}}
	for name, pg := range map[string]*sql.DB{"nil": nil, "dead": dgDeadPG(t)} {
		dgResetBreaker()
		env := fltCall(t, fltCH(t, fltScripts(), common), pg, `{}`)
		if env.Status != "anomalous" || env.Findings[0]["target_id"] != fltOra || env.Findings[1]["target_id"] != fltPG {
			t.Fatalf("%s: 대체 발견 후 순위: %+v", name, env.Findings)
		}
		more := env.Findings[len(env.Findings)-1]
		se, _ := more["source_errors"].(map[string]string)
		if more["section"] != dbFleetMoreSection || se["pg_inventory"] == "" || !dgHasSourceErr(env, "pg") {
			t.Fatalf("%s: 명부 결손 명시: %+v", name, more)
		}
		if !strings.Contains(env.Summary, "명부 조회 불가") || !strings.Contains(env.AssessmentBasis, "대체") {
			t.Fatalf("%s: 요약·근거에 대체 발견: %s / %s", name, env.Summary, env.AssessmentBasis)
		}
	}
}

// 세션 원천(CH)은 required — 전 대상 조회 실패면 봉투가 아니라 BackendError(가드가 backend_error로 사상).
func TestDBFleetDeadCH(t *testing.T) {
	dgResetBreaker()
	t.Cleanup(dgResetBreaker)
	_, err := NewDBBlockingTool(&CH{BaseURL: dgDead(t), Database: "lucida"}, fltInventoryPG(t), fltFirst, fltLast, nil).
		Call(context.Background(), json.RawMessage(`{}`))
	var be *BackendError
	if !errors.As(err, &be) || be.Backend != "ch" {
		t.Fatalf("CH 전면 실패는 BackendError(ch): %v", err)
	}
}

// 명부가 비면 no_data(not_collected) — 대상 자체가 없다.
func TestDBFleetEmptyInventory(t *testing.T) {
	p := newDgFakePG(t)
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	env := fltCall(t, fltCH(t, nil, nil), db, `{}`)
	if env.Status != "no_data" || env.NoDataReason != NoDataNotCollected || len(env.Findings) != 0 {
		t.Fatalf("빈 명부: %+v", env)
	}
}

// 인자 오류 문구 — db가 선택 인자임을 밝힌다.
func TestDBFleetArgErrors(t *testing.T) {
	tool := NewDBBlockingTool(fltCH(t, nil, nil), nil, fltFirst, fltLast, nil)
	for _, args := range []string{`{"db":"not-a-uuid"}`, `{"db":1}`} {
		_, err := tool.Call(context.Background(), json.RawMessage(args))
		if err == nil || !strings.Contains(err.Error(), "생략") {
			t.Fatalf("%s: %v", args, err)
		}
	}
}
