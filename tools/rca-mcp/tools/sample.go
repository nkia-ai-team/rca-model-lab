// sample_logs — "로그에 뭐가 나타났나"의 재설계 표면 (스펙 §3.3 ③,
// 2026-07-28 확정). 표본 정책을 두지 않는다 — 도구가 "중요한 줄"을
// 추측하는 대신 scan→read와 동형인 2단계 깔때기:
//
//   - map 모드: 템플릿 그룹핑을 존재 4구획(기준선 미관측/급증/소멸/최빈)
//     으로 준다. 신규성은 판정이 아니라 사실(not_seen_in_baseline 3값 +
//     양 창 계수)만. 레벨 분포는 판정 없는 요약 메타 — 구 get_logs의
//     "ERROR 존재=anomalous" 판정은 폐기(상시 ERROR와 사건성 구분 불가).
//   - grep 모드: template_id 또는 검색어로 raw 줄(레벨 불문 — F04 유일
//     미확정 원인의 해소)을 시간순으로. 매치 전후 맥락 줄은 같은 귀속
//     스트림 안에서만(남의 로그가 문맥으로 섞임 방지).
//
// 정본 테이블 lucida_logs_local, 귀속은 target_id OR host_target_id
// 이중(§5). 판정식 수치(급증 배율 등)는 전부 임시 — 계약 §7 열린 결정.
package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

const (
	sampleQuotaNew      = 8   // 기준선 미관측 구획 표시 상한
	sampleQuotaSurge    = 5   // 급증 구획
	sampleQuotaGone     = 5   // 소멸 구획
	sampleQuotaFrequent = 5   // 최빈 구획
	sampleSurgeRatio    = 5.0 // 분당 비율 급증 문턱(임시)
	sampleSurgeMinCount = 10  // 급증 판정 최소 현재 계수(임시)
	sampleGoneMinExpect = 5.0 // 소멸 판정 최소 기대 출현 수(임시) — 기준선
	// 비율×현재 창 길이가 이만큼인데 0회일 때만 소멸. 없으면 드문 로그가
	// 짧은 현재 창에 우연히 없다는 이유로 소멸 홍수가 난다(라이브 실측:
	// 60m 기준선 vs 11m 창에서 3,943종 오탐).
	sampleGrepLimit    = 30 // grep 기본 반환 줄 수
	sampleGrepMaxLimit = 100
	sampleContextCap   = 5   // 맥락 줄을 붙이는 매치 수 상한(비용)
	sampleBodyCap      = 400 // 줄당 body 표시 상한(문자)
)

// 템플릿 정규화 — 16진 토큰(대소문자)·숫자를 뭉갠다. 과병합·과분할
// 양방향 오류가 알려져 있음(Codex 검토) — 임시 판정식.
const sampleTmplExpr = `replaceRegexpAll(replaceRegexpAll(body, '[0-9a-fA-F]{8,}', '#'), '[0-9]+', 'N')`

// severity_text 하한 → OTel severity_number 경계.
var severityFloor = map[string]int{
	"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21,
}

