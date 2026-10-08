// db_blocking의 세션 상태 구획 — 블로킹 사건이 없어도 "누가 어떤 상태로 이 DB에
// 붙어 있었나"를 싣는다(2026-10-06 추가).
//
// 왜 필요한가(하네스 대 원시 SQL 절제 실측, 같은 교사·블라인드): 원시 팔은
// dpm_session_local을 직접 집계해 두 근본 원인을 찾았고 도구 팔은 못 찾았다.
//   - 커넥션을 잡은 채 외부 호출을 하는 서비스: DB 쪽에는 그 클라이언트(pod IP)의
//     "idle in transaction / ClientRead" 세션이 폴마다 여럿 보인다. 블로킹이 아니라
//     db_blocking은 아무것도 보여 주지 않았고, read_timeseries의 DB 전체 세션 수
//     지표는 클라이언트를 가르지 못했다.
//   - 행 잠금을 쥔 채 놀고 있는 세션: 블로킹 루트로는 보였지만 세션 번호와 SQL
//     해시뿐이라 무슨 문장으로 잡았는지(db_sql_text)·어느 클라이언트인지가 없었다.
//
// 판정은 하지 않는다. 상태·대기 값은 원천 문자열 그대로 묶음 키로만 쓰고, 순위는
// 수치(세션 수·경과 시간)로만 매긴다. 배경 세션 제외는 엔진 카탈로그의 열거값
// (pg backend_type, Oracle v$session.type, MySQL command)으로만 한다.
package tools

import (
	"context"
	"database/sql"
	"fmt"
	"net"
	"sort"
	"strings"
	"time"
)

const (
	dbSessMaxClients = 5     // 클라이언트 표시 상한(세션-폴 수 순)
	dbSessMaxStates  = 3     // 클라이언트당 상태 키 상한(나머지는 other_states_session_polls로 합산)
	dbSessMaxOverall = 4     // 전체 상태 분포 상한
	dbSessMaxHolders = 4     // 경과 시간 상위 묶음 상한
	dbSessMaxBuckets = 8     // 시간 버킷 상한
	dbSessSQLChars   = 200   // SQL 본문 절단 길이(rune)
	dbSessRowCap     = 20000 // 집계 행 상한(폴×클라이언트×상태)
	dbSessHolderScan = 200   // 경과 시간 후보 조회 상한
)

// dbSessKeys는 engine별 세션 행 계약이다(생산자 body 키 — dbBlkEngines와 같은 정본).
// 식은 정적 문자열만 조립한다(사용자 입력이 들어가지 않는다).
type dbSessKeys struct {
	id, client, state, stype, waitCls, wait, age, sqlKey, user string
	userOnly                                                   string // 사용자 세션 술어 — 엔진 카탈로그 열거값만(배경 세션 제외)
	ageKey                                                     string // 원천 body 키 이름(봉투에 그대로 밝힌다)
	ageField                                                   string // 응답 필드 이름(단위 접미)
	ageUnit                                                    string
	contract                                                   string // verified(실측) | unverified(계약만)
}

func dbSessJStr(k string) string   { return "JSONExtractString(body, '" + k + "')" }
func dbSessJFloat(k string) string { return "JSONExtractFloat(body, '" + k + "')" }
func dbSessJIntS(k string) string  { return "toString(JSONExtractInt(body, '" + k + "'))" }

var dbSessOracle = dbSessKeys{
	id: dbSessJIntS("sid"), client: dbSessJStr("machine"), state: dbSessJStr("status"), stype: "''",
	waitCls: dbSessJStr("waitClass"), wait: dbSessJStr("event"),
	// Oracle 원천에는 트랜잭션 경과 키가 없다 — 현재 대기 경과(waitTime, ms)로 순위를 매기고 그렇게 밝힌다.
	age: dbSessJFloat("waitTime"), sqlKey: dbSessJStr("sqlId"), user: dbSessJStr("schema"),
	userOnly: dbSessJStr("type") + " != 'BACKGROUND'",
	ageKey:   "waitTime", ageField: "wait_ms", ageUnit: "ms", contract: "verified",
}

var dbSessMySQL = dbSessKeys{
	id: dbSessJIntS("tid"), client: dbSessJStr("hostname"), state: dbSessJStr("command"), stype: dbSessJStr("sessionType"),
	waitCls: "''", wait: dbSessJStr("state"),
	age: dbSessJFloat("tranTime"), sqlKey: dbSessJIntS("sqlHash"), user: dbSessJStr("user"),
	userOnly: dbSessJStr("command") + " != 'Daemon'",
	ageKey:   "tranTime", ageField: "tran", ageUnit: "단위 미확인", contract: "unverified",
}

var dbSessEngines = map[string]dbSessKeys{
	"postgresql": {
		id: dbSessJIntS("pid"), client: dbSessJStr("ip"),
		state: "if(log_attributes['state'] != '', log_attributes['state'], " + dbSessJStr("sessionType") + ")",
		stype: dbSessJStr("sessionType"), waitCls: dbSessJStr("waitType"), wait: dbSessJStr("waitEvent"),
		age: dbSessJFloat("tranTime"), sqlKey: dbSessJIntS("sqlHash"), user: dbSessJStr("user"),
		userOnly: dbSessJStr("backendType") + " IN ('', 'client backend')",
		ageKey:   "tranTime", ageField: "tran_ms", ageUnit: "ms", contract: "verified",
	},
	"oracle":  dbSessOracle,
	"tibero":  func() dbSessKeys { k := dbSessOracle; k.contract = "unverified"; return k }(),
	"mysql":   dbSessMySQL,
	"mariadb": dbSessMySQL,
}

