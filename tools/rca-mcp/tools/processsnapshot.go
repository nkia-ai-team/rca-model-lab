package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// NewProcessSnapshotTool exposes the captured process tables for replay and
// historical RCA. It complements get_processes, which reads live VM metrics.
func NewProcessSnapshotTool(ch *CH) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target":   map[string]string{"type": "string", "description": "server target_id(UUID)"},
			"from":     map[string]string{"type": "string", "description": "UTC RFC3339"},
			"to":       map[string]string{"type": "string", "description": "UTC RFC3339"},
			"sort_by":  map[string]string{"type": "string", "description": "cpu | memory | threads (기본 cpu)"},
			"top_n":    map[string]string{"type": "integer", "description": "상위 N(기본 10, 최대 50)"},
			"offset":   map[string]string{"type": "integer", "description": "동일 필터·정렬의 페이지 시작 위치 (기본 0)"},
			"proc_key": map[string]string{"type": "string", "description": "정확한 UInt64 프로세스 키 (문자열)"},
			"pid":      map[string]string{"type": "integer", "description": "프로세스 PID"},
		},
		"required": []string{"target", "from", "to"},
	})
	return llm.Tool{
		Name:        "get_process_snapshot",
		Description: "캡처된 process_snapshot과 process_meta를 시간창·프로세스 키로 결합해 과거 CPU/RSS/상태와 PID·PPID·사용자·명령행을 조회한다. 라이브 get_processes와 별도의 재현용 도구다.",
		Parameters:  params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var fields map[string]json.RawMessage
			if err := json.Unmarshal(args, &fields); err != nil {
				return nil, err
			}
			for key := range fields {
				switch key {
				case "target", "from", "to", "sort_by", "top_n", "offset", "proc_key", "pid":
				default:
					return nil, fmt.Errorf("unsupported argument %q", key)
				}
			}
			in, err := parseTargetWindow(args)
			if err != nil {
				return nil, err
			}
			var opt struct {
				SortBy  string `json:"sort_by"`
				TopN    int    `json:"top_n"`
				Offset  int    `json:"offset"`
				ProcKey string `json:"proc_key"`
				PID     *int   `json:"pid"`
			}
			if err := json.Unmarshal(args, &opt); err != nil {
				return nil, err
			}
			if opt.Offset < 0 || opt.Offset > 1000000 || opt.TopN < 0 {
				return nil, fmt.Errorf("offset must be 0..1000000; top_n must be nonnegative")
			}
			if opt.ProcKey != "" {
				if _, err := strconv.ParseUint(opt.ProcKey, 10, 64); err != nil {
					return nil, fmt.Errorf("proc_key must be a UInt64 decimal string")
				}
			}
			if opt.PID != nil && (*opt.PID < 1 || uint64(*opt.PID) > 4294967295) {
				return nil, fmt.Errorf("pid must be 1..4294967295")
			}
			if opt.SortBy == "" {
				opt.SortBy = "cpu"
			}
			if opt.SortBy != "cpu" && opt.SortBy != "memory" && opt.SortBy != "threads" {
				return nil, fmt.Errorf("sort_by %q 미지원 — cpu|memory|threads", opt.SortBy)
			}
			if opt.TopN <= 0 {
				opt.TopN = 10
			}
			if opt.TopN > 50 {
				opt.TopN = 50
			}
			return queryProcessSnapshotPage(ctx, ch, in.Target, in.FromT, in.ToT, opt.SortBy, opt.TopN, opt.Offset, opt.ProcKey, opt.PID)
		},
	}
}

func queryProcessSnapshot(ctx context.Context, ch *CH, target string, from, to time.Time, sortBy string, topN int) (any, error) {
	return queryProcessSnapshotPage(ctx, ch, target, from, to, sortBy, topN, 0, "", nil)
}

