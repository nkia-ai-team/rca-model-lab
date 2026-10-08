// list_changes 롤아웃 후 자원 사용량 단위 가드 — 한도 변경 롤아웃에만, 새 RS pod 선택자로,
// 수치(절대값·%·피크 시각)만 붙는지. 실측 형상(f05-r): memory limit 1Gi → 640Mi.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestK8sParseQuantity(t *testing.T) {
	cases := []struct {
		q, res string
		want   float64
		ok     bool
	}{
		{"640Mi", "memory", 640 * (1 << 20), true},
		{"1Gi", "memory", 1 << 30, true},
		{"512M", "memory", 512e6, true},
		{"1e9", "memory", 1e9, true},
		{"134217728", "memory", 134217728, true},
		{"500m", "cpu", 500, true},
		{"2", "cpu", 2000, true},
		{"0.25", "cpu", 250, true},
		{"", "memory", 0, false},
		{"abcMi", "memory", 0, false},
	}
	for _, c := range cases {
		got, ok := k8sParseQuantity(c.q, c.res)
		if ok != c.ok || (ok && got != c.want) {
			t.Errorf("%s/%s: got %v,%v want %v,%v", c.q, c.res, got, ok, c.want, c.ok)
		}
	}
}

func TestK8sUsageEnd(t *testing.T) {
	at := time.Date(2026, 8, 25, 0, 23, 49, 0, time.UTC)
	r := k8sRollout{At: at, Cluster: "c", Namespace: "ns", Deployment: "pay", New: "pay-58c4"}
	all := []k8sRollout{r,
		{At: at.Add(150 * time.Second), Cluster: "c", Namespace: "ns", Deployment: "pay", New: "pay-65c8"}, // 다른 RS — 끝이 아니다
		{At: at.Add(40 * time.Minute), Cluster: "c", Namespace: "ns", Deployment: "pay", New: "pay-58c4"},  // 같은 RS 재활성 — 여기서 끊는다
		{At: at.Add(20 * time.Minute), Cluster: "c", Namespace: "ns2", Deployment: "pay", New: "pay-58c4"}, // 다른 namespace
	}
	end, basis := k8sUsageEnd(r, all, at.Add(5*time.Hour))
	if !end.Equal(at.Add(40*time.Minute)) || basis != "same_rs_reactivated" {
		t.Fatalf("end=%v basis=%s", end, basis)
	}
	end, basis = k8sUsageEnd(r, all[:2], at.Add(5*time.Hour))
	if !end.Equal(at.Add(k8sChgUsageSpan)) || basis != "span_cap" {
		t.Fatalf("span cap: end=%v basis=%s", end, basis)
	}
	end, basis = k8sUsageEnd(r, all[:2], at.Add(30*time.Minute))
	if !end.Equal(at.Add(30*time.Minute)) || basis != "window_end" {
		t.Fatalf("window end: end=%v basis=%s", end, basis)
	}
}

// usageVM은 사용량(range)·피크 시각·한도 조회를 질의 문자열로 가려 답하는 VM fake다. 받은 질의를 기록한다.
// 형상은 f05-r 실측을 줄인 것: 새 RS pod 하나, 컨테이너 인스턴스 2개(재시작으로 container_id 교체).
type usageVM struct {
	mu      sync.Mutex
	queries []string
	times   []string // instant 평가 시각("" = range)
}

