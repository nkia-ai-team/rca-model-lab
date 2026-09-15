// 명제 장부 — docs/spec-agent-structure.md §6.0이 정본.
//
// "이 술어는 이미 판정됐는가"를 **전역으로** 답하는 자리다. 가설마다 따로
// 세는 순간 threshold 3.0→2.9 같은 변형으로 반증 가설이 무한 부활한다.
//
// 세 가지가 이 파일의 계약이다:
//
//	① **키는 관측 키다**(§6.0) — Predicate·Threshold는 키에 없다. 관측값이
//	   한 번 실측되면 6종 Predicate × 임의 Threshold의 진리값은 즉시 파생된다.
//	② **truth를 저장하지 않는다** — 장부가 싣는 것은 관측값(typed)이고,
//	   truth는 조회 시 DeriveTruth가 계산한다. 저장하면 그 값의 의미가
//	   어느 threshold의 것인지 고정되지 않는다(3차 검토).
//	③ **두 조회를 코드에서 분리한다**(C-8): 판정·supersede는 창을 구분하고
//	   (Resolve·Truth), 재제안 차단의 "기실측" 조회는 창 축을 무시한다
//	   (MeasuredAnyWindow). 같은 함수에 플래그를 다는 대신 이름을 나눈 것은
//	   두 정의가 한 이름 아래 섞여 온 것이 C-8 지적 자체이기 때문이다.
//
// **경계**: 여기는 저장·파생·재판정까지다. §6 진리표 행 0~7의 게이트 판정
// (지지 자격·반증 자격·supported/invalidated 등급)은 1e의 몫이며, 이 파일은
// 그 판정의 입력(해소 상태 + 파생 truth + 파생 불가 사유)만 제공한다.
//
// **여기 없는 것 하나 더**: §15.2-4의 probe 반복 차단 키는 이 관측 키가
// **아니다** — (관측 키, 해상도 클래스)이며 §14-6에서 따로 정의된다.
// 관측 키를 그대로 차단 키로 쓰면 같은 시계열의 해상도만 올린 정밀화
// 재조회(§7.2-4 시간 요건)가 반복으로 세어져 3회째가 반려된다(C-8).
package evidence

import (
	"sort"
	"sync"
	"time"
)

// ObservationValue는 장부가 싣는 typed 관측값이다 — truth 파생의 유일한 입력.
type ObservationValue struct {
	// Present — 그 개체·사건이 관측됐는가. finding 레코드는 true이고,
	// 완전 조회 0건(observed_zero)으로 해소된 범위는 false다.
	Present bool
	// RecordKind — 관측의 출처 종류. query_scope는 사상표 class 검사를
	// 면제받는다(§6 진리표 행 0의 전용 경로).
	RecordKind RecordKind
	// FindingClass — 판정 시점의 허용 Predicate 검사(§6 행 2)가 읽는 값.
	FindingClass string
	Kind         EffectKind
	Metric       string
	Observed     *float64
	Baseline     *float64
	Magnitude    *float64
	Direction    Direction
	Availability Availability
	Status       Status
}

// Proposition은 관측 키 하나에 대한 장부 행이다(§6.0: 관측 키 → {최신 근거
// EID, 관측값, Availability, 판정 시각}).
type Proposition struct {
	Key ObservationKey
	// EIDs — 그 키의 active 근거. 보통 1건이며, 2건 이상이면 복수 해소다
	// (§6 진리표 행 1) — 하나를 골라 감추지 않는다.
	EIDs []string
	// Value — EIDs가 1건일 때의 관측값. 복수 해소에서는 의미가 없다(zero).
	Value ObservationValue
	// DecidedAt — 이 행이 마지막으로 갱신된 시각(§6.0 "판정 시각").
	DecidedAt time.Time
}

// EID는 최신 근거 EID다 — 복수 해소이면 빈 문자열(정의되지 않는다).
func (p Proposition) EID() string {
	if len(p.EIDs) != 1 {
		return ""
	}
	return p.EIDs[0]
}

