package pipeline

import (
	"context"
	"reflect"
	"testing"
	"time"
)

// [2]는 Triage 산출물의 증상·후보 대상 전부를, 시작 직전까지 넓힌
// 시간창으로 조회한다.
func TestScanChanges(t *testing.T) {
	tri, err := Triage(context.Background(), sampleSeed(), sampleMeta)
	if err != nil {
		t.Fatal(err)
	}

	var asked []string
	var gotFrom, gotTo time.Time
	get := func(ctx context.Context, target string, from, to time.Time) ([]ChangeEvent, error) {
		asked = append(asked, target)
		gotFrom, gotTo = from, to
		switch target {
		case "db-1":
			return []ChangeEvent{{Kind: "policy_deploy", At: trTs(-30), TargetID: "db-1", Detail: "인덱스 정책 배포"}}, nil
		case "net-1":
			return []ChangeEvent{{Kind: "audit", At: trTs(-30), TargetID: "net-1", Detail: "ACL 수정"}}, nil
		case "app-1":
			// 첫 증상(trTs(0)) 이후의 변경 — 발단의 원인일 수 없다.
			return []ChangeEvent{{Kind: "policy_deploy", At: trTs(5), TargetID: "app-1", Detail: "늦은 배포"}}, nil
		}
		return nil, nil
	}

	r, err := ScanChanges(context.Background(), tri, time.Hour, get)
	if err != nil {
		t.Fatalf("ScanChanges 실패: %v", err)
	}

	// 조회 범위 = 증상(app-1, db-1, cache-1) + 후보(host-9, net-1, stor-1).
	want := []string{"app-1", "cache-1", "db-1", "host-9", "net-1", "stor-1"}
	if !reflect.DeepEqual(r.Targets, want) || !reflect.DeepEqual(asked, want) {
		t.Errorf("Targets = %v (호출 %v), want %v", r.Targets, asked, want)
	}

	// 시간창 = [From-lookback, To].
	if !gotFrom.Equal(trTs(0).Add(-time.Hour)) || !gotTo.Equal(trTs(10)) {
		t.Errorf("window = [%v, %v], want [%v, %v]", gotFrom, gotTo, trTs(0).Add(-time.Hour), trTs(10))
	}
	if !r.From.Equal(gotFrom) || !r.To.Equal(gotTo) {
		t.Errorf("결과 window 기록 어긋남: [%v, %v]", r.From, r.To)
	}

	// 변경은 시각순, 동시각은 대상순 — db-1이 net-1보다 앞.
	if len(r.Changes) != 2 || r.Changes[0].TargetID != "db-1" || r.Changes[1].TargetID != "net-1" {
		t.Errorf("Changes 정렬 어긋남: %+v", r.Changes)
	}
	// 첫 증상 이후의 변경은 원인 후보 재료가 아니다 — 기록으로 격리(§14 ③).
	if len(r.LateChanges) != 1 || r.LateChanges[0].TargetID != "app-1" {
		t.Errorf("LateChanges = %+v, want app-1 늦은 배포", r.LateChanges)
	}
}

// 변경이 하나도 없어도 성립한다 — "봤는데 없었다"가 Targets에 남는다.
func TestScanChangesEmpty(t *testing.T) {
	tri, err := Triage(context.Background(), sampleSeed(), sampleMeta)
	if err != nil {
		t.Fatal(err)
	}
	r, err := ScanChanges(context.Background(), tri, time.Hour, func(context.Context, string, time.Time, time.Time) ([]ChangeEvent, error) {
		return nil, nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(r.Changes) != 0 || len(r.Targets) != 6 {
		t.Errorf("빈 결과 기대: changes=%d targets=%d", len(r.Changes), len(r.Targets))
	}
}

func TestScanChangesRequiresGetter(t *testing.T) {
	if _, err := ScanChanges(context.Background(), TriageResult{}, time.Hour, nil); err == nil {
		t.Fatal("getter nil인데 수락함")
	}
}
