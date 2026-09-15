package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"testing"
)

func TestSearchTargetsPagination(t *testing.T) {
	for _, tc := range []struct {
		name, args      string
		rows            [][]string
		returned, lower int
		more            bool
		next            int
	}{
		{"first", `{"limit":1}`, [][]string{{dgUUID, "pricing", "Pricing", "application", "10.0.0.1"}, {dgPeerUUID, "db", "DB", "database", "10.0.0.2"}}, 1, 2, true, 1},
		{"last", `{"limit":1,"offset":1}`, [][]string{{dgPeerUUID, "db", "DB", "database", "10.0.0.2"}}, 1, 2, false, 0},
		{"empty", `{}`, nil, 0, 0, false, 0},
		{"overshoot", `{"offset":900}`, nil, 0, 0, false, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := newDgFakePG(t)
			p.script("ORDER BY id LIMIT", []string{"id", "name", "display_name", "type", "address"}, tc.rows)
			db, err := sql.Open("pgx", p.dsn())
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			out, err := NewSearchTargetsTool(db).Call(context.Background(), json.RawMessage(tc.args))
			if err != nil {
				t.Fatal(err)
			}
			env := out.(Envelope)
			page := env.Findings[len(env.Findings)-1]
			if page["returned"] != tc.returned || page["matching_count_lower_bound"] != tc.lower || page["has_more"] != tc.more {
				t.Fatalf("page: %#v", page)
			}
			if _, exists := page["total"]; exists {
				t.Fatal("must not claim exact total")
			}
			if tc.more && page["next_offset"] != tc.next {
				t.Fatalf("next offset: %#v", page)
			}
			if !tc.more && page["next_offset"] != nil {
				t.Fatal("unexpected next page")
			}
			if len(env.Refs) != tc.returned || len(env.Scopes) != 0 {
				t.Fatalf("refs/scopes: %#v", env)
			}
			if tc.returned > 0 && env.Findings[0]["target_id"] != tc.rows[0][0] {
				t.Fatal("lost UUID")
			}
		})
	}
}

func TestSearchTargetsLiteralBoundQuery(t *testing.T) {
	p := newDgFakePG(t)
	// pgx simple protocol renders the separately bound parameter. This script
	// only succeeds if quote escaping preserves the value as one SQL literal.
	p.script("lower('price%_'' OR true --'::text)", []string{"id", "name", "display_name", "type", "address"}, nil)
	db, err := sql.Open("pgx", p.dsn())
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	_, err = NewSearchTargetsTool(db).Call(context.Background(), json.RawMessage(`{"query":"price%_' OR true --","type":"application"}`))
	if err != nil {
		t.Fatal(err)
	}
}

func TestSearchTargetsRejectInvalidBounds(t *testing.T) {
	for _, args := range []string{`{"limit":0}`, `{"limit":101}`, `{"offset":-1}`, `{"offset":1000001}`, `{"limit":"1"}`} {
		if _, err := NewSearchTargetsTool(nil).Call(context.Background(), json.RawMessage(args)); err == nil {
			t.Fatalf("accepted %s", args)
		}
	}
}
