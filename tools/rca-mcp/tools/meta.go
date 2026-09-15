// 메타 도구 (도구 계약 §3, 2026-07-15 레드팀 신설).
//
//	get_data_coverage — 이 대상·시간창에 어떤 데이터가 수집되고
//	  있(었)나: PG collectors 상태 + 저장소별 실제 관측을 대조해
//	  no_data_reason 5구분의 판별 재료를 준다.
//
// get_runtime_connections는 빠졌다(§12.4 자리 교체) — 원천
// host_connections에 앱 프로세스가 0건이라 "topology 밖 런타임 의존"을
// 낼 수 없었고, 조사자에게는 "소켓도 봤는데 없더라"는 거짓 안심만
// 줬다. 그 몫은 expand_topology의 apm_client_peer가 승계한다(호출자
// CLIENT span이 계측 밖 목적지를 더 정확히 지목한다).
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// NewDataCoverageTool은 get_data_coverage 도구를 만든다.
func NewDataCoverageTool(pg *sql.DB, ch *CH, vm *VM) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"targets": map[string]any{
				"type": "array", "items": map[string]string{"type": "string"},
				"description": "판별할 target_id(UUID) 목록",
			},
			"from": map[string]string{"type": "string", "description": "UTC RFC3339"},
			"to":   map[string]string{"type": "string", "description": "UTC RFC3339"},
		},
		"required": []string{"targets", "from", "to"},
	})
	return llm.Tool{
		Name: "get_data_coverage",
		Description: "대상별 수집기 상태(등록·최근 수집 성공/실패)와 저장소별 실제 관측 수를 대조한다 — " +
			"수집기 상태는 현재 스냅샷이며 사건 시간창의 연속 수집을 증명하지 않는다. 0건은 장애 배제 근거가 아니다.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Targets  []string `json:"targets"`
				From, To string
			}
			if err := json.Unmarshal(args, &in); err != nil || len(in.Targets) == 0 {
				return nil, fmt.Errorf(`인자 오류: {"targets": ["<uuid>", …], "from": …, "to": …} 필요`)
			}
			for _, t := range in.Targets {
				if !uuidRe.MatchString(t) {
					return nil, fmt.Errorf("%q는 target_id가 아님 — 순수 UUID 필요", t)
				}
			}
			from, e1 := time.Parse(time.RFC3339, in.From)
			to, e2 := time.Parse(time.RFC3339, in.To)
			if e1 != nil || e2 != nil || !from.Before(to) {
				return nil, fmt.Errorf("시간창 오류: from·to는 UTC RFC3339, from < to 필요")
			}
			// 대상 축 절단 — 종전엔 조용히 잘랐다(§4 A6이 ceil(N/10) 청크
			// 호출을 요구하는 근거). 잘린 수를 봉투에 싣는다(§5.1 계약 2).
			asked := len(in.Targets)
			if len(in.Targets) > 10 {
				in.Targets = in.Targets[:10]
			}
			out, err := queryDataCoverage(ctx, pg, ch, vm, in.Targets, from, to)
			if env, ok := out.(Envelope); ok && err == nil {
				env.Scopes = []QueryScope{qscope("target", asked, len(in.Targets))}
				env.Truncated = asked > len(in.Targets)
				return env, nil
			}
			return out, err
		},
	}
}

