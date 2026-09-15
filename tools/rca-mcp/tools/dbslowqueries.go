// db_slow_queries — "이 DB에서 어느 쿼리가 느린가"의 재설계 표면
// (스펙 §3.6 DB 메뉴 + §14 구현 설계, 2026-07-30 확정). get_slow_queries
// 자리 교체.
//
//   - 창 전역 + 2단 접기(§14.2): 폴 안에서 실행횟수 가중으로 먼저 합치고
//     (행 키가 engine마다 더 세분이라 같은 폴에 같은 식별자가 여러 행일
//     수 있다), 폴들을 백분위·최대·마지막으로 접는다. 교체된
//     get_slow_queries는 창의 마지막 폴 10초만 봐서 30분 창의 폴 30개
//     중 1개만 썼다.
//   - 단위를 정규화한다(§14.1 정정 1): PG/MySQL/MariaDB는 ms,
//     Oracle/Tibero/MSSQL은 μs다. 교체된 도구는 둘을 `avg_elapsed_ms`
//     한 필드에 담아 Oracle을 1000배 부풀렸다.
//   - 판정은 직전 24시간 자기 기준선 대비 3배, 두 팔 OR(§14.3):
//     중앙값 팔과 최대 팔. 라이브 배경 창쌍 1,448개에서 중앙값 비 최대
//     2.61배·3배 초과 0건이 문턱의 근거다. Codex의 `p50 > 기준선 p99`
//     안은 6창 실물 사건에서 첫 창만 발화하고 침묵해 탈락했다(기준선
//     p99가 자기 사건에 오염된다). 최대 팔의 기준선도 같은 오염을 겪어
//     두 팔은 AND가 아니라 OR다.
//   - 순위는 판정이 아니다(§14.3·14.4): 24h distinct SQL이 35~58개뿐이고
//     최상위는 만성이라(MySQL 풀스캔 p50 324ms가 1,182폴 전부 등장)
//     순위는 워크로드 상수다. 만성은 `chronic`으로 이름 붙이고
//     `anomalous`로 올리지 않는다.
//   - 원천은 이미 축별 top-5 합집합으로 절단됐고 절단축은 기록되지
//     않는다(§14.1 정정 2). 그래서 순위 필드는 `rank`가 아니라
//     `rank_among_collected`이고, 축 역추정은 금지다.
//   - 컬렉터 자기 쿼리는 표식하고 남긴다(§14.8): MySQL 행의 32%다.
//     순위에는 남고 `verdict`도 계산하되 봉투 status를 올리지 못한다 —
//     배경에서 컬렉터 쿼리가 3.34배로 발화하는 것을 실측했다.
//   - engine은 PG 레지스트리에서 얻는다(§14.9): 창에 행이 0개여도
//     `collector_dpm_engine_version`이 대상의 engine·버전을 준다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

const (
	sqDefaultTopN     = 10             // §14.10 — 창당 distinct SQL이 pg 23~42·oracle 14~23
	sqRatioThreshold  = 3.0            // §14.3 — 배경 창쌍 1,448개에서 초과 0건
	sqMinPolls        = 3              // 판정 최소 폴(창·기준선 각각)
	sqBaselineSpan    = 24 * time.Hour // §14.3 — 동길이 창은 긴 사건에서 자기 오염
	sqChronicPresence = 0.9            // §14.3 chronic 조건
	sqTextTrunc       = 800            // §14.6 — 평균 176~605자를 대개 통과, 최대 23,851자 차단
	sqFullTextCap     = 20000          // 드릴다운 안전 상한
	sqMaxFullTextFor  = 3              // 드릴다운 대상 상한
	sqMaxPlanFor      = 2              //
	sqPlanMinCaptures = 5              // §14.7 — 캡처가 적으면 안정성 등급을 매기지 않는다
	sqRowCap          = 20000          // 창 원천 행 상한(초과 시 truncated)
	sqNeighborLookup  = 30 * 24 * time.Hour
)

// sqEngine은 engine별 body 키·단위 계약이다(§14.1 정정 1·3 — 생산자
// 소스가 정본). ioKeys는 (응답 필드명 → body 키, ms 변환 여부)다.
type sqEngine struct {
	idKind   string
	latKey   string
	latUnit  string // "ms" | "us"
	execKey  string
	totalKey string // 빈 값 = 원천에 total 키 없음 → avg×execs로 파생
	scope    [][2]string
	io       []sqIO
	contract string // verified(실측) | unverified(계약만)
	hasPlan  bool
}

type sqIO struct {
	field, key string
	div1000    bool
}