func (u *usageVM) server(t *testing.T, t0 int64) *VM {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		u.mu.Lock()
		u.queries = append(u.queries, q)
		u.times = append(u.times, r.URL.Query().Get("time"))
		u.mu.Unlock()
		lbl := func(cid string) string {
			return fmt.Sprintf(`{"namespace":"ns","pod":"pay-58c4-aaaaa","container":"svc","container_id":%q,"target_id":"c"}`, cid)
		}
		inst := func(cid, val string) string { return fmt.Sprintf(`{"metric":%s,"value":[1,%q]}`, lbl(cid), val) }
		var res []string
		switch {
		case strings.HasPrefix(r.URL.Path, "/api/v1/query_range") && strings.Contains(q, "kcm.container.mem_usage"):
			if t0 > 0 {
				res = []string{
					fmt.Sprintf(`{"metric":%s,"values":[[%d,"0"],[%d,"598000000"]]}`, lbl("c1aaaaaaaaaaaaaaaa"), t0+60, t0+120),
					fmt.Sprintf(`{"metric":%s,"values":[[%d,"1167949824"],[%d,"638800000"]]}`, lbl("c2bbbbbbbbbbbbbbbb"), t0+180, t0+240),
				}
			}
		case strings.HasPrefix(q, "tmax_over_time({__name__=\"kcm.container.mem_usage\""):
			res = []string{inst("c1aaaaaaaaaaaaaaaa", fmt.Sprint(t0+120)), inst("c2bbbbbbbbbbbbbbbb", fmt.Sprint(t0+171))}
		case strings.HasPrefix(q, "max_over_time({__name__=\"kcm.container.mem_limit\""):
			res = []string{inst("c2bbbbbbbbbbbbbbbb", "671088640")}
		}
		fmt.Fprintf(w, `{"status":"success","data":{"result":[%s]}}`, strings.Join(res, ","))
	}))
	t.Cleanup(srv.Close)
	return &VM{BaseURL: srv.URL}
}

func TestK8sUsageObsPeakVsNewLimit(t *testing.T) {
	at := time.Date(2026, 8, 25, 0, 23, 49, 0, time.UTC)
	u := &usageVM{}
	vm := u.server(t, at.Unix())
	after, before := "640Mi", "1Gi"
	r := k8sRollout{At: at, Cluster: "c", Namespace: "ns", Deployment: "pay", New: "pay-58c4"}
	limits := []k8sDiff{
		{Container: "svc", Field: "resources.limits.memory", Change: "modified", Before: &before, After: &after},
		{Container: "init:setup", Field: "resources.limits.memory", Change: "modified", Before: &before, After: &after}, // init 제외
	}
	res := &k8sChangeResult{SourceStatus: map[string]string{}, SourceErrors: map[string]string{}, Meta: map[string]any{}}
	budget := k8sChgMaxUsageObs
	end := at.Add(4 * time.Minute)
	obs, refs := k8sUsageObs(context.Background(), vm, r, limits, end, "same_rs_reactivated", res, &budget)
	if len(obs) != 1 || budget != k8sChgMaxUsageObs-1 {
		t.Fatalf("init 컨테이너 제외 1건, 예산 1 소모: %d obs, budget %d", len(obs), budget)
	}
	o := obs[0]
	if o["peak_usage"] != 1167949824.0 || o["new_limit_value"] != float64(671088640) || o["peak_pct_of_new_limit"] != 174.0 {
		t.Fatalf("피크·한도·%%: %+v", o)
	}
	if o["peak_at"] != at.Add(171*time.Second).UTC().Format(time.RFC3339) || o["peak_pod"] != "pay-58c4-aaaaa" || o["per_instance_total"] != 2 {
		t.Fatalf("피크 시각(tmax 정확값)·pod·인스턴스: %+v", o)
	}
	if _, coarse := o["peak_at_precision_s"]; coarse {
		t.Fatalf("tmax 성공이면 정밀도 표식 없음: %+v", o)
	}
	rows := o["per_instance"].([]map[string]any)
	if len(rows) != 2 || rows[0]["container_id"] != "c1aaaaaaaaaa" || rows[0]["peak_pct"] != 89.1 ||
		rows[1]["last_value"] != 638800000.0 || rows[1]["last_pct"] != 95.2 || rows[1]["first_seen"] != at.Add(180*time.Second).UTC().Format(time.RFC3339) {
		t.Fatalf("인스턴스 요약(처음 관측 순): %+v", rows)
	}
	if o["observed_limit_max"] != 671088640.0 || o["unit"] != "B" || o["usage_metric"] != "kcm.container.mem_usage" || o["step_s"] != int64(60) {
		t.Fatalf("한도 지표·단위·step: %+v", o)
	}
	if len(refs) != 2 || !strings.HasPrefix(refs[0], "vm:kcm.container.mem_usage:c:ns/pay-58c4-*:svc:") {
		t.Fatalf("refs: %v", refs)
	}
	// 선택자 — 클러스터 target_id·namespace·컨테이너·새 RS pod 접두. instant는 구간 [롤아웃, 끝]을 끝 시각에 평가.
	if len(u.queries) != 3 {
		t.Fatalf("관측 1건 = VM 조회 3회: %v", u.queries)
	}
	for i, q := range u.queries {
		for _, want := range []string{`target_id="c"`, `namespace="ns"`, `container="svc"`, `pod=~"pay-58c4-[a-z0-9]+"`} {
			if !strings.Contains(q, want) {
				t.Fatalf("질의 %d에 %s 없음: %s", i, want, q)
			}
		}
		if u.times[i] != "" && (u.times[i] != fmt.Sprint(end.Unix()) || !strings.Contains(q, "[240s]")) {
			t.Fatalf("instant 질의 %d: time=%s q=%s", i, u.times[i], q)
		}
	}
	// 해석 문구 없음 — 수치·시각·토큰 키만.
	for k := range o {
		switch k {
		case "container", "resource", "new_limit", "new_limit_value", "unit", "usage_metric", "window",
			"peak_usage", "peak_pod", "peak_pct_of_new_limit", "peak_at", "observed_limit_max", "refs",
			"per_instance", "per_instance_total", "step_s":
		default:
			t.Fatalf("허용 밖 키 %s", k)
		}
	}
}

