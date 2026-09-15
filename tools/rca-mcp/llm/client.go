// llm은 LLM 자리 5개의 실 어댑터다. 프롬프트 정본은
// docs/spec-agent-prompts.md — 이 패키지는 문서를 옮겨 적는 쪽이고,
// 문안을 바꿀 때는 문서를 먼저 고친다.
//
// 공통 어댑터 규약(문서 §1): OpenAI 호환 chat completions, temperature
// 0 기본, 출력은 JSON만(코드펜스 제거 후 파싱, 실패는 오류 — 조용한
// 기본값 금지), JSON 필드는 이유가 결론보다 앞.
package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
)

// Client는 OpenAI 호환 엔드포인트 하나를 가리킨다.
type Client struct {
	BaseURL     string // 예: http://localhost:8448
	Model       string
	Temperature float64 // 0 기본 — 예외는 자리별 절에 명시(문서 §1)
	MaxTokens   int     // 0이면 4000
	HTTPClient  *http.Client
	Trace       *TraceLog // nil이면 궤적 기록 없음. 포인터 — 값 복사돼도 공유
	// Usage — run 단위 토큰 누적 계정. nil이면 계정 없음(additive).
	// Trace와 같은 이유로 포인터다: 어댑터들이 Client를 값으로 복사해
	// 들고 다녀도 계정은 run 하나를 공유해야 한다(§7.6 지출 전 잔여 검사).
	Usage *UsageMeter
}

// ── 토큰 계정 (§7.6의 선행 부품) ──────────────────────────────────

// usage는 OpenAI 호환 응답의 usage 절이다. **실물 대조**(2026-08-03,
// vLLM 0.21.0 / gemma4-26b-a4b-it-fp8): `"usage":{"prompt_tokens":14,
// "total_tokens":19,"completion_tokens":5,"prompt_tokens_details":null}`
// — 세 항목의 이름이 그대로이고 잉여 키가 섞이므로 strict 디코드는 쓰지 않는다.
type usage struct {
	PromptTokens     int `json:"prompt_tokens"`
	CompletionTokens int `json:"completion_tokens"`
	TotalTokens      int `json:"total_tokens"`
}

// ── 호출처 딱지 (§14-7 ③ — §11 예산표 대조·R/A 토큰 귀속의 전제) ──
//
// 어느 부품의 호출인지는 생산 시점에만 알 수 있다(trace·journal 사후
// 복원 불가 — 착공 실측). 어댑터들이 *Client 하나(base)를 공유하므로
// 딱지는 Client 필드가 아니라 **호출 컨텍스트**에 실린다. 중첩은 "/"로
// 잇는다 — 재생성 경로(loop/regen.go)가 "regen"을 얹으면 그 안의 후보
// 재호출은 "regen/candidates_investigation"이 된다.

type purposeCtxKey struct{}

// WithPurpose는 호출처 딱지 조각 하나를 컨텍스트에 얹는다. 이미 딱지가
// 있으면 뒤에 잇는다(바깥 경로가 앞).
func WithPurpose(ctx context.Context, seg string) context.Context {
	if prev := PurposeFrom(ctx); prev != "" {
		seg = prev + "/" + seg
	}
	return context.WithValue(ctx, purposeCtxKey{}, seg)
}

// PurposeFrom은 현재 딱지다. 없으면 빈 값 — 계정·이벤트에 빈 값 그대로
// 남는다(모르는 것을 지어내지 않는다).
func PurposeFrom(ctx context.Context) string {
	if v, ok := ctx.Value(purposeCtxKey{}).(string); ok {
		return v
	}
	return ""
}

// Usage는 계정의 스냅샷이다.
type Usage struct {
	PromptTokens     int
	CompletionTokens int
	TotalTokens      int
	// Calls — usage 절이 실린 응답의 수(계정이 실제로 센 호출 수).
	Calls int
}

