// 도구×backend 의존 사상표 (§15.2-3, §14-6 6b) — "숫자가 아니라 사상표가
// 정본이다": 어느 도구가 어느 저장소에 기대는지, 그 원천이 죽으면 무엇이
// 되는지를 표로 못 박는다. §13.1의 원천별 강제 실패 시험 목록은 이 표에서
// 기계 생성한다(ToolsOn).
//
// 원천 등급(§15.2-3):
//   - required           — 죽으면 그 도구 관측 전체가 성립 불가.
//   - semantic_auxiliary — 죽어도 조회는 되지만 값의 의미가 달라진다
//     (예: scan·read의 PG는 counter 판별 입력 — 실패 시 VM 값의 해석이
//     바뀐다, scan.go 판별부).
//   - optional           — 죽어도 해당 부가 관측만 빠진다.
//
// 집행(§14-7 2b): required 실패 → 도구 전체 no_data(backend_error) 봉투
// (guardedCall→withBackendGuard, PG는 pgErr 분류 경유). auxiliary·optional
// 원천의 부분 강등은 도구별로 구현됐다 — 산 원천 관측 유지 + 결손을
// finding의 source_errors(분류 토큰, 키는 backend 또는 backend_ 접두)로
// 명시. degrade_test.go가 이 표를 순회해 (도구, 원천, 등급) 전 조합을
// 강제 실패 주입으로 검증한다 — 표가 늘면 시험이 자동으로 넓어진다.
package tools

// SourceClass는 도구가 원천에 기대는 등급이다.
type SourceClass string

const (
	SourceRequired          SourceClass = "required"
	SourceSemanticAuxiliary SourceClass = "semantic_auxiliary"
	SourceOptional          SourceClass = "optional"
)

// ToolBackendDeps — 표면 도구 전수의 원천 의존(registry.go 생성자 인자
// 실측 2026-08-12와 1:1, 시험이 대조). 다중 원천 9종(4차 A-16)이 굵은 줄.
var ToolBackendDeps = map[string]map[string]SourceClass{
	"get_trace_spans":         {"ch": SourceRequired},
	"get_trace_activity":      {"ch": SourceRequired},
	"search_targets":          {"pg": SourceRequired},
	"describe_data_sources":   {}, // static catalog; no backend dependency
	"get_process_snapshot":    {"ch": SourceRequired},
	"get_trace_summaries":     {"ch": SourceRequired},
	"search_host_data":        {"ch": SourceRequired},
	"get_cluster_projections": {"ch": SourceRequired},
	// 신 표면 11종.
	"scan_metrics":        {"vm": SourceRequired, "pg": SourceSemanticAuxiliary}, // PG=counter 판별·한도 짝
	"read_timeseries":     {"vm": SourceRequired, "pg": SourceSemanticAuxiliary},
	"sample_logs":         {"ch": SourceRequired},
	"list_events":         {"ch": SourceRequired, "pg": SourceOptional}, // PG=K8s 정체 판별(kcm 합성 전제 — 실측 2b, 종전 주석 "표시명 해석"은 부정확)
	"describe_target":     {"pg": SourceRequired, "vm": SourceOptional}, // VM=한도 카탈로그 실측치
	"list_changes":        {"pg": SourceRequired},
	"compare_peers":       {"pg": SourceRequired, "vm": SourceRequired}, // 명단=PG·판정=VM
	"expand_topology":     {"pg": SourceRequired, "ch": SourceRequired}, // 위상=PG·호출 관측=CH
	"db_blocking":         {"ch": SourceRequired},
	"db_slow_queries":     {"ch": SourceRequired, "pg": SourceSemanticAuxiliary}, // PG=엔진·인벤토리
	"breakdown_endpoints": {"ch": SourceRequired},
	// 메타. vm·ch는 required가 아니라 aux다(2b 정정): 6b의 A6 선구현이
	// 이미 "실패 원천은 source_errors 명시 + 나머지 관측 유지"로 확정했고
	// (meta.go markErr — 대조의 반쪽이 빠져도 수집기 상태는 선다), 표의
	// 종전 required 선언은 그 구현과 모순이었다. PG(수집기 명부)만이
	// 이 도구의 성립 조건이다.
	"get_data_coverage": {"pg": SourceRequired, "ch": SourceSemanticAuxiliary, "vm": SourceSemanticAuxiliary},
	// 도메인 특화 3종.
	"get_processes":  {"vm": SourceRequired},
	"get_snmp_traps": {"ch": SourceRequired},
	"get_k8s_state":  {"vm": SourceRequired, "pg": SourceOptional},
}

// ToolsOn은 backend에 의존하는 도구 목록이다(§13.1 시험 목록의 기계
// 생성 표면). class가 빈 문자열이면 등급 무관 전부.
func ToolsOn(backend string, class SourceClass) []string {
	var out []string
	for tool, deps := range ToolBackendDeps {
		if c, ok := deps[backend]; ok && (class == "" || c == class) {
			out = append(out, tool)
		}
	}
	return out
}
