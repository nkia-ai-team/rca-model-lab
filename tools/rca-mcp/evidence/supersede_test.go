package evidence

import (
	"strings"
	"testing"
)

// keyed는 관측 키 축을 바꾼 최소 레코드다.
func keyed(metric, entity string, w WindowClass) EvidenceIndexRecord {
	r := minimalRecord()
	r.Effect.Metric = metric
	r.EntityKey = entity
	r.Window.Class = w
	return r
}

// supersede는 "동일 selector의 active head 하나 → 새 head 하나"인 함수다.
// selector가 다르면 대체가 아니다 — 이 검사가 없으면 재조회 하나가 무관한
// 관측의 head를 지운다.
func TestSupersedeRequiresSameSelector(t *testing.T) {
	ix := NewIndex()
	base, err := ix.Append(keyed("cpu.usage", "", WindowFull))
	if err != nil {
		t.Fatal(err)
	}
	other := keyed("mem.usage", "", WindowFull)
	other.Supersedes = base
	if _, err := ix.Append(other); err == nil {
		t.Fatal("다른 지표의 레코드가 대체를 통과했다 — supersede가 함수가 아니게 된다")
	} else if !strings.Contains(err.Error(), "selector 불일치") {
		t.Fatalf("사유가 selector 불일치가 아니다: %v", err)
	}
	// 종류가 다른 레코드도 서로의 head가 아니다.
	sc := keyed("cpu.usage", "", WindowFull)
	sc.RecordKind = KindQueryScope
	sc.EntityKey = EntityAny
	sc.Scope = &QueryScope{Selector: ObservationKey{}}
	sc.Scope.Selector = sc.Key()
	scEID, err := ix.Append(sc)
	if err != nil {
		t.Fatal(err)
	}
	f := keyed("cpu.usage", EntityAny, WindowFull)
	f.Supersedes = scEID
	if _, err := ix.Append(f); err == nil {
		t.Fatal("finding이 query_scope를 대체했다")
	}
}

// 4차 A-3의 핵심: full과 onset_narrow는 **서로 다른 head로 공존**한다.
// 창을 키에서 빼면 W2 고해상도 재조회가 full 창 관측(반증 근거 포함)을
// 조용히 지우고, 실측 4/8에서 오답 confirmed가 났다.
func TestWindowClassesAreSeparateHeads(t *testing.T) {
	ix := NewIndex()
	full, _ := ix.Append(keyed("cpu.usage", "", WindowFull))
	narrow, err := ix.Append(keyed("cpu.usage", "", WindowOnsetNarrow))
	if err != nil {
		t.Fatal(err)
	}
	if len(ix.Active()) != 2 {
		t.Fatal("창 클래스가 다른 두 관측이 하나로 접혔다")
	}
	fk := ObservationKey{Source: SrcScanMetrics, TargetID: "t1", Aspect: AspectMetric,
		Metric: "cpu.usage", Window: WindowFull}
	nk := fk
	nk.Window = WindowOnsetNarrow
	if h := ix.Heads(fk); len(h) != 1 || h[0] != full {
		t.Fatalf("full head가 %v", h)
	}
	if h := ix.Heads(nk); len(h) != 1 || h[0] != narrow {
		t.Fatalf("onset_narrow head가 %v", h)
	}
	// onset_narrow 재조회는 자기 창의 head만 대체한다.
	r := keyed("cpu.usage", "", WindowOnsetNarrow)
	r.Supersedes = narrow
	neu, err := ix.Append(r)
	if err != nil {
		t.Fatal(err)
	}
	if h := ix.Heads(fk); len(h) != 1 || h[0] != full {
		t.Fatalf("onset_narrow 재조회가 full head를 건드렸다: %v", h)
	}
	if h := ix.Heads(nk); len(h) != 1 || h[0] != neu {
		t.Fatalf("onset_narrow head가 후계로 넘어가지 않았다: %v", h)
	}
}

