// list_changes K8s 워크로드 구획 단위 — 저장소 무관(hermetic).
// 고정하는 규칙: 스케일 메시지 고정 템플릿 파싱 · 파드 템플릿 구조 비교(노출 허용
// 필드만, valueFrom은 참조 이름만) · 리비전 주석 기반 직전 RS 선택 · 이벤트 짝
// 기반 롤아웃 판별(재개·미준비 경로 포함). 데이터 모양은 2026-10-06 Polestar v3
// 캡처 실측(kcm_resources.yaml = API 객체 JSON, revision-history 재사용)을 본떴다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestParseScaleMessage(t *testing.T) {
	cases := []struct {
		reason, body string
		ok           bool
		dir, rs      string
		from         int // -1 = 미기록
		to           int
	}{
		{"ScalingReplicaSet", "Scaled down replica set app-68f6c6f697 to 0 from 1", true, "down", "app-68f6c6f697", 1, 0},
		{"ScalingReplicaSet", "Scaled up replica set app-78bfbc98d to 1 from 0", true, "up", "app-78bfbc98d", 0, 1},
		{"ScalingReplicaSet", "Scaled up replica set app-78bfbc98d to 3", true, "up", "app-78bfbc98d", -1, 3}, // 1.26 미만 서식
		{"SuccessfulRescale", "New size: 4; reason: cpu resource utilization (percentage of request) above target", true, "rescale", "", -1, 4},
		{"ScalingReplicaSet", "something else entirely", false, "", "", -1, 0},
		{"Killing", "Scaled down replica set app-1 to 0 from 1", false, "", "", -1, 0}, // 사유가 템플릿 소유자가 아니면 안 푼다
	}
	for _, c := range cases {
		dir, rs, from, to, _, ok := parseScaleMessage(c.reason, c.body)
		if ok != c.ok {
			t.Fatalf("%q ok=%v want %v", c.body, ok, c.ok)
		}
		if !ok {
			continue
		}
		if dir != c.dir || rs != c.rs || to != c.to {
			t.Fatalf("%q → dir=%s rs=%s to=%d", c.body, dir, rs, to)
		}
		if (c.from < 0) != (from == nil) || (from != nil && *from != c.from) {
			t.Fatalf("%q from=%v want %d", c.body, from, c.from)
		}
	}
}

// rsJSON은 실측 모양의 ReplicaSet 객체 JSON이다(라벨·주석 포함 — 비교가 그것을
// 노출하지 않음을 함께 검사한다).
func rsJSON(rev, hist string, env string, probePath, memLimit string) string {
	ann := `"deployment.kubernetes.io/revision":"` + rev + `"`
	if hist != "" {
		ann += `,"deployment.kubernetes.io/revision-history":"` + hist + `"`
	}
	return `{"kind":"ReplicaSet","metadata":{"name":"x","labels":{"app":"svc","secret-label":"LBL"},"annotations":{` + ann + `,"note":"ANNOT"}},` +
		`"spec":{"replicas":1,"template":{"metadata":{"labels":{"app":"svc"},"annotations":{"kubectl.kubernetes.io/restartedAt":"RESTARTED"}},` +
		`"spec":{"containers":[{"name":"svc","image":"svc:latest","ports":[{"containerPort":8080,"protocol":"TCP"}],` +
		`"env":[{"name":"DB_PASS","valueFrom":{"secretKeyRef":{"name":"pg-secret","key":"POSTGRES_PASSWORD"}}},` + env + `],` +
		`"resources":{"limits":{"cpu":"500m","memory":"` + memLimit + `"},"requests":{"cpu":"200m","memory":"256Mi"}},` +
		`"livenessProbe":{"httpGet":{"path":"` + probePath + `","port":8080,"scheme":"HTTP","httpHeaders":[{"name":"Authorization","value":"TOKEN"}]},"periodSeconds":15,"timeoutSeconds":3,"failureThreshold":5,"successThreshold":1}}]}}}}`
}

func mustPod(t *testing.T, raw string) (k8sPodSpec, int, []int) {
	t.Helper()
	pod, rev, hist, perr := parseRSSpec(raw)
	if perr != "" {
		t.Fatalf("parseRSSpec: %s", perr)
	}
	return pod, rev, hist
}

func TestParseRSSpecRevisions(t *testing.T) {
	_, rev, hist := mustPod(t, rsJSON("161", "143,145,159", `{"name":"A","value":"1"}`, "/h", "1Gi"))
	if rev != 161 || len(hist) != 3 || hist[2] != 159 {
		t.Fatalf("rev=%d hist=%v", rev, hist)
	}
	if _, _, _, perr := parseRSSpec("apiVersion: apps/v1\nkind: ReplicaSet"); perr == "" {
		t.Fatal("JSON 아닌 서식은 비교 불가로 표시돼야 한다")
	}
	if _, _, _, perr := parseRSSpec(""); perr == "" {
		t.Fatal("빈 스펙은 비교 불가로 표시돼야 한다")
	}
}

