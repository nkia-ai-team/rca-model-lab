// projector 정합 검증(§5.8-1) — **도구별 실측 봉투 → 기대 레코드의 골든 쌍**.
//
// 왼쪽(실측 봉투)은 evidence/testdata/envelope_*.json이다. 라이브 백엔드에서
// 캡처한 진짜 봉투이며 갱신 방법은 tools/fixture_capture_test.go에 적혀 있다.
// 오른쪽(기대 레코드)은 records_*.json이고 `go test ./evidence -update`로
// 재생성한다 — 재생성 후 **diff를 사람이 읽는 것**이 이 시험의 절반이다
// (projector의 추출 버그는 전 파이프라인의 조용한 오염이라, 값이 바뀌었을 때
// 시험이 조용히 통과하면 방어가 없다).
package evidence

import (
	"encoding/json"
	"flag"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

var update = flag.Bool("update", false, "골든 레코드 파일을 재생성한다")

// fixtureCase는 봉투 fixture 하나와 그 호출 맥락이다.
type fixtureCase struct {
	file   string
	source ObservationSource
	tool   string
	// live — 라이브 백엔드에서 캡처한 실물 봉투인가. 전부 true여야 한다;
	// false로 바뀌는 순간 "실물 대조"라는 주장 자체가 약해진다.
	live bool
}

var fixtures = []fixtureCase{
	{file: "envelope_scan_metrics.json", source: SrcScanMetrics, tool: "scan_metrics", live: true},
	{file: "envelope_read_timeseries.json", source: SrcReadTimeseries, tool: "read_timeseries", live: true},
	{file: "envelope_list_events.json", source: SrcListEvents, tool: "list_events", live: true},
	{file: "envelope_sample_logs.json", source: SrcSampleLogs, tool: "sample_logs", live: true},
	{file: "envelope_sample_logs_grep.json", source: SrcSampleLogs, tool: "sample_logs", live: true},
	{file: "envelope_db_slow_queries.json", source: SrcDBSlowQueries, tool: "db_slow_queries", live: true},
	{file: "envelope_db_blocking.json", source: SrcDBBlocking, tool: "db_blocking", live: true},
	{file: "envelope_breakdown_endpoints.json", source: SrcBreakdownEndpoints, tool: "breakdown_endpoints", live: true},
	{file: "envelope_compare_peers.json", source: SrcComparePeers, tool: "compare_peers", live: true},
	{file: "envelope_get_processes.json", source: SrcProcesses, tool: "get_processes", live: true},
	{file: "envelope_get_snmp_traps.json", source: SrcSNMPTraps, tool: "get_snmp_traps", live: true},
	{file: "envelope_get_k8s_state.json", source: SrcK8sState, tool: "get_k8s_state", live: true},
	// list_changes — 합성 원천 change의 실행 채널. 같은 사상표 행에 두 경로가
	// 투영되는 유일한 자리다([2] 합성 봉투 쪽 시험은 pipeline에 있다).
	{file: "envelope_list_changes.json", source: SrcChange, tool: "list_changes", live: true},
	// 술어 불가 3종 — 레코드 0건이 계약이다(봉투는 store에만 남는다).
	{file: "envelope_describe_target.json", source: "", tool: "describe_target", live: true},
	{file: "envelope_expand_topology.json", source: "", tool: "expand_topology", live: true},
	{file: "envelope_get_data_coverage.json", source: "", tool: "get_data_coverage", live: true},
}

const fixtureTarget = "11111111-2222-3333-4444-555555555555"

func testRequest(c fixtureCase) ProjectRequest {
	return ProjectRequest{
		Source: c.source, Tool: c.tool, TargetID: fixtureTarget, Domain: "commerce",
		WindowClass: WindowFull,
		From:        time.Date(2026, 8, 3, 2, 0, 0, 0, time.UTC),
		To:          time.Date(2026, 8, 3, 3, 0, 0, 0, time.UTC),
		ResolutionS: 30,
		ClockSkew:   ClockSkew{Status: TagUnknown},
		// CollectLag는 여기서 주지 않는다 — 봉투에서 파생된다(§5.5 원천 결정).
		// 골든 레코드의 collect_lag 값이 곧 그 파생의 대조다.
		// 실 배선에서는 ParamsDigest(args)가 채운다 — 골든 안정성을 위해 고정.
		ParamsDigest: "PD-fixture", EnvelopeRef: "EST-0001:fixture",
	}
}

func loadFixture(t *testing.T, file string) RawEnvelope {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", file))
	if err != nil {
		t.Fatalf("fixture %s: %v", file, err)
	}
	env, err := DecodeEnvelope(b)
	if err != nil {
		t.Fatalf("fixture %s 디코드: %v", file, err)
	}
	return env
}

// TestProjectorGolden은 §5.8-1 골든 쌍이다.
func TestProjectorGolden(t *testing.T) {
	for _, c := range fixtures {
		t.Run(strings.TrimSuffix(strings.TrimPrefix(c.file, "envelope_"), ".json"), func(t *testing.T) {
			env := loadFixture(t, c.file)
			res, err := Project(testRequest(c), env)
			if err != nil {
				t.Fatalf("투영: %v", err)
			}
			if len(res.UnknownClasses) > 0 {
				t.Fatalf("사상표에 없는 class %v — 실물 class 전수가 행 또는 meta 명시 제외 중 하나에 속해야 한다(§5.1)",
					res.UnknownClasses)
			}
			// EID 부여 + Validate를 실제 관문으로 통과시킨다.
			ix := NewIndex()
			if _, err := ix.AppendAll(res.Records); err != nil {
				t.Fatalf("index 적재: %v", err)
			}
			got := ix.All()

			golden := filepath.Join("testdata", "records_"+
				strings.TrimPrefix(c.file, "envelope_"))
			b, err := json.MarshalIndent(got, "", " ")
			if err != nil {
				t.Fatal(err)
			}
			if *update {
				if err := os.WriteFile(golden, append(b, '\n'), 0o600); err != nil {
					t.Fatal(err)
				}
				t.Logf("골든 갱신: %s (레코드 %d건)", golden, len(got))
				return
			}
			want, err := os.ReadFile(golden)
			if err != nil {
				t.Fatalf("골든 %s 없음 — `go test ./evidence -update`로 생성하고 diff를 읽어라: %v", golden, err)
			}
			if strings.TrimSpace(string(want)) != strings.TrimSpace(string(b)) {
				t.Errorf("레코드가 골든과 다르다(%s). 의도한 변경이면 -update로 갱신하고 diff를 검토하라.", golden)
			}
		})
	}
}

// TestPredicateFreeToolsProduceNoRecords — 술어 불가 도구는 index에 들어가지
// 않는다(§5.1). 봉투 적재·ref 발급은 store의 몫이고 여기서는 레코드 0건이 계약.
func TestPredicateFreeToolsProduceNoRecords(t *testing.T) {
	for _, c := range fixtures {
		if c.source != "" {
			continue
		}
		env := loadFixture(t, c.file)
		res, err := Project(testRequest(c), env)
		if err != nil {
			t.Fatalf("%s: %v", c.tool, err)
		}
		if len(res.Records) != 0 {
			t.Errorf("%s: 술어 불가 도구인데 레코드 %d건", c.tool, len(res.Records))
		}
	}
}

// findRecords는 골든 시험이 아니라 계약 시험에서 쓸 조회 도우미다.
func projectFixture(t *testing.T, c fixtureCase) []EvidenceIndexRecord {
	t.Helper()
	res, err := Project(testRequest(c), loadFixture(t, c.file))
	if err != nil {
		t.Fatalf("%s 투영: %v", c.file, err)
	}
	return res.Records
}

func fixtureBy(t *testing.T, file string) fixtureCase {
	t.Helper()
	for _, c := range fixtures {
		if c.file == file {
			return c
		}
	}
	t.Fatalf("fixture %s 미등록", file)
	return fixtureCase{}
}

func ofClass(recs []EvidenceIndexRecord, class string) []EvidenceIndexRecord {
	var out []EvidenceIndexRecord
	for _, r := range recs {
		if r.FindingClass == class && r.RecordKind == KindFinding {
			out = append(out, r)
		}
	}
	return out
}

// ── 사상표가 실물과 갈린 지점의 계약 시험 ─────────────────────────
//
// 아래는 전부 4차 검토 B급 지적을 실물 봉투로 판정한 결과다. 골든 파일이
// 갱신돼도 이 규칙들은 따로 선다 — 골든은 "값이 바뀌었나"를 보고 이쪽은
// "계약이 살아 있나"를 본다.

// B-13: scan_metrics appeared는 Kind=count가 아니라 Magnitude 부재다.
func TestScanAppearedHasNoMagnitude(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_scan_metrics.json"))
	shifted := ofClass(recs, "scan_metrics/shifted")
	if len(shifted) == 0 {
		t.Fatal("실물 봉투에 shifted 행이 없다 — fixture를 다시 캡처하라")
	}
	for _, r := range shifted {
		if r.Effect.Kind != EffectRatio || r.Effect.Observed == nil {
			t.Errorf("shifted %s: ratio + current_median이어야 한다(%+v)", r.EID, r.Effect)
		}
		if r.EntityKey != "" {
			t.Errorf("shifted %s: metric 계열은 EntityKey가 빈 값이고 Metric이 식별을 겸한다", r.EID)
		}
	}
	for _, r := range ofClass(recs, "scan_metrics/appeared") {
		if r.Effect.Kind != EffectCategorical || r.Effect.Magnitude != nil {
			t.Errorf("appeared %s: Magnitude 부재여야 한다 — representative는 gauge 중앙값과 counter 증가 합이 섞인 값이다(B-13)", r.EID)
		}
	}
}

// B-12(과잉 부여): db_slow_queries의 first_seen/last_seen은 폴 시각이라
// 변화구간이 아니다 — ChangeFrom/To는 nil이 계약이다.
func TestDBSlowQueriesHasNoOnset(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_db_slow_queries.json"))
	sql := ofClass(recs, "db_slow_queries/sql")
	if len(sql) == 0 {
		t.Fatal("실물 봉투에 sql 행이 없다")
	}
	for _, r := range sql {
		if r.Window.ChangeFrom != nil || r.Window.ChangeTo != nil {
			t.Errorf("%s: db_slow_queries에 변화구간이 붙었다 — 만성 쿼리가 onset으로 오인돼 시간 게이트를 그냥 통과한다(B-12)", r.EID)
		}
		if r.EntityKey == "" {
			t.Errorf("%s: EntityKey(sql_key)가 비었다", r.EID)
		}
	}
}

// B-12(과잉 금지): trap·kcm 이벤트는 first_at/last_at을 실제로 싣는다.
func TestEventSourcesCarryOnset(t *testing.T) {
	// list_events k8s 행이 실 fixture에 없을 수 있으므로(클러스터 조용) 합성
	// 봉투로 계약만 확인한다 — 값의 원천은 실물 키 이름이다.
	env := RawEnvelope{Status: "anomalous", Findings: []RawFinding{{
		"section": "k8s", "namespace": "ns1", "kind": "Pod", "name": "p1",
		"reason": "OOMKilled", "event_type": "Warning", "count": 3.0,
		"first_at": "2026-08-03 02:10:00", "last_at": "2026-08-03 02:40:00",
	}}}
	res, err := Project(ProjectRequest{Source: SrcListEvents, Tool: "list_events",
		TargetID: fixtureTarget, WindowClass: WindowFull}, env)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Records) != 1 {
		t.Fatalf("레코드 %d건", len(res.Records))
	}
	r := res.Records[0]
	if r.Window.ChangeFrom == nil || r.Window.ChangeTo == nil {
		t.Fatalf("kcm 이벤트에 변화구간이 없다 — 네트워크·K8s 장애에서 confirmed가 영구 불능이 된다(B-12)")
	}
	if r.EntityKey != "ns1|Pod|p1|OOMKilled" {
		t.Errorf("EntityKey %q — namespace/kind/name/reason 4열이어야 한다", r.EntityKey)
	}
}

