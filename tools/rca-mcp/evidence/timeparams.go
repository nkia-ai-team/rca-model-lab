// 시간 보정 두 항의 산출·배선 — docs/spec-agent-structure.md §5.1(tagged
// 타입)·§10(ClockSkew는 가정이 아니라 입력)·§15.2-3(CollectLag
// fail-closed)이 정본이다.
//
// 두 항은 원천이 다르다:
//
//	ClockSkew  — **배포 설정의 명시적 파라미터**다. 측정 인프라가 아니다.
//	             운영자가 NTP 상태로 입력하고, 모르면 unknown이다.
//	CollectLag — **그 조회가 받아온 관측 데이터 자체**에서 산출한다(§5.5 게이트
//	             조항의 원천 결정, 2026-08-04). 봉투마다 자기 값을 낳으므로
//	             대상별 사전 산출 표가 없다 — 산출 지점은 projector 하나다.
//
// **여기는 값의 산출과 전달까지다.** "unknown이면 시간 술어를 판정하지
// 않는다"는 규칙 본체는 §10이고 배선은 §14-5다 — 이 파일이 주는 것은
// Usable()로 그 판정을 내릴 수 있는 tagged 값이다.
package evidence

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

// ── ClockSkew: 배포 파라미터 ───────────────────────────────────────

// DefaultClockSkewToleranceS는 §10의 허용치 기본값이다(2초). 초과면
// 그 창의 시간 술어는 전부 inconclusive다 — 값을 알아도 못 쓴다.
const DefaultClockSkewToleranceS = 2

// 환경 변수 이름. 배포 설정의 입력 표면이다(§10 "운영자가 NTP 상태로 입력").
const (
	EnvClockSkewBound     = "RCA_CLOCK_SKEW_BOUND_S"
	EnvClockSkewTolerance = "RCA_CLOCK_SKEW_TOLERANCE_S"
)

// ClockSkewSetting은 운영자가 준 값이다. **Known이 별도 필드인 이유**는
// int 0이 "완벽한 시계"와 "미입력"을 구분하지 못하기 때문이다 — 4차 A-2가
// 레코드 스키마에서 닫은 문과 같은 문이며, 설정 층에도 있다.
type ClockSkewSetting struct {
	BoundS int
	Known  bool
	// ToleranceS — 허용치. 0이면 DefaultClockSkewToleranceS를 쓴다.
	ToleranceS int
}

// Resolve는 설정을 레코드에 실을 tagged 값으로 바꾼다.
//
//	미입력            → unknown  (§10 fail-closed)
//	허용치 초과       → exceeded (값은 알지만 시간 판정에 쓸 수 없다)
//	그 외             → observed
//
// 음수 입력은 unknown이다 — 오차 "한계"는 크기이므로 부호가 없다.
func (s ClockSkewSetting) Resolve() ClockSkew {
	if !s.Known || s.BoundS < 0 {
		return ClockSkew{Status: TagUnknown}
	}
	tol := s.ToleranceS
	if tol <= 0 {
		tol = DefaultClockSkewToleranceS
	}
	if s.BoundS > tol {
		return ClockSkew{BoundS: s.BoundS, Status: TagExceeded}
	}
	return ClockSkew{BoundS: s.BoundS, Status: TagObserved}
}

// ClockSkewFromEnv는 배포 설정을 읽는다. **미설정은 오류가 아니라
// unknown**이고(운영자가 모를 수 있다는 것이 §10의 전제), 설정됐는데 읽을
// 수 없으면 오류다 — 오타를 unknown으로 삼키면 "설정했다고 믿는 미설정"이
// 생긴다.
func ClockSkewFromEnv() (ClockSkewSetting, error) {
	var s ClockSkewSetting
	if raw := os.Getenv(EnvClockSkewBound); raw != "" {
		v, err := strconv.Atoi(raw)
		if err != nil {
			return ClockSkewSetting{}, fmt.Errorf("%s=%q 정수 아님: %w", EnvClockSkewBound, raw, err)
		}
		if v < 0 {
			return ClockSkewSetting{}, fmt.Errorf("%s=%d 음수 — 시계 오차 한계는 크기다", EnvClockSkewBound, v)
		}
		s.BoundS, s.Known = v, true
	}
	if raw := os.Getenv(EnvClockSkewTolerance); raw != "" {
		v, err := strconv.Atoi(raw)
		if err != nil {
			return ClockSkewSetting{}, fmt.Errorf("%s=%q 정수 아님: %w", EnvClockSkewTolerance, raw, err)
		}
		if v <= 0 {
			return ClockSkewSetting{}, fmt.Errorf("%s=%d — 허용치는 양수여야 한다", EnvClockSkewTolerance, v)
		}
		s.ToleranceS = v
	}
	return s, nil
}

