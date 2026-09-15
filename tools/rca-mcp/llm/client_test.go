package llm

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeServer는 고정 content를 돌려주는 OpenAI 호환 mock이다.
func fakeServer(t *testing.T, content string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		body := `{"choices":[{"message":{"content":` + jsonString(content) + `}}]}`
		w.Write([]byte(body))
	}))
}

func jsonString(s string) string {
	b := new(strings.Builder)
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}

func TestCompleteJSON_코드펜스를_벗기고_파싱한다(t *testing.T) {
	srv := fakeServer(t, "```json\n{\"note\": \"이유\", \"pass\": true}\n```")
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Model: "m"}
	var out struct {
		Note string `json:"note"`
		Pass bool   `json:"pass"`
	}
	if err := c.CompleteJSON(context.Background(), "s", "u", &out); err != nil {
		t.Fatalf("CompleteJSON: %v", err)
	}
	if !out.Pass || out.Note != "이유" {
		t.Fatalf("파싱 결과 이상: %+v", out)
	}
}

func TestCompleteJSON_JSON_아니면_오류다(t *testing.T) {
	srv := fakeServer(t, "죄송하지만 판단할 수 없습니다.")
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Model: "m"}
	var out map[string]any
	if err := c.CompleteJSON(context.Background(), "s", "u", &out); err == nil {
		t.Fatal("파싱 실패가 오류로 올라와야 한다 — 조용한 기본값 금지")
	}
}

func TestCompleteJSON_HTTP_오류는_오류다(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "boom", http.StatusInternalServerError)
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL, Model: "m"}
	var out map[string]any
	if err := c.CompleteJSON(context.Background(), "s", "u", &out); err == nil {
		t.Fatal("HTTP 오류가 올라와야 한다")
	}
}

func TestStripFence(t *testing.T) {
	cases := [][2]string{
		{"{\"a\":1}", "{\"a\":1}"},
		{"```json\n{\"a\":1}\n```", "{\"a\":1}"},
		{"```\n{\"a\":1}\n```", "{\"a\":1}"},
		{"  {\"a\":1}  ", "{\"a\":1}"},
	}
	for _, c := range cases {
		if got := StripFence(c[0]); got != c[1] {
			t.Errorf("StripFence(%q) = %q, want %q", c[0], got, c[1])
		}
	}
}
