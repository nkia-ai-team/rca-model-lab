package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
	"time"
)

func NewTraceSummaryTool(ch *CH) llm.Tool {
	params, _ := json.Marshal(map[string]any{"type": "object", "additionalProperties": false, "properties": map[string]any{
		"kind":      map[string]any{"type": "string", "enum": []string{"error_chain", "path"}},
		"target":    map[string]string{"type": "string", "description": "Exact target UUID or service name"},
		"from":      map[string]string{"type": "string", "description": "RFC3339 inclusive"},
		"to":        map[string]string{"type": "string", "description": "RFC3339 exclusive"},
		"signature": map[string]string{"type": "string", "description": "Exact signature filter for drilldown"},
		"top_n":     map[string]any{"type": "integer", "minimum": 1, "maximum": 100},
		"offset":    map[string]any{"type": "integer", "minimum": 0, "maximum": 1000000},
	}, "required": []string{"kind", "target", "from", "to"}})
	return llm.Tool{Name: "get_trace_summaries", Description: "Read stored path/error summaries overlapping a window. Counts cover each original summary window, not just the overlap. Follow exemplar_trace_ids with get_trace_spans; offset pages retain all matching summaries.", Parameters: params, Call: func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in struct {
			Kind, Target, From, To, Signature string
			TopN                              *int `json:"top_n"`
			Offset                            int  `json:"offset"`
		}
		if err := decodeTraceArgs(raw, &in); err != nil {
			return nil, err
		}
		if in.Kind != "path" && in.Kind != "error_chain" {
			return nil, fmt.Errorf("kind must be path or error_chain")
		}
		from, e1 := time.Parse(time.RFC3339, in.From)
		to, e2 := time.Parse(time.RFC3339, in.To)
		if in.Target == "" || e1 != nil || e2 != nil || !from.Before(to) {
			return nil, fmt.Errorf("target and RFC3339 from < to required")
		}
		limit := 20
		if in.TopN != nil {
			limit = *in.TopN
		}
		if limit < 1 || limit > 100 || in.Offset < 0 || in.Offset > 1000000 {
			return nil, fmt.Errorf("top_n 1..100 and offset 0..1000000 required")
		}
		table, sig, countCol, hash := "trace_error_chains_local", "chain_signature", "chain_count", "chain_hash"
		extra := ", chain_depth, route"
		if in.Kind == "path" {
			table, sig, countCol, hash = "trace_path_signatures_local", "path_signature", "path_count", "path_hash"
			extra = ""
		}
		query := fmt.Sprintf(`SELECT target_id, service_name, %s AS signature, %s AS occurrence_count, %s AS signature_hash%s, exemplar_trace_ids, window_start, window_end, computed_at
FROM %s WHERE (target_id={target:String} OR service_name={target:String})
AND window_start < parseDateTime64BestEffort({to:String},9)
AND window_end > parseDateTime64BestEffort({from:String},9)
AND ({signature:String}='' OR %s={signature:String})
ORDER BY occurrence_count DESC, window_start, window_end, target_id, service_name, signature_hash, signature, computed_at, exemplar_trace_ids
LIMIT {limit:UInt32} OFFSET {offset:UInt32}`, sig, countCol, hash, extra, table, sig)
		rows, tr, err := ch.Query(ctx, query, map[string]string{"target": in.Target, "from": chTime(from), "to": chTime(to), "signature": in.Signature, "limit": fmt.Sprint(limit + 1), "offset": fmt.Sprint(in.Offset)})
		if err != nil {
			return nil, fmt.Errorf("get_trace_summaries: %w", err)
		}
		env := tracePage(table, rows, tr, limit, in.Offset)
		env.Findings = append(env.Findings, Finding{"section": "query_scope", "requested_window": map[string]string{"from": in.From, "to": in.To}, "count_basis": "Full stored summary windows; do not interpret counts as restricted to overlap.", "next_tool": "get_trace_spans(trace_id=exemplar, from=window_start, to=window_end)"})
		return env, nil
	}}
}