func TestDiffPodSpecsExposedFieldsOnly(t *testing.T) {
	prev, _, _ := mustPod(t, rsJSON("159", "", `{"name":"JAVA_TOOL_OPTIONS","value":"-javaagent:/opt/a.jar"},{"name":"OLD","value":"gone"}`, "/actuator/health", "1Gi"))
	next, _, _ := mustPod(t, rsJSON("160", "", `{"name":"JAVA_TOOL_OPTIONS","value":"-javaagent:/opt/a.jar -Xmx96m -Xms96m"},{"name":"NEW","valueFrom":{"configMapKeyRef":{"name":"cfg","key":"K"}}}`, "/actuator/health/x-fail", "256Mi"))
	diffs, clipped := diffPodSpecs(prev, next)
	if clipped != 0 {
		t.Fatalf("짧은 값이 잘렸다: %d", clipped)
	}
	got := map[string]k8sDiff{}
	for _, d := range diffs {
		got[d.Field] = d
	}
	jt := got["env.JAVA_TOOL_OPTIONS"]
	if jt.Change != "modified" || jt.Before == nil || jt.After == nil || !strings.Contains(*jt.After, "-Xmx96m") || strings.Contains(*jt.Before, "-Xmx96m") {
		t.Fatalf("env 변경 이상: %+v", jt)
	}
	if d := got["env.OLD"]; d.Change != "removed" || d.After != nil {
		t.Fatalf("env 제거 이상: %+v", d)
	}
	if d := got["env.NEW"]; d.Change != "added" || d.After == nil || *d.After != "valueFrom configMapKeyRef(name=cfg)" {
		t.Fatalf("valueFrom은 참조 종류·이름만: %+v", d)
	}
	if d := got["resources.limits.memory"]; d.Change != "modified" || *d.Before != "1Gi" || *d.After != "256Mi" {
		t.Fatalf("limits 변경 이상: %+v", d)
	}
	if d := got["livenessProbe.httpGet.path"]; d.Change != "modified" || *d.After != "/actuator/health/x-fail" {
		t.Fatalf("프로브 경로 변경 이상: %+v", d)
	}
	if len(diffs) != 5 {
		t.Fatalf("차이 %d건, want 5: %+v", len(diffs), diffs)
	}
	// 노출 금지: 라벨·주석·시크릿 key·프로브 헤더 값이 어디에도 없어야 한다.
	b, _ := json.Marshal(diffs)
	for _, banned := range []string{"LBL", "ANNOT", "RESTARTED", "POSTGRES_PASSWORD", "TOKEN", "secret-label", "Authorization"} {
		if strings.Contains(string(b), banned) {
			t.Fatalf("비노출 값 %q가 차이에 실렸다: %s", banned, b)
		}
	}
}

func TestDiffPodSpecsNoChangeAndStructure(t *testing.T) {
	a, _, _ := mustPod(t, rsJSON("1", "", `{"name":"A","value":"1"}`, "/h", "1Gi"))
	b, _, _ := mustPod(t, rsJSON("2", "", `{"name":"A","value":"1"}`, "/h", "1Gi"))
	if diffs, _ := diffPodSpecs(a, b); len(diffs) != 0 {
		t.Fatalf("같은 템플릿인데 차이 %d건(라벨·주석 차이는 비교 대상 아님): %+v", len(diffs), diffs)
	}

	period := 10
	path := "/ready"
	probe := &k8sProbe{PeriodSeconds: &period}
	probe.HTTPGet = &struct {
		Path   string          `json:"path"`
		Port   json.RawMessage `json:"port"`
		Scheme string          `json:"scheme"`
	}{Path: path, Port: json.RawMessage(`"http"`)}
	prev := k8sPodSpec{Containers: []k8sContainerSpec{{Name: "app", Image: "app:1", Command: []string{"java"}, Args: []string{"-jar", "a.jar"}}}}
	next := k8sPodSpec{
		InitContainers: []k8sContainerSpec{{Name: "migrate", Image: "mig:1"}},
		Containers: []k8sContainerSpec{
			{Name: "app", Image: "app:2", Command: []string{"java"}, Args: []string{"-jar", "b.jar"}, ReadinessProbe: probe},
			{Name: "sidecar", Image: "proxy:1"},
		},
	}
	diffs, _ := diffPodSpecs(prev, next)
	want := []string{"init:migrate|container|added", "app|image|modified", "app|args|modified", "app|readinessProbe|added", "sidecar|container|added"}
	if len(diffs) != len(want) {
		t.Fatalf("차이 %d건 want %d: %+v", len(diffs), len(want), diffs)
	}
	for i, d := range diffs {
		if got := d.Container + "|" + d.Field + "|" + d.Change; got != want[i] {
			t.Fatalf("순서/내용[%d] = %s, want %s", i, got, want[i])
		}
	}
	if *diffs[3].After != "httpGet.path=/ready httpGet.port=http periodSeconds=10" {
		t.Fatalf("프로브 요약 이상: %q", *diffs[3].After)
	}

	// 긴 값은 자르고 센다.
	long := strings.Repeat("x", k8sChgValueMax+40)
	pa := k8sPodSpec{Containers: []k8sContainerSpec{{Name: "c", Env: []k8sEnvVar{{Name: "V", Value: &path}}}}}
	pb := k8sPodSpec{Containers: []k8sContainerSpec{{Name: "c", Env: []k8sEnvVar{{Name: "V", Value: &long}}}}}
	diffs, clipped := diffPodSpecs(pa, pb)
	if clipped != 1 || len([]rune(*diffs[0].After)) >= len(long) || !strings.Contains(*diffs[0].After, "전체") {
		t.Fatalf("절단 이상: clipped=%d after=%q", clipped, *diffs[0].After)
	}
}