// Refines는 supersede가 아니다 — 원본 head는 active로 남고 정밀본은 자기
// 키의 head가 된다(원본을 지우면 그 레코드에 결박된 반증·지지가 증발한다).
func TestRefinesKeepsOriginalActive(t *testing.T) {
	ix := NewIndex()
	scan, _ := ix.Append(keyed("cpu.usage", "", WindowFull))
	read := keyed("cpu.usage", "", WindowFull)
	read.Provenance.Source = SrcReadTimeseries
	read.FindingClass = "read_timeseries/series"
	read.Refines = scan
	fine, err := ix.Append(read)
	if err != nil {
		t.Fatal(err)
	}
	if len(ix.Active()) != 2 {
		t.Fatal("Refines가 원본을 지웠다 — 반증·지지가 증발한다(4차 A-3)")
	}
	if heir, _ := ix.ActiveHeir(scan); heir != scan {
		t.Fatal("Refines가 후계 사슬을 만들었다 — supersede가 아니다")
	}
	if h := ix.Heads(read.Key()); len(h) != 1 || h[0] != fine {
		t.Fatalf("정밀본이 자기 키의 head가 아니다: %v", h)
	}
	// 같은 관측 키의 재조회는 Refines를 참칭할 수 없다(= supersede여야 한다).
	same := keyed("cpu.usage", "", WindowFull)
	same.Refines = scan
	if _, err := ix.Append(same); err == nil {
		t.Fatal("같은 selector의 재조회가 Refines로 통과했다 — head가 둘이 된다")
	}
	// 없는 EID 정밀화도 반려.
	ghost := keyed("cpu.usage", "", WindowFull)
	ghost.Provenance.Source = SrcReadTimeseries
	ghost.FindingClass = "read_timeseries/series"
	ghost.Refines = "EIX-9999"
	if _, err := ix.Append(ghost); err == nil {
		t.Fatal("실재하지 않는 레코드를 정밀화한다는 주장이 통과했다")
	}
}

// supersede 체인은 여러 단계를 거쳐도 하나의 active head로 수렴한다.
func TestSupersedeChain(t *testing.T) {
	ix := NewIndex()
	e1, _ := ix.Append(keyed("cpu.usage", "", WindowFull))
	r2 := keyed("cpu.usage", "", WindowFull)
	r2.Supersedes = e1
	e2, _ := ix.Append(r2)
	r3 := keyed("cpu.usage", "", WindowFull)
	r3.Supersedes = e2
	e3, err := ix.Append(r3)
	if err != nil {
		t.Fatal(err)
	}
	if len(ix.Active()) != 1 || ix.Active()[0].EID != e3 {
		t.Fatalf("체인 끝의 active가 하나가 아니다: %v", ix.Active())
	}
	for _, from := range []string{e1, e2, e3} {
		if heir, err := ix.ActiveHeir(from); err != nil || heir != e3 {
			t.Fatalf("%s의 후계가 %s(%v)", from, heir, err)
		}
	}
	if len(ix.All()) != 3 {
		t.Fatal("append-only가 깨졌다")
	}
	// 사슬 중간을 다시 대체하려는 시도 = fork.
	f := keyed("cpu.usage", "", WindowFull)
	f.Supersedes = e2
	if _, err := ix.Append(f); err == nil || !strings.Contains(err.Error(), "fork") {
		t.Fatalf("사슬 중간 재대체가 fork로 반려되지 않았다: %v", err)
	}
}

// 순환은 append 계약상 만들 수 없다(대상은 반드시 기존 레코드이고 각
// 레코드는 최대 하나만 대체하므로 사슬은 삽입 순서로만 향한다). 그래도
// 후계 추적은 순환을 만나면 죽지 않고 오류를 낸다 — 색인이 깨진 상태에서
// 무한 루프로 도는 대신 실패로 드러나야 한다.
func TestCycleIsRejectedAndDetected(t *testing.T) {
	ix := NewIndex()
	e1, _ := ix.Append(keyed("cpu.usage", "", WindowFull))
	r2 := keyed("cpu.usage", "", WindowFull)
	r2.Supersedes = e1
	e2, _ := ix.Append(r2)
	// e1이 e2를 대체하는 레코드는 append로 만들 수 없다(e1은 이미 대체됨).
	back := keyed("cpu.usage", "", WindowFull)
	back.Supersedes = e2
	if _, err := ix.Append(back); err != nil {
		t.Fatalf("정상 후속 대체가 막혔다: %v", err)
	}
	// 색인을 직접 깨뜨려 순환을 만든다(백박스) — 탐지가 실동작함을 실증.
	ix.supersededBy[e1] = e2
	ix.supersededBy[e2] = e1
	if _, err := ix.ActiveHeir(e1); err == nil || !strings.Contains(err.Error(), "순환") {
		t.Fatalf("순환 사슬이 탐지되지 않았다: %v", err)
	}
}
