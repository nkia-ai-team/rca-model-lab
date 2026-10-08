// db_blocking 전체 개관(db 생략, 2026-10-06) — database 대상 전부를 같은 창에서 훑어
// 대상별 한 줄을 증거 강도 순으로 낸다.
//
// 왜 필요한가(하네스 대 원시 SQL 절제 실측, 같은 교사·블라인드): 서로 다른 두 DB에 독립
// 행 잠금이 동시에 걸린 캡처에서 도구 팔은 먼저 의심한 DB 하나만 db_blocking으로 보고 멈춰
// 두 번째 DB의 잠금을 보지 못했다. 원시 팔은 dpm_session_local을 target_id별로 한 번에
// 묶어 둘 다 찾았다. db가 필수라 DB 간 동시 경합이 표면 구조상 놓치기 쉬웠다 — 능력 결손이다.
//
// 판정·접기는 단일 DB 경로를 그대로 부른다: 폴 인벤토리(dbBlkPolls) → 판단 가능 여부
// (dbBlkJudgeable) → 사건 접기·정렬(dbBlkFoldEvents·dbBlkSortEvents) → 규모 판정식
// (blocked_peak>=dbBlkPeakThreshold), 경과 상위 세션(dbSessQueryHolders → dbSessGroupHolders).
// 발견은 search_targets type=database와 같은 명부 조회(inventoryTargets)다. 명부가 없거나
// 실패하면(PG는 이 도구에 optional) 세션 원천에서 창 내 행이 있는 target_id로 대체 발견한다.
//
// 순위는 수치로만 매긴다: 최대 막힌 세션 → 블로킹 폴 수 → 최장 세션 경과(ms 단위가 확인된
// engine 우선). 상태 문자열로 가르지 않는다 — 원천 문자열은 묶음 키 그대로 보여 줄 뿐이다.
// 대상별 상세(루트·짝·경합 객체·클라이언트 해석·SQL 본문)는 싣지 않는다 — 그 target_id로
// 다시 부른다. 응답은 글자 예산 안에서 상세 행 수를 줄여 맞춘다(학생은 앞 6000자만 본다).
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

const (
	dbFleetCharBudget   = 5000 // 응답 JSON 전체 글자 예산(학생 가시 6000자 안쪽 여유)
	dbFleetMaxHolders   = 2    // 상세 행당 경과 상위 세션 묶음 상한
	dbFleetInventoryCap = 1000 // 발견 상한(초과 시 절단 표시)
	dbFleetSection      = "fleet_db"
	dbFleetMoreSection  = "fleet_more"
	dbFleetClock        = "15:04:05"
	dbFleetAnomalous    = "anomalous"
	dbFleetNormal       = "normal"
	dbFleetNoData       = "no_data"
)

// dbFleetCaps는 (상세 행, 짧은 줄, 행 0 명단) 상한 단계다 — 예산을 넘으면 다음 단계로 줄인다.
// 행 0 명단 → 짧은 줄 → 상세 행 순으로 덜 중요한 것부터 줄인다(상세 행은 증거 강도 상위).
var dbFleetCaps = [][3]int{
	{5, 8, 6}, {5, 6, 3}, {4, 6, 3}, {4, 4, 2}, {3, 4, 2}, {3, 2, 1}, {2, 2, 1}, {2, 1, 0}, {1, 1, 0}, {1, 0, 0},
}

// dbFleetClass는 증거 강도 구획이다(작을수록 강하다 — 순위 1차 키).
type dbFleetClass int

const (
	dbFleetBlocking dbFleetClass = iota // 블로킹 사건 1건 이상
	dbFleetSessions                     // 블로킹 없음, 경과>0 사용자 세션 있음
	dbFleetPlain                        // 행은 있으나 블로킹·경과 세션 없음
	dbFleetNoAxis                       // 블로킹 판단 축 없음(공통 축·짝 키 계약 부재)
	dbFleetFailed                       // 조회 실패
	dbFleetQuiet                        // 창 내 세션 행 0
)

type dbFleetHolder struct {
	g *dbSessGroup
	k dbSessKeys
}

type dbFleetDB struct {
	t           inventoryTarget
	class       dbFleetClass
	engines     string
	polls       int
	blkPolls    int
	events      []dbBlkEvent // 정렬됨(가장 큰 사건이 앞)
	peak        int
	first, last time.Time
	holders     []dbFleetHolder
	errTok      string // 폴 인벤토리 조회 실패 토큰
	holderErr   string // 경과 상위 세션 조회 실패 토큰
}

