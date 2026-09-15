// compare_peers 순수 단위 — DB·VM 없이 §11의 판정 규칙을 고정한다.
// 최대 오답원은 "또래가 조용해서 alone"이다(§11.5) — 관측 없는 또래가
// 정상 또래로 세어지는 순간 미수집이 고신뢰 오답으로 승격한다.
// TestComparePeersExcludedNotCountedNormal이 그 회귀 가드다.
package tools

import (
	"strings"
	"testing"
	"time"
)

func cpRow(id string, self, deviating bool, cur, base float64) *peerRow {
	return &peerRow{ID: id, Self: self, Observed: true, Deviating: deviating,
		CurMedian: cur, BaseMed: base, Samples: 20, Direction: "상향"}
}

func cpExcluded(id, reason string) *peerRow {
	return &peerRow{ID: id, Excluded: reason, Samples: 0}
}

// cpEnvelope는 조립기를 기본 인자로 부른다.
func cpEnvelope(t *testing.T, set *peerSet, rows []*peerRow) Envelope {
	t.Helper()
	var self *peerRow
	for _, r := range rows {
		if r.Self {
			self = r
		}
	}
	if self == nil {
		t.Fatal("테스트 구성 오류: self 행 없음")
	}
	from := time.Date(2026, 7, 29, 0, 0, 0, 0, time.UTC)
	return comparePeersEnvelope(set, rows, self, "queue.pending", len(rows)-1, false,
		0, false, false, from, from.Add(30*time.Minute), from.Add(-time.Hour), from)
}

func cpFinding(t *testing.T, env Envelope, class string) Finding {
	t.Helper()
	for _, f := range env.Findings {
		if f["class"] == class {
			return f
		}
	}
	t.Fatalf("finding class=%s 없음", class)
	return nil
}

var cpGroup = &peerSet{Basis: "service_group", Confidence: "high",
	GroupKey: "s/service/g1", TargetType: "application"}

func TestComparePeersAlone(t *testing.T) {
	env := cpEnvelope(t, cpGroup, []*peerRow{
		cpRow("self", true, true, 20, 0),
		cpRow("p1", false, false, 0, 0),
		cpRow("p2", false, false, 0, 0),
	})
	v := cpFinding(t, env, "verdict")
	if v["verdict"] != "alone" {
		t.Errorf("verdict = %v, want alone", v["verdict"])
	}
	if env.Status != "anomalous" {
		t.Errorf("status = %s, want anomalous", env.Status)
	}
	if v["peers_compared"] != 2 || v["peers_deviating"] != 0 {
		t.Errorf("compared/deviating = %v/%v, want 2/0", v["peers_compared"], v["peers_deviating"])
	}
	// 비이탈 또래는 개별 행이 아니라 한 줄로 접힌다(§11.6).
	if f := cpFinding(t, env, "peers_folded"); f["count"] != 2 {
		t.Errorf("folded count = %v, want 2", f["count"])
	}
	for _, f := range env.Findings {
		if f["class"] == "peer" {
			t.Error("비이탈 또래가 개별 peer 행으로 펴짐 — 접기 규율 위반")
		}
	}
}

func TestComparePeersShared(t *testing.T) {
	env := cpEnvelope(t, cpGroup, []*peerRow{
		cpRow("self", true, true, 20, 0),
		cpRow("p1", false, true, 18, 0),
		cpRow("p2", false, false, 0, 0),
	})
	v := cpFinding(t, env, "verdict")
	if v["verdict"] != "shared" {
		t.Errorf("verdict = %v, want shared", v["verdict"])
	}
	if v["peers_deviating"] != 1 {
		t.Errorf("deviating = %v, want 1", v["peers_deviating"])
	}
	// 이탈 또래는 반드시 개별 행으로 펴진다 — 조사자가 누구인지 알아야 한다.
	found := false
	for _, f := range env.Findings {
		if f["class"] == "peer" && f["target_id"] == "p1" {
			found = true
		}
	}
	if !found {
		t.Error("이탈 또래가 개별 행으로 펴지지 않음")
	}
	// shared는 다음 수로 read_timeseries를 넘긴다(선후는 이 도구 몫이 아님).
	meta := cpFinding(t, env, "compare_meta")
	if ns, _ := meta["next_step"].(string); ns == "" || !strings.Contains(ns, "read_timeseries") {
		t.Errorf("next_step이 read_timeseries로 넘기지 않음: %q", ns)
	}
}