// UsageMeter는 run 단위 누적 계정이다. 동시성 안전 — 어댑터 여러 개가
// 같은 계정을 공유한다.
type UsageMeter struct {
	mu sync.Mutex
	u  Usage

	// OnCall — 호출 하나가 계정에 들어올 때마다 불린다(§14-6 6a — LLM
	// usage의 호출당 내구 기록. cmd가 ledger의 llm_usage 이벤트로 배선).
	// purpose는 호출처 딱지다(§14-7 ③ — WithPurpose 컨텍스트에서 승계).
	// 잠금 밖에서 불린다 — 훅이 다시 계정을 읽어도 교착하지 않는다.
	OnCall func(model, purpose string, prompt, completion, total int)
}

// add는 응답 하나의 usage를 누적한다. 수신자 nil은 무시다(계정 미설정).
//
// total이 0인데 prompt+completion이 있으면 그 합을 쓴다 — 백엔드가 total을
// 생략해도 상한 검사의 분자가 0으로 굳지 않게 한다. 셋 다 0이면 usage 절이
// 없었다는 뜻이라 호출 수도 세지 않는다(빈 계정과 "0 토큰 호출"의 구분).
func (m *UsageMeter) add(u usage) {
	if m == nil || (u.PromptTokens == 0 && u.CompletionTokens == 0 && u.TotalTokens == 0) {
		return
	}
	total := u.TotalTokens
	if total == 0 {
		total = u.PromptTokens + u.CompletionTokens
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.u.PromptTokens += u.PromptTokens
	m.u.CompletionTokens += u.CompletionTokens
	m.u.TotalTokens += total
	m.u.Calls++
}

// record는 add + OnCall 훅이다. 호출부(CompleteJSON)가 모델명·호출처
// 딱지를 아는 유일한 자리라 여기서 받는다. usage 절이 없던 응답(셋 다
// 0)은 add와 같은 이유로 훅도 부르지 않는다.
func (m *UsageMeter) record(model, purpose string, u usage) {
	if m == nil || (u.PromptTokens == 0 && u.CompletionTokens == 0 && u.TotalTokens == 0) {
		return
	}
	m.add(u)
	if m.OnCall != nil {
		total := u.TotalTokens
		if total == 0 {
			total = u.PromptTokens + u.CompletionTokens
		}
		m.OnCall(model, purpose, u.PromptTokens, u.CompletionTokens, total)
	}
}

// Snapshot은 현재 누적이다. 수신자 nil은 빈 계정이다.
func (m *UsageMeter) Snapshot() Usage {
	if m == nil {
		return Usage{}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.u
}

// ── 오류의 typed화 (§15.5 사상표 — §14-6 6a) ─────────────────────
//
// battery·probe의 오류들과 같은 규약이다: Reason()을 가진 오류를 상위
// (cmd/rca의 first-match 판정기)가 실패 선언으로 사상한다. 전송·계약
// 위반을 하나로 뭉치면 운영자 안내가 갈리지 않는다 — "서빙이 죽었다"
// (llm_error)와 "모델 출력이 계약을 어긴다"(parse_failure)는 조치가 다르다.

// CallError는 LLM 호출 자체의 실패다(전송·HTTP·응답 형식). 원인 오류를
// 보존한다 — cancel 중 끊긴 호출은 context.Canceled가 사슬에 남아
// 판정기의 cancelled first-match가 이긴다.
type CallError struct {
	Op  string // marshal · request · transport · http_status · decode · empty_choices · config
	Err error
}

func (e *CallError) Error() string {
	if e.Err == nil {
		return fmt.Sprintf("llm 호출 실패(%s)", e.Op)
	}
	return fmt.Sprintf("llm 호출 실패(%s): %v", e.Op, e.Err)
}
func (e *CallError) Unwrap() error                    { return e.Err }
func (e *CallError) Reason() ledger.FailureReason     { return ledger.ReasonLLMError }

// OutputParseError는 LLM 출력이 JSON 계약을 어겼다는 것이다 — 호출은
// 성공했고 토큰도 나갔다. 어댑터의 재시도가 소진된 뒤 표면으로 올라오면
// parse_failure다.
type OutputParseError struct {
	Err     error
	Excerpt string // 본문 머리(디버깅 — trace에 전문이 있다)
}

func (e *OutputParseError) Error() string {
	return fmt.Sprintf("llm 출력 JSON 파싱: %v (본문: %.200s)", e.Err, e.Excerpt)
}
func (e *OutputParseError) Unwrap() error                { return e.Err }
func (e *OutputParseError) Reason() ledger.FailureReason { return ledger.ReasonParseFailure }

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type chatRequest struct {
	Model          string        `json:"model"`
	Messages       []chatMessage `json:"messages"`
	Temperature    float64       `json:"temperature"`
	MaxTokens      int           `json:"max_tokens"`
	ResponseFormat *respFormat   `json:"response_format,omitempty"`
}

type respFormat struct {
	Type string `json:"type"`
}

type chatResponse struct {
	Choices []struct {
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
	} `json:"choices"`
	Usage usage `json:"usage"`
}

// CompleteJSON은 system+user 한 쌍을 보내고 응답 JSON을 out에 채운다.
// 파싱 실패는 오류다 — 어댑터는 기본값을 지어내지 않는다.
func (c *Client) CompleteJSON(ctx context.Context, system, user string, out any) error {
	if c.BaseURL == "" || c.Model == "" {
		return &CallError{Op: "config", Err: fmt.Errorf("BaseURL·Model 필수")}
	}
	maxTokens := c.MaxTokens
	if maxTokens == 0 {
		maxTokens = 4000
	}
	body, err := json.Marshal(chatRequest{
		Model: c.Model,
		Messages: []chatMessage{
			{Role: "system", Content: system},
			{Role: "user", Content: user},
		},
		Temperature:    c.Temperature,
		MaxTokens:      maxTokens,
		ResponseFormat: &respFormat{Type: "json_object"},
	})
	if err != nil {
		return &CallError{Op: "marshal", Err: err}
	}

	hc := c.HTTPClient
	if hc == nil {
		// 호출당 시한(§15.2-1 3층의 바닥) — 가안, env는 §13.1 주입용.
		sec := 300
		if v := os.Getenv("RCA_LLM_HTTP_TIMEOUT_S"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 {
				sec = n
			}
		}
		hc = &http.Client{Timeout: time.Duration(sec) * time.Second}
	}
	req, err := http.NewRequestWithContext(ctx, "POST", c.BaseURL+"/v1/chat/completions", bytes.NewReader(body))
	if err != nil {
		return &CallError{Op: "request", Err: err}
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := hc.Do(req)
	if err != nil {
		return &CallError{Op: "transport", Err: err}
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return &CallError{Op: "http_status", Err: fmt.Errorf("HTTP %d", resp.StatusCode)}
	}
	var cr chatResponse
	if err := json.NewDecoder(resp.Body).Decode(&cr); err != nil {
		return &CallError{Op: "decode", Err: err}
	}
	// 계정은 choices 검사보다 먼저다 — 빈 응답에도 토큰은 이미 나갔다.
	purpose := PurposeFrom(ctx)
	c.Usage.record(c.Model, purpose, cr.Usage)
	if len(cr.Choices) == 0 {
		return &CallError{Op: "empty_choices"}
	}
	text := StripFence(cr.Choices[0].Message.Content)
	parseErr := json.Unmarshal([]byte(text), out)
	entry := map[string]any{"kind": "complete_json", "model": c.Model, "purpose": purpose,
		"temperature": c.Temperature, "system": system, "user": user, "response": text}
	if parseErr != nil {
		entry["error"] = parseErr.Error()
	}
	c.Trace.write(entry)
	if parseErr != nil {
		return &OutputParseError{Err: parseErr, Excerpt: text}
	}
	return nil
}

// StripFence는 마크다운 코드펜스(```json ... ```)를 벗긴다(문서 §1).
// 도구 루프의 최종 자유 응답을 직접 파싱하는 소비처(cmd/react)가 생기며
// 승격.
func StripFence(s string) string {
	s = strings.TrimSpace(s)
	if !strings.HasPrefix(s, "```") {
		return s
	}
	s = strings.TrimPrefix(s, "```")
	s = strings.TrimPrefix(s, "json")
	if i := strings.LastIndex(s, "```"); i >= 0 {
		s = s[:i]
	}
	return strings.TrimSpace(s)
}
