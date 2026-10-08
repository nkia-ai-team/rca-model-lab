// list_changes의 Kubernetes 워크로드 변경 구획 — 롤아웃(파드 템플릿 구조 비교)과
// 레플리카 변경(스케일 이벤트).
//
// 왜 필요한가(2026-10-06 하네스 절제 실측): 같은 모델이 원시 SQL로는 찾은 근본
// 원인 둘을 도구 표면으로는 못 찾았다 — Deployment 0 스케일 다운(kcm 이벤트
// ScalingReplicaSet)과 롤아웃의 env 변경(kcm_resources ReplicaSet 스펙). 종전
// list_changes는 정책·수집기·감사 이력만 봤고, 롤아웃 전후 무엇이 바뀌었는지
// 보여 줄 도구가 없었다.
//
// 원천 실측(2026-10-06, Polestar v3 캡처):
//   - kcm_resources는 현재 상태 스냅샷이다(전 행 updated_at 동일). yaml 칸은
//     API 객체 JSON이고, ReplicaSet 파드 템플릿은 pod-template-hash별로 불변이라
//     스냅샷 비교가 곧 롤아웃 당시의 차이다. 다만 replicas·주석은 스냅샷 시점 값.
//   - creationTimestamp는 롤아웃 시각이 아니다 — Deployment는 템플릿이 과거
//     리비전과 같으면 옛 ReplicaSet을 재사용한다(8월 12일 생성 RS가 8월 22일
//     롤아웃, revision-history 주석이 재사용을 기록). 그래서 시각은 스케일
//     이벤트가, 이전 RS는 스케일 이벤트 짝 또는 리비전 주석이 정한다.
//   - kcm_events_local.target_id는 승격 리소스면 그 대상 UUID, 아니면 클러스터다.
//     클러스터는 host_target_id에 있다.
//
// 롤아웃 후 자원 사용량(2026-10-06): 템플릿 차이에 resources.limits(memory·cpu)가 있으면
// 새 ReplicaSet pod들의 그 자원 사용량 피크를 VictoriaMetrics(kcm.container.*, read_timeseries와
// 같은 라벨 규약 — target_id=클러스터·namespace·pod·container)에서 계산해 새 한도 대비 수치로만
// 붙인다(절대값·%·피크 시각, 해석 문구 없음). VM은 optional 원천이다.
//
// 판정은 하지 않는다: 스케일 메시지는 쿠버네티스 컨트롤러의 고정 템플릿만 파싱하고,
// 템플릿 차이는 노출 허용 필드의 구조 비교뿐이다. 라벨·주석은 절대 노출하지 않는다
// (주석은 리비전 번호를 내부 계산에만 쓴다). valueFrom은 참조 종류·이름만 싣는다.
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	k8sChgMaxRollouts = 6                // 반환 롤아웃 상한(항목당 ~2KB — 응답 앞부분만 보는 소비자 고려)
	k8sChgMaxReplica  = 10               // 반환 레플리카 변경 상한
	k8sChgMaxDiff     = 12               // 롤아웃당 템플릿 차이 항목 상한
	k8sChgValueMax    = 160              // 값 문자열 절단 길이(rune)
	k8sChgRolloutSpan = 10 * time.Minute // 한 롤아웃의 스케일 단계로 묶는 범위 = progressDeadlineSeconds 기본값
	k8sChgEventLimit  = 2000             // 창 내 스케일 이벤트 조회 상한
	k8sChgUsageSpan   = 2 * time.Hour    // 롤아웃 후 사용량 관측 구간 상한
	k8sChgMaxUsageObs = 4                // 호출당 사용량 관측(한도 차이 1건 = VM 조회 3회) 상한
	k8sUsagePoints    = 60               // 사용량 range 조회의 점 수 상한(step = 구간/60, 최소 60s)
	k8sUsageMaxInst   = 3                // 관측당 컨테이너 인스턴스 표시 상한(처음 관측 순)
)

// k8sUsageMetrics는 한도 자원 → (사용량 지표, 한도 지표, 단위)다. capacity.go의 검증된 짝과 같은 지표·단위.
var k8sUsageMetrics = map[string][3]string{
	"memory": {"kcm.container.mem_usage", "kcm.container.mem_limit", "B"},
	"cpu":    {"kcm.container.cpu_usage", "kcm.container.cpu_limit", "mCPU"},
}

// 쿠버네티스 컨트롤러가 내는 고정 메시지 템플릿(의미 판단이 아니라 서식 파싱).
//   - deployment-controller ScalingReplicaSet: "Scaled <up|down> replica set <rs> to <n> from <m>".
//     " from <m>"이 없는 서식은 새 RS를 만들며 올릴 때 나온다(실측 f05-h: 이벤트 시각 =
//     그 RS의 creationTimestamp) — 구버전 컨트롤러는 모든 스케일에서 from을 안 적는다.
//     그래서 from 미기록은 RS 생성 시각이 맞을 때만 0으로 본다(k8sNormalizeNewRS).
//   - horizontal-pod-autoscaler SuccessfulRescale: "New size: <n>; reason: <사유>"
var (
	k8sScaleRe   = regexp.MustCompile(`^Scaled (up|down) replica set (\S+) to (\d+)(?: from (\d+))?$`)
	k8sRescaleRe = regexp.MustCompile(`^New size: (\d+); reason: (.+)$`)
)

// k8sScale는 스케일 이벤트 하나(파싱 후)다.
type k8sScale struct {
	At        time.Time
	Cluster   string
	ObjTarget string // kcm 이벤트 target_id — 승격 리소스면 그 대상, 아니면 클러스터
	Namespace string
	Kind      string // Deployment | HorizontalPodAutoscaler
	Name      string
	Reason    string
	Dir       string // up | down | rescale
	RS        string
	From      *int
	To        int
	HPAReason string
	NewRS     bool // "to N"(이전 개수 없음) 서식 + RS 생성 시각 일치 = 새 RS 생성 시점의 업(0→N)
	consumed  bool
}

// parseScaleMessage는 고정 템플릿만 푼다. 모르는 서식은 ok=false — 호출부가
// 미해석 건수로 정직하게 센다(조용한 누락 금지).
func parseScaleMessage(reason, body string) (dir, rs string, from *int, to int, hpaReason string, ok bool) {
	body = strings.TrimSpace(body)
	switch reason {
	case "ScalingReplicaSet":
		m := k8sScaleRe.FindStringSubmatch(body)
		if m == nil {
			return "", "", nil, 0, "", false
		}
		to, _ = strconv.Atoi(m[3])
		if m[4] != "" {
			f, _ := strconv.Atoi(m[4])
			from = &f
		}
		return m[1], m[2], from, to, "", true
	case "SuccessfulRescale":
		m := k8sRescaleRe.FindStringSubmatch(body)
		if m == nil {
			return "", "", nil, 0, "", false
		}
		to, _ = strconv.Atoi(m[1])
		return "rescale", "", nil, to, m[2], true
	}
	return "", "", nil, 0, "", false
}

// ── ReplicaSet 스펙(파드 템플릿) ──────────────────────────────────────

type k8sEnvVar struct {
	Name      string                     `json:"name"`
	Value     *string                    `json:"value"`
	ValueFrom map[string]json.RawMessage `json:"valueFrom"`
}

type k8sNamedRef struct {
	Name string `json:"name"`
}

type k8sEnvFrom struct {
	Prefix       string       `json:"prefix"`
	ConfigMapRef *k8sNamedRef `json:"configMapRef"`
	SecretRef    *k8sNamedRef `json:"secretRef"`
}

type k8sPort struct {
	Name          string `json:"name"`
	ContainerPort int    `json:"containerPort"`
	Protocol      string `json:"protocol"`
}

type k8sProbe struct {
	HTTPGet *struct {
		Path   string          `json:"path"`
		Port   json.RawMessage `json:"port"`
		Scheme string          `json:"scheme"`
	} `json:"httpGet"`
	TCPSocket *struct {
		Port json.RawMessage `json:"port"`
	} `json:"tcpSocket"`
	Exec *struct {
		Command []string `json:"command"`
	} `json:"exec"`
	GRPC *struct {
		Port    int     `json:"port"`
		Service *string `json:"service"`
	} `json:"grpc"`
	InitialDelaySeconds           *int   `json:"initialDelaySeconds"`
	PeriodSeconds                 *int   `json:"periodSeconds"`
	TimeoutSeconds                *int   `json:"timeoutSeconds"`
	SuccessThreshold              *int   `json:"successThreshold"`
	FailureThreshold              *int   `json:"failureThreshold"`
	TerminationGracePeriodSeconds *int64 `json:"terminationGracePeriodSeconds"`
}

type k8sContainerSpec struct {
	Name      string       `json:"name"`
	Image     string       `json:"image"`
	Command   []string     `json:"command"`
	Args      []string     `json:"args"`
	Env       []k8sEnvVar  `json:"env"`
	EnvFrom   []k8sEnvFrom `json:"envFrom"`
	Resources struct {
		Limits   map[string]json.RawMessage `json:"limits"`
		Requests map[string]json.RawMessage `json:"requests"`
	} `json:"resources"`
	Ports          []k8sPort `json:"ports"`
	LivenessProbe  *k8sProbe `json:"livenessProbe"`
	ReadinessProbe *k8sProbe `json:"readinessProbe"`
	StartupProbe   *k8sProbe `json:"startupProbe"`
}

