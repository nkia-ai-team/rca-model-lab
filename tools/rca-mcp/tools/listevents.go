// list_events — "이 대상에 무슨 사건·알람이 있었나"의 재설계 표면
// (스펙 §3.3 ④ + §8 구현 설계, 2026-07-28 확정). get_events 자리 교체.
//
//   - 에피소드 접기: 같은 episode_id의 open/close를 한 사건으로 —
//     [발단, 해소]·지속·peak. 전이별 필드 비대칭(duration·peak는 close
//     전용, severity는 open 전용 — 실측)을 폴백으로 흡수하고, 경계
//     상태 4값으로 창 밖 발단/해소를 정직하게 표기한다(§8 결정 1).
//   - zero episode_id·빈 transition(alarm-bridge 실측)은 접지 않고
//     평면 단건(unpaired)으로 — 접으면 서로 다른 알람이 뭉친다.
//   - 성격 딱지: detector×출처 완전 매핑 — 감지(detection, 2차 신호) /
//     외부알람(external) / 기록(fact, kcm). class 컬럼은 실측 상수라
//     판별자가 아니다(§8 결정 2).
//   - K8s 합성: 대상 정체 세 갈래(클러스터/승격 리소스/비K8s) —
//     kcm_events_local은 재푸시 스냅샷이라 (kind,name,ns,reason)
//     dedup + max(count), 클러스터 조회는 namespace 구획(§8 결정 3).
//   - 0건 = normal이되 화이트리스트 한정 문구 + 원천별 조회 시도
//     표시(§8 결정 5).
//   - kcm 범위 술어(2026-10-06 실측 수리): kcm_events_local.target_id는
//     승격 리소스면 **그 리소스 대상**이고 클러스터는 host_target_id에 있다.
//     종전 target_id = 클러스터 조건은 승격된 Deployment·ReplicaSet·Pod의
//     이벤트(ScalingReplicaSet·Killing 등)를 통째로 놓쳤다(f09 캡처 115행 중
//     18행만 보임). 범위 = target_id ∈ {클러스터, 자기} ∪ host_target_id = 클러스터.
//   - 클러스터 대상 조회는 (namespace, kind, reason) 묶음 요약을 함께 싣는다 —
//     표시 쿼터에 밀린 Normal 사건(스케일·종료)도 Warning 옆에 보이게.
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
	listEvQuotaEpisode  = 20 // 에피소드 반환 상한(발단 최신순)
	listEvQuotaUnpaired = 10
	listEvQuotaKcm      = 15
	listEvQuotaReason   = 30 // 클러스터 조회의 (namespace, kind, reason) 묶음 요약 상한
	listEvZeroUUID      = "00000000-0000-0000-0000-000000000000"
)

// listEvTargetHelp는 대상 인자 안내다 — 클러스터 전역 조회 경로를 모델이
// 알 수 있게 오류 문구에도 싣는다(종전엔 어디에도 없었다).
const listEvTargetHelp = "target은 대상 UUID다. kubernetes 클러스터 대상의 UUID를 주면 그 클러스터 전체(모든 namespace)의 " +
	"쿠버네티스 이벤트를 창 안에서 전부 본다(클러스터 UUID는 search_targets type=kubernetes로 찾는다)"

// listEvLabel은 detector×출처의 성격 딱지다(§8 결정 2 — 완전 매핑,
// 미지 detector는 감지로 폴백하되 detector 원문이 finding에 남는다).
func listEvLabel(detector string) string {
	if detector == "alarm-bridge" {
		return "external"
	}
	return "detection"
}