// dbSessResult는 db_blocking 봉투에 합류할 구획이다.
type dbSessResult struct {
	Findings     []Finding
	Refs         []string
	Digest       string
	Truncated    bool
	SourceErrors map[string]string
}

type dbSessAgg struct {
	ts                            time.Time
	client, state, stype, wc, wev string
	sessions                      int
	maxAge                        float64
	users                         []string
}

type dbSessHolder struct {
	sid, client, state, stype, wc, wev, sqlKey, user string
	maxAge                                           float64
	maxAt, first, last                               time.Time
	polls                                            int
}

// dbSessStateKey는 묶음 키다 — 원천 문자열 그대로, 판정 없음.
func dbSessStateKey(state, stype, wc, wev string) string {
	k := state
	if stype != "" && stype != state {
		k += " (" + stype + ")"
	}
	switch {
	case wc != "" && wev != "" && wc != wev:
		k += " | " + wc + ":" + wev
	case wev != "":
		k += " | " + wev
	case wc != "":
		k += " | " + wc
	default:
		k += " | -"
	}
	return k
}

// dbSessBuckets는 폴 시각 → 버킷 위치다. 폴이 상한 이하면 폴 하나가 버킷 하나다.
type dbSessBuckets struct {
	labels []string
	widthS int
	of     func(time.Time) int
}

func dbSessMakeBuckets(polls []time.Time, from, to time.Time) dbSessBuckets {
	if len(polls) <= dbSessMaxBuckets {
		idx := map[time.Time]int{}
		b := dbSessBuckets{}
		for i, p := range polls {
			idx[p] = i
			b.labels = append(b.labels, p.UTC().Format(time.RFC3339))
		}
		b.of = func(t time.Time) int { return idx[t] }
		return b
	}
	secs := (int64(to.Sub(from)/time.Second) + dbSessMaxBuckets - 1) / dbSessMaxBuckets // 올림 — 초 단위 폭
	if secs < 1 {
		secs = 1
	}
	width := time.Duration(secs) * time.Second
	b := dbSessBuckets{widthS: int(width / time.Second)}
	for i := 0; i < dbSessMaxBuckets; i++ {
		b.labels = append(b.labels, from.Add(time.Duration(i)*width).UTC().Format(time.RFC3339))
	}
	b.of = func(t time.Time) int {
		i := int(t.Sub(from) / width)
		if i < 0 {
			i = 0
		}
		if i >= dbSessMaxBuckets {
			i = dbSessMaxBuckets - 1
		}
		return i
	}
	return b
}

// dbSessPeaks는 폴별 세션 수를 버킷별 최대값(동시 세션 수의 버킷 내 최대)으로 접는다.
func dbSessPeaks(perPoll map[time.Time]int, b dbSessBuckets) []int {
	out := make([]int, len(b.labels))
	for t, n := range perPoll {
		if i := b.of(t); n > out[i] {
			out[i] = n
		}
	}
	return out
}

func dbSessMaxPoll(perPoll map[time.Time]int) int {
	m := 0
	for _, n := range perPoll {
		if n > m {
			m = n
		}
	}
	return m
}

func dbSessRound(v float64) float64 { return round3(v) }

// dbSessCollect는 엔진별 세션 상태 구획을 만든다. 실패는 구획 안 결손으로 밝힌다 —
// 블로킹 판정(봉투 status)에는 영향을 주지 않는다.
func dbSessCollect(ctx context.Context, ch *CH, pg *sql.DB, target string, from, to time.Time,
	engines []string, acc *truncAcc) dbSessResult {
	res := dbSessResult{SourceErrors: map[string]string{}}
	for _, eng := range engines {
		keys, ok := dbSessEngines[eng]
		if !ok {
			continue
		}
		dbSessEngine(ctx, ch, pg, target, eng, keys, from, to, acc, &res)
	}
	return res
}