// scopeEntry는 조회 범위 레코드의 장부 사본이다. 개체 행이 아니라 **범위**라
// 별도 자리에 둔다 — 부재 개체의 관측은 개체 단위로 존재할 수 없기 때문이다
// (C-3: 존재하지 않는 sql_key·pid를 지목한 absent 술어는 어떤 조회를 해도
// 개체 행을 얻지 못한다. 그 술어를 해소하는 유일한 관측이 이 범위 행이다).
type scopeEntry struct {
	eid          string
	selector     ObservationKey
	complete     bool
	availability Availability
	at           time.Time
}

// Ledger는 명제 장부다(run 스코프, 전역).
type Ledger struct {
	mu     sync.Mutex
	props  map[ObservationKey]*Proposition
	scopes map[string]scopeEntry // EID → 범위 행
	now    func() time.Time
}

// NewLedger는 index에 부착된 장부를 만든다. 이미 실린 레코드는 재생해
// 반영하고, 그 뒤의 append는 Index.Append가 같은 임계 구역에서 넘긴다 —
// 그것이 supersede 시 "후계 관측값으로 원자적 재계산"의 실물이다(§5.7).
func NewLedger(ix *Index) *Ledger {
	l := &Ledger{
		props:  map[ObservationKey]*Proposition{},
		scopes: map[string]scopeEntry{},
		now:    time.Now,
	}
	for _, r := range ix.All() {
		if _, dead := ix.supersededOf(r.EID); dead {
			continue
		}
		l.apply(r, r.Key())
	}
	ix.attachLedger(l)
	return l
}

// SetClock은 시험용 시계 주입이다(판정 시각의 결정론).
func (l *Ledger) SetClock(f func() time.Time) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.now = f
}

// apply는 레코드 하나를 장부에 반영한다. Index.Append가 ix.mu를 쥔 채
// 부르므로 **장부가 index를 되짚지 않는다** — 필요한 값은 전부 레코드
// 사본에서 읽는다(역방향 잠금 없음).
func (l *Ledger) apply(r EvidenceIndexRecord, key ObservationKey) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := l.now()
	switch r.RecordKind {
	case KindMeta:
		return
	case KindQueryScope:
		if r.Supersedes != "" {
			delete(l.scopes, r.Supersedes)
		}
		l.scopes[r.EID] = scopeEntry{
			eid: r.EID, selector: key, complete: r.Scope.Complete(),
			availability: r.Quality.Availability, at: now,
		}
	case KindFinding:
		p := l.props[key]
		if p == nil {
			p = &Proposition{Key: key}
			l.props[key] = p
		}
		if r.Supersedes != "" {
			// 재판정: 선임을 빼고 후계를 넣는 것이 한 동작이다. 사이에
			// 미실측 상태가 존재하지 않는다(4차 A-3 — 종전 "미실측 복귀"
			// 문안은 W1·W2를 돌 때마다 명제를 리셋해 등록 규칙 3의 부활
			// 문을 무단으로 열었다).
			p.EIDs = removeString(p.EIDs, r.Supersedes)
		}
		p.EIDs = append(p.EIDs, r.EID)
		p.DecidedAt = now
		if len(p.EIDs) == 1 {
			p.Value = observationOf(r)
		} else {
			p.Value = ObservationValue{}
		}
	}
}

// observationOf는 레코드에서 typed 관측값을 뽑는다. **값을 만들지 않는다** —
// 레코드에 없는 것은 nil로 남는다.
func observationOf(r EvidenceIndexRecord) ObservationValue {
	return ObservationValue{
		Present:      true, // finding 레코드의 존재 자체가 개체 관측이다
		RecordKind:   r.RecordKind,
		FindingClass: r.FindingClass,
		Kind:         r.Effect.Kind,
		Metric:       r.Effect.Metric,
		Observed:     r.Effect.Observed,
		Baseline:     r.Effect.Baseline,
		Magnitude:    r.Effect.Magnitude,
		Direction:    r.Effect.Direction,
		Availability: r.Quality.Availability,
		Status:       r.Quality.Status,
	}
}