func queryProcessSnapshotPage(ctx context.Context, ch *CH, target string, from, to time.Time, sortBy string, topN, offset int, procKey string, pid *int) (any, error) {
	order := map[string]string{"cpu": "cpu_pct", "memory": "mem_rss", "threads": "threads"}[sortBy]
	filter := ""
	params := map[string]string{"target": target, "from": chTime(from), "to": chTime(to)}
	if procKey != "" {
		filter += " AND proc_key={proc_key:UInt64}"
		params["proc_key"] = procKey
	}
	if pid != nil {
		filter += " AND pid={pid:UInt32}"
		params["pid"] = strconv.Itoa(*pid)
	}
	q := fmt.Sprintf(`
WITH snapshots AS (
 SELECT target_id, proc_key,
   argMax(tuple(pid, ppid, name, state, create_time), ts) AS identity,
   max(cpu_pct) AS cpu_peak, max(mem_rss) AS rss_peak, max(threads) AS threads_peak,
   argMax(ts, tuple(cpu_pct, ts)) AS cpu_peak_at,
   argMax(ts, tuple(mem_rss, ts)) AS memory_peak_at,
   max(ts) AS observed_at
 FROM process_snapshot
 WHERE target_id={target:String}
   AND ts >= parseDateTime64BestEffort({from:String}, 9)
   AND ts < parseDateTime64BestEffort({to:String}, 9)%s
 GROUP BY target_id, proc_key
), metadata AS (
 SELECT m.target_id, m.proc_key,
   argMax(tuple(m.user, m.cmdline), m.seen_at) AS details,
   max(m.seen_at) AS metadata_seen_at, count() AS metadata_rows
 FROM process_meta m INNER JOIN snapshots s
   ON m.target_id=s.target_id AND m.proc_key=s.proc_key
 WHERE m.seen_at <= s.observed_at
 GROUP BY m.target_id, m.proc_key
)
SELECT s.target_id, toString(s.proc_key) AS proc_key,
 s.identity.1 AS pid, s.identity.2 AS ppid, s.identity.3 AS name,
 s.identity.4 AS state, toString(s.identity.5) AS create_time_unix,
 s.cpu_peak AS cpu_pct, s.rss_peak AS mem_rss, s.threads_peak AS threads,
 s.cpu_peak_at, s.memory_peak_at, s.observed_at,
 m.details.1 AS user, m.details.2 AS cmdline, m.metadata_seen_at, m.metadata_rows
FROM snapshots s LEFT JOIN metadata m
 ON m.target_id=s.target_id AND m.proc_key=s.proc_key
ORDER BY %s DESC, s.proc_key ASC
LIMIT %d OFFSET %d`, filter, order, topN+1, offset)
	rows, tr, err := ch.Query(ctx, q, params)
	if err != nil {
		return nil, fmt.Errorf("get_process_snapshot 조회: %w", err)
	}
	truncated := len(rows) > topN
	if truncated {
		rows = rows[:topN]
	}
	findings := make([]Finding, 0, len(rows))
	refs := make([]string, 0, len(rows))
	for _, r := range rows {
		ref := fmt.Sprintf("ch:process_snapshot:%s:%s:%s/%s", target, chStr(r["proc_key"]), from.UTC().Format(time.RFC3339Nano), to.UTC().Format(time.RFC3339Nano))
		refs = append(refs, ref)
		creation := ""
		if sec, err := strconv.ParseInt(chStr(r["create_time_unix"]), 10, 64); err == nil && sec > 0 && sec <= 253402300799 {
			creation = time.Unix(sec, 0).UTC().Format(time.RFC3339)
		}
		f := Finding{"target_id": target, "proc_key": chStr(r["proc_key"]), "pid": chInt(r["pid"]), "ppid": chInt(r["ppid"]), "name": chStr(r["name"]), "state": chStr(r["state"]), "cpu_pct": r["cpu_pct"], "mem_rss": r["mem_rss"], "threads": r["threads"], "create_time_unix": chStr(r["create_time_unix"]), "create_time": creation, "observed_at": chStr(r["observed_at"]), "cpu_peak_at": r["cpu_peak_at"], "memory_peak_at": r["memory_peak_at"], "refs": []string{ref}, "metadata_available": chInt(r["metadata_rows"]) > 0}
		if chInt(r["metadata_rows"]) > 0 {
			f["user"] = chStr(r["user"])
			f["cmdline"] = chStr(r["cmdline"])
			f["metadata_seen_at"] = r["metadata_seen_at"]
		}
		findings = append(findings, f)
	}
	env := Envelope{Status: "normal", Summary: fmt.Sprintf("캡처 프로세스 %d개 반환(sort_by=%s).", len(findings), sortBy), AssessmentBasis: "CPU/RSS/threads are independent window maxima; identity/state are latest snapshot values; metadata is latest at or before that snapshot. Offset pages require unchanged data and identical filters/sort.", Findings: findings, Refs: refs, ObservedRange: &TimeRange{From: from.UTC(), To: to.UTC()}, Truncated: truncated}
	if len(findings) == 0 {
		env.Status = "no_data"
		env.NoDataReason = NoDataZeroObservations
		env.Summary = "이 필터·페이지에 process_snapshot 관측이 없다. offset>0이면 앞 페이지의 관측 부재를 뜻하지 않는다."
	}
	if tr != nil {
		env.QueryTruncated = true
	}
	page := map[string]any{"offset": offset, "returned": len(rows), "has_more": truncated, "total_lower_bound": offset + len(rows) + boolInt(truncated), "total_exact": !truncated && tr == nil && (len(rows) > 0 || offset == 0)}
	if len(rows) == 0 && offset > 0 {
		page["total_lower_bound"] = 0
	}
	if truncated && tr == nil {
		page["next_offset"] = offset + len(rows)
	}
	if tr != nil {
		page["has_more"] = nil
		page["retry_smaller_page"] = true
	}
	if page["total_exact"] == true {
		env.Scopes = []QueryScope{qscope("process_snapshot", offset+len(rows), len(rows))}
	}
	page["section"] = "pagination"
	env.Findings = append(env.Findings, Finding(page))
	return env, nil
}
