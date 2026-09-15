// §13.1 부분 강등 시험 — ToolsOn 순회 원천별 강제 실패 (§15.2-3, §14-7 2b).
//
// deps.go의 사상표(정본)를 기계 순회해 (도구, 원천, 등급) 전 조합에 대해
// 그 원천만 죽인 상태로 도구를 호출하고 등급별 계약을 단언한다:
//   - required           → 도구 전체 no_data(backend_error) 봉투, Backend
//     토큰 일치(오류를 내부에서 삼켜 guard에 안 닿으면 여기서 검출된다).
//   - semantic_auxiliary → 호출 성공 + 산 원천 관측 유지(status != no_data,
//     findings 존재) + 결손 명시(source_errors 분류 토큰).
//   - optional           → semantic_auxiliary와 같은 표면 계약(강등 규칙의
//     차이는 도구별 문구·폴백 — 여기서는 결손 명시 존재만 공통 단언).
//
// 결손 명시 규약: 어느 finding이든 "source_errors" 맵의 키가 backend와
// 같거나 backend+"_" 접두다(meta.go A6의 vm_series·ch_logs 관례 승계).
//
// 표가 늘면 시험이 자동으로 넓어진다 — 도구 생성자·인자 매핑 테이블만은
// 하드코딩이 불가피하므로 ToolBackendDeps와의 키 일치를 단언한다.
//
// 판단 지점(브레이커 격리): defaultBreaker는 프로세스 전역이라 조합 간
// 실패가 이월되면 open 상태가 다음 조합을 오염시킨다. 공개 리셋 표면은
// 만들지 않았다 — 이 시험은 패키지 내부라 states를 직접 비운다. 패키지
// 밖 시험이 필요해지면 그때 리셋 표면을 청구한다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

const (
	dgUUID     = "11111111-1111-1111-1111-111111111111"
	dgPeerUUID = "22222222-2222-2222-2222-222222222222"
	dgFrom     = "2026-08-01T10:00:00Z"
	dgTo       = "2026-08-01T10:30:00Z"
)

var (
	dgFirst = time.Date(2026, 8, 1, 10, 0, 0, 0, time.UTC)
	dgLast  = time.Date(2026, 8, 1, 10, 30, 0, 0, time.UTC)
	dgNow   = func() time.Time { return dgLast }
)

// dgCtors — 도구 생성자 매핑(불가피한 테이블). 키는 ToolBackendDeps와
// 1:1이어야 한다(아래 단언).
var dgCtors = map[string]func(s Stores) llm.Tool{
	"get_trace_spans":         func(s Stores) llm.Tool { return NewTraceSpansTool(s.CH) },
	"get_trace_activity":      func(s Stores) llm.Tool { return NewTraceActivityTool(s.CH) },
	"search_targets":          func(s Stores) llm.Tool { return NewSearchTargetsTool(s.PG) },
	"describe_data_sources":   func(s Stores) llm.Tool { return NewDescribeDataSourcesTool() },
	"get_process_snapshot":    func(s Stores) llm.Tool { return NewProcessSnapshotTool(s.CH) },
	"get_trace_summaries":     func(s Stores) llm.Tool { return NewTraceSummaryTool(s.CH) },
	"search_host_data":        func(s Stores) llm.Tool { return NewHostObservationsTool(s.CH) },
	"get_cluster_projections": func(s Stores) llm.Tool { return NewClusterProjectionTool(s.CH) },
	"scan_metrics":            func(s Stores) llm.Tool { return NewScanMetricsTool(s.VM, s.PG, dgFirst) },
	"read_timeseries":         func(s Stores) llm.Tool { return NewReadTimeseriesTool(s.VM, s.PG, dgFirst, dgNow) },
	"sample_logs":             func(s Stores) llm.Tool { return NewSampleLogsTool(s.CH, dgFirst) },
	"list_events":             func(s Stores) llm.Tool { return NewListEventsTool(s.CH, s.PG, dgNow) },
	"describe_target":         func(s Stores) llm.Tool { return NewDescribeTargetTool(s.PG, s.VM, dgFirst) },
	"list_changes":            func(s Stores) llm.Tool { return NewListChangesTool(s.PG, dgFirst, dgLast, dgNow) },
	"compare_peers":           func(s Stores) llm.Tool { return NewComparePeersTool(s.PG, s.VM, dgFirst, dgNow) },
	"expand_topology":         func(s Stores) llm.Tool { return NewExpandTopologyTool(s.PG, s.CH, dgFirst, dgLast, dgNow) },
	"db_blocking":             func(s Stores) llm.Tool { return NewDBBlockingTool(s.CH, dgFirst, dgLast, dgNow) },
	"db_slow_queries":         func(s Stores) llm.Tool { return NewDBSlowQueriesTool(s.CH, s.PG, dgFirst, dgLast, dgNow) },
	"breakdown_endpoints":     func(s Stores) llm.Tool { return NewBreakdownEndpointsTool(s.CH, dgFirst, dgLast) },
	"get_data_coverage":       func(s Stores) llm.Tool { return NewDataCoverageTool(s.PG, s.CH, s.VM) },
	"get_processes":           func(s Stores) llm.Tool { return NewProcessesTool(s.VM) },
	"get_snmp_traps":          func(s Stores) llm.Tool { return NewSnmpTrapsTool(s.CH) },
	"get_k8s_state":           func(s Stores) llm.Tool { return NewK8sStateTool(s.VM, s.PG) },
}

