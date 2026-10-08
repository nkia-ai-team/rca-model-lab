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

// resourceToken: experiment-runner resources (scenario-*) and case ids (case-fNN*). A bare "case-" prefix
// also matched ordinary words such as "case-insensitive" in tool descriptions (2026-10-06).
var resourceToken = regexp.MustCompile(`(?i)\b(?:scenario-[a-z0-9][a-z0-9_.-]*|case-f[0-9]{2}[a-z0-9_.-]*)`)
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
	out, err := encode(v)
	return out, stats, err
}

// object keeps a JSON object's key order. Decoding into map[string]any and re-marshalling sorted every
// envelope alphabetically, which pushed "summary" behind "findings"/"refs" — past the 6000-char view the
// student reads (2026-10-06). Values are decoded and re-encoded in their original order.
type object []member

type member struct {
	key   string
	value any
}

func decode(raw []byte) (any, error) {
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	v, err := decodeValue(d)
	if err != nil {
		return nil, err
	}
	if _, err := d.Token(); err != io.EOF {
		return nil, fmt.Errorf("expected one JSON value")
	}
	return v, nil
}

func decodeValue(d *json.Decoder) (any, error) {
	tok, err := d.Token()
	if err != nil {
		return nil, err
	}
	delim, ok := tok.(json.Delim)
	if !ok {
		return tok, nil // string, json.Number, bool or nil
	}
	switch delim {
	case '{':
		obj := object{}
		for d.More() {
			kt, err := d.Token()
			if err != nil {
				return nil, err
			}
			key, ok := kt.(string)
			if !ok {
				return nil, fmt.Errorf("non-string object key")
			}
			value, err := decodeValue(d)
			if err != nil {
				return nil, err
			}
			obj = append(obj, member{key, value})
		}
		if _, err := d.Token(); err != nil {
			return nil, err
		}
		return obj, nil
	case '[':
		arr := []any{}
		for d.More() {
			value, err := decodeValue(d)
			if err != nil {
				return nil, err
			}
			arr = append(arr, value)
		}
		if _, err := d.Token(); err != nil {
			return nil, err
		}
		return arr, nil
	}
	return nil, fmt.Errorf("unexpected delimiter %v", delim)
}

func encode(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := encodeValue(&buf, v); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func encodeValue(buf *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case object:
		buf.WriteByte('{')
		for i, m := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			key, err := json.Marshal(m.key)
			if err != nil {
				return err
			}
			buf.Write(key)
			buf.WriteByte(':')
			if err := encodeValue(buf, m.value); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
	case []any:
		buf.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := encodeValue(buf, item); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
	default:
		b, err := json.Marshal(x)
		if err != nil {
			return err
		}
		buf.Write(b)
	}
	return nil
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
	case object:
		out := make(object, 0, len(x))
		for _, m := range x {
			if privateKey(m.key) {
				stats.RemovedFields++
				continue
			}
			out = append(out, member{sanitizeText(m.key, stats), walk(m.value, stats, depth+1)})
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
				if clean, err := encode(walk(inner, stats, depth+1)); err == nil {
					return string(clean)
				}
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
		return "[redacted]"
	}
	resource := func(token string) string {
		stats.OpaqueIdentifiers++
		return resourcePseudonym(token)
	}
	code := func(token string) string {
		stats.OpaqueIdentifiers++
		return codePseudonym(token)
	}
	return scenarioCode.ReplaceAllStringFunc(resourceToken.ReplaceAllStringFunc(s, resource), code)
}

// Pseudonyms look like ordinary identifiers so the replacement itself does not mark where an experiment
// touched the data (2026-10-06: "opaque-<hex>" next to an injected probe path read as "the injection is
// here"). They are deterministic (same token → same pseudonym across responses and nested copies), never
// match the patterns above (idempotent), and carry no meaning from the original token.
const pseudoConsonants = "bdfgklmnprstvz"
const pseudoVowels = "aeiou"

func pronounceable(sum []byte, letters int) string {
	var b strings.Builder
	for i := 0; i < letters; i++ {
		x := int(sum[i%len(sum)]) + 7*i
		if i%2 == 0 {
			b.WriteByte(pseudoConsonants[x%len(pseudoConsonants)])
		} else {
			b.WriteByte(pseudoVowels[x%len(pseudoVowels)])
		}
	}
	return b.String()
}

// codePseudonym replaces a scenario code (e.g. a family/variant label) with a 5-letter word, keeping case.
func codePseudonym(token string) string {
	sum := sha256.Sum256([]byte(strings.ToLower(token)))
	word := pronounceable(sum[:], 5)
	if token == strings.ToUpper(token) {
		return strings.ToUpper(word)
	}
	return word
}

// resourcePseudonym replaces an experiment-named resource with a workload-pod-shaped name: word-word-hash.
func resourcePseudonym(token string) string {
	sum := sha256.Sum256([]byte(strings.ToLower(token)))
	return pronounceable(sum[:8], 5) + "-" + pronounceable(sum[8:16], 5) + "-" + hex.EncodeToString(sum[16:19])[:5]
}