// NewListEventsTool은 list_events 도구를 만든다. nowFn은 "now" 상대
// 시간의 기준(테스트 주입용, nil이면 time.Now — read_timeseries 규약).
func NewListEventsTool(ch *CH, pg *sql.DB, nowFn func() time.Time) llm.Tool {
	if nowFn == nil {
		nowFn = time.Now
	}
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"target": map[string]string{"type": "string", "description": "target_id(UUID). kubernetes 클러스터 대상 UUID면 클러스터 전체 쿠버네티스 이벤트"},
			"from":   map[string]string{"type": "string", "description": "UTC RFC3339 또는 now/now-15m, 반개구간 시작(포함)"},
			"to":     map[string]string{"type": "string", "description": "UTC RFC3339 또는 now, 반개구간 끝(제외)"},
		},
		"required": []string{"target", "from", "to"},
	})
	return llm.Tool{
		Name: "list_events",
		Description: "대상의 시간창 내 사건 — 이상감지 에피소드(발단·해소·지속·진행 중), 외부 알람, K8s 대상이면 쿠버네티스 이벤트(Normal·Warning 모두)까지 합성. " +
			"kubernetes 클러스터 대상 UUID를 주면 클러스터 전체(모든 namespace) 쿠버네티스 이벤트를 창 안에서 전부 보고 (namespace, kind, reason) 묶음 요약을 함께 준다 — " +
			"승격 리소스(deployment·pod 등) UUID면 그 리소스 이름의 이벤트만. 롤아웃 전후 스펙 차이·레플리카 수 변경은 list_changes. " +
			"과거가 궁금하면(평소에도 울리나) 창을 옮겨 재호출.",
		Parameters: params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct{ Target, From, To string }
			if err := json.Unmarshal(args, &in); err != nil {
				return nil, fmt.Errorf(`인자 오류: {"target": "<uuid>", "from": "<RFC3339|now-1h>", "to": "<RFC3339|now>"} 필요 — %s`, listEvTargetHelp)
			}
			if in.Target == "" {
				return nil, fmt.Errorf("target 누락 — %s", listEvTargetHelp)
			}
			if !uuidRe.MatchString(in.Target) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — target_id는 순수 UUID다(접두 없음). %s", in.Target, listEvTargetHelp)
			}
			now := nowFn().UTC()
			fromT, err := parseFlexTime(in.From, now)
			if err != nil {
				return nil, fmt.Errorf("from 시간 오류: %v", err)
			}
			toT, err := parseFlexTime(in.To, now)
			if err != nil {
				return nil, fmt.Errorf("to 시간 오류: %v", err)
			}
			if !fromT.Before(toT) {
				return nil, fmt.Errorf("시간창 오류: from < to 필요 (받은 값 from=%q to=%q)", in.From, in.To)
			}
			return listEvents(ctx, ch, pg, in.Target, fromT, toT)
		},
	}
}

// k8sIdentity는 대상 정체 세 갈래 판별 결과다(§8 결정 3).
type k8sIdentity struct {
	branch     string // cluster | resource | none
	cluster    string // kcm 조회 키
	kind       string // 승격 리소스의 종류(소문자)
	namespace  string
	name       string // kcm object_name 매칭 값
	matchBasis string // name | parent_pod
	skipNote   string // branch=none일 때 이유(응답 표시용 — 조용한 누락 금지)
	pgErrTok   string // PG 조회 실패의 분류 토큰(§15.2-3, 2b) — pg=optional 강등의 결손 명시 원천
}

// resolveEventTarget은 PG 명부로 대상 정체를 판별한다. 유형 판별의
// 정본은 kcm_resource_targets의 resource_kind·resource_key다 —
// targets.type은 pod·container·node가 전부 kubernetes_resource라
// 구별하지 못한다(§8 결정 3 보강).
func resolveEventTarget(ctx context.Context, pg *sql.DB, target string) k8sIdentity {
	if pg == nil {
		return k8sIdentity{branch: "none", skipNote: "PG 미연결 — K8s 여부 판별 불가, kcm 조회 생략"}
	}
	var clusterID, kind, key string
	err := pg.QueryRowContext(ctx, `SELECT cluster_target_id::text, resource_kind, resource_key
		FROM kcm_resource_targets WHERE target_id = $1::uuid`, target).
		Scan(&clusterID, &kind, &key)
	switch err {
	case nil:
		id := k8sIdentity{branch: "resource", cluster: clusterID,
			kind: strings.ToLower(kind), matchBasis: "name"}
		seg := strings.Split(key, "/")
		switch id.kind {
		case "container": // resource_key = namespace/pod/container → 부모 pod로 연결
			if len(seg) == 3 {
				id.namespace, id.name = seg[0], seg[1]
				id.kind, id.matchBasis = "pod", "parent_pod"
			}
		case "node": // node는 namespace 없음 — 마지막 조각이 이름
			id.name = seg[len(seg)-1]
		default: // pod 포함 namespace/name 꼴
			if len(seg) == 2 {
				id.namespace, id.name = seg[0], seg[1]
			} else {
				id.name = seg[len(seg)-1]
			}
		}
		return id
	case sql.ErrNoRows:
		// 승격 리소스가 아니면 클러스터인지 본다.
		var typ string
		e := pg.QueryRowContext(ctx, `SELECT type::text FROM targets WHERE id = $1::uuid`, target).Scan(&typ)
		switch {
		case e == nil && typ == "kubernetes":
			return k8sIdentity{branch: "cluster", cluster: target}
		case e == nil:
			return k8sIdentity{branch: "none", skipNote: fmt.Sprintf("대상 유형 %s — K8s 아님, kcm 조회 안 함", typ)}
		case e == sql.ErrNoRows:
			return k8sIdentity{branch: "none", skipNote: "명부에 없는 대상 — K8s 여부 판별 불가, kcm 조회 생략"}
		default:
			// pg=optional(deps.go, 2b): CH 사건 관측은 유지하고 K8s 합성만
			// 뺀다 — 분류 토큰만, 원문 오류 문자열 금지(§15.3-2).
			tok := beDetail(pgErr(e))
			return k8sIdentity{branch: "none", pgErrTok: tok,
				skipNote: "명부 조회 실패(" + tok + ") — K8s 여부 판별 불가, kcm 이벤트 미조회(결손이지 무사건이 아니다)"}
		}
	default:
		tok := beDetail(pgErr(err))
		return k8sIdentity{branch: "none", pgErrTok: tok,
			skipNote: "명부 조회 실패(" + tok + ") — K8s 여부 판별 불가, kcm 이벤트 미조회(결손이지 무사건이 아니다)"}
	}
}