func dbSessEngine(ctx context.Context, ch *CH, pg *sql.DB, target, eng string, k dbSessKeys,
	from, to time.Time, acc *truncAcc, res *dbSessResult) {
	params, where := dbSessScope(target, eng, from, to)

	aggRaw, tr, err := ch.Query(ctx, `
		SELECT toString(timestamp) AS ts, `+k.client+` AS client, `+k.state+` AS state, `+k.stype+` AS stype,
		       `+k.waitCls+` AS wait_class, `+k.wait+` AS wait,
		       uniqExact(`+k.id+`) AS sess_n, max(`+k.age+`) AS max_age, groupUniqArray(3)(`+k.user+`) AS users
		FROM dpm_session_local `+where+` AND (`+k.userOnly+`)
		GROUP BY ts, client, state, stype, wait_class, wait
		ORDER BY ts, client
		LIMIT `+fmt.Sprint(dbSessRowCap+1), params)
	if err != nil {
		tok := beDetail(err)
		res.SourceErrors["ch_sessions"] = tok
		res.Findings = append(res.Findings, Finding{
			"section": "session_clients", "engine": eng, "source_errors": map[string]string{"ch_sessions": tok},
			"note": "세션 상태 집계 조회 실패 — 이 구획은 결손이다(세션이 없었다는 뜻이 아니다). 블로킹 사건 관측은 별개다",
		})
		return
	}
	acc.note(tr)
	capped := len(aggRaw) > dbSessRowCap
	if capped {
		aggRaw = aggRaw[:dbSessRowCap]
	}
	var aggs []dbSessAgg
	pollSet := map[time.Time]bool{}
	for _, r := range aggRaw {
		ts, ok := chParseTS(r["ts"])
		if !ok {
			continue
		}
		a := dbSessAgg{ts: ts, client: chStr(r["client"]), state: chStr(r["state"]), stype: chStr(r["stype"]),
			wc: chStr(r["wait_class"]), wev: chStr(r["wait"]), sessions: asInt(r["sess_n"]), maxAge: asFloat(r["max_age"])}
		if us, ok := r["users"].([]any); ok {
			for _, u := range us {
				if s := fmt.Sprint(u); s != "" {
					a.users = append(a.users, s)
				}
			}
		}
		aggs = append(aggs, a)
		pollSet[ts] = true
	}
	// 배경 세션 행 수 — 제외한 사실을 숫자로 밝힌다(단일 행 집계, 절단 불가 표면).
	bg := 0
	if bgRows, _, err := ch.Query(ctx, `SELECT countIf(NOT (`+k.userOnly+`)) AS bg FROM dpm_session_local `+where, params); err == nil && len(bgRows) == 1 {
		bg = asInt(bgRows[0]["bg"])
	}
	if len(aggs) == 0 {
		res.Findings = append(res.Findings, Finding{
			"section": "session_clients", "engine": eng, "user_session_polls": 0, "background_rows_excluded": bg,
			"note": "창 안 사용자 세션 행 0 — 배경 세션만 수집됐거나 수집 대상 상태의 세션이 없었다",
		})
		return
	}

	holders, err := dbSessQueryHolders(ctx, ch, k, where, params, acc)
	if err != nil {
		res.SourceErrors["ch_holders"] = beDetail(err)
	}
	sessionsTotal := len(holders)
	groups := dbSessGroupHolders(holders)
	groupsTotal := len(groups)
	if len(groups) > dbSessMaxHolders {
		groups = groups[:dbSessMaxHolders]
		res.Truncated = true
	}

	polls := make([]time.Time, 0, len(pollSet))
	for t := range pollSet {
		polls = append(polls, t)
	}
	sort.Slice(polls, func(i, j int) bool { return polls[i].Before(polls[j]) })
	b := dbSessMakeBuckets(polls, from, to)

	clients, overall := dbSessFold(aggs)
	clientsTotal := len(clients)
	if len(clients) > dbSessMaxClients {
		clients = clients[:dbSessMaxClients]
		res.Truncated = true
	}

	// 해석 대상 = 표시 클라이언트 ∪ 표시 보유 세션의 클라이언트.
	want := []string{}
	seen := map[string]bool{}
	for _, c := range clients {
		if !seen[c.client] {
			seen[c.client] = true
			want = append(want, c.client)
		}
	}
	for _, g := range groups {
		if !seen[g.rep.client] {
			seen[g.rep.client] = true
			want = append(want, g.rep.client)
		}
	}
	resolved, resRefs := dbSessResolve(ctx, ch, pg, want, from, to, res.SourceErrors)

	// SQL 본문(사전) — 표시 묶음 대표 세션의 sql_key만.
	var keysWanted []string
	for _, g := range groups {
		if g.rep.sqlKey != "" && g.rep.sqlKey != "0" {
			keysWanted = append(keysWanted, g.rep.sqlKey)
		}
	}
	texts := dbSessSQLTexts(ctx, pg, target, keysWanted, res.SourceErrors)

	hf, hrefs := dbSessHoldersFinding(target, eng, k, groups, groupsTotal, sessionsTotal, resolved, texts)
	cf, crefs := dbSessClientsFinding(target, eng, k, clients, clientsTotal, overall, b, polls, resolved, from, to, bg, capped)
	if len(res.SourceErrors) > 0 {
		se := map[string]string{}
		for kk, v := range res.SourceErrors {
			se[kk] = v
		}
		hf["source_errors"] = se
		cf["source_errors"] = se
	}
	hf["refs"] = hrefs
	allClientRefs := append(crefs, resRefs...)
	cf["refs"] = allClientRefs
	res.Findings = append(res.Findings, hf, cf)
	res.Refs = append(res.Refs, hrefs...)
	res.Refs = append(res.Refs, allClientRefs...)
	res.Digest = dbSessDigest(eng, k, groups, clients, resolved, texts, len(polls))
}

// dbSessWhere는 세션 상태 조회의 공통 범위(대상·engine·창)다 — 단일 DB 구획과 전체 개관이 같은 범위를 쓴다.
const dbSessWhere = `WHERE target_id = {target:String} AND engine = {engine:String}
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)`

func dbSessScope(target, eng string, from, to time.Time) (map[string]string, string) {
	return map[string]string{"target": target, "engine": eng, "from": chTime(from), "to": chTime(to)}, dbSessWhere
}

