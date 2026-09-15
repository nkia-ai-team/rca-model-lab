package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
	"time"
)

func NewTraceSpansTool(ch *CH) llm.Tool {
	params, _ := json.Marshal(map[string]any{"type": "object", "additionalProperties": false, "properties": map[string]any{
		"trace_id": map[string]string{"type": "string", "description": "Exact exemplar trace ID from a summary or trace finding"},
		"from":     map[string]string{"type": "string", "description": "RFC3339 inclusive"}, "to": map[string]string{"type": "string", "description": "RFC3339 exclusive; widen if trace crosses summary boundaries"},
		"limit": map[string]any{"type": "integer", "minimum": 1, "maximum": 100}, "offset": map[string]any{"type": "integer", "minimum": 0, "maximum": 1000000},
	}, "required": []string{"trace_id", "from", "to"}})
	return llm.Tool{Name: "get_trace_spans", Description: "Read raw spans and full attributes/events/links for an observed trace. duration_ns is an exact decimal string. Paginate for all spans; missing parents may lie outside the requested window or instrumentation.", Parameters: params, Call: func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in struct {
			TraceID  string `json:"trace_id"`
			From, To string
			Limit    *int
			Offset   int
		}
		if err := decodeTraceArgs(raw, &in); err != nil {
			return nil, err
		}
		from, e1 := time.Parse(time.RFC3339, in.From)
		to, e2 := time.Parse(time.RFC3339, in.To)
		if in.TraceID == "" || len(in.TraceID) > 128 || e1 != nil || e2 != nil || !from.Before(to) {
			return nil, fmt.Errorf("trace_id and RFC3339 from < to required")
		}
		limit := 50
		if in.Limit != nil {
			limit = *in.Limit
		}
		if limit < 1 || limit > 100 || in.Offset < 0 || in.Offset > 1000000 {
			return nil, fmt.Errorf("invalid page bounds")
		}
		rows, tr, err := ch.Query(ctx, `SELECT timestamp, trace_id, span_id, parent_span_id, trace_state, span_name, span_kind, status_code, status_message,
toString(duration_ns) AS duration_ns, end_timestamp, service_name, service_version, service_namespace, deployment_env, host_name,
k8s_namespace, k8s_pod_name, k8s_node_name, span_attributes, resource_attributes,
events_name, events_timestamp, events_attributes, links_trace_id, links_span_id, otel_schema_url, scope_name, scope_version
FROM otel_traces_local WHERE trace_id={trace:String}
AND timestamp >= parseDateTime64BestEffort({from:String},9) AND timestamp < parseDateTime64BestEffort({to:String},9)
ORDER BY timestamp, span_id, service_name, toJSONString(tuple(parent_span_id, span_name, status_code, duration_ns, span_attributes, resource_attributes, events_attributes))
LIMIT {limit:UInt32} OFFSET {offset:UInt32}`, map[string]string{"trace": in.TraceID, "from": chTime(from), "to": chTime(to), "limit": fmt.Sprint(limit + 1), "offset": fmt.Sprint(in.Offset)})
		if err != nil {
			return nil, fmt.Errorf("get_trace_spans: %w", err)
		}
		env := tracePage("otel_traces_local", rows, tr, limit, in.Offset)
		env.Findings = append(env.Findings, Finding{"section": "query_scope", "trace_id": in.TraceID, "requested_window": map[string]string{"from": in.From, "to": in.To}, "duration_unit": "nanoseconds", "limits": "Only spans whose start timestamp lies in the window; instrumentation/sampling gaps are not proof of no call."})
		return env, nil
	}}
}
