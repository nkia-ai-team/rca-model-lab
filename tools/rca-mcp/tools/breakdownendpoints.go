// breakdown_endpoints — "이 서비스 안에서 어느 처리 구간이 느린가 /
// 에러인가"의 재설계 표면(스펙 §3.6 APM 메뉴 + §15 구현 설계,
// 2026-07-30 확정). get_trace_breakdown + get_slow_endpoints 두 자리를
// 하나가 승계한다(§12 expand_topology에 이은 두 번째 2→1).
//
//   - 행은 "진입점"이 아니라 세 구획으로 나눈 처리 구간이다(§15.2):
//     entry(SERVER·CONSUMER·INTERNAL 루트) / step(비루트 INTERNAL) /
//     egress(CLIENT·PRODUCER). 축이 kind마다 다른 것은 숨기지 않고
//     axis 필드로 드러낸다 — CLIENT를 span_name으로 접으면 `GET`·`POST`
//     두 줄이 되어 정보가 0이 된다(§15.1 정정 3).
//   - SERVER 한정 금지(§15.1 정정 1): 메시지 구동 4개 서비스는 SERVER
//     span 전수가 /actuator/health다. 교체된 get_slow_endpoints는 그
//     넷을 헬스체크 한 줄로 보고했다.
//   - 에러는 "실패"와 "거절"을 분리한다(§15.3): SERVER status_code는
//     5xx만 ERROR로 만들어 404 84,386건이 0으로 세어졌고(정정 4),
//     INTERNAL의 ERROR 85.9%는 NoResultException(정상 흐름)이며,
//     CLIENT는 4xx도 ERROR로 찍는다.
//   - 지연 판정은 세 팔 OR(§15.4 — 세 안을 배경 8,870버킷·사건 3구간에
//     동시 대입해 정한 것): L0 p50 3배(SERVER·CONSUMER 한정 — 배경
//     소음 11건이 전부 INTERNAL 스케줄러였다) · L1 p95 3배 & 300ms ·
//     L2 p95 3000ms(기준선 오염 보험). L1 단독은 저지연 실사건을
//     침묵시키고(p95 71~95ms), L0 단독은 재현이 1/8이었다.
//     Codex의 호출량 근접 MAD 기준선은 배경 오탐 59건으로 기각했다.
//   - self-time은 직계 자식만·부모별 선접기로 계산한다(§15.6):
//     자식 겹침이 32,775건 중 0건이라 합집합과 단순 합이 같다.
//   - 구획 교차 비율을 만들지 않는다(§15.7): 교체된 get_trace_breakdown의
//     time_share_of_server는 스케줄러 트레이스가 SERVER 밖에 있어
//     분모에 대응물이 없다.
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
	beDefaultTopN   = 20               // §15.8 — 30분 창 서비스당 진입점 최대 9행
	beMaxTopN       = 50               //
	beMinSample     = 30               // §15.4 전제 — 창·기준선 각각
	beRatioP50      = 3.0              // L0 — SERVER/CONSUMER 배경 3배 초과 0건
	beRatioP95      = 3.0              // L1
	beFloorP95Ms    = 300.0            // L1 절대 하한
	beMagnitudeMs   = 3000.0           // L2 — 배경 최대 창 p95 1,534ms의 약 2배
	beFailMinN      = 5                // E1·E2
	beFailMinRate   = 0.05             // E1 — 만성 저율 5xx(75창) 제외
	beFailRatio     = 2.0              // E2
	beFailDelta     = 0.10             // E2
	beRejMinN       = 20               // R1
	beRejRatio      = 2.0              // R1
	beRejDelta      = 0.10             // R1 — 배경 |Δ4xx| q99 10.23pp
	beChildLookout  = 60 * time.Second // §15.6 — 관측 최대 진입점 27.7초에 근거한 가정
	beBaselineWiden = 4                // 표본 부족 시 1회만 창 4배
	beMinWindow     = 15 * time.Minute // 기준선 최소 길이
)

// beRow는 한 구획·kind·label의 창 전역 집계다.
type beRow struct {
	section, kind, axis, label string
	n, uniqN                   int
	rootN                      int
	p50, p95, maxMs, totalMs   float64
	http5xx, http4xx, httpN    int
	errNoHTTP, statusErr       int
	errLabels                  []string
	slowTrace, slowSpan        string
	failTrace, failSpan        string
	targetID                   string
	// self-time(entry 한정, §15.6)
	selfP50, selfP95, childShare float64
	childSpanN, selfClamped      int
	hasSelf                      bool
	// 판정
	failedN, rejectedN  int
	failedRate, rejRate float64
	semantics           string
	base                *beRow
	baseState           string
	arms                []string
}

// beSectionOf는 (kind, 루트 여부) → 구획이다(§15.2). entry의 정의는
// "트레이스 루트"가 아니라 "이 서비스에서 일이 시작되는 지점"이다 —
// CONSUMER는 100% 부모를 갖지만 프로세스 관점에서는 진입점이다.
func beSectionOf(kind string, root bool) string {
	switch kind {
	case "SERVER", "CONSUMER":
		return "entry"
	case "INTERNAL":
		if root {
			return "entry"
		}
		return "step"
	default: // CLIENT, PRODUCER
		return "egress"
	}
}

// beSectionExpr는 CH측 구획 계산식이다. Go측 beSectionOf와 같은 규칙을
// 쓴다 — SERVER span의 절반은 루트가 아니므로(원격 호출자가 부모)
// 루트 여부로 GROUP BY를 쪼개면 같은 엔드포인트가 두 줄로 갈린다.
const beSectionExpr = `multiIf(
	span_kind IN ('SERVER','CONSUMER'), 'entry',
	span_kind = 'INTERNAL' AND parent_span_id = '', 'entry',
	span_kind = 'INTERNAL', 'step',
	'egress')`