// B-11: baseline 0에서 ratio Magnitude를 만들지 않는다(비유한 값 차단 +
// 품질 강등). index 적재 관문까지 통과하는지 함께 본다.
func TestRatioBaselineZero(t *testing.T) {
	env := RawEnvelope{Status: "anomalous", Findings: []RawFinding{{
		"class": "shifted", "metric": "m1", "current_median": 1.0, "baseline_median": 0.0,
	}}}
	res, err := Project(ProjectRequest{Source: SrcScanMetrics, Tool: "scan_metrics",
		TargetID: fixtureTarget, WindowClass: WindowFull}, env)
	if err != nil {
		t.Fatal(err)
	}
	r := res.Records[0]
	if r.Effect.Magnitude != nil {
		t.Fatalf("baseline 0인데 Magnitude가 생겼다: %v", *r.Effect.Magnitude)
	}
	if r.Quality.Confidence != ConfLow {
		t.Error("분모 0으로 판정 크기를 못 만든 ratio는 low여야 한다")
	}
	ix := NewIndex()
	if _, err := ix.AppendAll(res.Records); err != nil {
		t.Fatalf("index 적재: %v", err)
	}
	b, err := json.Marshal(r)
	if err != nil || strings.Contains(string(b), "Inf") {
		t.Fatalf("직렬화가 비유한 값을 실었다: %v %s", err, b)
	}
	_ = math.Inf
}