// ── 조회 ①: 판정용(창 구분) ─────────────────────────────────────

// ResolveStatus는 selector 해소의 결과다 — §6 진리표의 입력이다(행 판정
// 자체는 1e).
type ResolveStatus string

const (
	// ResolvedEntity — 개체 레코드 하나로 해소(행 2 이하로 진행).
	ResolvedEntity ResolveStatus = "entity"
	// ResolvedScope — 개체 미해소 + 덮는 완전 조회 범위로 해소(행 0 전용 경로).
	ResolvedScope ResolveStatus = "scope"
	// Ambiguous — 복수 해소(행 1).
	Ambiguous ResolveStatus = "ambiguous"
	// ScopeIncomplete — 덮는 범위 행은 있으나 절단된 조회가 섞여 있다.
	// 잘린 꼬리에 그 개체가 있었을 수 있으므로 해소가 아니다(§5.1 계약 1).
	ScopeIncomplete ResolveStatus = "scope_incomplete"
	// Unmeasured — 그 관측 키로 아무것도 조회되지 않았다(행 1의 0건 갈래).
	Unmeasured ResolveStatus = "unmeasured"
)

// Resolution은 해소 결과다.
type Resolution struct {
	Status ResolveStatus
	Value  ObservationValue
	// EIDs — 해소에 쓰인 근거. 복수 해소면 그 전부, 범위 해소면 덮은 범위 행들.
	EIDs      []string
	DecidedAt time.Time
}

// Measured는 이 해소가 관측값을 내놓는가다(기실측 여부의 절반 — 나머지
// 절반은 그 값에서 술어 truth가 파생되는가다).
func (r Resolution) Measured() bool {
	return r.Status == ResolvedEntity || r.Status == ResolvedScope
}

// Resolve는 관측 키를 **창을 구분해** 해소한다 — 판정(§6 진리표)과
// supersede(§5.7)가 쓰는 조회다.
//
// 2단이다(C-3): ① 개체 행 정확 해소 → ② 없으면 덮는 범위 행. EntityKey가
// 비었거나 `*`인 질의는 개체 축을 열어 조회한다(§6 SignalPred: "EntityKey가
// 있으면 그 개체 레코드로, 없으면 (Target,Aspect,Metric) 해소").
func (l *Ledger) Resolve(k ObservationKey) Resolution {
	want := k.Canonical()
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.resolveLocked(want)
}

func (l *Ledger) resolveLocked(want ObservationKey) Resolution {
	var hits []*Proposition
	if want.EntityKey == "" || want.EntityKey == EntityAny {
		open := want
		open.EntityKey = ""
		for key, p := range l.props {
			probe := key
			probe.EntityKey = ""
			if probe == open && len(p.EIDs) > 0 {
				hits = append(hits, p)
			}
		}
	} else if p := l.props[want]; p != nil && len(p.EIDs) > 0 {
		hits = append(hits, p)
	}
	switch {
	case len(hits) == 1 && len(hits[0].EIDs) == 1:
		return Resolution{Status: ResolvedEntity, Value: hits[0].Value,
			EIDs: append([]string(nil), hits[0].EIDs...), DecidedAt: hits[0].DecidedAt}
	case len(hits) > 0:
		var eids []string
		var at time.Time
		for _, p := range hits {
			eids = append(eids, p.EIDs...)
			if p.DecidedAt.After(at) {
				at = p.DecidedAt
			}
		}
		sort.Strings(eids)
		return Resolution{Status: Ambiguous, EIDs: eids, DecidedAt: at}
	}
	return l.resolveByScopeLocked(want)
}

