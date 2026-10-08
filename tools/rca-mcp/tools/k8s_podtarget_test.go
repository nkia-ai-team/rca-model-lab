package tools

import (
	"context"
	"reflect"
	"testing"
)

// 클러스터 조회의 pod finding 은 이름만으로는 다음 행동(describe_target/expand_topology)으로 이어지지
// 않는다 — 등록 pod 면 target_id, 아니면 소유 워크로드(replicaset/deployment/statefulset)의 핸들을 붙인다.
func TestPodOwnerCandidates(t *testing.T) {
	cases := map[string][]string{
		"ns/testbed-payment-6cd7654fff-ts8lb": {"ns/testbed-payment-6cd7654fff", "ns/testbed-payment"},
		"ns/testbed-mysql-0":                  {"ns/testbed-mysql"},
		"ns/node-exporter-abcde":              {"ns/node-exporter", "ns/node"}, // 이름만으론 rs/deploy 구분 불가 — 등록된 쪽만 잡힌다
		"ns/single":                           nil,
		"nokey":                               nil,
	}
	for in, want := range cases {
		if got := podOwnerCandidates(in); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: got %v want %v", in, got, want)
		}
	}
}

func TestAttachPodTargets(t *testing.T) {
	findings := []Finding{
		{"signal": "restart_increase", "namespace": "ns", "pod": "testbed-payment-6cd7654fff-ts8lb", "value": 3.0},
		{"signal": "oom_killed", "namespace": "ns", "pod": "ghost-pod-abc12", "value": 1.0},
		{"signal": "running", "observation": "positive"}, // pod 없는 커버리지 행은 건드리지 않는다
	}
	if keys := podKeys(findings); len(keys) != 2 || keys[0] != "ns/testbed-payment-6cd7654fff-ts8lb" {
		t.Fatalf("podKeys = %v", keys)
	}
	attachPodTargets(findings, map[string]podTarget{
		"ns/testbed-payment-6cd7654fff-ts8lb": {OwnerID: "36854b47-0000-0000-0000-000000000001", OwnerKind: "replicaset", OwnerKey: "ns/testbed-payment-6cd7654fff"},
	})
	if findings[0]["target_id"] != nil || findings[0]["owner_target_id"] != "36854b47-0000-0000-0000-000000000001" || findings[0]["owner_kind"] != "replicaset" {
		t.Fatalf("resolved pod finding = %v", findings[0])
	}
	if findings[1]["target_id"] != nil || findings[1]["note"] == nil || findings[1]["owner_target_id"] != nil {
		t.Fatalf("unregistered pod must carry nil target_id and a search hint: %v", findings[1])
	}
	if _, touched := findings[2]["target_id"]; touched {
		t.Fatalf("coverage finding must stay untouched: %v", findings[2])
	}
}

// F05-H 실측 형상: 재시작 pod 미등록, replicaset·deployment 등록 → owner = replicaset(가장 구체적).
func TestLoadPodTargetsFromPG(t *testing.T) {
	p := newDgFakePG(t)
	p.script("FROM kcm_resource_targets", []string{"resource_key", "resource_kind", "target_id"},
		[][]string{
			{"ns/testbed-payment-6cd7654fff", "replicaset", "36854b47-0000-0000-0000-000000000001"},
			{"ns/testbed-payment", "deployment", "1f4a47ba-0000-0000-0000-000000000002"},
			{"ns/other-pod-abc12", "pod", "aaaaaaaa-0000-0000-0000-000000000003"},
		})
	pg, err := OpenPG(p.dsn())
	if err != nil {
		t.Fatalf("OpenPG: %v", err)
	}
	defer pg.Close()
	lookup, tok := loadPodTargets(context.Background(), pg, "a072cd3d-0000-0000-0000-000000000000",
		[]string{"ns/testbed-payment-6cd7654fff-ts8lb", "ns/other-pod-abc12", "ns/unknown-xyz12-abcde"})
	if tok != "" {
		t.Fatalf("unexpected pg token %q", tok)
	}
	if got := lookup["ns/testbed-payment-6cd7654fff-ts8lb"]; got.TargetID != "" || got.OwnerKind != "replicaset" || got.OwnerID != "36854b47-0000-0000-0000-000000000001" {
		t.Fatalf("payment pod lookup = %+v", got)
	}
	if got := lookup["ns/other-pod-abc12"]; got.TargetID != "aaaaaaaa-0000-0000-0000-000000000003" {
		t.Fatalf("registered pod lookup = %+v", got)
	}
	if _, ok := lookup["ns/unknown-xyz12-abcde"]; ok {
		t.Fatal("unknown pod must be absent")
	}
	if got, tok := loadPodTargets(context.Background(), nil, "x", []string{"a"}); len(got) != 0 || tok != "" {
		t.Fatalf("nil pg must degrade silently: %v %q", got, tok)
	}
}
