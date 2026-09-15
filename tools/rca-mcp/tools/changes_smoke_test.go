// 실 PG 스모크 — RCA_PG_DSN 없으면 skip. 파이프라인 [2]용 typed 자리
// (NewChangesFunc) 전용이다 — 조사자 표면은 list_changes로 자리 교체됐고
// (§10) 스모크도 listchanges_smoke_test.go로 분리됐다.
package tools

import (
	"context"
	"os"
	"testing"
	"time"
)

func TestChangesSmoke(t *testing.T) {
	dsn := os.Getenv("RCA_PG_DSN")
	if dsn == "" {
		t.Skip("RCA_PG_DSN 미설정 — 실 PG 스모크 생략")
	}
	db, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// 정책 배포가 실존하는 대상을 골라 그 시각을 포함하는 창으로 조회.
	var target string
	var at time.Time
	err = db.QueryRow(`SELECT target_ref, deployed_at FROM policy_deployments
		WHERE target_kind='target' ORDER BY deployed_at DESC LIMIT 1`).Scan(&target, &at)
	if err != nil {
		t.Fatalf("실 배포 행 조회: %v", err)
	}
	from, to := at.Add(-time.Hour), at.Add(time.Hour)

	// typed 자리([2]용): 변경이 잡히고 kind·ref 순서가 맞는지.
	evs, err := NewChangesFunc(db)(context.Background(), target, from, to)
	if err != nil {
		t.Fatal(err)
	}
	if len(evs) == 0 {
		t.Fatalf("배포 시각 포함 창인데 변경 0건: target=%s at=%s", target, at)
	}
	hasDeploy := false
	for _, ev := range evs {
		if ev.Kind == "policy_deploy" {
			hasDeploy = true
		}
		if ev.TargetID != target {
			t.Fatalf("귀속 오류: %+v", ev)
		}
	}
	if !hasDeploy {
		t.Fatalf("policy_deploy가 없음: %+v", evs)
	}
	t.Logf("typed: %d건 (첫 건: %+v)", len(evs), evs[0])

}
