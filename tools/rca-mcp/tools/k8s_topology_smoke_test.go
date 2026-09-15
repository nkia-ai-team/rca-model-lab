// 실 스모크 — k8s(VM+PG)·topology(PG 인시던트 박제 1건 실존).
package tools

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestK8sTopologySmoke(t *testing.T) {
	vmURL, dsn := os.Getenv("RCA_VM_URL"), os.Getenv("RCA_PG_DSN")
	if vmURL == "" || dsn == "" {
		t.Skip("RCA_VM_URL·RCA_PG_DSN 미설정 — 생략")
	}
	vm := &VM{BaseURL: vmURL}
	pg, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()

	now := time.Now().UTC()
	from, to := now.Add(-30*time.Minute).Format(time.RFC3339), now.Format(time.RFC3339)
	k8s := NewK8sStateTool(vm, pg)

	// ① 클러스터 대상 직접.
	var cluster string
	if err := pg.QueryRow(`SELECT id::text FROM targets WHERE type='kubernetes' LIMIT 1`).Scan(&cluster); err != nil {
		t.Fatalf("kubernetes 대상: %v", err)
	}
	args, _ := json.Marshal(map[string]string{"target": cluster, "from": from, "to": to})
	out, err := k8s.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if env.Status == "no_data" {
		t.Fatalf("KCM 수집 중인데 no_data: %+v", env)
	}
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화: %v", err)
	}
	t.Logf("k8s(cluster): status=%s %s", env.Status, env.Summary)

	// ② 승격 pod 대상 → 클러스터·pod 필터 해석.
	var podTarget, podKey string
	if err := pg.QueryRow(`SELECT target_id::text, resource_key FROM kcm_resource_targets
		WHERE resource_kind='pod' AND resource_key LIKE '%testbed%' LIMIT 1`).Scan(&podTarget, &podKey); err != nil {
		t.Fatalf("승격 pod 대상: %v", err)
	}
	args, _ = json.Marshal(map[string]string{"target": podTarget, "from": from, "to": to})
	out, err = k8s.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env = out.(Envelope)
	t.Logf("k8s(pod %s): status=%s %s", podKey, env.Status, env.Summary)

	// (구 get_topology 스모크는 제거 — expand_topology 스모크가
	// expandtopology_smoke_test.go에서 원천 즉석 조립을 검증한다, §12.9)
}
