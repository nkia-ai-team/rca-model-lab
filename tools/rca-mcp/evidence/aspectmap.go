// Aspect 사상표 — docs/spec-agent-structure.md §5.1의 "도구 × finding class"
// 표를 **코드 정본**으로 옮긴 것이다(§14-1 1b).
//
// **어휘의 정본은 스펙 표가 아니라 실물 봉투다.** 표를 그대로 옮기면 존재하지
// 않는 키를 읽어 nil이 되거나 projector가 값을 지어낸다(expand_topology 전례).
// 그래서 각 행은 evidence/testdata/envelope_*.json(라이브 캡처)과 도구의
// finding 생성 코드를 대조해 확정했고, 갈린 지점은 행마다 주석으로 남겼다.
//
// 행이 지는 계약 4가지(§5.1):
//
//	① Aspect        — 관점 축
//	② EntityKey 정본 — 개체를 유일하게 지목하는 열(복수 해소 = 영구 판정 불능)
//	③ 허용 Predicate — 그 class의 관측값에서 truth를 파생할 수 있는 술어만
//	④ Effect 산출   — Kind와 Observed/Baseline이 어느 봉투 키인가
package evidence

import (
	"sort"
	"strings"
)

// ClassRow는 사상표 한 행이다(도구 × finding class).
type ClassRow struct {
	Source ObservationSource
	// Class — 봉투 실물의 class 값. 도구마다 그 값이 실린 키가 다르다
	// (class·section·kind·signal·relation) — classKeyOf가 흡수한다.
	Class  string
	Aspect Aspect
	Kind   EffectKind
	// Allowed — 허용 Predicate(§5.1 열). 등록 시점은 도구 단위 합집합으로,
	// 판정 시점은 이 행으로 검사한다(2층 강제).
	Allowed []Predicate
	// EntityCols — EntityKey를 만드는 봉투 키들. 순서가 곧 조립 순서다.
	// 빈 목록은 "metric 계열 — Metric이 개체 식별을 겸한다"(§5.1 "—" 행).
	EntityCols []string
	// ObservedKey/BaselineKey — Effect의 원천 봉투 키. 빈 값은 원천 부재가
	// 의미인 자리다(appeared의 Baseline 등) — 0으로 채우지 않는다.
	ObservedKey string
	BaselineKey string
	// MagnitudeKey — 도구가 이미 판정 비율을 계산해 봉투에 실은 행에서만.
	// 하네스 재계산 금지(§5.1 "도구의 판정값 그대로")의 실물화다.
	MagnitudeKey string
	// MetricFixed — 판정 축 이름이 행마다 고정인 class(진입점 지연 등).
	// 빈 값이면 봉투의 metric 키에서 읽는다.
	MetricFixed string
	// Onset — 변화구간(ChangeFrom/To)을 산출할 수 있는 행인가(§5.1 가용성
	// 계약). false면 nil이 계약이며 조회 창으로 채우는 것은 금지다.
	Onset bool
	// OnsetFrom/OnsetTo — Onset=true인 행에서 시각을 읽을 봉투 키.
	OnsetFrom, OnsetTo string
	// Note — 스펙 표와 실물이 갈린 지점(있는 행만).
	Note string
}

// FindingClass는 레코드의 FindingClass 필드 값이다 — "<원천>/<class>".
// §8 다양성 축을 Aspect가 아니라 이 값으로 잡을 수 있게 하는 키다(C-5:
// db_slow_queries와 db_blocking은 Aspect가 같아도 이 값이 다르다).
func (r ClassRow) FindingClass() string { return string(r.Source) + "/" + r.Class }

// AllowsPredicate는 판정 시점의 class 층 검사다(§5.1 "허용 Predicate의 강제
// 지점은 2층" 중 ②). 허용하지 않으면 진리표 행 2와 같은 취급 —
// inconclusive이며 지지·반증 양쪽 금지다.
func (r ClassRow) AllowsPredicate(p Predicate) bool {
	for _, a := range r.Allowed {
		if a == p {
			return true
		}
	}
	return false
}

