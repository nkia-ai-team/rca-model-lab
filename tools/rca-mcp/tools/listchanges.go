// list_changes — "이 창에 무엇이 바뀌었나"의 재설계 표면
// (스펙 §3.3 ⑤ + §10 구현 설계, 2026-07-29 확정). get_changes 자리 교체.
//
//   - 대상은 필터가 아니라 정렬 힌트다(§10.3): 창 전역을 반환하고
//     target이 오면 귀속분을 위 구획으로 올린다. 대상으로 거르면
//     이웃 대상 변경(정책 배포 1건이 26대상)과 대상 개념이 없는
//     전역 사건(자산 트리·계정)에 구조적으로 못 닿는다.
//   - 감출 근거는 (category, operation) 읽기 전용 allowlist뿐이다
//     (§10.4): action=” 은 판별자가 아니다 — 파괴 작업 SESSION_KILL이
//     action 미기록으로 남는다(생산자 소스 실측). 미지는 노출 방향.
//   - 원천 3곳은 같은 사건을 다른 단위로 적는다(§10.5): 사건 단위로
//     교차 접고, 짝은 후보가 정확히 1개일 때만 잇는다(unique_inferred).
//     0개는 unmatched, 2개 이상은 ambiguous — 억지 1:1을 만들지 않는다.
//   - provenance는 3축이다: scope / correlation / 대상별 match_basis.
//     정책 사건은 ch 연결이 추정이어도 그 안의 target_ref는 exact_id다.
//   - 기본 창은 [firstEvent-24h, lastEvent]: 변경은 원인 선행사건이라
//     인시던트 창 안에 없는 것이 일반형이다(§10.6).
package tools

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

const (
	lcDefaultLookback = 24 * time.Hour // firstEvent 기준 소급(§10.6)
	lcPairWindow      = 2 * time.Second
	lcDefaultLimit    = 40 // 사건 반환 상한(접기·dedup 후 — §10.7)
	lcTargetSample    = 5  // 사건당 대상 UUID 표본 상한
)

// lcReadOnlyOps는 감출 근거가 되는 유일한 목록이다(§10.4) —
// category × operation 짝으로 검증된 조회 전용 작업. 여기 없으면
// action이 비어 있어도 노출한다. SESSION_KILL이 여기 없는 것이 요점.
var lcReadOnlyOps = map[string]map[string]bool{
	"데이터베이스 Live-SQL": {
		"CURRENT_SESSION_AND_LOCK": true, "CURRENT_SESSION_SUMMARY": true,
		"PARAMETER": true, "BACKEND_MEMORY_INFO": true, "SHARED_MEMORY_INFO": true,
		"ORACLE_SCHEMA": true, "ORACLE_USER": true, "ORACLE_TABLE": true,
		"ORACLE_PARAMETER": true,
	},
}

func lcIsReadOnly(category, operation string) bool {
	return lcReadOnlyOps[category][operation]
}

// lcTarget은 사건이 닿은 대상 하나다. Basis는 귀속 근거(§10.5 3축 중
// 대상별 축) — exact_id는 표에 UUID가 직접 있는 4경로, name은 연성.
type lcTarget struct {
	ID    string `json:"target_id"`
	Name  string `json:"name,omitempty"`
	Type  string `json:"type,omitempty"`
	Basis string `json:"match_basis"` // exact_id | name
}

// lcEvent는 접기 후의 고유 사건 하나다.
type lcEvent struct {
	At          time.Time
	FirstAt     time.Time
	LastAt      time.Time
	Count       int // 접힌 원본 행 수
	Kind        string
	Title       string
	Scope       string // target | global | control_plane
	Correlation string // exact | unique_inferred | ambiguous | unmatched
	DeltaMs     *int64
	Candidates  []string // ambiguous일 때 후보 refs
	Targets     []lcTarget
	Sources     []string
	Refs        []string
	Notes       []string
	Variants    []map[string]any // 요약 동일 접기의 변형들(§10.5-5)
	MatchKey    string           // 짝짓기 술어가 쓰는 값(수집기 = collectors.kind)
}

// lcInventory는 대상 명부 조회 결과다 — 귀속 5경로가 공유한다.
type lcInventory struct {
	byID   map[string]lcTargetMeta
	byName map[string]string // name/display_name/address → id
}

type lcTargetMeta struct{ Name, Type string }

