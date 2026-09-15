// get_metric_series / get_metric_dimensions — 지표 조회 2종 (도구 계약
// §3). metric 이름은 discover_signals가 알려준 것을 쓴다는 전제.
// 기준선 비교: baseline_window 생략 시 직전 동일 길이 창(계약 §2).
package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// selector는 이름·대상 고정 selector를 만든다(라벨 값은 %q 인용).
func selector(metric, target string) string {
	return fmt.Sprintf(`{__name__=%q, target_id=%q}`, metric, target)
}

// NewMetricSeriesTool은 get_metric_series 도구를 만든다.
func NewMetricSeriesTool(vm *VM) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target":        map[string]string{"type": "string", "description": "target_id(UUID)"},
			"metric":        map[string]string{"type": "string", "description": "지표 이름 — discover_signals가 알려준 것"},
			"from":          map[string]string{"type": "string", "description": "UTC RFC3339, 반개구간 시작(포함)"},
			"to":            map[string]string{"type": "string", "description": "UTC RFC3339, 반개구간 끝(제외)"},
			"baseline_from": map[string]string{"type": "string", "description": "기준선 창 시작(선택, 생략 시 직전 동일 길이 창)"},
			"baseline_to":   map[string]string{"type": "string", "description": "기준선 창 끝(선택)"},
		},
		"required": []string{"target", "metric", "from", "to"},
	})
	return llm.Tool{
		Name:        "get_metric_series",
		Description: "대상의 지표가 기준선 창 대비 어떤지 요약한다(평균·최대·마지막 값·변화율).",
		Parameters:  params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			in, err := parseTargetWindow(args)
			if err != nil {
				return nil, err
			}
			var ex struct {
				Metric       string `json:"metric"`
				BaselineFrom string `json:"baseline_from"`
				BaselineTo   string `json:"baseline_to"`
			}
			_ = json.Unmarshal(args, &ex)
			if ex.Metric == "" {
				return nil, fmt.Errorf("인자 오류: metric 필수 — 지표 이름은 discover_signals로 발굴하라")
			}
			bFrom, bTo := in.FromT.Add(-in.ToT.Sub(in.FromT)), in.FromT
			if ex.BaselineFrom != "" && ex.BaselineTo != "" {
				var e1, e2 error
				bFrom, e1 = time.Parse(time.RFC3339, ex.BaselineFrom)
				bTo, e2 = time.Parse(time.RFC3339, ex.BaselineTo)
				if e1 != nil || e2 != nil || !bFrom.Before(bTo) {
					return nil, fmt.Errorf("기준선 창 오류: baseline_from < baseline_to, UTC RFC3339 필요")
				}
			}
			return queryMetricSeries(ctx, vm, in.Target, ex.Metric, in.FromT, in.ToT, bFrom, bTo)
		},
	}
}

// windowStats는 한 창의 집계다(대상의 전 시계열 합산 관점).
type windowStats struct {
	Avg, Max, Min, Last float64
	Series              int
}

func queryWindowStats(ctx context.Context, vm *VM, sel string, from, to time.Time) (*windowStats, error) {
	w := windowSeconds(from, to)
	exprs := map[string]string{
		"avg":    fmt.Sprintf("avg(avg_over_time(%s[%s]))", sel, w),
		"max":    fmt.Sprintf("max(max_over_time(%s[%s]))", sel, w),
		"min":    fmt.Sprintf("min(min_over_time(%s[%s]))", sel, w),
		"last":   fmt.Sprintf("avg(last_over_time(%s[%s]))", sel, w),
		"series": fmt.Sprintf("count(count_over_time(%s[%s]))", sel, w),
	}
	st := windowStats{}
	for k, expr := range exprs {
		// 창의 끝 시각(제외 경계)에서 1스텝 물러난 시각으로 평가 —
		// [from, to) 반개구간의 rollup은 to 직전까지만 본다.
		samples, err := vm.InstantQuery(ctx, expr, to.Add(-time.Second))
		if err != nil {
			return nil, err
		}
		if len(samples) == 0 {
			if k == "avg" {
				return nil, nil // 창에 관측 없음
			}
			continue
		}
		v := samples[0].Value
		switch k {
		case "avg":
			st.Avg = v
		case "max":
			st.Max = v
		case "min":
			st.Min = v
		case "last":
			st.Last = v
		case "series":
			st.Series = int(v)
		}
	}
	return &st, nil
}