// 실측(f09 캡처): 5c4d…가 rev 161(이력 …159), 78bf…가 rev 160(이력 152).
// creationTimestamp(8월 11·12일)는 8월 22일 롤아웃과 무관하다 — 리비전이 정본.
func TestPrevByRevision(t *testing.T) {
	a := &k8sRS{Name: "o-5c4d", Revision: 161, History: []int{143, 145, 147, 149, 151, 153, 155, 157, 159}}
	b := &k8sRS{Name: "o-78bf", Revision: 160, History: []int{152}}
	c := &k8sRS{Name: "o-6f4c", Revision: 139, History: []int{69, 83}}
	sibs := []*k8sRS{a, b, c}
	if p := k8sPrevByRevision(b, sibs); p == nil || p.Name != "o-5c4d" {
		t.Fatalf("160의 직전(159)은 o-5c4d여야 한다: %v", p)
	}
	if p := k8sPrevByRevision(a, sibs); p == nil || p.Name != "o-78bf" {
		t.Fatalf("161의 직전(160)은 o-78bf여야 한다: %v", p)
	}
	// 직전 리비전을 자기 이력이 갖고 있으면(그 사이 RS 삭제) 지어내지 않는다.
	x := &k8sRS{Name: "x", Revision: 10, History: []int{9}}
	y := &k8sRS{Name: "y", Revision: 7}
	if p := k8sPrevByRevision(x, []*k8sRS{x, y}); p != nil {
		t.Fatalf("직전 리비전(9) 보유 RS가 없는데 %s를 골랐다", p.Name)
	}
	if k8sPrevByRevision(&k8sRS{Name: "z"}, sibs) != nil {
		t.Fatal("리비전 주석 없는 RS는 직전 RS 미상이어야 한다")
	}
	if l := k8sLatestRevision(sibs); l == nil || l.Name != "o-5c4d" {
		t.Fatalf("최신 리비전 RS = %v", l)
	}
}

func scale(at string, dir, rs string, from, to int) *k8sScale {
	ts, _ := time.Parse(time.RFC3339, at)
	f := from
	return &k8sScale{At: ts, Cluster: "c", ObjTarget: "dep-t", Namespace: "ns", Kind: "Deployment", Name: "o",
		Reason: "ScalingReplicaSet", Dir: dir, RS: rs, From: &f, To: to}
}

func TestDetectRolloutsScaleEventPairs(t *testing.T) {
	// f09 실측 순서: 업 78bf → 다운 5c4d → (되돌림) 업 5c4d → 다운 78bf.
	evs := []*k8sScale{
		scale("2026-08-22T04:19:17Z", "up", "o-78bf", 0, 1),
		scale("2026-08-22T04:20:32Z", "down", "o-5c4d", 1, 0),
		scale("2026-08-22T04:23:30Z", "up", "o-5c4d", 0, 1),
		scale("2026-08-22T04:24:50Z", "down", "o-78bf", 1, 0),
	}
	a := &k8sRS{Name: "o-5c4d", Revision: 161, History: []int{159}}
	b := &k8sRS{Name: "o-78bf", Revision: 160, History: []int{152}}
	ro := k8sDetectRollouts(evs, map[string]*k8sRS{a.Name: a, b.Name: b}, []*k8sRS{a, b})
	if len(ro) != 2 {
		t.Fatalf("롤아웃 %d건, want 2", len(ro))
	}
	if ro[0].New != "o-78bf" || ro[0].Prev != "o-5c4d" || ro[0].PrevBasis != "revision_order+scale_event" || ro[0].DeployTgt != "dep-t" {
		t.Fatalf("첫 롤아웃 이상: %+v", ro[0])
	}
	if ro[1].New != "o-5c4d" || ro[1].Prev != "o-78bf" || ro[1].PrevBasis != "revision_order+scale_event" {
		t.Fatalf("되돌림 롤아웃 이상: %+v", ro[1])
	}
	for i, e := range evs {
		if !e.consumed {
			t.Fatalf("이벤트[%d]가 롤아웃 단계로 묶이지 않았다", i)
		}
	}
}

