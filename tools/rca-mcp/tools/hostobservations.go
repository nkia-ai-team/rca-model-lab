package tools

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

type hostObservationInput struct {
	Kind, Target, Host, App, Query, Remote, Process, State, Direction, From, To string
	Limit                                                                       int  `json:"limit"`
	Offset                                                                      int  `json:"offset"`
	LocalPort                                                                   *int `json:"local_port"`
	RemotePort                                                                  *int `json:"remote_port"`
	SeverityMax                                                                 *int `json:"severity_max"`
}

// NewHostObservationsTool searches observations without assuming syslog has a
// target UUID. Offset paging is stable for a fixed dataset; identical rows are
// indistinguishable observations and deliberately share a content reference.
func NewHostObservationsTool(ch *CH) llm.Tool {
	properties := map[string]any{
		"kind":         map[string]any{"type": "string", "enum": []string{"syslog", "connections"}},
		"limit":        map[string]any{"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
		"offset":       map[string]any{"type": "integer", "minimum": 0, "maximum": 1000000, "default": 0, "description": "Use pagination.next_offset with unchanged filters on a fixed dataset. Beyond the bound, narrow the time window."},
		"severity_max": map[string]any{"type": "integer", "minimum": 0, "maximum": 7, "description": "syslog only: include severity <= this RFC level (0 emergency, 1 alert, 2 critical, 3 error, 4 warning, 5 notice, 6 info, 7 debug)."},
		"direction":    map[string]any{"type": "string", "enum": []string{"in", "out"}, "description": "connections only; exact recorded direction."},
	}
	for name, desc := range map[string]string{
		"target": "connections only: exact target UUID.", "host": "Exact hostname (syslog) or host_name (connections).", "app": "syslog only: exact app_name.",
		"query":  "Case-insensitive literal substring: syslog message/raw; connections local/remote addresses and decimal ports, process, state, direction and host.",
		"remote": "connections only: exact remote address.", "process": "connections only: exact process.", "state": "connections only: exact socket state.",
		"from": "Inclusive RFC3339 timestamp.", "to": "Exclusive RFC3339 timestamp.",
	} {
		properties[name] = map[string]any{"type": "string", "description": desc}
	}
	for _, name := range []string{"local_port", "remote_port"} {
		properties[name] = map[string]any{"type": "integer", "minimum": 0, "maximum": 65535, "description": "connections only: exact port."}
	}
	params, _ := json.Marshal(map[string]any{"type": "object", "properties": properties, "required": []string{"kind", "from", "to"}, "additionalProperties": false})
	return llm.Tool{Name: "search_host_data", Description: "Search syslog and host socket observations. Returns raw fields and content refs, plus pagination with a matching-count lower bound (not an exact total). Unsupported mode filters are rejected.", Parameters: params, Call: func(ctx context.Context, args json.RawMessage) (any, error) {
		in := hostObservationInput{Limit: 50}
		dec := json.NewDecoder(bytes.NewReader(args))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&in); err != nil {
			return nil, fmt.Errorf("invalid arguments: %w", err)
		}
		if err := dec.Decode(new(any)); err != io.EOF {
			return nil, fmt.Errorf("expected one JSON object")
		}
		var keys map[string]json.RawMessage
		if err := json.Unmarshal(args, &keys); err != nil || keys == nil {
			return nil, fmt.Errorf("expected JSON object")
		}
		for k, value := range keys {
			if _, ok := properties[k]; !ok || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
				return nil, fmt.Errorf("unsupported or null argument %q", k)
			}
		}
		if in.Kind != "syslog" && in.Kind != "connections" {
			return nil, fmt.Errorf("kind must be syslog or connections")
		}
		for _, k := range []string{"target", "remote", "process", "state", "direction", "local_port", "remote_port"} {
			if _, ok := keys[k]; ok && in.Kind == "syslog" {
				return nil, fmt.Errorf("%s only applies to connections", k)
			}
		}
		for _, k := range []string{"app", "severity_max"} {
			if _, ok := keys[k]; ok && in.Kind == "connections" {
				return nil, fmt.Errorf("%s only applies to syslog", k)
			}
		}
		if in.Limit < 1 || in.Limit > 200 || in.Offset < 0 || in.Offset > 1000000 {
			return nil, fmt.Errorf("limit must be 1..200 and offset 0..1000000")
		}
		for _, port := range []*int{in.LocalPort, in.RemotePort} {
			if port != nil && (*port < 0 || *port > 65535) {
				return nil, fmt.Errorf("port must be 0..65535")
			}
		}
		if in.SeverityMax != nil && (*in.SeverityMax < 0 || *in.SeverityMax > 7) {
			return nil, fmt.Errorf("severity_max must be 0..7")
		}
		if _, ok := keys["direction"]; ok && in.Direction != "in" && in.Direction != "out" {
			return nil, fmt.Errorf("direction must be in or out")
		}
		from, e1 := time.Parse(time.RFC3339, in.From)
		to, e2 := time.Parse(time.RFC3339, in.To)
		if e1 != nil || e2 != nil || !from.Before(to) {
			return nil, fmt.Errorf("from and to must be RFC3339 with from < to")
		}
		return queryHostObservations(ctx, ch, in, from, to)
	}}
}

