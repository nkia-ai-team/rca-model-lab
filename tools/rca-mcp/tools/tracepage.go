package tools

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
)

func decodeTraceArgs(raw json.RawMessage, value any) error {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(value); err != nil {
		return err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return fmt.Errorf("expected one JSON object")
	}
	return nil
}

func traceRecordRef(source string, row map[string]any) string {
	b, _ := json.Marshal(row)
	return fmt.Sprintf("ch:%s:record:%x", source, sha256.Sum256(b))
}

// A page reports a proven lower bound, never a made-up exact total.
func tracePage(source string, rows []map[string]any, tr *CHTrunc, limit, offset int) Envelope {
	fetched := len(rows)
	hasMore := fetched > limit
	if hasMore {
		rows = rows[:limit]
	}
	fs := make([]Finding, 0, len(rows)+1)
	refs := make([]string, 0, len(rows))
	for _, row := range rows {
		ref := traceRecordRef(source, row)
		row["refs"] = []string{ref}
		fs = append(fs, Finding(row))
		refs = append(refs, ref)
	}
	lower := 0
	if fetched > 0 {
		lower = offset + fetched
	}
	page := Finding{"section": "pagination", "offset": offset, "returned": len(rows), "has_more": hasMore, "matching_count_lower_bound": lower}
	if hasMore && tr == nil && offset+len(rows) <= 1000000 {
		page["next_offset"] = offset + len(rows)
	}
	if tr != nil {
		page["next_action"] = "Backend response truncated; narrow the time window or filters before paging."
	}
	if hasMore && offset+len(rows) > 1000000 {
		page["next_action"] = "Offset limit reached; narrow the query."
	}
	fs = append(fs, page)
	env := Envelope{Status: "normal", Summary: fmt.Sprintf("%s: %d rows returned; existence does not establish health.", source, len(rows)), Findings: fs, Refs: refs, Truncated: hasMore, QueryTruncated: tr != nil || offset > 0, AssessmentBasis: "Stored observations; pagination counts are lower bounds. Empty results do not establish collection completeness."}
	if len(rows) == 0 {
		env.Status = "no_data"
		env.NoDataReason = NoDataUnknown
	}
	return env
}
