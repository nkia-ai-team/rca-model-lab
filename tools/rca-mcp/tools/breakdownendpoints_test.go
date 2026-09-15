// breakdown_endpoints 단위 가드 — 실 CH 없이 §15의 결정들이 코드로
// 지켜지는지. 시나리오는 전부 라이브 실측에서 온 것이다:
// 07-27 04:00 core-banking 사건(기준선 보존 밖) · 07-28 23:30
// food-delivery-restaurant(저지연 배율 사건) · gateway 만성 51% 404 ·
// 스케줄러 OutcomeTrackingRunnable.run 배경 소음.
package tools

import (
	"strings"
	"testing"
	"time"
)

func beMk(section, kind, label string, n int, p50, p95 float64) *beRow {
	return &beRow{section: section, kind: kind, label: label, axis: beAxisOf(kind, label),
		n: n, uniqN: n, p50: p50, p95: p95, totalMs: float64(n) * p50}
}

func beJudged(row, base *beRow) []string {
	rows := []*beRow{row}
	var idx map[string]*beRow
	state := "unavailable"
	if base != nil {
		base.section, base.kind, base.label = row.section, row.kind, row.label
		idx = beIndex([]*beRow{base})
		state = "ok"
	}
	beDeriveErrors(rows)
	if base != nil {
		beDeriveErrors([]*beRow{base})
	}
	beJudge(rows, idx, state)
	return rows[0].arms
}

func beHas(arms []string, want string) bool {
	for _, a := range arms {
		if a == want {
			return true
		}
	}
	return false
}

// 결정 1(§15.2) — SERVER span의 절반은 루트가 아니다(원격 호출자가 부모).
// 루트 여부로 구획을 가르면 같은 엔드포인트가 두 줄로 갈린다.
func TestBESectionAssignment(t *testing.T) {
	cases := []struct {
		kind string
		root bool
		want string
	}{
		{"SERVER", true, "entry"}, {"SERVER", false, "entry"},
		{"CONSUMER", false, "entry"}, // 100% 부모를 갖지만 진입점이다
		{"INTERNAL", true, "entry"},  // 스케줄러
		{"INTERNAL", false, "step"},  // ORM
		{"CLIENT", false, "egress"}, {"PRODUCER", false, "egress"},
	}
	for _, c := range cases {
		if got := beSectionOf(c.kind, c.root); got != c.want {
			t.Fatalf("%s(root=%v) → %s, want %s", c.kind, c.root, got, c.want)
		}
	}
	// CH측 식과 Go측 규칙이 같은 문장을 쓰는지(문자열 가드).
	for _, frag := range []string{"'entry'", "'step'", "'egress'", "parent_span_id = ''"} {
		if !strings.Contains(beSectionExpr, frag) {
			t.Fatalf("beSectionExpr에 %s 없음", frag)
		}
	}
}

// 결정 1(§15.2) — CLIENT는 span_name이 HTTP 메서드뿐이라 축을 속성으로
// 복원한다. 축 이름은 응답에 드러낸다.
func TestBEAxisNaming(t *testing.T) {
	cases := []struct{ kind, label, want string }{
		{"SERVER", "GET /api/products/{id}", "http.route"},
		{"CONSUMER", "food.dispatch process", "messaging.destination"},
		{"PRODUCER", "commerce.orders publish", "messaging.destination"},
		{"INTERNAL", "hibernate-6.0 :: Transaction.commit", "framework_segment"},
		{"CLIENT", "db oracle SELECT accounts", "db_call"},
		{"CLIENT", "http POST testbed-payment:8083", "http_call"},
		{"CLIENT", "peer 10.0.0.1", "peer"},
	}
	for _, c := range cases {
		if got := beAxisOf(c.kind, c.label); got != c.want {
			t.Fatalf("%s/%q → %s, want %s", c.kind, c.label, got, c.want)
		}
	}
}

