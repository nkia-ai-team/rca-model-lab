// expand_topology 순수 단위 — 저장소 없이 도는 규칙만. 회귀 가드는
// 설계에서 실측으로 뒤집힌 지점들이다(§12): span_kind 조합 고정,
// peer 노드 키에서 path 제외, hosted_on의 hop 미소모, 창 기본값.
package tools

import (
	"testing"
	"time"
)

func TestETSpanKindEdge(t *testing.T) {
	// PRODUCER→CONSUMER를 sync_call로 올리면 안 된다 — parent-child만으로
	// 메시지 계보를 보장하지 못한다(span link 실측 0건).
	cases := []struct{ pk, ck, want string }{
		{"CLIENT", "SERVER", etKindSync},
		{"PRODUCER", "CONSUMER", etKindAsync},
		{"INTERNAL", "SERVER", etKindUnknown},
		{"CLIENT", "CONSUMER", etKindUnknown},
	}
	for _, c := range cases {
		if got, _ := etSpanKindEdge(c.pk, c.ck); got != c.want {
			t.Errorf("%s→%s: got %s want %s", c.pk, c.ck, got, c.want)
		}
	}
}

func TestETSplitURLDropsPath(t *testing.T) {
	// 노드 키에 path·query가 들어가면 동적 URL마다 노드가 생겨 상한이
	// 즉시 포화한다(§12.3). authority까지만 키다.
	scheme, auth, path := etSplitURL("http://testbed-product:8081/api/products?page=43&size=20", "")
	if scheme != "http" || auth != "testbed-product:8081" {
		t.Fatalf("authority 분리 실패: %s / %s", scheme, auth)
	}
	if path != "/api/products?page=43&size=20" {
		t.Fatalf("path 보존 실패: %s", path)
	}
	// url이 없으면 server.address가 authority다.
	_, auth2, _ := etSplitURL("", "testbed-external-pg-mock")
	if auth2 != "testbed-external-pg-mock" {
		t.Fatalf("addr 폴백 실패: %s", auth2)
	}
}

func TestETPeerNodeKeyMergesPaths(t *testing.T) {
	// 같은 목적지를 여러 경로로 부르면 노드는 하나, 경로는 간선 속성.
	res := newETResult()
	inv := &etInventory{byID: map[string]etTargetMeta{}}
	svcOf := map[string]etSvcID{}
	for _, p := range []etPeerRow{
		{Svc: "commerce-payment", Scheme: "http", Authority: "ext:9090", Path: "/pay", Spans: 108},
		{Svc: "food-delivery-payment", Scheme: "http", Authority: "ext:9090", Path: "/v1/payments", Spans: 128},
	} {
		etAddPeer(res, inv, svcOf, p, 1)
	}
	peerNodes := 0
	for id := range res.nodes {
		if len(id) > 5 && id[:5] == "peer:" {
			peerNodes++
		}
	}
	if peerNodes != 1 {
		t.Fatalf("목적지 노드가 합쳐지지 않음: %d개", peerNodes)
	}
	if len(res.edges) != 2 {
		t.Fatalf("호출자별 간선이 아님: %d개", len(res.edges))
	}
}

func TestETHostedOnDoesNotConsumeHop(t *testing.T) {
	// hop_cost=0 — 호스트가 같은 층에 들어와야 앱 이웃을 밀어내지 않는다.
	res := newETResult()
	app := res.addNode(&etNode{ID: "target:a", Name: "app", Type: "application", Hop: 1})
	host := res.addNode(&etNode{ID: "target:n", Name: "tb-w1", Type: "server", Hop: 1})
	res.addEdge(&etEdge{Source: app.ID, Target: host.ID, Kind: etKindHosted, Hop: 1})
	if host.Hop != app.Hop {
		t.Fatalf("hosted_on이 홉을 소모함: app=%d host=%d", app.Hop, host.Hop)
	}
}

func TestETObservationMismatch(t *testing.T) {
	// 호출자만 실패로 관측 = 피호출자가 자기 기록에 안 남김. 실측
	// (product→inventory 83% caller_err, callee_err 0)의 회귀 가드.
	res := newETResult()
	inv := &etInventory{byID: map[string]etTargetMeta{}}
	etAddPaired(res, inv, map[string]etSvcID{}, etPair{
		CallerSvc: "commerce-product", CalleeSvc: "commerce-inventory",
		PK: "CLIENT", CK: "SERVER", Pairs: 987, CallerErr: 822, CalleeErr: 0,
	}, 1)
	for _, e := range res.edges {
		if !e.ObservationMismatch {
			t.Fatal("관측 어긋남이 표기되지 않음 — 83% 실패가 건강해 보인다")
		}
		if e.RetriesCollapsed {
			t.Fatal("retries_collapsed가 true — 묶을 근거가 원천에 없다")
		}
		if e.CallerObserved.Basis != "caller_client_duration" || e.CalleeObserved.Basis != "callee_server_duration" {
			t.Fatalf("latency_basis 미분리: %+v", e)
		}
	}
}

func TestETAsyncLatencySemantics(t *testing.T) {
	res := newETResult()
	inv := &etInventory{byID: map[string]etTargetMeta{}}
	etAddPaired(res, inv, map[string]etSvcID{}, etPair{
		CallerSvc: "commerce-payment", CalleeSvc: "commerce-order",
		PK: "PRODUCER", CK: "CONSUMER", Pairs: 88,
	}, 1)
	for _, e := range res.edges {
		if e.Kind != etKindAsync {
			t.Fatalf("비동기 간선이 아님: %s", e.Kind)
		}
		if e.CalleeObserved.Basis != "consumer_duration" {
			t.Fatalf("consumer 지연 기준 미표기: %s", e.CalleeObserved.Basis)
		}
		if e.Note == "" {
			t.Fatal("consume_duration_not_edge_delay 표기 없음")
		}
	}
}