func queryHostObservations(ctx context.Context, ch *CH, in hostObservationInput, from, to time.Time) (any, error) {
	params := map[string]string{"from": chTime(from), "to": chTime(to), "target": in.Target, "host": in.Host, "app": in.App, "query": in.Query, "remote": in.Remote, "process": in.Process, "state": in.State, "direction": in.Direction, "limit": strconv.Itoa(in.Limit + 1), "offset": strconv.Itoa(in.Offset)}
	table := "host_connections"
	q := `SELECT timestamp AS observed_at, target_id, host_name, local_addr, local_port, remote_addr, remote_port, direction, process, state FROM host_connections WHERE timestamp >= parseDateTime64BestEffort({from:String},9) AND timestamp < parseDateTime64BestEffort({to:String},9) AND ({target:String}='' OR target_id={target:String}) AND ({host:String}='' OR host_name={host:String}) AND ({remote:String}='' OR remote_addr={remote:String}) AND ({process:String}='' OR process={process:String}) AND ({state:String}='' OR state={state:String}) AND ({direction:String}='' OR direction={direction:String}) AND ({query:String}='' OR arrayExists(x -> positionCaseInsensitive(x,{query:String})>0, [local_addr,toString(local_port),remote_addr,toString(remote_port),process,state,direction,host_name]))`
	order := `timestamp DESC, target_id, host_name, local_addr, local_port, remote_addr, remote_port, direction, process, state`
	if in.Kind == "syslog" {
		table = "syslog_local"
		q = `SELECT received_at AS observed_at, receiver_target_id, collector_id, device_target_id, registered, match_domain, source_ip, facility, severity, severity_text, hostname, app_name, proc_id, msg_id, structured_data, message, raw, encoding, listen_port, listen_proto FROM syslog_local WHERE received_at >= parseDateTime64BestEffort({from:String},9) AND received_at < parseDateTime64BestEffort({to:String},9) AND ({host:String}='' OR hostname={host:String}) AND ({app:String}='' OR app_name={app:String}) AND ({query:String}='' OR positionCaseInsensitive(message,{query:String})>0 OR positionCaseInsensitive(raw,{query:String})>0)`
		order = `received_at DESC, receiver_target_id, collector_id, device_target_id, registered, match_domain, source_ip, facility, severity, severity_text, hostname, app_name, proc_id, msg_id, structured_data, message, raw, encoding, listen_port, listen_proto`
		if in.SeverityMax != nil {
			q += ` AND severity <= {severity_max:UInt8}`
			params["severity_max"] = strconv.Itoa(*in.SeverityMax)
		}
	} else {
		if in.LocalPort != nil {
			q += ` AND local_port={local_port:UInt16}`
			params["local_port"] = strconv.Itoa(*in.LocalPort)
		}
		if in.RemotePort != nil {
			q += ` AND remote_port={remote_port:UInt16}`
			params["remote_port"] = strconv.Itoa(*in.RemotePort)
		}
	}
	q += ` ORDER BY ` + order + ` LIMIT {limit:UInt32} OFFSET {offset:UInt32}`
	rows, tr, err := ch.Query(ctx, q, params)
	if err != nil {
		return nil, fmt.Errorf("search_host_data query: %w", err)
	}
	seen := len(rows)
	more := seen > in.Limit
	if more {
		rows = rows[:in.Limit]
	}
	findings := make([]Finding, 0, len(rows)+1)
	refs := make([]string, 0, len(rows))
	for _, r := range rows {
		encoded, err := json.Marshal(r)
		if err != nil {
			return nil, fmt.Errorf("encode host observation: %w", err)
		}
		ref := fmt.Sprintf("ch:%s:%s/sha256:%x", table, chStr(r["observed_at"]), sha256.Sum256(encoded))
		refs = append(refs, ref)
		r["refs"] = []string{ref}
		findings = append(findings, Finding(r))
	}
	lower := 0
	if seen > 0 {
		lower = in.Offset + seen
	}
	page := Finding{"kind": "pagination", "offset": in.Offset, "limit": in.Limit, "returned": len(rows), "matching_count_lower_bound": lower, "has_more": more || tr != nil, "dataset_must_be_fixed": true}
	if (more || tr != nil) && len(rows) > 0 && in.Offset+len(rows) <= 1000000 {
		page["next_offset"] = in.Offset + len(rows)
	}
	if (more || tr != nil) && (len(rows) == 0 || in.Offset+len(rows) > 1000000) {
		page["next_action"] = "narrow the time window or filters"
	}
	findings = append(findings, page)
	status, reason := "normal", ""
	summary := fmt.Sprintf("%s: returned %d observations", in.Kind, len(rows))
	if len(rows) == 0 && tr == nil {
		status, reason = "no_data", NoDataZeroObservations
		summary = "No observations on this page; a nonzero offset does not prove an empty search."
	}
	return Envelope{Status: status, NoDataReason: reason, Summary: summary, AssessmentBasis: "Explicit time window and mode-specific filters; pagination counts are lower bounds only.", Findings: findings, Refs: refs, ObservedRange: &TimeRange{From: from.UTC(), To: to.UTC()}, Truncated: more, QueryTruncated: more || tr != nil}, nil
}