func TestK8sUsageObsFailureAndNoSamples(t *testing.T) {
	at := time.Date(2026, 8, 25, 0, 23, 49, 0, time.UTC)
	after := "1"
	r := k8sRollout{At: at, Cluster: "c", Namespace: "ns", Deployment: "pay", New: "pay-58c4"}
	limits := []k8sDiff{{Container: "svc", Field: "resources.limits.cpu", Change: "added", After: &after}}
	res := &k8sChangeResult{SourceStatus: map[string]string{}, SourceErrors: map[string]string{}, Meta: map[string]any{}}
	budget := k8sChgMaxUsageObs
	dgResetBreaker()
	obs, _ := k8sUsageObs(context.Background(), &VM{BaseURL: dgDead(t)}, r, limits, at.Add(time.Minute), "window_end", res, &budget)
	if len(obs) != 1 || obs[0]["observation"] != "query_failed" || res.SourceErrors["vm"] == "" || obs[0]["new_limit_value"] != 1000.0 {
		t.Fatalf("VM 실패는 결손 토큰: %+v %v", obs, res.SourceErrors)
	}
	dgResetBreaker()
	u := &usageVM{}
	obs, _ = k8sUsageObs(context.Background(), u.server(t, 0), r, limits, at.Add(time.Minute), "window_end", res, &budget) // t0=0 → range 표본 없음
	if len(obs) != 1 || obs[0]["observation"] != "no_samples" || obs[0]["peak_usage"] != nil {
		t.Fatalf("표본 없음: %+v", obs)
	}
	if obs, _ := k8sUsageObs(context.Background(), nil, r, limits, at.Add(time.Minute), "window_end", res, &budget); obs != nil ||
		res.SourceStatus["vm_usage"] == "" {
		t.Fatal("VM 미연결은 조회 없이 상태만 밝힌다")
	}
}