// aspectRows는 사상표 본체다. 행 키는 (Source, Class).
var aspectRows = []ClassRow{
	// ── scan_metrics ────────────────────────────────────────────────
	{
		Source: SrcScanMetrics, Class: "shifted", Aspect: AspectMetric, Kind: EffectRatio,
		Allowed: allPredicates(), ObservedKey: "current_median", BaselineKey: "baseline_median",
		Note: "봉투 z는 표시 클램프(z_capped 딱지)라 Magnitude로 승계하지 않는다 — " +
			"판정은 median 비율(스펙 표의 `z_capped`는 값 키가 아니라 클램프 여부 딱지다: 실측 정정).",
	},
	{
		Source: SrcScanMetrics, Class: "appeared", Aspect: AspectMetric, Kind: EffectCategorical,
		Allowed: existencePredicates(), ObservedKey: "representative",
		Note: "스펙 표는 Kind=count였다. 실물 representative는 gauge=중앙값 / counter=창 내 증가 합" +
			"(scan.go representative_kind)이라 밀리초와 건수가 한 정렬 버킷에 든다. " +
			"허용 Predicate가 present·absent뿐이라 Magnitude 부재로 정정해도 손실이 없다(B-13 채택).",
	},
	{
		Source: SrcScanMetrics, Class: "disappeared", Aspect: AspectMetric, Kind: EffectShare,
		Allowed: existencePredicates(), ObservedKey: "baseline_presence_ratio",
		Note: "현재값이 실물에 없다(소멸이 의미). scope(partial/total)는 Headline.",
	},

	// ── read_timeseries ─────────────────────────────────────────────
	{
		Source: SrcReadTimeseries, Class: "series", Aspect: AspectMetric, Kind: EffectRatio,
		Allowed: allPredicates(), ObservedKey: "stats.median", BaselineKey: "baseline_median",
		Onset: true, OnsetFrom: "onset.interval", OnsetTo: "onset.interval",
		Note: "실물 시계열 finding에는 class 키 자체가 없다(read.go) — 부재를 class \"series\"로 읽는다. " +
			"Observed는 중첩 키 stats.median.",
	},

	// ── list_events ─────────────────────────────────────────────────
	{
		Source: SrcListEvents, Class: "episode", Aspect: AspectEvent, Kind: EffectDuration,
		Allowed: allPredicates(), EntityCols: []string{"episode_id"}, ObservedKey: "duration_ms",
		Onset: true, OnsetFrom: "open_at|derived_open_at", OnsetTo: "close_at",
		Note: "duration_ms는 미해소 에피소드에서 문자열 \"미상\"이다(listevents.go) — 숫자가 아니면 " +
			"Observed·Magnitude 부재. 경계 4상태는 Headline.",
	},
	{
		Source: SrcListEvents, Class: "unpaired", Aspect: AspectEvent, Kind: EffectCount,
		Allowed: existencePredicates(), EntityCols: []string{"detector", "reason", "at"},
		Onset: true, OnsetFrom: "at", OnsetTo: "at",
		Note: "episode_id 없는 단건 — Observed는 봉투 키가 아니라 1 고정(개체 1건).",
	},
	{
		Source: SrcListEvents, Class: "k8s", Aspect: AspectEvent, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"namespace", "kind", "name", "reason"},
		ObservedKey: "count", Onset: true, OnsetFrom: "first_at", OnsetTo: "last_at",
		Note: "kcm 이벤트는 first_at/last_at을 실제로 싣는다(B-12 과잉 금지 지적 채택 — " +
			"이벤트성이라 first_at이 문자 그대로 이상의 시작이다).",
	},

	// ── sample_logs (map 4구획 + grep) ──────────────────────────────
	// 스펙은 "log (sample_logs, map)" 1행이지만 실물 class는 section 4종이다.
	// 행 단위 = 도구×class 선언을 지키려면 4행이어야 한다(정렬·다양성 축이
	// 구획을 구분해야 "새 템플릿 등장"과 "만성 최빈"이 같은 등급이 되지 않는다).
	{
		Source: SrcSampleLogs, Class: "new_in_window", Aspect: AspectLog, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"template_id"},
		ObservedKey: "current_count", BaselineKey: "baseline_count",
		Onset: true, OnsetFrom: "first_at", OnsetTo: "last_at",
		Note: "신규 템플릿에서만 first_at이 \"이상의 시작\" 의미다(§5.1) — 나머지 3구획은 Onset=false.",
	},
	{
		Source: SrcSampleLogs, Class: "surged", Aspect: AspectLog, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"template_id"},
		ObservedKey: "current_count", BaselineKey: "baseline_count",
	},
	{
		Source: SrcSampleLogs, Class: "disappeared", Aspect: AspectLog, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"template_id"},
		ObservedKey: "current_count", BaselineKey: "baseline_count",
		Note: "current_count=0이 소멸의 의미다 — 0으로 채운 것이 아니라 실물 키다.",
	},
	{
		Source: SrcSampleLogs, Class: "frequent", Aspect: AspectLog, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"template_id"},
		ObservedKey: "current_count", BaselineKey: "baseline_count",
	},
	{
		Source: SrcSampleLogs, Class: "grep", Aspect: AspectLog, Kind: EffectCount,
		Allowed: existencePredicates(), EntityCols: []string{"at", "bh"},
		Onset: true, OnsetFrom: "at", OnsetTo: "at",
		Note: "스펙의 EntityKey `at+bh`에서 bh는 **finding 키가 아니다** — ref " +
			"`ch:lucida_logs_local:<target>:<ts>:<bh>`의 마지막 조각에서만 회수된다(실측 정정). " +
			"Observed도 봉투 키가 아니라 1 고정이다: 실물 grep finding은 매치 '줄' 단건이고 " +
			"매치 총수는 query_scope가 진다. relation=before/after(맥락 줄)는 meta다.",
	},

	// ── db_slow_queries ─────────────────────────────────────────────
	{
		Source: SrcDBSlowQueries, Class: "sql", Aspect: AspectDB, Kind: EffectRatio,
		Allowed: allPredicates(), EntityCols: []string{"sql_key"},
		ObservedKey: "latency_ms_p50", BaselineKey: "baseline_latency_ms_p50",
		MagnitudeKey: "ratio_p50", MetricFixed: "latency_ms_p50",
		Note: "**Onset=false**(B-12 과잉 부여 지적 채택) — first_seen/last_seen은 top-SQL 수집 폴의 " +
			"최초·최종 시각이고 봉투 자신이 \"폴 시각이며 쿼리 실행 시각이 아니다\"라고 경고한다. " +
			"만성 쿼리가 onset으로 오인돼 시간 게이트를 그냥 통과하는 것을 막는다. " +
			"Magnitude는 도구가 계산한 ratio_p50을 승계한다(하네스 재계산 금지).",
	},

	// ── db_blocking ─────────────────────────────────────────────────
	{
		Source: SrcDBBlocking, Class: "event", Aspect: AspectDB, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"$target", "first_seen"},
		ObservedKey: "blocked_peak", MetricFixed: "blocked_peak",
		Onset: true, OnsetFrom: "first_seen", OnsetTo: "last_seen",
		Note: "실물 finding에 class 키가 없다 — 봉투의 finding 자체가 사건 1건이라 class \"event\"로 읽는다. " +
			"EntityKey는 DB×구간 시작 시각($target = 도구 호출 대상 인자)이며 **세션은 키가 아니라 속성**이다. " +
			"판정은 규모 단독(지속 절은 실측이 무효화).",
	},

	// ── breakdown_endpoints (판정 축별 2행) ─────────────────────────
	// 스펙 1행이 Kind를 duration·share 둘로 선언했다 — Kind는 레코드당 하나이고
	// 정렬이 Kind 간 고정 우선순위를 쓰므로 축별로 쪼갠다(B-13 채택).
	{
		Source: SrcBreakdownEndpoints, Class: "endpoint_latency", Aspect: AspectEndpoint, Kind: EffectDuration,
		Allowed: allPredicates(), EntityCols: []string{"section", "kind", "axis", "label"},
		ObservedKey: "p50_ms", BaselineKey: "baseline.p50_ms", MetricFixed: "p50_ms",
		Note: "EntityKey 4튜플 — 같은 (section,label)이 axis(by_name/by_target)·kind별로 복수 행이라 " +
			"2튜플로는 selector가 복수 해소된다(B-13 실측 채택).",
	},
	{
		Source: SrcBreakdownEndpoints, Class: "endpoint_failure", Aspect: AspectEndpoint, Kind: EffectShare,
		Allowed: allPredicates(), EntityCols: []string{"section", "kind", "axis", "label"},
		ObservedKey: "failed_rate", BaselineKey: "baseline.failed_rate", MetricFixed: "failed_rate",
		Note: "failed_rate는 표본 0(count=0)이면 봉투에 없다 — 그때는 레코드를 만들지 않는다.",
	},

	// ── compare_peers ───────────────────────────────────────────────
	{
		Source: SrcComparePeers, Class: "self", Aspect: AspectPeer, Kind: EffectCategorical,
		Allowed: existenceDirectionPredicates(), EntityCols: []string{"target_id"},
		MetricFixed: "deviating", Onset: true, OnsetFrom: "onset.interval", OnsetTo: "onset.interval",
		Note: "z 폐지 — 봉투에 z도 MAD 분모도 없다. magnitude finding은 도구가 " +
			"\"판정에 쓰지 않는다\"고 금지 선언한 참고값이라 Effect로 승계하지 않는다.",
	},
	{
		Source: SrcComparePeers, Class: "peer", Aspect: AspectPeer, Kind: EffectCategorical,
		Allowed: existenceDirectionPredicates(), EntityCols: []string{"target_id"},
		MetricFixed: "deviating", Onset: true, OnsetFrom: "onset.interval", OnsetTo: "onset.interval",
	},

	// ── get_processes ───────────────────────────────────────────────
	{
		Source: SrcProcesses, Class: "process", Aspect: AspectProcess, Kind: EffectShare,
		Allowed: allPredicates(), EntityCols: []string{"process", "pid"},
		ObservedKey: "share", MetricFixed: "share",
		Note: "스펙의 EntityKey `cmd+pid`에서 **cmd는 봉투에 없다** — 실물은 `avg by(name,pid)`라 " +
			"finding 키가 process(name)·pid뿐이다(B-13 실측 채택). PID 재사용 때문에 동일성 범위는 조회 창이다. " +
			"스펙이 선언한 Kind 복수(share·count) 중 count 축은 버렸다: 그 자리의 실물 키 avg는 " +
			"건수가 아니라 게이지 평균이라 count로 사상하면 단위가 섞인다. avg는 Headline에 싣는다.",
	},

	// ── get_snmp_traps ──────────────────────────────────────────────
	{
		Source: SrcSNMPTraps, Class: "trap", Aspect: AspectEvent, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"trap_type", "trap_oid", "$target"},
		ObservedKey: "count", MetricFixed: "count",
		Onset: true, OnsetFrom: "first_at", OnsetTo: "last_at",
		Note: "스펙의 \"trap OID+대상\"은 실물 GROUP BY (trap_type, trap_oid)보다 좁다 — trap_type을 넣지 않으면 " +
			"복수 해소된다(B-13 채택). Onset은 실물 min/max(received_at)이 원천이다(B-12 채택).",
	},

	// ── get_k8s_state (증가량 1 + 상태 게이지 3) ────────────────────
	{
		Source: SrcK8sState, Class: "restart_increase", Aspect: AspectEvent, Kind: EffectCount,
		Allowed: allPredicates(), EntityCols: []string{"namespace", "pod", "signal"},
		ObservedKey: "value", MetricFixed: "restart_increase",
		Note: "4신호 중 유일한 증가량(카운터 차분).",
	},
	{
		Source: SrcK8sState, Class: "crash_loop_back_off", Aspect: AspectEvent, Kind: EffectCount,
		Allowed: existencePredicates(), EntityCols: []string{"namespace", "pod", "signal"},
		ObservedKey: "value", MetricFixed: "crash_loop_back_off",
		Note: "실물은 max_over_time > 0의 게이지 불리언이고 sum by(namespace,pod)라 컨테이너 2개 동시 " +
			"waiting에 2가 나온다 — 판정은 \"1\"이 아니라 \"0 초과\"다(B-13 채택). 건수·크기 술어는 원천 불가.",
	},
	{
		Source: SrcK8sState, Class: "oom_killed", Aspect: AspectEvent, Kind: EffectCount,
		Allowed: existencePredicates(), EntityCols: []string{"namespace", "pod", "signal"},
		ObservedKey: "value", MetricFixed: "oom_killed",
	},
	{
		Source: SrcK8sState, Class: "waiting", Aspect: AspectEvent, Kind: EffectCount,
		Allowed: existencePredicates(), EntityCols: []string{"namespace", "pod", "signal"},
		ObservedKey: "value", MetricFixed: "waiting",
	},

	// ── change ([2] 합성 · list_changes) ────────────────────────────
	{
		Source: SrcChange, Class: "change", Aspect: AspectChange, Kind: EffectCount,
		Allowed: existencePredicates(), EntityCols: []string{"kind", "target_id", "at"},
		MetricFixed: "change", Onset: true, OnsetFrom: "at", OnsetTo: "at",
		Note: "변경 시각은 Window.ChangeFrom/To에 싣는다 — Observed는 *float64라 시각을 담을 수 없다. " +
			"Observed는 1 고정(변경 1건). 실행 채널은 list_changes다(ExecutableTool — B-15). " +
			"**두 경로의 봉투 키가 다르다**(실측): [2] 합성 봉투에는 target_id가 있고 list_changes " +
			"봉투에는 없다(창 전역 조회라 대상이 필터가 아니라 정렬 힌트 — listchanges.go). " +
			"같은 canonical 키가 되도록 없으면 호출 대상으로 채운다.",
	},
}

