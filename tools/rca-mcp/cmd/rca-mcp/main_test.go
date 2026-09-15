package main

import (
	"testing"
	"time"
)

func TestParseEventWindow(t *testing.T) {
	for _, tc := range []struct {
		name, first, last string
		wantErr           bool
	}{
		{name: "live defaults"},
		{name: "incident", first: "2026-09-01T01:00:00Z", last: "2026-09-01T02:00:00Z"},
		{name: "offsets", first: "2026-09-01T10:00:00+09:00", last: "2026-09-01T02:00:00Z"},
		{name: "fractional", first: "2026-09-01T01:00:00.123Z", last: "2026-09-01T01:00:00.456Z"},
		{name: "missing end", first: "2026-09-01T01:00:00Z", wantErr: true},
		{name: "missing start", last: "2026-09-01T01:00:00Z", wantErr: true},
		{name: "bad start", first: "yesterday", last: "2026-09-01T01:00:00Z", wantErr: true},
		{name: "bad end", first: "2026-09-01T01:00:00Z", last: "tomorrow", wantErr: true},
		{name: "equal", first: "2026-09-01T01:00:00Z", last: "2026-09-01T01:00:00Z", wantErr: true},
		{name: "reversed", first: "2026-09-01T02:00:00Z", last: "2026-09-01T01:00:00Z", wantErr: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			from, to, err := parseEventWindow(tc.first, tc.last)
			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr = %v", err, tc.wantErr)
			}
			if tc.wantErr {
				return
			}
			if tc.first == "" {
				if !from.IsZero() || !to.IsZero() {
					t.Fatal("omitted incident window must retain live defaults")
				}
				return
			}
			wantFrom, _ := time.Parse(time.RFC3339, tc.first)
			wantTo, _ := time.Parse(time.RFC3339, tc.last)
			if !from.Equal(wantFrom) || !to.Equal(wantTo) {
				t.Fatalf("window = %v..%v; want %v..%v", from, to, wantFrom, wantTo)
			}
		})
	}
}