// list_changes 전 경로 — 한도 변경 롤아웃에 사용량 관측이 붙고, 요약·refs·메타에 합류한다.
func TestListChangesPostRolloutUsageEndToEnd(t *testing.T) {
	const (
		cl   = "aaaaaaaa-0000-4000-8000-0000000000c1"
		depT = "aaaaaaaa-0000-4000-8000-0000000000c2"
	)
	ch := dgCH(t, []dgCHScript{
		{"WHERE reason IN", []map[string]any{
			{"at": "2026-08-25 00:23:49.000", "cluster": cl, "obj_target": depT, "namespace": "ns", "object_kind": "Deployment",
				"object_name": "pay", "reason": "ScalingReplicaSet", "body": "Scaled up replica set pay-58c4 to 1 from 0"},
			{"at": "2026-08-25 00:24:59.000", "cluster": cl, "obj_target": depT, "namespace": "ns", "object_kind": "Deployment",
				"object_name": "pay", "reason": "ScalingReplicaSet", "body": "Scaled down replica set pay-65c8 to 0 from 1"},
		}},
	})
	pgFake := newDgFakePG(t)
	pgFake.script("FROM targets", []string{"id", "name", "display_name", "address", "type"}, [][]string{
		{cl, "cluster-x", "prod", "", "kubernetes"},
		{depT, cl + ":deployment:ns/pay", "ns/pay", "", "kubernetes_resource"},
	})
	pgFake.script("= ANY(", []string{"target_id", "namespace", "name", "owner_kind", "owner_name", "created", "yaml"}, [][]string{
		{cl, "ns", "pay-58c4", "Deployment", "pay", "2026-08-25T00:23:49Z", rsJSON("218", "", `{"name":"A","value":"1"}`, "/h", "640Mi")},
		{cl, "ns", "pay-65c8", "Deployment", "pay", "2026-08-25T00:22:42Z", rsJSON("217", "", `{"name":"A","value":"1"}`, "/h", "1Gi")},
	})
	db, err := sql.Open("pgx", pgFake.dsn())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	at := time.Date(2026, 8, 25, 0, 23, 49, 0, time.UTC)
	peakAt := at.Add(171 * time.Second).Unix()
	u := &usageVM{}
	vm := u.server(t, at.Unix())
	// fake VM의 pod 이름은 pay-58c4-aaaaa — 선택자 접두와 맞물린다.
	first, last := at.Add(5*time.Minute), at.Add(30*time.Minute)
	out, err := NewListChangesTool(db, ch, vm, first, last, func() time.Time { return last }).Call(context.Background(), json.RawMessage(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	f0 := env.Findings[0]
	if f0["kind"] != "k8s_rollout" || f0["new_replicaset"] != "pay-58c4" {
		t.Fatalf("롤아웃: %+v", f0)
	}
	obs, ok := f0["post_rollout_usage"].([]map[string]any)
	if !ok || len(obs) != 1 || obs[0]["peak_pct_of_new_limit"] != 174.0 || obs[0]["new_limit"] != "640Mi" {
		t.Fatalf("롤아웃 후 사용량: %+v", f0["post_rollout_usage"])
	}
	w := obs[0]["window"].(map[string]any)
	if w["from"] != "2026-08-25T00:23:49Z" || w["to"] != last.Format(time.RFC3339) || w["to_basis"] != "window_end" {
		t.Fatalf("관측 구간: %+v", w)
	}
	refs := strings.Join(env.Refs, " ")
	if !strings.Contains(refs, "vm:kcm.container.mem_usage:"+cl+":ns/pay-58c4-*:svc:") ||
		!strings.Contains(refs, "vm:kcm.container.mem_limit:") {
		t.Fatalf("봉투 refs에 VM 관측 없음: %v", env.Refs)
	}
	if !strings.Contains(env.Summary, "[resources.limits.memory] memory 사용 피크 174%(새 한도 640Mi, 피크 "+time.Unix(peakAt, 0).UTC().Format(time.RFC3339)+
		", 컨테이너 인스턴스 2개, 마지막 인스턴스 마지막 값 95.2%)@2026-08-25T00:23:49Z") {
		t.Fatalf("요약 토막: %s", env.Summary)
	}
	meta := env.Findings[len(env.Findings)-1]["k8s_workload"].(map[string]any)
	pu := meta["post_rollout_usage"].(map[string]any)
	if pu["observations"] != 1 || pu["cap"] != k8sChgMaxUsageObs {
		t.Fatalf("메타: %+v", pu)
	}
}