// NewSampleLogsTool은 sample_logs 도구를 만든다. firstEvent는 map 모드
// 기준선 기본 창 [firstEvent-60m, firstEvent)의 기준점(scan_metrics와
// 동일 규약).
func NewSampleLogsTool(ch *CH, firstEvent time.Time) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"mode":          map[string]any{"type": "string", "enum": []string{"map", "grep"}, "description": "map=로그 종류 지도(뭐가 있는지 모를 때 시작점), grep=원문 드릴다운(지도의 template_id나 검색어로)"},
			"target":        map[string]string{"type": "string", "description": "target_id(UUID)"},
			"from":          map[string]string{"type": "string", "description": "UTC RFC3339, 반개구간 시작(포함)"},
			"to":            map[string]string{"type": "string", "description": "UTC RFC3339, 반개구간 끝(제외)"},
			"baseline_from": map[string]string{"type": "string", "description": "map 전용: 기준선 창 override 시작(선택 — 기본은 인시던트 첫 증상 직전 60분)"},
			"baseline_to":   map[string]string{"type": "string", "description": "map 전용: 기준선 창 override 끝(선택, baseline_from과 함께)"},
			"template_id":   map[string]string{"type": "string", "description": "grep 전용: map 응답의 template_id를 그대로 — 그 종류의 원문을 본다"},
			"query":         map[string]string{"type": "string", "description": "grep 전용: 본문 부분 일치 검색어(template_id 없을 때)"},
			"severity_min":  map[string]string{"type": "string", "description": "grep 전용(선택): 이 레벨 이상만 (TRACE|DEBUG|INFO|WARN|ERROR|FATAL)"},
			"context_lines": map[string]string{"type": "integer", "description": "grep 전용(선택): 매치 전후 맥락 줄 수(0~5, 같은 귀속 스트림 한정)"},
			"limit":         map[string]string{"type": "integer", "description": "grep 전용(선택): 최대 반환 매치 수(기본 30, 최대 100)"},
		},
		"required": []string{"mode", "target", "from", "to"},
	})
	return llm.Tool{
		Name: "sample_logs",
		Description: "대상의 로그 조사 — mode=map은 종류 지도(존재 4구획: 기준선 미관측/급증/소멸/최빈 + 레벨 분포), mode=grep은 raw 원문(레벨 불문, template_id·검색어·맥락 줄). " +
			"모르는 대상은 map부터, 지도의 template_id로 grep 드릴다운.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			in, err := parseTargetWindow(args)
			if err != nil {
				return nil, err
			}
			var f struct {
				Mode         string `json:"mode"`
				BaselineFrom string `json:"baseline_from"`
				BaselineTo   string `json:"baseline_to"`
				TemplateID   string `json:"template_id"`
				Query        string `json:"query"`
				SeverityMin  string `json:"severity_min"`
				ContextLines int    `json:"context_lines"`
				Limit        int    `json:"limit"`
			}
			if err := json.Unmarshal(args, &f); err != nil {
				return nil, fmt.Errorf("인자 파싱: %w", err)
			}
			switch f.Mode {
			case "map":
				baseFrom, baseTo := firstEvent.Add(-scanLookback), firstEvent
				if f.BaselineFrom != "" || f.BaselineTo != "" {
					bf, err1 := time.Parse(time.RFC3339, f.BaselineFrom)
					bt, err2 := time.Parse(time.RFC3339, f.BaselineTo)
					if err1 != nil || err2 != nil || !bf.Before(bt) {
						return nil, fmt.Errorf("baseline override 오류: baseline_from·baseline_to 둘 다 UTC RFC3339, from < to 필요")
					}
					baseFrom, baseTo = bf, bt
				}
				return sampleMap(ctx, ch, in.Target, in.FromT, in.ToT, baseFrom, baseTo)
			case "grep":
				if f.TemplateID == "" && f.Query == "" {
					return nil, fmt.Errorf("grep 모드는 template_id(map 응답의 것) 또는 query(검색어) 중 하나 필요")
				}
				if f.SeverityMin != "" {
					if _, ok := severityFloor[f.SeverityMin]; !ok {
						return nil, fmt.Errorf("severity_min %q 무효 — 유효값: TRACE DEBUG INFO WARN ERROR FATAL", f.SeverityMin)
					}
				}
				return sampleGrep(ctx, ch, in.Target, in.FromT, in.ToT, f.TemplateID, f.Query, f.SeverityMin, f.ContextLines, f.Limit)
			default:
				return nil, fmt.Errorf("mode %q 무효 — map(종류 지도) 또는 grep(원문 드릴다운)", f.Mode)
			}
		},
	}
}

const sampleAttribution = `(target_id = {target:String} OR host_target_id = {target:String})`

// tmplGroup은 한 창의 템플릿 그룹 관측이다.
type tmplGroup struct {
	Template   string
	TID        string
	Count      int
	FirstAt    string
	LastAt     string
	Sample     string // 최초 줄(대표성 규칙: 신규·전조 독해가 목적이므로 첫 등장)
	Severities []string
}