// beLabelExpr — 축이 kind마다 다르다(§15.2). CLIENT는 span_name이
// HTTP 메서드뿐이라 목적지를 속성으로 복원한다(§15.1 정정 3).
const beLabelExpr = `multiIf(
	span_kind IN ('SERVER','CONSUMER','PRODUCER'), span_name,
	span_kind = 'CLIENT' AND span_attributes['db.system'] != '',
		concat('db ', span_attributes['db.system'], ' ',
		       if(span_attributes['db.operation'] != '', span_attributes['db.operation'], '-'), ' ',
		       if(span_attributes['db.sql.table'] != '', span_attributes['db.sql.table'], '-')),
	span_kind = 'CLIENT' AND span_attributes['url.full'] != '',
		concat('http ', span_attributes['http.request.method'], ' ',
		       span_attributes['server.address'],
		       if(span_attributes['server.port'] != '', concat(':', span_attributes['server.port']), '')),
	span_kind = 'CLIENT', concat('peer ', span_attributes['server.address']),
	concat(replaceOne(scope_name, 'io.opentelemetry.', ''), ' :: ', span_name))`

// beAxisOf는 label을 만든 축의 이름이다(응답에 드러낸다).
func beAxisOf(kind, label string) string {
	switch kind {
	case "SERVER":
		return "http.route"
	case "CONSUMER", "PRODUCER":
		return "messaging.destination"
	case "INTERNAL":
		return "framework_segment"
	case "CLIENT":
		switch {
		case strings.HasPrefix(label, "db "):
			return "db_call"
		case strings.HasPrefix(label, "http "):
			return "http_call"
		default:
			return "peer"
		}
	}
	return "span_name"
}

// beAggSQL은 창 집계 쿼리다. 기준선도 **완전히 같은 SQL**을 창만 바꿔
// 돌린다(§15.10) — 정의 불일치로 인한 가짜 비율을 막기 위해서다.
const beAggSQL = `
SELECT ` + beSectionExpr + ` AS section,
       span_kind AS kind,
       ` + beLabelExpr + ` AS label,
       count() AS n,
       uniqExact((trace_id, span_id)) AS uniq_n,
       countIf(parent_span_id = '') AS root_n,
       quantile(0.5)(duration_ns)/1e6 AS p50_ms,
       quantile(0.95)(duration_ns)/1e6 AS p95_ms,
       max(duration_ns)/1e6 AS max_ms,
       sum(duration_ns)/1e6 AS total_ms,
       countIf(toUInt16OrZero(span_attributes['http.response.status_code']) >= 500) AS http_5xx,
       countIf(toUInt16OrZero(span_attributes['http.response.status_code']) >= 400
               AND toUInt16OrZero(span_attributes['http.response.status_code']) < 500) AS http_4xx,
       countIf(span_attributes['http.response.status_code'] != '') AS http_n,
       countIf(span_attributes['http.response.status_code'] = '' AND status_code = 'ERROR') AS err_nohttp,
       countIf(status_code = 'ERROR') AS status_err,
       topKIf(2)(errlab, errlab != '') AS err_labels,
       argMax(toString(trace_id), duration_ns) AS slow_trace,
       argMax(toString(span_id), duration_ns) AS slow_span,
       argMaxIf(toString(trace_id), timestamp, is_fail) AS fail_trace,
       argMaxIf(toString(span_id), timestamp, is_fail) AS fail_span,
       any(resource_attributes['lucida.target_id']) AS target_id
FROM (
  SELECT *,
    if(span_attributes['error.type'] != '', span_attributes['error.type'],
       arrayFirst(x -> x != '',
         arrayMap(m -> m['exception.type'],
           arrayFilter((m, nm) -> nm = 'exception', events_attributes, events_name)))) AS errlab,
    (toUInt16OrZero(span_attributes['http.response.status_code']) >= 500
     OR (span_attributes['http.response.status_code'] = '' AND status_code = 'ERROR')) AS is_fail
  FROM otel_traces_local
  WHERE service_name = {svc:String}
    AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
    AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
)
GROUP BY section, kind, label`

// beSelfSQL은 entry 구획의 self-time이다(§15.6). 자식을 부모별로 먼저
// 접어 부모 중복 계상을 구조적으로 막고, 직계 자식만 빼서 ORM↔JDBC
// 중복 차감을 막는다(계측 중첩은 형제가 아니라 사슬이다 — 정정 7).
const beSelfSQL = `
WITH kids AS (
  SELECT trace_id, parent_span_id, sum(duration_ns) AS child_ns, count() AS child_n
  FROM otel_traces_local
  WHERE service_name = {svc:String}
    AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
    AND timestamp <  parseDateTime64BestEffort({kidto:String}, 9)
    AND parent_span_id != ''
  GROUP BY trace_id, parent_span_id
)
SELECT p.kind AS kind, p.label AS label,
       quantile(0.5)(p.dur - least(p.child, p.dur))/1e6 AS self_p50_ms,
       quantile(0.95)(p.dur - least(p.child, p.dur))/1e6 AS self_p95_ms,
       sum(p.child)/sum(p.dur) AS child_share,
       sum(p.childn) AS child_span_n,
       countIf(p.child > p.dur) AS self_clamped_n
FROM (
  SELECT e.span_kind AS kind, ` + beLabelExpr + ` AS label,
         toFloat64(e.duration_ns) AS dur,
         toFloat64(ifNull(k.child_ns, 0)) AS child,
         toUInt64(ifNull(k.child_n, 0)) AS childn
  FROM (
    SELECT * FROM otel_traces_local
    WHERE service_name = {svc:String}
      AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
      AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
      AND (span_kind IN ('SERVER','CONSUMER') OR (span_kind = 'INTERNAL' AND parent_span_id = ''))
  ) AS e
  LEFT JOIN kids AS k ON e.trace_id = k.trace_id AND e.span_id = k.parent_span_id
) AS p
GROUP BY kind, label`

