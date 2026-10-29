package mcpserver

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"testing"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

func TestBlindToolBoundary(t *testing.T) {
	payload := map[string]any{"status": "normal", "findings": []any{
		map[string]any{"name": "scenario-f03-h-order-thread-pool-4jcn9", "ref": "ch:kcm:scenario-f03-h-order-thread-pool-4jcn9"},
		map[string]any{"body": "nkia : USER=root ; COMMAND=/usr/bin/bash -s -- cleanup F05-P memhog 6250 480"},
		map[string]any{"body": "kernel OOMKilled process java; LOCK TABLE inventory IN EXCLUSIVE MODE; HTTP 429", "count": 5},
	}}
	clean, err := ModelJSON(payload, true)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"F05-P", "scenario-f03", "order-thread-pool", "memhog", "6250"} {
		if strings.Contains(string(clean), forbidden) {
			t.Fatalf("private annotation survived: %s", forbidden)
		}
	}
	for _, required := range []string{"OOMKilled", "LOCK TABLE", "HTTP 429"} {
		if !strings.Contains(string(clean), required) {
			t.Fatalf("missing observation: %s", required)
		}
	}
	// The model must not be told where filtering happened (that marks the injected change).
	for _, marker := range []string{"blind_filter", "withheld", "opaque", "experiment"} {
		if strings.Contains(string(clean), marker) {
			t.Fatalf("filter marker visible to the model: %s in %s", marker, clean)
		}
	}
	// A second pass through an MCP text block cannot undo filtering.
	response := reply{JSONRPC: "2.0", ID: 1, Result: map[string]any{"content": []any{map[string]any{"type": "text", "text": string(clean)}}}}
	wrapped, err := ModelJSON(response, true)
	if err != nil || !json.Valid(wrapped) {
		t.Fatal(err)
	}
	if strings.Contains(string(wrapped), "memhog") {
		t.Fatal("nested leak")
	}
	raw, _ := ModelJSON(payload, false)
	if !strings.Contains(string(raw), "memhog") {
		t.Fatal("non-blind observations changed")
	}
	if !strings.Contains(payload["findings"].([]any)[1].(map[string]any)["body"].(string), "memhog") {
		t.Fatal("original was mutated")
	}
}

func TestBlindErrorsAndMarshalFailure(t *testing.T) {
	raw, err := ModelJSON(reply{JSONRPC: "2.0", ID: 1, Error: map[string]any{"message": "COMMAND=bash cleanup F05-P memhog"}}, true)
	if err != nil || strings.Contains(string(raw), "memhog") {
		t.Fatalf("error boundary: %s %v", raw, err)
	}
	if _, err := ModelJSON(map[string]any{"n": math.NaN()}, true); err == nil {
		t.Fatal("invalid response should fail closed")
	}
}

func TestSanitizeJSON(t *testing.T) {
	clean, err := SanitizeJSON([]byte(`{"case_id":"case-f01-h","alarms":[{"service":"scenario-f03-h-x"}],"n":1.0}`))
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(clean), "case_id") || strings.Contains(string(clean), "scenario-f03") || !strings.Contains(string(clean), `"n":1.0`) {
		t.Fatalf("seed filter: %s", clean)
	}
	if _, err := SanitizeJSON([]byte(`{`)); err == nil {
		t.Fatal("invalid seed must be rejected")
	}
}

// tools/list must follow registry order: a map iteration used to shuffle the catalog per process,
// which shuffled the student's prompt run to run and desynced it from the exported training catalog.
func TestToolListKeepsRegistryOrder(t *testing.T) {
	var ordered []llm.Tool
	for _, name := range []string{"describe_data_sources", "search_targets", "get_process_snapshot", "sample_logs"} {
		ordered = append(ordered, llm.Tool{Name: name, Parameters: []byte(`{"type":"object"}`)})
	}
	for round := 0; round < 3; round++ {
		got := ToolList(ordered)
		for i, want := range ordered {
			if got[i]["name"] != want.Name {
				t.Fatalf("round %d position %d = %v, want %s", round, i, got[i]["name"], want.Name)
			}
		}
	}
}