// resolveByScopeLocked는 부재 개체를 범위 관측으로 해소한다(C-3의 해소 경로).
//
// **덮는 범위 행 전부가 완전 조회여야 한다.** 하나라도 절단돼 있으면 그
// 개체가 잘린 꼬리에 있었을 수 있다(1c 실측: scan_metrics의 shifted·
// appeared·disappeared 세 구획이 한 관측 키로 접히는데 완전성이 엇갈린다 —
// "완전한 형제 하나"로 absent를 인정하면 거짓 부재가 만들어진다).
func (l *Ledger) resolveByScopeLocked(want ObservationKey) Resolution {
	var eids []string
	var at time.Time
	allZero, anyIncomplete := true, false
	// unusable — 덮는 범위 행 중 하나라도 관측을 못 얻은 것이 있는가
	// (Availability ∈ {missing, not_applicable}).
	var unusable Availability
	for _, s := range l.scopes {
		if !s.selector.Covers(want) {
			continue
		}
		eids = append(eids, s.eid)
		if !s.complete {
			anyIncomplete = true
		}
		if s.availability != AvailObservedZero {
			allZero = false
		}
		if unusable == "" && (s.availability == AvailMissing || s.availability == AvailNotApplicable) {
			unusable = s.availability
		}
		if s.at.After(at) {
			at = s.at
		}
	}
	if len(eids) == 0 {
		return Resolution{Status: Unmeasured}
	}
	sort.Strings(eids)
	if anyIncomplete {
		return Resolution{Status: ScopeIncomplete, EIDs: eids, DecidedAt: at}
	}
	// 완전 조회인데 그 개체가 없었다 = 부재를 관측했다. 전부 0건이면
	// observed_zero("최강 배제 근거"), 다른 개체는 나왔는데 이 개체만
	// 없었다면 observed다 — 어느 쪽이든 완전 열거이므로 부재는 실측이다.
	av := AvailObserved
	if allZero {
		av = AvailObservedZero
	}
	// **단, 절단이 없다는 것과 관측을 얻었다는 것은 다르다**(§14-5 5a 수리,
	// 4b 실측 재현). collector_gap·ttl_expired·backend_error로 값을 못 얻은
	// 범위 행은 Omitted=0이라 complete로 세어져 여기까지 흘러들었고, 그때
	// "완전 조회 = 부재 실측"이라 읽으면 **결손이 absent 술어의 지지가 된다**.
	// 그 범위의 해소 값은 부재가 아니라 결손이며, DeriveTruth가 행 2
	// (파생 불능 = 관측 부재)로 지지·반증 양쪽을 막는다.
	if unusable != "" {
		av = unusable
	}
	return Resolution{
		Status: ResolvedScope,
		Value: ObservationValue{
			Present: false, RecordKind: KindQueryScope,
			Metric: want.Metric, Kind: EffectCount, Direction: DirNA,
			Availability: av, Status: StatusNoData,
		},
		EIDs: eids, DecidedAt: at,
	}
}

// Observation은 R 심사(§6.2-5)에 첨부할 기왕 관측값이다 — **미실측이면
// nil**이다(C-2: "관측값 없음"과 "관측값 있음"을 R 어댑터가 구분할 수
// 있어야 미실측 necessary 술어를 자명성 심사에서 뺄 수 있고, 그래야
// "불확실은 불리 방향" 규율이 전 가설 반려로 번지지 않는다).
func (l *Ledger) Observation(k ObservationKey) *ObservationValue {
	r := l.Resolve(k)
	if !r.Measured() {
		return nil
	}
	v := r.Value
	return &v
}

// Propositions는 장부 전량이다(감사·§13 측정용). 키 문자열 정렬 순.
func (l *Ledger) Propositions() []Proposition {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]Proposition, 0, len(l.props))
	for _, p := range l.props {
		c := *p
		c.EIDs = append([]string(nil), p.EIDs...)
		out = append(out, c)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key.String() < out[j].Key.String() })
	return out
}

// ── 조회 ②: 재제안 차단용(창 무시) ──────────────────────────────