// 최대 오답원 회귀 가드 — 미관측 또래를 정상 또래로 세면 안 된다(§11.5).
func TestComparePeersExcludedNotCountedNormal(t *testing.T) {
	env := cpEnvelope(t, cpGroup, []*peerRow{
		cpRow("self", true, true, 20, 0),
		cpExcluded("p1", peerExcNoObservation),
		cpExcluded("p2", peerExcNoObservation),
		cpExcluded("p3", peerExcBaselineMissig),
	})
	v := cpFinding(t, env, "verdict")
	if v["verdict"] == "alone" {
		t.Fatal("또래 전원이 미관측인데 alone — 수집 결손이 '혼자 이상'으로 승격됐다(§11.5 위반)")
	}
	if v["verdict"] != "undecidable" {
		t.Errorf("verdict = %v, want undecidable", v["verdict"])
	}
	if env.Status != "no_data" || env.NoDataReason != NoDataUnknown {
		t.Errorf("status/reason = %s/%s, want no_data/unknown", env.Status, env.NoDataReason)
	}
	if v["peers_compared"] != 0 {
		t.Errorf("compared = %v, want 0 — 제외 또래가 모집단에 셌다", v["peers_compared"])
	}
	// 분모가 사라지면 안 된다.
	exc, ok := v["peers_excluded"].(Finding)
	if !ok || exc[peerExcNoObservation] != 2 || exc[peerExcBaselineMissig] != 1 {
		t.Errorf("peers_excluded 계수 누락/오류: %v", v["peers_excluded"])
	}
	if v["comparison_scope"] != "observed_peers_only" {
		t.Errorf("scope = %v, want observed_peers_only", v["comparison_scope"])
	}
	// zero_observations는 이 도구가 쓰지 않는다 — 배제 근거로 오용되면 안 된다.
	if env.NoDataReason == NoDataZeroObservations {
		t.Error("zero_observations 사용 — 미관측을 '봤는데 0'으로 승격했다(§11.5 위반)")
	}
}

// 일부만 관측되면 관측분으로 판정하되 분모를 남긴다.
func TestComparePeersPartialCoverage(t *testing.T) {
	env := cpEnvelope(t, cpGroup, []*peerRow{
		cpRow("self", true, true, 20, 0),
		cpRow("p1", false, false, 0, 0),
		cpExcluded("p2", peerExcEpisodic),
	})
	v := cpFinding(t, env, "verdict")
	if v["verdict"] != "alone" {
		t.Errorf("verdict = %v, want alone", v["verdict"])
	}
	if v["peers_compared"] != 1 || v["peers_eligible"] != 2 {
		t.Errorf("compared/eligible = %v/%v, want 1/2", v["peers_compared"], v["peers_eligible"])
	}
	if v["comparison_scope"] != "observed_peers_only" {
		t.Error("부분 커버리지인데 scope가 full_cohort")
	}
}

func TestComparePeersSelfNotDeviating(t *testing.T) {
	env := cpEnvelope(t, cpGroup, []*peerRow{
		cpRow("self", true, false, 3, 3),
		cpRow("p1", false, true, 90, 1),
	})
	v := cpFinding(t, env, "verdict")
	if v["verdict"] != "self_not_deviating" {
		t.Errorf("verdict = %v, want self_not_deviating", v["verdict"])
	}
	if env.Status != "normal" {
		t.Errorf("status = %s, want normal", env.Status)
	}
	// normal이 건강을 뜻하지 않는다는 경고가 판정 근거에 반드시 있어야 한다.
	if !strings.Contains(env.AssessmentBasis, "normal은 건강을 뜻하지 않는다") {
		t.Error("assessment_basis에 normal 오독 방지 문구 없음")
	}
}

// 약한 또래에서도 판정은 하되, 약함이 구조화 필드로 드러나야 한다(§11.4).
func TestComparePeersFallbackWarns(t *testing.T) {
	set := &peerSet{Basis: "type_fallback", Confidence: "low", TargetType: "application"}
	env := cpEnvelope(t, set, []*peerRow{
		cpRow("self", true, true, 20, 0),
		cpRow("p1", false, false, 0, 0),
	})
	if cpFinding(t, env, "verdict")["verdict"] != "alone" {
		t.Error("약한 근거라고 판정을 끄면 안 된다 — 실전 대부분이 fallback이다")
	}
	ps := cpFinding(t, env, "peer_set")
	if ps["confidence"] != "low" {
		t.Errorf("confidence = %v, want low", ps["confidence"])
	}
	if w, _ := ps["warning"].(string); !strings.Contains(w, "도메인") {
		t.Errorf("fallback 도메인 혼재 경고 없음: %q", w)
	}
	if ps["status_filter"] == nil {
		t.Error("status 미필터(생존자 편향 방지) 표식 없음")
	}
}

// 크기 비교는 실리되 판정에 쓰이지 않음이 명시돼야 한다(§11.2).
func TestComparePeersMagnitudeIsAdvisoryOnly(t *testing.T) {
	env := cpEnvelope(t, cpGroup, []*peerRow{
		cpRow("self", true, false, 5000, 4900), // 또래보다 100배 크지만 자기 기준선 대비는 평온
		cpRow("p1", false, false, 50, 49),
	})
	v := cpFinding(t, env, "verdict")
	if v["verdict"] != "self_not_deviating" {
		t.Errorf("verdict = %v — 크기 차이가 판정으로 승격됐다", v["verdict"])
	}
	m, ok := v["magnitude"].(Finding)
	if !ok {
		t.Fatal("magnitude 절 없음 — raw 크기는 숨기지 않는다")
	}
	if m["self_median"] != 5000.0 || m["peer_median"] != 50.0 {
		t.Errorf("magnitude 값 오류: %v", m)
	}
	if note, _ := m["note"].(string); !strings.Contains(note, "판정에 쓰지 않는다") {
		t.Error("magnitude에 '판정 아님' 표식 없음")
	}
}

