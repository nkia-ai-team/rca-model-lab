// describe_target 실 저장소 스모크 — RCA_PG_DSN + RCA_VM_URL 없으면 skip.
// F01-R 격리 세트 대조: 한도 카탈로그가 db.client.connections.max를
// 잡는지(§5.7 기지 — HikariPool 짝), 소속 4원천·미등록·형식 오류 경로.
// 실행(격리 세트):
//
//	RCA_VM_URL='http://localhost:38428' \
//	RCA_PG_DSN='postgres://lucida:lucida123@localhost:45432/lucida?sslmode=disable' \
//	go test ./tools -run TestDescribeTargetSmoke -v
package tools

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestDescribeTargetSmoke(t *testing.T) {
	if os.Getenv("RCA_PG_DSN") == "" || os.Getenv("RCA_VM_URL") == "" {
		t.Skip("RCA_PG_DSN·RCA_VM_URL 미설정 — 생략")
	}
	vm := vmFromEnv(t)
	pg, err := OpenPG(os.Getenv("RCA_PG_DSN"))
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()

	// 재생 세계의 시간 앵커 — 짝 표 대표 지표의 마지막 관측 시각.
	// instant 응답의 At은 평가 시각이라 tlast_over_time(MetricsQL)으로
	// 마지막 표본의 실제 시각을 값으로 받는다.
	now := time.Now().UTC()
	samples, err := vm.InstantQuery(context.Background(), `tlast_over_time(db.client.connections.max[30d])`, now)
	if err != nil || len(samples) == 0 {
		t.Skipf("db.client.connections.max 미관측 — F01-R 세트 아님? (%v)", err)
	}
	target, anchor := samples[0].Labels["target_id"], time.Unix(int64(samples[0].Value), 0).UTC()
	from := anchor.Add(-2 * time.Hour).Format(time.RFC3339)
	to := anchor.Add(time.Minute).Format(time.RFC3339)

	tool := NewDescribeTargetTool(pg, vm, anchor)
	call := func(id string) (Envelope, error) {
		args, _ := json.Marshal(map[string]string{"target": id, "from": from, "to": to})
		out, err := tool.Call(context.Background(), args)
		if err != nil {
			return Envelope{}, err
		}
		return out.(Envelope), nil
	}

	// ① 정상 경로 — 섹션 4개 + 한도 카탈로그에 HikariPool 짝.
	env, err := call(target)
	if err != nil {
		t.Fatal(err)
	}
	sections := map[string]Finding{}
	for _, f := range env.Findings {
		if s, ok := f["section"].(string); ok {
			sections[s] = f
		}
	}
	for _, s := range []string{"identity", "membership", "children", "capacity"} {
		if sections[s] == nil {
			t.Fatalf("섹션 %s 없음", s)
		}
	}
	ms := sections["membership"]["sources_checked"].(map[string]string)
	if len(ms) != 4 {
		t.Fatalf("소속 원천 4개 확인 내역 기대, 실제 %d: %v", len(ms), ms)
	}
	cap := sections["capacity"]
	if cap["current_not_checked"] != true {
		t.Fatal("current_not_checked 누락")
	}
	rows, _ := cap["catalog"].([]Finding)
	var hit Finding
	for _, r := range rows {
		if r["limit_metric"] == "db.client.connections.max" {
			hit = r
		}
	}
	if hit == nil {
		t.Fatalf("db.client.connections.max 짝이 카탈로그에 없음: %v", cap["summary"])
	}
	if hit["usage_observed"] != true {
		t.Fatal("usage 관측 여부 누락 — F01-R엔 usage도 있어야 함")
	}
	tl, _ := hit["limit_values"].([]Finding)
	if len(tl) == 0 {
		t.Fatal("limit 관측치 타임라인 없음")
	}
	for _, r := range tl {
		if r["stability"] == nil || r["sample_count"] == nil {
			t.Fatalf("타임라인 행에 stability·sample_count 필수: %v", r)
		}
	}
	t.Logf("capacity: %v / 대표 행: %v", cap["summary"], tl[0])

	// ② 미등록 UUID — 오류 아닌 정상 응답 + lookup_status.
	env, err = call("00000000-0000-4000-8000-000000000000")
	if err != nil {
		t.Fatalf("미등록은 오류가 아니어야 함(§9 결정 5): %v", err)
	}
	if len(env.Findings) == 0 || env.Findings[0]["lookup_status"] != "not_found" {
		t.Fatalf("lookup_status=not_found 기대: %+v", env.Findings)
	}

	// ③ 형식 오류 — topology 가짜 노드 복구 안내.
	if _, err := call("db:oracle"); err == nil {
		t.Fatal("형식 오류가 통과됨")
	}

	// ④ K8s 대상이 있으면 소속에 cluster 관계가 잡히는지.
	var podID string
	if e := pg.QueryRow(`SELECT target_id::text FROM kcm_resource_targets
		WHERE resource_kind='pod' LIMIT 1`).Scan(&podID); e == nil {
		env, err = call(podID)
		if err != nil {
			t.Fatal(err)
		}
		for _, f := range env.Findings {
			if f["section"] == "membership" {
				var kinds []string
				for _, r := range f["relations"].([]relationRow) {
					kinds = append(kinds, r.RelationKind)
				}
				t.Logf("pod 소속 관계: %v", kinds)
				var hasK8s bool
				for _, k := range kinds {
					if k == "cluster" || k == "direct_parent" {
						hasK8s = true
					}
				}
				if !hasK8s {
					t.Fatalf("pod 대상에 K8s 상위 관계 없음: %v", kinds)
				}
			}
		}
	}
}