func TestDetectRolloutsReplicaOnlyAndResume(t *testing.T) {
	// f04 실측 모양: 단독 0 스케일 다운 — 롤아웃이 아니라 레플리카 변경으로 남는다.
	down := scale("2026-08-21T04:41:04Z", "down", "s-68f6", 1, 0)
	if ro := k8sDetectRollouts([]*k8sScale{down}, nil, nil); len(ro) != 0 || down.consumed {
		t.Fatalf("단독 다운이 롤아웃이 됐다: %+v", ro)
	}
	// 같은 RS의 0→1 재개는 리비전 주석에 옛 RS가 있어도 롤아웃이 아니다.
	x := &k8sRS{Name: "s-68f6", Revision: 5, History: []int{3}}
	y := &k8sRS{Name: "s-old", Revision: 4}
	evs := []*k8sScale{
		scale("2026-08-21T04:41:04Z", "down", "s-68f6", 1, 0),
		scale("2026-08-21T04:50:00Z", "up", "s-68f6", 0, 1),
	}
	if ro := k8sDetectRollouts(evs, map[string]*k8sRS{x.Name: x, y.Name: y}, []*k8sRS{x, y}); len(ro) != 0 {
		t.Fatalf("재개가 롤아웃으로 판정됐다: %+v", ro)
	}
	if evs[0].consumed || evs[1].consumed {
		t.Fatal("재개 단계는 레플리카 변경으로 남아야 한다")
	}
}