// Measurement은 "기실측인가"의 답이다(등록 규칙 2·3의 입력).
type Measurement struct {
	// Measured — 어느 창에서든 실측됐고 그 관측값에서 이 술어의 truth가
	// 파생되는가. 이것이 참이면 그 술어는 예측이 아니라 회고다.
	Measured bool
	Truth    bool
	// Window — 실측이 발견된 창(감사용).
	Window WindowClass
	// Resolution — 발견된 해소(미실측이면 마지막으로 시도한 창의 것).
	Resolution Resolution
	// Derive — 파생 결과. Measured=false의 사유가 여기 있다.
	Derive DeriveStatus
}

// windowClasses — 닫힌 2값(§6 SignalPred.Window). 창 무시 조회가 훑는 축.
var windowClasses = []WindowClass{WindowFull, WindowOnsetNarrow}

// MeasuredAnyWindow는 **창 축을 무시하고** 기실측 여부를 답한다 —
// 등록 규칙 2(pre_satisfied 딱지)·3(미판정 술어 ≥1)의 정본 조회다.
//
// 4차 A-3: 반증된 술어를 full↔onset_narrow로만 바꿔 재등록하는 2값 우회의
// 봉쇄다. Predicate·Threshold 변형은 키에서 이미 빠져 있어(§6.0) 같은
// 관측값으로 즉시 재판정되고, 창 변형은 이 함수가 막는다.
//
// **판정·supersede는 이 함수를 쓰지 않는다** — 그쪽은 창을 구분한다(C-8:
// 두 정의가 한 이름 아래 섞이던 것이 지적의 내용이다).
func (l *Ledger) MeasuredAnyWindow(k ObservationKey, pred Predicate, threshold *float64) Measurement {
	var last Measurement
	for _, w := range windowClasses {
		probe := k.Canonical()
		probe.Window = w
		res := l.Resolve(probe)
		truth, st := DeriveTruth(res.Value, pred, threshold, res.Status)
		m := Measurement{Measured: st == DeriveOK, Truth: truth, Window: w, Resolution: res, Derive: st}
		if m.Measured {
			return m
		}
		if last.Resolution.Status == "" || res.Measured() {
			last = m
		}
	}
	return last
}

// ── truth 파생 ─────────────────────────────────────────────────

// DeriveStatus는 관측값에서 술어 truth를 파생할 수 있었는가다.
// **파생 불능은 관측 부재와 같다**(§5.1) — 지지도 반증도 아니다.
type DeriveStatus string

const (
	DeriveOK DeriveStatus = "ok"
	// DeriveUnresolved — 해소 자체가 안 됐다(진리표 행 1 갈래).
	DeriveUnresolved DeriveStatus = "unresolved"
	// DeriveUnusable — Availability ∈ {missing, not_applicable}(행 2).
	DeriveUnusable DeriveStatus = "unusable_availability"
	// DeriveClassNotAllowed — 해소 레코드의 class가 그 Predicate를 허용하지
	// 않는다(행 2 후단 — 사상표 2층 강제 중 ②).
	DeriveClassNotAllowed DeriveStatus = "class_not_allowed"
	// DeriveNoMagnitude — magnitude_* 인데 Magnitude가 없다(categorical
	// class, baseline 0의 부재 처리 등).
	DeriveNoMagnitude DeriveStatus = "no_magnitude"
	// DeriveNoThreshold — magnitude_* 인데 Threshold가 없다.
	DeriveNoThreshold DeriveStatus = "no_threshold"
	// DeriveNoDirection — direction_* 인데 방향이 n/a다.
	DeriveNoDirection DeriveStatus = "no_direction"
	// DeriveUnknownPredicate — 닫힌 6종 밖.
	DeriveUnknownPredicate DeriveStatus = "unknown_predicate"
)