// metaClasses는 사상표 행이 없는 실물 class다 — RecordKind=meta로 적재하고
// 술어 판정 대상에서 뺀다(§5.1 "술어 불가 class"). **명시 열거가 계약이다**:
// 실물 class 전수가 행 또는 이 목록 중 하나에 속해야 하며, 골든 정합 테스트가
// 그 전수성을 검사한다(모르는 class는 조용히 버려지지 않고 시험이 깨진다).
var metaClasses = map[ObservationSource][]string{
	SrcScanMetrics:        {"counts", "disappearance_concurrency"},
	SrcReadTimeseries:     {"read_meta", "onset_timeline"},
	SrcListEvents:         {"summary_meta"},
	SrcSampleLogs:         {"summary_meta", "before", "after"},
	SrcDBSlowQueries:      {"coverage", "sql_text_full", "sql_plan"},
	SrcDBBlocking:         {"coverage"},
	SrcBreakdownEndpoints: {"coverage"},
	SrcComparePeers:       {"verdict", "peer_set", "peers_folded", "peers_excluded", "compare_meta"},
	SrcK8sState:           {},
	SrcProcesses:          {},
	SrcSNMPTraps:          {},
	SrcChange:             {"summary_meta"},
}

// predicateFreeTools는 사상표 행이 아예 없는 도구다(§5.1 "술어 불가 도구").
// 위상·메타·커버리지는 술어의 판정 대상이 아니라 등록 어휘(§6.2-1ⓐ)·자격
// 게이트(§5.5)의 재료다. 봉투는 evidence store에 적재되고 ref도 발급되지만
// index 레코드는 만들지 않는다 — Provenance.Source의 어휘가 사상표에 행이 있는
// 원천으로 닫혀 있기 때문이다(ObservationSource에 이 셋이 없는 것이 그 계약).
var predicateFreeTools = map[string]bool{
	"describe_target": true, "expand_topology": true, "get_data_coverage": true,
}