// 결정 2(§15.3) — 실패와 거절을 분리한다. SERVER의 4xx는 실패가 아니고
// (OTel 규약상 status_code도 ERROR가 아니다), db CLIENT엔 거절 개념이 없다.
func TestBEErrorSeparation(t *testing.T) {
	srv := beMk("entry", "SERVER", "GET /api/products/**", 2625, 3.5, 4.9)
	srv.http4xx, srv.httpN = 1349, 2625 // 만성 51% 404
	db := beMk("egress", "CLIENT", "db oracle SELECT accounts", 644, 0.9, 1.4)
	db.statusErr = 3
	orm := beMk("step", "INTERNAL", "hibernate-6.0 :: SELECT com.commerce.inventory.entity.Inventory", 4443, 0.3, 1.1)
	orm.statusErr = 3817 // NoResultException 85.9%
	rows := []*beRow{srv, db, orm}
	beDeriveErrors(rows)

	if srv.failedN != 0 || srv.rejectedN != 1349 {
		t.Fatalf("SERVER 4xx는 거절이어야 함: failed=%d rejected=%d", srv.failedN, srv.rejectedN)
	}
	if srv.rejRate < 0.51 || srv.rejRate > 0.52 {
		t.Fatalf("거절율은 http 응답 보유 span 대비여야 함: %v", srv.rejRate)
	}
	if db.semantics != "db" || db.rejectedN != -1 {
		t.Fatalf("db CLIENT엔 거절 개념이 없어야 함: %s rejected=%d", db.semantics, db.rejectedN)
	}
	if orm.semantics != "framework" || orm.failedN != 3817 {
		t.Fatalf("ORM ERROR는 framework 의미로 남아야 함: %s failed=%d", orm.semantics, orm.failedN)
	}
}

// 결정 2 단위 가드(§15.3) — toUInt16OrZero가 빈 속성을 0으로 만들기 때문에
// fallback 조건은 "http status 문자열이 비어 있고"여야 한다. SQL 문면 가드.
func TestBEFallbackGuardsEmptyString(t *testing.T) {
	if !strings.Contains(beAggSQL, `span_attributes['http.response.status_code'] = '' AND status_code = 'ERROR'`) {
		t.Fatal("fallback이 빈 문자열 조건을 명시해야 함 — 숫자 0으로 판단하면 5xx 비교에서 조용히 빠진다")
	}
	if !strings.Contains(beAggSQL, "uniqExact((trace_id, span_id))") {
		t.Fatal("저장 중복 검증(uniqExact)이 집계 안에 있어야 함")
	}
}

// ★ 결정 3(§15.4) — L0(p50 3배)는 SERVER·CONSUMER에만 적용한다.
// 배경 발화 11건이 전부 INTERNAL 스케줄러였고, kind별로 재면 SERVER
// 4,185쌍·CONSUMER 650쌍에서 3배 초과가 0건이다.
func TestBELatencyRatioArmExcludesScheduler(t *testing.T) {
	// 실측: food-delivery-restaurant 07-28 23:30 — p50 1.65 → 11.58ms.
	srv := beMk("entry", "SERVER", "GET /api/restaurants/{id}/menu", 1383, 11.58, 83.36)
	srvBase := beMk("entry", "SERVER", "GET /api/restaurants/{id}/menu", 338, 1.65, 2.85)
	if arms := beJudged(srv, srvBase); !beHas(arms, "latency_ratio") {
		t.Fatalf("SERVER 저지연 배율 사건은 L0가 잡아야 함: %v", arms)
	}
	// 실측: 스케줄러 배경 소음 — p50 1.19 → 5.89ms(4.9배)인데 발화하면 안 된다.
	sch := beMk("entry", "INTERNAL", "spring-scheduling-3.1 :: OutcomeTrackingRunnable.run", 87, 5.89, 29.7)
	schBase := beMk("entry", "INTERNAL", "spring-scheduling-3.1 :: OutcomeTrackingRunnable.run", 90, 1.19, 8.8)
	if arms := beJudged(sch, schBase); len(arms) != 0 {
		t.Fatalf("스케줄러는 p50 배율로 발화하면 안 됨(주기 배치는 작업량이 폴마다 다르다): %v", arms)
	}
}

