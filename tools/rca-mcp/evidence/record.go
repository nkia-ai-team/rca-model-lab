// evidence index 레코드 계약 — docs/spec-agent-structure.md §5.1이 정본.
//
// 필드마다 산출 원천을 doc에 적었다. 원천은 셋 중 하나다:
//
//	① 봉투(tools.Envelope)의 특정 키 — 도구 공통 필드
//	② 사상표(§5.1 도구×class 표)의 특정 열 — 도구별 finding 키는 표가
//	   확정한다(1b 몫). 여기서는 "표의 어느 열이 채우는가"까지가 계약이다
//	③ 규칙 계산 — 스펙이 산식을 못박은 것만
//
// 원천을 지목할 수 없는 필드는 넣지 않는다(expand_topology 전례 —
// "이름만 있고 계약이 없는 필드"의 재발 방지).
package evidence

import (
	"fmt"
	"math"
	"time"
)

// ── 닫힌 어휘 ───────────────────────────────────────────────────

// Aspect는 관측의 관점 축이다(§5.1 Aspect 열).
type Aspect string

const (
	AspectMetric   Aspect = "metric"
	AspectEvent    Aspect = "event"
	AspectLog      Aspect = "log"
	AspectDB       Aspect = "db"
	AspectEndpoint Aspect = "endpoint"
	AspectPeer     Aspect = "peer"
	AspectProcess  Aspect = "process"
	AspectChange   Aspect = "change"
)

func ValidAspect(a Aspect) bool {
	switch a {
	case AspectMetric, AspectEvent, AspectLog, AspectDB,
		AspectEndpoint, AspectPeer, AspectProcess, AspectChange:
		return true
	}
	return false
}

// RecordKind는 레코드의 성격이다(§5.1, 4차 A-6). finding만 술어 판정
// 대상이고, query_scope는 조회 범위 계약(§5.1 계약 1), meta는 감사 재료다.
type RecordKind string

const (
	KindFinding    RecordKind = "finding"
	KindQueryScope RecordKind = "query_scope"
	KindMeta       RecordKind = "meta"
)

func ValidRecordKind(k RecordKind) bool {
	return k == KindFinding || k == KindQueryScope || k == KindMeta
}

// EffectKind는 Magnitude의 의미다(§5.1). z는 없다 — z를 선언한 유일 행
// (peer)의 봉투에 z가 없다는 실측으로 3차 검토가 폐지했다.
type EffectKind string

const (
	EffectRatio       EffectKind = "ratio"       // Magnitude = Observed/Baseline
	EffectCount       EffectKind = "count"       // Magnitude = 창 내 건수(=Observed)
	EffectDuration    EffectKind = "duration"    // Magnitude = 대표 소요(=Observed)
	EffectShare       EffectKind = "share"       // Magnitude = 점유율(=Observed)
	EffectCategorical EffectKind = "categorical" // Magnitude 부재 — 정렬·magnitude_* 불가
)

func ValidEffectKind(k EffectKind) bool {
	switch k {
	case EffectRatio, EffectCount, EffectDuration, EffectShare, EffectCategorical:
		return true
	}
	return false
}

// kindRank는 Kind 간 고정 우선순위다(§5.1 — 이종 Kind 수치 직접 비교
// 금지. 정렬은 "Kind 내 Magnitude 정렬 + Kind 간 고정 우선순위"). 가안
// ratio > duration > share > count이며 §13 실측으로 조정한다.
// categorical은 Magnitude가 없어 정렬 대상이 아니다.
var kindRank = map[EffectKind]int{
	EffectRatio: 4, EffectDuration: 3, EffectShare: 2, EffectCount: 1,
}

// KindRank는 Kind 간 고정 우선순위를 돌려준다(§5.3-6 절단 정렬과 §4 B2
// 지표 선택이 같은 정렬을 공유하도록 정의는 여기 한 곳).
func KindRank(k EffectKind) (int, bool) { r, ok := kindRank[k]; return r, ok }