// B-13: breakdown_endpoints의 EntityKey는 4튜플이어야 selector가 유일 해소된다.
func TestBreakdownEntityKeyIsFourTuple(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_breakdown_endpoints.json"))
	lat := ofClass(recs, "breakdown_endpoints/endpoint_latency")
	if len(lat) < 2 {
		t.Fatalf("실물 봉투에 진입점 행이 %d개뿐", len(lat))
	}
	seen := map[string]bool{}
	for _, r := range lat {
		if strings.Count(r.EntityKey, "|") != 3 {
			t.Errorf("EntityKey %q — section|kind|axis|label 4열이어야 한다(B-13)", r.EntityKey)
		}
		if seen[r.EntityKey] {
			t.Errorf("EntityKey %q 중복 — selector가 복수 해소되면 §8 진리표 행 1로 영구 inconclusive다", r.EntityKey)
		}
		seen[r.EntityKey] = true
	}
	// 판정 축이 둘이면 축별 레코드로 갈린다(Kind 복수 선언 금지).
	for _, r := range lat {
		if r.Effect.Kind != EffectDuration || r.Effect.Metric != "p50_ms" {
			t.Errorf("%s: 지연 축은 duration/p50_ms여야 한다(%+v)", r.EID, r.Effect)
		}
	}
	for _, r := range ofClass(recs, "breakdown_endpoints/endpoint_failure") {
		if r.Effect.Kind != EffectShare || r.Effect.Metric != "failed_rate" {
			t.Errorf("%s: 실패 축은 share/failed_rate여야 한다(%+v)", r.EID, r.Effect)
		}
	}
}