// 결정 3(§15.4) — L1의 300ms 절대 하한이 저지연 사건을 죽인다는 것이
// 합류의 근거였다. 하한 자체는 유지되어야 한다(L0가 그 구멍을 메운다).
func TestBELatencyShiftFloorHolds(t *testing.T) {
	r := beMk("entry", "SERVER", "GET /api/restaurants", 507, 23.72, 83.1)
	b := beMk("entry", "SERVER", "GET /api/restaurants", 46, 1.9, 2.76)
	arms := beJudged(r, b)
	if beHas(arms, "latency_shift") {
		t.Fatalf("p95 83ms는 300ms 하한 미만이라 L1이 발화하면 안 됨: %v", arms)
	}
	if !beHas(arms, "latency_ratio") {
		t.Fatalf("대신 L0가 잡아야 함: %v", arms)
	}
}

// 결정 3(§15.4) — L2(규모)는 기준선을 요구하지 않는다. 07-27 사건은
// 기준선 창이 보존(3일) 밖이라 변화 팔이 전부 꺼지는데도 잡혀야 한다.
func TestBEMagnitudeArmNeedsNoBaseline(t *testing.T) {
	r := beMk("entry", "SERVER", "POST /api/accounts/transfer", 72, 605.56, 15619.89)
	r.http5xx, r.httpN = 57, 72
	arms := beJudged(r, nil)
	if !beHas(arms, "latency_magnitude") {
		t.Fatalf("기준선 없이도 규모 팔이 잡아야 함: %v", arms)
	}
	if !beHas(arms, "failure_magnitude") {
		t.Fatalf("5xx 79%%는 실패 규모 팔이 잡아야 함: %v", arms)
	}
}

// 결정 2·3(§15.3·15.4) — framework 의미는 실패 규모 팔로 판정하지 않는다.
// NoResultException이 한 행의 85.9%다.
func TestBEFrameworkErrorsDoNotFireMagnitude(t *testing.T) {
	r := beMk("step", "INTERNAL", "hibernate-6.0 :: SELECT com.commerce.inventory.entity.Inventory", 4443, 0.3, 1.1)
	r.statusErr = 3817
	b := beMk("step", "INTERNAL", "hibernate-6.0 :: SELECT com.commerce.inventory.entity.Inventory", 4400, 0.3, 1.1)
	b.statusErr = 3780
	if arms := beJudged(r, b); len(arms) != 0 {
		t.Fatalf("framework ERROR는 발화하면 안 됨: %v", arms)
	}
}

// ★ 결정 3(§15.4) 회귀 가드 — 만성 4xx는 발화하지 않는다. 이 가드가
// 잡은 실제 결함: 기준선 행에 실패/거절 파생을 돌리지 않아 base.rejRate가
// 0으로 남고 "기준선 2배" 조건이 항상 참이 되어, gateway 만성 51% 404가
// 4행에서 rejection_shift로 발화했다.
func TestBEChronic4xxDoesNotFire(t *testing.T) {
	r := beMk("entry", "SERVER", "GET /api/products/**", 2625, 3.54, 4.89)
	r.http4xx, r.httpN = 1349, 2625 // 51.4%
	b := beMk("entry", "SERVER", "GET /api/products/**", 2530, 3.5, 4.8)
	b.http4xx, b.httpN = 1290, 2530 // 51.0% — 만성
	if arms := beJudged(r, b); len(arms) != 0 {
		t.Fatalf("만성 4xx는 발화하면 안 됨: %v", arms)
	}
	// 진짜 급변(51% → 92%)은 잡아야 한다.
	r2 := beMk("entry", "SERVER", "GET /api/products/**", 2625, 3.54, 4.89)
	r2.http4xx, r2.httpN = 2415, 2625
	if arms := beJudged(r2, b); !beHas(arms, "rejection_shift") {
		t.Fatalf("거절율 급변은 잡아야 함: %v", arms)
	}
}

