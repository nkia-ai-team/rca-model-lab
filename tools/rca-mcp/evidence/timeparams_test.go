package evidence

import (
	"os"
	"testing"
	"time"
)

// TestClockSkewResolve — 배포 파라미터의 3상태(§10 fail-closed).
func TestClockSkewResolve(t *testing.T) {
	cases := []struct {
		name string
		in   ClockSkewSetting
		want TagStatus
		val  int
	}{
		{"미입력은 unknown", ClockSkewSetting{}, TagUnknown, 0},
		{"0초 입력은 observed — 미입력과 다르다", ClockSkewSetting{BoundS: 0, Known: true}, TagObserved, 0},
		{"허용치 이내", ClockSkewSetting{BoundS: 2, Known: true}, TagObserved, 2},
		{"허용치 초과", ClockSkewSetting{BoundS: 3, Known: true}, TagExceeded, 3},
		{"허용치 상향하면 통과", ClockSkewSetting{BoundS: 3, Known: true, ToleranceS: 5}, TagObserved, 3},
		{"음수는 unknown", ClockSkewSetting{BoundS: -1, Known: true}, TagUnknown, 0},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := c.in.Resolve()
			if got.Status != c.want {
				t.Fatalf("status=%s, want %s", got.Status, c.want)
			}
			if got.Status == TagUnknown {
				return
			}
			if got.BoundS != c.val {
				t.Fatalf("bound=%d, want %d", got.BoundS, c.val)
			}
			// exceeded는 값이 있어도 §10 산식을 호출할 수 없다.
			if want := c.want == TagObserved; got.Usable() != want {
				t.Fatalf("usable=%v, want %v", got.Usable(), want)
			}
		})
	}
}

// TestClockSkewFromEnv — 미설정은 unknown, 오타는 오류(설정했다고 믿는
// 미설정을 만들지 않는다).
func TestClockSkewFromEnv(t *testing.T) {
	t.Setenv(EnvClockSkewBound, "")
	t.Setenv(EnvClockSkewTolerance, "")
	os.Unsetenv(EnvClockSkewBound)
	os.Unsetenv(EnvClockSkewTolerance)
	s, err := ClockSkewFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if s.Resolve().Status != TagUnknown {
		t.Fatalf("미설정인데 %s", s.Resolve().Status)
	}

	t.Setenv(EnvClockSkewBound, "1")
	s, err = ClockSkewFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if got := s.Resolve(); got.Status != TagObserved || got.BoundS != 1 {
		t.Fatalf("%+v", got)
	}

	t.Setenv(EnvClockSkewBound, "2초")
	if _, err := ClockSkewFromEnv(); err == nil {
		t.Fatal("오타를 unknown으로 삼켰다")
	}
	t.Setenv(EnvClockSkewBound, "-1")
	if _, err := ClockSkewFromEnv(); err == nil {
		t.Fatal("음수를 받았다")
	}
}