// NewBreakdownEndpointsTool은 breakdown_endpoints 도구를 만든다.
// firstEvent/lastEvent는 창 미지정 시 폴백이다(다른 신 도구와 같은 규약).
func NewBreakdownEndpointsTool(ch *CH, firstEvent, lastEvent time.Time) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]string{"type": "string",
				"description": "service.name 정확일치(또는 lucida.target_id UUID — 서비스와 1:1)"},
			"from": map[string]string{"type": "string", "description": "UTC RFC3339"},
			"to":   map[string]string{"type": "string", "description": "UTC RFC3339"},
			"sections": map[string]any{"type": "array", "items": map[string]string{"type": "string"},
				"description": "entry(진입 처리) | step(프레임워크 구간) | egress(나가는 호출). 기본 셋 전부"},
			"top_n": map[string]string{"type": "integer", "description": "구획당 최대 행(기본 20, 상한 50)"},
		},
		"required": []string{"target"},
	})
	return llm.Tool{
		Name: "breakdown_endpoints",
		Description: "한 서비스 안에서 어느 처리 구간이 느린가/에러인가를 분해한다. " +
			"HTTP 엔드포인트뿐 아니라 메시지 소비·스케줄러·프레임워크 구간·나가는 호출까지 " +
			"세 구획으로 나눠 직전 창 대비 지연·실패 변화를 판정한다.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			return callBreakdownEndpoints(ctx, ch, args, firstEvent, lastEvent)
		},
	}
}

type beArgs struct {
	Target   string   `json:"target"`
	From     string   `json:"from"`
	To       string   `json:"to"`
	Sections []string `json:"sections"`
	TopN     int      `json:"top_n"`
}

func callBreakdownEndpoints(ctx context.Context, ch *CH, args json.RawMessage, firstEvent, lastEvent time.Time) (any, error) {
	var in beArgs
	if err := json.Unmarshal(args, &in); err != nil {
		return nil, fmt.Errorf(`인자 오류: {"target": "<service.name>", "from": "<RFC3339>", "to": "<RFC3339>"} 필요`)
	}
	if strings.TrimSpace(in.Target) == "" {
		return nil, fmt.Errorf("인자 오류: target 필수 — service.name 정확일치(또는 lucida.target_id)")
	}
	from, to, err := beWindow(in, firstEvent, lastEvent)
	if err != nil {
		return nil, err
	}
	topN := in.TopN
	if topN <= 0 {
		topN = beDefaultTopN
	}
	if topN > beMaxTopN {
		topN = beMaxTopN
	}
	want := beWantSections(in.Sections)

	var acc truncAcc

	// 대상 해소 — service_name 먼저, 없으면 lucida.target_id(§15.10).
	svc, resolvedBy, err := beResolve(ctx, ch, in.Target, from, to)
	if err != nil {
		return nil, err
	}
	if svc == "" {
		return beNoTarget(ctx, ch, in.Target, from, to), nil
	}

	rows, err := beAggregate(ctx, ch, svc, from, to, &acc)
	if err != nil {
		return nil, fmt.Errorf("breakdown_endpoints 창 집계: %w", err)
	}
	if len(rows) == 0 {
		return beNoSpans(ctx, ch, svc, resolvedBy, from, to), nil
	}

	// 기준선 — 직전 동일 길이 창. 표본 부족이면 1회만 4배로 넓힌다(§15.4).
	baseFrom, baseTo, baseState := beBaselineWindow(from, to)
	var baseRows map[string]*beRow
	if baseState != "unavailable" {
		br, err := beAggregate(ctx, ch, svc, baseFrom, baseTo, &acc)
		if err != nil {
			return nil, fmt.Errorf("breakdown_endpoints 기준선 집계: %w", err)
		}
		baseRows = beIndex(br)
		if beNeedWiden(rows, baseRows) {
			wFrom := to.Add(-beBaselineWiden * to.Sub(from))
			if wFrom.Before(baseFrom) {
				br2, err := beAggregate(ctx, ch, svc, wFrom, from, &acc)
				if err != nil {
					return nil, fmt.Errorf("breakdown_endpoints 확장 기준선 집계: %w", err)
				}
				baseFrom, baseState = wFrom, "widened"
				baseRows = beIndex(br2)
			}
		}
	}
	if len(baseRows) == 0 {
		baseState = "unavailable"
	}

	beJudge(rows, baseRows, baseState)

	// self-time — entry 구획 요청 시에만(§15.6).
	if want["entry"] {
		if err := beAttachSelf(ctx, ch, svc, from, to, rows, &acc); err != nil {
			return nil, fmt.Errorf("breakdown_endpoints self-time: %w", err)
		}
	}

	return beEnvelope(svc, resolvedBy, rows, want, topN, from, to,
		baseFrom, baseTo, baseState, &acc), nil
}

// beWindow는 창을 정한다. 미지정이면 seed 창(firstEvent~lastEvent)으로
// 폴백하고, 그것도 없으면 최근 30분이다.
func beWindow(in beArgs, firstEvent, lastEvent time.Time) (time.Time, time.Time, error) {
	if in.From == "" && in.To == "" {
		switch {
		case !firstEvent.IsZero() && !lastEvent.IsZero() && firstEvent.Before(lastEvent):
			return firstEvent.UTC(), lastEvent.UTC(), nil
		case !firstEvent.IsZero():
			return firstEvent.UTC(), firstEvent.Add(30 * time.Minute).UTC(), nil
		default:
			now := time.Now().UTC()
			return now.Add(-30 * time.Minute), now, nil
		}
	}
	f, e1 := time.Parse(time.RFC3339, in.From)
	t, e2 := time.Parse(time.RFC3339, in.To)
	if e1 != nil || e2 != nil || !f.Before(t) {
		return time.Time{}, time.Time{}, fmt.Errorf(
			"시간창 오류: from·to는 UTC RFC3339, from < to 필요 (받은 값 from=%q to=%q)", in.From, in.To)
	}
	return f.UTC(), t.UTC(), nil
}