// B-13: get_processes의 개체는 cmd+pid가 아니라 process(name)+pid다.
func TestProcessEntityKeyHasNoCmd(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_get_processes.json"))
	ps := ofClass(recs, "get_processes/process")
	if len(ps) == 0 {
		t.Fatal("실물 봉투에 프로세스 행이 없다")
	}
	for _, r := range ps {
		if strings.Count(r.EntityKey, "|") != 1 || r.EntityKey == "|" {
			t.Errorf("EntityKey %q — process|pid여야 한다(봉투에 cmd가 없다, B-13)", r.EntityKey)
		}
		if r.Effect.Kind != EffectShare {
			t.Errorf("%s: 실물 avg는 건수가 아니라 게이지 평균이라 count 축을 만들지 않는다", r.EID)
		}
	}
}

// grep의 EntityKey `at+bh` — bh는 finding 키가 아니라 ref에서 회수된다.
func TestGrepEntityKeyRecoversHashFromRef(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_sample_logs_grep.json"))
	gs := ofClass(recs, "sample_logs/grep")
	if len(gs) == 0 {
		t.Fatal("실물 봉투에 grep 매치가 없다")
	}
	for _, r := range gs {
		at, bh, ok := strings.Cut(r.EntityKey, "|")
		if !ok || at == "" || bh == "" {
			t.Fatalf("EntityKey %q — at|bh여야 한다", r.EntityKey)
		}
		if r.RawExcerpt == "" {
			t.Errorf("%s: 매치 원문이 RawExcerpt에 없다", r.EID)
		}
		if strings.Contains(r.Headline, r.RawExcerpt) && r.RawExcerpt != "" {
			t.Errorf("%s: Headline에 원문이 들어갔다(§15.1-2 위반)", r.EID)
		}
	}
}

// §15.1-2: Headline은 기계 서술만, 원문은 RawExcerpt에 격리 + 상한.
func TestHeadlineAndExcerptDiscipline(t *testing.T) {
	for _, c := range fixtures {
		if c.source == "" {
			continue
		}
		for _, r := range projectFixture(t, c) {
			if n := len([]rune(r.Headline)); n > headlineMax {
				t.Errorf("%s %s: headline %d자", c.file, r.FindingClass, n)
			}
			if n := len([]rune(r.RawExcerpt)); n > rawExcerptMax {
				t.Errorf("%s %s: raw_excerpt %d자", c.file, r.FindingClass, n)
			}
			for _, bad := range []string{"\n", "\r", "system:", "```"} {
				if strings.Contains(r.RawExcerpt, bad) {
					t.Errorf("%s %s: 발췌에 탈출 문자열 %q가 남았다", c.file, r.FindingClass, bad)
				}
			}
		}
	}
}

// ── query_scope ────────────────────────────────────────────────────