func sampleWindowGroups(ctx context.Context, ch *CH, target string, from, to time.Time, acc *truncAcc) (map[string]tmplGroup, int, map[string]int, error) {
	rows, tr, err := ch.Query(ctx, `
		SELECT `+sampleTmplExpr+` AS template,
		       toString(cityHash64(template)) AS tid,
		       count() AS n,
		       min(timestamp) AS first_at, max(timestamp) AS last_at,
		       argMin(body, timestamp) AS sample,
		       groupUniqArray(severity_text) AS sevs
		FROM lucida_logs_local
		WHERE `+sampleAttribution+`
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		GROUP BY template`,
		map[string]string{"target": target, "from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, 0, nil, err
	}
	acc.note(tr)
	groups := map[string]tmplGroup{}
	total := 0
	levelDist := map[string]int{}
	for _, r := range rows {
		g := tmplGroup{
			Template: fmt.Sprint(r["template"]), TID: fmt.Sprint(r["tid"]),
			Count: asInt(r["n"]), FirstAt: fmt.Sprint(r["first_at"]), LastAt: fmt.Sprint(r["last_at"]),
			Sample: truncBody(fmt.Sprint(r["sample"])),
		}
		if sevs, ok := r["sevs"].([]any); ok {
			for _, s := range sevs {
				g.Severities = append(g.Severities, fmt.Sprint(s))
			}
		}
		groups[g.Template] = g
		total += g.Count
	}
	// 레벨 분포(판정 없는 요약 메타)는 별도 집계 — 템플릿 그룹과 축이 다르다.
	lv, trLv, err := ch.Query(ctx, `
		SELECT severity_text, count() AS n FROM lucida_logs_local
		WHERE `+sampleAttribution+`
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
		GROUP BY severity_text`,
		map[string]string{"target": target, "from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, 0, nil, err
	}
	acc.note(trLv)
	for _, r := range lv {
		levelDist[fmt.Sprint(r["severity_text"])] = asInt(r["n"])
	}
	return groups, total, levelDist, nil
}

// sampleHorizon은 이 대상의 로그 **도착 수평선**이다 — 창과 무관하게 "몇 시
// 것까지 도착해 있는가"(§14-5 5c 결정 ①). 창 조건을 빼는 것이 요점: 창 안
// max(timestamp)는 "창 후반 로그 없음"과 "수집 미도달"을 구분하지 못해 관측
// 범위 주장으로 쓸 수 없다. 실패·빈 스트림이면 ok=false — 봉투는
// observed_range 없이 나가고 CollectLag는 unknown으로 남는다(fail-closed).
func sampleHorizon(ctx context.Context, ch *CH, target string) (time.Time, bool) {
	// max() 단일 행 집계 — 절단 불가 표면이라 표식은 버린다(캡 미만 자명).
	rows, _, err := ch.Query(ctx, `SELECT toString(max(timestamp)) AS h FROM lucida_logs_local WHERE `+sampleAttribution,
		map[string]string{"target": target})
	if err != nil || len(rows) == 0 {
		return time.Time{}, false
	}
	return chParseTS(rows[0]["h"])
}

func sampleMap(ctx context.Context, ch *CH, target string, from, to, baseFrom, baseTo time.Time) (any, error) {
	var acc truncAcc
	horizon, _ := sampleHorizon(ctx, ch, target)
	cur, curTotal, levelDist, err := sampleWindowGroups(ctx, ch, target, from, to, &acc)
	if err != nil {
		return nil, fmt.Errorf("sample_logs map 현재 창 조회: %w", err)
	}
	if curTotal == 0 {
		return Envelope{
			Status:       "no_data",
			NoDataReason: NoDataUnknown,
			// 도착 수평선은 no_data에도 싣는다 — CollectLag 판정(no_data→
			// unknown)은 불변이지만, "어디까지 도착했는가"는 관측 사실이다.
			ObservedRange: horizonObservedRange(from, to, horizon),
			Summary: "시간창 내 로그 0행 — 로그 미발생인지 수집 결손인지 이 도구로는 구분 불가. " +
				"배제 근거로 쓰려면 get_data_coverage로 수집 상태를 확인하라.",
			QueryTruncated: bool(acc),
		}, nil
	}
	base, baseTotal, _, err := sampleWindowGroups(ctx, ch, target, baseFrom, baseTo, &acc)
	if err != nil {
		return nil, fmt.Errorf("sample_logs map 기준선 창 조회: %w", err)
	}
	baselineKnown := baseTotal > 0

	curMin := to.Sub(from).Minutes()
	baseMin := baseTo.Sub(baseFrom).Minutes()

	// 구획 분류 — 존재 사실만, 이상 단정은 조사자 몫.
	var newG, surgeG, frequentG []tmplGroup
	var goneG []tmplGroup
	for _, g := range cur {
		b, inBase := base[g.Template]
		switch {
		case !inBase:
			newG = append(newG, g)
		case baseMin > 0 && curMin > 0 && g.Count >= sampleSurgeMinCount &&
			(float64(g.Count)/curMin) >= sampleSurgeRatio*(float64(b.Count)/baseMin):
			surgeG = append(surgeG, g)
		default:
			frequentG = append(frequentG, g)
		}
	}
	skippedRareGone := 0
	for _, b := range base {
		if _, inCur := cur[b.Template]; inCur {
			continue
		}
		// 기대 출현 수 = 기준선 분당 비율 × 현재 창 길이. 문턱 미만이면
		// "드물어서 안 보임"과 "끊김"을 구분할 수 없다 — 소멸로 세지 않는다.
		if baseMin <= 0 || float64(b.Count)/baseMin*curMin < sampleGoneMinExpect {
			skippedRareGone++
			continue
		}
		goneG = append(goneG, b)
	}
	// 정렬: 신규=첫 등장 시각순(onset 근접 우선 — 저빈도 전조가 계수순에
	// 밀리지 않게), 급증·소멸·최빈=계수순.
	sort.Slice(newG, func(i, j int) bool { return newG[i].FirstAt < newG[j].FirstAt })
	sort.Slice(surgeG, func(i, j int) bool { return surgeG[i].Count > surgeG[j].Count })
	sort.Slice(goneG, func(i, j int) bool { return goneG[i].Count > goneG[j].Count })
	sort.Slice(frequentG, func(i, j int) bool { return frequentG[i].Count > frequentG[j].Count })

	novelty := func(inBase bool) string {
		if !baselineKnown {
			return "baseline_unknown"
		}
		if inBase {
			return "seen_in_baseline"
		}
		return "not_seen_in_baseline"
	}
	var findings []Finding
	var refs []string
	emit := func(section string, gs []tmplGroup, quota int, inBase bool) int {
		shown := 0
		for _, g := range gs {
			if shown >= quota {
				break
			}
			ref := fmt.Sprintf("ch:lucida_logs_local:%s:tmpl:%s", target, g.TID)
			refs = append(refs, ref)
			f := Finding{
				"section": section, "template_id": g.TID, "template": g.Template,
				"severities": g.Severities, "first_at": g.FirstAt, "last_at": g.LastAt,
				"sample_first_line": g.Sample, "baseline_seen": novelty(inBase),
				"refs": []string{ref},
			}
			if section == "disappeared" {
				f["baseline_count"] = g.Count
				f["current_count"] = 0
				f["note"] = "기준선 창에는 있었으나 현재 창에서 미관측 — 로그 끊김 자체가 단서일 수 있다"
			} else {
				f["current_count"] = g.Count
				if b, ok := base[g.Template]; ok {
					f["baseline_count"] = b.Count
				} else if baselineKnown {
					f["baseline_count"] = 0
				}
			}
			findings = append(findings, f)
			shown++
		}
		return shown
	}
	nNew := emit("new_in_window", newG, sampleQuotaNew, false)
	nSurge := emit("surged", surgeG, sampleQuotaSurge, true)
	nGone := emit("disappeared", goneG, sampleQuotaGone, true)
	nFreq := emit("frequent", frequentG, sampleQuotaFrequent, true)

	// 요약 메타 — 판정 없이 숫자만.
	meta := Finding{
		"section": "summary_meta",
		"volume": map[string]any{
			"current_total": curTotal, "baseline_total": baseTotal,
			"current_rate_per_min":  round1(float64(curTotal) / curMin),
			"baseline_rate_per_min": round1(safeDiv(float64(baseTotal), baseMin)),
		},
		"level_distribution": levelDist,
		"baseline_window":    fmt.Sprintf("%s/%s", baseFrom.UTC().Format(time.RFC3339), baseTo.UTC().Format(time.RFC3339)),
	}
	if !baselineKnown {
		meta["baseline_note"] = "기준선 창에 로그 0행 — 미관측/신규 구분 불가(baseline_unknown). 소멸 구획도 판단 불가."
	}
	if skippedRareGone > 0 {
		meta["gone_skipped_rare"] = skippedRareGone
		meta["gone_skipped_note"] = fmt.Sprintf(
			"기준선에만 있던 %d종은 드물어서(기대 출현 %.0f회 미만) 소멸로 세지 않음 — 부재가 판단 재료면 grep으로 직접 확인", skippedRareGone, sampleGoneMinExpect)
	}
	findings = append(findings, meta)

	status := "normal"
	if nNew > 0 || nSurge > 0 || nGone > 0 {
		status = "anomalous"
	}
	truncated := len(newG) > sampleQuotaNew || len(surgeG) > sampleQuotaSurge ||
		len(goneG) > sampleQuotaGone || len(frequentG) > sampleQuotaFrequent
	summary := fmt.Sprintf(
		"로그 종류 4구획: 기준선 미관측 %d/%d종, 급증 %d/%d종, 소멸 %d/%d종, 최빈 표시 %d/%d종. 원문은 mode=grep에 template_id로.",
		nNew, len(newG), nSurge, len(surgeG), nGone, len(goneG), nFreq, len(frequentG))
	if !baselineKnown {
		summary += " 기준선 로그 없음 — 신규성 판단 불가."
	}
	return Envelope{
		Status: status,
		AssessmentBasis: fmt.Sprintf(
			"존재 4구획(구획 존재 사실 기준 — 이상 단정 아님, 레벨 기반 판정 폐지). 급증=분당 비율 %.0f배·현재 %d건 이상(임시), 소멸=기대 출현 %.0f회 이상인데 0회(임시), 기준선=첫 증상 직전 %.0f분(override 가능), 템플릿=숫자·16진 정규화(양방향 오류 알려짐)",
			sampleSurgeRatio, sampleSurgeMinCount, sampleGoneMinExpect, scanLookback.Minutes()),
		Summary: summary,
		// 도착 수평선 기반(§14-5 5c 결정 ①) — 창 끝을 수평선이 못 넘으면
		// 그만큼이 수집 지연으로 계산된다(CollectLagOf).
		ObservedRange: horizonObservedRange(from, to, horizon),
		Findings:      findings,
		Refs:          refs,
		Truncated:      truncated,
		QueryTruncated: bool(acc),
		// 구획별 절단(§5.1 계약 2). 구획이 0종이어도 싣는다 — "이 구획을
		// 조회했고 0건이었다"가 observed_zero의 원천이다.
		Scopes: []QueryScope{
			qscope("new_in_window", len(newG), nNew),
			qscope("surged", len(surgeG), nSurge),
			qscope("disappeared", len(goneG), nGone),
			qscope("frequent", len(frequentG), nFreq),
		},
	}, nil
}

func sampleGrep(ctx context.Context, ch *CH, target string, from, to time.Time, templateID, query, sevMin string, contextLines, limit int) (any, error) {
	if limit <= 0 {
		limit = sampleGrepLimit
	}
	if limit > sampleGrepMaxLimit {
		limit = sampleGrepMaxLimit
	}
	if contextLines < 0 {
		contextLines = 0
	}
	if contextLines > 5 {
		contextLines = 5
	}
	horizon, _ := sampleHorizon(ctx, ch, target)

	p := map[string]string{"target": target, "from": chTime(from), "to": chTime(to)}
	where := sampleAttribution + `
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)`
	matchDesc := ""
	if templateID != "" {
		where += ` AND toString(cityHash64(` + sampleTmplExpr + `)) = {tid:String}`
		p["tid"] = templateID
		matchDesc = "template_id=" + templateID
	} else {
		where += ` AND body ILIKE {q:String}`
		p["q"] = "%" + query + "%"
		matchDesc = fmt.Sprintf("query=%q", query)
	}
	if sevMin != "" {
		where += fmt.Sprintf(` AND severity_number >= %d`, severityFloor[sevMin])
		matchDesc += " severity>=" + sevMin
	}

	var acc truncAcc
	cnt, trCnt, err := ch.Query(ctx, `SELECT count() AS n FROM lucida_logs_local WHERE `+where, p)
	if err != nil {
		return nil, fmt.Errorf("sample_logs grep 계수: %w", err)
	}
	acc.note(trCnt)
	total := 0
	if len(cnt) > 0 {
		total = asInt(cnt[0]["n"])
	}
	if total == 0 {
		return Envelope{
			Status:        "no_data",
			NoDataReason:  NoDataZeroObservations,
			ObservedRange: horizonObservedRange(from, to, horizon),
			Summary:       fmt.Sprintf("매치 0줄(%s) — 이 창의 이 대상 로그에서 해당 종류/검색어는 관측되지 않았다.", matchDesc),
			// 완전 조회 + 0건 = observed_zero의 원천(§5.1 계약 1).
			Scopes: []QueryScope{qscope("grep", 0, 0)},
		}, nil
	}

	rows, trRows, err := ch.Query(ctx, `
		SELECT timestamp, severity_text, body, target_id, host_target_id,
		       toString(cityHash64(body)) AS bh
		FROM lucida_logs_local WHERE `+where+`
		ORDER BY timestamp ASC LIMIT `+fmt.Sprint(limit), p)
	if err != nil {
		return nil, fmt.Errorf("sample_logs grep 조회: %w", err)
	}
	acc.note(trRows)

	var findings []Finding
	var refs []string
	for i, r := range rows {
		ts := fmt.Sprint(r["timestamp"])
		ref := fmt.Sprintf("ch:lucida_logs_local:%s:%s:%s", target, ts, fmt.Sprint(r["bh"]))
		refs = append(refs, ref)
		matchedBy := "target_id"
		if fmt.Sprint(r["target_id"]) != target {
			matchedBy = "host_target_id"
		}
		f := Finding{
			"relation": "match", "at": ts, "severity": r["severity_text"],
			"body": truncBody(fmt.Sprint(r["body"])), "matched_by": matchedBy,
			"refs": []string{ref},
		}
		findings = append(findings, f)
		// 맥락 줄 — 같은 귀속 스트림(매치 행과 같은 target_id 값) 안에서만.
		// 비용 상한: 앞쪽 매치 sampleContextCap개까지만.
		if contextLines > 0 && i < sampleContextCap {
			ctx, err := grepContext(ctx, ch, fmt.Sprint(r["target_id"]), fmt.Sprint(r["host_target_id"]), ts, contextLines, &acc)
			if err != nil {
				return nil, err
			}
			findings = append(findings, ctx...)
		}
	}

	summary := fmt.Sprintf("매치 %d줄 중 %d줄 반환(%s, 시간순).", total, len(rows), matchDesc)
	if contextLines > 0 {
		summary += fmt.Sprintf(" 맥락 줄은 앞쪽 매치 %d개까지(같은 귀속 스트림 한정).", sampleContextCap)
	}
	return Envelope{
		Status:          "normal",
		AssessmentBasis: "raw 원문 반환 — 판정 없음, 독해는 조사자 몫",
		Summary:         summary,
		ObservedRange:   horizonObservedRange(from, to, horizon),
		Findings:        findings,
		Refs:            refs,
		Truncated:       total > len(rows),
		QueryTruncated:  bool(acc),
		// grep은 매치 줄이 단일 조회 단위다(§5.1 계약 2) — 0건 grep이
		// no_data인데 Findings가 없어 absent 판정 원천이 없던 자리.
		Scopes: []QueryScope{qscope("grep", total, len(rows))},
	}, nil
}

// grepContext는 매치 행 전후의 맥락 줄을 같은 귀속 스트림에서 가져온다.
func grepContext(ctx context.Context, ch *CH, rowTarget, rowHost, ts string, n int, acc *truncAcc) ([]Finding, error) {
	stream := `target_id = {rt:String} AND host_target_id = {rh:String}`
	p := map[string]string{"rt": rowTarget, "rh": rowHost, "ts": ts}
	var out []Finding
	for _, dir := range []struct {
		rel, cmp, ord string
	}{
		{"before", "<", "DESC"},
		{"after", ">", "ASC"},
	} {
		rows, tr, err := ch.Query(ctx, `
			SELECT timestamp, severity_text, body FROM lucida_logs_local
			WHERE `+stream+` AND timestamp `+dir.cmp+` parseDateTime64BestEffort({ts:String}, 9)
			ORDER BY timestamp `+dir.ord+` LIMIT `+fmt.Sprint(n), p)
		if err != nil {
			return nil, fmt.Errorf("sample_logs 맥락 줄(%s): %w", dir.rel, err)
		}
		acc.note(tr)
		if dir.rel == "before" { // DESC로 가져왔으니 시간순으로 뒤집는다.
			for i, j := 0, len(rows)-1; i < j; i, j = i+1, j-1 {
				rows[i], rows[j] = rows[j], rows[i]
			}
		}
		for _, r := range rows {
			out = append(out, Finding{
				"relation": dir.rel, "at": fmt.Sprint(r["timestamp"]),
				"severity": r["severity_text"], "body": truncBody(fmt.Sprint(r["body"])),
			})
		}
	}
	return out, nil
}

func truncBody(s string) string {
	if len(s) <= sampleBodyCap {
		return s
	}
	return s[:sampleBodyCap] + "…(잘림)"
}

func round1(f float64) float64 { return float64(int(f*10+0.5)) / 10 }

func safeDiv(a, b float64) float64 {
	if b == 0 {
		return 0
	}
	return a / b
}