type k8sPodSpec struct {
	Containers     []k8sContainerSpec `json:"containers"`
	InitContainers []k8sContainerSpec `json:"initContainers"`
}

// k8sRS는 ReplicaSet 한 개의 비교 재료다. 주석에서는 리비전 번호만 꺼낸다.
type k8sRS struct {
	Cluster   string
	Namespace string
	Name      string
	OwnerKind string
	Owner     string
	Created   time.Time
	Revision  int   // deployment.kubernetes.io/revision (0 = 없음)
	History   []int // deployment.kubernetes.io/revision-history
	Pod       k8sPodSpec
	ParseErr  string // 비어 있지 않으면 템플릿 비교 불가 사유
}

// parseRSSpec은 kcm_resources.yaml(실측상 API 객체 JSON)을 푼다. JSON이 아니면
// 비교 불가로 표시한다 — YAML 파서는 의존성에 없고, 실측 248행 전부 JSON이었다.
func parseRSSpec(raw string) (k8sPodSpec, int, []int, string) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return k8sPodSpec{}, 0, nil, "스펙 칸 비어 있음"
	}
	if !strings.HasPrefix(raw, "{") {
		return k8sPodSpec{}, 0, nil, "스펙 칸이 JSON 아님(비교 미지원 서식)"
	}
	var doc struct {
		Metadata struct {
			Annotations map[string]string `json:"annotations"`
		} `json:"metadata"`
		Spec struct {
			Template struct {
				Spec k8sPodSpec `json:"spec"`
			} `json:"template"`
		} `json:"spec"`
	}
	if err := json.Unmarshal([]byte(raw), &doc); err != nil {
		return k8sPodSpec{}, 0, nil, "스펙 해석 실패"
	}
	rev, _ := strconv.Atoi(doc.Metadata.Annotations["deployment.kubernetes.io/revision"])
	var hist []int
	for _, s := range strings.Split(doc.Metadata.Annotations["deployment.kubernetes.io/revision-history"], ",") {
		if n, err := strconv.Atoi(strings.TrimSpace(s)); err == nil && n > 0 {
			hist = append(hist, n)
		}
	}
	return doc.Spec.Template.Spec, rev, hist, ""
}

// k8sPrevByRevision은 x의 현재 리비전 직전 리비전을 가진 형제 ReplicaSet이다.
// 직전 리비전을 x 자신의 이력이 갖고 있으면(그 사이 RS가 삭제돼 없다) nil —
// 지어내지 않는다. x의 리비전 주석은 스냅샷 시점 값이라 x가 창 뒤에 다시
// 활성화됐다면 그 마지막 활성화 기준이다(호출부가 basis로 밝힌다).
func k8sPrevByRevision(x *k8sRS, siblings []*k8sRS) *k8sRS {
	if x == nil || x.Revision <= 0 {
		return nil
	}
	bestOther, bestSelf := 0, 0
	var owner *k8sRS
	for _, h := range x.History {
		if h < x.Revision && h > bestSelf {
			bestSelf = h
		}
	}
	for _, s := range siblings {
		if s == nil || s.Name == x.Name {
			continue
		}
		for _, r := range append([]int{s.Revision}, s.History...) {
			if r > 0 && r < x.Revision && r > bestOther {
				bestOther, owner = r, s
			}
		}
	}
	if owner == nil || bestSelf > bestOther {
		return nil
	}
	return owner
}

// k8sLatestRevision은 형제 중 현재 리비전 주석이 가장 큰 RS(스냅샷 시점 Deployment의 현행 템플릿)다.
func k8sLatestRevision(sibs []*k8sRS) *k8sRS {
	var best *k8sRS
	for _, s := range sibs {
		if s != nil && s.Revision > 0 && (best == nil || s.Revision > best.Revision) {
			best = s
		}
	}
	return best
}

// ── 템플릿 구조 비교 ──────────────────────────────────────────────────

// k8sDiff는 노출 허용 필드 하나의 차이다(before=이전 RS, after=새 RS).
type k8sDiff struct {
	Container string  `json:"container"`
	Field     string  `json:"field"`
	Change    string  `json:"change"` // added | removed | modified
	Before    *string `json:"before,omitempty"`
	After     *string `json:"after,omitempty"`
}

// k8sClip은 긴 값을 자른다 — 잘렸으면 true.
func k8sClip(s string) (string, bool) {
	r := []rune(s)
	if len(r) <= k8sChgValueMax {
		return s, false
	}
	return string(r[:k8sChgValueMax]) + fmt.Sprintf("…(전체 %d자 중 앞 %d자)", len(r), k8sChgValueMax), true
}

func k8sRaw(v json.RawMessage) string {
	var s string
	if json.Unmarshal(v, &s) == nil {
		return s
	}
	return strings.TrimSpace(string(v))
}

// k8sEnvValue는 env 값 표기다. valueFrom은 참조 종류와 이름(또는 fieldPath·
// resource)만 — secretKeyRef의 key나 값은 절대 싣지 않는다.
func k8sEnvValue(e k8sEnvVar) string {
	if e.Value != nil {
		return *e.Value
	}
	if len(e.ValueFrom) == 0 {
		return ""
	}
	kinds := make([]string, 0, len(e.ValueFrom))
	for k := range e.ValueFrom {
		kinds = append(kinds, k)
	}
	sort.Strings(kinds)
	parts := make([]string, 0, len(kinds))
	for _, k := range kinds {
		var ref struct {
			Name      string `json:"name"`
			FieldPath string `json:"fieldPath"`
			Resource  string `json:"resource"`
		}
		_ = json.Unmarshal(e.ValueFrom[k], &ref)
		switch {
		case ref.Name != "":
			parts = append(parts, fmt.Sprintf("%s(name=%s)", k, ref.Name))
		case ref.FieldPath != "":
			parts = append(parts, fmt.Sprintf("%s(fieldPath=%s)", k, ref.FieldPath))
		case ref.Resource != "":
			parts = append(parts, fmt.Sprintf("%s(resource=%s)", k, ref.Resource))
		default:
			parts = append(parts, k)
		}
	}
	return "valueFrom " + strings.Join(parts, ",")
}

func k8sEnvFromValue(list []k8sEnvFrom) string {
	parts := make([]string, 0, len(list))
	for _, e := range list {
		p := ""
		switch {
		case e.ConfigMapRef != nil:
			p = "configMapRef(name=" + e.ConfigMapRef.Name + ")"
		case e.SecretRef != nil:
			p = "secretRef(name=" + e.SecretRef.Name + ")"
		default:
			continue
		}
		if e.Prefix != "" {
			p += " prefix=" + e.Prefix
		}
		parts = append(parts, p)
	}
	return strings.Join(parts, ", ")
}

func k8sPortsValue(ps []k8sPort) string {
	parts := make([]string, 0, len(ps))
	for _, p := range ps {
		s := fmt.Sprintf("%d/%s", p.ContainerPort, p.Protocol)
		if p.Name != "" {
			s += "(" + p.Name + ")"
		}
		parts = append(parts, s)
	}
	return strings.Join(parts, ", ")
}

// k8sProbeFields는 프로브의 노출 허용 필드를 고정 순서로 편다. httpGet의
// 헤더는 싣지 않는다(인증 토큰이 들어갈 수 있는 칸).
func k8sProbeFields(p *k8sProbe) [][2]string {
	if p == nil {
		return nil
	}
	var out [][2]string
	add := func(k, v string) { out = append(out, [2]string{k, v}) }
	if p.HTTPGet != nil {
		add("httpGet.path", p.HTTPGet.Path)
		add("httpGet.port", k8sRaw(p.HTTPGet.Port))
		if p.HTTPGet.Scheme != "" {
			add("httpGet.scheme", p.HTTPGet.Scheme)
		}
	}
	if p.TCPSocket != nil {
		add("tcpSocket.port", k8sRaw(p.TCPSocket.Port))
	}
	if p.Exec != nil {
		add("exec.command", strings.Join(p.Exec.Command, " "))
	}
	if p.GRPC != nil {
		add("grpc.port", strconv.Itoa(p.GRPC.Port))
		if p.GRPC.Service != nil {
			add("grpc.service", *p.GRPC.Service)
		}
	}
	ints := []struct {
		k string
		v *int
	}{
		{"initialDelaySeconds", p.InitialDelaySeconds}, {"periodSeconds", p.PeriodSeconds},
		{"timeoutSeconds", p.TimeoutSeconds}, {"successThreshold", p.SuccessThreshold},
		{"failureThreshold", p.FailureThreshold},
	}
	for _, f := range ints {
		if f.v != nil {
			add(f.k, strconv.Itoa(*f.v))
		}
	}
	if p.TerminationGracePeriodSeconds != nil {
		add("terminationGracePeriodSeconds", strconv.FormatInt(*p.TerminationGracePeriodSeconds, 10))
	}
	return out
}

func k8sProbeSummary(p *k8sProbe) string {
	fs := k8sProbeFields(p)
	parts := make([]string, 0, len(fs))
	for _, f := range fs {
		parts = append(parts, f[0]+"="+f[1])
	}
	return strings.Join(parts, " ")
}

// k8sDiffer는 차이 항목을 모으며 절단 건수를 센다.
type k8sDiffer struct {
	out     []k8sDiff
	clipped int
}

