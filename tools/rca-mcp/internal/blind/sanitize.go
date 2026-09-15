// Package blind removes known experiment labels from model-visible copies of
// observation JSON. It is bounded pattern protection, not proof that arbitrary
// telemetry cannot disclose an injected cause. Original captures must be retained.
package blind

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"strings"
)

// Stats records transformations without retaining the removed private content.
type Stats struct {
	RemovedFields     int `json:"removed_fields"`
	RedactedStrings   int `json:"redacted_strings"`
	OpaqueIdentifiers int `json:"opaque_identifiers"`
}

var resourceToken = regexp.MustCompile(`(?i)\b(?:scenario|case)-[a-z0-9][a-z0-9_.-]*`)
var scenarioCode = regexp.MustCompile(`(?i)\bF[0-9]{2}[-_][A-Z][A-Z0-9]*\b`)
var experimentAction = regexp.MustCompile(`(?i)\b(?:memhog|inject(?:ion)?|cleanup)\b`)

// SanitizeJSON returns a fresh JSON value. Invalid input is rejected so callers
// can withhold it rather than accidentally forwarding an unsanitized response.
// Stable pseudonyms preserve equality across responses; refs containing these
// names are display references and must not be used as executable backend keys.
func SanitizeJSON(raw []byte) ([]byte, Stats, error) {
	var stats Stats
	v, err := decode(raw)
	if err != nil {
		return nil, stats, err
	}
	v = walk(v, &stats, 0)
	out, err := json.Marshal(v)
	return out, stats, err
}

func decode(raw []byte) (any, error) {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var v any
	if err := d.Decode(&v); err != nil {
		return nil, err
	}
	var extra any
	if err := d.Decode(&extra); err != io.EOF {
		return nil, fmt.Errorf("expected one JSON value")
	}
	return v, nil
}

func privateKey(key string) bool {
	switch strings.ToLower(key) {
	case "scenario_metadata", "injection_summary", "distinguishing_evidence", "golden", "golden.rca.json", "golden.anomaly.json", "case_id", "scenario_id":
		return true
	}
	return false
}

func walk(v any, stats *Stats, depth int) any {
	switch x := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, value := range x {
			if privateKey(k) {
				stats.RemovedFields++
				continue
			}
			out[sanitizeText(k, stats)] = walk(value, stats, depth+1)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, value := range x {
			out[i] = walk(value, stats, depth+1)
		}
		return out
	case string:
		// Drivers sometimes embed a complete observation as serialized JSON in
		// raw/message fields. Sanitize that copy as well as its expanded form.
		trimmed := strings.TrimSpace(x)
		if depth < 64 && (strings.HasPrefix(trimmed, "{") || strings.HasPrefix(trimmed, "[")) {
			if inner, err := decode([]byte(trimmed)); err == nil {
				clean, _ := json.Marshal(walk(inner, stats, depth+1))
				return string(clean)
			}
		}
		return sanitizeText(x, stats)
	default:
		return v
	}
}

func sanitizeText(s string, stats *Stats) string {
	lower := strings.ToLower(s)
	privateContent := strings.Contains(lower, "scenario-runs") || strings.Contains(lower, "scenario_metadata") || strings.Contains(lower, "injection_summary") || strings.Contains(lower, "distinguishing_evidence") || strings.Contains(lower, "golden.rca") || strings.Contains(lower, "golden.anomaly")
	command := strings.Contains(lower, "command=") || strings.Contains(lower, "sudo ")
	if privateContent || (command && (scenarioCode.MatchString(s) || experimentAction.MatchString(s) || resourceToken.MatchString(s))) {
		stats.RedactedStrings++
		return "[experiment annotation withheld]"
	}
	opaque := func(token string) string {
		sum := sha256.Sum256([]byte(token))
		stats.OpaqueIdentifiers++
		return "opaque-" + hex.EncodeToString(sum[:12])
	}
	return scenarioCode.ReplaceAllStringFunc(resourceToken.ReplaceAllStringFunc(s, opaque), opaque)
}
