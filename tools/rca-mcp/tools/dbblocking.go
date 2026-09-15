// db_blocking — "이 DB에서 누가 누구를 블로킹하나"의 재설계 표면
// (스펙 §3.6 DB 메뉴 + §13 구현 설계, 2026-07-30 확정). get_db_sessions
// 자리 교체.
//
//   - 창 전역 스캔 + 사건 단위 접기(§13.2): 사건 키는 (대상, engine,
//     끊기지 않은 블로킹 폴 구간)이다. 루트를 키로 삼으면 Oracle의
//     루트 이동(227→220 실측)이 하나의 경합을 폴 수만큼 분열시킨다.
//     실제로 수집된 비블로킹 폴이 끼면 끊고, 행이 아예 없는 구간은
//     끊지 않고 gap_polls로 센다. 교체된 get_db_sessions는 창의
//     마지막 스냅샷 10초만 봐서 희박 사건(PG 1.9%·Oracle 0.03%)을
//     구조적으로 놓쳤다.
//   - 트리를 그리지 않는다(§13.3): blockingPid가 pg_blocking_pids()
//     배열 첫 원소만 보존하므로(생산자 소스 실측) 짝은 원천에서 이미
//     손실됐다. 역할 3구획(root/both/blocked_only)은 두 축의 조합이라
//     결손에 영향받지 않고, 결손은 두 방향 명단으로 드러낸다 —
//     unpaired_blockers(막는데 상대 없음) / orphan_blocked(막혔는데
//     상대가 표시 못 받음).
//   - 짝 계열은 대표 폴 한 폴의 관측이다(§13.4). 창 전역에서 짝을
//     모으면 루트 이동 시 어느 순간에도 존재하지 않은 트리가 합성된다.
//   - status는 규모 단독(§13.5): blocked_peak>=3. 지속은 배경과 사건을
//     가르지 못한다 — Oracle 배경 54사건이 2~3폴 지속인데 규모는
//     1세션이었다(사건 단위 실측).
//   - engine은 열린 집합이다(§13.6): 미인식 engine도 degraded로 적재된다.
//     공통 축(is_blocking)으로 발생까지 답하고 짝 연결만 등급으로 밝힌다.
//     ClickHouse는 공통 축 자체가 없어 "없음"이 아니라 "판단 불가"다.
package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

const (
	dbBlkPeakThreshold  = 3  // anomalous 경계 — 한 캡처 교정 임시값(§13.5)
	dbBlkDefaultEvents  = 20 // 사건 반환 상한(§13.8)
	dbBlkTimelinePolls  = 60 // 초과 시 앞 30 + 뒤 30(§13.8)
	dbBlkNeighborLookup = 24 * time.Hour
)

// dbBlkEngine은 engine별 body 키 계약이다(§13.1 정정 2 — 생산자 소스가
// 정본). pairing은 §13.6의 3등급 중 verified/unverified만 여기 담고,
// 표에 없는 engine은 unsupported로 떨어진다.
//
// sentinel 주의: 계약상 "없음"은 대부분 0이고 CUBRID만 -1이지만,
// 세션 id는 어느 engine에서도 양수라 판별식 `> 0`이 두 sentinel을
// 동시에 흡수한다(구현 정정 — §13 결정 5의 sentinel_used 필드를
// blocked_predicate로 바꾼 이유).
type dbBlkKeys struct {
	id, blocker, wait, waitClass, sqlKey string
	hasRelNames                          bool
	pairing                              string
}

var dbBlkEngines = map[string]dbBlkKeys{
	"postgresql": {id: "pid", blocker: "blockingPid", wait: "waitEvent", sqlKey: "sqlHash", hasRelNames: true, pairing: "verified"},
	"oracle":     {id: "sid", blocker: "blockingSession", wait: "event", waitClass: "waitClass", sqlKey: "sqlId", pairing: "verified"},
	"tibero":     {id: "sid", blocker: "blockingSession", wait: "event", waitClass: "waitClass", sqlKey: "sqlId", pairing: "unverified"},
	// mysql·mariadb: body의 command는 대기 이벤트가 아니라 명령 종류라
	// 쓰지 않는다(§13.7). 대기 표시 없음.
	"mysql":   {id: "tid", blocker: "blockingTid", sqlKey: "sqlHash", pairing: "unverified"},
	"mariadb": {id: "tid", blocker: "blockingTid", sqlKey: "sqlHash", pairing: "unverified"},
	"mssql":   {id: "sid", blocker: "blockingSid", wait: "waitType", sqlKey: "queryHash", pairing: "unverified"},
	"cubrid":  {id: "tid", blocker: "blockingTid", sqlKey: "sqlId", pairing: "unverified"},
}

