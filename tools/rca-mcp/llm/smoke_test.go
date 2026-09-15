package llm

import (
	"os"
	"testing"
)

// smokeClient — 실 vLLM이 필요한 스모크 시험의 공용 클라이언트. RCA_LLM_URL이
// 없으면 skip한다(평소 go test는 hermetic 유지).
//
//	RCA_LLM_URL=http://localhost:8448 go test ./llm -run Smoke -v
//
// 종전에는 verifier_smoke_test.go가 이 헬퍼의 집이었고, 그 파일이 구 확인자
// 표면과 함께 삭제되면서(§14-4 4c) 남은 소비자(grouper·candidates·writer)를
// 위해 여기로 옮겨 왔다.
func smokeClient(t *testing.T) *Client {
	t.Helper()
	url := os.Getenv("RCA_LLM_URL")
	if url == "" {
		t.Skip("RCA_LLM_URL 미설정 — 실 LLM 스모크 skip")
	}
	model := os.Getenv("RCA_LLM_MODEL")
	if model == "" {
		model = "gemma4-26b-a4b-it-fp8"
	}
	return &Client{BaseURL: url, Model: model, MaxTokens: 1200}
}
