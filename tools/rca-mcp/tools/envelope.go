// 도구 응답 봉투 v2 (도구 계약 §2) — 모든 실 도구가 공유하는 응답
// 형태. status 3값 + no_data_reason 5구분 + finding별 refs 결합.
package tools

import (
	"fmt"
	"time"
)

// chParseTS는 CH HTTP 응답의 시각 문자열을 해석한다(dbBlkParseTS 일반화 —
// §14-5 5c 결정 ①이 도착 수평선 파싱을 필요로 해 공용 승격).
func chParseTS(v any) (time.Time, bool) {
	s := fmt.Sprint(v)
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02 15:04:05", time.RFC3339} {
		if t, err := time.ParseInLocation(layout, s, time.UTC); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// horizonObservedRange는 §14-5 5c 결정 ①의 observed_range다 —
// To = min(창 끝, 도착 수평선). 조회 창을 그대로 싣는 것은 미도착 데이터를
// 완전 커버로 주장하는 fail-open이라 금지이며, 수평선이 창 시작 이전이거나
// epoch(빈 스트림의 max)면 관측 범위 주장을 하지 않는다(nil → CollectLag
// unknown 유지, fail-closed).
func horizonObservedRange(from, to, horizon time.Time) *TimeRange {
	if horizon.IsZero() || horizon.Unix() <= 0 {
		return nil
	}
	end := to
	if horizon.Before(end) {
		end = horizon
	}
	if !end.After(from) {
		return nil
	}
	return &TimeRange{From: from.UTC(), To: end.UTC()}
}

// Envelope는 도구 계약 §2의 응답 봉투다. status가 no_data면
// NoDataReason이 필수다 — collector_gap은 배제 근거로 쓰면 안 되는
// 결손이라 구분 없이는 ruled_out과 missing_evidence의 경계가 무너진다.
type Envelope struct {
	Status          string     `json:"status"` // normal | anomalous | no_data
	NoDataReason    string     `json:"no_data_reason,omitempty"`
	AssessmentBasis string     `json:"assessment_basis,omitempty"`
	Summary         string     `json:"summary"`
	Findings        []Finding  `json:"findings,omitempty"`
	ObservedRange   *TimeRange `json:"observed_range,omitempty"`
	Truncated       bool       `json:"truncated,omitempty"`
	// DegradedSources — **의미 강등** 원천의 분류 토큰(§15.2-3
	// semantic_auxiliary 결손, §14-7 2b D-3 — additive §12-5 예외).
	// 이 봉투의 관측이 "값은 있는데 해석 규칙이 결손"인 상태(예: counter
	// 판별 실패 → 전부 gauge 취급)에서만 싣는다 — projector가 파생 finding
	// 레코드를 기계 ConfLow로 강등한다(문구만으로는 결정론 층이 장님이라
	// normal이 반증 자격을 얻는 유출이 있었다). optional 원천 결손(부가
	// 필드 부재 — 관측 의미 불변)은 여기 싣지 않는다.
	DegradedSources map[string]string `json:"degraded_sources,omitempty"`

	// QueryTruncated — **질의 계층** 절단(§14-7 D-1, additive §12-5 예외).
	// CH 캡 절단처럼 절단 전 총량(Total)이 미상이라 Scopes로 셈할 수 없는
	// 절단만 싣는다. Truncated(표시 쿼터 절단 — 조회는 완전, Scopes가
	// 구획별로 정확히 셈)와 반드시 구분한다: 이걸 Truncated에 섞으면
	// 표시만 잘린 봉투의 정당한 observed_zero까지 강등된다. projector가
	// 이 필드만 레코드 완전성(가용성·신뢰도·절단 표식)에 합류시킨다.
	QueryTruncated bool     `json:"query_truncated,omitempty"`
	Refs           []string `json:"refs,omitempty"`
	// Scopes — 논리 조회 단위별 절단 계약(spec-agent-structure §5.1 계약 2의
	// additive 확장, §14-1 1b). **기존 필드의 의미는 그대로다** — 추가만 한다
	// (표면 동결 §12-5의 additive 예외).
	//
	// 왜 필요한가: 종전 봉투는 `Truncated bool` 하나뿐이라 "몇 개를 못 봤는지"의
	// 원천이 없었고, 0건 조회는 Findings가 비어 레코드가 아예 생기지 않았다.
	// 그 둘이 없으면 §5.1의 observed_zero·absent 판정과 §5.5 "절단되지 않음"
	// 자격이 전부 평가 불능이다. Summary 문자열 파싱은 금지이므로 구조화한다.
	Scopes []QueryScope `json:"scopes,omitempty"`

	// Backend·BackendDetail — no_data_reason=backend_error일 때의 분류
	// 토큰(§15.2-3, 6b). 어느 저장소가 어떤 계열로 실패했는지만 싣고,
	// 원문 오류 문자열은 로그·trace에만 남는다(§15.3-2).
	Backend       string `json:"backend,omitempty"`
	BackendDetail string `json:"backend_detail,omitempty"`
}

// QueryScope는 한 논리 조회 단위의 총량·반환·누락이다. 단위는 봉투가
// **아니라** 절단이 실제로 일어나는 구획이다(§5.1 계약 1, 4차 A-7:
// get_k8s_state 한 봉투는 신호별로 독립 쿼리 4개를 자른다 — "restart 3건·
// OOM 0건"을 봉투당 1건으로는 표현할 수 없다).
//
// Findings가 없어도 싣는다 — 0건 조회의 존재 증명이 이 레코드다.
type QueryScope struct {
	// Class — 절단 단위의 이름. 사상표의 finding class(scan의 shifted,
	// sample_logs의 new_in_window, k8s의 signal…)와 같은 어휘여야 한다.
	// projector가 이 값으로 사상표 행을 찾아 query_scope 레코드의 selector를
	// 조립한다.
	Class string `json:"class"`
	// Metric — 조회 단위가 지표축까지 갈리는 도구(compare_peers)에서만.
	Metric string `json:"metric,omitempty"`
	// Total — 절단 전 후보 총수. Returned — 봉투에 실린 수.
	// Omitted — Total-Returned. 0이면 완전 조회다.
	Total    int `json:"total"`
	Returned int `json:"returned"`
	Omitted  int `json:"omitted"`
}

// scope는 총량·반환에서 QueryScope 한 건을 만든다. 음수 누락은 0으로 접는다
// (총량 산출이 반환보다 작게 계산되는 도구가 있으면 그 자체가 결함이지만,
// 레코드에 음수 omitted를 실어 Validate를 죽이지는 않는다).
func qscope(class string, total, returned int) QueryScope {
	om := total - returned
	if om < 0 {
		om = 0
	}
	return QueryScope{Class: class, Total: total, Returned: returned, Omitted: om}
}

// boolInt — LIMIT+1 절단 판별에서 "총량 하한"을 만드는 데만 쓴다.
func boolInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

// scopeMetric은 지표축이 있는 조회 단위용이다.
func qscopeMetric(class, metric string, total, returned int) QueryScope {
	s := qscope(class, total, returned)
	s.Metric = metric
	return s
}

// Finding은 도구별 스키마의 구조화 관측 한 건이다. 자기를 지지하는
// refs를 결합해 든다(계약 §2 — 어느 관측을 어느 원본이 지지하는지).
type Finding map[string]any

// TimeRange는 event time 기준 [From, To) 반개구간(UTC)이다.
type TimeRange struct {
	From time.Time `json:"from"`
	To   time.Time `json:"to"`
}

// no_data_reason 6값 (계약 §2 + §15.2-3 신설).
const (
	NoDataZeroObservations   = "zero_observations" // 조회 성공, 관측 0건 — 가장 강한 배제 근거
	NoDataNotCollected       = "not_collected"     // 이 대상엔 원래 미적용
	NoDataCollectorGap       = "collector_gap"     // 수집기 장애 — 배제 근거 금지
	NoDataTTLExpired         = "ttl_expired"       // 보관 초과 삭제
	NoDataUnknown            = "unknown"
	NoDataMetricUnregistered = "metric_unregistered"
	// NoDataBackendError — 관측 백엔드 실패(§15.2-3, 6b). collector_gap
	// (수집기 장애)과 운영자 안내가 갈리고, 같은 급으로 배제 근거 금지
	// (§5.5). 봉투에는 분류 토큰(Backend·BackendDetail)만 — 원문 오류
	// 문자열 금지(§15.3-2·C-12).
	NoDataBackendError = "backend_error"
)
