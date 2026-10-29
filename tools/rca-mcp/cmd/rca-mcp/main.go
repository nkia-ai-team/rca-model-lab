// rca-mcp exposes the read-only RCA Toolset through the MCP stdio protocol.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/internal/blind"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/mcpserver"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/tools"
)

func main() {
	first := flag.String("first-event", "", "incident start (RFC3339; requires -last-event)")
	last := flag.String("last-event", "", "incident end (RFC3339; requires -first-event)")
	blindMode := flag.Bool("blind", false, "filter explicit experiment labels from model-visible responses")
	sanitizeStdin := flag.Bool("sanitize-stdin", false, "filter one JSON document from stdin without backend access")
	captureStart := flag.String("capture-start", "", "earliest instant the capture holds data for (RFC3339); clips default baselines")
	navHints := flag.Bool("nav-hints", false, "add query-path navigation to no-data envelopes (e.g. kcm metrics live under the cluster target)")
	flag.Parse()
	tools.NavHints = *navHints || os.Getenv("RCA_NAV_HINTS") == "1" // external captures pass it with the connection env
	if *sanitizeStdin {
		raw, err := io.ReadAll(io.LimitReader(os.Stdin, (16<<20)+1))
		if err != nil {
			fatal(err)
		}
		if len(raw) > 16<<20 {
			fatal(fmt.Errorf("public JSON exceeds input limit"))
		}
		clean, stats, err := blind.SanitizeJSON(raw)
		if err != nil {
			fatal(fmt.Errorf("invalid public seed JSON"))
		}
		_ = json.NewEncoder(os.Stderr).Encode(stats)
		fmt.Println(string(clean))
		return
	}
	firstEvent, lastEvent, err := parseEventWindow(*first, *last)
	if err != nil {
		fatal(err)
	}
	if *captureStart == "" {
		*captureStart = os.Getenv("RCA_CAPTURE_START") // external captures pass it with the connection env
	}
	if *captureStart != "" {
		cs, err := time.Parse(time.RFC3339, *captureStart)
		if err != nil {
			fatal(fmt.Errorf("invalid -capture-start: %w", err))
		}
		tools.CaptureStart = cs
	}
	avail, err := tools.LoadAvailability(os.Getenv("RCA_CAPTURE_SOURCES"))
	if err != nil {
		fatal(err)
	}
	s, err := storesFromEnv()
	if err != nil {
		fatal(err)
	}
	defer s.PG.Close()
	// 등록 순서를 보존한다 — tools/list 가 map 순회로 나가면 프로세스마다 도구 순서가
	// 달라져 학생 프롬프트(도구 카탈로그)가 런마다 흔들리고, 학습 프롬프트와도 어긋난다.
	ordered := tools.ToolsetWith(s, firstEvent, lastEvent, avail)
	srv := mcpserver.New(ordered, *blindMode)
	in := bufio.NewScanner(os.Stdin)
	for in.Scan() {
		if b, ok := srv.Handle(context.Background(), in.Bytes()); ok {
			fmt.Fprintln(os.Stdout, string(b))
		}
	}
}

// An omitted pair preserves live-mode defaults. A supplied pair binds tools
// that omit explicit query windows to the incident rather than wall-clock time.
func parseEventWindow(first, last string) (time.Time, time.Time, error) {
	if first == "" && last == "" {
		return time.Time{}, time.Time{}, nil
	}
	if first == "" || last == "" {
		return time.Time{}, time.Time{}, fmt.Errorf("-first-event and -last-event must be supplied together")
	}
	from, err := time.Parse(time.RFC3339, first)
	if err != nil {
		return time.Time{}, time.Time{}, fmt.Errorf("invalid -first-event: %w", err)
	}
	to, err := time.Parse(time.RFC3339, last)
	if err != nil {
		return time.Time{}, time.Time{}, fmt.Errorf("invalid -last-event: %w", err)
	}
	if !from.Before(to) {
		return time.Time{}, time.Time{}, fmt.Errorf("-first-event must precede -last-event")
	}
	return from, to, nil
}

func storesFromEnv() (tools.Stores, error) {
	dsn, chURL, vmURL := os.Getenv("RCA_PG_DSN"), os.Getenv("RCA_CH_URL"), os.Getenv("RCA_VM_URL")
	if dsn == "" || chURL == "" || vmURL == "" {
		return tools.Stores{}, fmt.Errorf("RCA_PG_DSN·RCA_CH_URL·RCA_VM_URL 전부 필요")
	}
	pg, e := tools.OpenPG(dsn)
	if e != nil {
		return tools.Stores{}, e
	}
	u, e := url.Parse(chURL)
	if e != nil {
		return tools.Stores{}, e
	}
	pass, _ := u.User.Password()
	return tools.Stores{PG: pg, CH: &tools.CH{BaseURL: u.Scheme + "://" + u.Host, User: u.User.Username(), Pass: pass, Database: "lucida"}, VM: &tools.VM{BaseURL: vmURL}}, nil
}
func fatal(e error) { fmt.Fprintln(os.Stderr, e); os.Exit(1) }
