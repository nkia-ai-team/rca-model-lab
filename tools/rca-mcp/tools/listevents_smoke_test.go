// list_events 실 저장소 스모크 — RCA_CH_URL + RCA_PG_DSN 없으면 skip.
// K8s 합성 세 갈래(§8 결정 3)를 실 명부·실 kcm_events로 검증한다.
// 실행(격리 세트):
//
//	RCA_CH_URL='http://lucida:lucida123@localhost:38123' \
//	RCA_PG_DSN='postgres://lucida:lucida123@localhost:45432/lucida?sslmode=disable' \
//	go test ./tools -run TestListEventsK8sSmoke -v
package tools

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestListEventsK8sSmoke(t *testing.T) {
	chURL, dsn := os.Getenv("RCA_CH_URL"), os.Getenv("RCA_PG_DSN")
	if chURL == "" || dsn == "" {
		t.Skip("RCA_CH_URL·RCA_PG_DSN 미설정 — 생략")
	}
	ch := chFromEnv(t)
	pg, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()

	// kcm 이벤트가 실존하는 (클러스터, pod) 하나를 고른다.
	rows, _, err := ch.Query(context.Background(), `
		SELECT target_id, namespace, object_name, max(timestamp) AS at
		FROM kcm_events_local
		WHERE lower(object_kind) = 'pod'
		GROUP BY target_id, namespace, object_name ORDER BY at DESC LIMIT 1`, nil)
	if err != nil || len(rows) == 0 {
		t.Skipf("kcm_events 없음 — K8s 합성 스모크 생략 (%v)", err)
	}
	cluster := rows[0]["target_id"].(string)
	ns, pod := rows[0]["namespace"].(string), rows[0]["object_name"].(string)
	at, err := time.Parse("2006-01-02 15:04:05.999999999", rows[0]["at"].(string))
	if err != nil {
		t.Fatal(err)
	}
	from, to := at.Add(-2*time.Hour).Format(time.RFC3339), at.Add(time.Minute).Format(time.RFC3339)

	tool := NewListEventsTool(ch, pg, nil)
	call := func(target string) Envelope {
		t.Helper()
		args, _ := json.Marshal(map[string]string{"target": target, "from": from, "to": to})
		out, err := tool.Call(context.Background(), args)
		if err != nil {
			t.Fatal(err)
		}
		return out.(Envelope)
	}

	// 갈래 ① 클러스터 대상 — kcm 합성 + namespace 구획.
	env := call(cluster)
	var k8sSeen bool
	for _, f := range env.Findings {
		if f["section"] == "k8s" {
			k8sSeen = true
			if f["label"] != "fact" || f["source"] != "kcm" {
				t.Fatalf("kcm 표식 이상: %+v", f)
			}
		}
	}
	if !k8sSeen {
		t.Fatalf("클러스터 조회에 kcm 합성 없음: %s", env.Summary)
	}
	t.Logf("cluster: %s", env.Summary)

	// 갈래 ② 승격 pod 대상 — 이름 매칭 분만, match_basis 표식.
	var podTarget string
	err = pg.QueryRow(`SELECT target_id::text FROM kcm_resource_targets
		WHERE resource_kind = 'pod' AND resource_key = $1 LIMIT 1`, ns+"/"+pod).Scan(&podTarget)
	if err != nil {
		t.Skipf("승격 pod 대상 %s/%s 명부에 없음 — 갈래 ② 생략: %v", ns, pod, err)
	}
	env = call(podTarget)
	for _, f := range env.Findings {
		if f["section"] != "k8s" {
			continue
		}
		if f["name"] != pod {
			t.Fatalf("pod 매칭 새어나감 — %v (기대 %s)", f["name"], pod)
		}
		if f["match_basis"] == nil || f["match_confidence"] == nil {
			t.Fatalf("이름 매칭 확신도 표식 누락: %+v", f)
		}
	}
	t.Logf("pod(%s): %s", pod, env.Summary)

	// 갈래 ③ 비K8s 대상 — kcm 조회 안 함 표시.
	var srvTarget string
	err = pg.QueryRow(`SELECT id::text FROM targets WHERE type IN ('server','database','application') LIMIT 1`).Scan(&srvTarget)
	if err != nil {
		t.Skipf("비K8s 대상 없음 — 갈래 ③ 생략: %v", err)
	}
	env = call(srvTarget)
	for _, f := range env.Findings {
		if f["section"] == "summary_meta" {
			src := f["sources"].(map[string]any)
			if src["kcm_events"] != "조회 안 함" {
				t.Fatalf("비K8s 대상의 kcm 표시 이상: %+v", src)
			}
		}
	}
	t.Logf("non-k8s: %s", env.Summary)
}