// 0건 완전 조회 = observed_zero(최강 배제 근거). k8s 4신호 중 3개가 0건인
// 실물 봉투가 이 계약의 시험대다 — 봉투는 anomalous인데 신호별로는 0건이다.
func TestQueryScopeObservedZero(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_get_k8s_state.json"))
	zero, nonzero := 0, 0
	for _, r := range recs {
		if r.RecordKind != KindQueryScope {
			continue
		}
		if r.Scope.Total == 0 {
			zero++
			if r.Quality.Availability != AvailObservedZero {
				t.Errorf("%s: 0건 완전 조회는 observed_zero여야 한다(%s)", r.FindingClass, r.Quality.Availability)
			}
			if r.Scope.Selector.EntityKey != EntityAny {
				t.Errorf("%s: 범위 행의 selector는 개체 축이 열려 있어야 한다", r.FindingClass)
			}
		} else {
			nonzero++
			if r.Quality.Availability != AvailObserved {
				t.Errorf("%s: 관측 있는 조회가 observed가 아니다(%s)", r.FindingClass, r.Quality.Availability)
			}
		}
	}
	if zero == 0 || nonzero == 0 {
		t.Fatalf("실물 봉투가 0건·비0건을 함께 갖지 않는다(zero=%d nonzero=%d) — 이 계약을 시험하지 못한다", zero, nonzero)
	}
}

// 절단된 조회는 absent 근거 자격이 없다 — Complete()=false.
func TestTruncatedScopeIsNotComplete(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_sample_logs_grep.json"))
	var found bool
	for _, r := range recs {
		if r.RecordKind != KindQueryScope {
			continue
		}
		found = true
		if r.Scope.Omitted <= 0 {
			t.Fatalf("grep fixture가 절단되지 않았다 — 이 시험의 전제가 깨졌다")
		}
		if r.Scope.Complete() {
			t.Error("절단된 조회가 완전 조회로 판정됐다")
		}
		if !r.Truncation.Truncated || r.Truncation.OmittedN != r.Scope.Omitted {
			t.Errorf("절단 사실이 레코드에 실리지 않았다: %+v", r.Truncation)
		}
	}
	if !found {
		t.Fatal("query_scope 레코드가 없다")
	}
}

// finding 레코드도 자기 구획의 절단 수를 진다 — normal + 절단이면 low 강등(§5.4-1).
func TestNormalWithOmittedIsLowConfidence(t *testing.T) {
	recs := projectFixture(t, fixtureBy(t, "envelope_sample_logs.json"))
	var checked int
	for _, r := range recs {
		if r.RecordKind != KindFinding || r.Quality.Status != StatusNormal {
			continue
		}
		if r.Truncation.OmittedN > 0 {
			checked++
			if r.Quality.Confidence != ConfLow {
				t.Errorf("%s: normal인데 못 본 후보 %d건 — low여야 한다(§5.4-1)", r.EID, r.Truncation.OmittedN)
			}
		}
	}
	if checked == 0 {
		t.Skip("이 fixture에는 절단된 normal 구획이 없다")
	}
}

// 질의 계층 절단(env.QueryTruncated — CH 캡 등 Total 미상 절단)이 레코드
// 완전성에 합류한다(§14-7 D-1). 안 실으면 절단된 조회의 0건 scope가
// observed_zero 자격을 얻어 §5.1 계약 1("절단된 조회의 0건은 배제 근거가
// 아니다")이 뚫린다 — 6b의 cap_exceeded 실패 시절엔 봉투 자체가 없어 못
// 생기던 유출. 표시 쿼터 절단(env.Truncated)은 합류하지 않는다 — 아래
// TestDisplayTruncationKeepsObservedZero가 그 경계를 지킨다.
func TestEnvelopeTruncatedJoinsCompleteness(t *testing.T) {
	c := fixtureBy(t, "envelope_get_k8s_state.json")
	env := loadFixture(t, c.file)
	env.QueryTruncated = true
	res, err := Project(testRequest(c), env)
	if err != nil {
		t.Fatal(err)
	}
	var zeroScopes, findings int
	for _, r := range res.Records {
		switch r.RecordKind {
		case KindQueryScope:
			if !r.Truncation.Truncated {
				t.Errorf("%s: 절단 봉투의 scope 레코드에 절단 표식이 없다", r.FindingClass)
			}
			if r.Scope.Total == 0 {
				zeroScopes++
				if r.Quality.Availability == AvailObservedZero {
					t.Errorf("%s: 절단된 조회의 0건이 observed_zero — 잘린 꼬리에 있었을 수 있다(§5.1 계약 1)", r.FindingClass)
				}
			}
			if r.Quality.Confidence != ConfLow {
				t.Errorf("%s: 절단 봉투의 scope 신뢰도가 %s — low여야 한다", r.FindingClass, r.Quality.Confidence)
			}
		case KindFinding:
			findings++
			if !r.Truncation.Truncated {
				t.Errorf("%s: 절단 봉투의 finding 레코드에 절단 표식이 없다", r.EID)
			}
			if r.Quality.Status == StatusNormal && r.Quality.Confidence != ConfLow {
				t.Errorf("%s: 절단 봉투의 normal이 low가 아니다(§5.4-1)", r.EID)
			}
		}
	}
	if zeroScopes == 0 || findings == 0 {
		t.Fatalf("시험 전제 부족(zeroScopes=%d findings=%d)", zeroScopes, findings)
	}
}