// dbBlkSQLExpr는 engine 분기를 CH multiIf로 흡수한 식들이다. 미지
// engine은 0/빈 문자열로 떨어져 "짝 산출 불가"가 데이터로 드러난다.
const (
	dbBlkIDExpr = `multiIf(engine = 'postgresql', JSONExtractInt(body, 'pid'),
		engine IN ('mysql','mariadb','cubrid'), JSONExtractInt(body, 'tid'),
		engine IN ('oracle','tibero','mssql'), JSONExtractInt(body, 'sid'), 0)`
	dbBlkBlockerExpr = `multiIf(engine = 'postgresql', JSONExtractInt(body, 'blockingPid'),
		engine IN ('oracle','tibero'), JSONExtractInt(body, 'blockingSession'),
		engine IN ('mysql','mariadb','cubrid'), JSONExtractInt(body, 'blockingTid'),
		engine = 'mssql', JSONExtractInt(body, 'blockingSid'), 0)`
	dbBlkWaitExpr = `multiIf(engine = 'postgresql', JSONExtractString(body, 'waitEvent'),
		engine IN ('oracle','tibero'), JSONExtractString(body, 'event'),
		engine = 'mssql', JSONExtractString(body, 'waitType'), '')`
	dbBlkWaitClassExpr = `if(engine IN ('oracle','tibero'), JSONExtractString(body, 'waitClass'), '')`
	dbBlkRelExpr       = `if(engine = 'postgresql', JSONExtractString(body, 'relNames'), '')`
	dbBlkSQLKeyExpr    = `multiIf(engine IN ('postgresql','mysql','mariadb'), toString(JSONExtractInt(body, 'sqlHash')),
		engine IN ('oracle','tibero','cubrid'), JSONExtractString(body, 'sqlId'),
		engine = 'mssql', JSONExtractString(body, 'queryHash'), '')`
)

// NewDBBlockingTool은 db_blocking 도구를 만든다. firstEvent·lastEvent는
// seed의 인시던트 창 — from·to 생략 시 기본 창이다(§13.8: 창 전역 스캔이
// 이 도구의 핵심이므로 조사자에게 창을 좁히도록 강요하지 않는다).
func NewDBBlockingTool(ch *CH, firstEvent, lastEvent time.Time, nowFn func() time.Time) llm.Tool {
	if nowFn == nil {
		nowFn = time.Now
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"db":         map[string]string{"type": "string", "description": "database 대상의 target_id(UUID)"},
			"from":       map[string]string{"type": "string", "description": "UTC RFC3339 또는 now/now-2h. 생략하면 인시던트 창"},
			"to":         map[string]string{"type": "string", "description": "UTC RFC3339 또는 now. 생략하면 인시던트 창"},
			"max_events": map[string]any{"type": "integer", "description": "사건 반환 상한(기본 20)"},
		},
		"required": []string{"db"},
	})
	return llm.Tool{
		Name: "db_blocking",
		Description: "DB의 시간창에서 블로킹 사건을 찾는다 — 누가 누구를 막았나(루트 세션 포함), 몇 세션이 막혔나, 어느 객체를 두고 다퉜나. " +
			"창 전역을 훑어 연속 관측을 한 사건으로 접는다. 세션 수·커넥션 포화는 이 도구가 아니라 read_timeseries의 몫.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				DB, From, To string
				MaxEvents    int `json:"max_events"`
			}
			if err := json.Unmarshal(args, &in); err != nil {
				return nil, fmt.Errorf(`인자 오류: {"db": "<uuid>", "from": "<RFC3339|now-2h>", "to": "<RFC3339|now>"} 필요(from·to 생략 가능)`)
			}
			if !uuidRe.MatchString(in.DB) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — database 대상의 순수 UUID 필요", in.DB)
			}
			now := nowFn().UTC()
			var basis []string
			fromT, toT := time.Time{}, time.Time{}
			if in.From != "" {
				t, err := parseFlexTime(in.From, now)
				if err != nil {
					return nil, fmt.Errorf("from 시간 오류: %v", err)
				}
				fromT, basis = t, append(basis, "from=인자")
			} else if !firstEvent.IsZero() {
				fromT, basis = firstEvent.UTC(), append(basis, "from=인시던트 창 시작(seed)")
			} else {
				fromT, basis = now.Add(-2*time.Hour), append(basis, "from=now-2h(seed 창 없음)")
			}
			if in.To != "" {
				t, err := parseFlexTime(in.To, now)
				if err != nil {
					return nil, fmt.Errorf("to 시간 오류: %v", err)
				}
				toT, basis = t, append(basis, "to=인자")
			} else if !lastEvent.IsZero() {
				toT, basis = lastEvent.UTC(), append(basis, "to=인시던트 창 끝(seed)")
			} else {
				toT, basis = now, append(basis, "to=now(seed 창 없음)")
			}
			if !fromT.Before(toT) {
				return nil, fmt.Errorf("시간창 오류: from < to 필요 (해석된 값 from=%s to=%s)",
					fromT.Format(time.RFC3339), toT.Format(time.RFC3339))
			}
			maxEvents := in.MaxEvents
			if maxEvents <= 0 {
				maxEvents = dbBlkDefaultEvents
			}
			return dbBlocking(ctx, ch, in.DB, fromT, toT, maxEvents, strings.Join(basis, ", "))
		},
	}
}

