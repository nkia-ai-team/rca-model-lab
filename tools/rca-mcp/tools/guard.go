// 백엔드 가드 — 서킷브레이커 + bulkhead + typed 백엔드 오류
// (§15.2-3, §14-6 6b).
//
// 계약:
//
//   - **오류는 typed다**: 백엔드 실패는 BackendError{Backend, Detail}로
//     분류되고, 봉투에는 분류 토큰만 실린다 — 백엔드 원문 오류 문자열
//     (접속 DSN·자격증명이 섞일 수 있다)은 error 사슬(로그·trace)에만
//     남는다(§15.3-2·C-12).
//   - **브레이커는 저장소별·프로세스 전역이다**: 연속 실패 N회 → open,
//     cooldown 경과 후 half-open 시험 1회 — 성공이면 close, 실패면 open
//     연장. 30초 blip이 run 나머지 전체를 backend_error로 만드는 것을
//     half-open이 막는다(§15.2-3).
//   - **bulkhead**: 백엔드별 동시 조회 상한 — run 하나가 규칙을 지켜도
//     합쳐서 고객 백엔드를 때리는 것을 막는 층(§15.2-7).
//   - 값은 전부 **가안(§14-7 실측 역산)**이며 환경변수로 조정 가능.
//
// PG는 이 브레이커에 편입하지 않는다(판단 지점, 6b 착공 기록): 도구
// 표면이 *sql.DB 직접이라 조회 단위 choke point가 없다 — 1차 가드는
// statement_timeout(§14-0)+SetMaxOpenConns(pg.go)가 지고, 편입은 §14-7
// 실측이 필요를 증명하면 driver 래핑과 함께 짓는다.
package tools

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"
)

// BackendError는 백엔드 실패의 typed 오류다. 봉투 사상(no_data
// backend_error)의 원천이며, 원문 오류는 Err 사슬에만 있다.
type BackendError struct {
	Backend string // vm | ch | pg
	Detail  string // 분류 토큰: circuit_open · connect_failed · timeout · http_5xx · http_4xx · decode_failed · cap_exceeded · bulkhead_timeout
	Err     error
}

func (e *BackendError) Error() string {
	if e.Err == nil {
		return fmt.Sprintf("backend %s: %s", e.Backend, e.Detail)
	}
	return fmt.Sprintf("backend %s: %s: %v", e.Backend, e.Detail, e.Err)
}
func (e *BackendError) Unwrap() error { return e.Err }

// classifyTransportErr는 전송 계열 오류의 분류 토큰이다.
func classifyTransportErr(err error) string {
	var ne net.Error
	switch {
	case errors.As(err, &ne) && ne.Timeout():
		return "timeout"
	case errors.Is(err, context.DeadlineExceeded):
		return "timeout"
	default:
		return "connect_failed"
	}
}

func classifyHTTPStatus(code int) string {
	if code >= 500 {
		return "http_5xx"
	}
	return "http_4xx"
}

// pgErr는 PG 조회 실패를 BackendError{Backend:"pg"}로 분류한다(§15.2-3,
// §14-7 2b). PG는 브레이커 미편입(아래 판단 지점)이지만 봉투 사상
// (withBackendGuard→no_data(backend_error))에는 typed 오류가 필요하다 —
// 종전엔 raw 오류가 그대로 올라가 guard에 안 닿았고, 도구 오류 문자열로
// LLM에 원문(접속 주소가 섞일 수 있다)이 노출됐다(§15.3-2 위반 실물).
// ctx 취소는 감싸지 않는다(run이 죽는 것 — §15.2-5). sql.ErrNoRows는
// 호출부가 이 함수 전에 처리해야 한다(0행은 실패가 아니다).
func pgErr(err error) error {
	if err == nil || errors.Is(err, context.Canceled) {
		return err
	}
	var be *BackendError
	if errors.As(err, &be) {
		return err
	}
	detail := "query_failed"
	var ne net.Error
	if errors.As(err, &ne) || errors.Is(err, context.DeadlineExceeded) {
		detail = classifyTransportErr(err)
	}
	return &BackendError{Backend: "pg", Detail: detail, Err: err}
}

