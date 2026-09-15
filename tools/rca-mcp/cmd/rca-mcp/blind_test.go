package main

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

func TestBlindToolBoundary(t *testing.T) {
	payload := map[string]any{"status": "normal", "findings": []any{
		map[string]any{"name": "scenario-f03-h-order-thread-pool-4jcn9", "ref": "ch:kcm:scenario-f03-h-order-thread-pool-4jcn9"},
		map[string]any{"body": "nkia : USER=root ; COMMAND=/usr/bin/bash -s -- cleanup F05-P memhog 6250 480"},
		map[string]any{"body": "kernel OOMKilled process java; LOCK TABLE inventory IN EXCLUSIVE MODE; HTTP 429", "count": 5},
	}}
	clean, err := modelJSON(payload, true)
	if err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"F05-P", "scenario-f03", "order-thread-pool", "memhog", "6250"} {
		if strings.Contains(string(clean), forbidden) {
			t.Fatalf("private annotation survived: %s", forbidden)
		}
	}
	for _, required := range []string{"OOMKilled", "LOCK TABLE", "HTTP 429", "blind_filter"} {
		if !strings.Contains(string(clean), required) {
			t.Fatalf("missing observation or filter notice: %s", required)
		}
	}
	// A second pass through an MCP text block cannot undo filtering.
	response := reply{JSONRPC: "2.0", ID: 1, Result: map[string]any{"content": []any{map[string]any{"type": "text", "text": string(clean)}}}}
	wrapped, err := modelJSON(response, true)
	if err != nil || !json.Valid(wrapped) {
		t.Fatal(err)
	}
	if strings.Contains(string(wrapped), "memhog") {
		t.Fatal("nested leak")
	}
	raw, _ := modelJSON(payload, false)
	if !strings.Contains(string(raw), "memhog") {
		t.Fatal("non-blind observations changed")
	}
	if !strings.Contains(payload["findings"].([]any)[1].(map[string]any)["body"].(string), "memhog") {
		t.Fatal("original was mutated")
	}
}

func TestBlindErrorsAndMarshalFailure(t *testing.T) {
	raw, err := modelJSON(reply{JSONRPC: "2.0", ID: 1, Error: map[string]any{"message": "COMMAND=bash cleanup F05-P memhog"}}, true)
	if err != nil || strings.Contains(string(raw), "memhog") {
		t.Fatalf("error boundary: %s %v", raw, err)
	}
	if _, err := modelJSON(map[string]any{"n": math.NaN()}, true); err == nil {
		t.Fatal("invalid response should fail closed")
	}
}