// Direction은 변화 방향이다(§5.1 Effect.Direction).
type Direction string

const (
	DirUp  Direction = "up"
	DirDn  Direction = "down"
	DirFlt Direction = "flat"
	DirNA  Direction = "n/a"
)

func ValidDirection(d Direction) bool {
	return d == DirUp || d == DirDn || d == DirFlt || d == DirNA
}

// Status는 봉투 status 3값의 승계다(tools.Envelope.Status).
type Status string

const (
	StatusNormal    Status = "normal"
	StatusAnomalous Status = "anomalous"
	StatusNoData    Status = "no_data"
)

func ValidStatus(s Status) bool {
	return s == StatusNormal || s == StatusAnomalous || s == StatusNoData
}

// NoDataReason은 봉투 5구분 + backend_error(§15.2 신설)다.
type NoDataReason string

const (
	NoDataZeroObservations NoDataReason = "zero_observations"
	NoDataNotCollected     NoDataReason = "not_collected"
	NoDataCollectorGap     NoDataReason = "collector_gap"
	NoDataTTLExpired       NoDataReason = "ttl_expired"
	NoDataUnknown          NoDataReason = "unknown"
	NoDataBackendError     NoDataReason = "backend_error"
)

func ValidNoDataReason(r NoDataReason) bool {
	switch r {
	case NoDataZeroObservations, NoDataNotCollected, NoDataCollectorGap,
		NoDataTTLExpired, NoDataUnknown, NoDataBackendError:
		return true
	}
	return false
}

// Availability는 "이 관측을 배제·반증 근거로 쓸 수 있는가"의 판정이다.
// 규칙 계산이며 LLM 선언이 아니다(§5.1 Quality.Availability).
type Availability string

const (
	// AvailObserved — 관측값이 있다.
	AvailObserved Availability = "observed"
	// AvailObservedZero — zero_observations + 조회 완전 + 백엔드 성공.
	// "0을 관측함" = 봉투 계약의 최강 배제 근거. absent 술어 평가 가능.
	AvailObservedZero Availability = "observed_zero"
	// AvailMissing — collector_gap·ttl_expired·backend_error·unknown.
	// 반증·배제 금지.
	AvailMissing Availability = "missing"
	// AvailNotApplicable — not_collected(이 대상엔 원래 미적용).
	// 반증·배제 불가 + §5.2 미조회 분모에서 제외.
	AvailNotApplicable Availability = "not_applicable"
)

func ValidAvailability(a Availability) bool {
	switch a {
	case AvailObserved, AvailObservedZero, AvailMissing, AvailNotApplicable:
		return true
	}
	return false
}

// AvailabilityOf는 §5.1의 규칙 계산을 그대로 옮긴 것이다 — 봉투의
// no_data_reason과 조회 완전성(절단 없음)·백엔드 성공에서 파생한다.
// 세 소비자(§5.3 절단 우선순위·§5.5 반증 자격·§6 진리표)가 각자 셈하지
// 않도록 산식은 여기 한 곳이다.
func AvailabilityOf(st Status, reason NoDataReason, complete bool) Availability {
	if st != StatusNoData {
		return AvailObserved
	}
	switch reason {
	case NoDataZeroObservations:
		if complete {
			return AvailObservedZero
		}
		// 절단된 조회의 0건은 배제 근거가 아니다 — 잘린 꼬리에 있었을 수
		// 있다(§5.1 계약 1).
		return AvailMissing
	case NoDataNotCollected:
		return AvailNotApplicable
	default: // collector_gap · ttl_expired · unknown · backend_error
		return AvailMissing
	}
}

// Confidence는 품질 등급이다 — 규칙 계산(표본 부족·부분 커버·거친 해상도,
// §5.1). §5.4-1의 "normal인데 OmittedN>0이면 low 강등"도 이 축이다.
type Confidence string