// listEventsHorizon은 이 대상의 이벤트 **도착 수평선**이다(§14-5 5c 결정 ①
// — sampleHorizon과 같은 규율: 창 조건 없는 max가 "창 후반 이벤트 없음"과
// "수집 미도달"을 구분한다). 원천이 복수(감지 이벤트 + K8s 이벤트)면 min을
// 취한다 — 가장 늦게 도착하는 원천 기준이어야 "여기까지 봤다"가 봉투의 전
// 원천에 대해 참이다. 어느 한쪽이라도 수평선을 못 내면 주장하지 않는다
// (fail-closed). 빈 스트림의 max=epoch는 horizonObservedRange가 거른다.
func listEventsHorizon(ctx context.Context, ch *CH, target string, id k8sIdentity) (time.Time, bool) {
	// max() 단일 행 집계 — 절단 불가 표면이라 표식은 버린다.
	one := func(q string, p map[string]string) (time.Time, bool) {
		rows, _, err := ch.Query(ctx, q, p)
		if err != nil || len(rows) == 0 {
			return time.Time{}, false
		}
		return chParseTS(rows[0]["h"])
	}
	h, ok := one(`SELECT toString(max(occurred_at)) AS h FROM lucida_events_local WHERE target_id = {target:String}`,
		map[string]string{"target": target})
	if !ok {
		return time.Time{}, false
	}
	if id.branch != "none" {
		k, kok := one(`SELECT toString(max(timestamp)) AS h FROM kcm_events_local WHERE `+listEvKcmScope,
			map[string]string{"cluster": id.cluster, "self": target})
		if !kok {
			return time.Time{}, false
		}
		if k.Before(h) {
			h = k
		}
	}
	return h, true
}

// listEvKcmScope는 kcm 이벤트의 클러스터 범위 술어다(머리 주석 2026-10-06 실측).
// 승격 리소스 이벤트는 target_id가 그 리소스·host_target_id가 클러스터이고,
// 비승격 객체 이벤트는 target_id가 클러스터다. {self}는 조회 대상 자신 —
// host_target_id가 비어 있는 원천에서 승격 리소스 자기 이벤트를 잃지 않게.
const listEvKcmScope = `(target_id = {cluster:String} OR host_target_id = {cluster:String} OR target_id = {self:String})`