func TestDetectRolloutsRevisionFallbackAndDisagreement(t *testing.T) {
	// 새 RS가 준비되지 않아 옛 RS가 안 내려간 경우(프로브 오설정 등) — 리비전 주석 경로.
	nw := &k8sRS{Name: "p-new", Revision: 12}
	old := &k8sRS{Name: "p-old", Revision: 11}
	up := scale("2026-08-22T01:00:00Z", "up", "p-new", 0, 1)
	ro := k8sDetectRollouts([]*k8sScale{up}, map[string]*k8sRS{nw.Name: nw, old.Name: old}, []*k8sRS{nw, old})
	if len(ro) != 1 || ro[0].Prev != "p-old" || ro[0].PrevBasis != "revision_order" || len(ro[0].Notes) == 0 {
		t.Fatalf("리비전 순서 경로 이상: %+v", ro)
	}
	if !up.consumed || len(ro[0].Steps) != 1 {
		t.Fatal("새 RS 업 단계가 롤아웃에 묶여야 한다")
	}

	// 리비전 순서가 맞춰졌으면 가장 가까운 다운(겹친 앞 롤아웃의 마무리)보다 리비전
	// r-1 소유 RS가 이전 템플릿이다 — 다운 RS는 메모로 밝힌다.
	other := &k8sRS{Name: "p-other", Revision: 11}
	evs := []*k8sScale{
		scale("2026-08-22T01:00:00Z", "up", "p-new", 0, 1),
		scale("2026-08-22T01:01:00Z", "down", "p-third", 1, 0),
	}
	ro = k8sDetectRollouts(evs, map[string]*k8sRS{nw.Name: nw, other.Name: other}, []*k8sRS{nw, other})
	if len(ro) != 1 || ro[0].Prev != "p-other" || ro[0].PrevBasis != "revision_order" || len(ro[0].Notes) == 0 ||
		!strings.Contains(ro[0].Notes[0], "p-third") {
		t.Fatalf("불일치 처리 이상: %+v", ro)
	}
	if evs[1].consumed {
		t.Fatal("이전 RS가 아닌 RS의 다운은 이 롤아웃 단계가 아니다(레플리카 변경으로 남아야 한다)")
	}

	// 맞춤이 안 되면(최신 리비전 소유가 다른 RS — 스냅샷 이후 롤아웃 등) 이벤트 짝으로 돌아간다.
	later := &k8sRS{Name: "p-later", Revision: 13}
	evs = []*k8sScale{
		scale("2026-08-22T01:00:00Z", "up", "p-new", 0, 1),
		scale("2026-08-22T01:01:00Z", "down", "p-third", 1, 0),
	}
	ro = k8sDetectRollouts(evs, map[string]*k8sRS{nw.Name: nw, other.Name: other, later.Name: later}, []*k8sRS{nw, other, later})
	if len(ro) != 1 || ro[0].Prev != "p-third" || ro[0].PrevBasis != "scale_event" ||
		len(ro[0].Notes) == 0 || !strings.Contains(ro[0].Notes[0], "p-other") {
		t.Fatalf("맞춤 실패 시 이벤트 짝 폴백 이상: %+v", ro)
	}

	// 이전 개수 미기록 서식(1.26 미만)의 업은 리비전 폴백을 쓰지 않는다 — 1→3 업일 수 있다.
	old126 := &k8sScale{At: up.At, Cluster: "c", Namespace: "ns", Kind: "Deployment", Name: "o", Dir: "up", RS: "p-new", To: 3}
	if ro := k8sDetectRollouts([]*k8sScale{old126}, map[string]*k8sRS{nw.Name: nw, old.Name: old}, []*k8sRS{nw, old}); len(ro) != 0 {
		t.Fatalf("from 미기록 업이 리비전 폴백으로 롤아웃이 됐다: %+v", ro)
	}

	// 단, 이전 개수 미기록이어도 RS 생성 시각이 맞으면 새 RS 생성(0→N)이다(f05-h 실측).
	created := &k8sRS{Name: "p-new", Revision: 12, Created: up.At, Cluster: "c", Namespace: "ns", Owner: "o"}
	noFrom := &k8sScale{At: up.At, Cluster: "c", Namespace: "ns", Kind: "Deployment", Name: "o", Dir: "up", RS: "p-new", To: 1}
	k8sNormalizeNewRS([]*k8sScale{noFrom}, map[string][]*k8sRS{k8sKey("c", "ns", "o"): {created, old}})
	if noFrom.From == nil || *noFrom.From != 0 || !noFrom.NewRS {
		t.Fatalf("생성 시각 일치 업이 0→N으로 확정되지 않았다: %+v", noFrom)
	}
	ro = k8sDetectRollouts([]*k8sScale{noFrom}, map[string]*k8sRS{created.Name: created, old.Name: old}, []*k8sRS{created, old})
	if len(ro) != 1 || ro[0].Prev != "p-old" || !strings.HasSuffix(k8sStep(ro[0].Steps[0]), "(RS 생성)") {
		t.Fatalf("새 RS 생성 롤아웃 이상: %+v", ro)
	}

	// 롤아웃 범위(±span) 밖의 다운은 짝이 아니다.
	far := []*k8sScale{
		scale("2026-08-22T01:00:00Z", "up", "p-new", 0, 1),
		scale("2026-08-22T03:00:00Z", "down", "p-third", 1, 0),
	}
	ro = k8sDetectRollouts(far, map[string]*k8sRS{}, nil)
	if len(ro) != 0 || far[1].consumed {
		t.Fatalf("범위 밖 다운이 짝이 됐다: %+v", ro)
	}
}

// 겹친 롤아웃(f05-r 실측): 힙 축소 템플릿(65c8) 롤아웃이 끝나기 전에 메모리 한도
// 축소 템플릿(58c4)이 적용됐다. 가장 가까운 다운 짝은 58c4의 이전을 7bdc로 오인한다 —
// 리비전 원장(216→217→218→219→220)을 끝에서 맞추면 전부 맞는다.
func TestDetectRolloutsOverlappingRevisionOrder(t *testing.T) {
	a := &k8sRS{Name: "p-7bdc", Revision: 220, History: []int{212, 214, 216}}
	b := &k8sRS{Name: "p-65c8", Revision: 219, History: []int{217}}
	c := &k8sRS{Name: "p-58c4", Revision: 218}
	sibs := []*k8sRS{a, b, c}
	byName := map[string]*k8sRS{a.Name: a, b.Name: b, c.Name: c}
	evs := []*k8sScale{
		scale("2026-08-25T00:22:42Z", "up", "p-65c8", 0, 1),
		scale("2026-08-25T00:23:47Z", "down", "p-7bdc", 1, 0),
		scale("2026-08-25T00:23:49Z", "up", "p-58c4", 0, 1),
		scale("2026-08-25T00:24:59Z", "down", "p-65c8", 1, 0),
		scale("2026-08-25T00:26:13Z", "up", "p-65c8", 0, 1),
		scale("2026-08-25T00:27:19Z", "down", "p-58c4", 1, 0),
		scale("2026-08-25T00:27:23Z", "up", "p-7bdc", 0, 1),
		scale("2026-08-25T00:28:24Z", "down", "p-65c8", 1, 0),
	}
	ro := k8sDetectRollouts(evs, byName, sibs)
	want := [][2]string{{"p-65c8", "p-7bdc"}, {"p-58c4", "p-65c8"}, {"p-65c8", "p-58c4"}, {"p-7bdc", "p-65c8"}}
	if len(ro) != len(want) {
		t.Fatalf("롤아웃 %d건 want %d: %+v", len(ro), len(want), ro)
	}
	for i, w := range want {
		if ro[i].New != w[0] || ro[i].Prev != w[1] || !strings.HasPrefix(ro[i].PrevBasis, "revision_order") {
			t.Fatalf("롤아웃[%d] = %s←%s (%s), want %s←%s", i, ro[i].New, ro[i].Prev, ro[i].PrevBasis, w[0], w[1])
		}
	}
	for i, e := range evs {
		if !e.consumed {
			t.Fatalf("이벤트[%d] %s %s가 어느 롤아웃 단계에도 안 묶였다", i, e.Dir, e.RS)
		}
	}
	if owner, revs := k8sRevisionOwners(sibs); owner[217] != "p-65c8" || revs[0] != 220 {
		t.Fatalf("리비전 원장 이상: %v %v", owner, revs)
	}
}