func beWantSections(sel []string) map[string]bool {
	want := map[string]bool{}
	for _, s := range sel {
		switch strings.ToLower(strings.TrimSpace(s)) {
		case "entry":
			want["entry"] = true
		case "step":
			want["step"] = true
		case "egress":
			want["egress"] = true
		}
	}
	if len(want) == 0 {
		return map[string]bool{"entry": true, "step": true, "egress": true}
	}
	return want
}

// Compare the requested historical interval against stored observations.
// Wall-clock retention assumptions are invalid for restored captures.
func beBaselineWindow(from, to time.Time) (time.Time, time.Time, string) {
	span := to.Sub(from)
	if span < beMinWindow {
		span = beMinWindow
	}
	bf := from.Add(-span)
	return bf, from, "ok"
}

// beNeedWiden은 창 표본이 판정 하한을 넘는 행 중 기준선 표본이 모자란
// 것이 하나라도 있는지다.
func beNeedWiden(rows []*beRow, base map[string]*beRow) bool {
	for _, r := range rows {
		if r.uniqN < beMinSample {
			continue
		}
		b := base[beKey(r)]
		if b == nil || b.uniqN < beMinSample {
			return true
		}
	}
	return false
}

func beKey(r *beRow) string { return r.section + "\x00" + r.kind + "\x00" + r.label }

func beIndex(rows []*beRow) map[string]*beRow {
	m := make(map[string]*beRow, len(rows))
	for _, r := range rows {
		m[beKey(r)] = r
	}
	return m
}

// beResolve는 target을 service_name으로 해소한다. UUID면
// resource_attributes['lucida.target_id']로 역인용한다(서비스와 1:1 —
// §15.1). 창에 없으면 빈 문자열을 돌려준다.
func beResolve(ctx context.Context, ch *CH, target string, from, to time.Time) (string, string, error) {
	p := map[string]string{"t": target, "from": chTime(from), "to": chTime(to)}
	// LIMIT 1 — 절단 불가 표면이라 표식은 버린다.
	rows, _, err := ch.Query(ctx, `
		SELECT service_name,
		       countIf(service_name = {t:String}) AS by_name,
		       countIf(resource_attributes['lucida.target_id'] = {t:String}) AS by_target
		FROM otel_traces_local
		WHERE timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		  AND (service_name = {t:String} OR resource_attributes['lucida.target_id'] = {t:String})
		GROUP BY service_name ORDER BY by_name DESC, by_target DESC LIMIT 1`, p)
	if err != nil {
		return "", "", fmt.Errorf("breakdown_endpoints 대상 해소: %w", err)
	}
	if len(rows) == 0 {
		return "", "", nil
	}
	name, _ := rows[0]["service_name"].(string)
	by := "lucida.target_id"
	if asInt(rows[0]["by_name"]) > 0 {
		by = "service_name"
	}
	return name, by, nil
}