func listEvents(ctx context.Context, ch *CH, pg *sql.DB, target string, from, to time.Time) (any, error) {
	p := map[string]string{"target": target, "from": chTime(from), "to": chTime(to)}
	window := `occurred_at >= parseDateTime64BestEffort({from:String}, 9)
	  AND occurred_at <  parseDateTime64BestEffort({to:String}, 9)`

	var acc truncAcc

	// ① 에피소드 접기 — zero episode·비정상 transition 제외(§8 결정 1).
	epRows, tr, err := ch.Query(ctx, `
		SELECT toString(episode_id) AS ep,
		       any(detector) AS detector,
		       argMax(reason, occurred_at) AS reason,
		       argMax(attributes['metric'], occurred_at) AS metric,
		       countIf(transition='open') AS n_open,
		       countIf(transition='close') AS n_close,
		       toString(minIf(occurred_at, transition='open')) AS open_at,
		       toString(maxIf(occurred_at, transition='close')) AS close_at,
		       argMaxIf(duration_ms, occurred_at, transition='close') AS dur_ms,
		       argMaxIf(peak_severity, occurred_at, transition='close') AS peak,
		       argMaxIf(severity, occurred_at, transition='open') AS open_sev,
		       argMax(toString(event_id), occurred_at) AS last_event_id
		FROM lucida_events_local
		WHERE target_id = {target:String} AND `+window+`
		  AND episode_id != toUUID('`+listEvZeroUUID+`')
		  AND transition IN ('open','close')
		GROUP BY episode_id
		LIMIT 500`, p)
	if err != nil {
		return nil, fmt.Errorf("list_events 에피소드 조회: %w", err)
	}
	acc.note(tr)

	// ② 접기 불가 행 — 평면 단건(unpaired). alarm-bridge가 이 경로.
	unRows, tr, err := ch.Query(ctx, `
		SELECT toString(event_id) AS eid, detector, severity, reason, event_kind,
		       toString(occurred_at) AS at,
		       attributes['status'] AS status,
		       attributes['metric'] AS metric
		FROM lucida_events_local
		WHERE target_id = {target:String} AND `+window+`
		  AND (episode_id = toUUID('`+listEvZeroUUID+`')
		       OR transition NOT IN ('open','close'))
		ORDER BY occurred_at DESC
		LIMIT 200`, p)
	if err != nil {
		return nil, fmt.Errorf("list_events unpaired 조회: %w", err)
	}
	acc.note(tr)

	// ③ K8s 합성 — 세 갈래(§8 결정 3).
	id := resolveEventTarget(ctx, pg, target)
	var kcmRows, reasonRows []map[string]any
	if id.branch != "none" {
		kp := map[string]string{"cluster": id.cluster, "self": target, "from": chTime(from), "to": chTime(to)}
		match := ""
		if id.branch == "resource" {
			// 매칭 키 고정: (cluster, lower(kind), namespace, 이름 정확 일치).
			match = ` AND lower(object_kind) = {kind:String} AND object_name = {name:String}`
			kp["kind"], kp["name"] = id.kind, id.name
			if id.namespace != "" {
				match += ` AND namespace = {ns:String}`
				kp["ns"] = id.namespace
			}
		}
		// 재푸시 dedup: (ns,kind,name,reason,event_type) + max(count).
		kcmRows, tr, err = ch.Query(ctx, `
			SELECT namespace, object_kind, object_name, reason, event_type,
			       max(count) AS max_count,
			       toString(min(timestamp)) AS first_at,
			       toString(max(timestamp)) AS last_at,
			       argMax(severity_text, timestamp) AS sev,
			       argMax(body, timestamp) AS body
			FROM kcm_events_local
			WHERE `+listEvKcmScope+match+`
			  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
			  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
			GROUP BY namespace, object_kind, object_name, reason, event_type
			ORDER BY last_at DESC
			LIMIT 300`, kp)
		if err != nil {
			return nil, fmt.Errorf("list_events kcm 조회: %w", err)
		}
		acc.note(tr)
		// 클러스터 조회 — (namespace, kind, reason) 묶음 요약. 표시 쿼터(15그룹)에
		// 밀린 사건도 사유 단위로는 전부 보이게 하는 집계다(판정 없음).
		if id.branch == "cluster" {
			reasonRows, tr, err = ch.Query(ctx, `
				SELECT namespace, object_kind, reason, event_type,
				       count() AS objects, sum(mc) AS count_sum, toString(max(la)) AS last_at
				FROM (
					SELECT namespace, object_kind, object_name, reason, event_type,
					       max(count) AS mc, max(timestamp) AS la
					FROM kcm_events_local
					WHERE `+listEvKcmScope+`
					  AND timestamp >= parseDateTime64BestEffort({from:String}, 9)
					  AND timestamp <  parseDateTime64BestEffort({to:String}, 9)
					GROUP BY namespace, object_kind, object_name, reason, event_type
				)
				GROUP BY namespace, object_kind, reason, event_type
				ORDER BY namespace, object_kind, reason, event_type
				LIMIT 500`, kp)
			if err != nil {
				return nil, fmt.Errorf("list_events kcm 요약 조회: %w", err)
			}
			acc.note(tr)
		}
	}

	// 도착 수평선(§14-5 5c 결정 ①)은 조회 주체(ctx·ch 보유)가 계산해
	// 조립기에 값으로 넘긴다.
	horizon, _ := listEventsHorizon(ctx, ch, target, id)
	out, err := assembleListEvents(target, id, epRows, unRows, kcmRows, horizonObservedRange(from, to, horizon), bool(acc))
	if err != nil || id.branch != "cluster" {
		return out, err
	}
	return attachReasonSummary(out.(Envelope), reasonRows), nil
}

