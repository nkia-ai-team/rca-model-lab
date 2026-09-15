// evidence — 관측 레코드(evidence index)의 타입 기층이다.
// 정본: docs/spec-agent-structure.md §5.1(레코드 스키마) · §6.0(관측 키).
//
// 이 파일은 "같은 관측인가"를 판정하는 키를 정의한다. 명제 장부(§6.0)의
// 명제 키, supersede(§5.7)의 selector, 술어 해소가 전부 이 키 하나를 쓴다 —
// 셋이 각자 키를 갖지 않게 하는 것이 이 타입의 존재 이유다.
package evidence

import "strings"

// WindowClass는 관측 키의 창 축이다 — 실제 시각이 아니라 창의 종류다
// (§6.0: 키에 드는 것은 창 enum. 실제 창은 레코드의 Window 필드).
// 실 창→클래스 사상은 projector의 몫(§14-1 1b)이다.
type WindowClass string

const (
	WindowFull        WindowClass = "full"
	WindowOnsetNarrow WindowClass = "onset_narrow" // ±5분
)

// ValidWindowClass — 닫힌 2값(§6 SignalPred.Window).
func ValidWindowClass(w WindowClass) bool {
	return w == WindowFull || w == WindowOnsetNarrow
}

// ObservationSource는 레코드의 관측 원천 이름이다 — §5.1 사상표에 행이
// 있는 도구 + 합성 원천 `change`. `SignalPred.Tool`의 허용 어휘가 이
// 집합이다(§5.1 "술어 불가 도구" 제외: describe_target·expand_topology·
// get_data_coverage는 행이 없어 여기 없다).
//
// **원천과 실행 채널의 분리** (B-15 대조): `change`는 관측 원천이지 도구가
// 아니다 — registry(tools/registry.go)에 `change`라는 도구는 없고
// `list_changes`만 있다. 원천 이름을 그대로 도구 이름으로 조립하면
// prospective change 술어가 실행 불가가 되므로, 실행 채널은
// ExecutableTool로 따로 해소한다.
type ObservationSource string

const (
	SrcScanMetrics        ObservationSource = "scan_metrics"
	SrcReadTimeseries     ObservationSource = "read_timeseries"
	SrcListEvents         ObservationSource = "list_events"
	SrcSampleLogs         ObservationSource = "sample_logs"
	SrcDBSlowQueries      ObservationSource = "db_slow_queries"
	SrcDBBlocking         ObservationSource = "db_blocking"
	SrcBreakdownEndpoints ObservationSource = "breakdown_endpoints"
	SrcComparePeers       ObservationSource = "compare_peers"
	SrcProcesses          ObservationSource = "get_processes"
	SrcSNMPTraps          ObservationSource = "get_snmp_traps"
	SrcK8sState           ObservationSource = "get_k8s_state"
	// SrcChange — [2] 변경 조회와 list_changes가 같은 자리에 투영되는
	// 합성 원천(§5.1 change 행: Provenance.Tool 고정값 = "change").
	SrcChange ObservationSource = "change"
)

// executableTool — 원천 → 실제 호출 가능한 registry 도구 이름.
var executableTool = map[ObservationSource]string{
	SrcScanMetrics:        "scan_metrics",
	SrcReadTimeseries:     "read_timeseries",
	SrcListEvents:         "list_events",
	SrcSampleLogs:         "sample_logs",
	SrcDBSlowQueries:      "db_slow_queries",
	SrcDBBlocking:         "db_blocking",
	SrcBreakdownEndpoints: "breakdown_endpoints",
	SrcComparePeers:       "compare_peers",
	SrcProcesses:          "get_processes",
	SrcSNMPTraps:          "get_snmp_traps",
	SrcK8sState:           "get_k8s_state",
	SrcChange:             "list_changes", // 합성 원천의 재조회 채널
}

// ValidSource — 사상표에 행이 있는 원천인가(등록 규칙 1ⓑ의 어휘 검사 재료).
func ValidSource(s ObservationSource) bool {
	_, ok := executableTool[s]
	return ok
}

// ExecutableTool은 원천을 실제 도구 호출 이름으로 해소한다. 하네스가
// PredID에서 도구·인자를 조립할 때(§7.1) 반드시 이 함수를 지난다.
func ExecutableTool(s ObservationSource) (string, bool) {
	t, ok := executableTool[s]
	return t, ok
}

// MustExecutableTool은 어휘 안의 원천이 확실한 자리(코드 상수)에서 쓴다 —
// 해소 실패는 표와 코드가 갈렸다는 뜻이라 조용히 빈 문자열로 흐르면 안 된다.
func MustExecutableTool(s ObservationSource) string {
	t, ok := executableTool[s]
	if !ok {
		panic("evidence: 원천 " + string(s) + "의 실행 채널이 없다(사상표와 registry가 갈림)")
	}
	return t
}