// list_changes 전 경로 — fake CH(스케일 이벤트) + fake PG(명부·ReplicaSet).
// K8s 항목이 앞에 오고, 대상 UUID·refs·요약 토막이 실려야 한다.
func TestListChangesK8sSectionEndToEnd(t *testing.T) {
	const (
		cl    = "aaaaaaaa-0000-4000-8000-000000000001"
		depT  = "aaaaaaaa-0000-4000-8000-000000000002"
		rsNew = "aaaaaaaa-0000-4000-8000-000000000003"
		rsOld = "aaaaaaaa-0000-4000-8000-000000000004"
		shipT = "aaaaaaaa-0000-4000-8000-000000000005"
	)
	ch := dgCH(t, []dgCHScript{
		{"WHERE reason IN", []map[string]any{
			{"at": "2026-08-22 04:19:17.000", "cluster": cl, "obj_target": depT, "namespace": "ns", "object_kind": "Deployment",
				"object_name": "order", "reason": "ScalingReplicaSet", "body": "Scaled up replica set order-new to 1 from 0"},
			{"at": "2026-08-22 04:20:32.000", "cluster": cl, "obj_target": depT, "namespace": "ns", "object_kind": "Deployment",
				"object_name": "order", "reason": "ScalingReplicaSet", "body": "Scaled down replica set order-old to 0 from 1"},
			{"at": "2026-08-22 04:41:04.000", "cluster": cl, "obj_target": shipT, "namespace": "ns", "object_kind": "Deployment",
				"object_name": "shipping", "reason": "ScalingReplicaSet", "body": "Scaled down replica set shipping-68f6 to 0 from 1"},
		}},
		{"AS lo, toString(max(timestamp)) AS hi", []map[string]any{{"lo": "2026-08-22 02:09:12.000", "hi": "2026-08-22 04:53:54.000", "n": 115}}},
	})
	pgFake := newDgFakePG(t)
	pgFake.script("FROM targets", []string{"id", "name", "display_name", "address", "type"}, [][]string{
		{cl, "cluster-x", "prod", "", "kubernetes"},
		{depT, cl + ":deployment:ns/order", "ns/order", "", "kubernetes_resource"},
		{rsNew, cl + ":replicaset:ns/order-new", "ns/order-new", "", "kubernetes_resource"},
		{rsOld, cl + ":replicaset:ns/order-old", "ns/order-old", "", "kubernetes_resource"},
		{shipT, cl + ":deployment:ns/shipping", "ns/shipping", "", "kubernetes_resource"},
	})
	rsCols := []string{"target_id", "namespace", "name", "owner_kind", "owner_name", "created", "yaml"}
	pgFake.script("= ANY(", rsCols, [][]string{
		{cl, "ns", "order-new", "Deployment", "order", "2026-08-12T07:11:05Z",
			rsJSON("160", "152", `{"name":"JAVA_TOOL_OPTIONS","value":"-javaagent:/a.jar -Xmx96m"}`, "/h", "1Gi")},
		{cl, "ns", "order-old", "Deployment", "order", "2026-08-11T00:03:35Z",
			rsJSON("161", "159", `{"name":"JAVA_TOOL_OPTIONS","value":"-javaagent:/a.jar"}`, "/h", "1Gi")},
	})
	db, err := sql.Open("pgx", pgFake.dsn())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })

	first := time.Date(2026, 8, 22, 4, 19, 16, 0, time.UTC)
	last := time.Date(2026, 8, 22, 4, 50, 0, 0, time.UTC)
	tool := NewListChangesTool(db, ch, nil, first, last, func() time.Time { return last })
	out, err := tool.Call(context.Background(), json.RawMessage(`{"target":"`+shipT+`"}`))
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if len(env.Findings) < 3 {
		t.Fatalf("findings %d건: %+v", len(env.Findings), env.Findings)
	}
	// 힌트(shipping) 귀속분이 맨 앞, 그다음 롤아웃.
	f0, f1 := env.Findings[0], env.Findings[1]
	if f0["kind"] != "k8s_replica_change" || f0["from"] != 1 || f0["to"] != 0 || f0["hint_target_included"] != true {
		t.Fatalf("레플리카 변경(힌트) 이상: %+v", f0)
	}
	if f1["kind"] != "k8s_rollout" || f1["new_replicaset"] != "order-new" || f1["previous_replicaset"] != "order-old" ||
		f1["template_diff_status"] != "compared" {
		t.Fatalf("롤아웃 이상: %+v", f1)
	}
	diffs := f1["template_diff"].([]k8sDiff)
	if len(diffs) != 1 || diffs[0].Field != "env.JAVA_TOOL_OPTIONS" {
		t.Fatalf("템플릿 차이 이상: %+v", diffs)
	}
	roles := map[string]string{}
	for _, tg := range f1["targets"].([]map[string]any) {
		roles[tg["role"].(string)] = tg["target_id"].(string)
	}
	if roles["deployment"] != depT || roles["new_replicaset"] != rsNew || roles["previous_replicaset"] != rsOld {
		t.Fatalf("대상 해소 이상: %v", roles)
	}
	if f1["snapshot_latest_replicaset"] != "order-old" {
		t.Fatalf("스냅샷 최신 리비전 RS 표기 이상: %v", f1["snapshot_latest_replicaset"])
	}
	refs := f1["refs"].([]string)
	if len(refs) != 4 || !strings.HasPrefix(refs[0], "ch:kcm_events_local:") || !strings.HasPrefix(refs[3], "pg:kcm_resources:") {
		t.Fatalf("refs 이상: %v", refs)
	}
	for _, r := range refs {
		found := false
		for _, er := range env.Refs {
			if er == r {
				found = true
			}
		}
		if !found {
			t.Fatalf("finding ref %s가 봉투 refs에 없다", r)
		}
	}
	if !strings.Contains(env.Summary, "롤아웃 1건·레플리카 변경 1건") || !strings.Contains(env.Summary, "env.JAVA_TOOL_OPTIONS") ||
		!strings.Contains(env.Summary, "레플리카 ns/shipping 1→0") {
		t.Fatalf("요약에 K8s 토막이 없다: %s", env.Summary)
	}
	meta := env.Findings[len(env.Findings)-1]
	k8s := meta["k8s_workload"].(map[string]any)
	if k8s["rollouts_total"] != 1 || k8s["replica_changes_total"] != 1 || k8s["kcm_events_note"] == nil {
		t.Fatalf("k8s 메타 이상: %+v", k8s)
	}
	if _, bad := meta["source_errors"]; bad {
		t.Fatalf("정상 조회인데 source_errors: %v", meta["source_errors"])
	}
}