// attachReasonSummary는 클러스터 조회 봉투에 (namespace, kind, reason) 묶음
// 요약을 더한다 — summary_meta 필드 추가와 요약 문장 꼬리(사유별 개수)뿐,
// 기존 필드·finding은 그대로다.
func attachReasonSummary(env Envelope, rows []map[string]any) Envelope {
	if len(rows) == 0 {
		return env
	}
	list := make([]map[string]any, 0, len(rows))
	byType := map[string]map[string]int{} // event_type → reason → objects
	for _, r := range rows {
		et, reason := fmt.Sprint(r["event_type"]), fmt.Sprint(r["reason"])
		if byType[et] == nil {
			byType[et] = map[string]int{}
		}
		byType[et][reason] += asInt(r["objects"])
		if len(list) < listEvQuotaReason {
			list = append(list, map[string]any{
				"namespace": r["namespace"], "kind": r["object_kind"], "reason": reason, "event_type": et,
				"objects": asInt(r["objects"]), "count_sum": asInt(r["count_sum"]), "last_at": r["last_at"],
			})
		}
	}
	for _, f := range env.Findings {
		if f["section"] != "summary_meta" {
			continue
		}
		f["k8s_reason_summary"] = list
		f["k8s_reason_summary_total"] = len(rows)
		if len(rows) > len(list) {
			f["k8s_reason_summary_truncated"] = true
		}
		f["k8s_reason_summary_note"] = "클러스터 전 namespace의 창 내 쿠버네티스 이벤트를 (namespace, kind, reason, event_type)으로 묶은 것 — " +
			"objects=객체 수, count_sum=객체별 이벤트 count 최댓값의 합(쿠버네티스 누적 count라 창 이전 발생분 포함 가능)"
	}
	var sb strings.Builder
	sb.WriteString(" 사유별 객체 수 —")
	for _, et := range []string{"Warning", "Normal"} {
		m := byType[et]
		if len(m) == 0 {
			continue
		}
		ks := make([]string, 0, len(m))
		for k := range m {
			ks = append(ks, k)
		}
		sort.Strings(ks)
		parts := make([]string, 0, len(ks))
		for _, k := range ks {
			parts = append(parts, fmt.Sprintf("%s %d", k, m[k]))
		}
		fmt.Fprintf(&sb, " %s[%s]", et, strings.Join(parts, ", "))
	}
	sb.WriteString(" (summary_meta.k8s_reason_summary에 namespace·kind별).")
	env.Summary += sb.String()
	return env
}

