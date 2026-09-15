// expand_topology 실 저장소 스모크 — RCA_PG_DSN + RCA_CH_URL 없으면 skip.
//
//	RCA_PG_DSN='postgres://lucida:lucida123@192.168.230.119:15432/lucida?sslmode=disable' \
//	RCA_CH_URL='http://lucida:lucida123@192.168.230.119:18123' \
//	go test ./tools/ -run TestExpandTopologySmoke -v
package tools

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func TestExpandTopologySmoke(t *testing.T) {
	dsn, chURL := os.Getenv("RCA_PG_DSN"), os.Getenv("RCA_CH_URL")
	if dsn == "" || chURL == "" {
		t.Skip("RCA_PG_DSN·RCA_CH_URL 미설정 — 생략")
	}
	pg, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer pg.Close()
	ch := chFromEnv(t)

	// 트레이스가 실제로 도는 앱을 고른다 — 이름만 보고 고르면 계측이
	// 없는 앱이 걸려 0건 경로만 검증하게 된다(첫 스모크 실측).
	now := time.Now().UTC()
	rows, _, err := ch.Query(context.Background(), `
		SELECT resource_attributes['lucida.target_id'] AS tid, count() AS n
		FROM otel_traces_local
		WHERE timestamp >= {from:DateTime64(9)} AND resource_attributes['lucida.target_id'] != ''
		GROUP BY tid ORDER BY n DESC LIMIT 5`,
		map[string]string{"from": chTime(now.Add(-time.Hour))})
	if err != nil || len(rows) == 0 {
		t.Skipf("창 안에 트레이스 없음: %v", err)
	}
	var app string
	for _, r := range rows {
		var exists bool
		if pg.QueryRow(`SELECT true FROM targets WHERE id = $1::uuid`, chStr(r["tid"])).Scan(&exists) == nil {
			app = chStr(r["tid"])
			break
		}
	}
	if app == "" {
		t.Skip("트레이스 대상이 명부에 없음")
	}
	tool := NewExpandTopologyTool(pg, ch, now.Add(-time.Hour), now, func() time.Time { return now })

	// 가짜 노드 ID 거부 — 구 topology의 db:postgresql류.
	if _, err := tool.Call(context.Background(), []byte(`{"target":"db:postgresql"}`)); err == nil ||
		!strings.Contains(err.Error(), "가짜 노드 ID") {
		t.Fatalf("가짜 노드 ID 복구 안내 없음: %v", err)
	}

	args, _ := json.Marshal(map[string]any{"target": app, "hops": 2})
	out, err := tool.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화: %v", err)
	}
	t.Logf("status=%s %s", env.Status, env.Summary)

	var nodes, edges int
	var cov Finding
	kinds := map[string]int{}
	for _, f := range env.Findings {
		switch f["kind"] {
		case "node":
			nodes++
		case "edge":
			edges++
			if e, ok := f["edge"].(*etEdge); ok {
				kinds[e.Kind]++
			}
		case "coverage":
			cov = f
		}
	}
	if nodes == 0 || edges == 0 {
		t.Fatalf("노드 %d·간선 %d — 트레이스 도는 앱인데 조립 실패: %s", nodes, edges, env.Summary)
	}
	// 앱에서 출발해도 hosted_on이 물리 층으로 잇는다(hop_cost=0).
	if kinds[etKindHosted] == 0 {
		t.Errorf("hosted_on 간선 없음 — 앱↔물리 다리가 끊겼다: %v", kinds)
	}
	if cov == nil {
		t.Fatal("coverage finding 없음 — 원천별 조회 시도 여부는 침묵 금지다")
	}
	// 소켓은 조회했으나 앱 소유권을 못 만든다는 사실을 반드시 밝힌다.
	src, _ := cov["sources"].(map[string]string)
	if !strings.Contains(src["socket"], "checked_unusable_for_app_ownership") {
		t.Fatalf("소켓 coverage 표기 없음: %v", src)
	}
	// target_id는 계약이 아니라 배포 규약 — coverage로 노출돼야 한다.
	ic, _ := cov["identity_coverage"].(map[string]any)
	if ic == nil || ic["with_target_id"] == nil {
		t.Fatalf("identity coverage 없음: %v", cov)
	}
	t.Logf("노드 %d · 간선 %d · 종류 %v", nodes, edges, kinds)

	// 상한 표기는 미검증임을 밝혀야 한다(현 규모의 10배 여유폭).
	lim, _ := cov["limits"].(map[string]any)
	if note, _ := lim["note"].(string); !strings.Contains(note, "미검증") {
		t.Fatalf("상한 미검증 표기 없음: %v", lim)
	}
}
