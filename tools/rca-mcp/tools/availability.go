package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// Capture availability (2026-09-30). Captures converted from external datasets (e.g. RCAEval) carry only
// part of the Polestar telemetry: no traces, no DB sessions, no Kubernetes state, ... Without a signal the
// tools report such gaps as "0 observations" (zero_observations / normal), which reads as evidence of
// absence and would teach the student that those tools are useless or that the source rules a cause out.
// A capture may therefore ship a manifest (path in RCA_CAPTURE_SOURCES):
//
//	{"not_collected": ["traces", "database", ...], "note": "..."}
//
// Every tool whose datasets are all listed answers no_data / not_collected instead of querying, and
// describe_data_sources lists the gaps. Without the variable nothing changes (all Polestar captures).

// Availability is the per-capture list of datasets that were not collected.
type Availability struct {
	NotCollected map[string]bool
	Note         string
}

// CaptureStart is the earliest instant the capture holds data for (flag -capture-start, zero = unknown).
// Default baselines that would reach before it are clipped to it (sample_logs), so a short pre-incident
// span is not divided as if it were the nominal 60 minutes.
var CaptureStart time.Time

// ToolDatasets maps each tool to the datasets it cannot work without. A tool is unavailable only when every
// dataset it needs is not collected; tools not listed (inventory, metrics, logs, events) are never blocked.
var ToolDatasets = map[string][]string{
	"get_trace_spans":         {"traces"},
	"get_trace_activity":      {"traces"},
	"get_trace_summaries":     {"traces"},
	"breakdown_endpoints":     {"traces"},
	"expand_topology":         {"traces"}, // app/db edges and hosted_on are trace-derived (no configured topology)
	"get_process_snapshot":    {"process"},
	"get_processes":           {"process"},
	"search_host_data":        {"host"},
	"get_snmp_traps":          {"snmp"},
	"get_cluster_projections": {"cluster_projection"},
	"db_blocking":             {"database"},
	"db_slow_queries":         {"database"},
	"get_k8s_state":           {"kubernetes"},
	"list_changes":            {"changes"},
}

// KnownDatasets are the names a manifest may use.
var KnownDatasets = map[string]string{
	"traces":             "분산 트레이스(otel_traces_local·trace 요약) — 호출 관계·구간 지연·앱 위상",
	"process":            "프로세스 스냅샷·프로세스 지표",
	"host":               "호스트 syslog·소켓 관측",
	"snmp":               "네트워크 장비 SNMP trap",
	"cluster_projection": "이벤트 클러스터 투영",
	"database":           "DB 세션(블로킹)·top-SQL",
	"kubernetes":         "Kubernetes 상태(재시작·OOMKilled·CrashLoop)·이벤트",
	"changes":            "변경 이력(배포·구성·정책)",
}

// LoadAvailability reads a manifest; an empty path means "everything collected".
func LoadAvailability(path string) (Availability, error) {
	a := Availability{NotCollected: map[string]bool{}}
	if path == "" {
		return a, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return a, fmt.Errorf("capture sources manifest: %w", err)
	}
	var m struct {
		NotCollected []string `json:"not_collected"`
		Note         string   `json:"note"`
	}
	if err := json.Unmarshal(raw, &m); err != nil {
		return a, fmt.Errorf("capture sources manifest: %w", err)
	}
	for _, d := range m.NotCollected {
		if _, ok := KnownDatasets[d]; !ok {
			return a, fmt.Errorf("capture sources manifest: unknown dataset %q", d)
		}
		a.NotCollected[d] = true
	}
	a.Note = m.Note
	return a, nil
}

// Missing returns the datasets of tool that are not collected, or nil when the tool is available.
func (a Availability) Missing(tool string) []string {
	need := ToolDatasets[tool]
	if len(need) == 0 {
		return nil
	}
	for _, d := range need {
		if !a.NotCollected[d] {
			return nil
		}
	}
	return need
}

func (a Availability) list() []string {
	out := make([]string, 0, len(a.NotCollected))
	for d := range a.NotCollected {
		out = append(out, d)
	}
	sort.Strings(out)
	return out
}

func withAvailability(t llm.Tool, a Availability) llm.Tool {
	missing := a.Missing(t.Name)
	if len(missing) > 0 {
		t.Call = func(ctx context.Context, args json.RawMessage) (any, error) {
			return Envelope{
				Status: "no_data", NoDataReason: NoDataNotCollected,
				Summary: fmt.Sprintf("이 캡처에는 %s 데이터가 수집되지 않았다 — 관측 결손이며 원인 배제 근거가 아니다. 다른 원천(지표·로그·이벤트)으로 조사하라.",
					strings.Join(missing, ", ")),
			}, nil
		}
		return t
	}
	if t.Name == "describe_data_sources" && len(a.NotCollected) > 0 {
		call := t.Call
		t.Call = func(ctx context.Context, args json.RawMessage) (any, error) {
			out, err := call(ctx, args)
			env, ok := out.(Envelope)
			if err != nil || !ok {
				return out, err
			}
			gaps := a.list()
			described := make([]string, 0, len(gaps))
			for _, d := range gaps {
				described = append(described, d+": "+KnownDatasets[d])
			}
			finding := Finding{"domain": "capture_availability", "not_collected": gaps, "descriptions": described}
			if a.Note != "" {
				finding["note"] = a.Note
			}
			env.Findings = append([]Finding{finding}, env.Findings...)
			env.Summary = "이 캡처에서 수집되지 않은 원천: " + strings.Join(gaps, ", ") + " — 해당 도구는 not_collected 로 답한다(배제 근거 아님). " + env.Summary
			return env, nil
		}
	}
	return t
}