func queryMetricSeries(ctx context.Context, vm *VM, target, metric string, from, to, bFrom, bTo time.Time) (any, error) {
	sel := selector(metric, target)
	cur, err := queryWindowStats(ctx, vm, sel, from, to)
	if err != nil {
		return nil, fmt.Errorf("get_metric_series 조회: %w", err)
	}
	if cur == nil {
		return Envelope{
			Status:       "no_data",
			NoDataReason: NoDataUnknown,
			Summary: fmt.Sprintf("창 내 %s 관측 없음 — 지표 이름 오류인지 수집 결손인지 구분하려면 "+
				"discover_signals(지표 인벤토리)와 get_data_coverage를 쓰라.", metric),
		}, nil
	}
	base, err := queryWindowStats(ctx, vm, sel, bFrom, bTo)
	if err != nil {
		return nil, fmt.Errorf("get_metric_series 기준선 조회: %w", err)
	}

	ref := fmt.Sprintf("vm:%s{target_id=%s}:%s/%s", metric, target,
		from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
	f := Finding{
		"metric": metric,
		"current": map[string]any{"avg": cur.Avg, "max": cur.Max, "min": cur.Min,
			"last": cur.Last, "series_count": cur.Series},
		"refs": []string{ref},
	}
	env := Envelope{Findings: []Finding{f}, Refs: []string{ref},
		ObservedRange: &TimeRange{From: from.UTC(), To: to.UTC()}}

	if base == nil {
		env.Status = "normal"
		env.AssessmentBasis = "기준선 창에 관측 없음 — 비교 불가, 보수적 normal(현재 창 절대값은 findings 참조)"
		env.Summary = fmt.Sprintf("%s 현재 창 avg=%.4g max=%.4g. 기준선 창(%s~%s)에 관측이 없어 변화 판정 불가.",
			metric, cur.Avg, cur.Max, bFrom.UTC().Format(time.RFC3339), bTo.UTC().Format(time.RFC3339))
		return env, nil
	}

	f["baseline"] = map[string]any{"avg": base.Avg, "max": base.Max, "min": base.Min}
	var changeText string
	anomalous := false
	switch {
	case base.Avg != 0:
		change := (cur.Avg - base.Avg) / math.Abs(base.Avg)
		f["avg_change_ratio"] = change // JSON에 Inf 금지 — 이 분기만 수치
		anomalous = math.Abs(change) >= 0.5
		changeText = fmt.Sprintf("변화율 %+.0f%%", change*100)
	case cur.Avg == 0:
		f["avg_change_ratio"] = 0.0
		changeText = "변화 없음(양쪽 0)"
	default:
		f["note"] = "기준선 평균 0에서 값 등장 — 변화율 산출 불가"
		anomalous = true
		changeText = "기준선 0 → 값 등장"
	}

	// 임시 판정식(계약 §7 열린 결정): 평균 ±50% 이상 변화 = anomalous.
	env.Status = "normal"
	if anomalous {
		env.Status = "anomalous"
	}
	env.AssessmentBasis = fmt.Sprintf("기준선 창(%s~%s) 평균 대비 변화율 ±50%% 기준(임시 판정식)",
		bFrom.UTC().Format(time.RFC3339), bTo.UTC().Format(time.RFC3339))
	env.Summary = fmt.Sprintf("%s: 현재 avg=%.4g(max %.4g) vs 기준선 avg=%.4g — %s.",
		metric, cur.Avg, cur.Max, base.Avg, changeText)
	return env, nil
}

// NewMetricDimensionsTool은 get_metric_dimensions 도구를 만든다 —
// "이 집계 지표를 누가 끌어올렸나"(process/interface/pod … 차원 분해).
func NewMetricDimensionsTool(vm *VM) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target":    map[string]string{"type": "string", "description": "target_id(UUID)"},
			"metric":    map[string]string{"type": "string", "description": "지표 이름"},
			"dim_label": map[string]string{"type": "string", "description": "분해 차원 라벨(예: process_executable_name, interface, pod_name)"},
			"from":      map[string]string{"type": "string", "description": "UTC RFC3339"},
			"to":        map[string]string{"type": "string", "description": "UTC RFC3339"},
			"top_n":     map[string]string{"type": "integer", "description": "상위 N(기본 5)"},
		},
		"required": []string{"target", "metric", "dim_label", "from", "to"},
	})
	return llm.Tool{
		Name:        "get_metric_dimensions",
		Description: "집계 지표를 차원 라벨(프로세스·인터페이스·pod 등)로 분해해 상위 기여자를 준다.",
		Parameters:  params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			in, err := parseTargetWindow(args)
			if err != nil {
				return nil, err
			}
			var ex struct {
				Metric string `json:"metric"`
				Dim    string `json:"dim_label"`
				TopN   int    `json:"top_n"`
			}
			_ = json.Unmarshal(args, &ex)
			if ex.Metric == "" || ex.Dim == "" {
				return nil, fmt.Errorf("인자 오류: metric·dim_label 필수")
			}
			if ex.TopN <= 0 {
				ex.TopN = 5
			}
			return queryMetricDimensions(ctx, vm, in.Target, ex.Metric, ex.Dim, in.FromT, in.ToT, ex.TopN)
		},
	}
}