// 클러스터 조회 묶음 요약 — summary_meta 필드 추가와 요약 꼬리만, 기존 finding 불변.
func TestAttachReasonSummary(t *testing.T) {
	out, err := assembleListEvents("c-1", k8sIdentity{branch: "cluster", cluster: "c-1"}, nil, nil, []map[string]any{
		{"namespace": "ns", "object_kind": "Pod", "object_name": "p1", "reason": "Unhealthy", "event_type": "Warning",
			"max_count": float64(3), "first_at": "2026-08-22 04:19:22", "last_at": "2026-08-22 04:19:42", "sev": "WARN", "body": "probe failed"},
	}, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	before := out.(Envelope)
	nFind := len(before.Findings)
	env := attachReasonSummary(before, []map[string]any{
		{"namespace": "ns", "object_kind": "Deployment", "reason": "ScalingReplicaSet", "event_type": "Normal",
			"objects": float64(1), "count_sum": float64(4), "last_at": "2026-08-22 04:24:50.000"},
		{"namespace": "ns", "object_kind": "Pod", "reason": "Unhealthy", "event_type": "Warning",
			"objects": float64(2), "count_sum": float64(27), "last_at": "2026-08-22 04:24:46.000"},
	})
	if len(env.Findings) != nFind {
		t.Fatalf("finding 수가 바뀌었다(필드 추가만 허용): %d → %d", nFind, len(env.Findings))
	}
	meta := env.Findings[len(env.Findings)-1]
	list, ok := meta["k8s_reason_summary"].([]map[string]any)
	if !ok || len(list) != 2 || meta["k8s_reason_summary_total"] != 2 {
		t.Fatalf("묶음 요약 이상: %+v", meta)
	}
	if !strings.Contains(env.Summary, "Normal[ScalingReplicaSet 1]") || !strings.Contains(env.Summary, "Warning[Unhealthy 2]") {
		t.Fatalf("요약 꼬리 이상: %s", env.Summary)
	}
	if same := attachReasonSummary(before, nil); same.Summary != before.Summary {
		t.Fatal("요약 행이 없으면 봉투 불변이어야 한다")
	}
}

// 대상 누락·형식 오류 문구가 클러스터 전역 조회 경로를 알려야 한다.
func TestListEventsArgErrorsMentionCluster(t *testing.T) {
	tool := NewListEventsTool(nil, nil, nil)
	for _, args := range []string{`{"from":"now-1h","to":"now"}`, `{"target":"svc-a","from":"now-1h","to":"now"}`, `not json`} {
		_, err := tool.Call(context.Background(), json.RawMessage(args))
		if err == nil || !strings.Contains(err.Error(), "kubernetes 클러스터") {
			t.Fatalf("%s → 오류 문구에 클러스터 안내 없음: %v", args, err)
		}
	}
	if !strings.Contains(tool.Description, "클러스터 전체") {
		t.Fatal("설명에 클러스터 전역 조회 안내가 없다")
	}
}

// 상한 — 롤아웃·레플리카 변경을 종류별로 자르고 봉투에 밝힌다. 스펙 조회 실패면
// 템플릿 차이는 결손(spec_unavailable)으로 표기한다(지어내지 않는다).
func TestK8sAssembleCapsAndSpecUnavailable(t *testing.T) {
	base := time.Date(2026, 8, 22, 4, 0, 0, 0, time.UTC)
	var rollouts []k8sRollout
	for i := 0; i < k8sChgMaxRollouts+2; i++ {
		rollouts = append(rollouts, k8sRollout{At: base.Add(time.Duration(i) * time.Minute), Cluster: "c", Namespace: "ns",
			Deployment: "d", New: "d-new", Prev: "d-old", PrevBasis: "revision_order"})
	}
	var replicas []*k8sScale
	for i := 0; i < k8sChgMaxReplica+3; i++ {
		replicas = append(replicas, scale(base.Add(time.Duration(i)*time.Second).Format(time.RFC3339), "down", "d-old", 1, 0))
	}
	res := k8sChangeResult{Meta: map[string]any{}, SourceStatus: map[string]string{}, SourceErrors: map[string]string{}}
	inv := lcInventory{byID: map[string]lcTargetMeta{}, byName: map[string]string{}}
	k8sAssemble(context.Background(), nil, &res, rollouts, replicas, nil, inv, "", false, time.Time{})
	if res.Returned != k8sChgMaxRollouts+k8sChgMaxReplica || !res.Truncated || res.Meta["truncation_note"] == nil {
		t.Fatalf("상한 처리 이상: returned=%d truncated=%v meta=%v", res.Returned, res.Truncated, res.Meta)
	}
	if res.Meta["rollouts_total"] != k8sChgMaxRollouts+2 || res.Meta["rollouts_returned"] != k8sChgMaxRollouts {
		t.Fatalf("롤아웃 계수 이상: %v", res.Meta)
	}
	if len(res.SummaryDigest) != 3 {
		t.Fatalf("요약 토막 %d건, want 3", len(res.SummaryDigest))
	}
	for _, f := range res.Findings {
		if f["kind"] == "k8s_rollout" && (f["template_diff_status"] != "spec_unavailable" || f["template_diff"] != nil) {
			t.Fatalf("스펙 조회 실패인데 차이를 실었다: %+v", f)
		}
	}
}

// 요약 첫머리 — 스케일 이벤트 조회 실패는 무변경이 아니라 결손으로 읽혀야 한다.
func TestK8sSummaryLine(t *testing.T) {
	failed := map[string]string{"kcm_events": "조회 실패(http_5xx)"}
	if s := k8sSummaryLine(k8sChangeResult{SourceStatus: failed}); !strings.Contains(s, "결손이지 무변경이 아니다") {
		t.Fatalf("실패·0건 문구 이상: %s", s)
	}
	s := k8sSummaryLine(k8sChangeResult{SourceStatus: failed, Rollouts: 1, SummaryDigest: []string{"롤아웃 ns/d a→b [image]@t"}})
	if !strings.Contains(s, "롤아웃 1건") || !strings.Contains(s, "결손") {
		t.Fatalf("실패·생성 경로 롤아웃 문구 이상: %s", s)
	}
	if s := k8sSummaryLine(k8sChangeResult{SourceStatus: map[string]string{"kcm_events": "조회함"}}); !strings.Contains(s, "0건") {
		t.Fatalf("정상 0건 문구 이상: %s", s)
	}
}