// dgArgs — 도구별 호출 인자. 죽은 원천에 닿기 전에 인자 검증에서 죽으면
// 시험이 공허해지므로 전부 유효값이다.
var dgArgs = map[string]string{
	"get_trace_spans":         `{"trace_id":"example","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"get_trace_activity":      `{"target":"svc-a","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"search_targets":          `{}`,
	"describe_data_sources":   `{}`,
	"get_process_snapshot":    `{"target":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"get_trace_summaries":     `{"kind":"path","target":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"search_host_data":        `{"kind":"connections","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"get_cluster_projections": `{"from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"scan_metrics":            `{"target":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"read_timeseries":         `{"targets":["` + dgUUID + `"],"metrics":["m1"],"from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"sample_logs":             `{"mode":"map","target":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"list_events":             `{"target":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"describe_target":         `{"target":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"list_changes":            `{"from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"compare_peers":           `{"target":"` + dgUUID + `","metric":"m1","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"expand_topology":         `{"target":"` + dgUUID + `"}`,
	"db_blocking":             `{"db":"` + dgUUID + `"}`,
	"db_slow_queries":         `{"db":"` + dgUUID + `"}`,
	"breakdown_endpoints":     `{"target":"svc-a"}`,
	"get_data_coverage":       `{"targets":["` + dgUUID + `"],"from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"get_processes":           `{"host":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"get_snmp_traps":          `{"device":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
	"get_k8s_state":           `{"target":"` + dgUUID + `","from":"` + dgFrom + `","to":"` + dgTo + `"}`,
}

// ── 산 원천 fake 3종 ────────────────────────────────────────────

// dgVM — 요청 selector에서 __name__·target_id를 뽑아 그대로 라벨로 되돌려
// 주는 범용 VM fake. 어떤 조회든 관측 1시리즈가 있는 세상이다.
func dgVM(t *testing.T) *VM {
	t.Helper()
	nameRe := regexp.MustCompile(`__name__="([^"]+)"`)
	targetRe := regexp.MustCompile(`target_id="([0-9a-fA-F-]+)"`)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		name, target := "m1", dgUUID
		if m := nameRe.FindStringSubmatch(q); m != nil {
			name = m[1]
		}
		if m := targetRe.FindStringSubmatch(q); m != nil {
			target = m[1]
		}
		labels := map[string]string{"__name__": name, "target_id": target,
			"namespace": "ns", "pod": "p1", "name": "proc", "pid": "1"}
		lj, _ := json.Marshal(labels)
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/v1/query_range"):
			start, _ := strconv.ParseInt(r.URL.Query().Get("start"), 10, 64)
			end, _ := strconv.ParseInt(r.URL.Query().Get("end"), 10, 64)
			step := int64(30)
			var vals []string
			for ts := start; ts <= end; ts += step {
				vals = append(vals, fmt.Sprintf(`[%d,"1.5"]`, ts))
			}
			fmt.Fprintf(w, `{"status":"success","data":{"result":[{"metric":%s,"values":[%s]}]}}`,
				lj, strings.Join(vals, ","))
		case strings.HasPrefix(r.URL.Path, "/api/v1/label/"):
			fmt.Fprint(w, `{"status":"success","data":[]}`)
		default: // instant
			fmt.Fprintf(w, `{"status":"success","data":{"result":[{"metric":%s,"value":[%d,"3"]}]}}`,
				lj, dgLast.Unix())
		}
	}))
	t.Cleanup(srv.Close)
	return &VM{BaseURL: srv.URL}
}

