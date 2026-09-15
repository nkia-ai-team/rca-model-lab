// compare_peers 실 PG+VM 스모크 — RCA_PG_DSN·RCA_VM_URL 없으면 skip.
//
//	RCA_PG_DSN='postgres://lucida:lucida123@localhost:45432/lucida?sslmode=disable' \
//	RCA_VM_URL='http://localhost:8428' go test ./tools -run ComparePeersSmoke -v
//
// §11의 계약을 실 데이터로 확인한다: 또래 열거가 typed 근거를 달고 나오는지 ·
// 미관측 또래가 판정 모집단에서 빠지고 분모가 남는지 · 판정이 봉투 v2에
// 얹히는지. 판정 결과 자체(alone/shared)는 데이터에 달렸으므로 고정하지
// 않는다 — 규칙 고정은 순수 단위(comparepeers_test.go)의 몫이다.
package tools

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"
)

func TestComparePeersSmoke(t *testing.T) {
	dsn, vmURL := os.Getenv("RCA_PG_DSN"), os.Getenv("RCA_VM_URL")
	if dsn == "" || vmURL == "" {
		t.Skip("RCA_PG_DSN·RCA_VM_URL 미설정 — 실 스모크 생략")
	}
	db, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	vm := &VM{BaseURL: vmURL}

	// 서비스 그룹에 동형 또래가 실재하는 대상을 고른다(§11.4 ① 경로).
	var target string
	err = db.QueryRow(`
		SELECT m1.target_id::text FROM asset_tree_members m1
		JOIN targets t1 ON t1.id = m1.target_id
		WHERE m1.kind = 'service'
		  AND EXISTS (
			SELECT 1 FROM asset_tree_members m2
			JOIN targets t2 ON t2.id = m2.target_id AND t2.type = t1.type
			WHERE m2.kind = 'service' AND m2.scope = m1.scope
			  AND m2.group_id = m1.group_id AND m2.target_id <> m1.target_id)
		LIMIT 1`).Scan(&target)
	if err != nil {
		t.Skipf("동형 또래를 가진 서비스 그룹 대상 없음 — 스모크 생략: %v", err)
	}

	// 이 대상에 실제로 수집되는 지표 하나를 고른다.
	now := time.Now().UTC()
	names, err := vm.LabelValues(context.Background(), "__name__", `{target_id="`+target+`"}`, now.Add(-6*time.Hour), now)
	if err != nil || len(names) == 0 {
		t.Skipf("대상 %s의 지표 인벤토리 없음 — 스모크 생략(err=%v)", target, err)
	}
	metric := names[0]

	from := now.Add(-30 * time.Minute)
	tool := NewComparePeersTool(db, vm, from, func() time.Time { return now })
	args, _ := json.Marshal(map[string]string{
		"target": target, "metric": metric,
		"from": from.Format(time.RFC3339), "to": now.Format(time.RFC3339),
	})
	out, err := tool.Call(context.Background(), args)
	if err != nil {
		t.Fatalf("compare_peers 호출 실패: %v", err)
	}
	env, ok := out.(Envelope)
	if !ok {
		t.Fatalf("봉투가 아닌 반환: %T", out)
	}
	t.Logf("compare_peers(%s, %s): status=%s\n%s", target, metric, env.Status, env.Summary)

	switch env.Status {
	case "normal", "anomalous":
	case "no_data":
		if env.NoDataReason == "" {
			t.Error("no_data인데 사유 없음 — 봉투 v2 위반")
		}
		if env.NoDataReason == NoDataZeroObservations {
			t.Error("zero_observations 사용 — 이 도구는 미관측 사유를 추측하지 않는다(§11.5)")
		}
		return
	default:
		t.Fatalf("status 3값 밖: %s", env.Status)
	}

	var verdict, peerSetF Finding
	for _, f := range env.Findings {
		switch f["class"] {
		case "verdict":
			verdict = f
		case "peer_set":
			peerSetF = f
		}
	}
	if verdict == nil || peerSetF == nil {
		t.Fatalf("verdict·peer_set 절 누락: %+v", env.Findings)
	}

	// 또래 근거는 산문이 아니라 typed 값이어야 한다(§11.4).
	basis, _ := peerSetF["basis"].(string)
	if basis != "service_group" && basis != "type_fallback" {
		t.Errorf("basis가 typed 값이 아님: %v", peerSetF["basis"])
	}
	if conf, _ := peerSetF["confidence"].(string); conf != "high" && conf != "low" {
		t.Errorf("confidence 누락/오류: %v", peerSetF["confidence"])
	}

	// 분모가 반드시 보여야 한다 — "또래 N개 중 판정에 쓴 건 M개"(§11.5).
	elig, okE := verdict["peers_eligible"].(int)
	comp, okC := verdict["peers_compared"].(int)
	if !okE || !okC {
		t.Fatalf("또래 분모 필드 누락: %+v", verdict)
	}
	if comp > elig {
		t.Errorf("판정 모집단(%d)이 후보(%d)보다 큼", comp, elig)
	}
	if comp < elig {
		if verdict["peers_excluded"] == nil {
			t.Error("제외된 또래가 있는데 peers_excluded 계수 없음 — 분모가 사라졌다")
		}
		if verdict["comparison_scope"] != "observed_peers_only" {
			t.Errorf("부분 커버리지인데 scope=%v", verdict["comparison_scope"])
		}
	}
	t.Logf("또래 후보 %d · 판정 %d · 이탈 %v · verdict=%v (근거 %s)",
		elig, comp, verdict["peers_deviating"], verdict["verdict"], basis)
}