// degradedIf는 PG 판별 원천 실패의 DegradedSources 값이다(D-3 — 현행
// 생산자는 scan/read의 counter 판별(vtErr)뿐) —
// nil이면 결손 없음(필드 생략).
func degradedIf(vtErr error) map[string]string {
	if vtErr == nil {
		return nil
	}
	return map[string]string{"pg": beDetail(pgErr(vtErr))}
}

// beDetail은 봉투에 실을 분류 토큰이다(§15.3-2 — 원문 오류 문자열 금지).
// 부분 강등(§15.2-3, 2b)의 결손 명시(source_errors)가 이 토큰을 쓴다.
func beDetail(err error) string {
	var be *BackendError
	if errors.As(err, &be) {
		return be.Detail
	}
	return "query_failed"
}

// ── 서킷브레이커 (§15.2-3) ──────────────────────────────────────

// 가안 값(§14-7 실측 역산 전). env로 조정 가능.
const (
	defaultBreakerFails    = 3                // RCA_BREAKER_FAILS
	defaultBreakerCooldown = 30 * time.Second // RCA_BREAKER_COOLDOWN_S
)

// Breaker는 저장소별 서킷브레이커다. 동시 사용 안전.
type Breaker struct {
	mu       sync.Mutex
	failsMax int
	cooldown time.Duration
	now      func() time.Time // 시험 주입
	states   map[string]*breakerState
}

type breakerState struct {
	fails     int
	openUntil time.Time
	probing   bool // half-open 시험 조회가 나가 있다
}

// defaultBreaker — 프로세스 전역(동시 run 공유, §15.2-3).
var defaultBreaker = NewBreaker()

func NewBreaker() *Breaker {
	b := &Breaker{
		failsMax: envInt("RCA_BREAKER_FAILS", defaultBreakerFails),
		cooldown: time.Duration(envInt("RCA_BREAKER_COOLDOWN_S", int(defaultBreakerCooldown/time.Second))) * time.Second,
		now:      time.Now,
		states:   map[string]*breakerState{},
	}
	return b
}

// Allow는 backend 조회 허용 여부다. open이면 circuit_open 오류를 즉시
// 돌려주고(백엔드를 때리지 않는다), cooldown이 지났으면 half-open 시험
// 1회만 통과시킨다.
func (b *Breaker) Allow(backend string) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	st := b.state(backend)
	if st.openUntil.IsZero() {
		return nil
	}
	if b.now().Before(st.openUntil) {
		return &BackendError{Backend: backend, Detail: "circuit_open"}
	}
	// cooldown 경과 — half-open: 시험 조회 1개만.
	if st.probing {
		return &BackendError{Backend: backend, Detail: "circuit_open"}
	}
	st.probing = true
	return nil
}

// Record는 조회 결과를 접수한다. 성공 = close(카운터 리셋), 실패 = 연속
// 실패 누적 → 임계면 open(half-open 시험 실패면 즉시 연장). 중립(취소·
// 비신호 오류)은 probing 반납만 한다.
func (b *Breaker) Record(backend string, err error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	st := b.state(backend)
	probing := st.probing
	st.probing = false
	if err == nil {
		st.fails = 0
		st.openUntil = time.Time{}
		return
	}
	if !countsAsBackendFailure(err) {
		return
	}
	st.fails++
	if probing || st.fails >= b.failsMax {
		st.openUntil = b.now().Add(b.cooldown)
	}
}

// DefaultBreaker는 프로세스 전역 브레이커다 — cmd의 실패 판정기가
// "전 원천 open → backend_down"(§15.5 사상표 7행)을 검사하는 표면.
func DefaultBreaker() *Breaker { return defaultBreaker }