// IsPredicateFreeTool — 그 도구 이름이 술어 불가 도구인가.
func IsPredicateFreeTool(tool string) bool { return predicateFreeTools[tool] }

// classRows는 **봉투 실물의 class 값**을 사상표 행으로 푼다. 셋 중 하나다:
//
//	· 1:1 — 대부분(class 값이 곧 행 이름)
//	· 이름만 다름 — sample_logs grep의 실물 class 값은 relation="match"다
//	· 1:N — breakdown_endpoints는 봉투 finding 하나에 판정 축이 둘(지연·실패율)
//	  이라 레코드가 둘로 갈린다. class 값(구획)과 행 이름이 다른 축이다.
//
// 이 함수가 finding 투영과 절단 단위(query_scope) 양쪽의 유일한 해소 경로다 —
// 둘이 각자 표를 가지면 갈린다.
func classRows(src ObservationSource, class string) []ClassRow {
	class = canonClass(src, class)
	if src == SrcBreakdownEndpoints {
		switch class {
		case "entry", "step", "egress":
			var out []ClassRow
			for _, c := range []string{"endpoint_latency", "endpoint_failure"} {
				if r, ok := Row(src, c); ok {
					out = append(out, r)
				}
			}
			return out
		}
	}
	if r, ok := Row(src, class); ok {
		return []ClassRow{r}
	}
	return nil
}

