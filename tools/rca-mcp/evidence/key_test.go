package evidence

import "testing"

func base() ObservationKey {
	return ObservationKey{
		Source: SrcScanMetrics, TargetID: "svc-1", Aspect: AspectMetric,
		Metric: "http.latency.p95", EntityKey: "", Window: WindowFull,
	}
}

// 표기 흔들림(대소문자·앞뒤 공백·연속 공백)은 새 키가 아니다(§6.0).
func TestCanonicalAbsorbsSpelling(t *testing.T) {
	a := base()
	b := base()
	b.TargetID = "  SVC-1 "
	b.Metric = "HTTP.Latency.P95"
	b.Aspect = "Metric"
	if a.Canonical() != b.Canonical() {
		t.Fatalf("표기 흔들림이 다른 키가 됨:\n%v\n%v", a.Canonical(), b.Canonical())
	}
	c := base()
	c.EntityKey = "tmpl   42"
	d := base()
	d.EntityKey = "TMPL 42"
	if c.Canonical() != d.Canonical() {
		t.Fatalf("EntityKey 공백 접기 실패: %v ≠ %v", c.Canonical(), d.Canonical())
	}
}

// 키에 Predicate·Threshold가 없다는 것은 타입 수준의 계약이다 —
// SignalPred가 어떤 연산자를 쓰든 같은 관측 키로 접힌다(ledger 쪽
// TestObservationKeyIgnoresPredicate가 짝 검사).
func TestKeyHasNoPredicateAxis(t *testing.T) {
	// 키 성분은 6개뿐. 필드가 늘면 이 시험이 깨져 재검토를 강제한다.
	k := base()
	if got := k.String(); got != "scan_metrics|svc-1|metric|http.latency.p95||full" {
		t.Fatalf("키 표기 = %q", got)
	}
}

func TestWindowIsPartOfKey(t *testing.T) {
	a, b := base(), base()
	b.Window = WindowOnsetNarrow
	if a.Canonical() == b.Canonical() {
		t.Fatal("창 클래스가 키에서 빠짐 — full과 onset_narrow는 별개 head(§5.7)")
	}
}

// C-3: 존재하지 않는 개체를 지목한 absent 술어는 개체 행으로는 영영
// 해소되지 않는다. 범위 행(EntityAny)이 그 키를 덮어야 한다.
func TestScopeCoversAbsentEntity(t *testing.T) {
	probe := base()
	probe.EntityKey = "sql_key:deadbeef" // 실재하지 않는 개체
	scope := probe.Scope()

	if scope.EntityKey != EntityAny {
		t.Fatalf("범위 키의 개체 축 = %q", scope.EntityKey)
	}
	if !scope.Covers(probe) {
		t.Fatal("범위 행이 개체 술어를 덮지 못함 — 영구 미판정")
	}
	if probe.Covers(scope) {
		t.Fatal("개체 행이 범위를 덮으면 안 된다")
	}
	// 범위가 다르면 덮지 않는다.
	other := probe
	other.TargetID = "svc-2"
	if scope.Covers(other) {
		t.Fatal("다른 대상까지 덮음")
	}
	other = probe
	other.Window = WindowOnsetNarrow
	if scope.Covers(other) {
		t.Fatal("다른 창까지 덮음")
	}
	// 자기 자신은 덮는다(정확 일치 경로).
	if !probe.Covers(probe) {
		t.Fatal("같은 키를 덮지 못함")
	}
}

// B-15: 합성 원천 change는 registry에 없는 이름이다 — 실행 채널은
// list_changes로 해소돼야 한다.
func TestChangeSourceResolvesToExecutableTool(t *testing.T) {
	tool, ok := ExecutableTool(SrcChange)
	if !ok || tool != "list_changes" {
		t.Fatalf("change 원천의 실행 채널 = %q(ok=%v)", tool, ok)
	}
	if tool, _ := ExecutableTool(SrcDBBlocking); tool != "db_blocking" {
		t.Fatalf("db_blocking 실행 채널 = %q", tool)
	}
	// 술어 불가 도구는 원천 어휘에 없다(§5.1).
	for _, bad := range []ObservationSource{"describe_target", "expand_topology", "get_data_coverage", ""} {
		if ValidSource(bad) {
			t.Fatalf("%q가 원천 어휘에 있음 — 사상표에 행이 없는 도구다", bad)
		}
	}
}

func TestValidWindowClass(t *testing.T) {
	if !ValidWindowClass(WindowFull) || !ValidWindowClass(WindowOnsetNarrow) {
		t.Fatal("정상 창 클래스가 거부됨")
	}
	if ValidWindowClass("wide") || ValidWindowClass("") {
		t.Fatal("미정의 창 클래스가 통과됨")
	}
}
