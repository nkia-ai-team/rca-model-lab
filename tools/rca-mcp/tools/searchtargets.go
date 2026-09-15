package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// NewSearchTargetsTool discovers UUIDs without requiring an incident briefing.
// The inventory describes the connected database snapshot, not historical liveness.
func NewSearchTargetsTool(pg *sql.DB) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"query":  map[string]any{"type": "string", "description": "Literal case-insensitive substring of name, display name, UUID or address; omit to list targets. %, _ are literal characters."},
			"type":   map[string]any{"type": "string", "description": "Exact target type, e.g. server, application, database, kubernetes; omit for all types."},
			"limit":  map[string]any{"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
			"offset": map[string]any{"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0},
		},
	})
	return llm.Tool{Name: "search_targets", Description: "Discover target UUIDs by name, type or address before calling describe_target or target-scoped RCA tools. Lists the connected inventory snapshot; existence does not establish health or incident involvement. Follow next_offset for more matches.", Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Query  string `json:"query"`
				Type   string `json:"type"`
				Limit  *int   `json:"limit"`
				Offset int    `json:"offset"`
			}
			if err := json.Unmarshal(args, &in); err != nil {
				return nil, fmt.Errorf("search_targets arguments: %w", err)
			}
			limit := 50
			if in.Limit != nil {
				limit = *in.Limit
			}
			if limit < 1 || limit > 100 || in.Offset < 0 || in.Offset > 1000000 {
				return nil, fmt.Errorf("search_targets requires limit 1..100 and offset 0..1000000")
			}
			rows, err := pg.QueryContext(ctx, `SELECT id::text, name, coalesce(display_name,''), type::text, coalesce(address,'')
				FROM targets
				WHERE ($1::text = '' OR position(lower($1::text) in lower(name)) > 0
				 OR position(lower($1::text) in lower(coalesce(display_name,''))) > 0
				 OR position(lower($1::text) in lower(id::text)) > 0
				 OR position(lower($1::text) in lower(coalesce(address,''))) > 0)
				AND ($2::text = '' OR type::text = $2::text)
				ORDER BY id LIMIT $3 OFFSET $4`, in.Query, in.Type, limit+1, in.Offset)
			if err != nil {
				return nil, fmt.Errorf("search_targets: %w", pgErr(err))
			}
			defer rows.Close()
			findings := make([]Finding, 0, limit+1)
			refs := make([]string, 0, limit)
			for rows.Next() {
				var id, name, display, typ, address string
				if err := rows.Scan(&id, &name, &display, &typ, &address); err != nil {
					return nil, fmt.Errorf("search_targets scan: %w", pgErr(err))
				}
				findings = append(findings, Finding{"section": "target", "target_id": id, "name": name, "display_name": display, "type": typ, "address": address, "refs": []string{"pg:targets:" + id}})
			}
			if err := rows.Err(); err != nil {
				return nil, fmt.Errorf("search_targets rows: %w", pgErr(err))
			}
			hasMore := len(findings) > limit
			// A nonempty offset page proves at least offset+fetched matches. An
			// empty offset page proves no global count (offset may overshoot).
			lowerBound := 0
			if len(findings) > 0 {
				lowerBound = in.Offset + len(findings)
			}
			if hasMore {
				findings = findings[:limit]
			}
			for _, f := range findings {
				refs = append(refs, f["refs"].([]string)...)
			}
			page := Finding{"section": "pagination", "query": in.Query, "type": in.Type, "offset": in.Offset, "limit": limit, "returned": len(findings), "has_more": hasMore, "matching_count_lower_bound": lowerBound, "inventory_scope": "connected_database_snapshot"}
			if hasMore && in.Offset+limit <= 1000000 {
				page["next_offset"] = in.Offset + limit
			}
			if hasMore && in.Offset+limit > 1000000 {
				page["next_action"] = "Narrow query or type; maximum offset reached."
			}
			returned := len(findings)
			findings = append(findings, page)
			return Envelope{Status: "normal", Summary: fmt.Sprintf("%d matching inventory targets returned; has_more=%t. Use target_id with describe_target.", returned, hasMore), Findings: findings, Refs: refs, QueryTruncated: hasMore || in.Offset > 0, AssessmentBasis: "Literal substring search and exact type filter on connected inventory; no health, historical presence, or exact global total is inferred."}, nil
		}}
}