const (
	ConfOK  Confidence = "ok"
	ConfLow Confidence = "low"
)

func ValidConfidence(c Confidence) bool { return c == ConfOK || c == ConfLow }

// TagStatus는 시간 보정 항의 가용 상태다(§5.1 4차 A-2).
// int 단독이던 시절 zero value 0이 "실측 0초"와 "미정"을 구분하지 못해
// 측정 실패 환경일수록 "지연 없음"으로 둔갑했다 — fail-closed가 스키마
// 때문에 fail-open이던 구멍을 이 태그가 닫는다.
type TagStatus string

const (
	TagObserved     TagStatus = "observed"
	TagUnknown      TagStatus = "unknown"
	TagBackendError TagStatus = "backend_error" // CollectLag 전용
	TagExceeded     TagStatus = "exceeded"      // ClockSkew 전용 — 허용치 초과
)

// ── 레코드 ─────────────────────────────────────────────────────

// CollectLag는 수집 지연이다(§5.1 Window.CollectLag).
//
// **원천은 관측 데이터 자체다**(§5.5 게이트 조항의 원천, 사용자 결정
// 2026-08-04): 산식은 `요청 창 끝 − 그 조회가 창 안에서 받아온 마지막
// 데이터 시각`이고 산출 지점은 projector 하나다(CollectLagOf). 종전 원천
// 이던 collectors.last_collected_at(A6 봉투)은 라이브에서 50중 32가 NULL·
// 나머지도 최대 14일 낡았는데 같은 대상들이 창 안에 데이터를 내고 있어
// 폐기됐다 — 죽은 것은 수집이 아니라 메타데이터였다.
type CollectLag struct {
	ValueS int
	Status TagStatus // observed | unknown | backend_error
}

// ClockSkew는 시계 오차 한계다(§5.1 Window.ClockSkew).
// 측정 인프라가 아니라 **배포 설정의 명시적 파라미터**다 — 운영자가 NTP
// 상태로 입력하고, 모르면 unknown(레포 전체 skew 코드 0건 실측).
// Status≠observed면 §10 시간 판정을 호출하지 않는다.
type ClockSkew struct {
	BoundS int
	Status TagStatus // observed | unknown | exceeded
}

// Usable은 §10 onset 산식을 호출해도 되는지다 — 두 항 모두 observed여야
// 한다(§10 "산식 호출의 전제").
func (c CollectLag) Usable() bool { return c.Status == TagObserved }
func (c ClockSkew) Usable() bool  { return c.Status == TagObserved }

// Effect는 도구의 판정값 그대로다 — **하네스 재평균 금지**(§5.1·§5.4-3:
// 분포 꼬리는 도구의 최악값을 승계하고, 재평균 경로 자체를 만들지 않는다).
type Effect struct {
	// Kind — Magnitude의 의미. 레코드당 정확히 하나다: 한 finding에
	// 판정축이 여럿이면(예: breakdown_endpoints의 지연·실패율) 축별로
	// 레코드를 나눈다(B-13 대조 — Kind 복수 선언 행은 사상표에서 쪼갠다).
	Kind EffectKind
	// Metric — 판정 축의 이름. 사상표 행의 Observed 열이 어느 봉투 키인지가
	// 이 값의 정본이다(1b 확정).
	Metric string
	// Observed/Baseline — 사상표 행의 "Observed / Baseline" 열. 실물에 없는
	// 쪽은 nil이다(appeared 행은 Baseline 부재, disappeared 행은 현재값
	// 부재가 각각 의미다 — 0으로 채우지 않는다).
	Observed *float64
	Baseline *float64
	// Magnitude — §5.1 Kind별 정의식. categorical에서는 nil(부재)이다.
	// 비유한 값(±Inf·NaN)은 실을 수 없다 — Validate가 막는다(B-11).
	Magnitude *float64
	Direction Direction
}