func testServer() *Server {
	return New([]llm.Tool{
		{Name: "echo", Description: "echo <args>", Parameters: json.RawMessage(`{"type":"object","properties":{"x":{"type":"string"}}}`),
			Call: func(_ context.Context, args json.RawMessage) (any, error) {
				return map[string]any{"status": "normal", "args": string(args), "note": "scenario-f01-h-pod"}, nil
			}},
		{Name: "boom", Parameters: json.RawMessage(`{"type":"object"}`),
			Call: func(context.Context, json.RawMessage) (any, error) {
				return nil, errors.New("backend <down> cleanup F05-P")
			}},
		{Name: "nan", Parameters: json.RawMessage(`{"type":"object"}`),
			Call: func(context.Context, json.RawMessage) (any, error) { return map[string]any{"v": math.NaN()}, nil }},
	}, true)
}

func TestHandleReplies(t *testing.T) {
	srv := testServer()
	for _, tc := range []struct {
		name, in, want string
	}{
		{"initialize", `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`,
			`{"jsonrpc":"2.0","id":1,"result":{"capabilities":{"tools":{}},"protocolVersion":"2024-11-05","serverInfo":{"name":"rca-tools","version":"1"}}}`},
		{"list", `{"jsonrpc":"2.0","id":2,"method":"tools/list"}`,
			`{"jsonrpc":"2.0","id":2,"result":{"tools":[{"description":"echo LTargsGT","inputSchema":{"properties":{"x":{"type":"string"}},"type":"object"},"name":"echo"},{"description":"","inputSchema":{"type":"object"},"name":"boom"},{"description":"","inputSchema":{"type":"object"},"name":"nan"}]}}`},
		{"unknown method", `{"jsonrpc":"2.0","id":"a","method":"ping"}`, `{"jsonrpc":"2.0","id":"a","result":{}}`},
		{"unknown tool", `{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"nope","arguments":{}}}`,
			`{"jsonrpc":"2.0","id":3,"error":{"code":-32000,"message":"unknown tool nope"}}`},
		{"tool error", `{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"boom","arguments":{}}}`,
			`{"jsonrpc":"2.0","id":4,"error":{"code":-32000,"message":"backend LTdownGT cleanup BESOB"}}`},
		{"unencodable result", `{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"nan","arguments":{}}}`,
			`{"jsonrpc":"2.0","id":5,"error":{"code":-32000,"message":"json: unsupported value: NaN"}}`},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := srv.Handle(context.Background(), []byte(tc.in))
			want := htmlEscaped(tc.want)
			if !ok || string(got) != want {
				t.Fatalf("Handle(%s)\n got %s (ok=%v)\nwant %s", tc.in, got, ok, want)
			}
		})
	}
}

func TestHandleToolCallTextBlock(t *testing.T) {
	got, ok := testServer().Handle(context.Background(), []byte(`{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"echo","arguments":{"x": "<a>"}}}`))
	if !ok {
		t.Fatal("no reply")
	}
	var r struct {
		Result struct {
			Content []struct{ Type, Text string } `json:"content"`
		} `json:"result"`
	}
	if err := json.Unmarshal(got, &r); err != nil || len(r.Result.Content) != 1 || r.Result.Content[0].Type != "text" {
		t.Fatalf("reply %s: %v", got, err)
	}
	text := r.Result.Content[0].Text
	if strings.Contains(text, "scenario-f01") || !strings.Contains(text, "u003ca") || !strings.HasPrefix(text, `{"args":`) {
		t.Fatalf("text block %s", text)
	}
}

func TestHandleIgnoresNotificationsAndGarbage(t *testing.T) {
	srv := testServer()
	for _, in := range []string{`{"jsonrpc":"2.0","method":"notifications/initialized"}`, `not json`, ``} {
		if out, ok := srv.Handle(context.Background(), []byte(in)); ok || out != nil {
			t.Fatalf("Handle(%q) = %s, want no reply", in, out)
		}
	}
}

// htmlEscaped writes the json.Marshal HTML escapes of < and > (placeholders LT/GT keep the literals readable).
func htmlEscaped(s string) string {
	return strings.NewReplacer("LT", `\`+"u003c", "GT", `\`+"u003e").Replace(s)
}