func assembleListEvents(target string, id k8sIdentity, epRows, unRows, kcmRows []map[string]any, observed *TimeRange, chTruncated bool) (any, error) {
	var findings []Finding
	var refs []string

	// 에피소드 — 경계 상태 4값 + 전이별 필드 폴백(§8 결정 1 보강).
	type epView struct {
		f     Finding
		onset string
	}
	var eps []epView
	ongoing := 0
	for _, r := range epRows {
		nOpen, nClose := asInt(r["n_open"]), asInt(r["n_close"])
		ref := fmt.Sprintf("ch:lucida_events_local:%v", r["last_event_id"])
		f := Finding{
			"section": "episode", "label": listEvLabel(fmt.Sprint(r["detector"])),
			"detector": r["detector"], "reason": r["reason"],
			"episode_id": r["ep"], "refs": []string{ref},
		}
		if m := fmt.Sprint(r["metric"]); m != "" {
			f["metric"] = m
		}
		var onset string
		switch {
		case nOpen > 0 && nClose > 0:
			f["boundary"] = "complete"
			f["open_at"], f["close_at"] = r["open_at"], r["close_at"]
			f["duration_ms"] = asInt(r["dur_ms"])
			onset = fmt.Sprint(r["open_at"])
		case nOpen > 0: // close가 창 밖 — "진행 중" 단정 금지.
			f["boundary"] = "closed_after_window"
			f["open_at"] = r["open_at"]
			f["duration_ms"] = "미상"
			f["note"] = "창 안에서 해소 기록 없음 — 진행 중 단정 아님, 창을 뒤로 넓혀 확인 가능"
			onset = fmt.Sprint(r["open_at"])
			ongoing++
		default: // close만 창 안 — 발단은 duration으로 역산(실측 검증).
			f["boundary"] = "opened_before_window"
			f["close_at"] = r["close_at"]
			f["duration_ms"] = asInt(r["dur_ms"])
			if closeT, err := time.Parse("2006-01-02 15:04:05.999999999", fmt.Sprint(r["close_at"])); err == nil {
				onset = closeT.Add(-time.Duration(asInt(r["dur_ms"])) * time.Millisecond).Format("2006-01-02 15:04:05")
				f["derived_open_at"] = onset
				f["note"] = "발단이 창 밖(해소 시각 − 지속시간으로 역산)"
			} else {
				onset = fmt.Sprint(r["close_at"])
				f["note"] = "발단이 창 밖(역산 실패 — 해소 시각만 표시)"
			}
		}
		// peak: close 전용 필드 폴백 — 빈 peak를 "경미"로 오독 금지.
		if pk := fmt.Sprint(r["peak"]); pk != "" {
			f["peak_severity"] = pk
		} else if sv := fmt.Sprint(r["open_sev"]); sv != "" {
			f["peak_severity"] = sv
			f["peak_basis"] = "잠정 — 미해소라 open 시점 심각도(확정 peak는 해소 후 기록됨)"
		}
		eps = append(eps, epView{f: f, onset: onset})
	}
	sort.Slice(eps, func(i, j int) bool { return eps[i].onset > eps[j].onset }) // 발단 최신순(§8 결정 4)
	epShown := 0
	for _, e := range eps {
		if epShown >= listEvQuotaEpisode {
			break
		}
		findings = append(findings, e.f)
		refs = append(refs, e.f["refs"].([]string)[0])
		epShown++
	}

	// unpaired 평면 단건.
	unShown, unActive := 0, 0
	for _, r := range unRows {
		sev := fmt.Sprint(r["severity"])
		if sev != "cleared" && sev != "info" {
			unActive++
		}
		if unShown >= listEvQuotaUnpaired {
			continue
		}
		ref := fmt.Sprintf("ch:lucida_events_local:%v", r["eid"])
		f := Finding{
			"section": "unpaired", "label": listEvLabel(fmt.Sprint(r["detector"])),
			"detector": r["detector"], "severity": sev, "reason": r["reason"],
			"at": r["at"], "lifecycle": "unpaired",
			"note": "에피소드 정보 없는 단건 — 짝(발단/해소) 복원 불가",
			"refs": []string{ref},
		}
		if st := fmt.Sprint(r["status"]); st != "" {
			f["status"] = st // alarm-bridge FIRING/RESOLVED 보완
		}
		if m := fmt.Sprint(r["metric"]); m != "" {
			f["metric"] = m
		}
		findings = append(findings, f)
		refs = append(refs, ref)
		unShown++
	}

	// kcm — namespace 구획, '기록' 딱지(발생 기록 ≠ 원인 확정).
	kcmShown, kcmWarn := 0, 0
	nsSeen := map[string]int{}
	for _, r := range kcmRows {
		ns := fmt.Sprint(r["namespace"])
		nsSeen[ns]++
		if fmt.Sprint(r["event_type"]) == "Warning" {
			kcmWarn++
		}
		if kcmShown >= listEvQuotaKcm {
			continue
		}
		ref := fmt.Sprintf("ch:kcm_events_local:%s:%s/%s:%v", id.cluster, ns, r["object_name"], r["reason"])
		f := Finding{
			"section": "k8s", "label": "fact", "source": "kcm",
			"namespace": ns, "kind": r["object_kind"], "name": r["object_name"],
			"reason": r["reason"], "event_type": r["event_type"], "severity": r["sev"],
			"count": asInt(r["max_count"]), "first_at": r["first_at"], "last_at": r["last_at"],
			"note": "쿠버네티스가 남긴 발생 기록 — 원인 확정 아님",
			"refs": []string{ref},
		}
		if id.branch == "resource" {
			f["match_basis"] = id.matchBasis
			f["match_confidence"] = "이름 매칭 — 이름 재사용 시 오귀속 가능(kcm 이벤트에 UID 없음)"
		}
		findings = append(findings, f)
		refs = append(refs, ref)
		kcmShown++
	}

	// 요약 메타 — 구획별 반환/전체 + 원천별 조회 시도(§8 결정 4·5).
	meta := Finding{
		"section": "summary_meta",
		"counts": map[string]any{
			"episodes_total": len(epRows), "episodes_returned": epShown,
			"unpaired_total": len(unRows), "unpaired_returned": unShown,
			"k8s_groups_total": len(kcmRows), "k8s_groups_returned": kcmShown,
		},
		"sources": map[string]any{
			"lucida_events": "조회함",
			"kcm_events":    map[string]any{"cluster": "조회함", "resource": "조회함(이름 매칭)", "none": "조회 안 함"}[id.branch],
		},
	}
	if id.branch == "none" && id.skipNote != "" {
		meta["kcm_skip_reason"] = id.skipNote
	}
	// 부분 강등(§15.2-3, 2b): PG 실패 = K8s 정체 판별 결손 — CH 관측은
	// 위에 그대로 있고, 빠진 부분(kcm 합성)을 토큰으로 명시한다.
	if id.pgErrTok != "" {
		meta["source_errors"] = map[string]string{"pg": id.pgErrTok}
	}
	if id.branch == "cluster" && len(nsSeen) > 1 {
		meta["namespace_note"] = fmt.Sprintf(
			"클러스터 조회라 %d개 namespace가 섞여 있다(구획: %s) — 모니터링 스택 자신의 이벤트일 수 있으니 namespace를 확인하라",
			len(nsSeen), nsKeys(nsSeen))
	}
	findings = append(findings, meta)

	// status — 감지 에피소드·활성 unpaired·kcm Warning 중 하나라도 있으면.
	status := "normal"
	if len(epRows) > 0 || unActive > 0 || kcmWarn > 0 {
		status = "anomalous"
	}

	var sb strings.Builder
	fmt.Fprintf(&sb, "사건 — 감지 에피소드 %d건(반환 %d, 창 안 미해소 %d), 단건 %d건, K8s 기록 %d그룹(Warning %d).",
		len(epRows), epShown, ongoing, len(unRows), len(kcmRows), kcmWarn)
	if status == "normal" {
		sb.WriteString(" 시간창 내 사건 0건 — 단, 이상감지는 감시 목록(화이트리스트) 안만 본다. 무혐의 뜻이 아니며 원본 확인은 scan_metrics·sample_logs 몫이다.")
	} else {
		sb.WriteString(" 감지(detection)는 2차 신호다 — 단서로 쓰고 원본 확인은 scan_metrics·sample_logs로.")
	}

	return Envelope{
		Status: status,
		AssessmentBasis: "감지 에피소드·활성 단건·K8s Warning 존재 여부. 감지는 화이트리스트×감도 통과분의 push 기록이라 " +
			"0건=정상 아님(§2.3 실측 — 감시 사각지대 존재). 에피소드 경계는 창 기준 4값(complete/opened_before_window/closed_after_window)",
		Summary: sb.String(),
		// 도착 수평선 기반(§14-5 5c 결정 ①) — 창 끝을 수평선이 못 넘으면
		// 그만큼이 수집 지연으로 계산된다(CollectLagOf).
		ObservedRange:  observed,
		Findings:       findings,
		Refs:           refs,
		Truncated:      len(epRows) > epShown || len(unRows) > unShown || len(kcmRows) > kcmShown,
		QueryTruncated: chTruncated,
		// 구획별 절단(§5.1 계약 2) — summary_meta.counts와 같은 숫자를
		// projector가 파싱 없이 읽는 자리.
		Scopes: []QueryScope{
			qscope("episode", len(epRows), epShown),
			qscope("unpaired", len(unRows), unShown),
			qscope("k8s", len(kcmRows), kcmShown),
		},
	}, nil
}

func nsKeys(m map[string]int) string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return strings.Join(ks, ", ")
}