// OpenBackends는 지금 open 상태인 백엔드 목록이다(정렬 — 결정론).
func (b *Breaker) OpenBackends() []string {
	b.mu.Lock()
	defer b.mu.Unlock()
	var out []string
	for name, st := range b.states {
		if !st.openUntil.IsZero() && b.now().Before(st.openUntil) {
			out = append(out, name)
		}
	}
	sort.Strings(out)
	return out
}

// abortProbe는 half-open 시험 허가를 반납한다 — Allow가 허가한 시험이
// 백엔드에 나가기도 전에(bulkhead·ctx) 좌초하면 Record가 안 불려 probing
// 이 영구 true로 고착됐다(6b 검증 D-1, 재현 실증).
func (b *Breaker) abortProbe(backend string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.state(backend).probing = false
}

// countsAsBackendFailure — 브레이커가 세는 실패는 "저장소가 죽었다"의
// 신호뿐이다(6b 검증 D-2): ctx 취소(run이 죽는 것), cap_exceeded(조회
// 모양 문제 — 백엔드는 건강), http_4xx(호출측 잘못)는 세지 않는다.
// decode_failed는 센다 — 깨진 응답은 백엔드·프록시 병의 신호다.
func countsAsBackendFailure(err error) bool {
	if errors.Is(err, context.Canceled) {
		return false
	}
	var be *BackendError
	if errors.As(err, &be) {
		switch be.Detail {
		case "cap_exceeded", "http_4xx", "bulkhead_timeout":
			return false
		}
	}
	return true
}

func (b *Breaker) state(backend string) *breakerState {
	st, ok := b.states[backend]
	if !ok {
		st = &breakerState{}
		b.states[backend] = st
	}
	return st
}

// ── bulkhead (§15.2-7) ──────────────────────────────────────────

// 가안 — 백엔드별 동시 조회 상한. RCA_BACKEND_CONCURRENCY로 조정.
const defaultBackendConcurrency = 4

var bulkheads = struct {
	mu   sync.Mutex
	sems map[string]chan struct{}
}{sems: map[string]chan struct{}{}}

// acquireBulkhead는 backend 슬롯 하나를 잡는다. ctx 취소까지 기다린다 —
// 대기는 정상이고(동시성 제한이 하는 일이 그것), 끝없는 대기는 호출당
// 시한·ctx가 끊는다.
func acquireBulkhead(ctx context.Context, backend string) (release func(), err error) {
	bulkheads.mu.Lock()
	sem, ok := bulkheads.sems[backend]
	if !ok {
		sem = make(chan struct{}, envInt("RCA_BACKEND_CONCURRENCY", defaultBackendConcurrency))
		bulkheads.sems[backend] = sem
	}
	bulkheads.mu.Unlock()
	select {
	case sem <- struct{}{}:
		return func() { <-sem }, nil
	case <-ctx.Done():
		return nil, &BackendError{Backend: backend, Detail: "bulkhead_timeout", Err: ctx.Err()}
	}
}

// guardedCall은 breaker+bulkhead를 두른 백엔드 호출 한 번이다. 반환
// 오류는 항상 BackendError(또는 nil)다.
func guardedCall[T any](ctx context.Context, backend string, fn func() (T, error)) (T, error) {
	var zero T
	if err := defaultBreaker.Allow(backend); err != nil {
		return zero, err
	}
	release, err := acquireBulkhead(ctx, backend)
	if err != nil {
		// bulkhead 대기 실패는 백엔드 실패가 아니다 — 브레이커에 안 세되,
		// Allow가 내준 half-open 시험 허가는 반납한다(D-1 — 안 하면
		// probing 고착으로 그 백엔드가 영구 차단된다).
		defaultBreaker.abortProbe(backend)
		return zero, err
	}
	defer release()
	out, err := fn()
	defaultBreaker.Record(backend, err)
	return out, err
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}