// EntityAny는 EntityKey 축의 와일드카드다 — "이 범위의 개체 전역"을
// 뜻한다. 개체 레코드가 아니라 조회 범위 레코드(query_scope, §5.1 계약 1)가
// 등록하는 값이며, 존재하지 않는 개체를 지목한 absent 술어를 범위 실측으로
// 해소하는 유일한 경로다(C-3 대조 — 정확 일치 lookup만으로는 그 술어가
// 영구 미판정이 되어 등록 규칙 3의 반증 재제안 차단이 뚫린다).
const EntityAny = "*"

// ObservationKey는 관측의 정본 키다(§6.0).
//
// **Predicate와 Threshold는 키에 없다** — 관측값이 한 번 실측되면 6종
// Predicate × 임의 Threshold의 진리값은 즉시 파생 계산되므로, 연산자만
// 바꾼 변형은 새 명제가 아니다(§6.0: 반증 가설이 연산자를 바꿔 무한
// 재등록되는 우회의 봉쇄).
//
// 필드는 전부 비교 가능한 값 타입이다 — 이 구조체 자체가 map 키로 쓰인다
// (명제 장부·supersede head 색인·중복 dedup).
type ObservationKey struct {
	Source    ObservationSource // §5.1 Provenance.Tool / SignalPred.Tool
	TargetID  string            // 위상 어휘(§6.2-1ⓐ)의 canonical target_id
	Aspect    Aspect            // §5.1 Aspect 열
	Metric    string            // 판정 축. metric 계열에서는 개체 식별을 겸한다
	EntityKey string            // §5.1 사상표 EntityKey 정본 열. EntityAny면 범위 행
	Window    WindowClass
}

// Canonical은 표기 흔들림을 흡수한 키를 돌려준다(§6.0 "정규화를 키 계약에
// 포함한다 — 표기 흔들림도 새 키가 아니다").
//
// 규칙: 앞뒤 공백 제거 · 내부 연속 공백 1칸으로 접기 · 소문자화.
// **별칭(alias) 정리는 여기 없다** — 별칭표는 사상표와 같은 자리에서
// 확정되므로(§6.0) 표가 실물로 확정되는 1b 전에 넣으면 근거 없는 사상이
// 된다. 별칭이 확정되면 이 함수 한 곳만 고친다.
func (k ObservationKey) Canonical() ObservationKey {
	k.Source = ObservationSource(canon(string(k.Source)))
	k.TargetID = canon(k.TargetID)
	k.Aspect = Aspect(canon(string(k.Aspect)))
	k.Metric = canon(k.Metric)
	if k.EntityKey != EntityAny {
		k.EntityKey = canon(k.EntityKey)
	}
	k.Window = WindowClass(canon(string(k.Window)))
	return k
}

// Scope는 개체 축을 와일드카드로 연 범위 키다 — query_scope 레코드가
// 등록하는 행의 키.
func (k ObservationKey) Scope() ObservationKey {
	k = k.Canonical()
	k.EntityKey = EntityAny
	return k
}

// Covers는 이 키가 other를 덮는지다. 같은 키면 참이고, 범위 키(EntityAny)는
// 개체 축만 다른 모든 키를 덮는다. 명제 장부 조회가 "정확 일치 → 없으면
// 덮는 범위 행"의 2단으로 동작하는 근거다(C-3).
//
// 절단된 조회(Omitted>0)의 query_scope는 범위 행을 등록하지 못한다 —
// 그 판정은 레코드를 장부에 넣는 쪽(§14-1 1c)의 몫이고, 여기서는 키 관계만
// 정의한다.
// **개체 축은 행마다 다르다**(1b 실측): 사상표의 metric 계열 행은 EntityKey가
// 비어 있고 Metric이 개체 식별을 겸한다(§5.1 "—" 행). 그런 행의 범위 레코드는
// Metric 축을 열어야 개체 행을 덮으므로, 두 축 어느 쪽이든 EntityAny면
// 와일드카드로 읽는다. EntityKey 축만 열던 종전 계약으로는 scan_metrics·
// sample_logs·list_events의 범위 행이 자기 개체 행을 하나도 덮지 못했다.
func (k ObservationKey) Covers(other ObservationKey) bool {
	a, b := k.Canonical(), other.Canonical()
	if a.EntityKey == EntityAny {
		a.EntityKey, b.EntityKey = "", ""
	}
	if a.Metric == EntityAny {
		a.Metric, b.Metric = "", ""
	}
	return a == b
}

// String은 로그·이벤트 기록용 안정 표기다(판정에 쓰지 않는다 — 판정은
// 구조체 동등 비교).
func (k ObservationKey) String() string {
	c := k.Canonical()
	return strings.Join([]string{
		string(c.Source), c.TargetID, string(c.Aspect), c.Metric, c.EntityKey, string(c.Window),
	}, "|")
}

func canon(s string) string { return strings.ToLower(strings.Join(strings.Fields(s), " ")) }