// 결정 3(§15.4) 전제 — 표본 하한 미달은 판정하지 않는다. 야간에는 전
// 서비스 트래픽이 시간당 ~1,000 span으로 떨어져 상시 발생한다.
func TestBEMinSampleBlocksJudgement(t *testing.T) {
	r := beMk("entry", "SERVER", "GET /actuator/health", 22, 100, 9000)
	if arms := beJudged(r, nil); len(arms) != 0 {
		t.Fatalf("표본 %d < %d면 판정하지 않아야 함: %v", r.uniqN, beMinSample, arms)
	}
}

// 결정 4(§15.6) — self-time은 자식을 부모별로 먼저 접고 직계 자식만 뺀다.
// SQL 문면 가드(두 함정 모두 이 두 문장으로 닫힌다).
func TestBESelfTimeSQLShape(t *testing.T) {
	if !strings.Contains(beSelfSQL, "GROUP BY trace_id, parent_span_id") {
		t.Fatal("자식을 부모별로 먼저 접어야 함 — 안 접으면 부모가 자식 수만큼 중복 계상된다")
	}
	if !strings.Contains(beSelfSQL, "e.span_id = k.parent_span_id") {
		t.Fatal("직계 자식만 조인해야 함 — 자손을 쓰면 ORM↔JDBC가 중복 차감된다")
	}
	if !strings.Contains(beSelfSQL, "least(p.child, p.dur)") {
		t.Fatal("self는 0으로 clamp해야 함(안전망)")
	}
}

// 결정 5(§15.7) — 구획 교차 비율을 만들지 않는다. 스케줄러 트레이스가
// SERVER 밖에 있어 분모에 대응물이 없다.
func TestBENoCrossSectionRatio(t *testing.T) {
	rows := []*beRow{
		beMk("entry", "SERVER", "POST /api/orders", 100, 10, 20),
		beMk("egress", "CLIENT", "http POST testbed-payment:8083", 100, 8, 9),
	}
	env := beEnvelope("svc", "service_name", rows, map[string]bool{"entry": true, "egress": true},
		beDefaultTopN, time.Now().Add(-30*time.Minute), time.Now(),
		time.Now().Add(-time.Hour), time.Now().Add(-30*time.Minute), "ok", new(truncAcc))
	for _, f := range env.Findings {
		for k := range f {
			if k == "time_share_of_server" || k == "self_processing_ratio" {
				t.Fatalf("구획 교차 비율 필드가 있으면 안 됨: %s", k)
			}
		}
		if share, ok := f["time_share_in_section"]; ok {
			if v, _ := share.(float64); v > 1.0 {
				t.Fatalf("구획 내부 비율이 1을 넘음: %v", v)
			}
		}
	}
}

// 결정 6(§15.8) — 판정 행은 top_n 밖이어도 emit한다. 판정을 내려 놓고
// 근거 행을 잘라내면 조사자가 인용할 수 없다.
func TestBEAnomalousRowSurvivesTruncation(t *testing.T) {
	var rows []*beRow
	for i := 0; i < 25; i++ {
		r := beMk("step", "INTERNAL", "seg"+string(rune('a'+i)), 100, float64(100-i), 1)
		r.totalMs = float64(10000 - i*100) // 앞쪽이 시간 점유가 크다
		rows = append(rows, r)
	}
	rows[24].arms = []string{"latency_shift"} // 시간 점유 꼴찌인데 판정됨
	env := beEnvelope("svc", "service_name", rows, map[string]bool{"step": true}, 5,
		time.Now().Add(-30*time.Minute), time.Now(), time.Now().Add(-time.Hour),
		time.Now().Add(-30*time.Minute), "ok", new(truncAcc))
	found := false
	for _, f := range env.Findings {
		if f["label"] == rows[24].label {
			found = true
		}
	}
	if !found {
		t.Fatal("판정된 행은 top_n 밖이어도 emit되어야 함")
	}
	if !env.Truncated {
		t.Fatal("잘린 행이 있으면 truncated여야 함")
	}
	if env.Status != "anomalous" {
		t.Fatalf("판정 행이 있으면 anomalous: %s", env.Status)
	}
}