// dbBlkPoll은 폴 인벤토리 한 행이다(폴 = 동일 timestamp 행 묶음).
type dbBlkPoll struct {
	ts        time.Time
	engine    string
	rows      int
	blockingN int  // is_blocking='1' 행 수
	blockedN  int  // 막힘 키 > 0 행 수
	hasAxis   bool // log_attributes에 is_blocking 키 존재
}

func (p dbBlkPoll) isBlockingPoll() bool { return p.blockingN > 0 || p.blockedN > 0 }

// dbBlkRow는 블로킹 관련 행 하나다(대표 폴 상세 + 폴별 루트 판정 재료).
type dbBlkRow struct {
	ts       time.Time
	engine   string
	sid      int
	blocker  int
	blocking bool
	wait     string
	waitCls  string
	rel      string
	sqlKey   string
}

func (r dbBlkRow) role() string {
	switch {
	case r.blocking && r.blocker <= 0:
		return "root"
	case r.blocking:
		return "both"
	default:
		return "blocked_only"
	}
}

func dbBlocking(ctx context.Context, ch *CH, target string, from, to time.Time, maxEvents int, windowBasis string) (any, error) {
	var acc truncAcc
	polls, err := dbBlkPolls(ctx, ch, target, from, to, &acc)
	if err != nil {
		return nil, err
	}
	obs := &TimeRange{From: from.UTC(), To: to.UTC()}

	if len(polls) == 0 {
		return dbBlkNoRows(ctx, ch, target, from, to, windowBasis, obs, &acc)
	}

	// 공통 축·짝 산출 가능성 판정(§13.6·§13.9) — 표에 없는 engine이면
	// 짝은 unsupported, 공통 축까지 없으면 판단 불가다.
	engines := map[string]bool{}
	axisAny := false
	for _, p := range polls {
		engines[p.engine] = true
		if p.hasAxis {
			axisAny = true
		}
	}
	pairing, engineList := dbBlkPairing(engines)
	if !axisAny && pairing == "unsupported" {
		return Envelope{
			Status:       "no_data",
			NoDataReason: NoDataNotCollected,
			Summary: fmt.Sprintf("이 대상(engine=%s)의 세션 스냅샷에는 블로킹 축이 없다 — 공통 축(is_blocking) 미부착이고 짝 키 계약도 없다. "+
				"블로킹이 없다는 뜻이 아니라 이 원천으로 판단할 수 없다는 뜻이다(폴 %d개·행 %d개 조회). 창: %s",
				engineList, len(polls), dbBlkTotalRows(polls), windowBasis),
			AssessmentBasis: "블로킹 판정 축(공통 attr·engine 짝 키) 부재 — 관측 없음이 아니라 산출 불가",
			ObservedRange:   obs,
			QueryTruncated:  bool(acc),
			Refs:            []string{fmt.Sprintf("ch:dpm_session_local:%s:polls:%d", target, len(polls))},
		}, nil
	}

	events := dbBlkFoldEvents(polls)
	if len(events) == 0 {
		return Envelope{
			Status: "normal",
			Summary: fmt.Sprintf("블로킹 없음 — 창 내 폴 %d개(행 %d개)를 전부 훑었고 블로킹 표식이 한 건도 없었다(engine=%s). 창: %s",
				len(polls), dbBlkTotalRows(polls), engineList, windowBasis),
			AssessmentBasis: dbBlkBasis(polls, nil, pairing),
			ObservedRange:   obs,
			QueryTruncated:  bool(acc),
			Refs:            []string{fmt.Sprintf("ch:dpm_session_local:%s:polls:%d", target, len(polls))},
			// 완전 조회 0건 — "블로킹이 없었다"는 배제 근거의 원천이다(§5.1 계약 1).
			// 이 줄이 없으면 그 배제가 index에서 표현되지 않는다.
			Scopes: []QueryScope{qscope("event", 0, 0)},
		}, nil
	}

	rows, err := dbBlkRows(ctx, ch, target, from, to, &acc)
	if err != nil {
		return nil, err
	}
	byPoll := map[string][]dbBlkRow{}
	for _, r := range rows {
		k := r.ts.Format(time.RFC3339Nano)
		byPoll[k] = append(byPoll[k], r)
	}

	sort.SliceStable(events, func(i, j int) bool {
		if events[i].peak != events[j].peak {
			return events[i].peak > events[j].peak
		}
		return len(events[i].polls) > len(events[j].polls)
	})
	truncated := len(events) > maxEvents
	total := len(events)
	if truncated {
		events = events[:maxEvents]
	}

	var findings []Finding
	var refs []string
	peakMax := 0
	for _, ev := range events {
		f, fr := dbBlkFinding(target, ev, byPoll, pairing)
		findings = append(findings, f)
		refs = append(refs, fr...)
		if ev.peak > peakMax {
			peakMax = ev.peak
		}
	}

	status := "normal"
	if peakMax >= dbBlkPeakThreshold {
		status = "anomalous"
	}
	summary := dbBlkSummary(events, total, truncated, status, engineList, pairing, windowBasis)
	return Envelope{
		Status:          status,
		Summary:         summary,
		AssessmentBasis: dbBlkBasis(polls, events, pairing),
		Findings:        findings,
		ObservedRange:   obs,
		Truncated:       truncated,
		QueryTruncated:  bool(acc),
		Refs:            refs,
		// 사건(시간 구간) 축 절단(§5.1 계약 2).
		Scopes: []QueryScope{qscope("event", total, len(events))},
	}, nil
}

