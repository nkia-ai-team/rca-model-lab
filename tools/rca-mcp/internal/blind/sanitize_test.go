package blind

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestSanitizeKnownExperimentLeaksAndNestedCopies(t *testing.T) {
	pod := "scenario-f03-h-order-thread-pool-4jcn9"
	command := "sudo: root COMMAND=/bin/sh cleanup F05-P memhog --pid 123"
	rawCopy, _ := json.Marshal(map[string]any{"pod": pod, "message": command, "scenario_metadata": map[string]string{"cause": "answer"}})
	source := map[string]any{
		"pod": pod, "refs": []string{"ch:events:" + pod}, "raw": string(rawCopy),
		"message": command, "path": "/data/scenario-runs/F05-P/run.log",
		"scenario_metadata": map[string]string{"cause": "answer"},
		"injection_summary": "answer", "distinguishing_evidence": "answer", "golden": "answer",
		"case_id": "case-f03-h-v3-123", "scenario_id": "F03-H",
		"cause": "OOMKilled", "sql": "LOCK TABLE orders IN ACCESS EXCLUSIVE MODE",
		"ordinary": "kernel: Out of memory: Killed process 123 (stress-ng)",
		"error":    "HTTP error429", "count": json.Number("9007199254740993"),
	}
	raw, _ := json.Marshal(source)
	original := string(raw)
	out, stats, err := SanitizeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != original {
		t.Fatal("modified input")
	}
	for _, hidden := range []string{pod, "order-thread-pool", "memhog", "F05-P", "scenario_metadata", "answer", "case_id", "scenario_id"} {
		if strings.Contains(string(out), hidden) {
			t.Errorf("leaked %q: %s", hidden, out)
		}
	}
	for _, kept := range []string{"OOMKilled", "LOCK TABLE orders", "stress-ng", "error429", "9007199254740993"} {
		if !strings.Contains(string(out), kept) {
			t.Errorf("lost observation %q", kept)
		}
	}
	var clean map[string]any
	if err := json.Unmarshal(out, &clean); err != nil {
		t.Fatal(err)
	}
	var nested map[string]any
	if err := json.Unmarshal([]byte(clean["raw"].(string)), &nested); err != nil {
		t.Fatal(err)
	}
	if nested["pod"] != clean["pod"] {
		t.Fatal("pseudonym changed across nested copy")
	}
	if !strings.Contains(clean["refs"].([]any)[0].(string), clean["pod"].(string)) {
		t.Fatal("ref pseudonym differs")
	}
	if stats.RemovedFields != 7 || stats.RedactedStrings != 3 || stats.OpaqueIdentifiers != 3 {
		t.Fatalf("unexpected stats %+v", stats)
	}
	second, _, err := SanitizeJSON(out)
	if err != nil || string(second) != string(out) {
		t.Fatal("sanitization not idempotent")
	}
}

func TestSanitizeRejectsMalformedJSON(t *testing.T) {
	for _, raw := range []string{"", `{"message":`, `{} {}`, `{"n":NaN}`} {
		if out, _, err := SanitizeJSON([]byte(raw)); err == nil || out != nil {
			t.Errorf("accepted %q", raw)
		}
	}
}

func TestSanitizePreservesOrdinaryCommands(t *testing.T) {
	raw := []byte(`{"message":"sudo COMMAND=/usr/bin/stress-ng --cpu 2", "cause":"SQL deadlock", "status":"OOMKilled"}`)
	out, stats, err := SanitizeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	if stats != (Stats{}) || !strings.Contains(string(out), "stress-ng") {
		t.Fatalf("ordinary telemetry changed %s %+v", out, stats)
	}
}