// sqEngines — top-SQL 수집기가 있는 engine. 여기 없고 sqNoTopSQL에도 없는
// engine은 "미검증"으로 공통 경로를 타고 coverage에 표시된다.
var sqEngines = map[string]sqEngine{
	"postgresql": {
		idKind: "pg_queryid", latKey: "avgExecTime", latUnit: "ms", execKey: "calls", totalKey: "totalExecTime",
		scope: [][2]string{{"dbs", "db"}, {"users", "user"}},
		io: []sqIO{{"shared_hit", "sharedHit", false}, {"shared_read", "sharedRead", false},
			{"temp_written", "tempWritten", false}, {"wal_bytes", "walBytes", false}},
		contract: "verified", hasPlan: false, // db_sql_plan pg 0행(§14.1 정정 5)
	},
	"oracle": {
		idKind: "oracle_sql_id", latKey: "avgElapsedTime", latUnit: "us", execKey: "executions", totalKey: "totalElapsedTime",
		scope: [][2]string{{"schemas", "schema"}, {"modules", "module"}},
		io: []sqIO{{"logical_reads", "avgLogicalReads", false}, {"physical_reads", "avgPhysicalReads", false},
			{"cpu_ms", "avgCpuTime", true}},
		contract: "verified", hasPlan: true,
	},
	"tibero": {
		idKind: "oracle_sql_id", latKey: "avgElapsedTime", latUnit: "us", execKey: "executions", totalKey: "totalElapsedTime",
		scope: [][2]string{{"schemas", "schema"}, {"modules", "module"}},
		io: []sqIO{{"logical_reads", "avgLogicalReads", false}, {"physical_reads", "avgPhysicalReads", false},
			{"cpu_ms", "avgCpuTime", true}},
		contract: "unverified", hasPlan: true,
	},
	"mysql": {
		idKind: "mysql_digest", latKey: "avgExecTime", latUnit: "ms", execKey: "calls", totalKey: "",
		scope: [][2]string{{"dbs", "db"}},
		io: []sqIO{{"rows_examined", "rowExamined", false}, {"rows_sent", "rowSent", false},
			{"lock_ms", "avgLockTime", false}, {"disk_temp_tables", "diskTempTables", false}},
		contract: "verified", hasPlan: true,
	},
	"mariadb": {
		idKind: "mysql_digest", latKey: "avgExecTime", latUnit: "ms", execKey: "calls", totalKey: "",
		scope: [][2]string{{"dbs", "db"}},
		io: []sqIO{{"rows_examined", "rowExamined", false}, {"rows_sent", "rowSent", false},
			{"lock_ms", "avgLockTime", false}, {"disk_temp_tables", "diskTempTables", false}},
		contract: "unverified", hasPlan: true,
	},
	"mssql": {
		// §14.9 — 식별자가 CH의 어디에 어떤 형식으로 들어가는지 미확인이다.
		// idKind를 단정하지 않는다.
		idKind: "", latKey: "avgElapsedTime", latUnit: "us", execKey: "executionCount", totalKey: "totalElapsedTime",
		scope:    [][2]string{{"dbs", "db"}},
		io:       []sqIO{{"cpu_ms", "avgCpuTime", true}},
		contract: "unverified", hasPlan: true,
	},
}

// sqNoTopSQL — top-SQL 수집기가 없는 engine(§14.9). 빈손이 "측정해서
// 없음"이 아니라 "원천 부재"다.
var sqNoTopSQL = map[string]bool{"cubrid": true, "mongodb": true, "clickhouse": true}

// sqSelfPatterns — 컬렉터 자기 쿼리 판별 패턴(§14.8, 실측 6개). 소문자
// 부분일치. 응답에 그대로 싣는다 — 근거 없는 불리언 금지.
var sqSelfPatterns = []string{
	"innodb_lock_waits", "x$processlist", "events_statements_summary",
	"pg_stat_statements", "pg_locks", "pg_blocking_pids",
}

// sqSortAxes — 허용 정렬축(§14.10). 잘못된 값은 오류다(조용한 기본값 대체 금지).
var sqSortAxes = map[string]bool{
	"total_ms": true, "ratio_p50": true, "latency_p50": true, "latency_max": true, "execs": true,
}

// sqKeyExpr는 sql_key 식이다(§14.1 정정 3): PG는 CH sql_id가 빈 문자열이라
// sql_hash를 쓴다.
func sqKeyExpr(engine string) string {
	if engine == "postgresql" {
		return "toString(sql_hash)"
	}
	return "sql_id"
}

// NewDBSlowQueriesTool은 db_slow_queries 도구를 만든다. pg는 engine
// 레지스트리·본문 사전·플랜 조회용(nil이면 강등 동작).
func NewDBSlowQueriesTool(ch *CH, pg *sql.DB, firstEvent, lastEvent time.Time, nowFn func() time.Time) llm.Tool {
	if nowFn == nil {
		nowFn = time.Now
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"db":    map[string]string{"type": "string", "description": "database 대상의 target_id(UUID)"},
			"from":  map[string]string{"type": "string", "description": "UTC RFC3339 또는 now/now-2h. 생략하면 인시던트 창"},
			"to":    map[string]string{"type": "string", "description": "UTC RFC3339 또는 now. 생략하면 인시던트 창"},
			"top_n": map[string]any{"type": "integer", "description": "반환 상한(기본 10)"},
			"sort":  map[string]any{"type": "string", "description": "정렬축: total_ms(기본)|ratio_p50|latency_p50|latency_max|execs"},
			"full_text_for": map[string]any{"type": "array", "items": map[string]string{"type": "string"},
				"description": "SQL 본문 전문을 받을 sql_key(최대 3개)"},
			"plan_for": map[string]any{"type": "array", "items": map[string]string{"type": "string"},
				"description": "실행계획을 받을 sql_key(최대 2개)"},
			"exclude_self_monitoring": map[string]any{"type": "boolean", "description": "컬렉터 자기 쿼리 제외(기본 false)"},
		},
		"required": []string{"db"},
	})
	return llm.Tool{
		Name: "db_slow_queries",
		Description: "DB의 시간창에서 수집된 top-SQL을 접어 준다 — 어느 쿼리가 느린가, 그리고 직전 24시간 자기 기준선 대비 느려졌나(만성과 사건을 구분한다). " +
			"SQL 본문·실행계획 존재까지 붙인다. 블로킹 짝은 db_blocking, 커넥션 포화는 read_timeseries의 몫.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				DB, From, To          string
				TopN                  int      `json:"top_n"`
				Sort                  string   `json:"sort"`
				FullTextFor           []string `json:"full_text_for"`
				PlanFor               []string `json:"plan_for"`
				ExcludeSelfMonitoring bool     `json:"exclude_self_monitoring"`
			}
			if err := json.Unmarshal(args, &in); err != nil {
				return nil, fmt.Errorf(`인자 오류: {"db": "<uuid>", "from": "<RFC3339|now-2h>", "to": "<RFC3339|now>"} 필요(from·to 생략 가능)`)
			}
			if !uuidRe.MatchString(in.DB) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — database 대상의 순수 UUID 필요", in.DB)
			}
			if in.Sort == "" {
				in.Sort = "total_ms"
			}
			if !sqSortAxes[in.Sort] {
				return nil, fmt.Errorf("sort=%q는 허용값이 아님 — total_ms|ratio_p50|latency_p50|latency_max|execs 중 하나", in.Sort)
			}
			if in.TopN <= 0 {
				in.TopN = sqDefaultTopN
			}
			if len(in.FullTextFor) > sqMaxFullTextFor {
				return nil, fmt.Errorf("full_text_for는 최대 %d개 — %d개 받음", sqMaxFullTextFor, len(in.FullTextFor))
			}
			if len(in.PlanFor) > sqMaxPlanFor {
				return nil, fmt.Errorf("plan_for는 최대 %d개 — %d개 받음", sqMaxPlanFor, len(in.PlanFor))
			}
			now := nowFn().UTC()
			from, to, basis, err := sqWindow(in.From, in.To, firstEvent, lastEvent, now)
			if err != nil {
				return nil, err
			}
			return dbSlowQueries(ctx, ch, pg, sqReq{
				target: in.DB, from: from, to: to, topN: in.TopN, sort: in.Sort,
				fullTextFor: in.FullTextFor, planFor: in.PlanFor,
				excludeSelf: in.ExcludeSelfMonitoring, windowBasis: basis,
			})
		},
	}
}