// dgCHScript — SQL 본문 부분 문자열 → JSONEachRow 행 대본.
type dgCHScript struct {
	contains string
	rows     []map[string]any
}

// dgCH — 대본 순서 일치 CH fake. 대본에 없는 조회는 0행 성공.
func dgCH(t *testing.T, scripts []dgCHScript) *CH {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := make([]byte, 1<<20)
		n, _ := r.Body.Read(body)
		sql := string(body[:n])
		for _, sc := range scripts {
			if strings.Contains(sql, sc.contains) {
				for _, row := range sc.rows {
					b, _ := json.Marshal(row)
					w.Write(b)
					w.Write([]byte("\n"))
				}
				return
			}
		}
	}))
	t.Cleanup(srv.Close)
	return &CH{BaseURL: srv.URL, Database: "lucida"}
}

// dgCHScripts — 산 CH가 답해야 하는 조회 전부(도구별 최소 성공 대본).
func dgCHScripts() []dgCHScript {
	return []dgCHScript{
		// db_slow_queries — 기준선·폴 수·engine 폴백은 창 조회보다 먼저
		// 매칭돼야 한다(전부 dpm_topsql_local을 포함).
		{"quantileExact", []map[string]any{{"sql_key": "q1", "mode": "delta", "p50": 9.0, "mx": 12.0, "polls": 10}}},
		{"uniqExact(timestamp)", []map[string]any{{"n": 10}}},
		{"SELECT engine FROM dpm_topsql_local", []map[string]any{{"engine": "postgresql"}}},
		{"FROM dpm_topsql_local", []map[string]any{
			{"ts": "2026-08-01 10:05:00", "sql_key": "q1", "mode": "delta", "lat_ms": 10.0, "execs": 5.0,
				"total_ms": 50.0, "has_total": 1, "io0": 1.0, "io1": 1.0, "io2": 0.0, "io3": 0.0,
				"sc0": "db1", "sc1": "u1", "sql_text": "SELECT 1", "engine": "postgresql"},
			{"ts": "2026-08-01 10:10:00", "sql_key": "q1", "mode": "delta", "lat_ms": 11.0, "execs": 5.0,
				"total_ms": 55.0, "has_total": 1, "io0": 1.0, "io1": 1.0, "io2": 0.0, "io3": 0.0,
				"sc0": "db1", "sc1": "u1", "sql_text": "SELECT 1", "engine": "postgresql"},
			{"ts": "2026-08-01 10:15:00", "sql_key": "q1", "mode": "delta", "lat_ms": 10.5, "execs": 5.0,
				"total_ms": 52.0, "has_total": 1, "io0": 1.0, "io1": 1.0, "io2": 0.0, "io3": 0.0,
				"sc0": "db1", "sc1": "u1", "sql_text": "SELECT 1", "engine": "postgresql"},
		}},
		// list_events — 에피소드 1건 + 도착 수평선.
		{"GROUP BY episode_id", []map[string]any{{"ep": "e1", "detector": "metric-anomaly", "reason": "cpu high",
			"metric": "m1", "n_open": 1, "n_close": 1, "open_at": "2026-08-01 10:05:00",
			"close_at": "2026-08-01 10:10:00", "dur_ms": 300000, "peak": "major", "open_sev": "minor",
			"last_event_id": "ev1"}}},
		{"max(occurred_at)", []map[string]any{{"h": "2026-08-01 10:29:00"}}},
		// get_data_coverage — 창 내 관측 수 2종.
		{"count() AS n FROM lucida_logs_local", []map[string]any{{"n": 2}}},
		{"count() AS n FROM lucida_events_local", []map[string]any{{"n": 1}}},
	}
}