// ── CollectLag: 관측 데이터 자체에서 산출 ──────────────────────────

// CollectLagOf는 봉투 하나의 수집 지연이다 — **산식은 그대로, 원천만
// 바뀌었다**(§5.5 게이트 조항의 원천, 사용자 결정 2026-08-04).
//
//	종전  window_end − collectors.last_collected_at   (수집기 메타데이터)
//	현행  요청 창 끝 − 그 조회가 창 안에서 받아온 마지막 데이터 시각
//
// 메타데이터를 버린 이유는 실측이다: 라이브 119의 collectors 50행 중 32행이
// last_collected_at NULL이고 값이 있는 것도 최대 14일 낡았는데, **같은
// 대상들이 창 안에 데이터를 활발히 내고 있었다** — 죽은 것은 수집이 아니라
// 메타데이터다. 그것을 원천으로 fail-closed를 걸면 자격 지지 0건 =
// confirmed 구조적 불가가 된다(1e TestGateOverLiveEnvelope 실측).
//
// **마지막 데이터 시각의 원천은 봉투의 observed_range.to다.** 그것이 도구가
// "이 창에서 내가 실제로 본 범위"를 선언하는 유일한 공통 필드이고(§5.1
// Window.From/To의 원천도 같다), 레코드마다 따로 시각을 캐면 도구별로 있는
// 필드가 달라 판정이 도구에 의존하게 된다.
//
// **"조회 시점과의 차"는 여기서 절반, 게이트에서 절반이다**(판단 지점):
// 결정 문면은 "마지막 데이터 시각과 조회 시점의 차"이지만 그 값을 그대로
// 실으면 게이트 판정식 `Now ≥ Window.To + lag`이 항등식이 된다 —
// Window.To 자신이 observed_range.to이기 때문이다(projector). 그래서 값에는
// **창 끝까지의 미도달분**만 싣고, 조회 시점(Now)과의 비교는 게이트가 그대로
// 진다. 두 항을 곱씹으면 판정식은 "요청한 창 끝이 지났고, 그 대상의 데이터가
// 창 끝까지 도착했는가"가 된다 — 조항 이름(collect_lag_cutoff)의 의미 그대로다.
//
// 전부 fail-closed다:
//
//	observed_range 없음        → unknown (데이터 끝을 알 수 없다)
//	요청 창 끝 없음            → unknown (기준점이 없다)
//	봉투 no_data(backend)      → backend_error
//	봉투 no_data(그 외)        → unknown (창 안에 데이터가 아예 없다 — 0으로 흘리지 않는다)
//	데이터가 창 끝을 넘김      → 0초 (지연 없음)
func CollectLagOf(requestedTo time.Time, env RawEnvelope) CollectLag {
	if Status(env.Status) == StatusNoData {
		if NoDataReason(env.NoDataReason) == NoDataBackendError {
			return CollectLag{Status: TagBackendError}
		}
		// 창 안에 데이터가 없다 — 마지막 데이터 시각이 존재하지 않는다.
		// 여기서 0을 주면 "지연 없음"이 되어 §15.2-3이 닫은 문이 다시 열린다.
		return CollectLag{Status: TagUnknown}
	}
	if env.ObservedRange == nil || env.ObservedRange.To.IsZero() {
		return CollectLag{Status: TagUnknown}
	}
	if requestedTo.IsZero() {
		return CollectLag{Status: TagUnknown}
	}
	lag := int(requestedTo.UTC().Sub(env.ObservedRange.To.UTC()) / time.Second)
	if lag < 0 {
		// 요청 창 끝보다 뒤의 데이터까지 받았다 = 그 창에 대해서는 지연이 없다.
		lag = 0
	}
	return CollectLag{ValueS: lag, Status: TagObserved}
}
