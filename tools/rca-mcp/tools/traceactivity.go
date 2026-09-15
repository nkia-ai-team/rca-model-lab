package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
	"sort"
	"time"
)

type traceActivityInput struct {
	Target, From, To, Topic string
	ConsumerGroup           string `json:"consumer_group"`
	BucketSeconds           int    `json:"bucket_seconds"`
	Limit                   *int
	Offset                  int
}

func NewTraceActivityTool(ch *CH) llm.Tool {
	params, _ := json.Marshal(map[string]any{"type": "object", "additionalProperties": false, "properties": map[string]any{
		"target": map[string]string{"type": "string", "description": "Exact service name or target UUID"},
		"from":   map[string]string{"type": "string", "description": "RFC3339 current-window start"}, "to": map[string]string{"type": "string", "description": "RFC3339 current-window end; max 24 hours"},
		"topic": map[string]string{"type": "string", "description": "Optional exact messaging.destination.name"}, "consumer_group": map[string]string{"type": "string", "description": "Optional exact messaging.kafka.consumer.group"},
		"bucket_seconds": map[string]any{"type": "integer", "minimum": 1, "maximum": 3600, "description": "At most 240 buckets/window. Default chooses 60s or larger."},
		"limit":          map[string]any{"type": "integer", "minimum": 1, "maximum": 50}, "offset": map[string]any{"type": "integer", "minimum": 0, "maximum": 1000000},
	}, "required": []string{"target", "from", "to"}})
	return llm.Tool{Name: "get_trace_activity", Description: "Compare time-bucket span counts to the immediately preceding equal-length window, retaining baseline-only disappeared operations and empty buckets. Grouped by service/kind/operation/topic/consumer group. Counts measure captured spans, not broker backlog or guaranteed delivery.", Parameters: params, Call: func(ctx context.Context, raw json.RawMessage) (any, error) {
		var in traceActivityInput
		if err := decodeTraceArgs(raw, &in); err != nil {
			return nil, err
		}
		from, e1 := time.Parse(time.RFC3339, in.From)
		to, e2 := time.Parse(time.RFC3339, in.To)
		if in.Target == "" || e1 != nil || e2 != nil || !from.Before(to) || to.Sub(from) > 24*time.Hour {
			return nil, fmt.Errorf("target and RFC3339 window of 0..24h required")
		}
		seconds := int(to.Sub(from).Seconds())
		if seconds < 1 {
			return nil, fmt.Errorf("window must be at least 1 second")
		}
		bucket := in.BucketSeconds
		if bucket == 0 {
			bucket = max(60, (seconds+239)/240)
		}
		if bucket < 1 || bucket > 3600 || int(to.Sub(from).Seconds()/float64(bucket))+boolInt(to.Sub(from)%(time.Duration(bucket)*time.Second) != 0) > 240 {
			return nil, fmt.Errorf("bucket_seconds must yield at most 240 buckets")
		}
		limit := 20
		if in.Limit != nil {
			limit = *in.Limit
		}
		if limit < 1 || limit > 50 || in.Offset < 0 || in.Offset > 1000000 {
			return nil, fmt.Errorf("invalid page bounds")
		}
		baselineFrom := from.Add(-to.Sub(from))
		rows, tr, err := ch.Query(ctx, `SELECT service_name, span_kind, span_name,
span_attributes['messaging.destination.name'] AS topic,
span_attributes['messaging.kafka.consumer.group'] AS consumer_group,
if(timestamp < parseDateTime64BestEffort({from:String},9), 'baseline','current') AS phase,
intDiv(dateDiff('millisecond', if(timestamp < parseDateTime64BestEffort({from:String},9), parseDateTime64BestEffort({baseline:String},9), parseDateTime64BestEffort({from:String},9)), timestamp), {bucket_ms:UInt64}) AS bucket,
count() AS raw_count, uniqExact(tuple(trace_id,span_id)) AS unique_count,
argMax(toString(trace_id),duration_ns) AS exemplar_trace_id,
min(timestamp) AS first_seen, max(timestamp) AS last_seen
FROM otel_traces_local WHERE timestamp >= parseDateTime64BestEffort({baseline:String},9) AND timestamp < parseDateTime64BestEffort({to:String},9)
AND (service_name={target:String} OR resource_attributes['lucida.target_id']={target:String})
AND ({topic:String}='' OR span_attributes['messaging.destination.name']={topic:String})
AND ({group:String}='' OR span_attributes['messaging.kafka.consumer.group']={group:String})
GROUP BY service_name,span_kind,span_name,topic,consumer_group,phase,bucket
ORDER BY service_name,span_kind,span_name,topic,consumer_group,phase,bucket`, map[string]string{"target": in.Target, "from": chTime(from), "to": chTime(to), "baseline": chTime(baselineFrom), "bucket_ms": fmt.Sprint(bucket * 1000), "topic": in.Topic, "group": in.ConsumerGroup})
		if err != nil {
			return nil, fmt.Errorf("get_trace_activity: %w", err)
		}
		return traceActivityEnvelope(rows, tr, in, from, to, baselineFrom, bucket, limit), nil
	}}
}

