// discover_signals — 메타 도구 (도구 계약 §3, 2026-07-15 레드팀 신설).
// "이 대상에서 무엇이 이상한가": 지표 인벤토리(이름·단위) + 기준선 대비
// 이상 상위 N. 조사자가 지표 이름을 아는 유일한 경로 — Examiner는
// 대상마다 이 도구로 시작한다(계약 §6).
//
// 구현: VM의 이름 없는 selector 집계(MetricsQL)로 전 지표를 2번의
// 쿼리(현재 창·기준선 창)로 훑고, 의미(단위)는 PG metric_definitions
// (metric_key→unit)를 결합한다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

const inventoryCap = 300 // 인벤토리 이름 나열 상한 — 넘으면 truncated

// NewDiscoverSignalsTool은 discover_signals 도구를 만든다. db(PG)는
// 지표 의미 결합용 — nil이면 단위 없이 동작한다.
func NewDiscoverSignalsTool(vm *VM, db *sql.DB) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]string{"type": "string", "description": "target_id(UUID)"},
			"from":   map[string]string{"type": "string", "description": "UTC RFC3339, 반개구간 시작(포함)"},
			"to":     map[string]string{"type": "string", "description": "UTC RFC3339, 반개구간 끝(제외)"},
			"top_n":  map[string]string{"type": "integer", "description": "이상 상위 N(기본 10)"},
		},
		"required": []string{"target", "from", "to"},
	})
	return llm.Tool{
		Name: "discover_signals",
		Description: "대상의 지표 인벤토리(이름·단위)와 기준선 창 대비 변화 상위 N을 준다 — " +
			"조사 시작점. 여기 나온 지표 이름으로 get_metric_series/get_metric_dimensions를 부른다.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			in, err := parseTargetWindow(args)
			if err != nil {
				return nil, err
			}
			var ex struct {
				TopN int `json:"top_n"`
			}
			_ = json.Unmarshal(args, &ex)
			if ex.TopN <= 0 {
				ex.TopN = 10
			}
			return discoverSignals(ctx, vm, db, in.Target, in.FromT, in.ToT, ex.TopN)
		},
	}
}

func discoverSignals(ctx context.Context, vm *VM, db *sql.DB, target string, from, to time.Time, topN int) (any, error) {
	match := fmt.Sprintf(`{target_id=%q}`, target)
	names, err := vm.LabelValues(ctx, "__name__", match, from, to)
	if err != nil {
		return nil, fmt.Errorf("discover_signals 인벤토리 조회: %w", err)
	}
	if len(names) == 0 {
		return Envelope{
			Status:       "no_data",
			NoDataReason: NoDataUnknown,
			Summary: "이 대상·시간창에 지표가 하나도 없음 — 메트릭 미수집 대상(application 등 " +
				"CH 원천 도메인)이거나 수집 결손. get_data_coverage로 판별하라.",
		}, nil
	}

	w := windowSeconds(from, to)
	curExpr := fmt.Sprintf("avg by(__name__)(avg_over_time(%s[%s]))", match, w)
	cur, err := vm.InstantQuery(ctx, curExpr, to.Add(-time.Second))
	if err != nil {
		return nil, fmt.Errorf("discover_signals 현재 창 집계: %w", err)
	}
	baseExpr := fmt.Sprintf("avg by(__name__)(avg_over_time(%s[%s]))", match, w)
	base, err := vm.InstantQuery(ctx, baseExpr, from.Add(-time.Second))
	if err != nil {
		return nil, fmt.Errorf("discover_signals 기준선 창 집계: %w", err)
	}
	baseByName := map[string]float64{}
	for _, s := range base {
		baseByName[s.Labels["__name__"]] = s.Value
	}
	units, _ := metricUnits(ctx, db, names) // 실패해도 진행(단위는 보조 정보)

	type scored struct {
		name             string
		cur, base, score float64
		hasBase          bool
	}
	var sc []scored
	for _, s := range cur {
		name := s.Labels["__name__"]
		b, ok := baseByName[name]
		e := scored{name: name, cur: s.Value, base: b, hasBase: ok}
		if ok {
			denom := math.Max(math.Abs(b), 1e-9)
			e.score = math.Abs(s.Value-b) / denom
		} else {
			e.score = math.Inf(1) // 기준선에 없던 지표의 등장 자체가 신호
		}
		sc = append(sc, e)
	}
	sort.Slice(sc, func(i, j int) bool { return sc[i].score > sc[j].score })

	shown := sc
	if len(shown) > topN {
		shown = sc[:topN]
	}
	var findings []Finding
	var refs []string
	anomalous := 0
	for _, e := range shown {
		ref := fmt.Sprintf("vm:%s{target_id=%s}:%s/%s", e.name, target,
			from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
		refs = append(refs, ref)
		f := Finding{"metric": e.name, "unit": units[e.name],
			"current_avg": e.cur, "refs": []string{ref}}
		if e.hasBase {
			f["baseline_avg"] = e.base
			f["change_score"] = e.score
			if e.score >= 0.5 {
				anomalous++
			}
		} else {
			f["note"] = "기준선 창에 없던 지표 — 새로 나타남"
			anomalous++
		}
		findings = append(findings, f)
	}

	invNames := names
	invTruncated := len(invNames) > inventoryCap
	if invTruncated {
		invNames = invNames[:inventoryCap]
	}
	findings = append(findings, Finding{
		"inventory_total": len(names), "inventory": invNames,
		"inventory_truncated": invTruncated,
	})

	status := "normal"
	if anomalous > 0 {
		status = "anomalous"
	}
	return Envelope{
		Status: status,
		Summary: fmt.Sprintf("지표 %d종 수집 중. 기준선 창 대비 변화 상위 %d개 중 유의 변화(±50%% 이상 또는 신규 등장) %d개 — findings 참조.",
			len(names), len(shown), anomalous),
		AssessmentBasis: "직전 동일 길이 창 평균 대비 변화율 ±50%(임시 판정식) + 신규 등장",
		Findings:        findings,
		Refs:            refs,
		Truncated:       len(sc) > topN || invTruncated,
		ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
	}, nil
}

// metricUnits는 PG metric_definitions에서 지표 단위를 가져온다.
func metricUnits(ctx context.Context, db *sql.DB, names []string) (map[string]string, error) {
	units := map[string]string{}
	if db == nil {
		return units, nil
	}
	rows, err := db.QueryContext(ctx, `SELECT metric_key, coalesce(unit, '') FROM metric_definitions
		WHERE metric_key = ANY($1)`, names)
	if err != nil {
		return units, err
	}
	defer rows.Close()
	for rows.Next() {
		var k, u string
		if err := rows.Scan(&k, &u); err != nil {
			return units, err
		}
		units[k] = u
	}
	return units, rows.Err()
}
