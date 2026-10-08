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

func TestPseudonymsLookOrdinaryAndAreStable(t *testing.T) {
	raw := []byte(`{"path":"/actuator/health/f05-h-fail","pod":"scenario-f03-h-order-thread-pool-4jcn9","code":"F05-P","msg":"sudo COMMAND=/bin/sh memhog F05-P"}`)
	out, _, err := SanitizeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	s := string(out)
	for _, marker := range []string{"opaque", "experiment", "withheld", "f05-h", "F05-P", "scenario-", "order-thread-pool"} {
		if strings.Contains(s, marker) {
			t.Errorf("marker or leak %q in %s", marker, s)
		}
	}
	var clean map[string]string
	if err := json.Unmarshal(out, &clean); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(clean["path"], "/actuator/health/") || !strings.HasSuffix(clean["path"], "-fail") {
		t.Errorf("path shape changed: %s", clean["path"])
	}
	if clean["code"] != strings.ToUpper(clean["code"]) || len(clean["code"]) != 5 {
		t.Errorf("code pseudonym should keep case and be 5 letters: %s", clean["code"])
	}
	if parts := strings.Split(clean["pod"], "-"); len(parts) != 3 || len(parts[2]) != 5 {
		t.Errorf("resource pseudonym should look like word-word-hash: %s", clean["pod"])
	}
	if clean["msg"] != "[redacted]" {
		t.Errorf("experiment command should be redacted neutrally: %s", clean["msg"])
	}
	again, _, _ := SanitizeJSON(out)
	if string(again) != s {
		t.Fatal("not idempotent")
	}
	other, _, _ := SanitizeJSON([]byte(`{"p":"F05-H fail on f05-h"}`))
	var o map[string]string
	_ = json.Unmarshal(other, &o)
	if w := strings.Fields(o["p"]); len(w) != 4 || strings.ToLower(w[0]) != w[3] {
		t.Errorf("same code in different case should map to the same word (case kept): %s", o["p"])
	}
}

func TestOrdinaryCaseWordsSurvive(t *testing.T) {
	raw := []byte(`{"d":"Case-insensitive literal substring; case-sensitive match","id":"case-f03-h-v3-123 ran"}`)
	out, _, err := SanitizeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	s := string(out)
	for _, kept := range []string{"Case-insensitive", "case-sensitive"} {
		if !strings.Contains(s, kept) {
			t.Errorf("ordinary word %q changed: %s", kept, s)
		}
	}
	if strings.Contains(s, "case-f03-h") {
		t.Errorf("case id leaked: %s", s)
	}
}

func TestKeyOrderPreserved(t *testing.T) {
	raw := []byte(`{"status":"anomalous","summary":"first","findings":[{"z":1,"a":{"y":2,"b":3}}],"refs":["r"],"text":"{\"status\":\"normal\",\"summary\":\"inner\",\"findings\":[]}"}`)
	out, _, err := SanitizeJSON(raw)
	if err != nil {
		t.Fatal(err)
	}
	s := string(out)
	order := []string{`"status"`, `"summary"`, `"findings"`, `"refs"`, `"text"`}
	last := -1
	for _, k := range order {
		i := strings.Index(s, k)
		if i <= last {
			t.Fatalf("top-level order lost at %s: %s", k, s)
		}
		last = i
	}
	if !strings.Contains(s, `{"z":1,"a":{"y":2,"b":3}}`) {
		t.Fatalf("nested order lost: %s", s)
	}
	if !strings.Contains(s, `\"status\":\"normal\",\"summary\":\"inner\"`) {
		t.Fatalf("embedded JSON order lost: %s", s)
	}
}