// dgPGScripts — 산 PG가 답해야 하는 조회 전부. 부분 문자열은 도구별로
// 유일해야 한다(fakePG는 등록 순서 매칭).
func dgPGScripts(p *dgFakePG) {
	// expand_topology 명부 — seed가 있어야 CH(죽은 쪽)까지 간다.
	p.script("meta->>'resource_kind'", []string{"id", "name", "type", "rk", "rkey", "rtype", "parent"},
		[][]string{{dgUUID, "app1", "application", "", "", "", ""}})
	// compare_peers — 유형 → 그룹 없음 → 유형 전체 폴백 또래 1.
	p.script("SELECT type FROM targets WHERE id", []string{"type"}, [][]string{{"application"}})
	p.script("FROM targets WHERE type", []string{"id"}, [][]string{{dgPeerUUID}})
	// describe_target 정체.
	p.script("in_maintenance", []string{"type", "name", "display_name", "address", "status", "in_maintenance", "meta_host"},
		[][]string{{"server", "host1", "disp1", "10.0.0.1", "active", "f", ""}})
	// db_slow_queries engine 레지스트리(ch 죽은 조합에서 engine 확정용).
	p.script("collector_dpm_engine_version", []string{"engine", "version"}, [][]string{{"postgresql", "16.0"}})
}

// dgDead — 항상 5xx인 HTTP 백엔드(vm·ch용).
func dgDead(t *testing.T) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "down", http.StatusInternalServerError)
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