// dbSessQueryHolders는 경과 시간 상위 세션 후보(세션별 창 내 최대 경과, 내림차순)를 읽는다.
// 실패면 nil과 오류 — 호출부가 결손을 밝힌다(구획 source_errors 또는 개관 행의 결손 표시).
func dbSessQueryHolders(ctx context.Context, ch *CH, k dbSessKeys, where string, params map[string]string, acc *truncAcc) ([]dbSessHolder, error) {
	hRaw, tr, err := ch.Query(ctx, `
		SELECT `+k.id+` AS sid, `+k.client+` AS client, max(`+k.age+`) AS max_age,
		       toString(argMax(timestamp, `+k.age+`)) AS max_at,
		       argMax(`+k.state+`, `+k.age+`) AS state, argMax(`+k.stype+`, `+k.age+`) AS stype,
		       argMax(`+k.waitCls+`, `+k.age+`) AS wait_class, argMax(`+k.wait+`, `+k.age+`) AS wait,
		       argMax(`+k.sqlKey+`, `+k.age+`) AS sql_key, argMax(`+k.user+`, `+k.age+`) AS usr,
		       toString(min(timestamp)) AS first_ts, toString(max(timestamp)) AS last_ts, uniqExact(timestamp) AS polls
		FROM dpm_session_local `+where+` AND (`+k.userOnly+`) AND `+k.age+` > 0
		GROUP BY sid, client
		ORDER BY max_age DESC, sid
		LIMIT `+fmt.Sprint(dbSessHolderScan), params)
	if err != nil {
		return nil, err
	}
	acc.note(tr)
	var holders []dbSessHolder
	for _, r := range hRaw {
		h := dbSessHolder{sid: chStr(r["sid"]), client: chStr(r["client"]), state: chStr(r["state"]), stype: chStr(r["stype"]),
			wc: chStr(r["wait_class"]), wev: chStr(r["wait"]), sqlKey: chStr(r["sql_key"]), user: chStr(r["usr"]),
			maxAge: asFloat(r["max_age"]), polls: asInt(r["polls"])}
		h.maxAt, _ = chParseTS(r["max_at"])
		h.first, _ = chParseTS(r["first_ts"])
		h.last, _ = chParseTS(r["last_ts"])
		holders = append(holders, h)
	}
	return holders, nil
}

// dbSessHolderRef는 보유 세션의 좌표 ref다(그 세션이 최대 경과를 찍은 폴).
func dbSessHolderRef(target string, h dbSessHolder) string {
	return fmt.Sprintf("ch:dpm_session_local:%s:%s:session:%s", target, h.maxAt.UTC().Format(time.RFC3339), h.sid)
}

// dbSessGroup은 같은 (클라이언트, 상태 키, sql_key) 세션 묶음이다 — 같은 속성 튜플을
// 접는 구조 접기다(대표 = 묶음 안 경과 최대 세션).
type dbSessGroup struct {
	rep    dbSessHolder
	n      int
	others []string
}

func dbSessGroupHolders(hs []dbSessHolder) []*dbSessGroup {
	by := map[string]*dbSessGroup{}
	var out []*dbSessGroup
	for _, h := range hs { // hs는 경과 내림차순 — 처음 본 세션이 대표
		key := h.client + "\x00" + dbSessStateKey(h.state, h.stype, h.wc, h.wev) + "\x00" + h.sqlKey
		g, ok := by[key]
		if !ok {
			g = &dbSessGroup{rep: h}
			by[key] = g
			out = append(out, g)
		} else if len(g.others) < 3 {
			g.others = append(g.others, h.sid)
		}
		g.n++
	}
	return out
}

// dbSessClient는 클라이언트 하나의 접기 결과다.
type dbSessClient struct {
	client       string
	sessionPolls int
	perPoll      map[time.Time]int
	maxAge       float64
	users        map[string]bool
	states       map[string]*dbSessState
}

type dbSessState struct {
	key          string
	perPoll      map[time.Time]int
	sessionPolls int
	maxAge       float64
}

func dbSessAddState(m map[string]*dbSessState, key string, a dbSessAgg) {
	s, ok := m[key]
	if !ok {
		s = &dbSessState{key: key, perPoll: map[time.Time]int{}}
		m[key] = s
	}
	s.perPoll[a.ts] += a.sessions
	s.sessionPolls += a.sessions
	if a.maxAge > s.maxAge {
		s.maxAge = a.maxAge
	}
}

func dbSessSortedStates(m map[string]*dbSessState) []*dbSessState {
	out := make([]*dbSessState, 0, len(m))
	for _, s := range m {
		out = append(out, s)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].sessionPolls != out[j].sessionPolls {
			return out[i].sessionPolls > out[j].sessionPolls
		}
		return out[i].key < out[j].key
	})
	return out
}

// dbSessFold는 집계 행을 클라이언트별·전체 상태별로 접는다. 클라이언트 순위는
// 세션-폴 수(폴마다 센 세션 수의 합) 내림차순 — 수치 순위만.
func dbSessFold(aggs []dbSessAgg) ([]*dbSessClient, []*dbSessState) {
	byClient := map[string]*dbSessClient{}
	overall := map[string]*dbSessState{}
	for _, a := range aggs {
		c, ok := byClient[a.client]
		if !ok {
			c = &dbSessClient{client: a.client, perPoll: map[time.Time]int{}, users: map[string]bool{}, states: map[string]*dbSessState{}}
			byClient[a.client] = c
		}
		key := dbSessStateKey(a.state, a.stype, a.wc, a.wev)
		c.sessionPolls += a.sessions
		c.perPoll[a.ts] += a.sessions
		if a.maxAge > c.maxAge {
			c.maxAge = a.maxAge
		}
		for _, u := range a.users {
			c.users[u] = true
		}
		dbSessAddState(c.states, key, a)
		dbSessAddState(overall, key, a)
	}
	clients := make([]*dbSessClient, 0, len(byClient))
	for _, c := range byClient {
		clients = append(clients, c)
	}
	sort.Slice(clients, func(i, j int) bool {
		if clients[i].sessionPolls != clients[j].sessionPolls {
			return clients[i].sessionPolls > clients[j].sessionPolls
		}
		return clients[i].client < clients[j].client
	})
	return clients, dbSessSortedStates(overall)
}

