// 도구 호출 루프 — Examiner·Investigator가 공유하는 어댑터 하부
// (파이프라인 [3] 결정: "LLM 어댑터 코드는 공유"). 도구 자체는
// Tool.Call 인터페이스 뒤에 있다 — 지금은 테스트 mock, 도구 계약의
// lucida-next 실 구현이 오면 그대로 꽂는다.
package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// Tool은 조사자에게 노출되는 조회 도구 하나다(도구 계약 §3~4).
// Parameters는 JSON Schema, Call은 응답 봉투(status/summary/findings/
// refs) 또는 복구 정보를 담은 오류 객체를 돌려준다(계약 §2).
type Tool struct {
	Name        string
	Description string
	Parameters  json.RawMessage
	Call        func(ctx context.Context, args json.RawMessage) (any, error)
}

type toolDef struct {
	Type     string `json:"type"`
	Function struct {
		Name        string          `json:"name"`
		Description string          `json:"description"`
		Parameters  json.RawMessage `json:"parameters"`
	} `json:"function"`
}

type toolCall struct {
	ID   string `json:"id"`
	Type string `json:"type"` // "function" — 어시스턴트 메시지 echo에 필수
	Function struct {
		Name      string `json:"name"`
		Arguments string `json:"arguments"`
	} `json:"function"`
}

type toolsResponse struct {
	Choices []struct {
		Message struct {
			Content   string     `json:"content"`
			ToolCalls []toolCall `json:"tool_calls"`
		} `json:"message"`
	} `json:"choices"`
	// Usage — 토큰 계정(client.go). 도구 루프는 턴마다 응답을 받으므로
	// 턴별로 누적한다 — 마지막 턴만 세면 도구 왕복의 지출이 통째로 빠진다.
	Usage usage `json:"usage"`
}

// ChatOption은 ChatTools의 호출별 손잡이다.
type ChatOption func(*chatOpts)

type chatOpts struct {
	forceFirstTool bool
	callLog        *[]pipeline.ToolCallRecord
}

// CollectCalls는 성공한 도구 호출(이름·인자)을 into에 쌓는다 — 커버리지
// 격자(§15.2)의 결정론 입력. 오류 응답(무효 도구·중복 차단)은 커버리지가
// 아니므로 기록하지 않는다.
func CollectCalls(into *[]pipeline.ToolCallRecord) ChatOption {
	return func(o *chatOpts) { o.callLog = into }
}

// ForceFirstTool은 첫 턴에 도구 호출을 강제한다(tool_choice=required).
// 걸음의 정의가 probe 실행인 조사자처럼 "도구를 부를지"가 질문이 아닌
// 자리에 쓴다 — 답이 정해진 질문은 규칙이 답한다(가드 ③과 같은 원리).
// 실측 배경: 모델이 도구 호출을 시뮬레이션하고 관측을 지어내는 걸음이
// 있었고(2026-07-16 실 주행), 반려 피드백만으로는 교정되지 않았다.
func ForceFirstTool() ChatOption { return func(o *chatOpts) { o.forceFirstTool = true } }