// dgDeadPG — 닫힌 포트 DSN의 *sql.DB(dial 실패 = connect_failed 계열).
func dgDeadPG(t *testing.T) *sql.DB {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ln.Addr().String()
	ln.Close()
	db, err := sql.Open("pgx", "postgres://u:p@"+addr+"/lucida?sslmode=disable&connect_timeout=1")
	if err != nil {
		t.Fatalf("dead PG open: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// dgResetBreaker — 조합 간 전역 브레이커 상태 이월 차단(머리 주석의 판단
// 지점). cooldown 대기 금지 — 상태를 직접 비운다.
func dgResetBreaker() {
	defaultBreaker.mu.Lock()
	defaultBreaker.states = map[string]*breakerState{}
	defaultBreaker.mu.Unlock()
}

// dgHasSourceErr는 결손 명시 규약(source_errors 키 = backend 또는
// backend_ 접두) 충족 여부다.
func dgHasSourceErr(env Envelope, backend string) bool {
	for _, f := range env.Findings {
		se, ok := f["source_errors"]
		if !ok {
			continue
		}
		switch m := se.(type) {
		case map[string]string:
			for k, v := range m {
				if v != "" && (k == backend || strings.HasPrefix(k, backend+"_")) {
					return true
				}
			}
		case map[string]any:
			for k, v := range m {
				if fmt.Sprint(v) != "" && (k == backend || strings.HasPrefix(k, backend+"_")) {
					return true
				}
			}
		}
	}
	return false
}

// TestDegradeByBackendClass — 사상표 전 조합의 원천별 강제 실패.
func TestDegradeByBackendClass(t *testing.T) {
	// 매핑 테이블과 사상표의 키 일치 — 표가 늘었는데 테이블이 안 늘면
	// 여기서 죽는다(자동 확장의 강제 장치).
	for name := range ToolBackendDeps {
		if _, ok := dgCtors[name]; !ok {
			t.Fatalf("사상표의 %s가 dgCtors에 없음 — 생성자 매핑을 추가하라", name)
		}
		if _, ok := dgArgs[name]; !ok {
			t.Fatalf("사상표의 %s가 dgArgs에 없음", name)
		}
	}
	for name := range dgCtors {
		if _, ok := ToolBackendDeps[name]; !ok {
			t.Fatalf("dgCtors의 %s가 사상표에 없음", name)
		}
	}

	alivePG := newDgFakePG(t)
	dgPGScripts(alivePG)
	aliveDB, err := sql.Open("pgx", alivePG.dsn())
	if err != nil {
		t.Fatalf("alive PG open: %v", err)
	}
	t.Cleanup(func() { aliveDB.Close() })
	deadDB := dgDeadPG(t)
	deadURL := dgDead(t)

	total := 0
	for _, deps := range ToolBackendDeps {
		total += len(deps)
	}
	ran := 0

	for _, backend := range []string{"pg", "ch", "vm"} {
		for _, class := range []SourceClass{SourceRequired, SourceSemanticAuxiliary, SourceOptional} {
			for _, tool := range ToolsOn(backend, class) {
				tool, backend, class := tool, backend, class
				ran++
				t.Run(fmt.Sprintf("%s/%s/%s", tool, backend, class), func(t *testing.T) {
					dgResetBreaker()
					s := Stores{PG: aliveDB, CH: dgCH(t, dgCHScripts()), VM: dgVM(t)}
					switch backend {
					case "pg":
						s.PG = deadDB
					case "ch":
						s.CH = &CH{BaseURL: deadURL, Database: "lucida"}
					case "vm":
						s.VM = &VM{BaseURL: deadURL}
					}
					guarded := withBackendGuard(dgCtors[tool](s))
					out, err := guarded.Call(context.Background(), json.RawMessage(dgArgs[tool]))
					if err != nil {
						t.Fatalf("도구 오류로 종료 — 봉투 사상이어야 한다(guard에 안 닿는 삼킴/미분류): %v", err)
					}
					env, ok := out.(Envelope)
					if !ok {
						t.Fatalf("봉투가 아님: %T", out)
					}
					switch class {
					case SourceRequired:
						if env.Status != "no_data" || env.NoDataReason != NoDataBackendError {
							t.Fatalf("required 원천 %s 실패인데 no_data(backend_error)가 아님: status=%s reason=%s summary=%q",
								backend, env.Status, env.NoDataReason, env.Summary)
						}
						if env.Backend != backend {
							t.Fatalf("Backend 토큰 불일치: got %q want %q (detail=%q)", env.Backend, backend, env.BackendDetail)
						}
						if env.BackendDetail == "" {
							t.Fatal("BackendDetail 토큰이 비었다")
						}
					default: // semantic_auxiliary · optional — 부분 강등.
						if env.Status == "no_data" {
							t.Fatalf("%s 원천 %s 실패에 도구 전체가 no_data — 산 원천 관측이 유지돼야 한다: reason=%s summary=%q",
								class, backend, env.NoDataReason, env.Summary)
						}
						if len(env.Findings) == 0 {
							t.Fatal("산 원천 관측(findings)이 비었다")
						}
						if !dgHasSourceErr(env, backend) {
							b, _ := json.Marshal(env.Findings)
							t.Fatalf("결손 명시(source_errors[%s*]) 없음 — 조용한 결손: %s", backend, b)
						}
					}
				})
			}
		}
	}
	if ran != total {
		t.Fatalf("조합 커버 %d/%d — 등급 3종 순회가 표를 다 못 덮었다", ran, total)
	}
}