func lcLoadInventory(ctx context.Context, pg *sql.DB) (lcInventory, error) {
	inv := lcInventory{byID: map[string]lcTargetMeta{}, byName: map[string]string{}}
	rows, err := pg.QueryContext(ctx, `SELECT id::text, name, coalesce(display_name,''), coalesce(address,''), type::text FROM targets`)
	if err != nil {
		return inv, fmt.Errorf("list_changes 대상 명부 조회: %w", pgErr(err))
	}
	defer rows.Close()
	for rows.Next() {
		var id, name, disp, addr, typ string
		if err := rows.Scan(&id, &name, &disp, &addr, &typ); err != nil {
			return inv, fmt.Errorf("list_changes 대상 명부 행: %w", pgErr(err))
		}
		inv.byID[id] = lcTargetMeta{Name: name, Type: typ}
		for _, k := range []string{name, disp, addr} {
			if k != "" {
				if _, dup := inv.byName[k]; !dup {
					inv.byName[k] = id
				}
			}
		}
	}
	return inv, pgErr(rows.Err())
}

// resolve는 UUID를 대상으로 푼다(exact_id 경로).
func (inv lcInventory) resolve(id, basis string) (lcTarget, bool) {
	m, ok := inv.byID[id]
	if !ok {
		return lcTarget{}, false
	}
	return lcTarget{ID: id, Name: m.Name, Type: m.Type, Basis: basis}, true
}

// NewListChangesTool은 list_changes 도구를 만든다. firstEvent·lastEvent는
// seed의 인시던트 창 — 기본 창 [firstEvent-24h, lastEvent]의 재료다
// (§10.6). lastEvent가 zero면 now를 쓰고 window_basis로 밝힌다.
func NewListChangesTool(pg *sql.DB, firstEvent, lastEvent time.Time, nowFn func() time.Time) llm.Tool {
	if nowFn == nil {
		nowFn = time.Now
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]string{"type": "string",
				"description": "target_id(UUID) — 선택. 필터가 아니라 정렬 힌트다(주면 귀속분이 위로, 나머지도 함께 반환)"},
			"from": map[string]string{"type": "string",
				"description": "선택. UTC RFC3339 또는 now-24h. 기본 = 인시던트 발단 -24시간"},
			"to": map[string]string{"type": "string",
				"description": "선택. UTC RFC3339 또는 now. 기본 = 인시던트 창 끝(없으면 now)"},
			"include_audit_activity": map[string]any{"type": "boolean",
				"description": "기본 false. true면 조회 전용 감사 기록(DB 화면 조회 등)까지 포함 — 접기는 풀지 않는다"},
			"limit": map[string]any{"type": "integer",
				"description": fmt.Sprintf("선택. 반환 사건 상한(기본 %d, 접기 후 기준)", lcDefaultLimit)},
		},
	})
	return llm.Tool{
		Name: "list_changes",
		Description: "시간창 안에 일어난 변경 전부 — 정책 배포·수집기 등록·구성 변경·자산 트리 편집·계정 변경. " +
			"대상은 선택이며 필터가 아니라 정렬 힌트다(이웃 대상 변경이 원인인 경우가 흔해 창 전역을 반환한다). " +
			"기본 창은 발단 기준 24시간 소급 — 배포는 새벽, 발현은 오전인 축적형 원인을 잡기 위해서다.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct {
				Target               string `json:"target"`
				From                 string `json:"from"`
				To                   string `json:"to"`
				IncludeAuditActivity bool   `json:"include_audit_activity"`
				Limit                int    `json:"limit"`
			}
			if len(args) > 0 {
				if err := json.Unmarshal(args, &in); err != nil {
					return nil, fmt.Errorf(`인자 오류: 전부 선택이다 — {"target": "<uuid>", "from": "<RFC3339|now-24h>", "to": "<RFC3339|now>", "include_audit_activity": false, "limit": 40}`)
				}
			}
			if in.Target != "" && !uuidRe.MatchString(in.Target) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — target_id는 순수 UUID다(접두 없음). 대상 없이 호출하면 창 전역을 본다", in.Target)
			}
			now := nowFn().UTC()

			// 창 결정(§10.6) — 기준점은 to가 아니라 firstEvent다.
			basis := []string{}
			var fromT, toT time.Time
			if in.To != "" {
				t, err := parseFlexTime(in.To, now)
				if err != nil {
					return nil, fmt.Errorf("to 시간 오류: %v", err)
				}
				toT, basis = t, append(basis, "to=인자")
			} else if !lastEvent.IsZero() {
				toT, basis = lastEvent.UTC(), append(basis, "to=인시던트 창 끝(seed)")
			} else {
				toT, basis = now, append(basis, "to=now(라이브)")
			}
			if in.From != "" {
				t, err := parseFlexTime(in.From, now)
				if err != nil {
					return nil, fmt.Errorf("from 시간 오류: %v", err)
				}
				fromT, basis = t, append(basis, "from=인자")
			} else if !firstEvent.IsZero() {
				fromT = firstEvent.UTC().Add(-lcDefaultLookback)
				basis = append(basis, "from=발단-24h")
			} else {
				fromT = toT.Add(-lcDefaultLookback)
				basis = append(basis, "from=to-24h(발단 미상)")
			}
			if !fromT.Before(toT) {
				return nil, fmt.Errorf("시간창 오류: from < to 필요 (계산된 값 from=%s to=%s)",
					fromT.Format(time.RFC3339), toT.Format(time.RFC3339))
			}
			limit := in.Limit
			if limit <= 0 {
				limit = lcDefaultLimit
			}
			return listChanges(ctx, pg, in.Target, fromT, toT, in.IncludeAuditActivity, limit,
				strings.Join(basis, ", "), firstEvent)
		},
	}
}