type sqReq struct {
	target               string
	from, to             time.Time
	topN                 int
	sort                 string
	fullTextFor, planFor []string
	excludeSelf          bool
	windowBasis          string
}

// sqWindow는 창을 해석한다(db_blocking과 같은 관례 — 창 전역 스캔이 핵심
// 이므로 조사자에게 창을 강요하지 않는다).
func sqWindow(fromS, toS string, firstEvent, lastEvent, now time.Time) (time.Time, time.Time, string, error) {
	var basis []string
	var from, to time.Time
	switch {
	case fromS != "":
		t, err := parseFlexTime(fromS, now)
		if err != nil {
			return from, to, "", fmt.Errorf("from 시간 오류: %v", err)
		}
		from, basis = t, append(basis, "from=인자")
	case !firstEvent.IsZero():
		from, basis = firstEvent.UTC(), append(basis, "from=인시던트 창 시작(seed)")
	default:
		from, basis = now.Add(-2*time.Hour), append(basis, "from=now-2h(seed 창 없음)")
	}
	switch {
	case toS != "":
		t, err := parseFlexTime(toS, now)
		if err != nil {
			return from, to, "", fmt.Errorf("to 시간 오류: %v", err)
		}
		to, basis = t, append(basis, "to=인자")
	case !lastEvent.IsZero():
		to, basis = lastEvent.UTC(), append(basis, "to=인시던트 창 끝(seed)")
	default:
		to, basis = now, append(basis, "to=now(seed 창 없음)")
	}
	if !from.Before(to) {
		return from, to, "", fmt.Errorf("시간창 오류: from < to 필요 (해석된 값 from=%s to=%s)",
			from.Format(time.RFC3339), to.Format(time.RFC3339))
	}
	return from, to, strings.Join(basis, ", "), nil
}

// sqRow는 원천 한 행이다(폴 내 접기 전).
type sqRow struct {
	ts      time.Time
	sqlKey  string
	mode    string
	lat     float64 // ms 정규화 후
	execs   float64
	totalMS float64
	hasTot  bool
	io      map[string]float64
	text    string
	scope   map[string]string
}

// sqPoll은 폴 내 접기 결과다(§14.2 1단).
type sqPoll struct {
	ts         time.Time
	lat        float64
	execs      float64
	totalMS    float64
	rows       int
	unweighted bool
}

// sqGroup은 (sql_key, mode) 하나의 창 접기 결과다(§14.2 2단).
type sqGroup struct {
	sqlKey, mode string
	polls        []sqPoll
	io           map[string][]float64
	text         string
	scope        map[string]map[string]bool
	dupPolls     int
	unweighted   int
}