// ── 클라이언트 해석(IP·호스트 → pod → 워크로드·애플리케이션) ───────────────

type dbSessRes struct {
	status                        string // resolved | ambiguous | unresolved | not_queried
	basis                         string // pod_ip | pod_name
	pod, namespace, workload, app string
	workloadTarget, appTgt        string
	candidates                    int
}

type dbSessPodRow struct {
	cluster, ns, name, ip, created, ownerKind, owner, rsOwnerKind, rsOwner string
}

// dbSessClientAddr는 클라이언트 문자열을 IP(포트 제거) 또는 호스트 이름으로 가른다.
func dbSessClientAddr(c string) (ip, host string) {
	c = strings.TrimSpace(c)
	if c == "" {
		return "", ""
	}
	if h, _, err := net.SplitHostPort(c); err == nil {
		c = h
	}
	if net.ParseIP(c) != nil {
		return c, ""
	}
	return "", c
}

// dbSessResolve는 클라이언트를 현재 스냅샷의 pod로 푼다. 경로:
//
//	접속 IP → kcm_resources(kind='pod').data->>'podIp' (호스트 이름이면 pod 이름)
//	pod → 소유 ReplicaSet → Deployment(또는 StatefulSet·DaemonSet) — 정본 이름으로 대상 UUID
//	pod 이름 → otel_traces_local.host_name → service_name → application 대상(expand_topology와 같은 다리)
//
// 스냅샷이라 창 뒤에 생성된 pod는 후보에서 뺀다(그 IP의 당시 주인이 될 수 없다). 후보가
// 둘 이상이면 고르지 않는다(ambiguous). 실패는 errs에 분류 토큰으로 남긴다.
func dbSessResolve(ctx context.Context, ch *CH, pg *sql.DB, clients []string, from, to time.Time,
	errs map[string]string) (map[string]dbSessRes, []string) {
	out := map[string]dbSessRes{}
	if len(clients) == 0 {
		return out, nil
	}
	if pg == nil {
		for _, c := range clients {
			out[c] = dbSessRes{status: "not_queried"}
		}
		return out, nil
	}
	var ips, hosts []string
	for _, c := range clients {
		ip, host := dbSessClientAddr(c)
		if ip != "" {
			ips = append(ips, ip)
		} else if host != "" {
			hosts = append(hosts, host)
		}
	}
	rows, err := pg.QueryContext(ctx, `SELECT p.target_id::text, p.namespace, p.name,
		       coalesce(p.data->>'podIp', ''), coalesce(p.data->>'creationTimestamp', ''),
		       coalesce(p.data->'ownerReference'->>'ownerKind', ''), coalesce(p.data->'ownerReference'->>'ownerName', ''),
		       coalesce(rs.data->'ownerReference'->>'ownerKind', ''), coalesce(rs.data->'ownerReference'->>'ownerName', '')
		FROM kcm_resources p
		LEFT JOIN kcm_resources rs ON rs.kind = 'replicaset' AND rs.target_id = p.target_id
		     AND rs.namespace = p.namespace AND rs.name = p.data->'ownerReference'->>'ownerName'
		WHERE p.kind = 'pod' AND (coalesce(p.data->>'podIp', '') = ANY($1::text[]) OR p.name = ANY($2::text[]))`,
		dbSessNonNil(ips), dbSessNonNil(hosts))
	if err != nil {
		errs["pg_resolution"] = beDetail(pgErr(err))
		for _, c := range clients {
			out[c] = dbSessRes{status: "not_queried"}
		}
		return out, nil
	}
	byIP := map[string][]dbSessPodRow{}
	byName := map[string][]dbSessPodRow{}
	for rows.Next() {
		var p dbSessPodRow
		if err := rows.Scan(&p.cluster, &p.ns, &p.name, &p.ip, &p.created, &p.ownerKind, &p.owner, &p.rsOwnerKind, &p.rsOwner); err != nil {
			continue
		}
		// 창 뒤에 생성된 pod는 당시 클라이언트일 수 없다(RFC3339 Z 서식 — 시각 비교).
		if t, err := time.Parse(time.RFC3339, p.created); err == nil && !t.Before(to) {
			continue
		}
		if p.ip != "" {
			byIP[p.ip] = append(byIP[p.ip], p)
		}
		byName[p.name] = append(byName[p.name], p)
	}
	rows.Close()

	picked := map[string]dbSessPodRow{}
	var podNames []string
	targetNames := map[string]bool{}
	for _, c := range clients {
		ip, host := dbSessClientAddr(c)
		cands, basis := byIP[ip], "pod_ip"
		if ip == "" {
			cands, basis = byName[host], "pod_name"
		}
		switch len(cands) {
		case 0:
			out[c] = dbSessRes{status: "unresolved"}
		case 1:
			p := cands[0]
			picked[c] = p
			r := dbSessRes{status: "resolved", basis: basis, pod: p.name, namespace: p.ns, candidates: 1}
			kind, name := p.ownerKind, p.owner
			if p.ownerKind == "ReplicaSet" && p.rsOwner != "" {
				kind, name = p.rsOwnerKind, p.rsOwner
			}
			if name != "" {
				r.workload = kind + "/" + name
				targetNames[p.cluster+":"+strings.ToLower(kind)+":"+p.ns+"/"+name] = true
			}
			out[c] = r
			podNames = append(podNames, p.name)
		default:
			out[c] = dbSessRes{status: "ambiguous", basis: basis, candidates: len(cands)}
		}
	}
	if len(podNames) == 0 {
		return out, nil
	}
	var refs []string
	traced := false
	// pod → 서비스(트레이스 host_name). 실패해도 pod·워크로드 해석은 유지.
	svcOf := map[string]string{}
	if ch != nil {
		tr, _, err := ch.Query(ctx, `
			SELECT host_name AS pod, service_name AS svc, count() AS n
			FROM otel_traces_local
			WHERE timestamp >= {from:DateTime64(9)} AND timestamp < {to:DateTime64(9)}
			  AND has({pods:Array(String)}, host_name)
			GROUP BY pod, svc ORDER BY n DESC, pod, svc LIMIT 200`,
			map[string]string{"from": chTime(from), "to": chTime(to), "pods": chArray(podNames)})
		if err != nil {
			errs["ch_traces"] = beDetail(err)
		} else {
			for _, r := range tr {
				pod := chStr(r["pod"])
				if _, dup := svcOf[pod]; !dup && chStr(r["svc"]) != "" {
					svcOf[pod] = chStr(r["svc"])
					targetNames[chStr(r["svc"])] = true
				}
			}
		}
	}
	// 정본 이름 → 대상 UUID(pod·워크로드·application).
	names := make([]string, 0, len(targetNames))
	for n := range targetNames {
		names = append(names, n)
	}
	sort.Strings(names)
	idOf := map[string]string{}
	appOf := map[string]string{}
	if trows, err := pg.QueryContext(ctx, `SELECT id::text, name, type::text FROM targets WHERE name = ANY($1::text[])`, names); err != nil {
		errs["pg_resolution"] = beDetail(pgErr(err))
	} else {
		for trows.Next() {
			var id, name, typ string
			if trows.Scan(&id, &name, &typ) == nil {
				if typ == "application" {
					appOf[name] = id
				} else {
					idOf[name] = id
				}
			}
		}
		trows.Close()
	}
	for c, p := range picked {
		r := out[c]
		if r.workload != "" {
			kind, name, _ := strings.Cut(r.workload, "/")
			r.workloadTarget = idOf[p.cluster+":"+strings.ToLower(kind)+":"+p.ns+"/"+name]
		}
		if svc := svcOf[p.name]; svc != "" {
			r.app = svc
			r.appTgt = appOf[svc]
			traced = true
		}
		refs = append(refs, fmt.Sprintf("pg:kcm_resources:%s:pod:%s/%s", p.cluster, p.ns, p.name))
		out[c] = r
	}
	sort.Strings(refs)
	if traced { // pod 이름 → service_name 다리는 한 조회 — ref 하나로 인용한다
		refs = append(refs, fmt.Sprintf("ch:otel_traces_local:host_name->service_name:%s/%s",
			from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339)))
	}
	return out, refs
}

func dbSessNonNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// dbSessLabel은 해석 결과의 짧은 표기다(application > 워크로드 > pod).
func dbSessLabel(r dbSessRes) string {
	switch {
	case r.status != "resolved":
		return ""
	case r.app != "":
		return r.app
	case r.workload != "":
		return r.workload
	}
	return r.namespace + "/" + r.pod
}

func dbSessApplyLabel(f Finding, resolved map[string]dbSessRes, client string) {
	r, ok := resolved[client]
	if !ok {
		f["client_resolution"] = "not_queried"
		return
	}
	f["client_resolution"] = r.status
	if l := dbSessLabel(r); l != "" {
		f["client_label"] = l
	}
}

// dbSessResMap은 해석된 클라이언트의 상세(대상 UUID 포함)다 — 항목마다 반복하지 않고 한 번만.
func dbSessResMap(resolved map[string]dbSessRes) map[string]any {
	out := map[string]any{}
	for c, r := range resolved {
		switch r.status {
		case "resolved":
			m := map[string]any{"pod": r.namespace + "/" + r.pod, "match_basis": r.basis}
			if r.workload != "" {
				m["workload"] = r.workload
			}
			if r.workloadTarget != "" {
				m["workload_target_id"] = r.workloadTarget
			}
			if r.app != "" {
				m["app"] = r.app
			}
			if r.appTgt != "" {
				m["app_target_id"] = r.appTgt
			}
			out[c] = m
		case "ambiguous":
			out[c] = map[string]any{"candidates": r.candidates, "match_basis": r.basis}
		}
	}
	return out
}

type dbSessText struct {
	text string
	full int
	cut  bool
}

// dbSessSQLTexts는 db_sql_text 사전에서 본문을 읽는다(키 = 대상·sql_key). 없는 키는
// 맵에 없다 — "사전에 없음"과 "사전을 못 봄"(errs)을 구분한다.
func dbSessSQLTexts(ctx context.Context, pg *sql.DB, target string, keys []string, errs map[string]string) map[string]dbSessText {
	out := map[string]dbSessText{}
	if pg == nil || len(keys) == 0 {
		return out
	}
	rows, err := pg.QueryContext(ctx, `SELECT sql_key, sql_text FROM db_sql_text WHERE resource_id = $1 AND sql_key = ANY($2::text[])`, target, keys)
	if err != nil {
		errs["pg_sql_text"] = beDetail(pgErr(err))
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var k, t string
		if rows.Scan(&k, &t) != nil {
			continue
		}
		r := []rune(strings.TrimSpace(t))
		st := dbSessText{text: string(r), full: len(r)}
		if len(r) > dbSessSQLChars {
			st.text, st.cut = string(r[:dbSessSQLChars]), true
		}
		out[k] = st
	}
	return out
}