// dbBlkPolls는 폴 인벤토리를 가져온다(창 전역·서버측 집계).
func dbBlkPolls(ctx context.Context, ch *CH, target string, from, to time.Time, acc *truncAcc) ([]dbBlkPoll, error) {
	rows, tr, err := ch.Query(ctx, `
		SELECT toString(timestamp) AS ts, engine, count() AS rows,
		       countIf(log_attributes['is_blocking'] = '1') AS blocking_n,
		       countIf(`+dbBlkBlockerExpr+` > 0) AS blocked_n,
		       countIf(has(mapKeys(log_attributes), 'is_blocking')) AS axis_n
		FROM dpm_session_local
		WHERE target_id = {target:String}
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		GROUP BY ts, engine ORDER BY ts`,
		map[string]string{"target": target, "from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, fmt.Errorf("db_blocking 폴 인벤토리 조회: %w", err)
	}
	acc.note(tr)
	var out []dbBlkPoll
	for _, r := range rows {
		ts, err := dbBlkParseTS(r["ts"])
		if err != nil {
			continue
		}
		out = append(out, dbBlkPoll{
			ts: ts, engine: fmt.Sprint(r["engine"]),
			rows: asInt(r["rows"]), blockingN: asInt(r["blocking_n"]),
			blockedN: asInt(r["blocked_n"]), hasAxis: asInt(r["axis_n"]) > 0,
		})
	}
	return out, nil
}

// dbBlkRows는 블로킹 관련 행만 가져온다(전 행이 아니라 축이 켜진 행 —
// 실측상 PG 588행/전기간이라 창 전역이어도 작다).
func dbBlkRows(ctx context.Context, ch *CH, target string, from, to time.Time, acc *truncAcc) ([]dbBlkRow, error) {
	rows, tr, err := ch.Query(ctx, `
		SELECT toString(timestamp) AS ts, engine,
		       `+dbBlkIDExpr+` AS sid,
		       `+dbBlkBlockerExpr+` AS blocker,
		       log_attributes['is_blocking'] AS isblk,
		       `+dbBlkWaitExpr+` AS wait,
		       `+dbBlkWaitClassExpr+` AS wait_class,
		       `+dbBlkRelExpr+` AS rel,
		       `+dbBlkSQLKeyExpr+` AS sql_key
		FROM dpm_session_local
		WHERE target_id = {target:String}
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		  AND (log_attributes['is_blocking'] = '1' OR `+dbBlkBlockerExpr+` > 0)
		ORDER BY ts, sid`,
		map[string]string{"target": target, "from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, fmt.Errorf("db_blocking 블로킹 행 조회: %w", err)
	}
	acc.note(tr)
	var out []dbBlkRow
	for _, r := range rows {
		ts, err := dbBlkParseTS(r["ts"])
		if err != nil {
			continue
		}
		out = append(out, dbBlkRow{
			ts: ts, engine: fmt.Sprint(r["engine"]),
			sid: asInt(r["sid"]), blocker: asInt(r["blocker"]),
			blocking: fmt.Sprint(r["isblk"]) == "1",
			wait:     fmt.Sprint(r["wait"]), waitCls: fmt.Sprint(r["wait_class"]),
			rel: fmt.Sprint(r["rel"]), sqlKey: fmt.Sprint(r["sql_key"]),
		})
	}
	return out, nil
}

// dbBlkEvent는 접힌 사건 하나다(§13.2).
type dbBlkEvent struct {
	engine   string
	polls    []dbBlkPoll // 블로킹 폴만, 시각순
	peak     int
	last     int
	gapPolls int
}

// dbBlkFoldEvents는 engine별로 연속 블로킹 폴 구간을 사건으로 접는다.
// 실제로 수집된 비블로킹 폴이 끼면 끊고, 행이 아예 없는 구간(폴 부재)은
// 끊지 않고 gap_polls로 센다(§13.2).
func dbBlkFoldEvents(polls []dbBlkPoll) []dbBlkEvent {
	byEngine := map[string][]dbBlkPoll{}
	var order []string
	for _, p := range polls {
		if _, seen := byEngine[p.engine]; !seen {
			order = append(order, p.engine)
		}
		byEngine[p.engine] = append(byEngine[p.engine], p)
	}
	var out []dbBlkEvent
	for _, eng := range order {
		ps := byEngine[eng]
		interval := dbBlkPollInterval(ps)
		var cur *dbBlkEvent
		flush := func() {
			if cur != nil {
				out = append(out, *cur)
				cur = nil
			}
		}
		for _, p := range ps {
			if !p.isBlockingPoll() {
				flush() // 수집된 비블로킹 폴 = 사건 종료
				continue
			}
			if cur == nil {
				cur = &dbBlkEvent{engine: eng}
			} else if interval > 0 {
				// 폴 부재 구간은 끊지 않고 센다.
				if miss := int(p.ts.Sub(cur.polls[len(cur.polls)-1].ts)/interval) - 1; miss > 0 {
					cur.gapPolls += miss
				}
			}
			cur.polls = append(cur.polls, p)
			if p.blockedN > cur.peak {
				cur.peak = p.blockedN
			}
			cur.last = p.blockedN
		}
		flush()
	}
	return out
}

// dbBlkPollInterval은 폴 주기를 데이터에서 구한다(§13.2 — 1분 하드코딩
// 금지. 폴 주기는 계약이 아니다).
//
// 최빈값을 쓴다. 중앙값은 표본이 적을 때 공백 자체에 밀린다 — [1m, 3m]의
// 중앙값이 3m이 되어 3m 공백을 "정상 주기"로 삼고 gap_polls가 0이 된다
// (단위 가드가 잡은 구현 결함). 주기는 "가장 자주 나오는 간격"이다.
// 동수면 짧은 쪽 — 긴 간격을 주기로 잡으면 공백을 덜 센다.
func dbBlkPollInterval(ps []dbBlkPoll) time.Duration {
	if len(ps) < 2 {
		return 0
	}
	// 초 단위로 뭉쳐 센다(폴 시각은 초 이하로 흔들린다 — 실측 08:36:30.017
	// / 08:37:31.384).
	count := map[int64]int{}
	for i := 1; i < len(ps); i++ {
		if gap := ps[i].ts.Sub(ps[i-1].ts); gap > 0 {
			count[int64(gap.Round(time.Second)/time.Second)]++
		}
	}
	if len(count) == 0 {
		return 0
	}
	var best int64
	bestN := 0
	for sec, n := range count {
		if n > bestN || (n == bestN && sec < best) {
			best, bestN = sec, n
		}
	}
	return time.Duration(best) * time.Second
}

// dbBlkFinding은 사건 1건을 finding 1행으로 만든다. 짝 계열은 대표 폴
// 한 폴 소속이다(§13.4).
func dbBlkFinding(target string, ev dbBlkEvent, byPoll map[string][]dbBlkRow, pairing string) (Finding, []string) {
	first, last := ev.polls[0].ts, ev.polls[len(ev.polls)-1].ts

	// 대표 폴 = blocked_n 최대, 동수면 가장 늦은 폴.
	rep := ev.polls[0]
	for _, p := range ev.polls {
		if p.blockedN >= rep.blockedN {
			rep = p
		}
	}
	repRows := byPoll[rep.ts.Format(time.RFC3339Nano)]

	// 폴별 타임라인 + 역할 합집합(사건 전역).
	var rootTL, blockedTL []map[string]any
	rootsSeen := map[string]bool{}
	roles := map[string]map[int]bool{"root": {}, "both": {}, "blocked_only": {}}
	migrated := false
	var prevRoots string
	for _, p := range ev.polls {
		rs := byPoll[p.ts.Format(time.RFC3339Nano)]
		var roots []string
		for _, r := range rs {
			roles[r.role()][r.sid] = true
			if r.role() == "root" {
				roots = append(roots, fmt.Sprint(r.sid))
			}
		}
		sort.Strings(roots)
		key := strings.Join(roots, ",")
		if prevRoots != "" && key != prevRoots {
			migrated = true
		}
		if key != "" {
			prevRoots = key
			rootsSeen[key] = true
		}
		rootTL = append(rootTL, map[string]any{"ts": p.ts.UTC().Format(time.RFC3339), "root_session_ids": roots})
		blockedTL = append(blockedTL, map[string]any{"ts": p.ts.UTC().Format(time.RFC3339), "blocked_n": p.blockedN})
	}
	tlTruncated := false
	if len(ev.polls) > dbBlkTimelinePolls {
		half := dbBlkTimelinePolls / 2
		rootTL = append(append([]map[string]any{}, rootTL[:half]...), rootTL[len(rootTL)-half:]...)
		blockedTL = append(append([]map[string]any{}, blockedTL[:half]...), blockedTL[len(blockedTL)-half:]...)
		tlTruncated = true
	}

	// 대표 폴의 짝과 두 방향 결손(§13.3).
	var edges [][]string
	blockerOf := map[int]bool{}   // 간선에서 blocker로 등장한 세션
	present := map[int]dbBlkRow{} // 대표 폴에 행이 있는 세션
	for _, r := range repRows {
		present[r.sid] = r
	}
	for _, r := range repRows {
		if r.blocker > 0 {
			edges = append(edges, []string{fmt.Sprint(r.sid), fmt.Sprint(r.blocker)})
			blockerOf[r.blocker] = true
		}
	}
	var unpaired, orphan []string
	for _, r := range repRows {
		if r.blocking && !blockerOf[r.sid] {
			unpaired = append(unpaired, fmt.Sprint(r.sid))
		}
		if r.blocker > 0 {
			b, ok := present[r.blocker]
			if !ok || !b.blocking {
				orphan = append(orphan, fmt.Sprint(r.sid))
			}
		}
	}
	sort.Strings(unpaired)
	sort.Strings(orphan)

	f := Finding{
		"engine":                   ev.engine,
		"first_seen":               first.UTC().Format(time.RFC3339),
		"last_seen":                last.UTC().Format(time.RFC3339),
		"polls":                    len(ev.polls),
		"root_timeline":            rootTL,
		"root_migrated":            migrated,
		"blocked_timeline":         blockedTL,
		"blocked_peak":             ev.peak,
		"blocked_last":             ev.last,
		"roles":                    dbBlkRoles(roles),
		"representative_poll_ts":   rep.ts.UTC().Format(time.RFC3339),
		"edges":                    dbBlkEdges(edges),
		"unpaired_blockers":        dbBlkStrs(unpaired),
		"orphan_blocked":           dbBlkStrs(orphan),
		"chain_depth_min_observed": dbBlkDepth(edges),
		"blocked_predicate":        dbBlkPredicate(ev.engine),
		"pair_linking":             pairing,
	}
	if ev.gapPolls > 0 {
		f["gap_polls"] = ev.gapPolls
	}
	if tlTruncated {
		f["timeline_truncated"] = fmt.Sprintf("폴 %d개 중 앞뒤 %d개만 — polls·blocked_peak은 전량에서 계산", len(ev.polls), dbBlkTimelinePolls)
	}
	if w := dbBlkWaitSummary(repRows); len(w) > 0 {
		f["wait_summary"] = w
	}
	if ks := dbBlkEngines[ev.engine]; ks.hasRelNames {
		f["contended_objects"] = dbBlkRelations(repRows)
	}
	if sr := dbBlkSQLRefs(repRows); len(sr) > 0 {
		f["sql_refs"] = sr
	}

	refs := []string{fmt.Sprintf("ch:dpm_session_local:%s:%s", target, rep.ts.UTC().Format(time.RFC3339))}
	for _, r := range repRows {
		if r.role() == "root" {
			refs = append(refs, fmt.Sprintf("ch:dpm_session_local:%s:%s:session:%d",
				target, rep.ts.UTC().Format(time.RFC3339), r.sid))
		}
	}
	f["refs"] = refs
	return f, refs
}

func dbBlkRoles(roles map[string]map[int]bool) map[string][]string {
	out := map[string][]string{}
	for k, set := range roles {
		var ids []int
		for id := range set {
			ids = append(ids, id)
		}
		sort.Ints(ids)
		vals := []string{}
		for _, id := range ids {
			vals = append(vals, fmt.Sprint(id))
		}
		out[k] = vals
	}
	return out
}

// dbBlkEdges는 nil을 빈 배열로 정규화한다 — []는 "지원되며 관측 없음",
// 필드 부재는 "산출 계약 없음"이다(§13.9).
func dbBlkEdges(e [][]string) [][]string {
	if e == nil {
		return [][]string{}
	}
	return e
}

func dbBlkStrs(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// dbBlkDepth는 관측된 간선만으로 만든 최장 경로의 노드 수다 — 짝이
// 원천에서 손실되므로 하한이다(§13.3).
func dbBlkDepth(edges [][]string) int {
	parent := map[string]string{}
	for _, e := range edges {
		parent[e[0]] = e[1] // blocked -> blocker
	}
	best := 0
	for child := range parent {
		seen := map[string]bool{child: true}
		n, cur := 1, child
		for {
			p, ok := parent[cur]
			if !ok || seen[p] {
				break // 사이클 방어(폴 내 사이클은 실측 미관측이나 방어는 둔다)
			}
			seen[p] = true
			n, cur = n+1, p
		}
		if n > best {
			best = n
		}
	}
	if best == 0 && len(edges) > 0 {
		best = 2
	}
	return best
}

func dbBlkWaitSummary(rows []dbBlkRow) map[string]int {
	out := map[string]int{}
	for _, r := range rows {
		k := r.wait
		if k == "" {
			continue
		}
		if r.waitCls != "" {
			k = k + " (" + r.waitCls + ")"
		}
		out[k]++
	}
	return out
}

// dbBlkRelations는 relNames 원문을 분리·중복 제거·정렬한다. 가공하지
// 않는다 — inventory_pkey는 인덱스지만 접미사를 벗기면 조사자가 인용한
// 이름이 원천에 없게 된다(§13.10).
func dbBlkRelations(rows []dbBlkRow) []string {
	set := map[string]bool{}
	for _, r := range rows {
		for _, part := range strings.Split(r.rel, ",") {
			if p := strings.TrimSpace(part); p != "" {
				set[p] = true
			}
		}
	}
	out := []string{}
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func dbBlkSQLRefs(rows []dbBlkRow) []map[string]string {
	var out []map[string]string
	seen := map[string]bool{}
	for _, r := range rows {
		if r.sqlKey == "" || r.sqlKey == "0" {
			continue
		}
		k := fmt.Sprint(r.sid) + ":" + r.sqlKey
		if seen[k] {
			continue
		}
		seen[k] = true
		out = append(out, map[string]string{"session": fmt.Sprint(r.sid), "sql_ref": r.sqlKey})
	}
	return out
}

// dbBlkPredicate는 실제로 쓴 판별식을 밝힌다 — 조사자가 검산할 수 있게
// 하고, sentinel 차이(CUBRID -1)를 `> 0`이 흡수했음을 드러낸다.
func dbBlkPredicate(engine string) string {
	if ks, ok := dbBlkEngines[engine]; ok {
		return ks.blocker + " > 0"
	}
	return "짝 키 계약 없음(engine 미등록) — 공통 축 is_blocking만 사용"
}

// dbBlkPairing은 창에 나타난 engine들의 짝 연결 등급을 합산한다.
// 섞이면 가장 낮은 등급으로 내린다.
func dbBlkPairing(engines map[string]bool) (string, string) {
	var names []string
	for e := range engines {
		names = append(names, e)
	}
	sort.Strings(names)
	grade := ""
	for _, e := range names {
		g := "unsupported"
		if ks, ok := dbBlkEngines[e]; ok {
			g = ks.pairing
		}
		if grade == "" || dbBlkGradeRank(g) < dbBlkGradeRank(grade) {
			grade = g
		}
	}
	if grade == "" {
		grade = "unsupported"
	}
	return grade, strings.Join(names, ",")
}

func dbBlkGradeRank(g string) int {
	switch g {
	case "unsupported":
		return 0
	case "unverified":
		return 1
	default:
		return 2
	}
}

func dbBlkTotalRows(polls []dbBlkPoll) int {
	n := 0
	for _, p := range polls {
		n += p.rows
	}
	return n
}

func dbBlkParseTS(v any) (time.Time, error) {
	s := fmt.Sprint(v)
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02 15:04:05"} {
		if t, err := time.ParseInLocation(layout, s, time.UTC); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("timestamp 해석 불가: %q", s)
}

// dbBlkNoRows는 창에 행이 0인 경우다. 인접 창을 한 번 더 조회해 "이 창만
// 빔"과 "대상 자체 미수집"을 사유 문구로 구분한다(§13.9).
func dbBlkNoRows(ctx context.Context, ch *CH, target string, from, to time.Time, windowBasis string, obs *TimeRange, acc *truncAcc) (any, error) {
	// GROUP BY ... ORDER BY ... LIMIT 1 — 단일 행 보장, 절단 불가 표면이라 표식은 버린다.
	near, _, err := ch.Query(ctx, `
		SELECT count() AS n, engine FROM dpm_session_local
		WHERE target_id = {target:String}
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		GROUP BY engine ORDER BY n DESC LIMIT 1`,
		map[string]string{"target": target,
			"from": chTime(from.Add(-dbBlkNeighborLookup)), "to": chTime(to.Add(dbBlkNeighborLookup))})
	if err != nil {
		return nil, fmt.Errorf("db_blocking 인접 창 조회: %w", err)
	}
	reason := "이 대상의 세션 스냅샷이 인접 ±24h에도 없다 — 대상 자체가 DPM 수집 밖일 수 있다(대상 정체는 describe_target, 수집 여부는 get_data_coverage)"
	if len(near) > 0 && asInt(near[0]["n"]) > 0 {
		reason = fmt.Sprintf("인접 ±24h에는 스냅샷이 있다(engine=%v, %d행) — 이 창만 비었다",
			near[0]["engine"], asInt(near[0]["n"]))
	}
	return Envelope{
		Status:          "no_data",
		NoDataReason:    NoDataZeroObservations,
		Summary:         fmt.Sprintf("창 내 이 대상의 세션 스냅샷이 0행이다. %s. 창: %s", reason, windowBasis),
		AssessmentBasis: "세션 스냅샷 0행 — 블로킹 없음이 아니라 관측 없음",
		ObservedRange:   obs,
		QueryTruncated:  bool(*acc),
		Refs:            []string{fmt.Sprintf("ch:dpm_session_local:%s:rows:0", target)},
	}, nil
}

func dbBlkBasis(polls []dbBlkPoll, events []dbBlkEvent, pairing string) string {
	peak, longest := 0, 0
	for _, ev := range events {
		if ev.peak > peak {
			peak = ev.peak
		}
		if len(ev.polls) > longest {
			longest = len(ev.polls)
		}
	}
	blkPolls := 0
	for _, p := range polls {
		if p.isBlockingPoll() {
			blkPolls++
		}
	}
	ratio := 0.0
	if n := len(polls); n > 0 {
		ratio = float64(blkPolls) / float64(n) * 100
	}
	if len(events) == 0 {
		return fmt.Sprintf("폴 %d개 전량에서 블로킹 표식 0건 — 조회 성공한 배제 근거. 짝 연결 등급=%s", len(polls), pairing)
	}
	return fmt.Sprintf("폴 %d개 중 블로킹 폴 %d개(%.1f%%)를 사건 %d건으로 접었다. 최대 막힌 세션 %d개(판정식: blocked_peak>=%d 단독 — "+
		"지속은 배경과 사건을 가르지 못해 판정에서 뺐다), 최장 지속 %d폴. 임계는 PG·Oracle 관측으로만 교정된 임시값. 짝 연결 등급=%s",
		len(polls), blkPolls, ratio, len(events), peak, dbBlkPeakThreshold, longest, pairing)
}

func dbBlkSummary(events []dbBlkEvent, total int, truncated bool, status, engineList, pairing, windowBasis string) string {
	var b strings.Builder
	top := events[0]
	fmt.Fprintf(&b, "%s에서 블로킹 사건 %d건", engineList, total)
	if truncated {
		fmt.Fprintf(&b, "(상한 %d건만 반환 — 최대 막힌 세션 순)", len(events))
	}
	fmt.Fprintf(&b, ". 가장 큰 사건은 최대 %d세션이 막혔고 %d폴 목격됐다(%s~%s).",
		top.peak, len(top.polls),
		top.polls[0].ts.UTC().Format("15:04:05"), top.polls[len(top.polls)-1].ts.UTC().Format("15:04:05"))
	if status == "normal" {
		fmt.Fprintf(&b, " 전부 소규모(막힌 세션 %d개 미만)라 이상으로 올리지 않았으나 관측은 findings에 그대로 있다.", dbBlkPeakThreshold)
	}
	b.WriteString(" 짝은 원천이 첫 blocker만 보존하므로 unpaired_blockers·orphan_blocked가 있으면 그만큼 연결이 지워진 것이다;" +
		" edges 계열은 representative_poll_ts 한 폴의 관측이다.")
	if pairing == "unverified" {
		b.WriteString(" 이 engine의 짝 연결은 생산자 계약에서 읽은 것이며 블로킹 실측으로 대조된 적이 없다 — edges가 비었거나 어긋나면 도구 결함일 수 있다.")
	} else if pairing == "unsupported" {
		b.WriteString(" 이 engine은 짝 키 계약이 없어 누가 누구를 막았는지는 확인할 수 없다(발생 여부만 공통 축으로 답한 것이다).")
	}
	fmt.Fprintf(&b, " 창: %s", windowBasis)
	return b.String()
}