// DeriveTruth는 관측값에서 술어의 진리값을 파생한다 — **장부는 truth를
// 저장하지 않고 이 함수가 조회 시 계산한다**(§6.0 ②).
//
// status는 해소 결과다(미해소면 파생할 값이 없다). 게이트 행 판정은 1e가
// 이 두 값(truth, DeriveStatus)과 자격 게이트를 합쳐 내린다.
func DeriveTruth(v ObservationValue, pred Predicate, threshold *float64, status ResolveStatus) (bool, DeriveStatus) {
	if status != ResolvedEntity && status != ResolvedScope {
		return false, DeriveUnresolved
	}
	if !ValidPredicate(pred) {
		return false, DeriveUnknownPredicate
	}
	switch v.Availability {
	case AvailMissing, AvailNotApplicable:
		// 반증·배제 금지 — "원래 없는 지표"가 absent 예측의 지지로 둔갑하는
		// 문을 닫는다(§5.1·§6 행 2).
		return false, DeriveUnusable
	}
	// class 층 검사는 **finding 레코드에만** 건다 — RecordKind=query_scope는
	// 사상표 class 검사 대상이 아니다(§6 진리표 행 0: "행 1·2를 면제하는
	// 전용 경로"). 범위 관측이 낼 수 있는 술어는 존재 여부뿐이므로 아래
	// present/absent 갈래가 실질 제한을 진다.
	if v.RecordKind == KindFinding {
		row, ok := RowFor(v.FindingClass)
		if !ok || !row.AllowsPredicate(pred) {
			return false, DeriveClassNotAllowed
		}
	}
	switch pred {
	case PredPresent:
		return v.Present, DeriveOK
	case PredAbsent:
		return !v.Present, DeriveOK
	case PredMagnitudeGE, PredMagnitudeLT:
		if v.RecordKind == KindQueryScope || !v.Present {
			// 부재 개체에는 크기가 없다 — 0으로 읽으면 magnitude_lt가
			// 부재에서 참이 되어 "없으니 작다"가 지지 근거로 둔갑한다.
			return false, DeriveNoMagnitude
		}
		if v.Magnitude == nil {
			return false, DeriveNoMagnitude
		}
		if threshold == nil {
			return false, DeriveNoThreshold
		}
		if pred == PredMagnitudeGE {
			return *v.Magnitude >= *threshold, DeriveOK
		}
		return *v.Magnitude < *threshold, DeriveOK
	case PredDirectionUp, PredDirectionDown:
		if v.RecordKind == KindQueryScope || !v.Present {
			return false, DeriveNoDirection
		}
		switch v.Direction {
		case DirUp:
			return pred == PredDirectionUp, DeriveOK
		case DirDn:
			return pred == PredDirectionDown, DeriveOK
		case DirFlt:
			// flat은 방향 술어를 판정할 수 있다 — 둘 다 거짓이다.
			return false, DeriveOK
		default: // n/a — 원천에 방향이 없다
			return false, DeriveNoDirection
		}
	}
	return false, DeriveUnknownPredicate
}

// Holds는 파생한 truth를 Expectation과 맞춘다 — must_hold면 truth 그대로,
// must_not_hold면 뒤집는다. §6 진리표의 "truth = Expectation 일치" 판정
// 입력이며, 일치/불일치 다음의 등급(supported·invalidated·자격)은 1e다.
func Holds(truth bool, exp Expectation) bool {
	return truth == (exp == ExpectMustHold)
}

// Expectation은 must_hold | must_not_hold다(§6 SignalPred.Expectation).
// **정의가 evidence에 있는 이유**: truth 파생이 이 값과 짝을 이루는데
// ledger→evidence 방향으로만 import가 가능하다(Predicate를 1b에서 옮긴 것과
// 같은 사유). ledger는 별칭으로 승계한다 — 사용처는 바뀌지 않는다.
type Expectation string

const (
	ExpectMustHold    Expectation = "must_hold"
	ExpectMustNotHold Expectation = "must_not_hold"
)

func ValidExpectation(e Expectation) bool {
	return e == ExpectMustHold || e == ExpectMustNotHold
}
