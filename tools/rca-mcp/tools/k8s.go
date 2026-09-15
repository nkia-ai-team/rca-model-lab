// get_k8s_state — "pod 재시작·스케줄링·리소스 상태는" (도구 계약 §4,
// kubernetes 도메인). 원천 실측 확정(2026-07-15): VM kcm.pod.*
// (라벨 namespace·pod·target_id=클러스터) + PG kcm_resource_targets
// (승격 리소스 → 클러스터·resource_key="namespace/name" 해석).
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

// NewK8sStateTool은 get_k8s_state 도구를 만든다. target은 kubernetes
// 클러스터 대상이거나 승격된 하위 리소스 대상(pod 등) 어느 쪽이든 된다.
func NewK8sStateTool(vm *VM, pg *sql.DB) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]string{"type": "string", "description": "kubernetes 클러스터 또는 승격 리소스의 target_id(UUID)"},
			"from":   map[string]string{"type": "string", "description": "UTC RFC3339"},
			"to":     map[string]string{"type": "string", "description": "UTC RFC3339"},
		},
		"required": []string{"target", "from", "to"},
	})
	return llm.Tool{
		Name:        "get_k8s_state",
		Description: "클러스터(또는 특정 pod)의 창 내 상태 — 재시작 증가, CrashLoopBackOff, OOMKilled, waiting pod.",
		Parameters:  params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			in, err := parseTargetWindow(args)
			if err != nil {
				return nil, err
			}
			return queryK8sState(ctx, vm, pg, in.Target, in.FromT, in.ToT)
		},
	}
}

// resolveK8sTarget은 승격 리소스면 (클러스터, pod 필터)로 푼다. pgTok은
// PG 조회 실패의 분류 토큰이다(§15.2-3, 2b — pg=optional): 실패하면 대상을
// 클러스터로 간주하고 진행하되, 승격 리소스였다면 조회 범위가 클러스터
// 전체로 오해석된다는 결손을 봉투가 명시해야 한다. 종전엔 optional 원천
// 실패가 도구 전체 오류가 됐다(등급 위반 실물, 2b 수리).
func resolveK8sTarget(ctx context.Context, pg *sql.DB, target string) (cluster, podFilter, pgTok string) {
	if pg == nil {
		return target, "", ""
	}
	var clusterID, kind, key string
	row := pg.QueryRowContext(ctx, `SELECT cluster_target_id::text, resource_kind, resource_key
		FROM kcm_resource_targets WHERE target_id = $1::uuid`, target)
	switch e := row.Scan(&clusterID, &kind, &key); e {
	case nil:
		if kind == "pod" {
			if i := strings.IndexByte(key, '/'); i >= 0 {
				return clusterID, key, ""
			}
		}
		// pod 외 리소스는 클러스터 전체로 확장(1차 — deployment 등
		// 하위 pod 매칭은 시나리오가 요구하면 추가).
		return clusterID, "", ""
	case sql.ErrNoRows:
		return target, "", "" // 클러스터 대상으로 간주
	default:
		return target, "", beDetail(pgErr(e))
	}
}