func dbSessHoldersFinding(target, eng string, k dbSessKeys, groups []*dbSessGroup, groupsTotal, sessionsTotal int,
	resolved map[string]dbSessRes, texts map[string]dbSessText) (Finding, []string) {
	var items []Finding
	var refs []string
	textAt := map[string]string{} // sql_key → 본문을 처음 실은 세션
	for _, g := range groups {
		h := g.rep
		it := Finding{
			"session": h.sid, "client": h.client,
			"max_" + k.ageField: dbSessRound(h.maxAge),
			"state":             h.state,
			"first_seen":        h.first.UTC().Format(time.RFC3339),
			"polls":             h.polls,
		}
		if !h.maxAt.IsZero() {
			it["max_at"] = h.maxAt.UTC().Format(time.RFC3339)
		}
		if !h.last.Equal(h.maxAt) {
			it["last_seen"] = h.last.UTC().Format(time.RFC3339)
		}
		if h.stype != "" && h.stype != h.state {
			it["session_type"] = h.stype
		}
		if w := dbSessWait(h.wc, h.wev); w != "" {
			it["wait"] = w
		}
		if h.user != "" {
			it["db_user"] = h.user
		}
		if g.n > 1 {
			it["sessions_same_key"] = g.n // 같은 (클라이언트, 상태 키, sql_key) 세션 수 — 대표만 싣는다
			it["other_sessions"] = g.others
		}
		dbSessApplyLabel(it, resolved, h.client)
		hrefs := []string{dbSessHolderRef(target, h)}
		if h.sqlKey != "" && h.sqlKey != "0" {
			it["sql_key"] = h.sqlKey
			t, ok := texts[h.sqlKey]
			switch first, dup := textAt[h.sqlKey]; {
			case dup:
				it["sql_text_same_as_session"] = first // 같은 문장 본문은 한 번만 싣는다
			case ok:
				v := t.text
				if t.cut {
					v += fmt.Sprintf("…(전체 %d자 중 앞 %d자 — 전문은 db_slow_queries full_text_for)", t.full, dbSessSQLChars)
				}
				it["sql_text"] = v
				textAt[h.sqlKey] = h.sid
				hrefs = append(hrefs, fmt.Sprintf("pg:db_sql_text:%s:%s", target, h.sqlKey))
			default:
				it["sql_text"] = nil // 사전에 없음(사전 조회 실패면 source_errors)
			}
		}
		it["refs"] = hrefs
		refs = append(refs, hrefs...)
		items = append(items, it)
	}
	f := Finding{
		"section":        "session_holders",
		"engine":         eng,
		"rank_key":       k.ageKey + "(" + k.ageUnit + ") 세션별 창 내 최대값 내림차순 — 같은 (클라이언트, 상태 키, sql_key)는 한 묶음",
		"holders":        dbSessNonNilF(items),
		"groups_total":   groupsTotal,
		"sessions_total": sessionsTotal,
		"cap":            dbSessMaxHolders,
		"basis":          "상태·대기·sql_key는 그 세션이 최대값을 찍은 폴의 원천 값. sql_key = 그 순간 실행 중이거나 마지막으로 실행한 문장(트랜잭션 전체가 아님), sql_text = db_sql_text 사전 본문",
	}
	if sessionsTotal >= dbSessHolderScan {
		f["sessions_total"] = fmt.Sprintf(">=%d", dbSessHolderScan)
	}
	if groupsTotal > len(items) {
		f["truncation_note"] = fmt.Sprintf("묶음 %d개 중 경과 상위 %d개만", groupsTotal, len(items))
	}
	if eng == "oracle" || eng == "tibero" {
		f["rank_key_note"] = "이 engine 원천에는 트랜잭션 경과 키가 없어 현재 대기 경과(waitTime)로 순위를 매겼다"
	}
	if k.contract != "verified" {
		f["contract"] = "unverified — 이 engine의 세션 키·단위는 생산자 계약에서 읽은 것이며 실측 대조 전이다"
	}
	return f, refs
}

func dbSessNonNilF(f []Finding) []Finding {
	if f == nil {
		return []Finding{}
	}
	return f
}

func dbSessWait(wc, wev string) string {
	switch {
	case wc != "" && wev != "" && wc != wev:
		return wc + ":" + wev
	case wev != "":
		return wev
	}
	return wc
}