// 의미 강등(degraded_sources — 2b D-3): 해석 규칙 결손 봉투의 파생
// finding은 상태 불문 ConfLow다. 문구만으로는 결정론 층이 장님이라
// counter 오판별 normal이 반증 자격을 얻는 유출이 있었다.
func TestDegradedSourcesForceLowConfidence(t *testing.T) {
	c := fixtureBy(t, "envelope_scan_metrics.json")
	env := loadFixture(t, c.file)
	env.DegradedSources = map[string]string{"pg": "connect_failed"}
	res, err := Project(testRequest(c), env)
	if err != nil {
		t.Fatal(err)
	}
	var findings int
	for _, r := range res.Records {
		if r.RecordKind != KindFinding {
			continue
		}
		findings++
		if r.Quality.Confidence != ConfLow {
			t.Errorf("%s(%s): 의미 강등 봉투의 파생 관측이 %s — low여야 한다(§15.2-3)", r.EID, r.Quality.Status, r.Quality.Confidence)
		}
	}
	if findings == 0 {
		t.Fatal("finding 레코드 0 — 시험 전제 부족")
	}
}

// 표시 쿼터 절단(env.Truncated)은 완전성에 합류하지 **않는다** — 조회는
// 완전하고 표시만 잘린 것이라, 0건 구획의 observed_zero(최강 배제 근거)는
// 살아야 한다. 이 경계가 무너지면 절단 봉투마다 배제 근거가 증발한다.
func TestDisplayTruncationKeepsObservedZero(t *testing.T) {
	c := fixtureBy(t, "envelope_get_k8s_state.json")
	env := loadFixture(t, c.file)
	env.Truncated = true // 표시 절단만 — 질의는 완전
	res, err := Project(testRequest(c), env)
	if err != nil {
		t.Fatal(err)
	}
	var zeroObserved int
	for _, r := range res.Records {
		if r.RecordKind != KindQueryScope || r.Scope.Total != 0 {
			continue
		}
		if r.Quality.Availability == AvailObservedZero {
			zeroObserved++
		}
	}
	if zeroObserved == 0 {
		t.Fatal("표시 절단이 0건 완전 조회의 observed_zero를 강등했다 — 질의 절단과의 경계 붕괴")
	}
}

// 범위 행이 개체 행을 덮는다 — 존재하지 않는 개체를 지목한 absent 술어의
// 유일한 해소 경로(C-3).
func TestScopeCoversMissingEntity(t *testing.T) {
	c := fixtureBy(t, "envelope_get_k8s_state.json")
	res, err := Project(testRequest(c), loadFixture(t, c.file))
	if err != nil {
		t.Fatal(err)
	}
	ix := NewIndex()
	if _, err := ix.AppendAll(res.Records); err != nil {
		t.Fatal(err)
	}
	want := ObservationKey{Source: SrcK8sState, TargetID: fixtureTarget, Aspect: AspectEvent,
		Metric: "oom_killed", EntityKey: "ns-없음|pod-없음|oom_killed", Window: WindowFull}
	if got := ix.ByKey(want); len(got) != 0 {
		t.Fatalf("없는 개체가 개체 행으로 해소됐다: %d건", len(got))
	}
	scope, ok := ix.ScopeFor(want)
	if !ok {
		t.Fatal("덮는 범위 행이 없다 — 그 술어는 영구 미판정이고 등록 규칙 3의 반증 재제안 차단이 뚫린다(C-3)")
	}
	if scope.Quality.Availability != AvailObservedZero {
		t.Errorf("범위 행 가용성 %s — 0건 완전 조회는 observed_zero다", scope.Quality.Availability)
	}
}

// ── 사상표 전수성·정합 ────────────────────────────────────────────

// 실물 fixture의 전 class가 사상표 행 또는 meta 명시 제외 중 하나에 속한다.
func TestAllLiveClassesAreMapped(t *testing.T) {
	for _, c := range fixtures {
		if c.source == "" {
			continue
		}
		env := loadFixture(t, c.file)
		for _, f := range env.Findings {
			class := classKeyOf(c.source, f)
			if len(classRows(c.source, class)) == 0 && !IsMetaClass(c.source, class) {
				t.Errorf("%s: class %q가 사상표에도 meta 목록에도 없다", c.file, class)
			}
		}
		for _, s := range env.Scopes {
			if len(scopeRows(c.source, s.Class)) == 0 {
				t.Errorf("%s: 절단 단위 %q를 사상표 행으로 풀 수 없다", c.file, s.Class)
			}
		}
	}
}

