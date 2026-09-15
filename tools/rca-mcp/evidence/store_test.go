package evidence

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 봉투 원문은 가공 없이 남고 ref로 되찾을 수 있다 — §8.1 인용 무결성
// 게이트와 §5.8-2 스팟 체크가 이 왕복 위에 선다.
func TestStoreRoundTrip(t *testing.T) {
	st, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"status":"anomalous","summary":"원문 그대로"}`)
	ref, err := st.Put("scan_metrics", body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(ref, "EST-0001:") {
		t.Errorf("ref %q — 일련번호 서식이 아니다", ref)
	}
	got, err := st.Get(ref)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(body) {
		t.Errorf("원문이 바뀌었다: %s", got)
	}
	if !st.Has(ref) {
		t.Error("발급한 ref가 실재 검사에서 없다고 나온다")
	}
	if st.Has("EST-9999:없음") {
		t.Error("발급하지 않은 ref가 실재한다고 나온다")
	}
	if _, err := st.Get("EST-9999:없음"); err == nil {
		t.Error("없는 ref 조회가 성공했다 — 게이트가 지어낸 인용을 통과시킨다")
	}
}

// §15.3: store는 마스킹하지 않지만 접근은 통제한다(디렉토리 0700·파일 0600).
func TestStorePermissions(t *testing.T) {
	dir := t.TempDir()
	st, err := NewStore(dir)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := st.Put("sample_logs", []byte(`{}`)); err != nil {
		t.Fatal(err)
	}
	di, err := os.Stat(st.Dir())
	if err != nil {
		t.Fatal(err)
	}
	if di.Mode().Perm() != 0o700 {
		t.Errorf("store 디렉토리 권한 %v — 장비의 아무 계정이나 원문을 읽는다", di.Mode().Perm())
	}
	ents, err := os.ReadDir(st.Dir())
	if err != nil || len(ents) != 1 {
		t.Fatalf("파일 %d개: %v", len(ents), err)
	}
	fi, err := os.Stat(filepath.Join(st.Dir(), ents[0].Name()))
	if err != nil {
		t.Fatal(err)
	}
	if fi.Mode().Perm() != 0o600 {
		t.Errorf("봉투 파일 권한 %v", fi.Mode().Perm())
	}
}

func minimalRecord() EvidenceIndexRecord {
	return EvidenceIndexRecord{
		TargetID: "t1", Aspect: AspectMetric, RecordKind: KindFinding,
		FindingClass: "scan_metrics/shifted", Headline: "h",
		Effect:     Effect{Kind: EffectCount, Metric: "m", Direction: DirNA},
		Window:     TimeWindow{Class: WindowFull},
		Quality:    Quality{Status: StatusNormal, Availability: AvailObserved, Confidence: ConfOK},
		Provenance: Provenance{Source: SrcScanMetrics},
	}
}

// EID는 index가 부여하고 그 뒤로 불변이다(§5.7).
func TestIndexAssignsEID(t *testing.T) {
	ix := NewIndex()
	e1, err := ix.Append(minimalRecord())
	if err != nil {
		t.Fatal(err)
	}
	e2, _ := ix.Append(minimalRecord())
	if e1 != "EIX-0001" || e2 != "EIX-0002" {
		t.Fatalf("EID 서식/순서: %s %s", e1, e2)
	}
	r := minimalRecord()
	r.EID = "EIX-0099"
	if _, err := ix.Append(r); err == nil {
		t.Error("레코드가 EID를 들고 왔는데 통과했다 — 부여 주체가 흔들린다")
	}
	// Validate 관문이 실제로 선다.
	bad := minimalRecord()
	bad.FindingClass = ""
	if _, err := ix.Append(bad); err == nil {
		t.Error("class 없는 finding이 index에 들어갔다 — 허용 Predicate 판정 불능(§6 행 2)")
	}
}

// supersede 골격: 대체된 레코드는 active에서 빠지고, 후계 추적이 된다.
// (계약 본체 — 단일 head·Refines·cycle 반려 — 는 1c 몫이다.)
func TestSupersedeActiveView(t *testing.T) {
	ix := NewIndex()
	old, _ := ix.Append(minimalRecord())
	r := minimalRecord()
	r.Supersedes = old
	neu, err := ix.Append(r)
	if err != nil {
		t.Fatal(err)
	}
	if got := len(ix.Active()); got != 1 {
		t.Fatalf("active %d건 — 대체된 레코드가 남아 있다", got)
	}
	if len(ix.All()) != 2 {
		t.Error("append-only가 깨졌다 — 감사 추적이 사라진다")
	}
	heir, err := ix.ActiveHeir(old)
	if err != nil || heir != neu {
		t.Fatalf("후계 추적 실패: %s %v", heir, err)
	}
	// fork 반려 — 한 레코드를 둘이 대체하면 active head가 둘이 된다.
	f := minimalRecord()
	f.Supersedes = old
	if _, err := ix.Append(f); err == nil {
		t.Error("fork가 통과했다 — 같은 키에 active가 둘이면 진리표 행 1로 영구 inconclusive다")
	}
	// 없는 EID 대체도 반려.
	g := minimalRecord()
	g.Supersedes = "EIX-9999"
	if _, err := ix.Append(g); err == nil {
		t.Error("실재하지 않는 EID를 대체한다는 레코드가 통과했다")
	}
}

// metric 계열 행(EntityKey 없음)의 범위 레코드가 개체 행을 덮는다 —
// 개체 축이 Metric인 행에서 Covers가 그 축을 열지 않으면 absent 술어의
// 해소 경로가 통째로 사라진다.
func TestScopeCoversMetricSeriesEntities(t *testing.T) {
	c := fixtureCase{file: "envelope_scan_metrics.json", source: SrcScanMetrics, tool: "scan_metrics"}
	res, err := Project(testRequest(c), loadFixture(t, c.file))
	if err != nil {
		t.Fatal(err)
	}
	ix := NewIndex()
	if _, err := ix.AppendAll(res.Records); err != nil {
		t.Fatal(err)
	}
	// 봉투에 없던 지표를 지목한 absent 술어의 키.
	want := ObservationKey{Source: SrcScanMetrics, TargetID: fixtureTarget, Aspect: AspectMetric,
		Metric: "존재하지.않는.지표", Window: WindowFull}
	if got := ix.ByKey(want); len(got) != 0 {
		t.Fatalf("없는 지표가 개체 행으로 해소됐다: %d건", len(got))
	}
	if _, ok := ix.ScopeFor(want); !ok {
		t.Fatal("metric 계열 범위 행이 개체 키를 덮지 못한다 — absent 술어가 영구 미판정이 된다(C-3)")
	}
	// 절단된 구획(disappeared)의 범위 행은 absent 근거 자격이 없다.
	for _, r := range res.Records {
		if r.RecordKind == KindQueryScope && r.FindingClass == "scan_metrics/disappeared" {
			if r.Scope.Omitted > 0 && r.Scope.Complete() {
				t.Error("절단된 조회가 완전 조회로 판정됐다")
			}
		}
	}
}

// 롤업(§5.2) — 대상별 anomalous·weak·no_data 관점 집계.
func TestRollups(t *testing.T) {
	ix := NewIndex()
	a := minimalRecord()
	a.Quality.Status = StatusAnomalous
	w := minimalRecord()
	w.Quality.Confidence = ConfLow
	n := minimalRecord()
	n.Quality.Status = StatusNoData
	n.Quality.NoDataReason = NoDataCollectorGap
	n.Quality.Availability = AvailMissing
	n.FindingClass = "scan_metrics/appeared"
	for _, r := range []EvidenceIndexRecord{a, w, n} {
		if _, err := ix.Append(r); err != nil {
			t.Fatal(err)
		}
	}
	ru := ix.Rollups()
	if len(ru) != 1 || ru[0].Anomalous != 1 || ru[0].Weak != 1 || len(ru[0].NoDataViews) != 1 {
		t.Fatalf("롤업이 어긋났다: %+v", ru)
	}
}

// ParamsDigest는 키 순서에 무관하게 같은 요청을 같은 값으로 본다.
func TestParamsDigestIsOrderStable(t *testing.T) {
	a := ParamsDigest([]byte(`{"target":"t1","from":"A","to":"B"}`))
	b := ParamsDigest([]byte(`{"to":"B","from":"A","target":"t1"}`))
	if a != b {
		t.Fatalf("같은 요청이 다른 지문: %s %s", a, b)
	}
	if a == ParamsDigest([]byte(`{"target":"t2","from":"A","to":"B"}`)) {
		t.Fatal("다른 대상이 같은 지문")
	}
	// JSON이 아니어도 죽지 않는다(감사 값이지 판정 값이 아니다).
	if ParamsDigest([]byte(`not json`)) == "" {
		t.Fatal("비 JSON 인자에서 지문이 비었다")
	}
}

// 봉투 원문 → 레코드 → 다시 직렬화까지 JSON이 깨지지 않는다.
func TestRecordsSerialize(t *testing.T) {
	for _, c := range fixtures {
		if c.source == "" {
			continue
		}
		for _, r := range projectFixture(t, c) {
			if _, err := json.Marshal(r); err != nil {
				t.Fatalf("%s %s 직렬화: %v", c.file, r.FindingClass, err)
			}
		}
	}
}

// store 쿼터(§15.2-6, 6b 골격) — 초과의 전이는 절단이 아니라 오류
// (QuotaError → store_failure)다.
func TestStoreQuota(t *testing.T) {
	s, err := NewStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	s.SetQuota(2)
	if _, err := s.Put("t", []byte(`{}`)); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Put("t", []byte(`{}`)); err != nil {
		t.Fatal(err)
	}
	_, err = s.Put("t", []byte(`{}`))
	var qe *QuotaError
	if !errors.As(err, &qe) {
		t.Fatalf("쿼터 초과가 QuotaError가 아님: %v", err)
	}
}