func dbSlowQueries(ctx context.Context, ch *CH, pg *sql.DB, r sqReq) (any, error) {
	var acc truncAcc
	obs := &TimeRange{From: r.from.UTC(), To: r.to.UTC()}
	// pg=semantic_auxiliary(deps.go, 2b): PG 실패는 도구 실패가 아니라
	// 부분 결손이다 — 어느 부위(레지스트리·본문 사전·플랜)가 빠졌는지를
	// pgDegraded로 모아 coverage에 분류 토큰으로 명시한다(§15.2-3).
	pgDegraded := map[string]string{}
	engine, engineVer, engineSrc, pgTok := sqResolveEngine(ctx, ch, pg, r.target, r.from, r.to)
	if pgTok != "" {
		pgDegraded["engine_registry"] = pgTok
	}

	if engine == "" {
		env := Envelope{
			Status: "no_data", NoDataReason: NoDataUnknown,
			Summary: fmt.Sprintf("이 대상의 engine을 확정할 수 없다 — PG 레지스트리(collector_dpm_engine_version)에도 없고 "+
				"CH top-SQL 원천에도 행이 없다. top-SQL 미수집인지 대상 종류가 다른지 이 도구로는 가를 수 없다. 창: %s", r.windowBasis),
			AssessmentBasis: "engine 미확정 — 관측 없음이 아니라 대상 성격 판별 불가",
			ObservedRange:   obs,
		}
		if pgTok != "" {
			env.Summary = fmt.Sprintf("이 대상의 engine을 확정할 수 없다 — PG 레지스트리 조회가 실패했고(%s) "+
				"CH top-SQL 원천에도 행이 없다. '레지스트리에 없음'이 아니라 '레지스트리를 못 봄'이다. 창: %s", pgTok, r.windowBasis)
			env.Findings = []Finding{{"kind": "coverage", "source_errors": map[string]string{"pg": pgTok}}}
		}
		return env, nil
	}
	if sqNoTopSQL[engine] {
		return Envelope{
			Status: "no_data", NoDataReason: NoDataNotCollected,
			Summary: fmt.Sprintf("engine=%s에는 top-SQL 수집기가 없다(생산자 소스 기준) — 느린 쿼리가 없다는 뜻이 아니라 "+
				"이 원천으로 답할 수 없다는 뜻이다. 창: %s", engine, r.windowBasis),
			AssessmentBasis: "engine에 top-SQL 원천 부재 — 측정해서 없는 것이 아니다",
			ObservedRange:   obs,
			Findings:        []Finding{{"kind": "coverage", "engine": engine, "engine_version": engineVer, "engine_source": engineSrc}},
		}, nil
	}

	spec, known := sqEngines[engine]
	if !known {
		// 열린 집합(§14.1) — 공통 경로를 타되 단위·키를 단정하지 않는다.
		spec = sqEngine{latKey: "avgExecTime", latUnit: "unknown", execKey: "calls", contract: "unknown"}
	}

	rows, rowsHit, otherEngine, err := sqFetchWindow(ctx, ch, r.target, engine, spec, r.from, r.to, &acc)
	if err != nil {
		return nil, err
	}
	if len(rows) == 0 {
		return sqNoRows(ctx, ch, r, engine, engineVer, engineSrc, obs, &acc, pgDegraded)
	}

	groups, pollsInWindow := sqFold(rows)
	base, basePolls, err := sqFetchBaseline(ctx, ch, r.target, engine, spec, r.from.Add(-sqBaselineSpan), r.from, &acc)
	if err != nil {
		return nil, err
	}

	// 본문 결손 채우기(§14.6): body에 없으면 사전 조인. engine으로
	// 가정하지 않고 값으로 판단한다.
	need := []string{}
	for _, g := range groups {
		if g.text == "" {
			need = append(need, g.sqlKey)
		}
	}
	dict, dictMiss, dictTok := sqFetchTexts(ctx, pg, r.target, need)
	if dictTok != "" {
		pgDegraded["sql_text_dictionary"] = dictTok
	}

	items := make([]*sqItem, 0, len(groups))
	for _, g := range groups {
		it := sqBuild(g, base, basePolls, pollsInWindow, spec, engine, dict)
		items = append(items, it)
	}
	excludedSelf := 0
	if r.excludeSelf {
		kept := items[:0]
		for _, it := range items {
			if it.selfMon {
				excludedSelf++
				continue
			}
			kept = append(kept, it)
		}
		items = kept
	}
	sqSort(items, r.sort)
	total := len(items)
	truncated := total > r.topN
	if truncated {
		items = items[:r.topN]
	}

	// 플랜 요약은 반환 행에 한해 조회한다(§14.7).
	planKeys := []string{}
	if spec.hasPlan {
		for _, it := range items {
			planKeys = append(planKeys, it.sqlKey)
		}
	}
	plans, planTok := sqFetchPlanSummaries(ctx, pg, r.target, planKeys)
	if planTok != "" {
		pgDegraded["plan_summaries"] = planTok
	}

	findings := make([]Finding, 0, len(items)+3)
	refs := []string{}
	anomalous := false
	for i, it := range items {
		f, fr := it.finding(r.target, i+1, plans)
		findings = append(findings, f)
		refs = append(refs, fr...)
		if it.verdict == "slower" && !it.selfMon {
			anomalous = true
		}
	}
	if d := sqDrilldownText(ctx, items, r.fullTextFor, pg, r.target, dict); d != nil {
		findings = append(findings, d...)
	}
	if d := sqDrilldownPlan(ctx, pg, r.target, engine, spec, items, r.planFor); d != nil {
		findings = append(findings, d...)
	}

	status := "normal"
	if anomalous {
		status = "anomalous"
	}
	cov := sqCoverage(engine, engineVer, engineSrc, spec, r, len(rows), rowsHit, otherEngine,
		pollsInWindow, basePolls, total, truncated, dictMiss, excludedSelf, items, pgDegraded)
	findings = append(findings, cov)

	return Envelope{
		Status:          status,
		Summary:         sqSummary(items, total, truncated, status, engine, r),
		AssessmentBasis: sqBasis(items, pollsInWindow, basePolls),
		Findings:        findings,
		ObservedRange:   obs,
		Truncated:       truncated,
		QueryTruncated:  bool(acc),
		Refs:            refs,
		// sql_key 축 절단(§5.1 계약 2) — total은 접기 후 sql_key 총수,
		// 반환은 top-N. 0건이어도 싣는다(관측 이력 없음의 원천).
		Scopes: []QueryScope{qscope("sql", total, len(items))},
	}, nil
}