// TimeWindow는 레코드의 시간 계약이다(§5.1 Window).
type TimeWindow struct {
	// From/To — 조회 창. 원천: 봉투 observed_range, 없으면 도구 호출 인자.
	From, To time.Time
	// ChangeFrom/ChangeTo — 변화구간(이상의 시작~끝). §10의 유일한 입력이다.
	// **산출 가능 도구에서만 non-nil**이고, 창 대 창 집계 도구의 레코드는
	// nil이 계약이다 — 조회 창으로 채우면 §10이 전부 "겹침=불명"이 되어
	// confirmed가 영구 불가해진다(§5.1 가용성 계약, scan.go:321 "onset은
	// read의 몫"). normal 레코드도 nil이다.
	// 어느 도구가 채우는지는 사상표와 함께 1b가 확정한다.
	ChangeFrom *time.Time
	ChangeTo   *time.Time
	// ResolutionS — 버킷 해상도(초). 원천: 도구의 스캔 스텝(예 read.go의
	// 30s 고정). §10 onset_lo 산식의 항이다.
	ResolutionS int
	// Class — 이 관측이 어느 창 클래스로 조회됐는가(full | onset_narrow).
	// 원천: 호출 맥락(ProjectRequest.WindowClass) — 실 창 From/To만으로는
	// 판정할 수 없다(§6.0 "키에 드는 것은 창 enum").
	//
	// **1c 추가 사유**: 관측 키의 창 축이 레코드에 없으면 supersede
	// selector가 정의되지 않는다 — §5.7이 "full과 onset_narrow는 서로 다른
	// head로 공존"을 요구하는데, 창 클래스를 조회자가 들고 오는 종전
	// 계약으로는 onset_narrow 레코드가 full 조회에 그대로 걸려 W2 재조회가
	// full 창 관측을 지우는 4차 A-3의 오답 경로가 그대로 열린다.
	Class WindowClass
	CollectLag  CollectLag
	ClockSkew   ClockSkew
}

// Quality는 관측의 품질·가용성이다(§5.1 Quality).
type Quality struct {
	// SampleN — 표본 수. **모든 행에 원천이 있지는 않다** — 실측상
	// compare_peers 봉투의 `samples`(comparepeers.go:438)가 유일한 표본
	// 키다. 그래서 int가 아니라 *int이며 nil = "표본 수 미상"이다:
	// int 0은 "표본 0건"과 구분되지 않아 Confidence 규칙("표본 부족")이
	// 미상 환경에서 fail-open된다 — CollectLag가 4차 A-2로 고친 것과
	// 정확히 같은 병이다.
	SampleN      *int
	Status       Status       // 봉투 status 승계
	NoDataReason NoDataReason // 봉투 no_data_reason 승계 + backend_error
	Availability Availability // 규칙 계산 — AvailabilityOf
	Confidence   Confidence   // 규칙 계산
}

// Provenance는 이 레코드가 어디서 왔는지다(§5.1 Provenance).
type Provenance struct {
	// Source — 관측 원천. change 합성 원천 포함(§5.1 change 행).
	Source ObservationSource
	// ParamsDigest — 파라미터 정규화 해시. 요청 동일성(감사·재조회 근거).
	ParamsDigest string
	// EnvelopeRef — evidence store 키. 원천은 withEnvelopeRef가 봉투에
	// 보증하는 조회면 ref(tools/registry.go:26)다. §8.1 인용 무결성
	// 게이트가 실재를 검사하는 대상.
	EnvelopeRef string
	// LineageID — 원천 계보 해시(백엔드 종류 + 물리 데이터셋/시계열 + 대상).
	// **조회 창은 제외**한다 — 같은 시계열을 full과 onset_narrow로 두 번
	// 읽은 것은 같은 계보다. §8이 중복 파생을 하나로 세는 근거이며
	// ParamsDigest(요청 동일성)와 다르다.
	LineageID string
}

