// 파이프라인 [2]용 typed 변경 조회 — "뭐가 바뀌었나"의 대상 지목
// 계약(도구 계약 §3). 변경 3테이블을 합산:
// policy_deployments(정책 배포) + collectors(수집기 등록 — created_at만,
// updated_at은 매 폴마다 last_collected_at과 함께 갱신되는 노이즈라
// targets.updated_at과 같은 함정) + change_history(감사 이력 — 대상
// 귀속은 name 컬럼의 이름 문자열뿐이라 targets의 name/display_name/
// address 일치로 잇는다).
//
// 조사자용 봉투 도구는 list_changes(listchanges.go)가 승계했다(§10
// 자리 교체) — 이 파일은 파이프라인 [2]용 typed 함수(NewChangesFunc)
// 전용이다. 두 경로는 조회를 공유하지 않는다: [2]는 대상 지목 계약,
// list_changes는 창 전역 + 사건 단위 접기라 "변경"의 정의가 다르다
// (§10.9 이월 — [2] 승격은 발동 조건부).
package tools

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/pipeline"
)

// NewChangesFunc는 파이프라인 [2] ScanChanges가 소비하는 결정론 자리다.
func NewChangesFunc(db *sql.DB) pipeline.GetChangesFunc {
	return func(ctx context.Context, target string, from, to time.Time) ([]pipeline.ChangeEvent, error) {
		if !uuidRe.MatchString(target) {
			return nil, fmt.Errorf("%q는 target_id가 아님 — 순수 UUID 필요", target)
		}
		evs, _, err := queryChanges(ctx, db, target, from, to)
		return evs, err
	}
}

// queryChanges는 3원천을 시간창 [from, to)로 합산한다. 반환 refs는
// 이벤트와 같은 순서의 원본 참조다.
func queryChanges(ctx context.Context, db *sql.DB, target string, from, to time.Time) ([]pipeline.ChangeEvent, []string, error) {
	var evs []pipeline.ChangeEvent
	var refs []string

	// 1. 정책 배포 — target_ref가 대상 UUID(현재 target_kind='target'뿐).
	rows, err := db.QueryContext(ctx, `
		SELECT pd.id::text, pd.deployed_at, pd.role, coalesce(pt.name, pd.template_id::text)
		FROM policy_deployments pd LEFT JOIN policy_templates pt ON pt.id = pd.template_id
		WHERE pd.target_kind = 'target' AND pd.target_ref = $1
		  AND pd.deployed_at >= $2 AND pd.deployed_at < $3`, target, from, to)
	if err != nil {
		return nil, nil, fmt.Errorf("변경 조회 정책 배포 조회: %w", pgErr(err))
	}
	if err := scanChangeRows(rows, "pg:policy_deployments:", func(id string, at time.Time, a, b string) (pipeline.ChangeEvent, string) {
		return pipeline.ChangeEvent{Kind: "policy_deploy", At: at, TargetID: target,
			Detail: fmt.Sprintf("정책 배포(%s): %s", a, b)}, id
	}, &evs, &refs); err != nil {
		return nil, nil, err
	}

	// 2. 수집기 등록 — created_at만 변경 신호다(파일 머리 주석).
	rows, err = db.QueryContext(ctx, `
		SELECT id::text, created_at, kind, '' FROM collectors
		WHERE target_id = $1 AND created_at >= $2 AND created_at < $3`, target, from, to)
	if err != nil {
		return nil, nil, fmt.Errorf("변경 조회 수집기 조회: %w", pgErr(err))
	}
	if err := scanChangeRows(rows, "pg:collectors:", func(id string, at time.Time, a, _ string) (pipeline.ChangeEvent, string) {
		return pipeline.ChangeEvent{Kind: "collector_update", At: at, TargetID: target,
			Detail: "수집기 등록: " + a}, id
	}, &evs, &refs); err != nil {
		return nil, nil, err
	}

	// 3. 감사 이력 — name 문자열 귀속(대상의 name/display_name/address 일치).
	rows, err = db.QueryContext(ctx, `
		SELECT ch.id::text, ch.occurred_at,
		       ch.category || ' — ' || ch.operation,
		       coalesce(nullif(ch.after_summary, ''), ch.name)
		FROM change_history ch
		WHERE ch.occurred_at >= $2 AND ch.occurred_at < $3
		  AND ch.name IN (SELECT unnest(ARRAY[name, coalesce(display_name, ''), coalesce(address, '')])
		                  FROM targets WHERE id = $1::uuid)`, target, from, to)
	if err != nil {
		return nil, nil, fmt.Errorf("변경 조회 감사 이력 조회: %w", pgErr(err))
	}
	if err := scanChangeRows(rows, "pg:change_history:", func(id string, at time.Time, a, b string) (pipeline.ChangeEvent, string) {
		kind := "audit"
		switch {
		case strings.HasPrefix(a, "구성 대상"):
			kind = "target_update"
		case strings.HasPrefix(a, "수집기"):
			kind = "collector_update"
		}
		return pipeline.ChangeEvent{Kind: kind, At: at, TargetID: target,
			Detail: fmt.Sprintf("%s: %s", a, b)}, id
	}, &evs, &refs); err != nil {
		return nil, nil, err
	}

	return evs, refs, nil
}

// scanChangeRows는 (id, at, a, b) 4열 결과를 이벤트로 변환한다.
func scanChangeRows(rows *sql.Rows, refPrefix string,
	mk func(id string, at time.Time, a, b string) (pipeline.ChangeEvent, string),
	evs *[]pipeline.ChangeEvent, refs *[]string) error {
	defer rows.Close()
	for rows.Next() {
		var id, a, b string
		var at time.Time
		if err := rows.Scan(&id, &at, &a, &b); err != nil {
			return fmt.Errorf("변경 조회 행 읽기: %w", pgErr(err))
		}
		ev, rid := mk(id, at.UTC(), a, b)
		*evs = append(*evs, ev)
		*refs = append(*refs, refPrefix+rid)
	}
	return pgErr(rows.Err())
}