func (d *k8sDiffer) add(container, field, change string, before, after *string) {
	clip := func(p *string) *string {
		if p == nil {
			return nil
		}
		s, cut := k8sClip(*p)
		if cut {
			d.clipped++
		}
		return &s
	}
	d.out = append(d.out, k8sDiff{Container: container, Field: field, Change: change, Before: clip(before), After: clip(after)})
}

// cmp는 존재 여부까지 보는 비교다(hasA·hasB가 다르면 추가/제거).
func (d *k8sDiffer) cmp(container, field string, a string, hasA bool, b string, hasB bool) {
	switch {
	case hasA && hasB && a != b:
		d.add(container, field, "modified", &a, &b)
	case hasA && !hasB:
		d.add(container, field, "removed", &a, nil)
	case !hasA && hasB:
		d.add(container, field, "added", nil, &b)
	}
}

// diffPodSpecs는 이전 → 새 파드 템플릿의 노출 허용 필드 구조 비교다.
// 반환 순서는 결정론적이다(새 템플릿의 컨테이너·env 순서, 그다음 제거분).
func diffPodSpecs(prev, next k8sPodSpec) ([]k8sDiff, int) {
	d := &k8sDiffer{}
	diffContainerLists(d, "init:", prev.InitContainers, next.InitContainers)
	diffContainerLists(d, "", prev.Containers, next.Containers)
	return d.out, d.clipped
}

// diffContainerLists는 컨테이너를 이름으로 짝짓는다. init 컨테이너는 "init:" 접두로 구별한다.
func diffContainerLists(d *k8sDiffer, prefix string, prev, next []k8sContainerSpec) {
	pm := map[string]k8sContainerSpec{}
	for _, c := range prev {
		pm[c.Name] = c
	}
	nm := map[string]bool{}
	for _, c := range next {
		nm[c.Name] = true
		p, ok := pm[c.Name]
		if !ok {
			img := "image=" + c.Image
			d.add(prefix+c.Name, "container", "added", nil, &img)
			continue
		}
		diffContainer(d, prefix+c.Name, p, c)
	}
	for _, c := range prev {
		if !nm[c.Name] {
			img := "image=" + c.Image
			d.add(prefix+c.Name, "container", "removed", &img, nil)
		}
	}
}

func diffContainer(d *k8sDiffer, n string, a, b k8sContainerSpec) {
	d.cmp(n, "image", a.Image, true, b.Image, true)
	d.cmp(n, "command", strings.Join(a.Command, " "), len(a.Command) > 0, strings.Join(b.Command, " "), len(b.Command) > 0)
	d.cmp(n, "args", strings.Join(a.Args, " "), len(a.Args) > 0, strings.Join(b.Args, " "), len(b.Args) > 0)

	// env — 이름 단위.
	av := map[string]string{}
	for _, e := range a.Env {
		av[e.Name] = k8sEnvValue(e)
	}
	seen := map[string]bool{}
	for _, e := range b.Env {
		seen[e.Name] = true
		old, had := av[e.Name]
		d.cmp(n, "env."+e.Name, old, had, k8sEnvValue(e), true)
	}
	for _, e := range a.Env {
		if !seen[e.Name] {
			d.cmp(n, "env."+e.Name, av[e.Name], true, "", false)
		}
	}
	d.cmp(n, "envFrom", k8sEnvFromValue(a.EnvFrom), len(a.EnvFrom) > 0, k8sEnvFromValue(b.EnvFrom), len(b.EnvFrom) > 0)

	// resources — requests·limits 자원별.
	for _, side := range []struct {
		name string
		a, b map[string]json.RawMessage
	}{{"requests", a.Resources.Requests, b.Resources.Requests}, {"limits", a.Resources.Limits, b.Resources.Limits}} {
		keys := map[string]bool{}
		for k := range side.a {
			keys[k] = true
		}
		for k := range side.b {
			keys[k] = true
		}
		ks := make([]string, 0, len(keys))
		for k := range keys {
			ks = append(ks, k)
		}
		sort.Strings(ks)
		for _, k := range ks {
			va, ha := side.a[k]
			vb, hb := side.b[k]
			d.cmp(n, "resources."+side.name+"."+k, k8sRaw(va), ha, k8sRaw(vb), hb)
		}
	}

	// probes — 한쪽만 있으면 요약 1건, 둘 다 있으면 필드별.
	for _, pr := range []struct {
		name string
		a, b *k8sProbe
	}{{"livenessProbe", a.LivenessProbe, b.LivenessProbe},
		{"readinessProbe", a.ReadinessProbe, b.ReadinessProbe},
		{"startupProbe", a.StartupProbe, b.StartupProbe}} {
		switch {
		case pr.a == nil && pr.b == nil:
		case pr.a == nil || pr.b == nil:
			d.cmp(n, pr.name, k8sProbeSummary(pr.a), pr.a != nil, k8sProbeSummary(pr.b), pr.b != nil)
		default:
			fa := map[string]string{}
			for _, f := range k8sProbeFields(pr.a) {
				fa[f[0]] = f[1]
			}
			seenF := map[string]bool{}
			for _, f := range k8sProbeFields(pr.b) {
				seenF[f[0]] = true
				old, had := fa[f[0]]
				d.cmp(n, pr.name+"."+f[0], old, had, f[1], true)
			}
			for _, f := range k8sProbeFields(pr.a) {
				if !seenF[f[0]] {
					d.cmp(n, pr.name+"."+f[0], f[1], true, "", false)
				}
			}
		}
	}
	d.cmp(n, "ports", k8sPortsValue(a.Ports), len(a.Ports) > 0, k8sPortsValue(b.Ports), len(b.Ports) > 0)
}

// ── 롤아웃 판별 ──────────────────────────────────────────────────────

// k8sRollout은 Deployment 한 번의 파드 템플릿 전환이다.
type k8sRollout struct {
	At         time.Time
	Cluster    string
	Namespace  string
	Deployment string
	DeployTgt  string // kcm 이벤트의 승격 대상(없으면 "")
	New        string
	Prev       string
	PrevBasis  string // revision_order[+scale_event] | scale_event | revision_annotation | rs_created+revision_annotation
	Notes      []string
	Steps      []*k8sScale
}

func k8sAbs(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}

// k8sRevisionOwners는 Deployment 형제 RS의 리비전 → RS 이름 표와 내림차순 리비전 목록이다.
// 리비전은 롤아웃마다 max+1로 매겨지고, 옛 RS를 되살리면 그 RS의 옛 번호는
// revision-history로 옮겨진다(deployment-controller) — 그래서 현재+이력의 합집합이
// 그 Deployment 템플릿 전환의 순서 원장이다(실측 f05-r: 216→217→218→219→220).
func k8sRevisionOwners(sibs []*k8sRS) (map[int]string, []int) {
	owner := map[int]string{}
	var revs []int
	for _, s := range sibs {
		if s == nil {
			continue
		}
		for _, r := range append([]int{s.Revision}, s.History...) {
			if _, dup := owner[r]; r > 0 && !dup {
				owner[r] = s.Name
				revs = append(revs, r)
			}
		}
	}
	sort.Sort(sort.Reverse(sort.IntSlice(revs)))
	return owner, revs
}

// k8sAlignRevisions는 롤아웃 후보(시간순, 이벤트 원천 끝까지)를 끝에서부터 리비전
// 내림차순에 맞춘다 — 마지막 활성화 = 최대 리비전. 후보의 RS와 그 리비전 소유 RS가
// 다르면 거기서 멈춘다(그 앞은 맞춤 불가 — 이벤트 결손·스냅샷 이후 롤아웃 등).
// 반환: 후보 위치 → 리비전.
func k8sAlignRevisions(cands []*k8sScale, owner map[int]string, revs []int) map[int]int {
	out := map[int]int{}
	ri := 0
	for ci := len(cands) - 1; ci >= 0 && ri < len(revs); ci-- {
		if owner[revs[ri]] != cands[ci].RS {
			break
		}
		out[ci] = revs[ri]
		ri++
	}
	return out
}