// Truncation은 절단 사실이다 — "절단 사실은 절단하지 않는다"(§5.1).
type Truncation struct {
	Truncated bool // 봉투 Truncated 승계
	// OmittedN — top-K 밖 후보 수. 원천은 봉투의 **구조화 절단 필드**
	// {total, returned, omitted}이며 이 필드의 additive 확장이 1b 몫이다.
	// Summary 문자열 파싱은 금지다(3차 검토: 현행 봉투는 Truncated bool뿐
	// 이라 이 값을 만들 원천이 없었다 — 원천을 만들고 나서 쓴다).
	OmittedN int
}

// QueryScope는 조회 범위 레코드(RecordKind=query_scope)의 알맹이다
// (§5.1 계약 1). 논리 조회 단위마다 Findings 유무와 무관하게 생성되며,
// 단위는 봉투가 아니라 (Tool, class/signal, TargetID, Metric, Window)다
// — get_k8s_state 한 봉투는 신호별로 독립 쿼리 4개를 자른다(4차 A-7).
//
// selector는 **구조화**다(ParamsDigest가 아니다) — 일방향 해시로는
// SignalPred와 동치 비교가 불가능하기 때문이다.
type QueryScope struct {
	Selector ObservationKey // 구조화 selector. EntityKey는 보통 EntityAny(범위)
	Total    int
	Returned int
	Omitted  int
}

// Complete는 절단 없는 완전 조회인가다 — absent 술어를 이 레코드로
// 해소해도 되는지의 판정(§6 진리표 행 0의 전제).
//
// **주의: 질의 계층 절단(봉투 QueryTruncated — §14-7 D-1)은 여기 안
// 보인다** — 그 절단은 Total 미상이라 Omitted로 못 오고, 레코드의
// Quality.Availability(missing 강등)와 Truncation.Truncated가 진다.
// 완전성 판정의 정본은 Complete() 단독이 아니라 availability다 —
// Complete()만 믿는 새 소비자를 만들면 D-1 유출이 재개된다.
func (q QueryScope) Complete() bool { return q.Omitted == 0 }

// EvidenceIndexRecord는 evidence index의 레코드 한 건이다(§5.1).
// 단위는 **개체**다 — sql_key별 슬로우쿼리, blocker/waiter 세션이 아니라
// 사건, 진입점별 분해, 이벤트 에피소드, 로그 템플릿. 도메인 등급("DB
// severe")으로 접지 않는다(§9 lucida 전례 ①의 방어).
type EvidenceIndexRecord struct {
	// EID — "EIX-0007". [4][5]가 인용하는 전역 키이며 불변이다(§5.7).
	// 부여 주체는 index 적재기(1b)다.
	EID string
	// TargetID — 도구 호출의 대상 인자. 위상 어휘(§6.2-1ⓐ)의 canonical 값.
	TargetID string
	// Domain — 대상의 도메인. 원천: [1] Triage의 TargetDomains
	// (pipeline/triage.go:83 — 정본 get_target_meta, 보조 topology).
	Domain string
	Aspect Aspect
	// RecordKind — finding | query_scope | meta(4차 A-6).
	RecordKind RecordKind
	// FindingClass — 사상표 행 키(도구×class). 허용 Predicate 판정
	// (§6 진리표 행 2)의 저장 매체다 — 이 필드가 없어 판정식이 읽을 곳이
	// 없었다. 값의 어휘는 사상표가 확정한다(1b).
	FindingClass string
	// EntityKey — 개체 정본 키. 사상표의 EntityKey 정본 열이 정의하며,
	// selector 해소·ChainClaim 링크·§8 말단 깊이 게이트가 전부 이 필드
	// 하나를 쓴다. metric 계열("—" 행)은 Effect.Metric이 식별을 겸한다.
	EntityKey string
	// Headline — ≤120자, **기계 생성 서술만**. 원문 직삽 금지(§15.1).
	Headline string
	// RawExcerpt — 원문 발췌 전용 ≤200자(§15.1 이스케이프 규율). 정체성
	// 보존과 원문 반입은 다른 것이다(§5.6).
	RawExcerpt string

	Effect     Effect
	Window     TimeWindow
	Quality    Quality
	Provenance Provenance

	// Scope — RecordKind=query_scope일 때만 non-nil(§5.1 계약 1).
	Scope *QueryScope

	// Supersedes — 재조회로 대체한 이전 레코드의 EID(§5.7). 같은 selector
	// (=관측 키)의 active head 하나 → 새 head 하나인 함수로 제한된다.
	Supersedes string
	// Refines — 도구가 다른 정밀 재조회(read_timeseries가 scan_metrics
	// 자리)의 관계다(§5.7, 4차 A-3). supersede가 아니다 — 원본 head를
	// 지우면 반증·지지가 증발한다. 원본은 active로 남고 정밀본은 자기
	// 키의 head가 된다.
	Refines string

	// DrilldownRefs — 봉투 refs 승계. [5] fetch_ref의 입력.
	DrilldownRefs []string
	// Masked — 마스킹된 필드명 목록(§15.3-2). bool로는 무엇이 가려졌는지
	// 표현 불가라 목록이다. **채우는 주체(마스킹 스캐너)는 §14-6 몫**이고
	// 여기서는 자리만 둔다 — 나중에 붙이려면 전 타입을 다시 뜯어야 한다.
	Masked []string

	Truncation Truncation
}

