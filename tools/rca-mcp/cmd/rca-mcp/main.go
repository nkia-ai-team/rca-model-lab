// rca-mcp exposes the read-only RCA Toolset through the MCP stdio protocol.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/internal/blind"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/tools"
)

type rpc struct {
	JSONRPC string          `json:"jsonrpc"`
	ID      any             `json:"id,omitempty"`
	Method  string          `json:"method"`
	Params  json.RawMessage `json:"params,omitempty"`
}
type reply struct {
	JSONRPC string `json:"jsonrpc"`
	ID      any    `json:"id,omitempty"`
	Result  any    `json:"result,omitempty"`
	Error   any    `json:"error,omitempty"`
}

func main() {
	first := flag.String("first-event", "", "incident start (RFC3339; requires -last-event)")
	last := flag.String("last-event", "", "incident end (RFC3339; requires -first-event)")
	blindMode := flag.Bool("blind", false, "filter explicit experiment labels from model-visible responses")
	sanitizeStdin := flag.Bool("sanitize-stdin", false, "filter one JSON document from stdin without backend access")
	flag.Parse()
	if *sanitizeStdin {
		raw, err := io.ReadAll(io.LimitReader(os.Stdin, (16<<20)+1))
		if err != nil {
			fatal(err)
		}
		if len(raw) > 16<<20 {
			fatal(fmt.Errorf("public JSON exceeds input limit"))
		}
		clean, stats, err := blind.SanitizeJSON(raw)
		if err != nil {
			fatal(fmt.Errorf("invalid public seed JSON"))
		}
		_ = json.NewEncoder(os.Stderr).Encode(stats)
		fmt.Println(string(clean))
		return
	}
	firstEvent, lastEvent, err := parseEventWindow(*first, *last)
	if err != nil {
		fatal(err)
	}
	s, err := storesFromEnv()
	if err != nil {
		fatal(err)
	}
	defer s.PG.Close()
	by := map[string]llm.Tool{}
	for _, t := range tools.Toolset(s, firstEvent, lastEvent) {
		by[t.Name] = t
	}
	in := bufio.NewScanner(os.Stdin)
	enc := json.NewEncoder(os.Stdout)
	for in.Scan() {
		var q rpc
		if json.Unmarshal(in.Bytes(), &q) != nil || q.ID == nil {
			continue
		}
		var res any
		var e error
		switch q.Method {
		case "initialize":
			res = map[string]any{"protocolVersion": "2024-11-05", "capabilities": map[string]any{"tools": map[string]any{}}, "serverInfo": map[string]string{"name": "rca-tools", "version": "1"}}
		case "tools/list":
			var ts []map[string]any
			for _, t := range by {
				var schema any
				_ = json.Unmarshal(t.Parameters, &schema)
				ts = append(ts, map[string]any{"name": t.Name, "description": t.Description, "inputSchema": schema})
			}
			res = map[string]any{"tools": ts}
		case "tools/call":
			var p struct {
				Name      string          `json:"name"`
				Arguments json.RawMessage `json:"arguments"`
			}
			_ = json.Unmarshal(q.Params, &p)
			t, ok := by[p.Name]
			if !ok {
				e = fmt.Errorf("unknown tool %s", p.Name)
			} else {
				var out any
				out, e = t.Call(context.Background(), p.Arguments)
				if e == nil {
					var b []byte
					b, e = modelJSON(out, *blindMode)
					if e == nil {
						res = map[string]any{"content": []map[string]string{{"type": "text", "text": string(b)}}}
					}
				}
			}
		default:
			res = map[string]any{}
		}
		response := reply{JSONRPC: "2.0", ID: q.ID, Result: res}
		if e != nil {
			response = reply{JSONRPC: "2.0", ID: q.ID, Error: map[string]any{"code": -32000, "message": e.Error()}}
		}
		// Also filter list schemas and protocol/tool errors. Nested content text
		// was filtered above before its JSON was encoded as an MCP text block.
		b, err := modelJSON(response, *blindMode)
		if err != nil {
			_ = enc.Encode(reply{JSONRPC: "2.0", ID: q.ID, Error: map[string]any{"code": -32603, "message": "response unavailable"}})
			continue
		}
		fmt.Fprintln(os.Stdout, string(b))
	}
}

func modelJSON(value any, sanitize bool) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil || !sanitize {
		return raw, err
	}
	clean, stats, err := blind.SanitizeJSON(raw)
	if err == nil && (stats.RemovedFields > 0 || stats.RedactedStrings > 0 || stats.OpaqueIdentifiers > 0) {
		var envelope map[string]any
		if json.Unmarshal(clean, &envelope) == nil && envelope != nil && envelope["status"] != nil {
			envelope["blind_filter"] = stats
			envelope["blind_filter_note"] = "Explicit experiment annotations withheld; opaque references are display citations, not raw backend selectors. This does not establish full absence of leakage."
			clean, err = json.Marshal(envelope)
		}
	}
	return clean, err
}

// An omitted pair preserves live-mode defaults. A supplied pair binds tools
// that omit explicit query windows to the incident rather than wall-clock time.
func parseEventWindow(first, last string) (time.Time, time.Time, error) {
	if first == "" && last == "" {
		return time.Time{}, time.Time{}, nil
	}
	if first == "" || last == "" {
		return time.Time{}, time.Time{}, fmt.Errorf("-first-event and -last-event must be supplied together")
	}
	from, err := time.Parse(time.RFC3339, first)
	if err != nil {
		return time.Time{}, time.Time{}, fmt.Errorf("invalid -first-event: %w", err)
	}
	to, err := time.Parse(time.RFC3339, last)
	if err != nil {
		return time.Time{}, time.Time{}, fmt.Errorf("invalid -last-event: %w", err)
	}
	if !from.Before(to) {
		return time.Time{}, time.Time{}, fmt.Errorf("-first-event must precede -last-event")
	}
	return from, to, nil
}

func storesFromEnv() (tools.Stores, error) {
	dsn, chURL, vmURL := os.Getenv("RCA_PG_DSN"), os.Getenv("RCA_CH_URL"), os.Getenv("RCA_VM_URL")
	if dsn == "" || chURL == "" || vmURL == "" {
		return tools.Stores{}, fmt.Errorf("RCA_PG_DSN·RCA_CH_URL·RCA_VM_URL 전부 필요")
	}
	pg, e := tools.OpenPG(dsn)
	if e != nil {
		return tools.Stores{}, e
	}
	u, e := url.Parse(chURL)
	if e != nil {
		return tools.Stores{}, e
	}
	pass, _ := u.User.Password()
	return tools.Stores{PG: pg, CH: &tools.CH{BaseURL: u.Scheme + "://" + u.Host, User: u.User.Username(), Pass: pass, Database: "lucida"}, VM: &tools.VM{BaseURL: vmURL}}, nil
}
func fatal(e error) { fmt.Fprintln(os.Stderr, e); os.Exit(1) }