// verdict_arms는 nil이 아니라 빈 배열이다 — JSON null은 "판정하지 않음"과
// "팔이 붙지 않음"을 구분하지 못한다.
func TestBEArmsNeverNull(t *testing.T) {
	rows := []*beRow{beMk("entry", "SERVER", "GET /x", 100, 1, 2)}
	env := beEnvelope("svc", "service_name", rows, map[string]bool{"entry": true}, beDefaultTopN,
		time.Now().Add(-30*time.Minute), time.Now(), time.Now().Add(-time.Hour),
		time.Now().Add(-30*time.Minute), "ok", new(truncAcc))
	arms, ok := env.Findings[0]["verdict_arms"].([]string)
	if !ok || arms == nil {
		t.Fatalf("verdict_arms는 빈 배열이어야 함: %#v", env.Findings[0]["verdict_arms"])
	}
}

// Historical captures can retain data older than the live TTL.
func TestBEBaselineOutsideRetention(t *testing.T) {
	old := time.Date(2000, 1, 1, 10, 0, 0, 0, time.UTC)
	oldFrom, oldTo, state := beBaselineWindow(old, old.Add(30*time.Minute))
	if state != "ok" || !oldTo.Equal(old) || !oldFrom.Equal(old.Add(-30*time.Minute)) {
		t.Fatalf("과거 기준선도 실제 조회해야 함: %s %s~%s", state, oldFrom, oldTo)
	}
	now := time.Now().UTC()
	bf, bt, st := beBaselineWindow(now.Add(-30*time.Minute), now)
	if st != "ok" || !bt.Equal(now.Add(-30*time.Minute)) || bf.After(bt) {
		t.Fatalf("직전 동일 길이 창이어야 함: %s %s~%s", st, bf, bt)
	}
	// 창이 15분보다 짧으면 기준선은 15분으로 늘린다(표본 확보).
	bf2, bt2, _ := beBaselineWindow(now.Add(-5*time.Minute), now)
	if bt2.Sub(bf2) < beMinWindow {
		t.Fatalf("기준선 최소 길이 %v 미만: %v", beMinWindow, bt2.Sub(bf2))
	}
}

// 결정 7(§15.9) — 메시지 구동 서비스는 SERVER가 헬스체크뿐이라는 사실을
// 응답이 말해야 한다. 말하지 않으면 "SERVER에 없다 = 트래픽 없다"로 오독된다.
func TestBELimitsSurfaceHealthOnlyService(t *testing.T) {
	rows := []*beRow{
		beMk("entry", "SERVER", "GET /actuator/health", 20, 0.5, 0.7),
		beMk("entry", "CONSUMER", "food.dispatch process", 302, 0.07, 0.27),
	}
	lim := beLimits(rows, map[string]bool{"entry": true}, "ok")
	joined := strings.Join(lim, "\n")
	if !strings.Contains(joined, "헬스체크뿐") {
		t.Fatalf("헬스체크 전용 사실이 limits에 없음:\n%s", joined)
	}
	if !strings.Contains(joined, "실제 조회 결과") || strings.Contains(joined, "보존은 3일") {
		t.Fatalf("실제 가용성 기준이어야 함:\n%s", joined)
	}
	// 4xx 만성 문구는 거절이 있을 때만.
	if strings.Contains(joined, "4xx는 이 환경에서 만성") {
		t.Fatal("거절 0건인데 4xx 문구가 실렸다")
	}
}

// 인자 계약 — sections 기본은 셋 전부, 알 수 없는 값은 무시.
func TestBEWantSections(t *testing.T) {
	all := beWantSections(nil)
	if !all["entry"] || !all["step"] || !all["egress"] {
		t.Fatalf("기본은 셋 전부: %v", all)
	}
	one := beWantSections([]string{"Entry", "없는구획"})
	if !one["entry"] || one["step"] || one["egress"] {
		t.Fatalf("부분 요청이 반영되어야 함: %v", one)
	}
}
