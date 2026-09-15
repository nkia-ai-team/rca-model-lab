// [3] 커버리지 격자 — 탐색 정책 §15.2의 결정론 부분. "꼼꼼하게"를
// 검사 가능한 구조로 바꾼다: 대상 × 관점 격자를 조사자의 실 도구 호출
// 로그에서 규칙이 계산하고, 빈 칸을 기록한다.
//
// v1은 계측 먼저(§14 ① 선례): 빈 칸은 비게이트 기록이다 — 실측으로
// 빈 칸 율을 본 뒤 보충 라운드·결정론 채움·특성화 계산(전부/일부·
// 혼자/또래)의 승격을 결정한다(§12 관문 원칙).
package pipeline

import (
	"encoding/json"
	"sort"
)

// ToolCallRecord는 어댑터가 기록한 실 도구 호출 하나다 — 격자의
// 결정론 입력. 오류 응답(무효 도구·중복 차단)은 커버리지가 아니므로
// 어댑터는 성공 호출만 기록한다.
type ToolCallRecord struct {
	Name string
	Args json.RawMessage
}

// Viewpoint는 격자의 관점 축이다(§15.2). v1은 3열 — 골든 시그널
// 4종은 지표 관점 하나로 묶는다(개별 시그널 분류는 지표명 매핑이
// 필요해 칸 배정표의 후속 확장 몫). 변경 관점은 [2]가 담당.
type Viewpoint string

const (
	ViewMetrics Viewpoint = "metrics" // scan_metrics가 일괄 주력(§15.4 ①, 과도기 표면 §7)
	ViewEvents  Viewpoint = "events"
	ViewLogs    Viewpoint = "logs"
)

// gridAssign은 칸 배정표다 — 어느 도구 호출이 어느 관점 칸을 채우는가.
// 정본은 도구 계약 §9(문서가 정본, 코드가 옮겨 적는 관계).
var gridAssign = map[string][]Viewpoint{
	"scan_metrics":    {ViewMetrics},
	"read_timeseries": {ViewMetrics},
	"compare_peers":   {ViewMetrics},
	// breakdown_endpoints는 지표 관점이다(재설계 §15 — 설계 때 빠뜨린
	// 배정을 §7.3 재주행 실측 후 채웠다). 원천은 트레이스지만 내놓는
	// 것은 처리 구간별 지연·실패율, 즉 골든 시그널이다 — "골든 시그널
	// 4종을 지표 관점 하나로 묶는다"는 이 표의 규칙과 같은 자리다.
	"breakdown_endpoints": {ViewMetrics},
	"list_events":         {ViewEvents},
	"get_snmp_traps":      {ViewEvents},
	"get_k8s_state":       {ViewEvents},
	"sample_logs":         {ViewLogs},
}

// gridViewpoints — 열 순서 고정(순회 결정론).
var gridViewpoints = []Viewpoint{ViewMetrics, ViewEvents, ViewLogs}

// GridGap은 빈 칸 하나다 — "이 대상의 이 관점은 조회조차 안 됐다".
type GridGap struct {
	TargetID  string
	Viewpoint Viewpoint
}

// CoverageGrid는 [3]의 커버리지 장부다. Filled는 대상별 채워진 관점
// (열 순서), Empty는 빈 칸 전부(대상·관점 순 정렬) — 완료 조건 "빈 칸
// 없음"의 감사 재료.
type CoverageGrid struct {
	Targets []string
	Filled  map[string][]Viewpoint
	Empty   []GridGap
}

// callTargets는 호출 인자에서 대상 식별자를 꺼낸다 — 실 도구는 "target"
// 통일, mock 세계(spike3)는 service/db/host/device 변형이 있고,
// read_timeseries(과도기 표면 §7)는 "targets" 복수 배열이다.
func callTargets(args json.RawMessage) []string {
	var m map[string]any
	if err := json.Unmarshal(args, &m); err != nil {
		return nil
	}
	for _, k := range []string{"target", "service", "db", "host", "device"} {
		if s, ok := m[k].(string); ok && s != "" {
			return []string{s}
		}
	}
	if list, ok := m["targets"].([]any); ok {
		var out []string
		for _, v := range list {
			if s, ok := v.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

// BuildGrid는 조사 대상 목록과 도구 호출 로그에서 격자를 계산한다.
func BuildGrid(targets []string, calls []ToolCallRecord) CoverageGrid {
	g := CoverageGrid{Filled: map[string][]Viewpoint{}}
	inScope := map[string]bool{}
	for _, t := range targets {
		if t == "" || inScope[t] {
			continue
		}
		inScope[t] = true
		g.Targets = append(g.Targets, t)
	}
	sort.Strings(g.Targets)

	filled := map[string]map[Viewpoint]bool{}
	for _, c := range calls {
		views := gridAssign[c.Name]
		if len(views) == 0 {
			continue // 격자 밖 도구(topology·meta·드릴다운 등)
		}
		for _, t := range callTargets(c.Args) {
			if !inScope[t] {
				continue
			}
			if filled[t] == nil {
				filled[t] = map[Viewpoint]bool{}
			}
			for _, v := range views {
				filled[t][v] = true
			}
		}
	}
	for _, t := range g.Targets {
		for _, v := range gridViewpoints {
			if filled[t][v] {
				g.Filled[t] = append(g.Filled[t], v)
			} else {
				g.Empty = append(g.Empty, GridGap{TargetID: t, Viewpoint: v})
			}
		}
	}
	return g
}