// dbFleetInv는 발견 경로의 사실이다.
type dbFleetInv struct {
	capped bool
	pgErr  string // 명부 조회 실패(또는 미연결) 토큰 — 비어 있지 않으면 세션 원천으로 대체 발견했다
}

func (d dbFleetDB) label() string {
	name := d.t.Name
	if name == "" {
		name = d.t.Display
	}
	head := d.t.ID
	if d.engines != "" {
		head += " " + d.engines
	}
	if name != "" {
		head += "(" + name + ")"
	}
	return head
}

func (d dbFleetDB) status() string {
	if d.peak >= dbBlkPeakThreshold {
		return dbFleetAnomalous
	}
	return dbFleetNormal
}

// topAge는 순위용 최장 세션 경과다 — ms 단위가 확인된 engine의 값이 있으면 그것(known=true).
func (d dbFleetDB) topAge() (known bool, age float64) {
	for _, h := range d.holders {
		ms := h.k.ageUnit == "ms"
		switch {
		case ms && (!known || h.g.rep.maxAge > age):
			known, age = true, h.g.rep.maxAge
		case !ms && !known && h.g.rep.maxAge > age:
			age = h.g.rep.maxAge
		}
	}
	return known, age
}

// dbFleet은 db 생략 호출의 본체다.
func dbFleet(ctx context.Context, ch *CH, pg *sql.DB, from, to time.Time, windowBasis string) (any, error) {
	var acc truncAcc
	targets, inv, err := dbFleetTargets(ctx, ch, pg, from, to, &acc)
	if err != nil {
		return nil, err
	}
	obs := &TimeRange{From: from.UTC(), To: to.UTC()}
	if len(targets) == 0 {
		return dbFleetEmpty(inv, obs, windowBasis, bool(acc)), nil
	}
	dbs := make([]dbFleetDB, 0, len(targets))
	var firstErr error
	for _, t := range targets {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		d, err := dbFleetScan(ctx, ch, t, from, to, &acc)
		if err != nil && firstErr == nil {
			firstErr = err
		}
		dbs = append(dbs, d)
	}
	if firstErr != nil && dbFleetAllFailed(dbs) {
		return nil, firstErr // 세션 원천 전면 실패 — withBackendGuard가 no_data(backend_error)로 사상한다
	}
	dbFleetRank(dbs)
	return dbFleetBuild(dbs, inv, obs, windowBasis, bool(acc)), nil
}

// dbFleetTargets는 database 대상 명단이다. 명부(search_targets type=database와 같은 조회)가
// 정본이고, 명부를 못 보면 세션 원천의 창 내 target_id로 대체한다.
func dbFleetTargets(ctx context.Context, ch *CH, pg *sql.DB, from, to time.Time, acc *truncAcc) ([]inventoryTarget, dbFleetInv, error) {
	inv := dbFleetInv{pgErr: "not_configured"}
	if pg != nil {
		ts, err := inventoryTargets(ctx, pg, "db_blocking 대상 발견", "", "database", dbFleetInventoryCap+1, 0)
		if err == nil {
			inv.pgErr = ""
			if len(ts) > dbFleetInventoryCap {
				ts, inv.capped = ts[:dbFleetInventoryCap], true
			}
			return ts, inv, nil
		}
		if ctx.Err() != nil {
			return nil, inv, ctx.Err()
		}
		inv.pgErr = beDetail(err)
	}
	ts, capped, err := dbFleetCHTargets(ctx, ch, from, to, acc)
	inv.capped = capped
	return ts, inv, err
}