// Key는 이 레코드의 관측 키다(§6.0). supersede selector(§5.7)·명제
// 장부(§6.0)·술어 해소가 전부 이 값 하나로 대조한다.
//
// 창 클래스는 **레코드 자신이 들고 있다**(Window.Class) — 조회자가 넘기던
// 종전 계약(1a·1b)에서는 같은 레코드가 조회 창에 따라 다른 키를 갖게 되어
// selector가 정의되지 않았다(1c 변경 사유는 TimeWindow.Class 주석 참조).
func (r EvidenceIndexRecord) Key() ObservationKey {
	return ObservationKey{
		Source:    r.Provenance.Source,
		TargetID:  r.TargetID,
		Aspect:    r.Aspect,
		Metric:    r.Effect.Metric,
		EntityKey: r.EntityKey,
		Window:    r.Window.Class,
	}.Canonical()
}

const (
	headlineMax   = 120
	rawExcerptMax = 200
)

// Validate는 레코드가 스키마 계약을 지키는지 검사한다. projector(1b)가
// index에 append하기 전에 반드시 지나야 하는 관문이다.
func (r EvidenceIndexRecord) Validate() error {
	if r.EID == "" {
		return fmt.Errorf("EID 없음")
	}
	if !ValidRecordKind(r.RecordKind) {
		return fmt.Errorf("record_kind %q 미정의", r.RecordKind)
	}
	if !ValidAspect(r.Aspect) {
		return fmt.Errorf("aspect %q 미정의", r.Aspect)
	}
	if !ValidSource(r.Provenance.Source) {
		return fmt.Errorf("원천 %q는 사상표에 행이 없음", r.Provenance.Source)
	}
	if len([]rune(r.Headline)) > headlineMax {
		return fmt.Errorf("headline %d자 — 상한 %d자(§5.1)", len([]rune(r.Headline)), headlineMax)
	}
	if len([]rune(r.RawExcerpt)) > rawExcerptMax {
		return fmt.Errorf("raw_excerpt %d자 — 상한 %d자(§15.1)", len([]rune(r.RawExcerpt)), rawExcerptMax)
	}
	if r.RecordKind == KindQueryScope && r.Scope == nil {
		return fmt.Errorf("query_scope 레코드에 scope 없음")
	}
	// 창 클래스는 관측 키의 축이다 — 판정 대상 레코드(finding·query_scope)에
	// 없으면 supersede selector와 장부 키가 정의되지 않는다(§5.7·§6.0).
	// meta는 술어 판정 대상이 아니라 요구하지 않는다.
	if r.RecordKind != KindMeta && !ValidWindowClass(r.Window.Class) {
		return fmt.Errorf("window.class %q 미정의 — 관측 키의 창 축이 빈다(§6.0)", r.Window.Class)
	}
	// query_scope의 구조화 selector는 레코드의 관측 키와 같은 것이어야 한다
	// — 둘이 갈리면 "덮는 범위 행" 조회와 supersede head 색인이 다른 키를
	// 보게 되어 C-3 해소 경로가 조용히 어긋난다.
	if r.RecordKind == KindQueryScope && r.Scope != nil {
		if got, want := r.Scope.Selector.Canonical(), r.Key(); got != want {
			return fmt.Errorf("query_scope selector %s가 레코드 관측 키 %s와 다름", got, want)
		}
	}
	if r.RecordKind != KindQueryScope && r.Scope != nil {
		return fmt.Errorf("%s 레코드에 scope가 붙음 — query_scope 전용", r.RecordKind)
	}
	if r.Supersedes != "" && r.Refines != "" {
		return fmt.Errorf("supersede와 refine을 동시에 주장할 수 없음(§5.7)")
	}
	if r.RecordKind == KindFinding {
		if !ValidEffectKind(r.Effect.Kind) {
			return fmt.Errorf("effect.kind %q 미정의", r.Effect.Kind)
		}
		if !ValidDirection(r.Effect.Direction) {
			return fmt.Errorf("effect.direction %q 미정의", r.Effect.Direction)
		}
		if r.Effect.Kind == EffectCategorical && r.Effect.Magnitude != nil {
			return fmt.Errorf("categorical에는 magnitude가 없다(§5.1 정의식)")
		}
		if r.FindingClass == "" {
			return fmt.Errorf("finding 레코드에 finding_class 없음 — 허용 Predicate 판정 불능(§6 행 2)")
		}
	}
	if !ValidStatus(r.Quality.Status) {
		return fmt.Errorf("quality.status %q 미정의", r.Quality.Status)
	}
	if r.Quality.Status == StatusNoData && !ValidNoDataReason(r.Quality.NoDataReason) {
		return fmt.Errorf("no_data인데 사유 %q 미정의", r.Quality.NoDataReason)
	}
	if !ValidAvailability(r.Quality.Availability) {
		return fmt.Errorf("availability %q 미정의", r.Quality.Availability)
	}
	if !ValidConfidence(r.Quality.Confidence) {
		return fmt.Errorf("confidence %q 미정의", r.Quality.Confidence)
	}
	if r.Quality.SampleN != nil && *r.Quality.SampleN < 0 {
		return fmt.Errorf("sample_n 음수")
	}
	// 비유한 수치 금지(B-11). baseline 0에서 ratio가 +Inf가 되면 JSON
	// 직렬화가 실패해 projector_failure가 되거나, 정렬에서 무한대가
	// 최상위로 올라가 오판한다. **분모 0 분기는 projector의 계약**이고,
	// 여기서는 그 계약을 어긴 값이 index에 들어가지 못하게 막는다.
	for name, v := range map[string]*float64{
		"observed": r.Effect.Observed, "baseline": r.Effect.Baseline, "magnitude": r.Effect.Magnitude,
	} {
		if v != nil && (math.IsInf(*v, 0) || math.IsNaN(*v)) {
			return fmt.Errorf("effect.%s가 비유한 값 — index 적재 불가(B-11)", name)
		}
	}
	if r.Truncation.OmittedN < 0 {
		return fmt.Errorf("omitted_n 음수")
	}
	if r.Truncation.OmittedN > 0 && !r.Truncation.Truncated {
		return fmt.Errorf("omitted_n>0인데 truncated=false — 절단 사실 누락")
	}
	return nil
}