func dbSessClientsFinding(target, eng string, k dbSessKeys, clients []*dbSessClient, total int, overall []*dbSessState,
	b dbSessBuckets, polls []time.Time, resolved map[string]dbSessRes, from, to time.Time, bg int, capped bool) (Finding, []string) {
	var items []Finding
	for _, c := range clients {
		states := map[string]any{}
		ss := dbSessSortedStates(c.states)
		other := 0
		for i, s := range ss {
			if i >= dbSessMaxStates {
				other += s.sessionPolls
				continue
			}
			st := map[string]any{"per_bucket": dbSessPeaks(s.perPoll, b), "session_polls": s.sessionPolls}
			if s.maxAge > 0 {
				st["max_"+k.ageField] = dbSessRound(s.maxAge)
			}
			states[s.key] = st
		}
		users := make([]string, 0, len(c.users))
		for u := range c.users {
			users = append(users, u)
		}
		sort.Strings(users)
		it := Finding{
			"client": c.client, "session_polls": c.sessionPolls, "max_sessions_per_poll": dbSessMaxPoll(c.perPoll),
			"states": states,
		}
		if c.maxAge > 0 {
			it["max_"+k.ageField] = dbSessRound(c.maxAge)
		}
		if len(users) > 0 {
			it["db_users"] = users
		}
		if other > 0 {
			it["other_states_session_polls"] = other
		}
		dbSessApplyLabel(it, resolved, c.client)
		items = append(items, it)
	}
	var ov []map[string]any
	for i, s := range overall {
		if i >= dbSessMaxOverall {
			break
		}
		m := map[string]any{"state": s.key, "session_polls": s.sessionPolls, "max_sessions_per_poll": dbSessMaxPoll(s.perPoll)}
		if s.maxAge > 0 {
			m["max_"+k.ageField] = dbSessRound(s.maxAge)
		}
		ov = append(ov, m)
	}
	ref := fmt.Sprintf("ch:dpm_session_local:%s:sessions:%s/%s", target, from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
	f := Finding{
		"section":              "session_clients",
		"engine":               eng,
		"polls":                len(polls),
		"buckets":              b.labels,
		"clients":              dbSessNonNilF(items),
		"clients_total":        total,
		"overall_states":       ov,
		"overall_states_total": len(overall),
		"resolution_detail":    dbSessResMap(resolved),
		"caps": map[string]any{"clients": dbSessMaxClients, "states_per_client": dbSessMaxStates,
			"overall_states": dbSessMaxOverall, "buckets": dbSessMaxBuckets},
		"background_rows_excluded": bg,
		"notes": "client=" + dbSessClientKeyNote(eng) + ". 상태 키 = 상태 (수집기 분류, 다를 때만) | 대기 클래스:이벤트 — 원천 문자열 그대로. " +
			"per_bucket = 버킷 안 폴별 세션 수의 최대값. 순위 = 세션-폴 수(폴마다 센 고유 세션 수의 합). " +
			"client_label·resolution_detail = 현재 kcm 스냅샷 pod(IP·이름 일치, 창 뒤 생성 pod 제외) → 워크로드, pod 이름 → 트레이스 service_name. unresolved = 스냅샷에 그 pod 없음(삭제됐거나 클러스터 밖)",
		"refs": []string{ref},
	}
	if b.widthS > 0 {
		f["bucket_width_s"] = b.widthS
	}
	if total > len(items) {
		f["truncation_note"] = fmt.Sprintf("클라이언트 %d개 중 상위 %d개만, 상태 키는 클라이언트당 %d개", total, len(items), dbSessMaxStates)
	}
	if capped {
		f["source_rows_capped_at"] = dbSessRowCap
	}
	return f, []string{ref}
}

func dbSessClientKeyNote(eng string) string {
	switch eng {
	case "postgresql":
		return "body.ip(접속 IP)"
	case "oracle", "tibero":
		return "body.machine(클라이언트 호스트 이름)"
	case "mysql", "mariadb":
		return "body.hostname(접속 호스트:포트)"
	}
	return ""
}

// dbSessDigest는 요약 꼬리 한 토막이다 — 응답 앞부분만 보는 소비자 대비 상위 2 보유 묶음과
// 상위 3 클라이언트를 이름·수치로만(해석 문구 없음).
func dbSessDigest(eng string, k dbSessKeys, groups []*dbSessGroup, clients []*dbSessClient,
	resolved map[string]dbSessRes, texts map[string]dbSessText, polls int) string {
	var sb strings.Builder
	fmt.Fprintf(&sb, "세션 상태(%s, 폴 %d개):", eng, polls)
	name := func(c string) string {
		r, ok := resolved[c]
		if !ok {
			return c
		}
		if l := dbSessLabel(r); l != "" {
			return c + "→" + l
		}
		return c + "(" + r.status + ")"
	}
	if len(groups) > 0 {
		fmt.Fprintf(&sb, " %s 상위 —", k.ageKey)
		for i, g := range groups {
			if i >= 2 {
				break
			}
			h := g.rep
			fmt.Fprintf(&sb, " 세션 %s %s [%s] %s=%g", h.sid, name(h.client), dbSessStateKey(h.state, h.stype, h.wc, h.wev), k.ageField, dbSessRound(h.maxAge))
			if g.n > 1 {
				fmt.Fprintf(&sb, " 외 같은 키 %d세션", g.n-1)
			}
			if t, ok := texts[h.sqlKey]; ok {
				r := []rune(t.text)
				if len(r) > 90 {
					r = append(r[:90], '…')
				}
				fmt.Fprintf(&sb, " sql[%s]", string(r))
			} else if h.sqlKey != "" && h.sqlKey != "0" {
				fmt.Fprintf(&sb, " sql_key=%s", h.sqlKey)
			}
			sb.WriteString(";")
		}
	}
	if len(clients) > 0 {
		sb.WriteString(" 세션 수 상위 클라이언트 —")
		for i, c := range clients {
			if i >= 3 {
				break
			}
			top := dbSessSortedStates(c.states)[0]
			fmt.Fprintf(&sb, " %s 세션-폴 %d·폴당 최대 %d(최다 [%s] %d);", name(c.client), c.sessionPolls,
				dbSessMaxPoll(c.perPoll), top.key, top.sessionPolls)
		}
	}
	return strings.TrimSuffix(sb.String(), ";") + "."
}