// 행의 어휘 자체가 유효한가 — Aspect·Kind·Predicate·categorical 정합.
func TestAspectMapRowsAreWellFormed(t *testing.T) {
	seen := map[string]bool{}
	for _, r := range Rows() {
		if !ValidSource(r.Source) {
			t.Errorf("%s: 원천 어휘 밖", r.FindingClass())
		}
		if !ValidAspect(r.Aspect) {
			t.Errorf("%s: aspect %q 미정의", r.FindingClass(), r.Aspect)
		}
		if !ValidEffectKind(r.Kind) {
			t.Errorf("%s: kind %q 미정의", r.FindingClass(), r.Kind)
		}
		if len(r.Allowed) == 0 {
			t.Errorf("%s: 허용 Predicate가 비었다 — 등록·판정 게이트의 입력이 없다", r.FindingClass())
		}
		for _, p := range r.Allowed {
			if !ValidPredicate(p) {
				t.Errorf("%s: predicate %q 미정의", r.FindingClass(), p)
			}
		}
		// categorical은 Magnitude가 없으므로 크기 술어를 허용할 수 없다.
		if r.Kind == EffectCategorical {
			for _, p := range r.Allowed {
				if p == PredMagnitudeGE || p == PredMagnitudeLT {
					t.Errorf("%s: categorical인데 %s를 허용한다 — 원천 불가", r.FindingClass(), p)
				}
			}
		}
		if r.Onset && r.OnsetFrom == "" {
			t.Errorf("%s: Onset=true인데 시각을 읽을 키가 없다", r.FindingClass())
		}
		if seen[r.FindingClass()] {
			t.Errorf("%s: 행 중복", r.FindingClass())
		}
		seen[r.FindingClass()] = true
	}
	// 사상표 원천 전수가 실행 채널로 해소돼야 한다(B-15).
	for _, r := range Rows() {
		if _, ok := ExecutableTool(r.Source); !ok {
			t.Errorf("%s: 실행 채널이 없다", r.Source)
		}
	}
}

// 등록 시점 검사가 쓰는 도구 단위 합집합 — compare_peers + magnitude_ge처럼
// 도구 전체에서 원천 불가한 조합이 여기서 죽어야 한다(규칙 1ⓓ).
func TestAllowedUnionBlocksImpossibleCombo(t *testing.T) {
	union := AllowedUnion(SrcComparePeers)
	for _, p := range union {
		if p == PredMagnitudeGE || p == PredMagnitudeLT {
			t.Fatal("compare_peers 합집합에 크기 술어가 있다 — 봉투에 z도 MAD 분모도 없다")
		}
	}
	if len(AllowedUnion(SrcScanMetrics)) != len(allPredicates()) {
		t.Errorf("scan_metrics 합집합 %v — shifted 행이 전부를 허용하므로 6종이어야 한다", AllowedUnion(SrcScanMetrics))
	}
}

// ── 실패 격리 ──────────────────────────────────────────────────────

// 추출 패닉은 error가 된다 — 없으면 §15.5 사상표 11행(crash)으로 떨어져
// 9행(projector_failure) 분리 취지가 무너진다.
func TestProjectRecoversFromPanic(t *testing.T) {
	// findings에 스칼라가 오면 디코드 단계에서 걸리므로, 패닉 경로는
	// extractor에 nil map을 태워 만든다.
	env := RawEnvelope{Status: "anomalous", Findings: []RawFinding{nil}}
	res, err := Project(ProjectRequest{Source: SrcScanMetrics, Tool: "scan_metrics",
		TargetID: fixtureTarget, WindowClass: WindowFull}, env)
	if err != nil {
		t.Logf("패닉이 error로 격리됐다: %v", err)
		return
	}
	// 패닉하지 않는 것도 정답이다(nil map 읽기는 Go에서 합법) — 다만 그때는
	// 레코드가 조용히 이상해지지 않았는지 본다.
	for _, r := range res.Records {
		if err := r.Validate(); err == nil && r.RecordKind == KindFinding && r.FindingClass == "" {
			t.Error("class 없는 finding 레코드가 통과했다")
		}
	}
}