// lcRawCH는 change_history 원본 행이다.
type lcRawCH struct {
	ID        string
	At        time.Time
	Category  string
	Operation string
	Action    string
	Name      string
	After     string
	Detail    string
	TargetID  string // detail->>'target_id' (없으면 "")
}

func listChanges(ctx context.Context, pg *sql.DB, hint string, from, to time.Time, includeAudit bool,
	limit int, windowBasis string, firstEvent time.Time) (any, error) {
	if pg == nil {
		return nil, fmt.Errorf("list_changes: PG 미연결")
	}
	inv, err := lcLoadInventory(ctx, pg)
	if err != nil {
		return nil, err
	}
	sourceStatus := map[string]string{}

	// ── 1. 정책 배포: (template_id, role, target_kind, 초) 묶음 = 1사건.
	polEvents, polRaw, err := lcQueryPolicy(ctx, pg, inv, from, to)
	if err != nil {
		sourceStatus["policy_deployments"] = "조회 실패(" + beDetail(err) + ")"
	} else {
		sourceStatus["policy_deployments"] = "조회함"
	}

	// ── 2. 수집기 등록: (초, kind) 묶음 = 1사건. 1:1 짝짓기를 포기하고
	// collectors 쪽도 접는다 — ch 27행당 후보가 최대 17개인 실측(§10.5).
	colEvents, colRaw, err := lcQueryCollectors(ctx, pg, inv, from, to)
	if err != nil {
		sourceStatus["collectors"] = "조회 실패(" + beDetail(err) + ")"
	} else {
		sourceStatus["collectors"] = "조회함"
	}

	// ── 3. change_history 전량.
	chRows, err := lcQueryChangeHistory(ctx, pg, from, to)
	if err != nil {
		sourceStatus["change_history"] = "조회 실패(" + beDetail(err) + ")"
	} else {
		sourceStatus["change_history"] = "조회함"
	}
	rawTotal := polRaw + colRaw + len(chRows)

	// ── 4. 원천 교차 짝짓기 — 후보 1개일 때만 잇는다(§10.5-1).
	consumed := map[string]bool{}
	lcPair(polEvents, chRows, "정책 템플릿", func(_ *lcEvent, _ lcRawCH) bool { return true }, consumed)
	lcPair(colEvents, chRows, "수집기", func(e *lcEvent, r lcRawCH) bool {
		return r.Name == e.MatchKey
	}, consumed)

	// ── 5. 남은 change_history 행 → 사건(요약 동일 접기 + 귀속).
	chEvents, excluded, exMeta := lcFoldChangeHistory(chRows, consumed, inv, includeAudit)

	events := append(append(polEvents, colEvents...), chEvents...)

	// ── 6. 정렬: 힌트 귀속 → 대상 → 전역 → 관제면, 구획 내 최신순.
	sort.SliceStable(events, func(i, j int) bool {
		bi, bj := lcBucket(events[i], hint), lcBucket(events[j], hint)
		if bi != bj {
			return bi < bj
		}
		if !events[i].At.Equal(events[j].At) {
			return events[i].At.After(events[j].At)
		}
		return lcStableKey(events[i]) < lcStableKey(events[j]) // 동시각 결정론
	})

	foldedTotal := len(events)
	truncated := foldedTotal > limit
	if truncated {
		events = events[:limit]
	}

	// ── 7. 봉투 조립.
	findings := make([]Finding, 0, len(events)+1)
	var refs []string
	hintHits := 0
	for _, e := range events {
		f, hit := lcFinding(e, hint, inv)
		if hit {
			hintHits++
		}
		findings = append(findings, f)
		refs = append(refs, e.Refs...)
	}

	meta := Finding{
		"section":      "summary_meta",
		"window":       map[string]any{"from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339), "basis": windowBasis},
		"counts":       map[string]any{"total_raw": rawTotal, "total_folded": foldedTotal, "returned": len(events), "excluded": excluded},
		"sources":      sourceStatus,
		"source_notes": map[string]any{"policy_deployments": "current_state_not_history — 현재 남아 있으며 deployed_at이 이 창에 든 링크일 뿐 당시 부착 상태가 아니다. 재배포는 기존 링크를 지우고 새로 넣으므로 이 원천의 0건으로 '과거에 배포가 없었다'를 판정하면 안 된다"},
	}
	if len(exMeta) > 0 {
		meta["excluded_detail"] = exMeta
		meta["excluded_note"] = "조회 전용 감사(allowlist) — include_audit_activity=true로 열람. 무관하다는 뜻이 아니라 기본 화면에서 내린 것"
	}
	if !firstEvent.IsZero() {
		meta["coverage_note"] = fmt.Sprintf("발단(%s) 기준 24시간 소급까지만 확인 — 그 이전 변경은 안 봤다. 더 보려면 from을 늘려라",
			firstEvent.UTC().Format(time.RFC3339))
	}
	if hint != "" {
		meta["hint_note"] = fmt.Sprintf("지목 대상 귀속 %d건이 위 구획 — 나머지는 같은 창의 다른 변경이다(이웃 변경이 원인인 경우가 흔하다)", hintHits)
	} else {
		meta["hint_note"] = "대상 미지정 — 창 전역. target을 주면 귀속분이 위로 정렬된다"
	}
	findings = append(findings, meta)

	var sb strings.Builder
	if foldedTotal == 0 {
		fmt.Fprintf(&sb, "창 [%s, %s) 내 변경 0건(원본 %d행, 제외 %d행).",
			from.Format(time.RFC3339), to.Format(time.RFC3339), rawTotal, excluded)
		sb.WriteString(" 단, policy_deployments는 현재 상태 표라 과거 배포 부재의 근거가 못 된다.")
	} else {
		fmt.Fprintf(&sb, "변경 %d건(원본 %d행 → 접기 %d건, 반환 %d",
			foldedTotal, rawTotal, foldedTotal, len(events))
		if truncated {
			sb.WriteString(", 잘림")
		}
		fmt.Fprintf(&sb, "). 제외 %d행.", excluded)
		if hint != "" {
			fmt.Fprintf(&sb, " 지목 대상 귀속 %d건.", hintHits)
		}
	}

	return Envelope{
		Status:          "normal",
		AssessmentBasis: "변경 유무는 사실 관측 — 정상/이상 판정 없음. 귀속은 3축 표기(scope·correlation·match_basis)이며 correlation=unique_inferred는 시각 근접 추정이다",
		Summary:         sb.String(),
		Findings:        findings,
		ObservedRange:   &TimeRange{From: from, To: to},
		Truncated:       truncated,
		Refs:            refs,
	}, nil
}

// lcSplitAgg는 string_agg(',') 결과를 쪼갠다 — pgx v5 단독 의존이라
// 배열 디코더가 없어 문자열 집계를 쓴다(lib/pq 도입 회피).
func lcSplitAgg(s sql.NullString) []string {
	if !s.Valid || s.String == "" {
		return nil
	}
	parts := strings.Split(s.String, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

// lcQueryPolicy는 정책 배포를 (template, role, kind, 초) 묶음으로 접는다.
func lcQueryPolicy(ctx context.Context, pg *sql.DB, inv lcInventory, from, to time.Time) ([]*lcEvent, int, error) {
	rows, err := pg.QueryContext(ctx, `
		SELECT pd.template_id::text, pd.role, pd.target_kind,
		       min(pd.deployed_at), max(pd.deployed_at), count(*),
		       coalesce(max(pt.name), pd.template_id::text) AS tname,
		       string_agg(pd.target_ref, ',')
		FROM policy_deployments pd
		LEFT JOIN policy_templates pt ON pt.id = pd.template_id
		WHERE pd.deployed_at >= $1 AND pd.deployed_at < $2
		GROUP BY pd.template_id, pd.role, pd.target_kind, date_trunc('second', pd.deployed_at)`,
		from, to)
	if err != nil {
		return nil, 0, fmt.Errorf("정책 배포 조회: %w", pgErr(err))
	}
	defer rows.Close()
	var out []*lcEvent
	raw := 0
	for rows.Next() {
		var tmpl, role, kind, tname string
		var first, last time.Time
		var n int
		var targetRefs sql.NullString
		if err := rows.Scan(&tmpl, &role, &kind, &first, &last, &n, &tname, &targetRefs); err != nil {
			return nil, raw, fmt.Errorf("정책 배포 행: %w", pgErr(err))
		}
		raw += n
		e := &lcEvent{
			At: first.UTC(), FirstAt: first.UTC(), LastAt: last.UTC(), Count: n,
			Kind: "policy_deploy", Scope: "target", Correlation: "unmatched",
			Title:   fmt.Sprintf("정책 배포(%s): %s → 대상 %d개", role, tname, n),
			Sources: []string{"policy_deployments"},
			Refs:    []string{fmt.Sprintf("pg:policy_deployments:%s@%s", tmpl, first.UTC().Format(time.RFC3339))},
		}
		for _, ref := range lcSplitAgg(targetRefs) {
			if kind == "target" {
				if t, ok := inv.resolve(ref, "exact_id"); ok {
					e.Targets = append(e.Targets, t)
					continue
				}
			}
			e.Targets = append(e.Targets, lcTarget{ID: ref, Basis: "exact_id"})
		}
		if kind != "target" {
			e.Notes = append(e.Notes, "target_kind="+kind+" — 그룹 배포라 개별 대상은 그룹 구성에 따른다")
		}
		out = append(out, e)
	}
	return out, raw, pgErr(rows.Err())
}

// lcQueryCollectors는 수집기 등록을 (초, kind) 묶음으로 접는다.
func lcQueryCollectors(ctx context.Context, pg *sql.DB, inv lcInventory, from, to time.Time) ([]*lcEvent, int, error) {
	rows, err := pg.QueryContext(ctx, `
		SELECT kind, min(created_at), max(created_at), count(*), string_agg(target_id::text, ',')
		FROM collectors
		WHERE created_at >= $1 AND created_at < $2
		GROUP BY kind, date_trunc('second', created_at)`, from, to)
	if err != nil {
		return nil, 0, fmt.Errorf("수집기 조회: %w", pgErr(err))
	}
	defer rows.Close()
	var out []*lcEvent
	raw := 0
	for rows.Next() {
		var kind string
		var first, last time.Time
		var n int
		var ids sql.NullString
		if err := rows.Scan(&kind, &first, &last, &n, &ids); err != nil {
			return nil, raw, fmt.Errorf("수집기 행: %w", pgErr(err))
		}
		raw += n
		e := &lcEvent{
			At: first.UTC(), FirstAt: first.UTC(), LastAt: last.UTC(), Count: n,
			Kind: "collector_register", Scope: "target", Correlation: "unmatched",
			Title:    fmt.Sprintf("수집기 등록: %s ×%d", kind, n),
			Sources:  []string{"collectors"},
			MatchKey: kind,
			Refs:     []string{fmt.Sprintf("pg:collectors:%s@%s", kind, first.UTC().Format(time.RFC3339))},
		}
		for _, id := range lcSplitAgg(ids) {
			if t, ok := inv.resolve(id, "exact_id"); ok {
				e.Targets = append(e.Targets, t)
			} else {
				e.Targets = append(e.Targets, lcTarget{ID: id, Basis: "exact_id"})
			}
		}
		out = append(out, e)
	}
	return out, raw, pgErr(rows.Err())
}

func lcQueryChangeHistory(ctx context.Context, pg *sql.DB, from, to time.Time) ([]lcRawCH, error) {
	rows, err := pg.QueryContext(ctx, `
		SELECT id::text, occurred_at, category, operation, action, name,
		       coalesce(nullif(after_summary,''), ''), detail::text,
		       coalesce(detail->>'target_id', '')
		FROM change_history
		WHERE occurred_at >= $1 AND occurred_at < $2
		ORDER BY occurred_at`, from, to)
	if err != nil {
		return nil, fmt.Errorf("감사 이력 조회: %w", pgErr(err))
	}
	defer rows.Close()
	var out []lcRawCH
	for rows.Next() {
		var r lcRawCH
		if err := rows.Scan(&r.ID, &r.At, &r.Category, &r.Operation, &r.Action,
			&r.Name, &r.After, &r.Detail, &r.TargetID); err != nil {
			return nil, fmt.Errorf("감사 이력 행: %w", pgErr(err))
		}
		r.At = r.At.UTC()
		out = append(out, r)
	}
	return out, pgErr(rows.Err())
}

// lcPair는 원천 사건에 change_history 실행 기록을 잇는다 — 후보가 정확히
// 1개일 때만(§10.5-1). 억지 1:1 금지: 0개는 unmatched로 두고, 2개 이상은
// ambiguous로 후보를 노출하며 어느 것도 소비하지 않는다.
func lcPair(events []*lcEvent, chRows []lcRawCH, category string,
	extra func(*lcEvent, lcRawCH) bool, consumed map[string]bool) {
	for _, e := range events {
		var cand []lcRawCH
		for _, r := range chRows {
			if r.Category != category || consumed[r.ID] {
				continue
			}
			d := r.At.Sub(e.At)
			if d < 0 {
				d = -d
			}
			if d <= lcPairWindow && extra(e, r) {
				cand = append(cand, r)
			}
		}
		switch len(cand) {
		case 0:
			e.Correlation = "unmatched"
			e.Notes = append(e.Notes, "감사 이력에 짝이 없음 — 누락이 아니라 원천 대응이 부분적이라는 사실")
		case 1:
			r := cand[0]
			consumed[r.ID] = true
			e.Correlation = "unique_inferred"
			ms := r.At.Sub(e.At).Milliseconds()
			if ms < 0 {
				ms = -ms
			}
			e.DeltaMs = &ms
			e.Sources = append(e.Sources, "change_history")
			e.Refs = append(e.Refs, "pg:change_history:"+r.ID)
			if r.After != "" {
				e.Notes = append(e.Notes, "감사: "+r.After)
			}
		default:
			e.Correlation = "ambiguous"
			for _, r := range cand {
				e.Candidates = append(e.Candidates, "pg:change_history:"+r.ID)
			}
			e.Notes = append(e.Notes, fmt.Sprintf(
				"±2초 안에 감사 기록 후보가 %d개 — 어느 것인지 결정 불가라 잇지 않았다", len(cand)))
		}
	}
}

// lcFoldChangeHistory는 남은 감사 행을 "기록된 요약 동일" 접기로 묶고
// 귀속한다. detail은 내용이 아니라 요약(개수)만 담으므로 "내용 동일"을
// 주장하지 않는다(§10.5-5). 같은 (category, operation, name) 계열의
// 서로 다른 요약은 한 사건의 variants로 묶어 변화를 보인다.
func lcFoldChangeHistory(rows []lcRawCH, consumed map[string]bool, inv lcInventory,
	includeAudit bool) ([]*lcEvent, int, []map[string]any) {
	type key struct{ cat, op, name string }
	groups := map[key]*lcEvent{}
	var order []key
	excluded := 0
	exAgg := map[string]*struct {
		N           int
		First, Last time.Time
		Ops         map[string]int
	}{}

	for _, r := range rows {
		if consumed[r.ID] {
			continue
		}
		if lcIsReadOnly(r.Category, r.Operation) && !includeAudit {
			excluded++
			a, ok := exAgg[r.Category]
			if !ok {
				a = &struct {
					N           int
					First, Last time.Time
					Ops         map[string]int
				}{First: r.At, Last: r.At, Ops: map[string]int{}}
				exAgg[r.Category] = a
			}
			a.N++
			a.Ops[r.Operation]++
			if r.At.Before(a.First) {
				a.First = r.At
			}
			if r.At.After(a.Last) {
				a.Last = r.At
			}
			continue
		}
		k := key{r.Category, r.Operation, r.Name}
		e, ok := groups[k]
		if !ok {
			e = &lcEvent{
				At: r.At, FirstAt: r.At, LastAt: r.At,
				Kind: lcKind(r), Scope: lcScope(r), Correlation: "exact",
				Sources: []string{"change_history"},
			}
			groups[k] = e
			order = append(order, k)
			// 귀속 — exact_id 3경로(detail.target_id · name이 UUID) 우선,
			// 없으면 name 연성(§10.2-1).
			switch {
			case r.TargetID != "":
				if t, ok := inv.resolve(r.TargetID, "exact_id"); ok {
					e.Targets = append(e.Targets, t)
				}
			case uuidRe.MatchString(r.Name):
				if t, ok := inv.resolve(r.Name, "exact_id"); ok {
					e.Targets = append(e.Targets, t)
				}
			}
			if len(e.Targets) == 0 && e.Scope == "target" {
				if id, ok := inv.byName[r.Name]; ok {
					if t, ok2 := inv.resolve(id, "name"); ok2 {
						e.Targets = append(e.Targets, t)
					}
				}
			}
			if len(e.Targets) == 0 && e.Scope == "target" {
				e.Notes = append(e.Notes, lcUnresolvedNote(r.Category))
			}
		}
		e.Count++
		e.Refs = append(e.Refs, "pg:change_history:"+r.ID)
		if r.At.Before(e.FirstAt) {
			e.FirstAt = r.At
		}
		if r.At.After(e.LastAt) {
			e.LastAt, e.At = r.At, r.At
		}
		if r.Action == "" {
			if !lcHasNote(e, "action_missing") {
				e.Notes = append(e.Notes,
					"action_missing — 이 기록은 작업 유형 칸이 비어 있다(생산자 미기록). 변경이 아니라는 뜻이 아니다")
			}
		}
		lcAddVariant(e, r)
	}

	out := make([]*lcEvent, 0, len(order))
	for _, k := range order {
		e := groups[k]
		e.Title = lcTitle(k.cat, k.op, k.name, e)
		if len(e.Variants) > 1 {
			e.Notes = append(e.Notes, fmt.Sprintf(
				"요약 %d종으로 접음 — 기록된 요약이 다르다(감사 detail은 내용이 아니라 개수만 담아 '내용 동일'은 판정 불가)", len(e.Variants)))
		}
		out = append(out, e)
	}

	var exMeta []map[string]any
	for cat, a := range exAgg {
		exMeta = append(exMeta, map[string]any{
			"category": cat, "rows": a.N,
			"first_at": a.First.Format(time.RFC3339), "last_at": a.Last.Format(time.RFC3339),
			"operations": a.Ops,
		})
	}
	sort.Slice(exMeta, func(i, j int) bool {
		return fmt.Sprint(exMeta[i]["category"]) < fmt.Sprint(exMeta[j]["category"])
	})
	return out, excluded, exMeta
}

// lcUnresolvedNote는 대상이 안 풀린 이유를 카테고리별로 정직하게 쓴다.
// 수집기·정책 템플릿 감사 행의 name은 **대상 이름이 아니라** 수집기
// 종류·템플릿 이름이라 애초에 해소될 수 없다(§10.0 — 이름이 안 맞은 게
// 아니라 대상 이름을 적는 칸이 아니었다). 이 행들은 collectors·
// policy_deployments 쪽 사건과 같은 사건일 수 있으나 짝을 결정하지
// 못한 잔여이므로 중복 위험을 함께 알린다.
func lcUnresolvedNote(category string) string {
	switch category {
	case "수집기":
		return "대상 미해소 — 이 행의 이름은 대상이 아니라 수집기 종류다. " +
			"같은 창의 collector_register 사건과 같은 사건일 수 있으나 짝을 결정하지 못했다(중복 계수 주의)"
	case "정책 템플릿":
		return "대상 미해소 — 이 행의 이름은 대상이 아니라 템플릿 표기다. " +
			"같은 창의 policy_deploy 사건과 같은 사건일 수 있으나 짝을 결정하지 못했다(중복 계수 주의)"
	}
	return "대상 미해소 — 이름이 명부에 없다(삭제됐거나 rename됐을 수 있다)"
}

func lcHasNote(e *lcEvent, prefix string) bool {
	for _, n := range e.Notes {
		if strings.HasPrefix(n, prefix) {
			return true
		}
	}
	return false
}

// lcAddVariant는 "기록된 요약"별 계수·구간을 누적한다.
func lcAddVariant(e *lcEvent, r lcRawCH) {
	for _, v := range e.Variants {
		if v["summary"] == r.Detail && v["action"] == r.Action {
			v["count"] = v["count"].(int) + 1
			v["last_at"] = r.At.Format(time.RFC3339)
			return
		}
	}
	e.Variants = append(e.Variants, map[string]any{
		"summary": r.Detail, "action": r.Action, "count": 1,
		"first_at": r.At.Format(time.RFC3339), "last_at": r.At.Format(time.RFC3339),
	})
}

// lcScope는 사건 범위 축이다(§10.5) — 대상 개념이 없는 것과 관제면
// 활동을 대상 사건과 섞지 않는다.
func lcScope(r lcRawCH) string {
	switch r.Category {
	case "사용자":
		return "control_plane"
	case "자산 트리":
		return "global"
	}
	return "target"
}

func lcKind(r lcRawCH) string {
	switch {
	case strings.HasPrefix(r.Category, "구성 대상"):
		return "target_update"
	case r.Category == "수집기":
		return "collector_update"
	case r.Category == "정책 템플릿":
		return "policy_deploy"
	case r.Category == "자산 트리":
		return "asset_tree_edit"
	case r.Category == "사용자":
		return "account_change"
	}
	return "audit"
}

func lcTitle(cat, op, name string, e *lcEvent) string {
	head := cat
	if op != "" {
		head = cat + " — " + op
	}
	if e.Count > 1 {
		return fmt.Sprintf("%s: %s ×%d (%s~%s)", head, name, e.Count,
			e.FirstAt.Format("15:04:05"), e.LastAt.Format("15:04:05"))
	}
	if name == "" {
		return head
	}
	return head + ": " + name
}

// lcBucket은 정렬 구획이다 — 힌트 귀속 → 대상 → 전역 → 관제면(§10.7).
func lcBucket(e *lcEvent, hint string) int {
	if hint != "" {
		for _, t := range e.Targets {
			if t.ID == hint {
				return 0
			}
		}
	}
	switch e.Scope {
	case "target":
		return 1
	case "global":
		return 2
	default:
		return 3
	}
}

func lcStableKey(e *lcEvent) string {
	if len(e.Refs) > 0 {
		return e.Refs[0]
	}
	return e.Kind + e.Title
}

// lcFinding은 사건 하나를 응답 형태로 옮긴다. 대상 목록은 표본 + 총계
// (무제한 array_agg 금지 — §10.7). 힌트가 없으면 유형별 개수로 폴백한다.
func lcFinding(e *lcEvent, hint string, inv lcInventory) (Finding, bool) {
	f := Finding{
		"at": e.At.Format(time.RFC3339), "kind": e.Kind, "title": e.Title,
		"scope": e.Scope, "correlation": e.Correlation,
		"sources": e.Sources, "refs": e.Refs,
	}
	if e.Count > 1 {
		f["count"] = e.Count
		f["first_at"] = e.FirstAt.Format(time.RFC3339)
		f["last_at"] = e.LastAt.Format(time.RFC3339)
	}
	if e.DeltaMs != nil {
		f["correlation_delta_ms"] = *e.DeltaMs
	}
	if len(e.Candidates) > 0 {
		f["correlation_candidates"] = e.Candidates
	}
	if len(e.Variants) > 1 {
		f["variants"] = e.Variants
	}
	if len(e.Notes) > 0 {
		f["notes"] = e.Notes
	}

	hit := false
	if len(e.Targets) > 0 {
		sample := make([]map[string]any, 0, lcTargetSample)
		byType := map[string]int{}
		for i, t := range e.Targets {
			if t.ID == hint {
				hit = true
			}
			if m, ok := inv.byID[t.ID]; ok {
				byType[m.Type]++
			} else {
				byType["미등록"]++
			}
			if i < lcTargetSample {
				sample = append(sample, map[string]any{
					"target_id": t.ID, "name": t.Name, "type": t.Type, "match_basis": t.Basis})
			}
		}
		f["targets_total"] = len(e.Targets)
		f["targets"] = sample
		if len(e.Targets) > lcTargetSample {
			f["targets_truncated"] = true
			f["targets_by_type"] = byType // 힌트 없이도 규모·구성이 보이게(§10.7 폴백)
		}
		if hint != "" {
			f["hint_target_included"] = hit
		}
	} else if e.Scope == "target" {
		f["targets_total"] = 0
		f["match_basis"] = "unattributed"
	}
	return f, hit
}