func TestETNormTypeUsesResourceKind(t *testing.T) {
	// targets.type은 정본이 아니다 — pod·container·node가 전부
	// kubernetes_resource다(§8 결정 3).
	if got := etNormType("kubernetes_resource", "node"); got != "server" {
		t.Errorf("node → %s", got)
	}
	if got := etNormType("kubernetes_resource", "pod"); got != "k8s" {
		t.Errorf("pod → %s", got)
	}
	if got := etNormType("application", ""); got != "application" {
		t.Errorf("application → %s", got)
	}
}

func TestETWindowBasis(t *testing.T) {
	now := time.Date(2026, 7, 30, 12, 0, 0, 0, time.UTC)
	fe := now.Add(-2 * time.Hour)
	le := now.Add(-1 * time.Hour)

	from, to, basis, err := etWindow("", "", fe, le, now)
	if err != nil || !from.Equal(fe) || !to.Equal(le) {
		t.Fatalf("인시던트 창 기본 실패: %v %v %v", from, to, err)
	}
	if basis == "" {
		t.Fatal("window_basis 미표기")
	}

	// 발단만 있으면 ±30분, 그 사실을 밝힌다.
	from, to, basis, _ = etWindow("", "", fe, time.Time{}, now)
	if to.Sub(from) != time.Hour {
		t.Fatalf("발단 ±30분 아님: %v", to.Sub(from))
	}

	// 둘 다 없으면 라이브 탐색.
	_, _, basis, _ = etWindow("", "", time.Time{}, time.Time{}, now)
	if basis == "" {
		t.Fatal("라이브 폴백 미표기")
	}

	if _, _, _, err := etWindow("2026-07-30T12:00:00Z", "2026-07-30T11:00:00Z", fe, le, now); err == nil {
		t.Fatal("from >= to인데 통과함")
	}
}

func TestETSvcNodeNoFakeUUID(t *testing.T) {
	// target_id 미주입 서비스는 service_name 폴백이지 가짜 UUID가 아니다.
	inv := &etInventory{byID: map[string]etTargetMeta{}}
	n := etSvcNode(inv, map[string]etSvcID{"svc-x": {Status: "unresolved"}}, "svc-x", 1)
	if n.TargetID != "" {
		t.Fatalf("가짜 UUID 생성: %s", n.TargetID)
	}
	if n.MatchBasis != "service_name" || n.ResolutionStatus != "unresolved" {
		t.Fatalf("폴백 표식 없음: %+v", n)
	}

	// 복수 UUID는 ambiguous — max()로 숨기지 않는다.
	n2 := etSvcNode(inv, map[string]etSvcID{"svc-y": {Status: "ambiguous", Candidates: []string{"a", "b"}}}, "svc-y", 1)
	if n2.ResolutionStatus != "ambiguous" || n2.CandidateCount != 2 {
		t.Fatalf("복수 UUID를 숨김: %+v", n2)
	}
}

func TestETErrRateOrdering(t *testing.T) {
	// 호출수 단독 정렬이면 호출량 적고 에러율 높은 간선이 탈락한다.
	hi := &etEdge{PairedSpanPairs: 10, CallerObserved: &etObs{Errors: 9}}
	lo := &etEdge{PairedSpanPairs: 1000, CallerObserved: &etObs{Errors: 1}}
	if etErrRate(hi) <= etErrRate(lo) {
		t.Fatal("에러율 우선순위가 뒤집힘")
	}
}

func TestETPeerResolvesToK8sService(t *testing.T) {
	// 고아 CLIENT의 목적지가 내부 k8s Service면 그림자 노드를 만들지
	// 않는다 — 만들면 같은 의존이 sync_call(대상)과 apm_client_peer(peer)
	// 두 정체로 중복된다(구현 스모크 실측 회귀 가드).
	inv := &etInventory{
		byID:   map[string]etTargetMeta{"svc-1": {Name: "ns/testbed-product", Type: "k8s"}},
		k8sSvc: map[string]string{"testbed-product": "svc-1"},
		k8sAmb: map[string][]string{},
	}
	n := etPeerNode(inv, etPeerRow{Scheme: "http", Authority: "testbed-product:8081"}, 1)
	if n.ResolutionStatus != "resolved" || n.MatchBasis != "k8s_service_name" {
		t.Fatalf("k8s Service 해소 실패: %+v", n)
	}
	if n.TargetID != "svc-1" {
		t.Fatalf("대상 노드가 아님: %+v", n)
	}
	// 못 푸는 목적지는 그대로 peer — 가짜 UUID를 만들지 않는다.
	ext := etPeerNode(inv, etPeerRow{Scheme: "http", Authority: "testbed-external-pg-mock:9090"}, 1)
	if ext.ResolutionStatus != "unresolved" || ext.TargetID != "" {
		t.Fatalf("외부 목적지에 가짜 정체 부여: %+v", ext)
	}
	if ext.Scope != "peer_observed" {
		t.Fatalf("scope가 peer_observed가 아님: %s", ext.Scope)
	}
}