// 사상표에 없는 class는 조용히 버려지지 않는다.
func TestUnknownClassIsSurfaced(t *testing.T) {
	env := RawEnvelope{Status: "normal", Findings: []RawFinding{{"class": "brand_new_class"}}}
	res, err := Project(ProjectRequest{Source: SrcScanMetrics, Tool: "scan_metrics",
		TargetID: fixtureTarget, WindowClass: WindowFull}, env)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.UnknownClasses) != 1 || res.UnknownClasses[0] != "brand_new_class" {
		t.Fatalf("미등록 class가 드러나지 않았다: %v", res.UnknownClasses)
	}
	if len(res.Records) != 1 || res.Records[0].RecordKind != KindMeta {
		t.Fatal("미등록 class의 관측이 사라졌다 — meta로라도 남아야 한다")
	}
}

// 공허한 backend_error 봉투(guard 사상 — Findings·Scopes 둘 다 빈)는
// KindMeta 1건으로 남는다(§14-7 ③ 안 A). 이것이 없으면 §15.4-2
// BackendGaps·재생성 입력 ⑤가 결손의 존재를 못 본다. 관측 자격은 그대로
// 0이다 — no_data(backend_error)라 진리표·반증·observed_zero 어디에도
// 들지 않는다(§15.2-3 fail-closed).
func TestEmptyBackendErrorEnvelopeLeavesMetaRecord(t *testing.T) {
	env := RawEnvelope{Status: "no_data", NoDataReason: string(NoDataBackendError),
		Backend: "ch", BackendDetail: "http_5xx",
		Summary: "관측 백엔드 ch 실패(http_5xx)"}
	res, err := Project(ProjectRequest{Source: SrcScanMetrics, Tool: "scan_metrics",
		TargetID: fixtureTarget, WindowClass: WindowFull,
		EnvelopeRef: "EST-0009:scan_metrics"}, env)
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Records) != 1 {
		t.Fatalf("레코드 %d건, 기대 1건(meta 합성)", len(res.Records))
	}
	r := res.Records[0]
	if r.RecordKind != KindMeta {
		t.Fatalf("RecordKind = %s, 기대 meta", r.RecordKind)
	}
	if r.Quality.Status != StatusNoData || r.Quality.NoDataReason != NoDataBackendError {
		t.Fatalf("Quality = %+v — no_data(backend_error) 승계 실패", r.Quality)
	}
	if r.Quality.Availability != AvailMissing {
		t.Fatalf("Availability = %s, 기대 missing", r.Quality.Availability)
	}
	if !strings.Contains(r.Headline, "backend_error(ch/http_5xx)") {
		t.Fatalf("Headline에 분류 토큰 없음: %q", r.Headline)
	}
	if r.Effect != (Effect{}) || r.EntityKey != "" {
		t.Fatalf("meta 계약 위반 — Effect·EntityKey는 비어야 한다: %+v", r)
	}

	// 다른 사유의 공허 no_data는 종전대로 0건 — 중복 생성 회피(도구 자신이
	// no_data를 낼 때는 Scopes를 채우는 관행이라 이 가지의 몫이 아니다).
	env2 := RawEnvelope{Status: "no_data", NoDataReason: string(NoDataCollectorGap)}
	res2, err := Project(ProjectRequest{Source: SrcScanMetrics, Tool: "scan_metrics",
		TargetID: fixtureTarget, WindowClass: WindowFull}, env2)
	if err != nil {
		t.Fatal(err)
	}
	if len(res2.Records) != 0 {
		t.Fatalf("collector_gap 공허 봉투에서 %d건 — 합성은 backend_error 한정", len(res2.Records))
	}
}

// 레코드 수와 관측 키가 안정적인지 — 골든과 별개로 요약을 눈에 보이게 남긴다.
func TestFixtureSummary(t *testing.T) {
	var lines []string
	for _, c := range fixtures {
		if !c.live {
			t.Errorf("%s: 라이브 캡처 봉투가 아니다 — \"실물 대조\"라는 주장이 성립하지 않는다", c.file)
		}
		if c.source == "" {
			lines = append(lines, c.tool+": 술어 불가 — 레코드 0")
			continue
		}
		recs := projectFixture(t, c)
		kinds := map[RecordKind]int{}
		for _, r := range recs {
			kinds[r.RecordKind]++
		}
		lines = append(lines, c.file+": finding "+itoa(kinds[KindFinding])+
			" / query_scope "+itoa(kinds[KindQueryScope])+" / meta "+itoa(kinds[KindMeta]))
	}
	sort.Strings(lines)
	t.Log("실물 봉투 재생 요약:\n" + strings.Join(lines, "\n"))
}

func itoa(n int) string { return strings.TrimSpace(strings.Replace(jsonNum(n), "\"", "", -1)) }

func jsonNum(n int) string { b, _ := json.Marshal(n); return string(b) }