func queryDataCoverage(ctx context.Context, pg *sql.DB, ch *CH, vm *VM, targets []string, from, to time.Time) (any, error) {
	var findings []Finding
	var refs []string
	problems := 0
	for _, target := range targets {
		f := Finding{"target_id": target}
		ref := "pg:collectors:target:" + target
		f["refs"] = []string{ref}
		refs = append(refs, ref)

		// ① 수집기 상태.
		rows, err := pg.QueryContext(ctx, `
			SELECT kind, enabled, coalesce(last_collect_status, ''),
			       coalesce(last_collect_error, ''), last_collected_at
			FROM collectors WHERE target_id = $1::uuid`, target)
		if err != nil {
			return nil, fmt.Errorf("get_data_coverage 수집기 조회: %w", pgErr(err))
		}
		var collectors []map[string]any
		collectorProblem := false
		func() {
			defer rows.Close()
			for rows.Next() {
				var kind, status, cerr string
				var enabled bool
				var at sql.NullTime
				if e := rows.Scan(&kind, &enabled, &status, &cerr, &at); e != nil {
					err = e
					return
				}
				c := map[string]any{"kind": kind, "enabled": enabled, "last_status": status}
				c["window_relation"] = collectorWindowRelation(at, from, to)
				if cerr != "" {
					c["last_error"] = cerr
				}
				if at.Valid {
					c["last_collected_at"] = at.Time.UTC().Format(time.RFC3339)
					// 창 끝 이전 마지막 수집이 창 시작보다 오래됐으면 공백.
					if at.Time.Before(from) {
						c["stale_for_window"] = true
						collectorProblem = true
					}
				}
				if !enabled || (status != "" && status != "success") {
					collectorProblem = true
				}
				collectors = append(collectors, c)
			}
			if rows.Err() != nil {
				err = rows.Err()
			}
		}()
		if err != nil {
			return nil, fmt.Errorf("get_data_coverage 수집기 읽기: %w", pgErr(err))
		}
		f["collectors"] = collectors
		f["collector_scope"] = "latest metadata snapshot; not collection history or window coverage proof"

		// ② 저장소별 창 내 관측 수. 원천 실패는 조용히 무시하지 않는다
		// (§15.2-3 A6 파급 계약, 6b — 종전 if err==nil 채택은 "0건"과
		// "조회 실패"를 구분 불능으로 만들었다): 실패한 원천은
		// source_errors에 분류 토큰으로 명시한다.
		obs := map[string]any{}
		srcErrs := map[string]string{}
		markErr := func(name string, err error) {
			var be *BackendError
			if errors.As(err, &be) {
				srcErrs[name] = be.Detail
				return
			}
			srcErrs[name] = "query_failed"
		}
		if vm != nil {
			s, err := vm.InstantQuery(ctx,
				fmt.Sprintf(`count(count_over_time({target_id=%q}[%s]))`, target, windowSeconds(from, to)),
				to.Add(-time.Second))
			if err == nil {
				n := 0
				if len(s) > 0 {
					n = int(s[0].Value)
				}
				obs["vm_series"] = n
			} else {
				markErr("vm_series", err)
			}
		}
		if ch != nil {
			p := map[string]string{"t": target, "from": chTime(from), "to": chTime(to)}
			for name, q := range map[string]string{
				"ch_logs": `SELECT count() AS n FROM lucida_logs_local
					WHERE (target_id = {t:String} OR host_target_id = {t:String})
					  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
					  AND timestamp < parseDateTime64BestEffort({to:String}, 9)`,
				"ch_events": `SELECT count() AS n FROM lucida_events_local
					WHERE target_id = {t:String}
					  AND occurred_at >= parseDateTime64BestEffort({from:String}, 9)
					  AND occurred_at < parseDateTime64BestEffort({to:String}, 9)`,
			} {
				// 단일 행 집계(count() 단독, GROUP BY 없음) — 절단 불가 표면, 표식 버림.
				if rows, _, err := ch.Query(ctx, q, p); err == nil && len(rows) == 1 {
					obs[name] = asInt(rows[0]["n"])
				} else if err != nil {
					markErr(name, err)
				} else {
					srcErrs[name] = "unexpected_aggregate_result"
				}
			}
		}
		f["window_observations"] = obs
		if len(srcErrs) > 0 {
			f["source_errors"] = srcErrs
		}

		// ③ 판별 재료 요약 — 판정은 조사자·규칙 몫, 재료만 정리.
		switch {
		case len(collectors) == 0:
			f["coverage_note"] = "수집기 메타데이터 없음. 실제 관측 유무는 window_observations 참조; 미수집으로 단정할 수 없음"
		case collectorProblem:
			f["coverage_note"] = "수집기 스냅샷에 비활성·실패·창 이전 수집이 있음. 해당 시간창의 실제 수집 이력은 미확인; 0건을 장애 배제 근거로 사용하지 말라"
			problems++
		default:
			f["coverage_note"] = "최근 수집기 상태만 확인됨. 데이터 종류별 시간창 내 연속 수집은 미확인; 관측 0건은 빈 조회 결과이며 장애 배제 근거가 아님"
		}
		findings = append(findings, f)
	}

	status := "normal"
	if problems > 0 {
		status = "anomalous"
	}
	return Envelope{
		Status: status,
		Summary: fmt.Sprintf("대상 %d개의 수집기 상태·저장소 관측 대조. 수집기 문제 있는 대상 %d개 — coverage_note가 no_data_reason 해석 지침이다.",
			len(findings), problems),
		AssessmentBasis: "수집기 enabled·last_collect_status 기준(수집기 문제 = 관측 신뢰 저하 신호)",
		Findings:        findings,
		Refs:            refs,
		ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
	}, nil
}

func collectorWindowRelation(at sql.NullTime, from, to time.Time) string {
	if !at.Valid {
		return "unknown"
	}
	if at.Time.Before(from) {
		return "before_window"
	}
	if !at.Time.Before(to) {
		return "after_window"
	}
	return "within_window_snapshot_only"
}
