// list_changes 실 PG 스모크 — RCA_PG_DSN 없으면 skip.
//
//	RCA_PG_DSN='postgres://lucida:lucida123@localhost:45432/lucida?sslmode=disable' \
//	  go test ./tools -run ListChangesSmoke -v
//
// §10의 핵심 계약을 실 데이터로 확인한다: 창 전역 반환(대상 없이도 동작) ·
// 정책 배포 사건 접기(338행 → 묶음) · allowlist 제외의 정직 표기 ·
// 대상 힌트가 필터가 아니라 정렬임 · 3축 표식 존재.
package tools

import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"
)

func TestListChangesSmoke(t *testing.T) {
	dsn := os.Getenv("RCA_PG_DSN")
	if dsn == "" {
		t.Skip("RCA_PG_DSN 미설정 — 실 PG 스모크 생략")
	}
	db, err := OpenPG(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// 변경이 실존하는 창을 잡는다 — 정책 배포 시각 기준.
	var target string
	var at time.Time
	if err := db.QueryRow(`SELECT target_ref, deployed_at FROM policy_deployments
		WHERE target_kind='target' ORDER BY deployed_at DESC LIMIT 1`).Scan(&target, &at); err != nil {
		t.Skipf("정책 배포 행 없음 — 스모크 생략: %v", err)
	}
	from := at.Add(-time.Hour).UTC().Format(time.RFC3339)
	to := at.Add(time.Hour).UTC().Format(time.RFC3339)
	tool := NewListChangesTool(db, at, at, nil)

	call := func(m map[string]any) Envelope {
		t.Helper()
		args, _ := json.Marshal(m)
		out, err := tool.Call(context.Background(), args)
		if err != nil {
			t.Fatalf("call %v: %v", m, err)
		}
		return out.(Envelope)
	}
	meta := func(env Envelope) Finding {
		t.Helper()
		for _, f := range env.Findings {
			if f["section"] == "summary_meta" {
				return f
			}
		}
		t.Fatal("summary_meta 없음")
		return nil
	}

	// ① 대상 없이 — 창 전역이 나와야 한다(get_changes는 target 필수였다).
	global := call(map[string]any{"from": from, "to": to})
	if len(global.Findings) < 2 {
		t.Fatalf("창 전역 조회가 비었다: %+v", global)
	}
	gm := meta(global)
	counts := gm["counts"].(map[string]any)
	if counts["total_raw"].(int) == 0 {
		t.Fatalf("원본 0행 — 창 선정 실패: %+v", gm)
	}
	t.Logf("전역: raw=%v folded=%v returned=%v excluded=%v",
		counts["total_raw"], counts["total_folded"], counts["returned"], counts["excluded"])

	// ② 정책 배포가 사건으로 접혔는가 — 338행이 아니라 묶음으로.
	var deploy Finding
	for _, f := range global.Findings {
		if f["kind"] == "policy_deploy" && f["sources"] != nil {
			deploy = f
			break
		}
	}
	if deploy == nil {
		t.Fatalf("정책 배포 사건이 없다: %+v", global.Findings)
	}
	if _, ok := deploy["targets_total"]; !ok {
		t.Fatalf("배포 사건에 targets_total이 없다(대상 목록 접기 미동작): %+v", deploy)
	}
	for _, axis := range []string{"scope", "correlation"} {
		if deploy[axis] == nil || deploy[axis] == "" {
			t.Fatalf("3축 표식 %s 누락: %+v", axis, deploy)
		}
	}
	if n, ok := deploy["targets_total"].(int); ok && n > lcTargetSample {
		if deploy["targets_truncated"] != true || deploy["targets_by_type"] == nil {
			t.Fatalf("대상 %d개인데 표본·유형 요약이 없다: %+v", n, deploy)
		}
	}
	t.Logf("배포 사건: %v (대상 %v, correlation=%v)", deploy["title"], deploy["targets_total"], deploy["correlation"])

	// ③ 대상 힌트는 필터가 아니다 — 결과가 줄어들면 안 된다.
	hinted := call(map[string]any{"from": from, "to": to, "target": target})
	if len(hinted.Findings) != len(global.Findings) {
		t.Fatalf("힌트가 필터로 동작했다: 전역 %d건 → 힌트 %d건",
			len(global.Findings), len(hinted.Findings))
	}
	if hinted.Findings[0]["hint_target_included"] != true {
		t.Fatalf("힌트 귀속분이 맨 위가 아니다: %+v", hinted.Findings[0])
	}

	// ④ allowlist 제외는 숨기지 않는다 — 건수·종류가 메타에 실린다.
	hm := meta(hinted)
	if ex := hm["counts"].(map[string]any)["excluded"].(int); ex > 0 {
		if hm["excluded_detail"] == nil {
			t.Fatalf("제외 %d행인데 내역이 없다: %+v", ex, hm)
		}
		// include_audit_activity=true면 제외분이 사건으로 올라온다.
		opened := call(map[string]any{"from": from, "to": to, "include_audit_activity": true})
		om := meta(opened)
		if om["counts"].(map[string]any)["excluded"].(int) != 0 {
			t.Fatalf("include_audit_activity=true인데 여전히 제외됨: %+v", om)
		}
		if len(opened.Findings) <= len(global.Findings) {
			t.Fatalf("열람 모드인데 사건이 안 늘었다: %d → %d",
				len(global.Findings), len(opened.Findings))
		}
		t.Logf("제외 %d행 → 열람 시 사건 %d → %d건", ex, len(global.Findings), len(opened.Findings))
	}

	// ⑤ 기본 창 — 인자 없이 호출해도 [firstEvent-24h, lastEvent]로 동작.
	def := call(map[string]any{})
	dm := meta(def)
	w := dm["window"].(map[string]any)
	if !strings.Contains(w["basis"].(string), "발단-24h") {
		t.Fatalf("기본 창 기준점이 발단이 아니다: %v", w)
	}
	if dm["coverage_note"] == nil {
		t.Fatalf("확인 경계 고지가 없다: %+v", dm)
	}

	// ⑥ policy 원천의 현재상태 한계는 항상 실린다.
	notes := dm["source_notes"].(map[string]any)
	if !strings.Contains(notes["policy_deployments"].(string), "current_state_not_history") {
		t.Fatalf("policy 한계 표기 누락: %v", notes)
	}

	// ⑦ 인자 검증 — 잘못된 UUID는 복구 정보를 담은 오류.
	badArgs, _ := json.Marshal(map[string]any{"target": "db:postgresql"})
	if _, err := tool.Call(context.Background(), badArgs); err == nil || !strings.Contains(err.Error(), "순수 UUID") {
		t.Fatalf("UUID 오류에 복구 정보가 아님: %v", err)
	}
}