// TestCollectLagFromLiveEnvelope — **실물 봉투에서 산출된다**(§5.5 원천
// 결정 2026-08-04). 원천은 collectors 메타데이터가 아니라 그 조회가 창 안에서
// 실제로 받아온 관측 범위다.
//
// 종전 원천(collectors.last_collected_at)이 폐기된 이유가 이 fixture에
// 그대로 남아 있다: envelope_get_data_coverage_collectors.json의 두 대상 중
// 하나는 last_collected_at이 14일 낡았고 하나는 NULL인데, **둘 다 창 안에
// 관측을 내고 있다**(window_observations: vm_series 2·1764). 죽은 것은
// 수집이 아니라 메타데이터였다.
func TestCollectLagFromLiveEnvelope(t *testing.T) {
	env := loadEnvelopeFixture(t, "testdata/envelope_scan_metrics.json")
	if env.ObservedRange == nil {
		t.Fatal("fixture에 observed_range가 없다 — 이 시험의 전제가 깨졌다")
	}
	obsTo := env.ObservedRange.To.UTC()

	// ① 요청 창 끝이 관측 끝과 같다 = 창 끝까지 데이터가 왔다 → 지연 0 실측.
	if l := CollectLagOf(obsTo, env); l.Status != TagObserved || l.ValueS != 0 {
		t.Fatalf("%+v — 창 끝까지 데이터가 온 조회의 지연은 0 실측이다", l)
	}
	// ② 요청 창이 관측보다 10분 더 뒤까지였다면 그만큼이 미도달분이다.
	if l := CollectLagOf(obsTo.Add(10*time.Minute), env); l.Status != TagObserved || l.ValueS != 600 {
		t.Fatalf("%+v — 요청 창 끝 − 마지막 데이터 시각이어야 한다", l)
	}
	// ③ 종전 원천이라면 미상이었을 봉투가 실측을 낸다 — 이것이 교체의 요점이다.
	cov := loadEnvelopeFixture(t, "testdata/envelope_get_data_coverage_collectors.json")
	if l := CollectLagOf(cov.ObservedRange.To, cov); !l.Usable() {
		t.Fatalf("%+v — collectors가 NULL·14일 낡아도 관측은 창 안에 있다", l)
	}
}

// TestCollectLagRules — 산출 규칙 전수(전부 fail-closed).
func TestCollectLagRules(t *testing.T) {
	end := time.Date(2026, 8, 3, 4, 0, 0, 0, time.UTC)
	mk := func(obsTo time.Time) RawEnvelope {
		e := RawEnvelope{Status: "normal"}
		if !obsTo.IsZero() {
			e.ObservedRange = &RawRange{From: end.Add(-30 * time.Minute), To: obsTo}
		}
		return e
	}

	t.Run("창 끝까지 데이터가 왔으면 지연 0", func(t *testing.T) {
		if l := CollectLagOf(end, mk(end)); l.Status != TagObserved || l.ValueS != 0 {
			t.Fatalf("%+v", l)
		}
	})
	t.Run("데이터가 일찍 끊기면 그 간격이 지연", func(t *testing.T) {
		if l := CollectLagOf(end, mk(end.Add(-90*time.Second))); l.Status != TagObserved || l.ValueS != 90 {
			t.Fatalf("%+v", l)
		}
	})
	t.Run("요청 창 끝보다 뒤의 데이터까지 받았으면 지연 0", func(t *testing.T) {
		if l := CollectLagOf(end, mk(end.Add(5*time.Minute))); l.Status != TagObserved || l.ValueS != 0 {
			t.Fatalf("%+v", l)
		}
	})
	t.Run("observed_range 없으면 unknown", func(t *testing.T) {
		if l := CollectLagOf(end, mk(time.Time{})); l.Status != TagUnknown {
			t.Fatalf("%+v", l)
		}
	})
	t.Run("요청 창 끝이 없으면 unknown", func(t *testing.T) {
		if l := CollectLagOf(time.Time{}, mk(end)); l.Status != TagUnknown {
			t.Fatalf("%+v", l)
		}
	})
	t.Run("창 안에 데이터가 없으면 0이 아니라 unknown", func(t *testing.T) {
		e := mk(end)
		e.Status, e.NoDataReason = "no_data", string(NoDataZeroObservations)
		if l := CollectLagOf(end, e); l.Status != TagUnknown || l.ValueS != 0 {
			t.Fatalf("%+v — 관측 부재를 지연 0으로 흘리면 fail-closed가 뒤집힌다", l)
		}
	})
	t.Run("백엔드 실패는 backend_error", func(t *testing.T) {
		e := mk(end)
		e.Status, e.NoDataReason = "no_data", string(NoDataBackendError)
		if l := CollectLagOf(end, e); l.Status != TagBackendError {
			t.Fatalf("%+v", l)
		}
	})
}

func loadEnvelopeFixture(t *testing.T, path string) RawEnvelope {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	env, err := DecodeEnvelope(b)
	if err != nil {
		t.Fatal(err)
	}
	return env
}
