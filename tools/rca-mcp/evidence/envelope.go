// 봉투의 투영 입력 표현 — tools.Envelope의 **JSON 계약**을 디코드한다.
//
// 왜 tools.Envelope를 직접 쓰지 않는가: 패키지 의존이 tools → pipeline →
// ledger → evidence 방향이라 evidence가 tools를 import하면 순환이다. 그리고
// projector의 실제 입력은 evidence store에 적재된 **봉투 원문 JSON**이므로,
// JSON을 정본으로 두는 편이 store·golden fixture·런타임이 같은 것을 읽는다.
// 두 타입이 갈리는 위험은 tools 쪽 왕복 테스트(envelope_projection_test.go)가
// 막는다 — tools는 evidence를 import할 수 있다.
package evidence

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
	"unicode"
)

// RawEnvelope는 도구 응답 봉투다(tools.Envelope의 JSON 거울).
type RawEnvelope struct {
	Status          string       `json:"status"`
	NoDataReason    string       `json:"no_data_reason,omitempty"`
	AssessmentBasis string       `json:"assessment_basis,omitempty"`
	Summary         string       `json:"summary"`
	Findings        []RawFinding `json:"findings,omitempty"`
	ObservedRange   *RawRange    `json:"observed_range,omitempty"`
	Truncated       bool         `json:"truncated,omitempty"`
	// DegradedSources — 의미 강등 원천 토큰(§15.2-3 aux 결손, 2b D-3).
	// 비어 있지 않으면 파생 finding 레코드는 ConfLow다 — "해석 규칙이
	// 결손인 관측"이 ok 신뢰도로 진리표·반증 자격에 드는 유출 차단.
	DegradedSources map[string]string `json:"degraded_sources,omitempty"`

	// QueryTruncated — 질의 계층 절단(CH 캡 등 Total 미상, §14-7 D-1).
	// 표시 쿼터 절단(Truncated·Scopes)과 달리 이것만 레코드 완전성에
	// 합류한다 — 섞으면 표시만 잘린 봉투의 observed_zero까지 강등된다.
	QueryTruncated bool `json:"query_truncated,omitempty"`
	Refs            []string     `json:"refs,omitempty"`
	Scopes          []RawScope   `json:"scopes,omitempty"`

	// Backend·BackendDetail — no_data_reason=backend_error의 분류 토큰
	// (tools.Envelope 거울, §15.2-3). 원문 오류 문자열이 아니다 — guard가
	// 원천에서 토큰만 싣는다(§15.3-2).
	Backend       string `json:"backend,omitempty"`
	BackendDetail string `json:"backend_detail,omitempty"`
}

// RawFinding은 도구별 스키마의 관측 한 건이다 — 키가 도구마다 다르다.
type RawFinding map[string]any

// RawRange는 봉투의 실관측 범위다.
type RawRange struct {
	From time.Time `json:"from"`
	To   time.Time `json:"to"`
}

// RawScope는 논리 조회 단위의 절단 계약이다(tools.QueryScope의 거울).
type RawScope struct {
	Class    string `json:"class"`
	Metric   string `json:"metric,omitempty"`
	Total    int    `json:"total"`
	Returned int    `json:"returned"`
	Omitted  int    `json:"omitted"`
}

// DecodeEnvelope는 store에 적재된 봉투 원문을 읽는다.
func DecodeEnvelope(b []byte) (RawEnvelope, error) {
	var e RawEnvelope
	if err := json.Unmarshal(b, &e); err != nil {
		return RawEnvelope{}, fmt.Errorf("봉투 디코드: %w", err)
	}
	return e, nil
}

// ── finding 값 읽기 ────────────────────────────────────────────────
//
// 봉투는 map[string]any라 값 타입이 JSON 왕복으로 흔들린다(int → float64,
// 시각 → 문자열). 아래 함수들이 그 흔들림을 한곳에서 흡수한다 — 도구별
// 추출 코드가 각자 타입 단언을 하면 한 곳만 어긋나도 조용히 nil이 된다.

// str은 문자열 값이다. 없거나 문자열이 아니면 빈 값.
func (f RawFinding) str(key string) string {
	v, ok := f[key]
	if !ok || v == nil {
		return ""
	}
	if s, ok := v.(string); ok {
		return s
	}
	return fmt.Sprint(v)
}

