// Package mcpserver is the rca-mcp JSON-RPC handler shared by the stdio binary (cmd/rca-mcp) and
// in-process embedders (the lucida-next Query service runs the same tools behind its own lease checks).
//
// Embedders must reproduce what the trained investigator saw byte for byte: tools/list order, the blind
// filter, the MCP text block and the error envelopes. Keeping one implementation here — the binary is only
// a stdin/stdout loop around Handle — is what makes that a property of the code rather than of a copy.
package mcpserver

import (
	"context"
	"encoding/json"
	"fmt"
	"log"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/internal/blind"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// ProtocolVersion is the MCP revision the server reports on initialize.
const ProtocolVersion = "2024-11-05"

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

// Server answers MCP requests for one ordered toolset.
type Server struct {
	ordered []llm.Tool
	by      map[string]llm.Tool
	blind   bool
}

// New binds a server to the registry-ordered toolset. blind filters experiment labels from every
// model-visible response (the -blind flag of the binary).
func New(ordered []llm.Tool, blind bool) *Server {
	by := make(map[string]llm.Tool, len(ordered))
	for _, t := range ordered {
		by[t.Name] = t
	}
	return &Server{ordered: ordered, by: by, blind: blind}
}

// Handle answers one JSON-RPC message (one line of the stdio protocol, without the newline). ok is false
// when the message gets no reply: undecodable input and notifications (no id) are ignored, as on stdio.
func (s *Server) Handle(ctx context.Context, line []byte) (out []byte, ok bool) {
	var q rpc
	if json.Unmarshal(line, &q) != nil || q.ID == nil {
		return nil, false
	}
	res, e := s.dispatch(ctx, q)
	response := reply{JSONRPC: "2.0", ID: q.ID, Result: res}
	if e != nil {
		response = reply{JSONRPC: "2.0", ID: q.ID, Error: map[string]any{"code": -32000, "message": e.Error()}}
	}
	// Also filter list schemas and protocol/tool errors. Nested content text
	// was filtered in dispatch before its JSON was encoded as an MCP text block.
	b, err := ModelJSON(response, s.blind)
	if err != nil {
		b, _ = json.Marshal(reply{JSONRPC: "2.0", ID: q.ID, Error: map[string]any{"code": -32603, "message": "response unavailable"}})
	}
	return b, true
}

func (s *Server) dispatch(ctx context.Context, q rpc) (any, error) {
	switch q.Method {
	case "initialize":
		return map[string]any{"protocolVersion": ProtocolVersion, "capabilities": map[string]any{"tools": map[string]any{}}, "serverInfo": map[string]string{"name": "rca-tools", "version": "1"}}, nil
	case "tools/list":
		return map[string]any{"tools": ToolList(s.ordered)}, nil
	case "tools/call":
		return s.call(ctx, q.Params)
	default:
		return map[string]any{}, nil
	}
}

func (s *Server) call(ctx context.Context, params json.RawMessage) (any, error) {
	var p struct {
		Name      string          `json:"name"`
		Arguments json.RawMessage `json:"arguments"`
	}
	_ = json.Unmarshal(params, &p)
	t, ok := s.by[p.Name]
	if !ok {
		return nil, fmt.Errorf("unknown tool %s", p.Name)
	}
	out, err := t.Call(ctx, p.Arguments)
	if err != nil {
		return nil, err
	}
	b, err := ModelJSON(out, s.blind)
	if err != nil {
		return nil, err
	}
	return map[string]any{"content": []map[string]string{{"type": "text", "text": string(b)}}}, nil
}

// ModelJSON encodes a model-visible value, through the blind filter when sanitize is set.
func ModelJSON(value any, sanitize bool) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil || !sanitize {
		return raw, err
	}
	clean, stats, err := blind.SanitizeJSON(raw)
	if err == nil && (stats.RemovedFields > 0 || stats.RedactedStrings > 0 || stats.OpaqueIdentifiers > 0) {
		// Diagnostics go to stderr only. Annotating the model-visible envelope ("blind_filter" +
		// "experiment annotations withheld") told the model exactly which response touched the
		// injected change (2026-10-06 leak review).
		log.Printf("blind_filter removed_fields=%d redacted_strings=%d pseudonymized=%d", stats.RemovedFields, stats.RedactedStrings, stats.OpaqueIdentifiers)
	}
	return clean, err
}

// SanitizeJSON filters one public JSON document (the observed-alarm seed) the way -sanitize-stdin does.
func SanitizeJSON(raw []byte) ([]byte, error) {
	clean, _, err := blind.SanitizeJSON(raw)
	return clean, err
}

// ToolList renders the tools/list payload in the given (registry) order. The order is part of the
// student's prompt, so it must be deterministic and identical to the exported training catalog.
func ToolList(ordered []llm.Tool) []map[string]any {
	ts := make([]map[string]any, 0, len(ordered))
	for _, t := range ordered {
		var schema any
		_ = json.Unmarshal(t.Parameters, &schema)
		ts = append(ts, map[string]any{"name": t.Name, "description": t.Description, "inputSchema": schema})
	}
	return ts
}