// sqResolveEngine은 대상의 engine을 정한다(§14.9): PG 레지스트리 우선,
// 없으면 CH 관측(창 → 전 기간). pgTok은 레지스트리 조회 실패의 분류
// 토큰이다(§15.2-3, 2b — 종전엔 "레지스트리에 없음"과 "PG 죽음"이 같은
// CH 폴백으로 뭉개졌다). 실패해도 CH 폴백은 그대로 탄다(pg=aux).
func sqResolveEngine(ctx context.Context, ch *CH, pg *sql.DB, target string, from, to time.Time) (engine, version, source, pgTok string) {
	if pg != nil {
		var e, v string
		err := pg.QueryRowContext(ctx, `SELECT engine, COALESCE(version, '') FROM collector_dpm_engine_version WHERE target_id = $1`, target).Scan(&e, &v)
		switch {
		case err == nil && e != "":
			return e, v, "pg:collector_dpm_engine_version", ""
		case err != nil && err != sql.ErrNoRows:
			pgTok = beDetail(pgErr(err))
		}
	}
	if ch == nil {
		return "", "", "", pgTok
	}
	for _, q := range []struct{ where, src string }{
		{`AND timestamp >= parseDateTime64BestEffort({from:String}, 9) AND timestamp < parseDateTime64BestEffort({to:String}, 9)`, "ch:window"},
		{``, "ch:any"},
	} {
		// LIMIT 1 — 단일 행 보장, 절단 불가 표면이라 표식은 버린다.
		rows, _, err := ch.Query(ctx, `SELECT engine FROM dpm_topsql_local WHERE target_id = {target:String} `+q.where+` LIMIT 1`,
			map[string]string{"target": target, "from": chTime(from), "to": chTime(to)})
		if err == nil && len(rows) > 0 {
			if e, _ := rows[0]["engine"].(string); e != "" {
				return e, "", q.src, pgTok
			}
		}
	}
	return "", "", "", pgTok
}

// sqSelectExprs는 engine별 SELECT 식을 만든다.
func sqSelectExprs(engine string, spec sqEngine) string {
	lat := fmt.Sprintf("JSONExtractFloat(body, '%s')", spec.latKey)
	if spec.latUnit == "us" {
		lat += " / 1000"
	} else if spec.latUnit == "unknown" {
		// 미검증 engine: ms 키가 없으면 μs 키를 쓰되 나누지 않는다 —
		// 단위를 모른다는 사실을 latency_unit_source로 싣는다.
		lat = "if(JSONExtractFloat(body, 'avgExecTime') != 0, JSONExtractFloat(body, 'avgExecTime'), JSONExtractFloat(body, 'avgElapsedTime'))"
	}
	b := &strings.Builder{}
	fmt.Fprintf(b, "toString(timestamp) AS ts, %s AS sql_key, ", sqKeyExpr(engine))
	fmt.Fprintf(b, "if(log_attributes['mode'] = '', 'legacy', log_attributes['mode']) AS mode, ")
	fmt.Fprintf(b, "%s AS lat_ms, JSONExtractFloat(body, '%s') AS execs, ", lat, spec.execKey)
	if spec.totalKey != "" {
		div := ""
		if spec.latUnit == "us" {
			div = " / 1000"
		}
		fmt.Fprintf(b, "JSONExtractFloat(body, '%s')%s AS total_ms, 1 AS has_total, ", spec.totalKey, div)
	} else {
		b.WriteString("0 AS total_ms, 0 AS has_total, ")
	}
	for i, io := range spec.io {
		div := ""
		if io.div1000 {
			div = " / 1000"
		}
		fmt.Fprintf(b, "JSONExtractFloat(body, '%s')%s AS io%d, ", io.key, div, i)
	}
	for i, sc := range spec.scope {
		fmt.Fprintf(b, "log_attributes['%s'] AS sc%d, ", sc[1], i)
	}
	b.WriteString("JSONExtractString(body, 'sqlText') AS sql_text, engine")
	return b.String()
}