type activityGroup struct {
	Identity          map[string]any
	Current, Baseline []int64
	RawCount          int64
	Examples          map[string]bool
}

func traceActivityEnvelope(rows []map[string]any, tr *CHTrunc, in traceActivityInput, from, to, baselineFrom time.Time, bucket, limit int) Envelope {
	n := int((to.Sub(from) + time.Duration(bucket)*time.Second - 1) / (time.Duration(bucket) * time.Second))
	groups := map[string]*activityGroup{}
	for _, r := range rows {
		identity := map[string]any{}
		for _, k := range []string{"service_name", "span_kind", "span_name", "topic", "consumer_group"} {
			identity[k] = r[k]
		}
		encoded, _ := json.Marshal(identity)
		key := string(encoded)
		g := groups[key]
		if g == nil {
			g = &activityGroup{Identity: identity, Current: make([]int64, n), Baseline: make([]int64, n), Examples: map[string]bool{}}
			groups[key] = g
		}
		idx := int(chInt(r["bucket"]))
		if idx < 0 || idx >= n {
			continue
		}
		if chStr(r["phase"]) == "baseline" {
			g.Baseline[idx] += chInt(r["unique_count"])
		} else {
			g.Current[idx] += chInt(r["unique_count"])
		}
		g.RawCount += chInt(r["raw_count"])
		if id := chStr(r["exemplar_trace_id"]); id != "" {
			g.Examples[id] = true
		}
	}
	keys := make([]string, 0, len(groups))
	for k := range groups {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	start := min(in.Offset, len(keys))
	end := min(start+limit, len(keys))
	fs := []Finding{}
	refs := []string{}
	for _, key := range keys[start:end] {
		g := groups[key]
		f := Finding(g.Identity)
		cur, base := int64(0), int64(0)
		for _, v := range g.Current {
			cur += v
		}
		for _, v := range g.Baseline {
			base += v
		}
		state := "observed_in_both"
		if base == 0 {
			state = "not_seen_in_baseline"
		}
		if cur == 0 {
			state = "not_seen_in_current"
		}
		if tr != nil {
			state = "incomplete_query"
		}
		f["activity_state"] = state
		f["current_counts"] = g.Current
		f["baseline_counts"] = g.Baseline
		f["current_total"] = cur
		f["baseline_total"] = base
		f["raw_rows_both_windows"] = g.RawCount
		f["bucket_seconds"] = bucket
		f["current_from"] = from.Format(time.RFC3339Nano)
		f["baseline_from"] = baselineFrom.Format(time.RFC3339Nano)
		f["to"] = to.Format(time.RFC3339Nano)
		examples := []string{}
		for id := range g.Examples {
			examples = append(examples, id)
		}
		sort.Strings(examples)
		f["exemplar_trace_ids"] = examples
		ref := traceRecordRef("otel_traces_local:activity", map[string]any(f))
		f["refs"] = []string{ref}
		refs = append(refs, ref)
		fs = append(fs, f)
	}
	page := Finding{"section": "pagination", "offset": in.Offset, "returned": end - start, "has_more": end < len(keys), "groups_observed": len(keys)}
	if tr == nil && end < len(keys) && end <= 1000000 {
		page["next_offset"] = end
	}
	if tr != nil {
		page["next_action"] = "Narrow time/topic/group: incomplete backend query"
	}
	fs = append(fs, page, Finding{"section": "coverage", "current_window": map[string]string{"from": from.Format(time.RFC3339Nano), "to": to.Format(time.RFC3339Nano)}, "baseline_window": map[string]string{"from": baselineFrom.Format(time.RFC3339Nano), "to": from.Format(time.RFC3339Nano)}, "buckets_per_window": n, "last_bucket_may_be_partial": to.Sub(from)%(time.Duration(bucket)*time.Second) != 0, "limits": "Zero counts mean no recorded spans for that group/bucket, not proof of service outage or complete collection. Producer/consumer counts cannot prove backlog, loss or end-to-end delivery (sampling, retries, fan-out, prior backlog)."})
	env := Envelope{Status: "normal", Summary: fmt.Sprintf("Trace activity: %d groups returned from %d observed groups. Compare bucket arrays for gaps and recovery.", end-start, len(keys)), Findings: fs, Refs: refs, Truncated: end < len(keys), QueryTruncated: tr != nil || in.Offset > 0, AssessmentBasis: "Current and baseline union of operation groups; recorded-span counts without health inference."}
	if len(keys) == 0 {
		env.Status = "no_data"
		env.NoDataReason = NoDataUnknown
	}
	return env
}