// ChatTools는 도구 호출 루프를 종료(도구 없는 응답)까지 돌리고 최종
// content와 이번 대화에서 실제 돌려준 도구 응답의 ref 집합을 준다 —
// ref 집합은 근거 ref 검증 가드(문서 §6.6 ①)의 입력이다.
func (c *Client) ChatTools(ctx context.Context, system, user string, tools []Tool, maxTurns int, opts ...ChatOption) (content string, refs map[string]bool, err error) {
	var o chatOpts
	for _, fn := range opts {
		fn(&o)
	}

	// 궤적 — 이 대화가 실제로 주고받은 전부를 한 줄로 남긴다(trace.go).
	var traceTurns []map[string]any
	defer func() {
		entry := map[string]any{"kind": "chat_tools", "model": c.Model,
			"temperature": c.Temperature, "force_first_tool": o.forceFirstTool,
			"system": system, "user": user, "turns": traceTurns, "final": content}
		if err != nil {
			entry["error"] = err.Error()
		}
		c.Trace.write(entry)
	}()
	if maxTurns == 0 {
		maxTurns = 14
	}
	byName := map[string]Tool{}
	defs := make([]toolDef, len(tools))
	names := make([]string, len(tools))
	for i, t := range tools {
		byName[t.Name] = t
		names[i] = t.Name
		defs[i].Type = "function"
		defs[i].Function.Name = t.Name
		defs[i].Function.Description = t.Description
		defs[i].Function.Parameters = t.Parameters
	}

	messages := []map[string]any{
		{"role": "system", "content": system},
		{"role": "user", "content": user},
	}
	seenRefs := map[string]bool{}
	// 동일 (도구, 인자) 재호출 차단 — no_data 응답을 받고도 같은 호출을
	// 반복하다 턴을 소진하는 모드가 관찰됐다(2026-07-20 재생 주행).
	// 같은 대화에서 같은 호출의 결과는 같다 — 답이 정해진 재시도는
	// 규칙이 끊고, 복구 정보를 준다.
	calledOnce := map[string]bool{}

	hc := c.HTTPClient
	if hc == nil {
		hc = &http.Client{Timeout: 300 * time.Second}
	}
	maxTokens := c.MaxTokens
	if maxTokens == 0 {
		maxTokens = 4000
	}

	for turn := 0; turn < maxTurns; turn++ {
		req := map[string]any{
			"model": c.Model, "messages": messages, "tools": defs,
			"temperature": c.Temperature, "max_tokens": maxTokens,
		}
		if turn == 0 && o.forceFirstTool {
			req["tool_choice"] = "required"
		}
		body, err := json.Marshal(req)
		if err != nil {
			return "", nil, fmt.Errorf("llm: 요청 직렬화: %w", err)
		}
		httpReq, err := http.NewRequestWithContext(ctx, "POST", c.BaseURL+"/v1/chat/completions", bytes.NewReader(body))
		if err != nil {
			return "", nil, fmt.Errorf("llm: 요청 생성: %w", err)
		}
		httpReq.Header.Set("Content-Type", "application/json")
		resp, err := hc.Do(httpReq)
		if err != nil {
			return "", nil, fmt.Errorf("llm: 호출: %w", err)
		}
		raw, err := io.ReadAll(resp.Body)
		resp.Body.Close()
		if err != nil {
			return "", nil, fmt.Errorf("llm: 응답 읽기: %w", err)
		}
		if resp.StatusCode != http.StatusOK {
			return "", nil, fmt.Errorf("llm: HTTP %d: %.300s", resp.StatusCode, raw)
		}
		var tr toolsResponse
		if err := json.Unmarshal(raw, &tr); err != nil {
			return "", nil, fmt.Errorf("llm: 응답 디코드: %w", err)
		}
		c.Usage.add(tr.Usage)
		if len(tr.Choices) == 0 {
			return "", nil, fmt.Errorf("llm: 응답 choices 비어 있음")
		}
		msg := tr.Choices[0].Message
		if len(msg.ToolCalls) == 0 {
			if os.Getenv("RCA_LLM_DEBUG") != "" {
				fmt.Fprintf(os.Stderr, "[final turn=%d] %.600s\n", turn, msg.Content)
			}
			return msg.Content, seenRefs, nil
		}

		for i := range msg.ToolCalls {
			if msg.ToolCalls[i].Type == "" {
				msg.ToolCalls[i].Type = "function"
			}
		}
		messages = append(messages, map[string]any{
			"role": "assistant", "content": msg.Content, "tool_calls": msg.ToolCalls,
		})
		turnTrace := map[string]any{"assistant": msg.Content}
		var callsTrace []map[string]any
		traceTurns = append(traceTurns, turnTrace)
		for _, tc := range msg.ToolCalls {
			var result any
			callKey := tc.Function.Name + "\x00" + tc.Function.Arguments
			tool, ok := byName[tc.Function.Name]
			if !ok {
				// 오류엔 복구 정보(도구 계약 §2) — 유효 도구 목록.
				result = map[string]any{"error": fmt.Sprintf(
					"존재하지 않는 도구 %q. 유효한 도구: %v", tc.Function.Name, names)}
			} else if calledOnce[callKey] {
				result = map[string]any{"error": fmt.Sprintf(
					"%s를 같은 인자로 이미 호출했다 — 같은 대화에서 결과는 같다. 인자(지표 이름·대상·창)를 바꾸거나 아직 안 본 관점의 도구(list_events·sample_logs·read_timeseries 등)를 쓰라. 더 볼 것이 없으면 도구 없이 최종 보고하라(데이터가 없었다면 outcome=infeasible).",
					tc.Function.Name)}
			} else {
				calledOnce[callKey] = true
				out, err := tool.Call(ctx, json.RawMessage(tc.Function.Arguments))
				if err != nil {
					result = map[string]any{"error": err.Error()}
				} else {
					result = out
					if o.callLog != nil {
						*o.callLog = append(*o.callLog, pipeline.ToolCallRecord{
							Name: tc.Function.Name, Args: json.RawMessage(tc.Function.Arguments)})
					}
				}
			}
			content, err := json.Marshal(result)
			if err != nil {
				return "", nil, fmt.Errorf("llm: 도구 응답 직렬화: %w", err)
			}
			// ref 수집은 직렬화 결과에서 — 도구 반환이 구조체(Envelope)든
			// map이든 같은 JSON 표면을 보게 한다.
			var decoded any
			if err := json.Unmarshal(content, &decoded); err == nil {
				collectRefs(decoded, seenRefs)
			}
			callsTrace = append(callsTrace, map[string]any{
				"name": tc.Function.Name, "args": tc.Function.Arguments,
				"result": json.RawMessage(content)})
			turnTrace["calls"] = callsTrace
			if os.Getenv("RCA_LLM_DEBUG") != "" {
				fmt.Fprintf(os.Stderr, "[tool] %s(%s) -> %.200s\n",
					tc.Function.Name, tc.Function.Arguments, content)
			}
			messages = append(messages, map[string]any{
				"role": "tool", "tool_call_id": tc.ID, "content": string(content),
			})
		}
	}
	return "", nil, fmt.Errorf("llm: 도구 루프 %d turn 초과 — 미종료", maxTurns)
}

// collectRefs는 도구 응답 JSON에서 refs 배열을 재귀적으로 모은다 —
// 봉투 최상위 refs와 finding별 refs(봉투 v2) 모두. 입력은 직렬화를
// 거친 값이라 map/[]any/스칼라만 온다.
func collectRefs(v any, into map[string]bool) {
	switch t := v.(type) {
	case map[string]any:
		if refs, ok := t["refs"].([]any); ok {
			for _, r := range refs {
				if s, ok := r.(string); ok {
					into[s] = true
				}
			}
		}
		for _, child := range t {
			collectRefs(child, into)
		}
	case []any:
		for _, child := range t {
			collectRefs(child, into)
		}
	}
}