// k8sDetectRollouts는 Deployment 하나의 스케일 이벤트(시간순, 창 시작부터 원천 끝까지)에서
// 롤아웃을 고른다. 규칙(결정론):
//  1. 후보 = 어떤 RS X가 0에서 스케일 업(새 RS 생성 시점 포함 — k8sNormalizeNewRS).
//     그 시점에 다른 RS가 활성이 아니고 X가 0으로 내려간 기록이 있으면 재개(레플리카
//     변경)라 후보가 아니다(k8sIsResume).
//  2. 리비전 맞춤(k8sAlignRevisions)이 된 후보의 이전 RS = 리비전 r-1의 소유 RS.
//     겹친 롤아웃(앞 롤아웃이 끝나기 전 다음 템플릿 적용)에서도 맞는 유일한 규칙이다.
//  3. 맞춤이 안 되면 이벤트 짝 = 후보 사이 구간·±span 안에서 X가 아닌 RS의 스케일
//     다운 중 시각이 가장 가까운 것.
//  4. 그것도 없으면 X의 현재 리비전 주석 직전(k8sPrevByRevision) — 이전 개수가
//     기록된 0→N 업에서만(구버전 서식의 1→3 업을 롤아웃으로 오인하지 않게).
//  5. 아무것도 없으면 롤아웃이 아니라 레플리카 변경으로 남긴다.
//
// 롤아웃에 묶인 단계(X 업·이전 RS 다운, 구간 안 ±span)는 consumed로 표시된다.
// 창 필터는 호출부 몫이다(맞춤에는 창 뒤 활성화까지 필요하다).
func k8sDetectRollouts(events []*k8sScale, rsByName map[string]*k8sRS, siblings []*k8sRS) []k8sRollout {
	var ups []int // 0에서의 업 전부(구간 경계)
	for i, e := range events {
		if e.Dir == "up" && (e.From == nil || *e.From == 0) {
			ups = append(ups, i)
		}
	}
	type cand struct{ idx, lo, hi int }
	var cands []cand
	for k, idx := range ups {
		lo, hi := 0, len(events)
		if k > 0 {
			lo = ups[k-1] + 1
		}
		if k+1 < len(ups) {
			hi = ups[k+1]
		}
		if k8sIsResume(events[:idx], events[idx].RS) {
			continue // 재개 — 레플리카 변경으로 남는다.
		}
		cands = append(cands, cand{idx, lo, hi})
	}
	// 리비전 맞춤 — 이전 개수가 확정된 후보만 원장에 올린다.
	owner, revs := k8sRevisionOwners(siblings)
	var known []*k8sScale
	knownPos := map[int]int{} // cands 위치 → known 위치
	for i, c := range cands {
		if e := events[c.idx]; e.From != nil {
			knownPos[i] = len(known)
			known = append(known, e)
		}
	}
	aligned := k8sAlignRevisions(known, owner, revs)

	var out []k8sRollout
	for i, cd := range cands {
		c := events[cd.idx]
		var evPrev *k8sScale
		for j := cd.lo; j < cd.hi; j++ {
			e := events[j]
			if e.Dir != "down" || e.RS == c.RS || k8sAbs(e.At.Sub(c.At)) > k8sChgRolloutSpan {
				continue
			}
			if evPrev == nil || k8sAbs(e.At.Sub(c.At)) < k8sAbs(evPrev.At.Sub(c.At)) {
				evPrev = e
			}
		}
		revPrev, isAligned := "", false
		if kp, ok := knownPos[i]; ok {
			if r, ok := aligned[kp]; ok {
				isAligned = true
				if o, ok := owner[r-1]; ok && o != c.RS {
					revPrev = o
				}
			}
		}
		r := k8sRollout{At: c.At, Cluster: c.Cluster, Namespace: c.Namespace, Deployment: c.Name, New: c.RS}
		if c.ObjTarget != "" && c.ObjTarget != c.Cluster {
			r.DeployTgt = c.ObjTarget
		}
		x := rsByName[c.RS]
		reusedNote := func() {
			if x != nil && !x.Created.IsZero() && c.At.Sub(x.Created) > k8sChgRolloutSpan {
				r.Notes = append(r.Notes, "새 RS는 재사용 RS(생성 "+x.Created.Format(time.RFC3339)+
					") — 다른 RS 다운 이벤트가 없어 되돌림 롤아웃과 같은 RS의 0→N 재개를 확정 구분하지 못한다")
			}
		}
		switch {
		case revPrev != "":
			r.Prev, r.PrevBasis = revPrev, "revision_order"
			switch {
			case evPrev != nil && evPrev.RS == revPrev:
				r.PrevBasis = "revision_order+scale_event"
			case evPrev != nil:
				r.Notes = append(r.Notes, "가장 가까운 다른 RS 다운("+evPrev.RS+")은 겹친 앞 롤아웃의 마무리 — 리비전 순서상 직전 템플릿은 "+revPrev)
			default:
				r.Notes = append(r.Notes, "이전 RS의 스케일 다운 이벤트가 없다(새 RS 미준비로 옛 RS가 남았거나 창·원천 밖) — 이전 RS는 리비전 순서 기준")
				reusedNote()
			}
		case evPrev != nil:
			r.Prev, r.PrevBasis = evPrev.RS, "scale_event"
			if !isAligned && x != nil {
				if g := k8sPrevByRevision(x, siblings); g != nil && g.Name != evPrev.RS {
					r.Notes = append(r.Notes, "리비전 주석상 직전 RS는 "+g.Name+
						" — 리비전 순서 맞춤이 안 돼(이벤트 결손·스냅샷 이후 롤아웃 가능) 창 안 이벤트 짝을 택했다")
				}
			}
		case !isAligned && c.From != nil && x != nil && k8sPrevByRevision(x, siblings) != nil:
			r.Prev, r.PrevBasis = k8sPrevByRevision(x, siblings).Name, "revision_annotation"
			r.Notes = append(r.Notes, "이전 RS의 스케일 다운 이벤트가 없고 리비전 순서 맞춤도 안 됐다 — 이전 RS는 새 RS 현재 리비전 주석의 직전(마지막 활성화 기준)")
			reusedNote()
		default:
			continue
		}
		for j := cd.lo; j < cd.hi; j++ {
			e := events[j]
			if k8sAbs(e.At.Sub(c.At)) > k8sChgRolloutSpan {
				continue
			}
			if (e.RS == c.RS && e.Dir == "up") || (e.RS == r.Prev && e.Dir == "down") {
				e.consumed = true
				r.Steps = append(r.Steps, e)
			}
		}
		out = append(out, r)
	}
	return out
}

// k8sNormalizeNewRS는 이전 개수 미기록 업 중 RS 생성 시각이 이벤트와 ±span 안인 것을
// 0→N(새 RS 생성)으로 확정한다. 생성 시각이 다르면(재사용 RS·구버전 서식) 미상 그대로.
func k8sNormalizeNewRS(scales []*k8sScale, rsByDeploy map[string][]*k8sRS) {
	for _, s := range scales {
		if s.Dir != "up" || s.From != nil || s.RS == "" {
			continue
		}
		for _, x := range rsByDeploy[k8sKey(s.Cluster, s.Namespace, s.Name)] {
			if x.Name == s.RS && !x.Created.IsZero() && k8sAbs(s.At.Sub(x.Created)) <= k8sChgRolloutSpan {
				zero := 0
				s.From, s.NewRS = &zero, true
			}
		}
	}
}

// k8sIsResume은 X의 0에서의 업이 같은 RS의 재개인지다. 창 안 기록으로 RS별 마지막
// 개수를 따라가, X 말고 활성(마지막 개수 > 0)인 RS가 하나도 없고 X가 0으로 내려간
// 기록이 있을 때만 재개다. 다른 RS가 활성이면 되돌림·새 템플릿 롤아웃이다(f09: 78bf
// 활성 중 5c4d 업 = 되돌림). 기록이 없어 모르면 재개라 하지 않는다(뒤 규칙이 정한다).
func k8sIsResume(before []*k8sScale, rs string) bool {
	last := map[string]int{}
	for _, e := range before {
		if e.RS != "" && (e.Dir == "up" || e.Dir == "down") {
			last[e.RS] = e.To
		}
	}
	for name, n := range last {
		if name != rs && n > 0 {
			return false
		}
	}
	n, seen := last[rs]
	return seen && n == 0
}

// ── 조회·조립 ────────────────────────────────────────────────────────

// k8sChangeResult는 list_changes에 합류할 구획 결과다.
type k8sChangeResult struct {
	Findings      []Finding
	Refs          []string
	Meta          map[string]any
	SourceStatus  map[string]string
	SourceErrors  map[string]string
	Rollouts      int
	Replica       int
	Returned      int
	Truncated     bool
	HintHits      int
	SummaryDigest []string
	CHTruncated   bool
}

func k8sKey(cluster, ns, name string) string { return cluster + "|" + ns + "|" + name }

// k8sWorkloadChanges는 창 전역 K8s 워크로드 변경을 모은다. 실패는 구획 안에서
// 결손으로 표기한다(list_changes 본체 관측은 유지 — CH는 optional 원천).
func k8sWorkloadChanges(ctx context.Context, ch *CH, pg *sql.DB, vm *VM, inv lcInventory,
	from, to time.Time, hint string) k8sChangeResult {
	res := k8sChangeResult{SourceStatus: map[string]string{}, SourceErrors: map[string]string{}, Meta: map[string]any{}}
	scales := k8sLoadScales(ctx, ch, from, &res)
	created, rsByDeploy, pgOK := k8sLoadReplicaSets(ctx, pg, scales, from, to, &res)
	k8sNormalizeNewRS(scales, rsByDeploy)
	rollouts := k8sCollectRollouts(scales, created, rsByDeploy, from, to)

	// 레플리카 변경 = 창 안의, 롤아웃에 묶이지 않은 스케일 이벤트.
	var replicas []*k8sScale
	inWindow := 0
	for _, s := range scales {
		if s.At.Before(from) || !s.At.Before(to) {
			continue
		}
		inWindow++
		if !s.consumed {
			replicas = append(replicas, s)
		}
	}
	res.Meta["scale_events_seen"] = inWindow
	if len(scales) > inWindow {
		res.Meta["scale_events_after_window"] = len(scales) - inWindow // 리비전 맞춤에만 쓰고 보고하지 않는다
	}
	k8sAssemble(ctx, vm, &res, rollouts, replicas, rsByDeploy, inv, hint, pgOK, to)
	return res
}