// num은 수치 값이다. **문자열은 숫자로 읽지 않는다** — list_events의
// duration_ms는 미해소 에피소드에서 "미상"이라 문자열이며, 그것을 0으로 읽으면
// "지속 0ms"라는 없는 관측이 생긴다.
func (f RawFinding) num(key string) *float64 {
	v, ok := f[key]
	if !ok || v == nil {
		return nil
	}
	switch n := v.(type) {
	case float64:
		return &n
	case int:
		x := float64(n)
		return &x
	case json.Number:
		x, err := n.Float64()
		if err != nil {
			return nil
		}
		return &x
	}
	return nil
}

// nested는 "a.b" 꼴 중첩 키를 읽는다(stats.median · baseline.p50_ms).
func (f RawFinding) nested(path string) *float64 {
	head, rest, found := strings.Cut(path, ".")
	if !found {
		return f.num(path)
	}
	sub, ok := f[head].(map[string]any)
	if !ok {
		return nil
	}
	return RawFinding(sub).nested(rest)
}

// sub는 중첩 객체다.
func (f RawFinding) sub(key string) RawFinding {
	m, ok := f[key].(map[string]any)
	if !ok {
		return nil
	}
	return RawFinding(m)
}

// refs는 finding에 결합된 ref 목록이다.
func (f RawFinding) refs() []string {
	raw, ok := f["refs"].([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(raw))
	for _, v := range raw {
		if s, ok := v.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

// parseAt은 도구들이 쓰는 시각 표기 전부를 읽는다 — CH의 공백 구분
// DateTime64, RFC3339, 초 단위. 못 읽으면 zero다(호출자가 검사한다).
func parseAt(s string) (time.Time, bool) {
	s = strings.TrimSpace(s)
	if s == "" {
		return time.Time{}, false
	}
	for _, layout := range []string{
		time.RFC3339Nano, time.RFC3339,
		"2006-01-02 15:04:05.999999999", "2006-01-02 15:04:05",
	} {
		if t, err := time.Parse(layout, s); err == nil {
			return t.UTC(), true
		}
	}
	return time.Time{}, false
}

// parseInterval은 "<from>/<to>" 구간 표기를 읽는다(onset.interval).
func parseInterval(s string) (time.Time, time.Time, bool) {
	a, b, found := strings.Cut(s, "/")
	if !found {
		return time.Time{}, time.Time{}, false
	}
	from, ok1 := parseAt(a)
	to, ok2 := parseAt(b)
	return from, to, ok1 && ok2
}

// ── §15.1 원문 격리 ────────────────────────────────────────────────

// sanitizeExcerpt는 원문 발췌를 RawExcerpt에 실을 수 있는 형태로 만든다
// (§15.1-1 ②③ + §15.1-2 상한).
//
// 여기서 하는 것은 **탈출 차단**이다: 구분자·역할 토큰 치환, 제어문자·개행
// 제거, 길이 상한. 프롬프트 프레이밍(①의 고정 구분자 블록·기계 딱지)은
// 직렬화 시점의 몫이고(§14-4), 마스킹 스캐너는 §14-6이다 — 여기서 미리 하면
// 무엇이 어느 층의 책임인지가 흐려진다.
func sanitizeExcerpt(s string) string {
	if s == "" {
		return ""
	}
	// 제어문자·개행 → 공백(줄바꿈으로 블록을 깨는 탈출 차단).
	var b strings.Builder
	for _, r := range s {
		if r == '\n' || r == '\r' || r == '\t' || unicode.IsControl(r) {
			b.WriteRune(' ')
			continue
		}
		b.WriteRune(r)
	}
	out := strings.Join(strings.Fields(b.String()), " ")
	// 역할 토큰·구분자 치환.
	for _, tok := range []string{"system:", "assistant:", "user:", "###", "</", "```"} {
		out = strings.ReplaceAll(out, tok, "[ESC]")
	}
	return clip(out, rawExcerptMax)
}

// clip은 룬 단위 상한이다 — 바이트로 자르면 한글이 깨진다.
func clip(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	if max <= 1 {
		return string(r[:max])
	}
	return string(r[:max-1]) + "…"
}
