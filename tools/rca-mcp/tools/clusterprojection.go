package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// NewClusterProjectionTool exposes the captured event-to-cluster projection
// used to investigate rollout and assignment changes.
func NewClusterProjectionTool(ch *CH) llm.Tool {
	params, _ := json.Marshal(map[string]any{"type": "object", "properties": map[string]any{
		"cluster": map[string]string{"type": "string"}, "from": map[string]string{"type": "string"}, "to": map[string]string{"type": "string"}, "limit": map[string]string{"type": "integer"},
	}, "required": []string{"from", "to"}})
	return llm.Tool{Name: "get_cluster_projections", Description: "캡처된 event_cluster_projection_local에서 이벤트·클러스터·desired version 할당을 조회한다.", Parameters: params, Call: func(ctx context.Context, args json.RawMessage) (any, error) {
		var in struct {
			Cluster, From, To string
			Limit             int `json:"limit"`
		}
		if err := json.Unmarshal(args, &in); err != nil {
			return nil, fmt.Errorf("인자 오류: %w", err)
		}
		from, e1 := time.Parse(time.RFC3339, in.From)
		to, e2 := time.Parse(time.RFC3339, in.To)
		if e1 != nil || e2 != nil || !from.Before(to) {
			return nil, fmt.Errorf("from·to는 UTC RFC3339, from < to 필요")
		}
		if in.Limit <= 0 {
			in.Limit = 50
		}
		if in.Limit > 200 {
			in.Limit = 200
		}
		return queryClusterProjections(ctx, ch, in.Cluster, from, to, in.Limit)
	}}
}

func queryClusterProjections(ctx context.Context, ch *CH, cluster string, from, to time.Time, limit int) (any, error) {
	q := fmt.Sprintf(`SELECT event_id, cluster_id, desired_version, assigned_at FROM event_cluster_projection_local WHERE assigned_at >= parseDateTime64BestEffort({from:String},9) AND assigned_at < parseDateTime64BestEffort({to:String},9) AND ({cluster:String}='' OR cluster_id={cluster:String}) ORDER BY assigned_at DESC LIMIT %d`, limit+1)
	rows, tr, err := ch.Query(ctx, q, map[string]string{"from": chTime(from), "to": chTime(to), "cluster": cluster})
	if err != nil {
		return nil, fmt.Errorf("get_cluster_projections 조회: %w", err)
	}
	truncated := len(rows) > limit
	if truncated {
		rows = rows[:limit]
	}
	findings := make([]Finding, 0, len(rows))
	refs := make([]string, 0, len(rows))
	for _, r := range rows {
		ref := fmt.Sprintf("ch:event_cluster_projection_local:%s", chStr(r["event_id"]))
		refs = append(refs, ref)
		findings = append(findings, Finding{"event_id": chStr(r["event_id"]), "cluster_id": chStr(r["cluster_id"]), "desired_version": r["desired_version"], "assigned_at": chStr(r["assigned_at"]), "refs": []string{ref}})
	}
	status, reason := "normal", ""
	summary := fmt.Sprintf("cluster projection %d건 반환", len(findings))
	if len(findings) == 0 {
		status, reason, summary = "no_data", NoDataZeroObservations, "시간창과 cluster 조건에 맞는 projection이 없다."
	}
	env := Envelope{Status: status, NoDataReason: reason, Summary: summary, AssessmentBasis: "assigned_at 시간창·cluster 필터", Findings: findings, Refs: refs, ObservedRange: &TimeRange{From: from.UTC(), To: to.UTC()}, Truncated: truncated, Scopes: []QueryScope{qscope("cluster_projection", len(rows)+boolInt(truncated), len(rows))}}
	if tr != nil {
		env.QueryTruncated = true
	}
	return env, nil
}