func dbFleetCHTargets(ctx context.Context, ch *CH, from, to time.Time, acc *truncAcc) ([]inventoryTarget, bool, error) {
	rows, tr, err := ch.Query(ctx, `
		SELECT toString(target_id) AS target, count() AS fleet_rows FROM dpm_session_local
		WHERE timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		GROUP BY target ORDER BY target
		LIMIT `+fmt.Sprint(dbFleetInventoryCap+1),
		map[string]string{"from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, false, fmt.Errorf("db_blocking 대상 발견(세션 원천): %w", err)
	}
	acc.note(tr)
	var out []inventoryTarget
	for _, r := range rows {
		if id := chStr(r["target"]); uuidRe.MatchString(id) {
			out = append(out, inventoryTarget{ID: id})
		}
	}
	capped := len(out) > dbFleetInventoryCap
	if capped {
		out = out[:dbFleetInventoryCap]
	}
	return out, capped, nil
}

// dbFleetScan은 대상 하나를 단일 DB 경로의 부품으로 훑는다.
func dbFleetScan(ctx context.Context, ch *CH, t inventoryTarget, from, to time.Time, acc *truncAcc) (dbFleetDB, error) {
	d := dbFleetDB{t: t}
	polls, err := dbBlkPolls(ctx, ch, t.ID, from, to, acc)
	if err != nil {
		d.class, d.errTok = dbFleetFailed, beDetail(err)
		return d, err
	}
	if len(polls) == 0 {
		d.class = dbFleetQuiet
		return d, nil
	}
	d.polls = len(polls)
	for _, p := range polls {
		if p.isBlockingPoll() {
			d.blkPolls++
		}
	}
	_, engineList, judgeable := dbBlkJudgeable(polls)
	d.engines = engineList
	d.holders, d.holderErr = dbFleetHolders(ctx, ch, t.ID, dbBlkEngineList(polls), from, to, acc)
	if !judgeable {
		d.class = dbFleetNoAxis
		return d, nil
	}
	d.events = dbBlkFoldEvents(polls)
	switch {
	case len(d.events) > 0:
		d.class = dbFleetBlocking
		dbBlkSortEvents(d.events)
		d.peak = d.events[0].peak
		d.first, d.last = dbFleetSpan(d.events)
	case len(d.holders) > 0:
		d.class = dbFleetSessions
	default:
		d.class = dbFleetPlain
	}
	return d, nil
}

// dbFleetSpan은 블로킹 사건 전체의 처음·마지막 관측 폴 시각이다.
func dbFleetSpan(events []dbBlkEvent) (time.Time, time.Time) {
	first, last := events[0].polls[0].ts, events[0].polls[len(events[0].polls)-1].ts
	for _, ev := range events[1:] {
		if t := ev.polls[0].ts; t.Before(first) {
			first = t
		}
		if t := ev.polls[len(ev.polls)-1].ts; t.After(last) {
			last = t
		}
	}
	return first, last
}

// dbFleetHolders는 engine별 경과 상위 세션 묶음이다(세션 키 계약이 있는 engine만).
func dbFleetHolders(ctx context.Context, ch *CH, target string, engines []string, from, to time.Time, acc *truncAcc) ([]dbFleetHolder, string) {
	var out []dbFleetHolder
	errTok := ""
	for _, eng := range engines {
		k, ok := dbSessEngines[eng]
		if !ok {
			continue
		}
		params, where := dbSessScope(target, eng, from, to)
		hs, err := dbSessQueryHolders(ctx, ch, k, where, params, acc)
		if err != nil {
			errTok = beDetail(err)
			continue
		}
		for _, g := range dbSessGroupHolders(hs) {
			out = append(out, dbFleetHolder{g: g, k: k})
		}
	}
	return out, errTok
}

func dbFleetAllFailed(dbs []dbFleetDB) bool {
	for _, d := range dbs {
		if d.class != dbFleetFailed {
			return false
		}
	}
	return true
}

// dbFleetRank는 증거 강도 순 정렬이다 — 구획 → 최대 막힌 세션 → 블로킹 폴 수 → 최장 세션 경과 → target_id.
func dbFleetRank(dbs []dbFleetDB) {
	sort.SliceStable(dbs, func(i, j int) bool { return dbFleetLess(dbs[i], dbs[j]) })
}

func dbFleetLess(a, b dbFleetDB) bool {
	if a.class != b.class {
		return a.class < b.class
	}
	if a.peak != b.peak {
		return a.peak > b.peak
	}
	if a.blkPolls != b.blkPolls {
		return a.blkPolls > b.blkPolls
	}
	ka, va := a.topAge()
	kb, vb := b.topAge()
	if ka != kb {
		return ka
	}
	if va != vb {
		return va > vb
	}
	return a.t.ID < b.t.ID
}

// dbFleetBuild는 예산 안에 드는 가장 너그러운 상한 단계로 봉투를 만든다.
func dbFleetBuild(dbs []dbFleetDB, inv dbFleetInv, obs *TimeRange, windowBasis string, qtrunc bool) Envelope {
	var env Envelope
	for _, caps := range dbFleetCaps {
		env = dbFleetEnvelope(dbs, caps, inv, obs, windowBasis, qtrunc)
		if dbFleetSize(env) <= dbFleetCharBudget {
			break
		}
	}
	return env
}

func dbFleetSize(env Envelope) int {
	b, err := json.Marshal(env)
	if err != nil {
		return 0
	}
	return utf8.RuneCount(b)
}

func dbFleetActive(dbs []dbFleetDB) int {
	n := 0
	for _, d := range dbs {
		if d.class <= dbFleetSessions {
			n++
		}
	}
	return n
}

func dbFleetEnvelope(dbs []dbFleetDB, caps [3]int, inv dbFleetInv, obs *TimeRange, windowBasis string, qtrunc bool) Envelope {
	active := dbFleetActive(dbs)
	detail := min(caps[0], active)
	var findings []Finding
	var refs []string // 봉투 refs = 실린 대상마다 대표 관측 좌표 하나(보유 세션 ref는 행 안에만 — 글자 예산)
	for _, d := range dbs[:detail] {
		f, fr := dbFleetDetailFinding(d)
		findings = append(findings, f)
		refs = append(refs, fr[0])
	}
	listed := detail
	if rest := dbs[detail:]; len(rest) > 0 || inv.pgErr != "" {
		f, fr, n := dbFleetMoreFinding(rest, caps[1], caps[2])
		if inv.pgErr != "" {
			// 결손 명시 규약(deps.go) — 명부(PG optional) 결손은 분류 토큰으로 밝힌다.
			f["source_errors"] = map[string]string{"pg_inventory": inv.pgErr}
		}
		findings = append(findings, f)
		refs = append(refs, fr...)
		listed += n
	}
	status, reason := dbFleetStatus(dbs)
	return Envelope{
		Status:          status,
		NoDataReason:    reason,
		AssessmentBasis: dbFleetBasis(inv),
		Summary:         dbFleetSummary(dbs, detail, inv, windowBasis),
		Findings:        findings,
		ObservedRange:   obs,
		Truncated:       listed < len(dbs) || detail < active || inv.capped,
		QueryTruncated:  qtrunc,
		Refs:            refs,
		// 대상 축 절단 — 총량은 발견 상한에 걸렸으면 하한(+1)이다.
		Scopes: []QueryScope{qscope("database", len(dbs)+boolInt(inv.capped), listed)},
	}
}

// dbFleetStatus — 대상 하나라도 단일 DB 판정식(blocked_peak>=임계)을 넘으면 anomalous.
func dbFleetStatus(dbs []dbFleetDB) (string, string) {
	anyPolls, anyFailed, allNoAxis := false, false, true
	for _, d := range dbs {
		if d.peak >= dbBlkPeakThreshold {
			return dbFleetAnomalous, ""
		}
		if d.polls > 0 {
			anyPolls = true
			allNoAxis = allNoAxis && d.class == dbFleetNoAxis
		}
		anyFailed = anyFailed || d.class == dbFleetFailed
	}
	switch {
	case !anyPolls && anyFailed:
		return dbFleetNoData, NoDataUnknown
	case !anyPolls:
		return dbFleetNoData, NoDataZeroObservations
	case allNoAxis:
		return dbFleetNoData, NoDataNotCollected
	}
	return dbFleetNormal, ""
}

func dbFleetDetailFinding(d dbFleetDB) (Finding, []string) {
	f := Finding{
		"section":         dbFleetSection,
		"target_id":       d.t.ID,
		"engine":          d.engines,
		"status":          d.status(),
		"polls":           d.polls,
		"blocking_events": len(d.events),
	}
	if name := d.t.Name; name != "" {
		f["name"] = name
	}
	var refs []string
	if len(d.events) > 0 {
		f["blocked_peak"] = d.peak
		f["blocking_polls"] = d.blkPolls
		f["first_seen"] = d.first.UTC().Format(time.RFC3339)
		f["last_seen"] = d.last.UTC().Format(time.RFC3339)
		refs = append(refs, dbBlkEventRef(d.t.ID, dbBlkRepPoll(d.events[0])))
	} else {
		refs = append(refs, dbBlkPollsRef(d.t.ID, d.polls))
	}
	var tops []Finding
	for i, h := range d.holders {
		if i >= dbFleetMaxHolders {
			break
		}
		tops = append(tops, dbFleetHolderItem(h))
		refs = append(refs, dbSessHolderRef(d.t.ID, h.g.rep))
	}
	f["top_sessions"] = dbSessNonNilF(tops)
	if d.holderErr != "" {
		f["source_errors"] = map[string]string{"ch_holders": d.holderErr}
	}
	f["refs"] = refs
	return f, refs
}

func dbFleetHolderItem(h dbFleetHolder) Finding {
	r := h.g.rep
	it := Finding{
		"session":             r.sid,
		"client":              r.client,
		"state":               dbSessStateKey(r.state, r.stype, r.wc, r.wev),
		"max_" + h.k.ageField: dbSessRound(r.maxAge),
	}
	if h.g.n > 1 {
		it["sessions_same_key"] = h.g.n
	}
	return it
}

// dbFleetMoreFinding은 상세 행 밖 대상의 짧은 줄과 창 내 행 0 명단이다.
func dbFleetMoreFinding(rest []dbFleetDB, briefCap, quietCap int) (Finding, []string, int) {
	var lines, quiet, refs []string
	quietTotal, omitted := 0, 0
	for _, d := range rest {
		switch {
		case d.class == dbFleetQuiet:
			quietTotal++
			if len(quiet) < quietCap {
				quiet = append(quiet, d.label())
				continue
			}
		case len(lines) < briefCap:
			lines = append(lines, dbFleetBriefLine(d))
			if r := dbFleetBriefRef(d); r != "" {
				refs = append(refs, r)
			}
			continue
		}
		omitted++
	}
	f := Finding{"section": dbFleetMoreSection, "databases": dbSessNonNil(lines), "refs": dbSessNonNil(refs)}
	if quietTotal > 0 {
		f["no_session_rows"] = dbSessNonNil(quiet)
		f["no_session_rows_total"] = quietTotal
	}
	if omitted > 0 {
		f["omitted"] = omitted
	}
	return f, refs, len(lines) + len(quiet)
}

func dbFleetBriefLine(d dbFleetDB) string {
	head := d.label()
	switch d.class {
	case dbFleetBlocking:
		return head + ": " + dbFleetBlkText(d)
	case dbFleetPlain:
		return head + ": " + dbFleetBlkText(d) + ", 경과>0 사용자 세션 없음"
	case dbFleetSessions:
		h := d.holders[0]
		return fmt.Sprintf("%s: %s, 최장 세션 %s=%g", head, dbFleetBlkText(d), h.k.ageField, dbSessRound(h.g.rep.maxAge))
	case dbFleetNoAxis:
		return fmt.Sprintf("%s: 블로킹 판단 축 없음(폴 %d개 — 공통 축·짝 키 계약 부재)", head, d.polls)
	}
	return fmt.Sprintf("%s: 세션 원천 조회 실패(%s) — 관측 없음이 아니라 결손", head, d.errTok)
}

// dbFleetBlkText는 블로킹 관측 한 토막이다(요약 줄·짧은 줄 공용).
func dbFleetBlkText(d dbFleetDB) string {
	if len(d.events) == 0 {
		return fmt.Sprintf("블로킹 없음(폴 %d개)", d.polls)
	}
	return fmt.Sprintf("블로킹 사건 %d건·최대 %d세션 막힘(%s~%s)", len(d.events), d.peak,
		d.first.UTC().Format(dbFleetClock), d.last.UTC().Format(dbFleetClock))
}

func dbFleetBriefRef(d dbFleetDB) string {
	switch d.class {
	case dbFleetBlocking:
		return dbBlkEventRef(d.t.ID, dbBlkRepPoll(d.events[0]))
	case dbFleetSessions, dbFleetPlain, dbFleetNoAxis:
		return dbBlkPollsRef(d.t.ID, d.polls)
	}
	return ""
}

func dbFleetBasis(inv dbFleetInv) string {
	src := "대상 발견 = search_targets type=database와 같은 명부 조회"
	if inv.pgErr != "" {
		src = "대상 발견 = 명부 조회 불가(" + inv.pgErr + ")로 세션 원천의 창 내 target_id 대체"
	}
	return src + ". 대상마다 단일 DB 모드와 같은 폴 인벤토리·사건 접기·판정식(blocked_peak " + fmt.Sprint(dbBlkPeakThreshold) +
		" 이상이면 anomalous)과 경과 상위 세션 조회. 순위 = 최대 막힌 세션 → 블로킹 폴 수 → 최장 세션 경과(ms 단위 engine 우선). " +
		"경과 키: PG max_tran_ms(트랜잭션 경과), Oracle 계열 max_wait_ms(현재 대기 경과), MySQL 계열 max_tran(단위 미확인). 짝·경합 객체·클라이언트 해석·SQL 본문은 단일 DB 모드의 몫"
}

// dbFleetSummary는 응답 앞부분만 읽는 소비자용 요약이다 — 구획별 대상 수 + 상세 행 대상의 한 줄씩.
func dbFleetSummary(dbs []dbFleetDB, detail int, inv dbFleetInv, windowBasis string) string {
	var b strings.Builder
	b.WriteString(dbFleetHeader(dbs, inv))
	for i, d := range dbs[:detail] {
		fmt.Fprintf(&b, " %d) %s", i+1, dbFleetClause(d))
	}
	if rest := len(dbs) - detail; rest > 0 {
		fmt.Fprintf(&b, " 나머지 %d개는 %s.", rest, dbFleetMoreSection)
	}
	fmt.Fprintf(&b, " 루트 세션·짝·경합 객체·클라이언트 해석·SQL 본문은 그 target_id를 db로 다시 부른다. 창: %s", windowBasis)
	return b.String()
}

var dbFleetClassLabels = map[dbFleetClass]string{
	dbFleetSessions: "블로킹 없이 세션 관측",
	dbFleetPlain:    "블로킹·경과 세션 없음",
	dbFleetNoAxis:   "블로킹 판단 축 없음",
	dbFleetFailed:   "조회 실패",
	dbFleetQuiet:    "창 내 세션 행 0",
}

func dbFleetHeader(dbs []dbFleetDB, inv dbFleetInv) string {
	counts := map[dbFleetClass]int{}
	anom := 0
	for _, d := range dbs {
		counts[d.class]++
		if d.peak >= dbBlkPeakThreshold {
			anom++
		}
	}
	var b strings.Builder
	fmt.Fprintf(&b, "database 대상 %d개 전체 개관 — 블로킹 관측 %d개", len(dbs), counts[dbFleetBlocking])
	if counts[dbFleetBlocking] > 0 {
		fmt.Fprintf(&b, "(막힌 세션 %d개 이상 %d개)", dbBlkPeakThreshold, anom)
	}
	for c := dbFleetSessions; c <= dbFleetQuiet; c++ {
		if counts[c] > 0 {
			fmt.Fprintf(&b, ", %s %d개", dbFleetClassLabels[c], counts[c])
		}
	}
	b.WriteString(".")
	if inv.pgErr != "" {
		fmt.Fprintf(&b, " 명부 조회 불가(%s) — 세션 원천에 창 내 행이 있는 target_id만 훑었다(행 없는 대상·이름은 빠진다).", inv.pgErr)
	}
	if inv.capped {
		fmt.Fprintf(&b, " 발견 상한 %d개에서 잘랐다.", dbFleetInventoryCap)
	}
	return b.String()
}

func dbFleetClause(d dbFleetDB) string {
	var b strings.Builder
	b.WriteString(d.label() + ": " + dbFleetBlkText(d))
	if len(d.holders) > 0 {
		h := d.holders[0]
		r := h.g.rep
		fmt.Fprintf(&b, ", 최장 세션 %s %s [%s] %s=%g", r.sid, r.client, dbSessStateKey(r.state, r.stype, r.wc, r.wev),
			h.k.ageField, dbSessRound(r.maxAge))
		if h.g.n > 1 {
			fmt.Fprintf(&b, " 외 같은 키 %d세션", h.g.n-1)
		}
	}
	b.WriteString(";")
	return b.String()
}

// dbFleetEmpty는 대상이 하나도 없을 때다 — 명부가 비었는지, 명부를 못 보고 세션 원천도 비었는지 가른다.
func dbFleetEmpty(inv dbFleetInv, obs *TimeRange, windowBasis string, qtrunc bool) Envelope {
	env := Envelope{
		Status:          dbFleetNoData,
		NoDataReason:    NoDataNotCollected,
		Summary:         "명부에 database 대상이 없다(search_targets type=database 0건) — DB 세션 관측 대상 자체가 없다. 창: " + windowBasis,
		AssessmentBasis: dbFleetBasis(inv),
		ObservedRange:   obs,
		QueryTruncated:  qtrunc,
	}
	if inv.pgErr != "" {
		env.NoDataReason = NoDataUnknown
		env.Summary = fmt.Sprintf("명부 조회 불가(%s)이고 세션 원천에도 창 내 행이 있는 대상이 없다 — database 대상이 없다는 뜻이 아니다. 창: %s",
			inv.pgErr, windowBasis)
	}
	return env
}
