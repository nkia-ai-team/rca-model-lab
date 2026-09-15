// Package seed 는 입력 계약 IncidentSeed 의 거울이다 — RCA의 출발점
// [1] Triage 가 받는 인시던트 박제(snapshot). 정본은 lucida-next
// `backend/services/ai/features/operator/rca/model/signals.go` 이고
// 스냅샷 기준은 docs/ref-rca-io-contract.md §1 (커밋 90a163f71). 이
// 프로젝트는 계약을 정의하지 않고 추적만 한다 — 필드 재정의·삭제 금지.
//
// 거울 범위는 seed 가 담는 것까지다: IncidentSeed / SeedMember /
// Graph(토폴로지) / KinTarget. 도구 응답으로 오는 신호 타입(Series·
// LogLine·ChangeEvent 등)은 도구 계약(spec-agent-tools.md)의 몫이라
// 여기 없다.
//
// canonical entity = target_id. seed 멤버·토폴로지 노드/엣지·근거
// 참조가 모두 같은 키를 쓴다(계약 공통 원칙).
package seed

import "time"

// IncidentSeed 는 RCA 출발점(인시던트 박제)이다.
type IncidentSeed struct {
	IncidentID     string
	ClusterID      string
	Title          string
	Severity       string // warning|critical
	Status         string // active|closed
	MainEventID    string
	MainTargetID   string
	MainService    string
	MainMetric     string
	MainObserved   *float64
	MainBaseline   *float64
	MainDirection  string // up|down
	FirstEventAt   time.Time
	LastEventAt    time.Time
	BlastTargetIDs []string
	BlastServices  []string
	Members        []SeedMember // incident_members 스냅샷(원인 후보)
	Topology       Graph        // 토폴로지 인접 subgraph
	HostKin        []KinTarget  // 물리 호스트 친족(없으면 nil)
}

// SeedMember 는 구성 멤버(원인 후보) 1행이다.
type SeedMember struct {
	MemberKey   string
	EventID     string
	Class       string // anomaly|context|prediction
	Severity    string // critical|warning|caution|cleared|info
	Detector    string // stream-anomaly|log-anomaly|trace-anomaly|forecast|alarm-bridge|change-detect
	Reason      string // 탐지 사유(metric_anomaly, config_change …)
	TargetID    string
	ServiceName string
	Metric      string
	Direction   string // up|down
	Baseline    *float64
	Observed    *float64
	OccurredAt  time.Time
}

// KinTarget 은 server_resource 멤버의 물리 호스트 친족이다 — "리소스
// 개별 문제냐 호스트 레벨 원인이냐"를 같은 호스트 축에서 보는 추가
// 조사 대상(토폴로지·cohort 가 못 잡는 축).
type KinTarget struct {
	TargetID     string // 호스트 또는 형제 리소스 target_id
	Relation     string // host|sibling
	ResourceKind string // 형제 리소스 종류(cpu/memory/filesystem/…) — host 는 ""
}

// Graph 는 토폴로지 인접이다. JSON 태그까지 정본과 동일하게 유지한다
// (incidents.topology projection 을 그대로 역직렬화할 수 있어야 함).
type Graph struct {
	Nodes []GraphNode `json:"nodes"`
	Edges []Edge      `json:"edges"`
}

// GraphNode 는 토폴로지 노드 1개다. Domain 은 후보 도메인 확장(설계
// §3 — 증상 도메인 + topology 1~2홉)의 입력이 된다.
type GraphNode struct {
	ID       string `json:"id"`       // 토폴로지 node id(원본 식별)
	TargetID string `json:"targetId"` // canonical entity = target_id
	Label    string `json:"label"`
	Kind     string `json:"kind"`
	Domain   string `json:"domain"`
}

// Edge 는 토폴로지/전파 엣지 1개다(source/target = target_id).
type Edge struct {
	Source    string `json:"source"`
	Target    string `json:"target"`
	Kind      string `json:"kind,omitempty"`
	Direction string `json:"direction,omitempty"`
	// 호출 메트릭(apm_call/apm_db 엣지) — 의존성 실패 신호.
	ErrPct float64 `json:"err_pct,omitempty"` // 에러율(%)
	P99Ms  float64 `json:"p99_ms,omitempty"`  // p99 지연(ms)
	Calls  int     `json:"calls,omitempty"`   // 총 호출 수
}