func beAggregate(ctx context.Context, ch *CH, svc string, from, to time.Time, acc *truncAcc) ([]*beRow, error) {
	raw, tr, err := ch.Query(ctx, beAggSQL, map[string]string{
		"svc": svc, "from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, err
	}
	acc.note(tr)
	out := make([]*beRow, 0, len(raw))
	for _, r := range raw {
		row := &beRow{
			section:   str(r["section"]),
			kind:      str(r["kind"]),
			label:     str(r["label"]),
			n:         asInt(r["n"]),
			uniqN:     asInt(r["uniq_n"]),
			rootN:     asInt(r["root_n"]),
			p50:       flt(r["p50_ms"]),
			p95:       flt(r["p95_ms"]),
			maxMs:     flt(r["max_ms"]),
			totalMs:   flt(r["total_ms"]),
			http5xx:   asInt(r["http_5xx"]),
			http4xx:   asInt(r["http_4xx"]),
			httpN:     asInt(r["http_n"]),
			errNoHTTP: asInt(r["err_nohttp"]),
			statusErr: asInt(r["status_err"]),
			slowTrace: str(r["slow_trace"]),
			slowSpan:  str(r["slow_span"]),
			failTrace: str(r["fail_trace"]),
			failSpan:  str(r["fail_span"]),
			targetID:  str(r["target_id"]),
		}
		if arr, ok := r["err_labels"].([]any); ok {
			for _, v := range arr {
				if s := str(v); s != "" {
					row.errLabels = append(row.errLabels, s)
				}
			}
		}
		row.axis = beAxisOf(row.kind, row.label)
		out = append(out, row)
	}
	// 실패/거절 파생은 집계의 일부다 — 호출자에게 맡기면 기준선 쪽을
	// 빠뜨려 base.rejRate가 0으로 남고, "기준선 2배" 조건이 항상 참이
	// 되어 만성 4xx가 rejection_shift로 발화한다(구현 중 라이브 실측으로
	// 잡은 결함: gateway 만성 51% 404가 4행에서 발화했다).
	beDeriveErrors(out)
	return out, nil
}

// beDeriveErrors는 §15.3의 실패/거절 분리를 적용한다. fallback 조건이
// "http status **문자열이 비어 있고**"인 것이 단위 가드다 —
// toUInt16OrZero가 빈 속성을 0으로 만들어 >=500 비교에서 조용히 빠진다.
func beDeriveErrors(rows []*beRow) {
	for _, r := range rows {
		isDB := r.kind == "CLIENT" && strings.HasPrefix(r.label, "db ")
		isHTTP := r.kind == "CLIENT" && strings.HasPrefix(r.label, "http ")
		switch {
		case r.kind == "SERVER":
			r.semantics = "http_server"
			r.failedN = r.http5xx + r.errNoHTTP
			r.rejectedN = r.http4xx
		case isHTTP:
			r.semantics = "http_client"
			r.failedN = r.http5xx + r.errNoHTTP
			r.rejectedN = r.http4xx
		case isDB:
			r.semantics = "db"
			r.failedN = r.statusErr
			r.rejectedN = -1
		case r.kind == "CLIENT":
			r.semantics = "http_client"
			r.failedN = r.statusErr
			r.rejectedN = -1
		default: // INTERNAL, CONSUMER, PRODUCER
			r.semantics = "framework"
			r.failedN = r.statusErr
			r.rejectedN = -1
		}
		if r.failedN == 0 && r.rejectedN <= 0 && len(r.errLabels) == 0 {
			r.semantics = "none"
		}
		if r.uniqN > 0 {
			r.failedRate = float64(r.failedN) / float64(r.uniqN)
			if r.rejectedN >= 0 && r.httpN > 0 {
				r.rejRate = float64(r.rejectedN) / float64(r.httpN)
			}
		}
	}
}

// beJudge는 §15.4의 여섯 팔을 적용한다.
func beJudge(rows []*beRow, base map[string]*beRow, baseState string) {
	for _, r := range rows {
		r.baseState = baseState
		if base != nil {
			r.base = base[beKey(r)]
		}
		if r.base == nil || r.base.uniqN < beMinSample {
			if baseState != "unavailable" {
				r.baseState = "insufficient_samples"
			}
		}
		if r.uniqN < beMinSample {
			continue // 표본 하한 미달 — 판정하지 않는다
		}
		changeOK := r.base != nil && r.base.uniqN >= beMinSample

		// L0 지연·배율 — SERVER·CONSUMER 한정(§15.4: 배경 소음이 전부
		// INTERNAL 스케줄러였다. 주기 배치는 폴마다 작업량이 달라
		// 중앙값이 배로 뛰는 것이 정상 동작이다).
		if changeOK && (r.kind == "SERVER" || r.kind == "CONSUMER") &&
			r.base.p50 > 0 && r.p50 >= beRatioP50*r.base.p50 {
			r.arms = append(r.arms, "latency_ratio")
		}
		// L1 지연·변화
		if changeOK && r.base.p95 > 0 && r.p95 >= beRatioP95*r.base.p95 && r.p95 >= beFloorP95Ms {
			r.arms = append(r.arms, "latency_shift")
		}
		// L2 지연·규모 — 기준선 불요(오염 보험)
		if r.p95 >= beMagnitudeMs {
			r.arms = append(r.arms, "latency_magnitude")
		}
		// E1 실패·규모 — framework는 제외(NoResultException 85.9%)
		if r.semantics != "framework" && r.failedN >= beFailMinN && r.failedRate >= beFailMinRate {
			r.arms = append(r.arms, "failure_magnitude")
		}
		// E2 실패·변화
		if changeOK && r.failedN >= beFailMinN &&
			r.failedRate >= beFailRatio*r.base.failedRate &&
			r.failedRate-r.base.failedRate >= beFailDelta {
			r.arms = append(r.arms, "failure_shift")
		}
		// R1 거절·변화 — 만성 4xx는 비율이 일정해 Δ 문턱을 통과하지 못한다.
		// 상승비 팔에 "잔여 반감" 팔을 OR로 더한다: 기준선 거절율이 50%를
		// 넘으면 "2배"가 원리적으로 불가능해(102%) 팔이 죽는다 —
		// gateway 만성 51% 404는 92%로 뛰어도 발화하지 못했다(구현 중
		// 단위 가드로 잡은 결함). 잔여 성공분이 절반으로 줄었는지를 같이
		// 본다. 라이브 3일 대조에서 이 팔을 더해도 배경 발화는 4건 그대로다.
		if changeOK && r.rejectedN >= beRejMinN &&
			r.rejRate-r.base.rejRate >= beRejDelta &&
			(r.rejRate >= beRejRatio*r.base.rejRate ||
				1-r.rejRate <= (1-r.base.rejRate)/beRejRatio) {
			r.arms = append(r.arms, "rejection_shift")
		}
	}
}

func beAttachSelf(ctx context.Context, ch *CH, svc string, from, to time.Time, rows []*beRow, acc *truncAcc) error {
	raw, tr, err := ch.Query(ctx, beSelfSQL, map[string]string{
		"svc": svc, "from": chTime(from), "to": chTime(to),
		"kidto": chTime(to.Add(beChildLookout))})
	if err != nil {
		return err
	}
	acc.note(tr)
	idx := map[string]*beRow{}
	for _, r := range rows {
		if r.section == "entry" {
			idx[r.kind+"\x00"+r.label] = r
		}
	}
	for _, r := range raw {
		row := idx[str(r["kind"])+"\x00"+str(r["label"])]
		if row == nil {
			continue
		}
		row.selfP50 = flt(r["self_p50_ms"])
		row.selfP95 = flt(r["self_p95_ms"])
		row.childShare = flt(r["child_share"])
		row.childSpanN = asInt(r["child_span_n"])
		row.selfClamped = asInt(r["self_clamped_n"])
		row.hasSelf = true
	}
	return nil
}

func beEnvelope(svc, resolvedBy string, rows []*beRow, want map[string]bool, topN int,
	from, to, baseFrom, baseTo time.Time, baseState string, acc *truncAcc) Envelope {

	sectionTotal := map[string]float64{}
	byKind := map[string]int{}
	spansTotal, spansUnique := 0, 0
	for _, r := range rows {
		sectionTotal[r.section] += r.totalMs
		byKind[r.kind] += r.n
		spansTotal += r.n
		spansUnique += r.uniqN
	}

	// 구획별 정렬·절단(§15.8). anomalous 행은 top_n 밖이어도 emit한다.
	bySection := map[string][]*beRow{}
	for _, r := range rows {
		if want[r.section] {
			bySection[r.section] = append(bySection[r.section], r)
		}
	}
	var findings []Finding
	var refs []string
	emitted, total := map[string]int{}, map[string]int{}
	truncated := map[string]int{}
	anomalous := false
	for _, sec := range []string{"entry", "step", "egress"} {
		list := bySection[sec]
		if len(list) == 0 {
			continue
		}
		sort.SliceStable(list, func(i, j int) bool {
			ri, rj := beRank(list[i]), beRank(list[j])
			if ri != rj {
				return ri < rj
			}
			return list[i].totalMs > list[j].totalMs
		})
		total[sec] = len(list)
		kept := 0
		for i, r := range list {
			if i >= topN && len(r.arms) == 0 {
				truncated[sec]++
				continue
			}
			if len(r.arms) > 0 {
				anomalous = true
			}
			f, rr := beFinding(svc, r, sectionTotal[r.section], from, to)
			findings = append(findings, f)
			refs = append(refs, rr...)
			kept++
		}
		emitted[sec] = kept
	}

	status := "normal"
	if anomalous {
		status = "anomalous"
	}
	env := Envelope{
		Status:          status,
		Summary:         beSummary(svc, findings, bySection, want, baseState),
		AssessmentBasis: beBasis(baseFrom, baseTo, baseState),
		Findings:        findings,
		Refs:            refs,
		ObservedRange:   &TimeRange{From: from, To: to},
		Truncated:       len(truncated) > 0,
		QueryTruncated:  bool(*acc),
	}
	// 구획별 절단(§5.1 계약 2) — 세 구획이 각자 top-N으로 잘린다.
	// 판정 팔이 붙은 행은 상한 밖이어도 남으므로 emitted가 topN을 넘을 수 있다.
	for _, sec := range []string{"entry", "step", "egress"} {
		env.Scopes = append(env.Scopes, qscope(sec, total[sec], emitted[sec]))
	}
	cov := Finding{
		"observation":   "coverage",
		"resolved_by":   resolvedBy,
		"service_name":  svc,
		"spans_total":   spansTotal,
		"spans_unique":  spansUnique,
		"spans_by_kind": byKind,
		"rows_emitted":  emitted,
		"rows_total":    total,
		"section_totals_ms": map[string]float64{
			"entry": round2(sectionTotal["entry"]), "step": round2(sectionTotal["step"]),
			"egress": round2(sectionTotal["egress"])},
		"baseline_window": beWinStr(baseFrom, baseTo, baseState),
		"limits":          beLimits(rows, want, baseState),
	}
	if len(truncated) > 0 {
		cov["truncated_sections"] = truncated
	}
	env.Findings = append(env.Findings, cov)
	return env
}

// beRank는 정렬 1차 키다 — 판정 행이 먼저, 표본 미달이 마지막.
// beArms는 nil 슬라이스를 빈 배열로 만든다 — JSON null은 "판정하지
// 않음"과 "팔이 붙지 않음"을 구분하지 못한다.
func beArms(a []string) []string {
	if a == nil {
		return []string{}
	}
	return a
}

func beRank(r *beRow) int {
	switch {
	case len(r.arms) > 0:
		return 0
	case r.uniqN < beMinSample:
		return 2
	default:
		return 1
	}
}

func beFinding(svc string, r *beRow, sectionTotal float64, from, to time.Time) (Finding, []string) {
	rowRef := fmt.Sprintf("ch:otel_traces_local:%s:%s:%s", svc, r.kind, r.label)
	rowRefs := []string{rowRef}
	if r.slowTrace != "" {
		rowRefs = append(rowRefs, fmt.Sprintf("ch:otel_traces_local:trace:%s#%s", r.slowTrace, r.slowSpan))
	}
	if r.failedN > 0 && r.failTrace != "" && r.failTrace != r.slowTrace {
		rowRefs = append(rowRefs, fmt.Sprintf("ch:otel_traces_local:trace:%s#%s", r.failTrace, r.failSpan))
	}
	f := Finding{
		"section": r.section, "kind": r.kind, "axis": r.axis, "label": r.label,
		"count":  r.uniqN,
		"p50_ms": round2(r.p50), "p95_ms": round2(r.p95), "max_ms": round2(r.maxMs),
		"total_ms": round2(r.totalMs),
		"errors": map[string]int{
			"failed": r.failedN, "status_error": r.statusErr,
			"http_5xx": r.http5xx, "http_4xx": r.http4xx},
		"error_semantics": r.semantics,
		"verdict_arms":    beArms(r.arms),
		"refs":            rowRefs,
	}
	if sectionTotal > 0 {
		f["time_share_in_section"] = round3(r.totalMs / sectionTotal)
	}
	if r.uniqN > 0 {
		f["failed_rate"] = round3(r.failedRate)
	}
	if r.rejectedN >= 0 {
		f["rejected_n"] = r.rejectedN
		f["rejected_rate"] = round3(r.rejRate)
	}
	if len(r.errLabels) > 0 {
		f["error_labels"] = r.errLabels
	}
	if r.section == "entry" {
		f["trace_root_n"] = r.rootN
		if r.hasSelf {
			f["self_ms_p50"] = round2(r.selfP50)
			f["self_ms_p95"] = round2(r.selfP95)
			f["child_share"] = round3(r.childShare)
			f["child_span_n"] = r.childSpanN
			if r.selfClamped > 0 {
				f["self_clamped_n"] = r.selfClamped
			}
		}
	}
	if r.targetID != "" {
		f["target_id"] = r.targetID
	}
	if r.uniqN < beMinSample {
		f["verdict"] = "insufficient_sample"
	} else if len(r.arms) > 0 {
		f["verdict"] = "anomalous"
	} else {
		f["verdict"] = "normal"
	}
	if r.base != nil {
		f["baseline"] = map[string]any{
			"count": r.base.uniqN, "p50_ms": round2(r.base.p50), "p95_ms": round2(r.base.p95),
			"failed_rate": round3(r.base.failedRate), "rejected_rate": round3(r.base.rejRate),
			"state": r.baseState}
	} else {
		f["baseline"] = map[string]any{"state": r.baseState}
	}
	// step 행과 db egress 행이 함께 있을 때의 층 겹침 경고는 봉투
	// limits에도 있으나, 조사자가 행만 인용하는 경우를 위해 행에도 단다.
	if r.section == "step" {
		f["nesting_note"] = "이 구간은 egress의 db 행과 같은 DB 접근을 다른 계측 층에서 본 것이다. 두 시간을 더하지 말 것."
	}
	return f, rowRefs
}

func beSummary(svc string, findings []Finding, bySection map[string][]*beRow,
	want map[string]bool, baseState string) string {
	var anom []string
	for _, f := range findings {
		if arms, ok := f["verdict_arms"].([]string); ok && len(arms) > 0 {
			anom = append(anom, fmt.Sprintf("%v(%s)", f["label"], strings.Join(arms, "+")))
		}
	}
	var parts []string
	for _, sec := range []string{"entry", "step", "egress"} {
		if want[sec] && len(bySection[sec]) > 0 {
			parts = append(parts, fmt.Sprintf("%s %d", sec, len(bySection[sec])))
		}
	}
	head := fmt.Sprintf("%s 처리 구간 분해(%s).", svc, strings.Join(parts, " · "))
	if len(anom) == 0 {
		if baseState == "unavailable" {
			return head + " 조회한 기준선 창에 관측이 없어 변화 판정을 하지 못했다 — 이는 정상의 증거가 아니다. 절대값은 findings 참조."
		}
		return head + " 지연·실패 모두 문턱을 넘은 구간이 없다(이 도구의 보수적 변화식으로 이상 상승을 확인하지 못했다는 뜻이지 건강하다는 뜻이 아니다)."
	}
	if len(anom) > 4 {
		anom = append(anom[:4], fmt.Sprintf("외 %d개", len(anom)-4))
	}
	return head + " 문턱을 넘은 구간: " + strings.Join(anom, ", ") + "."
}

func beBasis(baseFrom, baseTo time.Time, baseState string) string {
	if baseState == "unavailable" {
		return "조회한 기준선 창에 관측이 없어 변화 팔을 끄고 규모 팔만 적용했다: 지연 p95 3000ms · 실패 5건 이상이며 5% 이상. 보존 만료·미수집·무트래픽 중 원인은 미확정이다."
	}
	return fmt.Sprintf(
		"기준선은 %s~%s(%s, 창 집계와 동일 SQL). 여섯 팔 OR — 지연: p50 3배(SERVER·CONSUMER 한정) · p95 3배이며 300ms 이상 · p95 3000ms(기준선 불요). "+
			"실패: 5건 이상이며 5%% 이상(framework 의미 제외) · 기준선 2배이며 +10%%p. 거절(4xx): 20건 이상이며 기준선 2배이며 +10%%p. "+
			"문턱은 라이브 3일 배경 8,870버킷·사건 3구간 동시 대입으로 정했다(실 오탐 2건) — 다른 환경에 재측정 없이 옮길 근거는 없다.",
		baseFrom.Format(time.RFC3339), baseTo.Format(time.RFC3339), baseState)
}

func beWinStr(f, t time.Time, state string) map[string]any {
	m := map[string]any{"state": state}
	if !f.IsZero() && !t.IsZero() {
		m["from"] = f.Format(time.RFC3339)
		m["to"] = t.Format(time.RFC3339)
	}
	return m
}

// beLimits는 §15.9의 조건부 한계다.
func beLimits(rows []*beRow, want map[string]bool, baseState string) []string {
	lim := []string{
		"기준선 가용성은 요청 시간창의 실제 조회 결과로 판단한다. 빈 결과만으로 보존 만료·미수집·무트래픽을 구분할 수 없다.",
		"k8s_node_name·k8s_pod_name은 전 span 빈 값이다 — 인스턴스 구분은 host_name뿐이고, 레플리카 비교는 compare_peers 몫이다.",
		"section_totals_ms의 세 값을 더하지 말 것 — 서로 부모-자식 관계이거나 별개 트레이스다.",
	}
	var hasClient, hasStep, hasDB, hasFramework, hasReject, hasWild, hasProducer, hasClamp bool
	serverOnlyHealth := true
	sawServer := false
	for _, r := range rows {
		switch {
		case r.kind == "CLIENT":
			hasClient = true
			if strings.HasPrefix(r.label, "db ") {
				hasDB = true
			}
		case r.kind == "PRODUCER":
			hasProducer = true
		}
		if r.section == "step" {
			hasStep = true
		}
		if r.semantics == "framework" && r.statusErr > 0 {
			hasFramework = true
		}
		if r.rejectedN > 0 {
			hasReject = true
		}
		if strings.Contains(r.label, "**") {
			hasWild = true
		}
		if r.selfClamped > 0 {
			hasClamp = true
		}
		if r.kind == "SERVER" {
			sawServer = true
			if !strings.Contains(r.label, "/actuator/") {
				serverOnlyHealth = false
			}
		}
	}
	if sawServer && serverOnlyHealth {
		lim = append(lim, "이 서비스의 HTTP 진입점은 헬스체크뿐이다 — 실제 처리는 entry 구획의 CONSUMER·스케줄러 행에 있다.")
	}
	if hasClient && want["egress"] {
		lim = append(lim, "CLIENT span_name은 HTTP 메서드뿐이라 목적지를 server.address·db.* 속성으로 복원했다. 속성이 없으면 peer <address>로 뭉친다.")
	}
	if hasStep && hasDB && want["step"] && want["egress"] {
		lim = append(lim, "ORM 구간(hibernate/spring-data)과 JDBC 호출은 같은 DB 접근을 다른 계측 층에서 본 것이다. 두 시간을 더하지 말 것.")
	}
	if hasFramework {
		lim = append(lim, "INTERNAL·CONSUMER의 ERROR 표시는 대부분 정상 업무 흐름이다(예: NoResultException이 한 행의 85.9%). 규모 팔로 판정하지 않았다.")
	}
	if hasReject {
		lim = append(lim, "4xx는 이 환경에서 만성이다(게이트웨이 40~44% 상시). 존재가 아니라 기준선 대비 변화로만 판정했다.")
	}
	if hasWild {
		lim = append(lim, "게이트웨이 라우트는 와일드카드 템플릿(**)이라 개별 경로가 접혀 있다 — 생산자 계약이지 도구가 고칠 수 있는 것이 아니다.")
	}
	if hasProducer && want["egress"] {
		lim = append(lim, "PRODUCER duration은 로컬 publish 소요다. 브로커·컨슈머 지연은 이 원천에 없다.")
	}
	if want["entry"] {
		lim = append(lim, "self-time 계산 시 자식 스캔 창을 창 끝에서 60초 넓혔다 — 관측된 최대 진입점 duration 27.7초에 근거한 가정이며 상한이 증명된 값은 아니다.")
	}
	if hasClamp {
		lim = append(lim, "자식 시간 합이 부모를 넘은 span이 있어 self-time을 0으로 깎았다(자식 겹침 실측 0건이었으므로 계측 변화 신호일 수 있다).")
	}
	if baseState == "insufficient_samples" || baseState == "widened" {
		lim = append(lim, "기준선 표본이 모자라 창을 넓혔거나 일부 행의 변화 팔을 끄고 규모 팔만 적용했다.")
	}
	return lim
}

// beServiceList는 창 내 유효 서비스 목록(복구 정보)이다. 교체된
// apm.go에서 이 함수만 살아남았다 — 이름 오타·별칭 문제를 한 번에
// 끝내는 값이고, 창 내 서비스가 21개뿐이라 전수를 실어도 작다.
func beServiceList(ctx context.Context, ch *CH, from, to time.Time) []string {
	// LIMIT 40 — no_data 복구 정보용 소량 조회, 절단 무관 표면이라 표식은 버린다.
	rows, _, err := ch.Query(ctx, `
		SELECT DISTINCT service_name FROM otel_traces_local
		WHERE timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		ORDER BY service_name LIMIT 40`,
		map[string]string{"from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil
	}
	var names []string
	for _, r := range rows {
		if s, ok := r["service_name"].(string); ok {
			names = append(names, s)
		}
	}
	return names
}

// beNoTarget — 대상 미해소(§15.9 no_data 분기 1).
func beNoTarget(ctx context.Context, ch *CH, target string, from, to time.Time) Envelope {
	names := beServiceList(ctx, ch, from, to)
	return Envelope{
		Status:       "no_data",
		NoDataReason: NoDataUnknown,
		Summary: fmt.Sprintf("%q를 창 안에서 해소하지 못했다 — service.name도 lucida.target_id도 일치하지 않는다. 창 내 유효 서비스: %v",
			target, names),
		AssessmentBasis: "대상 해소 실패 — 무트래픽인지 이름 오류인지는 위 목록으로 가른다.",
		ObservedRange:   &TimeRange{From: from, To: to},
	}
}

// beNoSpans — 해소됐으나 창에 span 0건(§15.9 no_data 분기 2). 창 밖
// 가장 가까운 관측을 준다: 야간 무트래픽이 이 분기의 주 원인이다.
func beNoSpans(ctx context.Context, ch *CH, svc, resolvedBy string, from, to time.Time) Envelope {
	reason, nearest := NoDataZeroObservations, ""
	// max() 단일 행 집계 — 절단 불가 표면이라 표식은 버린다.
	rows, _, err := ch.Query(ctx, `
		SELECT max(timestamp) AS before_ts FROM otel_traces_local
		WHERE service_name = {svc:String} AND timestamp < parseDateTime64BestEffort({from:String}, 9)`,
		map[string]string{"svc": svc, "from": chTime(from)})
	if err == nil && len(rows) > 0 {
		nearest = str(rows[0]["before_ts"])
	}
	sum := fmt.Sprintf("%s: 창 안 span 0건.", svc)
	if nearest != "" {
		sum += fmt.Sprintf(" 창 이전 가장 가까운 관측은 %s다 — 창을 옮기면 볼 수 있다.", nearest)
	}
	sum += " 보존 만료·미수집·무트래픽 중 원인은 이 조회만으로 확정하지 않는다."
	return Envelope{
		Status:          reason2status(reason),
		NoDataReason:    reason,
		Summary:         sum,
		AssessmentBasis: fmt.Sprintf("대상은 %s로 해소됐고 창 안 관측이 0건이다.", resolvedBy),
		ObservedRange:   &TimeRange{From: from, To: to},
	}
}

func reason2status(string) string { return "no_data" }

func str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}

func flt(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case json.Number:
		f, _ := n.Float64()
		return f
	}
	return 0
}

// round3은 dbslowqueries.go의 것을 공유한다(부호 인지 반올림).
func round2(f float64) float64 { return float64(int64(f*100+sign(f)*0.5)) / 100 }