func queryK8sState(ctx context.Context, vm *VM, pg *sql.DB, target string, from, to time.Time) (any, error) {
	cluster, podFilter, pgTok := resolveK8sTarget(ctx, pg, target)
	// 부분 강등의 결손 명시(2b) — VM 관측 봉투 어느 갈래로 나가든 싣는다.
	degrade := func(env Envelope) Envelope {
		if cluster != target && podFilter == "" {
			env.Findings = append(env.Findings, Finding{"section": "resolution", "requested_target": target, "queried_cluster": cluster, "note": "비-pod 리소스는 클러스터 전체로 확장해 조회했다. 결과를 요청 리소스만의 상태로 귀속하지 말라."})
		}
		if pgTok == "" {
			return env
		}
		env.Findings = append(env.Findings, Finding{
			"section":       "resolution",
			"source_errors": map[string]string{"pg": pgTok},
			"note": "승격 리소스 해석 실패(PG) — 이 target_id를 클러스터로 간주해 조회했다. " +
				"pod 등 승격 리소스였다면 결과 범위가 틀리다(결손이지 판정이 아니다).",
		})
		env.Summary += " [주의: PG 실패로 승격 리소스 해석 없이 클러스터로 간주 조회 — resolution finding 참조]"
		return env
	}
	w := windowSeconds(from, to)
	podSel := fmt.Sprintf(`target_id=%q`, cluster)
	if podFilter != "" {
		ns, pod, _ := strings.Cut(podFilter, "/")
		podSel += fmt.Sprintf(`, namespace=%q, pod=%q`, ns, pod)
	}

	// 문제 신호별 pod 단위 집계 — 창 내 최대값(상태 게이지) 또는 증가량(카운터).
	type probe struct{ key, expr, desc string }
	probes := []probe{
		{"restart_increase",
			fmt.Sprintf(`sum by(namespace,pod)(max_over_time({__name__="kcm.pod.container_restart_count",%s}[%s])) - sum by(namespace,pod)(min_over_time({__name__="kcm.pod.container_restart_count",%s}[%s])) > 0`, podSel, w, podSel, w),
			"재시작 증가"},
		{"crash_loop_back_off",
			fmt.Sprintf(`sum by(namespace,pod)(max_over_time({__name__="kcm.pod.container_crash_loop_back_off",%s}[%s])) > 0`, podSel, w),
			"CrashLoopBackOff"},
		{"oom_killed",
			fmt.Sprintf(`sum by(namespace,pod)(max_over_time({__name__="kcm.pod.container_oom_killed",%s}[%s])) > 0`, podSel, w),
			"OOMKilled"},
		{"waiting",
			fmt.Sprintf(`sum by(namespace,pod)(max_over_time({__name__="kcm.pod.container_state_waiting",%s}[%s])) > 0`, podSel, w),
			"waiting 컨테이너"},
	}

	var findings []Finding
	var refs []string
	var scopes []QueryScope
	seenAny := false
	observedSignals := 0
	var observedPodCounts []int
	var signalCoverage []Finding
	var probeFailure error
	truncatedAny := false
	for _, p := range probes {
		// Keep measured zero values; a missing series is not an observed zero.
		p.expr = strings.TrimSuffix(p.expr, " > 0")
		samples, err := vm.InstantQuery(ctx, p.expr, to.Add(-time.Second))
		if err != nil {
			probeFailure = fmt.Errorf("get_k8s_state %s 조회: %w", p.key, err)
			signalCoverage = append(signalCoverage, Finding{"signal": p.key, "observation": "query_failed", "source_errors": map[string]string{"vm": "query_failed"}})
			continue
		}
		state := "absent"
		if len(samples) > 0 {
			observedSignals++
			observedPodCounts = append(observedPodCounts, len(samples))
			state = "observed_zero"
		}
		positive := samples[:0]
		for _, s := range samples {
			if s.Value > 0 {
				positive = append(positive, s)
				state = "positive"
			}
		}
		coverageRef := fmt.Sprintf("vm:kcm.pod:%s:%s:%s:%s/%s", cluster, podFilter, p.key, from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
		refs = append(refs, coverageRef)
		signalCoverage = append(signalCoverage, Finding{"signal": p.key, "observation": state, "observed_pods": len(samples), "refs": []string{coverageRef}})
		samples = positive
		sort.Slice(samples, func(i, j int) bool { return samples[i].Value > samples[j].Value })
		// 신호별 절단(§5.1 계약 2) — 한 봉투 안에 독립 쿼리 4개가 각자
		// 잘린다. 종전엔 여기서 잘라놓고 Truncated조차 세우지 않아
		// "restart 3건·OOM 0건"을 표현할 원천이 없었다(4차 A-7).
		total := len(samples)
		if len(samples) > 10 {
			samples = samples[:10]
			truncatedAny = true
		}
		if state != "absent" {
			scopes = append(scopes, qscope(p.key, total, len(samples)))
		}
		for _, s := range samples {
			seenAny = true
			ref := fmt.Sprintf("vm:kcm.pod:%s:%s/%s:%s:%s/%s", cluster, s.Labels["namespace"], s.Labels["pod"], p.key,
				from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
			refs = append(refs, ref)
			findings = append(findings, Finding{
				"signal": p.key, "desc": p.desc,
				"namespace": s.Labels["namespace"], "pod": s.Labels["pod"], "value": s.Value,
				"refs": []string{ref},
			})
		}
	}

	// 커버리지 확인 — 신호가 0이어도 클러스터가 관측되고 있었는지.
	alive, err := vm.InstantQuery(ctx,
		fmt.Sprintf(`count(sum by(namespace,pod)(count_over_time({__name__="kcm.pod.container_state_running",%s}[%s])))`, podSel, w),
		to.Add(-time.Second))
	if err != nil {
		if observedSignals == 0 {
			return nil, fmt.Errorf("get_k8s_state 관측 확인: %w", err)
		}
		signalCoverage = append(signalCoverage, Finding{"signal": "running", "observation": "query_failed", "note": "running 조회 실패; 다른 신호의 관측은 보존됨"})
	}
	var degradedSources map[string]string
	if observedSignals < len(probes) {
		degradedSources = map[string]string{"vm": "partial_signal_coverage"}
	}
	if observedSignals == 0 {
		if probeFailure != nil {
			return nil, probeFailure
		}
		scope := "클러스터"
		if podFilter != "" {
			scope = "pod " + podFilter
		}
		reason := NoDataUnknown
		summary := fmt.Sprintf("창 내 %s의 문제 신호 4종 관측 없음. running 관측 유무로 문제 신호의 정상 여부를 판단할 수 없다. 미수집·대상 매핑·시간창 원인은 미확인.", scope)
		return degrade(Envelope{
			Status:          "no_data",
			NoDataReason:    reason,
			Summary:         summary,
			Findings:        signalCoverage,
			Refs:            refs,
			ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
			DegradedSources: degradedSources,
		}), nil
	}
	observedPods := 0
	if len(alive) > 0 {
		observedPods = int(alive[0].Value)
	}
	for _, n := range observedPodCounts {
		if n != observedPods {
			degradedSources = map[string]string{"vm": "partial_signal_coverage"}
		}
	}
	positiveFindings := len(findings)
	findings = append(findings, signalCoverage...)

	scope := fmt.Sprintf("클러스터 %s", cluster)
	if podFilter != "" {
		scope = fmt.Sprintf("pod %s", podFilter)
	}
	if !seenAny {
		return degrade(Envelope{
			Status:          "normal",
			Summary:         fmt.Sprintf("%s: running 관측 pod %d개, 문제 신호 %d/4종 관측. 관측된 값 중 양수 없음. 미관측 신호 및 pod의 정상 여부는 알 수 없다.", scope, observedPods, observedSignals),
			AssessmentBasis: "조회된 신호 값에만 근거함; absent는 0이 아니며 장애 배제 근거가 아님",
			Findings:        findings,
			Refs:            refs,
			ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
			DegradedSources: degradedSources,
			Scopes:          scopes,
		}), nil
	}
	return degrade(Envelope{
		Status: "anomalous",
		Summary: fmt.Sprintf("%s: 관측 pod %d개 중 문제 신호 %d건 — 신호·pod는 findings 참조.",
			scope, observedPods, positiveFindings),
		AssessmentBasis: "문제 신호 4종 중 1개 이상 발생",
		Findings:        findings,
		Refs:            refs,
		ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
		Truncated:       truncatedAny,
		Scopes:          scopes,
		DegradedSources: degradedSources,
	}), nil
}