func sqFetchWindow(ctx context.Context, ch *CH, target, engine string, spec sqEngine, from, to time.Time, acc *truncAcc) ([]sqRow, bool, int, error) {
	q := fmt.Sprintf(`SELECT %s FROM dpm_topsql_local
		WHERE target_id = {target:String}
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		ORDER BY timestamp LIMIT %d`, sqSelectExprs(engine, spec), sqRowCap)
	raw, tr, err := ch.Query(ctx, q, map[string]string{"target": target, "from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, false, 0, fmt.Errorf("db_slow_queries 창 조회: %w", err)
	}
	acc.note(tr)
	out := make([]sqRow, 0, len(raw))
	other := 0
	for _, m := range raw {
		if e, _ := m["engine"].(string); e != engine {
			other++
			continue
		}
		ts, err := time.Parse("2006-01-02 15:04:05.999999999", strings.TrimSuffix(fmt.Sprint(m["ts"]), " +0000 UTC"))
		if err != nil {
			continue
		}
		row := sqRow{
			ts: ts.UTC(), sqlKey: fmt.Sprint(m["sql_key"]), mode: fmt.Sprint(m["mode"]),
			lat: asFloat(m["lat_ms"]), execs: asFloat(m["execs"]),
			totalMS: asFloat(m["total_ms"]), hasTot: asInt(m["has_total"]) == 1,
			io: map[string]float64{}, scope: map[string]string{},
		}
		row.text, _ = m["sql_text"].(string)
		for i, io := range spec.io {
			row.io[io.field] = asFloat(m[fmt.Sprintf("io%d", i)])
		}
		for i, sc := range spec.scope {
			if v, _ := m[fmt.Sprintf("sc%d", i)].(string); v != "" {
				row.scope[sc[0]] = v
			}
		}
		out = append(out, row)
	}
	return out, len(raw) >= sqRowCap, other, nil
}

// sqFold는 2단 접기다(§14.2). 반환: (sql_key, mode)별 그룹 + 창 내 대상의
// distinct 폴 수.
func sqFold(rows []sqRow) ([]*sqGroup, int) {
	type gk struct{ key, mode string }
	byG := map[gk]*sqGroup{}
	order := []gk{}
	allPolls := map[time.Time]bool{}
	// 1단 — (그룹, 폴)별 실행횟수 가중 접기.
	type pk struct {
		g  gk
		ts time.Time
	}
	acc := map[pk]*struct {
		sumLatX, sumExec, sumLat, sumTot float64
		n                                int
	}{}
	pkOrder := []pk{}
	for _, r := range rows {
		allPolls[r.ts] = true
		g := gk{r.sqlKey, r.mode}
		if byG[g] == nil {
			byG[g] = &sqGroup{sqlKey: r.sqlKey, mode: r.mode, io: map[string][]float64{},
				scope: map[string]map[string]bool{}}
			order = append(order, g)
		}
		grp := byG[g]
		if r.text != "" && grp.text == "" {
			grp.text = r.text
		}
		for k, v := range r.io {
			grp.io[k] = append(grp.io[k], v)
		}
		for k, v := range r.scope {
			if grp.scope[k] == nil {
				grp.scope[k] = map[string]bool{}
			}
			grp.scope[k][v] = true
		}
		p := pk{g, r.ts}
		if acc[p] == nil {
			acc[p] = &struct {
				sumLatX, sumExec, sumLat, sumTot float64
				n                                int
			}{}
			pkOrder = append(pkOrder, p)
		}
		a := acc[p]
		a.sumLatX += r.lat * r.execs
		a.sumExec += r.execs
		a.sumLat += r.lat
		a.sumTot += r.totalMS
		a.n++
	}
	for _, p := range pkOrder {
		a := acc[p]
		poll := sqPoll{ts: p.ts, execs: a.sumExec, totalMS: a.sumTot, rows: a.n}
		if a.sumExec > 0 {
			poll.lat = a.sumLatX / a.sumExec
		} else {
			poll.lat = a.sumLat / float64(a.n)
			poll.unweighted = true
		}
		g := byG[p.g]
		g.polls = append(g.polls, poll)
		if a.n > 1 {
			g.dupPolls++
		}
		if poll.unweighted {
			g.unweighted++
		}
	}
	out := make([]*sqGroup, 0, len(order))
	for _, g := range order {
		grp := byG[g]
		sort.Slice(grp.polls, func(i, j int) bool { return grp.polls[i].ts.Before(grp.polls[j].ts) })
		out = append(out, grp)
	}
	return out, len(allPolls)
}

// sqBaseStat은 기준선 창의 (sql_key, mode)별 집계다(서버측 계산).
type sqBaseStat struct {
	p50, max float64
	polls    int
}

func sqFetchBaseline(ctx context.Context, ch *CH, target, engine string, spec sqEngine, from, to time.Time, acc *truncAcc) (map[string]sqBaseStat, int, error) {
	inner := sqSelectExprs(engine, spec)
	q := fmt.Sprintf(`
		SELECT sql_key, mode, quantileExact(0.5)(lat_poll) AS p50, max(lat_poll) AS mx, count() AS polls
		FROM (
		  SELECT sql_key, mode, ts,
		         if(sum(execs) > 0, sum(lat_ms * execs) / sum(execs), avg(lat_ms)) AS lat_poll
		  FROM (SELECT %s FROM dpm_topsql_local
		        WHERE target_id = {target:String} AND engine = {engine:String}
		          AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		          AND timestamp <  parseDateTime64BestEffort({to:String}, 9))
		  GROUP BY sql_key, mode, ts
		)
		GROUP BY sql_key, mode`, inner)
	p := map[string]string{"target": target, "engine": engine, "from": chTime(from), "to": chTime(to)}
	raw, tr, err := ch.Query(ctx, q, p)
	if err != nil {
		return nil, 0, fmt.Errorf("db_slow_queries 기준선 조회: %w", err)
	}
	acc.note(tr)
	out := map[string]sqBaseStat{}
	for _, m := range raw {
		k := fmt.Sprint(m["sql_key"]) + "\x00" + fmt.Sprint(m["mode"])
		out[k] = sqBaseStat{p50: asFloat(m["p50"]), max: asFloat(m["mx"]), polls: asInt(m["polls"])}
	}
	// uniqExact() 단일 행 집계 — 절단 불가 표면이라 표식은 버린다.
	pollRows, _, err := ch.Query(ctx, `SELECT uniqExact(timestamp) AS n FROM dpm_topsql_local
		WHERE target_id = {target:String} AND engine = {engine:String}
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)`, p)
	polls := 0
	if err == nil && len(pollRows) > 0 {
		polls = asInt(pollRows[0]["n"])
	}
	return out, polls, nil
}

// sqItem은 findings 한 행의 계산 결과다.
type sqItem struct {
	sqlKey, mode  string
	engine        string
	idKind        string
	latP50        float64
	latMax        float64
	latMaxAt      time.Time
	latLast       float64
	unitSource    string
	execsTotal    float64
	execsP50      float64
	totalMS       float64
	totalSrc      string
	polls         int
	pollsInWindow int
	presence      float64
	firstSeen     time.Time
	lastSeen      time.Time
	base          sqBaseStat
	basePresence  float64
	ratioP50      float64
	ratioMax      float64
	hasRatioP50   bool
	hasRatioMax   bool
	verdict       string
	slowerBasis   string
	text          string
	textSource    string
	textKind      string
	textTrunc     bool
	textLen       int
	selfMon       bool
	selfPattern   string
	selfKnown     bool
	io            map[string]float64
	scope         map[string][]string
	dupPolls      int
	unweighted    int
}

// sqBuild는 한 그룹을 판정까지 마친 행으로 만든다(§14.3·14.5).
func sqBuild(g *sqGroup, base map[string]sqBaseStat, basePolls, pollsInWindow int,
	spec sqEngine, engine string, dict map[string]sqDictText) *sqItem {

	lats := make([]float64, 0, len(g.polls))
	var execsTotal, totalMS float64
	execsPer := make([]float64, 0, len(g.polls))
	hasTot := false
	it := &sqItem{
		sqlKey: g.sqlKey, mode: g.mode, engine: engine, idKind: spec.idKind,
		unitSource: spec.latUnit, polls: len(g.polls), pollsInWindow: pollsInWindow,
		io: map[string]float64{}, scope: map[string][]string{},
		dupPolls: g.dupPolls, unweighted: g.unweighted,
	}
	for _, p := range g.polls {
		lats = append(lats, p.lat)
		execsPer = append(execsPer, p.execs)
		execsTotal += p.execs
		totalMS += p.totalMS
		if p.totalMS != 0 {
			hasTot = true
		}
		if p.lat > it.latMax {
			it.latMax, it.latMaxAt = p.lat, p.ts
		}
	}
	it.latP50 = sqP50(lats)
	it.execsP50 = sqP50(execsPer)
	it.latLast = g.polls[len(g.polls)-1].lat
	it.firstSeen, it.lastSeen = g.polls[0].ts, g.polls[len(g.polls)-1].ts
	if pollsInWindow > 0 {
		it.presence = float64(it.polls) / float64(pollsInWindow)
	}
	// total_ms — 원천 키가 있으면 그 합, 없으면 avg×execs 파생(§14.5).
	if spec.totalKey != "" && hasTot {
		it.totalMS, it.totalSrc = totalMS, "source_key"
	} else {
		d := 0.0
		for _, p := range g.polls {
			d += p.lat * p.execs
		}
		it.totalMS, it.totalSrc = d, "derived"
	}
	if g.mode == "legacy" {
		// 누적 카운터를 폴별로 더하면 중복 계산이다(§14.2).
		it.execsTotal, it.totalMS, it.totalSrc = 0, 0, "unavailable_legacy"
	} else {
		it.execsTotal = execsTotal
	}
	for k, vs := range g.io {
		it.io[k] = sqP50(vs)
	}
	for k, set := range g.scope {
		vals := make([]string, 0, len(set))
		for v := range set {
			vals = append(vals, v)
		}
		sort.Strings(vals)
		it.scope[k] = vals
	}

	// 본문(§14.6) — body 우선, 없으면 사전.
	it.text, it.textSource, it.textKind = g.text, "ch_body", sqTextKind(engine, "ch_body")
	if it.text == "" {
		if d, ok := dict[g.sqlKey]; ok && d.text != "" {
			it.text, it.textSource, it.textKind = d.text, "dictionary", sqTextKind(engine, "dictionary")
		} else {
			it.textSource, it.textKind = "none", ""
		}
	}
	it.textLen = len(it.text)
	if it.textLen > sqTextTrunc {
		it.text, it.textTrunc = it.text[:sqTextTrunc], true
	}
	// 자기관측 판별(§14.8) — 본문이 없으면 판별 불가이므로 단정하지 않는다.
	if it.textSource != "none" {
		it.selfKnown = true
		low := strings.ToLower(it.text)
		for _, p := range sqSelfPatterns {
			if strings.Contains(low, p) {
				it.selfMon, it.selfPattern = true, p
				break
			}
		}
	}

	// 판정(§14.3).
	it.base = base[g.sqlKey+"\x00"+g.mode]
	if basePolls > 0 {
		it.basePresence = float64(it.base.polls) / float64(basePolls)
	}
	it.verdict = sqVerdict(it)
	return it
}

// sqVerdict는 §14.3의 판정식이다. 두 팔은 OR — 최대 팔의 기준선은 24시간
// 중 한 번의 급등으로 정해져 긴 사건에서 침묵하기 때문이다(실측).
func sqVerdict(it *sqItem) string {
	if it.mode == "legacy" {
		return "not_assessable_legacy"
	}
	if it.polls < sqMinPolls || it.base.polls < sqMinPolls {
		return "insufficient_baseline"
	}
	if it.base.p50 > 0 {
		it.ratioP50, it.hasRatioP50 = it.latP50/it.base.p50, true
	}
	if it.base.max > 0 {
		it.ratioMax, it.hasRatioMax = it.latMax/it.base.max, true
	}
	med := it.hasRatioP50 && it.ratioP50 >= sqRatioThreshold
	mx := it.hasRatioMax && it.ratioMax >= sqRatioThreshold
	switch {
	case med && mx:
		it.slowerBasis = "both"
		return "slower"
	case med:
		it.slowerBasis = "median"
		return "slower"
	case mx:
		it.slowerBasis = "max"
		return "slower"
	case it.hasRatioP50 && it.ratioP50 <= 1/sqRatioThreshold:
		return "faster"
	case it.basePresence >= sqChronicPresence && it.hasRatioP50:
		return "chronic"
	}
	return "stable"
}

// sqP50은 nearest-rank 중앙값이다(CH quantileExact와 같은 위치 규약 —
// 창은 Go가, 기준선은 CH가 계산하므로 정의가 어긋나면 비가 뜻을 잃는다).
func sqP50(vals []float64) float64 {
	if len(vals) == 0 {
		return 0
	}
	s := append([]float64{}, vals...)
	sort.Float64s(s)
	i := len(s) / 2
	if i >= len(s) {
		i = len(s) - 1
	}
	return s[i]
}

func sqTextKind(engine, source string) string {
	switch {
	case engine == "postgresql":
		return "pgss_normalized_masked"
	case engine == "mysql" || engine == "mariadb":
		return "digest_normalized"
	case source == "dictionary":
		// Oracle 사전 텍스트의 정규화 여부는 자료에 없다 — 주장하지 않는다.
		return "dict_captured"
	}
	return "unknown"
}

func sqSort(items []*sqItem, axis string) {
	key := func(it *sqItem) float64 {
		switch axis {
		case "ratio_p50":
			return it.ratioP50
		case "latency_p50":
			return it.latP50
		case "latency_max":
			return it.latMax
		case "execs":
			return it.execsTotal
		}
		return it.totalMS
	}
	sort.SliceStable(items, func(i, j int) bool {
		// 자기관측 행은 같은 값에서 뒤로(§14.8 — 상한을 잡아먹지 않게).
		if items[i].selfMon != items[j].selfMon {
			return !items[i].selfMon
		}
		if key(items[i]) != key(items[j]) {
			return key(items[i]) > key(items[j])
		}
		return items[i].sqlKey < items[j].sqlKey
	})
}

func (it *sqItem) finding(target string, rank int, plans map[string]sqPlanSummary) (Finding, []string) {
	ref := fmt.Sprintf("ch:dpm_topsql_local:%s:sql:%s", target, it.sqlKey)
	refs := []string{ref}
	f := Finding{
		"kind": "sql", "sql_key": it.sqlKey, "engine": it.engine, "mode": it.mode,
		"latency_ms_p50": round3(it.latP50), "latency_ms_max": round3(it.latMax),
		"latency_ms_last": round3(it.latLast), "latency_unit_source": it.unitSource,
		"polls_seen": it.polls, "polls_in_window": it.pollsInWindow, "presence": round3(it.presence),
		"first_seen": it.firstSeen.UTC().Format(time.RFC3339), "last_seen": it.lastSeen.UTC().Format(time.RFC3339),
		"verdict": it.verdict, "rank_among_collected": rank,
		"execs_per_poll_p50": round3(it.execsP50),
		"refs":               refs,
	}
	if !it.latMaxAt.IsZero() {
		f["latency_ms_max_at"] = it.latMaxAt.UTC().Format(time.RFC3339)
	}
	if it.idKind != "" {
		f["id_kind"] = it.idKind
	}
	if it.presence < 0.5 {
		f["partial_presence"] = true
	}
	if it.mode != "legacy" {
		f["execs_total"] = round3(it.execsTotal)
		f["total_ms"] = round3(it.totalMS)
	}
	f["total_ms_source"] = it.totalSrc
	if len(it.scope) > 0 {
		f["scope"] = it.scope
	}
	if len(it.io) > 0 {
		io := map[string]float64{}
		for k, v := range it.io {
			io[k] = round3(v)
		}
		f["io"] = io
	}
	f["baseline_polls"] = it.base.polls
	if it.base.polls > 0 {
		f["baseline_latency_ms_p50"] = round3(it.base.p50)
		f["baseline_latency_ms_max"] = round3(it.base.max)
		f["baseline_presence"] = round3(it.basePresence)
	}
	if it.hasRatioP50 {
		f["ratio_p50"] = round3(it.ratioP50)
	}
	if it.hasRatioMax {
		f["ratio_max"] = round3(it.ratioMax)
	}
	if it.slowerBasis != "" {
		f["slower_basis"] = it.slowerBasis
	}
	if it.textSource != "" {
		f["sql_text_source"] = it.textSource
	}
	if it.text != "" {
		f["sql_text"] = it.text
		f["sql_text_kind"] = it.textKind
		f["sql_text_full_length"] = it.textLen
		if it.textTrunc {
			f["sql_text_truncated"] = true
		}
		if it.textSource == "dictionary" {
			refs = append(refs, fmt.Sprintf("pg:db_sql_text:%s:%s", target, it.sqlKey))
			f["refs"] = refs
		}
	}
	if it.selfKnown {
		f["self_monitoring"] = it.selfMon
		if it.selfMon {
			f["self_monitoring_pattern"] = it.selfPattern
		}
	}
	if ps, ok := plans[it.sqlKey]; ok {
		f["plan_records"] = ps.finding()
		refs = append(refs, fmt.Sprintf("pg:db_sql_plan:%s:%s", target, it.sqlKey))
		f["refs"] = refs
	}
	if it.dupPolls > 0 {
		f["duplicate_rows_in_poll"] = it.dupPolls
	}
	if it.unweighted > 0 {
		f["unweighted_polls"] = it.unweighted
	}
	return f, refs
}

func round3(v float64) float64 {
	return float64(int64(v*1000+sign(v)*0.5)) / 1000
}

func sign(v float64) float64 {
	if v < 0 {
		return -1
	}
	return 1
}

// asFloat는 CH JSONEachRow의 숫자를 float64로 바꾼다.
func asFloat(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case json.Number:
		f, _ := n.Float64()
		return f
	case string:
		var f float64
		fmt.Sscanf(n, "%g", &f)
		return f
	}
	return 0
}
