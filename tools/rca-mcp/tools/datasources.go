package tools

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// NewDescribeDataSourcesTool exposes the complete captured-data inventory so
// an investigator can discover data that is not tied to a pre-written
// scenario. It is deliberately read-only and backend-independent: the tool
// describes the available evidence surface; domain tools retrieve the rows.
func NewDescribeDataSourcesTool() llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"domain": map[string]any{
				"type":        "string",
				"description": "선택 필터: inventory | metrics | logs | events | traces | database | process | network | kubernetes",
			},
		},
	})
	return llm.Tool{
		Name:        "describe_data_sources",
		Description: "현재 캡처·수집 데이터 전체를 탐색한다. 각 데이터셋의 주요 필드, 시간·대상 축, 담당 조회 도구, 제한사항을 반환한다. 시나리오에 나오지 않은 근거를 찾을 때 먼저 사용한다.",
		Parameters:  params,
		Call: func(_ context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Domain string `json:"domain"`
			}
			if len(args) > 0 {
				if err := json.Unmarshal(args, &in); err != nil {
					return nil, err
				}
			}
			findings := dataSourceFindings(in.Domain)
			if len(findings) == 0 {
				return nil, fmt.Errorf("unknown domain %q", in.Domain)
			}
			return Envelope{
				Status:          "normal",
				AssessmentBasis: "정적 데이터 카탈로그 — 실제 행 조회가 아니라 조사 가능한 원천·축·도구 표면을 설명한다.",
				Summary:         "수집 데이터 원천과 조회 도구를 반환했다. 대상 UUID를 모르면 search_targets로 이름·유형·주소를 검색한 뒤 담당 도구로 조회하라.",
				Findings:        findings,
				Refs:            []string{"catalog:captured-data:v1"},
			}, nil
		},
	}
}

func dataSourceFindings(domain string) []Finding {
	all := []Finding{
		{"domain": "inventory", "datasets": []string{"targets"}, "fields": []string{"id", "name", "display_name", "type", "address"}, "axes": []string{"query", "type", "offset"}, "tools": []string{"search_targets", "describe_target"}, "limits": []string{"현재 연결한 DB의 인벤토리이며 대상 존재가 장애·건강을 증명하지 않는다."}},
		{"domain": "metrics", "datasets": []string{"VictoriaMetrics"}, "fields": []string{"metric", "target_id", "timestamp", "value", "labels"}, "axes": []string{"target", "metric", "time", "label"}, "tools": []string{"scan_metrics", "read_timeseries", "compare_peers", "get_processes"}, "limits": []string{"지표 이름·대상 축은 먼저 scan_metrics로 발견", "보존기간·수집 상태는 get_data_coverage로 확인"}},
		{"domain": "logs", "datasets": []string{"lucida_logs_local", "syslog_local"}, "fields": []string{"timestamp", "target_id", "host_name", "service_name", "severity", "body", "trace_id", "span_id", "facility", "app_name", "message"}, "axes": []string{"time", "target", "host", "service", "severity", "text"}, "tools": []string{"sample_logs", "search_host_data"}, "limits": []string{"syslog 일부 행은 target_id가 비어 host·app·본문 기준 탐색이 필요", "sample_logs는 표본·상한이 있음"}},
		{"domain": "events", "datasets": []string{"lucida_events_local", "kcm_events_local", "event_cluster_projection_local"}, "fields": []string{"event_id", "occurred_at", "episode_id", "evidence", "object_name", "reason", "cluster_id", "desired_version", "assigned_at"}, "axes": []string{"time", "target", "episode", "object", "cluster"}, "tools": []string{"list_events", "list_changes", "get_k8s_state", "get_cluster_projections"}, "limits": []string{"KCM은 Kubernetes 대상에서만 조회"}},
		{"domain": "traces", "datasets": []string{"otel_traces_local", "trace_error_chains_local", "trace_path_signatures_local"}, "fields": []string{"trace_id", "span_id", "parent_span_id", "span_kind", "service_name", "span_name", "duration_ns", "status_code", "span_attributes", "chain_signature", "path_signature", "exemplar_trace_ids"}, "axes": []string{"time", "service", "span_kind", "route", "trace", "attribute"}, "tools": []string{"breakdown_endpoints", "expand_topology", "get_trace_summaries", "get_trace_spans", "get_trace_activity"}, "limits": []string{"topic·consumer_group 원문은 topology edge에서 제공", "get_trace_spans로 exemplar 원문 조회; get_trace_activity로 현재/기준선의 시간별 공백 비교"}},
		{"domain": "database", "datasets": []string{"dpm_session_local", "dpm_topsql_local"}, "fields": []string{"timestamp", "target_id", "engine", "sql_id", "sql_hash", "severity_text", "body"}, "axes": []string{"time", "database", "engine", "sql_key", "severity"}, "tools": []string{"db_blocking", "db_slow_queries", "read_timeseries"}, "limits": []string{"세션 볼륨·커넥션 포화는 read_timeseries로 분리"}},
		{"domain": "process", "datasets": []string{"process_snapshot", "process_meta"}, "fields": []string{"target_id", "proc_key", "pid", "ppid", "name", "state", "cpu_pct", "mem_rss", "threads", "user", "cmdline", "create_time"}, "axes": []string{"time", "target", "pid", "proc_key", "offset"}, "tools": []string{"get_processes", "get_process_snapshot"}, "limits": []string{"snapshot 수치는 구간 최대값; 최신 상태와 최고 자원 사용 시각은 다를 수 있다"}},
		{"domain": "network", "datasets": []string{"host_connections"}, "fields": []string{"timestamp", "target_id", "host_name", "local_addr", "local_port", "remote_addr", "remote_port", "direction", "process", "state"}, "axes": []string{"time", "target", "host", "remote", "local_port", "remote_port", "direction", "query", "process", "state", "offset"}, "tools": []string{"expand_topology", "search_host_data", "get_snmp_traps"}, "limits": []string{"socket 관측은 앱 소유권 근거로 사용하지 않음"}},
		{"domain": "kubernetes", "datasets": []string{"kcm_events_local", "event_cluster_projection_local"}, "fields": []string{"timestamp", "target_id", "namespace", "object_kind", "object_name", "reason", "event_type", "cluster_id", "desired_version"}, "axes": []string{"time", "cluster", "namespace", "object", "reason"}, "tools": []string{"list_events", "get_k8s_state", "list_changes"}, "limits": []string{"KCM 수집·창 관측·지표 등록 결손을 구분해야 함"}},
	}
	if domain == "" {
		return all
	}
	out := make([]Finding, 0, 1)
	for _, f := range all {
		if f["domain"] == domain {
			out = append(out, f)
		}
	}
	return out
}