func queryMetricDimensions(ctx context.Context, vm *VM, target, metric, dim string, from, to time.Time, topN int) (any, error) {
	sel := selector(metric, target)
	w := windowSeconds(from, to)
	expr := fmt.Sprintf("sum by(%s)(avg_over_time(%s[%s]))", dim, sel, w)
	samples, err := vm.InstantQuery(ctx, expr, to.Add(-time.Second))
	if err != nil {
		return nil, fmt.Errorf("get_metric_dimensions 조회: %w", err)
	}
	if len(samples) == 0 {
		return Envelope{
			Status:       "no_data",
			NoDataReason: NoDataUnknown,
			Summary: fmt.Sprintf("%s의 창 내 관측 없음 — 지표 이름·차원 라벨 확인은 discover_signals와 "+
				"get_metric_series(라벨 없는 조회)로.", metric),
		}, nil
	}
	// 차원 라벨이 아예 없으면 전부 빈 라벨 하나로 뭉친다 — 복구 정보로
	// 사용 가능한 라벨 키 목록을 준다.
	if len(samples) == 1 && samples[0].Labels[dim] == "" {
		one, qerr := vm.InstantQuery(ctx, fmt.Sprintf("last_over_time(%s[%s])", sel, w), to.Add(-time.Second))
		var keys []string
		if qerr == nil && len(one) > 0 {
			for k := range one[0].Labels {
				if k != "__name__" && k != "target_id" {
					keys = append(keys, k)
				}
			}
			sort.Strings(keys)
		}
		return nil, fmt.Errorf("지표 %s에 차원 라벨 %q가 없음 — 이 지표의 라벨: %v", metric, dim, keys)
	}

	sort.Slice(samples, func(i, j int) bool { return samples[i].Value > samples[j].Value })
	var total float64
	for _, s := range samples {
		total += s.Value
	}
	truncated := len(samples) > topN
	shown := samples
	if truncated {
		shown = samples[:topN]
	}
	var findings []Finding
	var refs []string
	for _, s := range shown {
		val := s.Labels[dim]
		ref := fmt.Sprintf("vm:%s{target_id=%s,%s=%s}:%s/%s", metric, target, dim, val,
			from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
		refs = append(refs, ref)
		share := 0.0
		if total != 0 {
			share = s.Value / total
		}
		findings = append(findings, Finding{
			dim: val, "avg": s.Value, "share": share, "refs": []string{ref},
		})
	}
	topShare, _ := findings[0]["share"].(float64)
	status := "normal"
	if topShare >= 0.5 {
		status = "anomalous"
	}
	return Envelope{
		Status: status,
		Summary: fmt.Sprintf("%s를 %s 차원 %d개로 분해(전체 %d개). 최대 기여 %s=%v(점유 %.0f%%).",
			metric, dim, len(shown), len(samples), dim, findings[0][dim], topShare*100),
		AssessmentBasis: "최대 기여자 점유율 50% 이상 = 지배 기여자 존재(임시 판정식)",
		Findings:        findings,
		Refs:            refs,
		Truncated:       truncated,
		ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
	}, nil
}