// canonClass는 봉투 class 값의 표기를 절단 단위 이름과 같은 어휘로 맞춘다.
// grep은 finding에 relation="match", 절단 단위에는 "grep"으로 실려 있어 그대로
// 두면 finding이 자기 구획의 절단 수를 못 찾는다(절단 사실 누락).
func canonClass(src ObservationSource, class string) string {
	if src == SrcSampleLogs && class == "match" {
		return "grep"
	}
	return class
}

// rowIndex는 (Source, Class) → 행.
var rowIndex = func() map[[2]string]ClassRow {
	m := map[[2]string]ClassRow{}
	for _, r := range aspectRows {
		m[[2]string{string(r.Source), r.Class}] = r
	}
	return m
}()

// Row는 사상표 행을 찾는다.
func Row(src ObservationSource, class string) (ClassRow, bool) {
	r, ok := rowIndex[[2]string{string(src), class}]
	return r, ok
}

// RowFor는 레코드의 FindingClass 값("<원천>/<class>")으로 행을 찾는다 —
// 판정 시점의 class 층 검사(§5.1 2층 강제 중 ②, §6 진리표 행 2)는 레코드가
// 들고 있는 이 문자열 하나에서 출발한다.
func RowFor(findingClass string) (ClassRow, bool) {
	src, class, ok := strings.Cut(findingClass, "/")
	if !ok {
		return ClassRow{}, false
	}
	return Row(ObservationSource(src), class)
}