// k8sLoadScales는 창 시작 이후의 스케일 이벤트를 읽는다(CH). 상한 없음: 리비전
// 맞춤(k8sAlignRevisions)이 창 뒤 활성화까지 필요하다 — 보고는 호출부가 창으로 거른다.
func k8sLoadScales(ctx context.Context, ch *CH, from time.Time, res *k8sChangeResult) []*k8sScale {
	if ch == nil {
		res.SourceStatus["kcm_events"] = "CH 미연결 — 스케일 이벤트 미조회"
		return nil
	}
	rows, tr, err := ch.Query(ctx, `
		SELECT toString(timestamp) AS at,
		       if(host_target_id != '', host_target_id, target_id) AS cluster,
		       target_id AS obj_target, namespace, object_kind, object_name, reason, body
		FROM kcm_events_local
		WHERE reason IN ('ScalingReplicaSet', 'SuccessfulRescale')
		  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
		GROUP BY at, cluster, obj_target, namespace, object_kind, object_name, reason, body
		ORDER BY at, object_name, body
		LIMIT `+strconv.Itoa(k8sChgEventLimit),
		map[string]string{"from": chTime(from)})
	if err != nil {
		tok := beDetail(err)
		res.SourceStatus["kcm_events"] = "조회 실패(" + tok + ")"
		res.SourceErrors["ch"] = tok
		return nil
	}
	res.SourceStatus["kcm_events"] = "조회함"
	res.CHTruncated = tr != nil || len(rows) >= k8sChgEventLimit
	var scales []*k8sScale
	unparsed := 0
	for _, r := range rows {
		at, ok := chParseTS(r["at"])
		if !ok {
			continue
		}
		dir, rs, fromN, toN, hpaReason, ok := parseScaleMessage(chStr(r["reason"]), chStr(r["body"]))
		if !ok {
			unparsed++
			continue
		}
		scales = append(scales, &k8sScale{
			At: at.UTC(), Cluster: chStr(r["cluster"]), ObjTarget: chStr(r["obj_target"]),
			Namespace: chStr(r["namespace"]), Kind: chStr(r["object_kind"]), Name: chStr(r["object_name"]),
			Reason: chStr(r["reason"]), Dir: dir, RS: rs, From: fromN, To: toN, HPAReason: hpaReason,
		})
	}
	if unparsed > 0 {
		res.Meta["scale_events_unparsed"] = unparsed
		res.Meta["unparsed_note"] = "고정 템플릿 밖 스케일 메시지 — 건수만 센다(list_events로 원문 확인)"
	}
	// 같은 초의 단계는 CH 순서에 기대지 않고 결정론으로 — 업이 다운보다 먼저
	// (롤링 업데이트의 실제 순서), 그다음 이름.
	sort.SliceStable(scales, func(i, j int) bool {
		a, b := scales[i], scales[j]
		if !a.At.Equal(b.At) {
			return a.At.Before(b.At)
		}
		if a.Dir != b.Dir {
			return a.Dir == "up"
		}
		return a.RS < b.RS
	})
	// 원천 보유 구간 — 창 앞부분이 원천 밖이면 "이벤트 없음"이 아니라 "못 봄"이다.
	cov, _, err := ch.Query(ctx, `SELECT toString(min(timestamp)) AS lo, toString(max(timestamp)) AS hi, count() AS n FROM kcm_events_local`, nil)
	if err != nil || len(cov) != 1 || asInt(cov[0]["n"]) == 0 {
		return scales
	}
	lo, lok := chParseTS(cov[0]["lo"])
	hi, hok := chParseTS(cov[0]["hi"])
	if lok && hok {
		res.Meta["kcm_events_held"] = map[string]any{"from": lo.UTC().Format(time.RFC3339), "to": hi.UTC().Format(time.RFC3339)}
		if lo.After(from) {
			res.Meta["kcm_events_note"] = fmt.Sprintf("kcm 이벤트 원천은 %s 이후만 보유 — 창 앞부분(%s~)의 스케일은 못 봤다(무변경 근거 아님)",
				lo.UTC().Format(time.RFC3339), from.UTC().Format(time.RFC3339))
		}
	}
	return scales
}