// Rows는 사상표 전 행을 돌려준다(감사·문서 생성·전수성 검사용).
func Rows() []ClassRow { return append([]ClassRow(nil), aspectRows...) }

// IsMetaClass는 그 class가 명시적으로 meta로 분류됐는지다.
func IsMetaClass(src ObservationSource, class string) bool {
	for _, c := range metaClasses[src] {
		if c == class {
			return true
		}
	}
	return false
}

// AllowedUnion은 그 도구 전 행의 허용 Predicate 합집합이다 — 등록 시점
// 검사(규칙 1ⓓ)가 쓰는 값이다. compare_peers + magnitude_ge처럼 도구 전체에서
// 원천 불가한 조합이 여기서 죽는다.
func AllowedUnion(src ObservationSource) []Predicate {
	seen := map[Predicate]bool{}
	for _, r := range aspectRows {
		if r.Source != src {
			continue
		}
		for _, p := range r.Allowed {
			seen[p] = true
		}
	}
	out := make([]Predicate, 0, len(seen))
	for p := range seen {
		out = append(out, p)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// AspectOf는 그 원천의 Aspect 집합이다. 한 도구가 Aspect를 하나만 갖지는
// 않는다(list_events는 event 단일이지만 sample_logs는 log 단일 — 확장 대비).
func AspectOf(src ObservationSource) []Aspect {
	seen := map[Aspect]bool{}
	for _, r := range aspectRows {
		if r.Source == src {
			seen[r.Aspect] = true
		}
	}
	out := make([]Aspect, 0, len(seen))
	for a := range seen {
		out = append(out, a)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}