// k8sLoadReplicaSets는 비교 재료 RS를 읽는다(PG) — 이벤트에 나온 Deployment와
// 창 안에서 생성된 RS의 Deployment에 대해 형제 RS 전부.
func k8sLoadReplicaSets(ctx context.Context, pg *sql.DB, scales []*k8sScale, from, to time.Time,
	res *k8sChangeResult) ([]*k8sRS, map[string][]*k8sRS, bool) {
	rsByDeploy := map[string][]*k8sRS{}
	if pg == nil {
		res.SourceStatus["kcm_resources"] = "PG 미연결"
		return nil, rsByDeploy, false
	}
	created, err := k8sQueryRS(ctx, pg, nil, from, to)
	if err != nil {
		res.SourceStatus["kcm_resources"] = "조회 실패(" + beDetail(err) + ")"
		return nil, rsByDeploy, false
	}
	deploys := map[string]bool{}
	for _, s := range scales {
		if s.Kind == "Deployment" && s.RS != "" {
			deploys[k8sKey(s.Cluster, s.Namespace, s.Name)] = true
		}
	}
	for _, x := range created {
		if x.OwnerKind == "Deployment" && x.Owner != "" {
			deploys[k8sKey(x.Cluster, x.Namespace, x.Owner)] = true
		}
	}
	keys := make([]string, 0, len(deploys))
	for k := range deploys {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if len(keys) > 0 {
		all, err := k8sQueryRS(ctx, pg, keys, time.Time{}, time.Time{})
		if err != nil {
			res.SourceStatus["kcm_resources"] = "조회 실패(" + beDetail(err) + ")"
			return created, rsByDeploy, false
		}
		for _, x := range all {
			k := k8sKey(x.Cluster, x.Namespace, x.Owner)
			rsByDeploy[k] = append(rsByDeploy[k], x)
		}
	}
	res.SourceStatus["kcm_resources"] = "조회함(현재 상태 스냅샷)"
	return created, rsByDeploy, true
}

// k8sCollectRollouts는 Deployment별 롤아웃 판별 후 창 [from, to) 안 것만 남기고,
// 이벤트로 안 보인 창 안 RS 생성을 생성 시각 기준 롤아웃으로 더한다.
func k8sCollectRollouts(scales []*k8sScale, created []*k8sRS, rsByDeploy map[string][]*k8sRS,
	from, to time.Time) []k8sRollout {
	inWin := func(t time.Time) bool { return !t.Before(from) && t.Before(to) }
	byDeploy := map[string][]*k8sScale{}
	var dkeys []string
	for _, s := range scales {
		if s.Kind != "Deployment" || s.RS == "" {
			continue
		}
		k := k8sKey(s.Cluster, s.Namespace, s.Name)
		if _, ok := byDeploy[k]; !ok {
			dkeys = append(dkeys, k)
		}
		byDeploy[k] = append(byDeploy[k], s)
	}
	sort.Strings(dkeys)
	var rollouts []k8sRollout
	seenNew := map[string]bool{}
	for _, k := range dkeys {
		sibs := rsByDeploy[k]
		byName := map[string]*k8sRS{}
		for _, x := range sibs {
			byName[x.Name] = x
		}
		var kept []k8sRollout
		for _, r := range k8sDetectRollouts(byDeploy[k], byName, sibs) {
			seenNew[k+"|"+r.New] = true
			if !inWin(r.At) {
				for _, st := range r.Steps { // 창 밖 롤아웃의 단계는 창 안이면 레플리카 변경으로 돌려놓는다
					st.consumed = false
				}
				continue
			}
			kept = append(kept, r)
		}
		for _, r := range kept {
			var steps []*k8sScale
			for _, st := range r.Steps {
				st.consumed = true
				if inWin(st.At) {
					steps = append(steps, st)
				}
			}
			r.Steps = steps
			rollouts = append(rollouts, r)
		}
	}
	// 이벤트 없이 창 안에서 생성된 RS(이벤트 원천 밖 구간·미수집) — 생성 시각 기준.
	for _, x := range created {
		if x.OwnerKind != "Deployment" || x.Owner == "" {
			continue
		}
		k := k8sKey(x.Cluster, x.Namespace, x.Owner)
		if seenNew[k+"|"+x.Name] {
			continue
		}
		sibs := rsByDeploy[k]
		self := x
		for _, s := range sibs {
			if s.Name == x.Name {
				self = s
			}
		}
		prev := k8sPrevByRevision(self, sibs)
		if prev == nil {
			continue // 첫 리비전(신규 Deployment)이거나 직전 RS 삭제 — 비교 대상 없음
		}
		rollouts = append(rollouts, k8sRollout{
			At: x.Created, Cluster: x.Cluster, Namespace: x.Namespace, Deployment: x.Owner,
			New: x.Name, Prev: prev.Name, PrevBasis: "rs_created+revision_annotation",
			Notes: []string{"창 안 스케일 이벤트로는 안 보인 롤아웃 — 새 RS 생성 시각 기준"},
		})
	}
	return rollouts
}

// k8sAssemble는 봉투 항목을 만든다 — 힌트 귀속 → 최신순, 종류별 상한 후 합친다.
func k8sAssemble(ctx context.Context, vm *VM, res *k8sChangeResult, rollouts []k8sRollout, replicas []*k8sScale,
	rsByDeploy map[string][]*k8sRS, inv lcInventory, hint string, pgOK bool, to time.Time) {
	type item struct {
		f      Finding
		at     time.Time
		hit    bool
		key    string
		digest string
		roll   *k8sRollout
		limits []k8sDiff
	}
	var ritems, sitems []item
	totalClipped := 0
	for i := range rollouts {
		r := rollouts[i]
		f, refs, hit, clipped, limits := k8sRolloutFinding(r, rsByDeploy[k8sKey(r.Cluster, r.Namespace, r.Deployment)], inv, hint, pgOK)
		totalClipped += clipped
		f["refs"] = refs
		ritems = append(ritems, item{f: f, at: r.At, hit: hit, key: r.Namespace + "/" + r.New,
			digest: k8sRolloutDigest(r, f), roll: &rollouts[i], limits: limits})
	}
	for _, s := range replicas {
		f, refs, hit := k8sReplicaFinding(s, inv, hint)
		f["refs"] = refs
		sitems = append(sitems, item{f: f, at: s.At, hit: hit, key: s.Namespace + "/" + s.Name + "/" + s.RS,
			digest: fmt.Sprintf("레플리카 %s/%s %v→%d@%s", s.Namespace, s.Name, f["from"], s.To, s.At.Format(time.RFC3339))})
	}
	order := func(xs []item) {
		sort.SliceStable(xs, func(i, j int) bool {
			if xs[i].hit != xs[j].hit {
				return xs[i].hit
			}
			if !xs[i].at.Equal(xs[j].at) {
				return xs[i].at.After(xs[j].at)
			}
			return xs[i].key < xs[j].key
		})
	}
	order(ritems)
	order(sitems)
	rShown, sShown := min(len(ritems), k8sChgMaxRollouts), min(len(sitems), k8sChgMaxReplica)
	merged := append(append([]item{}, ritems[:rShown]...), sitems[:sShown]...)
	order(merged)
	// 롤아웃 후 자원 사용량 — 반환되는 롤아웃 중 한도 차이가 있는 것만(VM 조회 상한).
	budget, usageN := k8sChgMaxUsageObs, 0
	for i := range merged {
		it := &merged[i]
		if it.roll == nil || len(it.limits) == 0 {
			continue
		}
		end, endBasis := k8sUsageEnd(*it.roll, rollouts, to)
		obs, orefs := k8sUsageObs(ctx, vm, *it.roll, it.limits, end, endBasis, res, &budget)
		if len(obs) > 0 {
			usageN += len(obs)
			it.f["post_rollout_usage"] = obs
			it.f["refs"] = append(it.f["refs"].([]string), orefs...)
			it.digest = k8sRolloutDigest(*it.roll, it.f)
		}
	}
	if usageN > 0 || budget < k8sChgMaxUsageObs {
		res.Meta["post_rollout_usage"] = map[string]any{
			"observations": usageN, "cap": k8sChgMaxUsageObs, "span_cap": k8sChgUsageSpan.String(),
			"basis": "resources.limits(memory·cpu) 차이가 있는 반환 롤아웃만. 새 ReplicaSet pod(pod=~<RS>-*)·컨테이너의 사용량 지표 " +
				"max_over_time/tmax_over_time을 [롤아웃 시각, 끝] 구간에서 계산(끝 = 같은 RS를 다시 쓰는 롤아웃 직전·창 끝·상한 중 이른 것). " +
				"%는 템플릿의 새 한도 값 대비, observed_limit_max는 한도 지표의 관측 최대값. 인스턴스 = pod·container_id(재시작하면 새 인스턴스). " +
				"peak_at은 tmax_over_time 정확값, per_instance의 first_seen·last_seen·last_value는 range 점(step_s 구간 끝 시각, ±step_s)",
		}
		if budget <= 0 {
			res.Meta["post_rollout_usage"].(map[string]any)["capped"] = true
		}
	}
	for i, it := range merged {
		res.Findings = append(res.Findings, it.f)
		res.Refs = append(res.Refs, it.f["refs"].([]string)...)
		if it.hit {
			res.HintHits++
		}
		if i < 3 { // 요약 한 줄 재료 — 응답 앞부분만 보는 소비자 대비 상위 3건
			res.SummaryDigest = append(res.SummaryDigest, it.digest)
		}
	}
	res.Rollouts, res.Replica, res.Returned = len(rollouts), len(replicas), len(merged)
	res.Truncated = rShown < len(ritems) || sShown < len(sitems)
	res.Meta["rollouts_total"] = len(rollouts)
	res.Meta["replica_changes_total"] = len(replicas)
	res.Meta["rollouts_returned"] = rShown
	res.Meta["replica_changes_returned"] = sShown
	res.Meta["caps"] = map[string]any{"rollouts": k8sChgMaxRollouts, "replica_changes": k8sChgMaxReplica,
		"template_diff_per_rollout": k8sChgMaxDiff, "value_chars": k8sChgValueMax}
	if res.Truncated {
		res.Meta["truncation_note"] = "상한 초과분은 반환하지 않았다(힌트 귀속 → 최신순으로 자름) — from/to를 좁히거나 target 힌트로 다시 조회"
	}
	if totalClipped > 0 {
		res.Meta["values_clipped"] = totalClipped
	}
	if res.CHTruncated {
		res.Meta["scale_events_capped"] = true
	}
	res.Meta["basis"] = "롤아웃 = 같은 Deployment에서 한 ReplicaSet이 0에서 스케일 업(스케일 이벤트)되었거나 창 안에서 새 ReplicaSet이 생성된 것. " +
		"이전 RS = ReplicaSet 리비전 원장(현재+revision-history)의 직전 리비전 소유 RS — 원장 맞춤이 안 되면 가장 가까운 다른 RS 스케일 다운. " +
		"파드 템플릿은 ReplicaSet별 불변이라 현재 스냅샷 비교가 당시 차이다. 비교 필드는 이미지·env·envFrom·리소스·프로브·command/args·포트뿐(라벨·주석 비노출)"
	res.Meta["not_covered"] = "StatefulSet·DaemonSet 템플릿 변경(ControllerRevision 미수집), 이미 삭제된 ReplicaSet의 템플릿"
}

// k8sRolloutDigest는 요약 문장용 한 토막이다 — 필드 이름만(값 없음), 최대 3개.
func k8sRolloutDigest(r k8sRollout, f Finding) string {
	tag := ""
	switch f["template_diff_status"] {
	case "compared":
		diffs, _ := f["template_diff"].([]k8sDiff)
		names := []string{}
		for i, d := range diffs {
			if i >= 3 {
				names = append(names, "…")
				break
			}
			names = append(names, d.Field)
		}
		tag = "[" + strings.Join(names, ", ") + "]"
	case "no_exposed_difference":
		tag = "[비교 필드 차이 없음]"
	default:
		tag = "[스펙 비교 불가]"
	}
	if obs, ok := f["post_rollout_usage"].([]map[string]any); ok {
		for _, o := range obs {
			if pct, ok := o["peak_pct_of_new_limit"]; ok {
				tag += fmt.Sprintf(" %s 사용 피크 %v%%(새 한도 %v, 피크 %v", o["resource"], pct, o["new_limit"], o["peak_at"])
				if rows, ok := o["per_instance"].([]map[string]any); ok && len(rows) > 0 {
					tag += fmt.Sprintf(", 컨테이너 인스턴스 %v개", o["per_instance_total"])
					if lp, ok := rows[len(rows)-1]["last_pct"]; ok && lp != nil {
						tag += fmt.Sprintf(", 마지막 인스턴스 마지막 값 %v%%", lp)
					}
				}
				tag += ")"
				break
			}
		}
	}
	return fmt.Sprintf("롤아웃 %s/%s %s→%s %s@%s", r.Namespace, r.Deployment, r.Prev, r.New, tag, r.At.Format(time.RFC3339))
}

// k8sUsageEnd는 롤아웃 후 사용량 관측 구간의 끝이다 — 같은 RS를 다시 활성화하는 뒤 롤아웃
// 직전(그 뒤 pod는 같은 RS 이름이라 섞인다), 창 끝, 상한 중 이른 것.
func k8sUsageEnd(r k8sRollout, all []k8sRollout, to time.Time) (time.Time, string) {
	end, basis := to, "window_end"
	if lim := r.At.Add(k8sChgUsageSpan); lim.Before(end) {
		end, basis = lim, "span_cap"
	}
	for _, o := range all {
		if o.Cluster == r.Cluster && o.Namespace == r.Namespace && o.Deployment == r.Deployment &&
			o.New == r.New && o.At.After(r.At) && o.At.Before(end) {
			end, basis = o.At, "same_rs_reactivated"
		}
	}
	return end, basis
}

// k8sParseQuantity는 쿠버네티스 수량 문자열을 지표 단위로 바꾼다 — memory는 바이트, cpu는 밀리코어.
func k8sParseQuantity(q, resource string) (float64, bool) {
	q = strings.TrimSpace(q)
	if q == "" {
		return 0, false
	}
	mult := 1.0
	num := q
	bin := map[string]float64{"Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40, "Pi": 1 << 50, "Ei": 1 << 60}
	dec := map[string]float64{"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
	if len(q) > 2 {
		if m, ok := bin[q[len(q)-2:]]; ok {
			mult, num = m, q[:len(q)-2]
		}
	}
	if mult == 1 && len(q) > 1 {
		if m, ok := dec[q[len(q)-1:]]; ok {
			mult, num = m, q[:len(q)-1]
		}
	}
	v, err := strconv.ParseFloat(num, 64)
	if err != nil || v < 0 {
		return 0, false
	}
	v *= mult
	if resource == "cpu" {
		v *= 1000 // 코어 → 밀리코어
	}
	return v, true
}

// k8sUsageInst는 컨테이너 인스턴스(pod·container_id — 재시작하면 container_id가 바뀌어 새 시리즈) 하나의 사용량 요약이다.
type k8sUsageInst struct {
	pod, cid           string
	peak, last         float64
	first, lastAt, pAt time.Time
}

// k8sUsageObs는 한도 차이 하나당 사용량 관측 하나를 만든다. VM 조회 3회:
//   - range max_over_time(선택자[step]) — 인스턴스별 피크·처음/마지막 관측·마지막 값(정확한 최대, 시각은 ±step)
//   - instant tmax_over_time(선택자[구간]) — 인스턴스별 피크의 정확한 시각
//   - instant max_over_time(한도 선택자[구간]) — 한도 지표 관측 최대값
func k8sUsageObs(ctx context.Context, vm *VM, r k8sRollout, limits []k8sDiff, end time.Time, endBasis string,
	res *k8sChangeResult, budget *int) ([]map[string]any, []string) {
	if vm == nil {
		res.SourceStatus["vm_usage"] = "VM 미연결 — 롤아웃 후 사용량 미조회"
		return nil, nil
	}
	if !end.After(r.At) {
		return nil, nil
	}
	var out []map[string]any
	var refs []string
	secs := int64(end.Sub(r.At) / time.Second)
	if secs < 1 {
		secs = 1
	}
	stepS := (secs + k8sUsagePoints - 1) / k8sUsagePoints
	if stepS < 60 {
		stepS = 60 // kcm 수집 주기(실측 1분) 미만은 같은 표본의 반복일 뿐이다
	}
	step := time.Duration(stepS) * time.Second
	w := "[" + strconv.FormatInt(secs, 10) + "s]"
	win := r.At.UTC().Format(time.RFC3339) + "/" + end.UTC().Format(time.RFC3339)
	for _, d := range limits {
		resource := strings.TrimPrefix(d.Field, "resources.limits.")
		m, ok := k8sUsageMetrics[resource]
		if !ok || d.After == nil || strings.HasPrefix(d.Container, "init:") {
			continue
		}
		if *budget <= 0 {
			break
		}
		*budget--
		podRe := regexp.QuoteMeta(r.New) + "-[a-z0-9]+"
		sel := func(metric string) string {
			return fmt.Sprintf(`{__name__=%q,target_id=%q,namespace=%q,container=%q,pod=~%q}`, metric, r.Cluster, r.Namespace, d.Container, podRe)
		}
		obs := map[string]any{
			"container": d.Container, "resource": resource,
			"new_limit": *d.After, "unit": m[2], "usage_metric": m[0],
			"window": map[string]any{"from": r.At.UTC().Format(time.RFC3339), "to": end.UTC().Format(time.RFC3339), "to_basis": endBasis},
		}
		limitVal, limOK := k8sParseQuantity(*d.After, resource)
		if limOK {
			obs["new_limit_value"] = limitVal
		}
		pct := func(v float64) any {
			if !limOK || limitVal <= 0 {
				return nil
			}
			return float64(int64(v/limitVal*1000+0.5)) / 10
		}
		ref := fmt.Sprintf("vm:%s:%s:%s/%s-*:%s:%s", m[0], r.Cluster, r.Namespace, r.New, d.Container, win)
		series, err := vm.RangeQuery(ctx, fmt.Sprintf("max_over_time(%s[%ds])", sel(m[0]), stepS), r.At, end, step)
		if err != nil {
			obs["observation"] = "query_failed"
			res.SourceErrors["vm"] = beDetail(err)
			out = append(out, obs)
			continue
		}
		obs["refs"] = []string{ref}
		refs = append(refs, ref)
		var insts []*k8sUsageInst
		byKey := map[string]*k8sUsageInst{}
		for _, sr := range series {
			if len(sr.Values) == 0 {
				continue
			}
			in := &k8sUsageInst{pod: sr.Labels["pod"], cid: sr.Labels["container_id"], first: sr.Times[0],
				lastAt: sr.Times[len(sr.Times)-1], last: sr.Values[len(sr.Values)-1], peak: sr.Values[0], pAt: sr.Times[0]}
			for i, v := range sr.Values {
				if v > in.peak {
					in.peak, in.pAt = v, sr.Times[i]
				}
			}
			insts = append(insts, in)
			byKey[in.pod+"|"+in.cid] = in
		}
		if len(insts) == 0 {
			obs["observation"] = "no_samples"
			obs["peak_usage"] = nil
			out = append(out, obs)
			continue
		}
		// 정확한 피크 시각(인스턴스별) — 실패하면 range 점 시각(±step)을 그대로 둔다.
		exact := false
		if tm, err := vm.InstantQuery(ctx, "tmax_over_time("+sel(m[0])+w+")", end); err == nil {
			for _, sm := range tm {
				if in, ok := byKey[sm.Labels["pod"]+"|"+sm.Labels["container_id"]]; ok && sm.Value > 0 {
					in.pAt, exact = time.Unix(int64(sm.Value), 0).UTC(), true
				}
			}
		}
		sort.Slice(insts, func(i, j int) bool {
			if !insts[i].first.Equal(insts[j].first) {
				return insts[i].first.Before(insts[j].first)
			}
			return insts[i].pod+insts[i].cid < insts[j].pod+insts[j].cid
		})
		best := insts[0]
		for _, in := range insts[1:] {
			if in.peak > best.peak {
				best = in
			}
		}
		obs["peak_usage"] = round3(best.peak)
		obs["peak_pod"] = best.pod
		obs["peak_at"] = best.pAt.UTC().Format(time.RFC3339)
		if p := pct(best.peak); p != nil {
			obs["peak_pct_of_new_limit"] = p
		}
		if !exact {
			obs["peak_at_precision_s"] = stepS
		}
		// pod·컨테이너 인스턴스 수(같은 pod에서 container_id가 바뀌면 별도 인스턴스). 키 이름은 -blind의
		// 알파벳순 재정렬에서 peak_* 뒤에 오도록 per_ 접두.
		obs["per_instance_total"] = len(insts)
		var rows []map[string]any
		for i, in := range insts {
			if i >= k8sUsageMaxInst {
				break
			}
			cid := in.cid
			if len(cid) > 12 {
				cid = cid[:12]
			}
			row := map[string]any{"pod": in.pod, "container_id": cid, "peak": round3(in.peak), "peak_at": in.pAt.UTC().Format(time.RFC3339),
				"first_seen": in.first.UTC().Format(time.RFC3339), "last_seen": in.lastAt.UTC().Format(time.RFC3339), "last_value": round3(in.last)}
			if p := pct(in.peak); p != nil {
				row["peak_pct"] = p
				row["last_pct"] = pct(in.last)
			}
			rows = append(rows, row)
		}
		obs["per_instance"] = rows
		obs["step_s"] = stepS
		if lim, err := vm.InstantQuery(ctx, "max_over_time("+sel(m[1])+w+")", end); err == nil && len(lim) > 0 {
			mx := lim[0].Value
			for _, sm := range lim[1:] {
				if sm.Value > mx {
					mx = sm.Value
				}
			}
			obs["observed_limit_max"] = round3(mx)
			lref := fmt.Sprintf("vm:%s:%s:%s/%s-*:%s:%s", m[1], r.Cluster, r.Namespace, r.New, d.Container, win)
			obs["refs"] = []string{ref, lref}
			refs = append(refs, lref)
		}
		out = append(out, obs)
	}
	return out, refs
}

// k8sQueryRS는 kcm_resources의 ReplicaSet을 읽는다. keys가 있으면 그 Deployment들의
// 형제 전부, 없으면 창 [from,to) 안에서 생성된 것. 실측상 creationTimestamp는
// 'YYYY-MM-DDTHH:MM:SSZ'라 같은 서식 문자열 비교가 시각 비교다(캐스트 실패로
// 조회 전체가 죽는 것을 피한다).
func k8sQueryRS(ctx context.Context, pg *sql.DB, keys []string, from, to time.Time) ([]*k8sRS, error) {
	base := `SELECT target_id::text, namespace, name,
	       coalesce(data->'ownerReference'->>'ownerKind', ''),
	       coalesce(data->'ownerReference'->>'ownerName', ''),
	       coalesce(data->>'creationTimestamp', ''), coalesce(yaml, '')
	FROM kcm_resources WHERE kind = 'replicaset' AND `
	var rows *sql.Rows
	var err error
	if keys != nil {
		rows, err = pg.QueryContext(ctx, base+
			`(target_id::text || '|' || namespace || '|' || coalesce(data->'ownerReference'->>'ownerName', '')) = ANY($1)`, keys)
	} else {
		rows, err = pg.QueryContext(ctx, base+
			`coalesce(data->>'creationTimestamp', '') >= $1 AND coalesce(data->>'creationTimestamp', '') < $2`,
			from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
	}
	if err != nil {
		return nil, fmt.Errorf("list_changes ReplicaSet 조회: %w", pgErr(err))
	}
	defer rows.Close()
	var out []*k8sRS
	for rows.Next() {
		var x k8sRS
		var created, raw string
		if err := rows.Scan(&x.Cluster, &x.Namespace, &x.Name, &x.OwnerKind, &x.Owner, &created, &raw); err != nil {
			return nil, fmt.Errorf("list_changes ReplicaSet 행: %w", pgErr(err))
		}
		if t, err := time.Parse(time.RFC3339, created); err == nil {
			x.Created = t.UTC()
		}
		x.Pod, x.Revision, x.History, x.ParseErr = parseRSSpec(raw)
		out = append(out, &x)
	}
	return out, pgErr(rows.Err())
}

// k8sResolve는 K8s 리소스를 대상 명부의 정본 이름(클러스터:종류:ns/이름)으로 푼다.
func k8sResolve(inv lcInventory, cluster, kind, ns, name string) (string, bool) {
	id, ok := inv.byName[cluster+":"+kind+":"+ns+"/"+name]
	return id, ok
}

func k8sTargetEntry(id, role, basis string) map[string]any {
	return map[string]any{"target_id": id, "role": role, "match_basis": basis}
}

func k8sStep(s *k8sScale) string {
	from := "?"
	if s.From != nil {
		from = strconv.Itoa(*s.From)
	}
	step := fmt.Sprintf("%s %s %s %s→%d", s.At.Format(time.RFC3339), s.Dir, s.RS, from, s.To)
	if s.NewRS {
		step += " (RS 생성)"
	}
	return step
}

func k8sEventRef(s *k8sScale) string {
	return fmt.Sprintf("ch:kcm_events_local:%s:%s/%s:%s@%s", s.Cluster, s.Namespace, s.Name, s.Reason, s.At.Format(time.RFC3339))
}

func k8sRSRef(cluster, ns, rs string) string {
	return fmt.Sprintf("pg:kcm_resources:%s:replicaset:%s/%s", cluster, ns, rs)
}

func k8sRolloutFinding(r k8sRollout, sibs []*k8sRS, inv lcInventory, hint string, pgOK bool) (Finding, []string, bool, int, []k8sDiff) {
	var newRS, prevRS *k8sRS
	for _, s := range sibs {
		switch s.Name {
		case r.New:
			newRS = s
		case r.Prev:
			prevRS = s
		}
	}
	f := Finding{
		"at": r.At.Format(time.RFC3339), "kind": "k8s_rollout",
		"scope": "target", "correlation": "exact",
		"sources":  []string{"kcm_events", "kcm_resources"},
		"workload": "Deployment/" + r.Deployment, "namespace": r.Namespace,
		"new_replicaset": r.New, "previous_replicaset": r.Prev, "previous_basis": r.PrevBasis,
	}
	if len(r.Steps) == 0 {
		f["sources"] = []string{"kcm_resources"}
	}
	notes := append([]string{}, r.Notes...)
	clipped := 0
	nDiff := 0
	var limits []k8sDiff // resources.limits 차이(절단 전 전량) — 롤아웃 후 사용량 관측 재료
	switch {
	case !pgOK:
		f["template_diff_status"] = "spec_unavailable"
		notes = append(notes, "ReplicaSet 스펙 조회 실패 — 템플릿 차이 미산출(결손)")
	case newRS == nil || prevRS == nil:
		f["template_diff_status"] = "spec_unavailable"
		notes = append(notes, "비교할 ReplicaSet이 현재 스냅샷에 없다(삭제됨) — 템플릿 차이 미산출")
	case newRS.ParseErr != "" || prevRS.ParseErr != "":
		f["template_diff_status"] = "spec_unavailable"
		notes = append(notes, "스펙 해석 불가: "+strings.TrimSpace(newRS.ParseErr+" "+prevRS.ParseErr))
	default:
		diffs, c := diffPodSpecs(prevRS.Pod, newRS.Pod)
		clipped = c
		nDiff = len(diffs)
		for _, d := range diffs {
			if strings.HasPrefix(d.Field, "resources.limits.") && d.Change != "removed" {
				limits = append(limits, d)
			}
		}
		f["template_diff_total"] = nDiff
		if nDiff == 0 {
			f["template_diff_status"] = "no_exposed_difference"
			notes = append(notes, "비교 필드(이미지·env·리소스·프로브·command/args·포트)에는 차이 없음 — 템플릿의 비노출 부분이 바뀌었다")
		} else {
			f["template_diff_status"] = "compared"
			if nDiff > k8sChgMaxDiff {
				diffs = diffs[:k8sChgMaxDiff]
				f["template_diff_truncated"] = true
			}
		}
		if len(diffs) > 0 {
			f["template_diff"] = diffs
		}
	}
	f["title"] = fmt.Sprintf("롤아웃 Deployment %s/%s — 파드 템플릿 차이 %d건", r.Namespace, r.Deployment, nDiff)
	// 스냅샷 시점 최신 리비전 RS — 새 RS가 이후 다른 RS로 대체됐는지(되돌림 포함)의
	// 사실. 이벤트 원천 밖 롤아웃(생성 시각 경로)은 대체 시각을 모르므로 함께 밝힌다.
	if latest := k8sLatestRevision(sibs); latest != nil && latest.Name != r.New {
		f["snapshot_latest_replicaset"] = latest.Name
		if len(r.Steps) == 0 {
			notes = append(notes, "스냅샷 시점 최신 리비전은 "+latest.Name+" — 이 롤아웃은 이후 대체됐다(대체 시각은 이벤트 원천 밖이라 미상)")
		}
	}
	if len(r.Steps) > 0 {
		steps := make([]string, 0, len(r.Steps))
		for _, s := range r.Steps {
			steps = append(steps, k8sStep(s))
		}
		f["scale_steps"] = steps
	}

	// 대상·refs.
	hit := hint != "" && hint == r.Cluster
	var targets []map[string]any
	add := func(id, role, basis string) {
		if id == "" {
			return
		}
		if id == hint {
			hit = true
		}
		targets = append(targets, k8sTargetEntry(id, role, basis))
	}
	if r.DeployTgt != "" {
		if _, ok := inv.byID[r.DeployTgt]; ok {
			add(r.DeployTgt, "deployment", "exact_id")
		}
	}
	if len(targets) == 0 {
		if id, ok := k8sResolve(inv, r.Cluster, "deployment", r.Namespace, r.Deployment); ok {
			add(id, "deployment", "resource_key")
		}
	}
	if id, ok := k8sResolve(inv, r.Cluster, "replicaset", r.Namespace, r.New); ok {
		add(id, "new_replicaset", "resource_key")
	}
	if id, ok := k8sResolve(inv, r.Cluster, "replicaset", r.Namespace, r.Prev); ok {
		add(id, "previous_replicaset", "resource_key")
	}
	if len(targets) > 0 {
		f["targets"] = targets
		f["targets_total"] = len(targets)
	} else {
		f["targets_total"] = 0
		f["match_basis"] = "unattributed"
	}
	if hint != "" {
		f["hint_target_included"] = hit
	}
	if len(notes) > 0 {
		f["notes"] = notes
	}
	refs := []string{}
	for _, s := range r.Steps {
		refs = append(refs, k8sEventRef(s))
	}
	if newRS != nil {
		refs = append(refs, k8sRSRef(r.Cluster, r.Namespace, r.New))
	}
	if prevRS != nil {
		refs = append(refs, k8sRSRef(r.Cluster, r.Namespace, r.Prev))
	}
	return f, refs, hit, clipped, limits
}

func k8sReplicaFinding(s *k8sScale, inv lcInventory, hint string) (Finding, []string, bool) {
	var from any = "미상"
	fromS := "?"
	if s.From != nil {
		from, fromS = *s.From, strconv.Itoa(*s.From)
	}
	f := Finding{
		"at": s.At.Format(time.RFC3339), "kind": "k8s_replica_change",
		"scope": "target", "correlation": "exact", "sources": []string{"kcm_events"},
		"workload": s.Kind + "/" + s.Name, "namespace": s.Namespace,
		"from": from, "to": s.To,
		"title": fmt.Sprintf("레플리카 변경 %s %s/%s %s→%d", s.Kind, s.Namespace, s.Name, fromS, s.To),
	}
	if s.RS != "" {
		f["replicaset"] = s.RS
	}
	if s.NewRS {
		f["replicaset_created"] = true // 이 업이 RS 생성 시점(0→N) — 직전 RS가 없는 첫 리비전 등
	}
	if s.HPAReason != "" {
		v, _ := k8sClip(s.HPAReason)
		f["hpa_reason"] = v
	}
	hit := hint != "" && hint == s.Cluster
	var targets []map[string]any
	add := func(id, role, basis string) {
		if id == "" {
			return
		}
		if id == hint {
			hit = true
		}
		targets = append(targets, k8sTargetEntry(id, role, basis))
	}
	kind := strings.ToLower(s.Kind)
	if kind == "horizontalpodautoscaler" {
		kind = "hpa"
	}
	if s.ObjTarget != "" && s.ObjTarget != s.Cluster {
		if _, ok := inv.byID[s.ObjTarget]; ok {
			add(s.ObjTarget, kind, "exact_id")
		}
	}
	if len(targets) == 0 {
		if id, ok := k8sResolve(inv, s.Cluster, kind, s.Namespace, s.Name); ok {
			add(id, kind, "resource_key")
		}
	}
	if s.RS != "" {
		if id, ok := k8sResolve(inv, s.Cluster, "replicaset", s.Namespace, s.RS); ok {
			add(id, "replicaset", "resource_key")
		}
	}
	if len(targets) > 0 {
		f["targets"] = targets
		f["targets_total"] = len(targets)
	} else {
		f["targets_total"] = 0
		f["match_basis"] = "unattributed"
	}
	if hint != "" {
		f["hint_target_included"] = hit
	}
	return f, []string{k8sEventRef(s)}, hit
}
