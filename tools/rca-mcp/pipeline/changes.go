// 파이프라인 [2] 변경 조회 — "뭐가 바뀌었지?"를 1급 단계로 (설계 §2).
// LLM 없음: 모든 인시던트에서 무조건, 증상 대상 + 후보 대상에 대해
// 변경 조회를 결정론 호출한다(도구 계약 §6 배치). 결과는 [4] 가설
// 생성의 출처 "변경 이력"의 재료이자 사전확률 1순위 입력이다.
//
// 주의(§10.9 이월): 이 경로는 **대상 지목 계약**이라 대상 개념이 없는
// 전역 변경(자산 트리 편집·계정)과 이웃 대상 변경에 닿지 못한다.
// 조사자 표면 list_changes는 창 전역을 보도록 바뀌었으나 여기는 그대로다
// — 승격은 발동 조건부(평가에서 [2]의 0건이 오답에 기여한 실측 시).
package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// ChangeEvent는 변경 신호 1건이다 — 계약 거울(lucida-next signals.go
// ChangeEvent, ref-rca-io-contract.md의 정본 추적 규칙과 동일).
type ChangeEvent struct {
	Kind     string    `json:"kind"` // audit|policy_deploy|target_update|collector_update
	At       time.Time `json:"at"`
	TargetID string    `json:"targetId"`
	Detail   string    `json:"detail"`
}

// GetChangesFunc는 변경 조회(target, window)의 typed 자리다
// (tools.NewChangesFunc가 채운다 — LLM 표면 list_changes와 별개 조회).
type GetChangesFunc func(ctx context.Context, target string, from, to time.Time) ([]ChangeEvent, error)

// ChangeScanResult는 [2]의 산출물이다. Targets는 조회 범위의 기록이다
// — "어디를 봤는데 변경이 없었나"가 리포트의 ruled_out 재료가 된다.
type ChangeScanResult struct {
	From    time.Time
	To      time.Time
	Targets []string      // 조회한 대상 (정렬·중복 제거)
	Changes []ChangeEvent // 원인 후보 자격 변경 — 시각순 (동시각이면 대상순)

	// LateChanges — 첫 증상(FirstEventAt) 이후의 변경. 원인은 결과보다
	// 먼저여야 하므로 인시던트 발단의 원인 후보가 될 수 없어 [4]의
	// 변경 출처 재료에서 제외하되, 기록은 남긴다(설계 §14 ③ — 제일 싼
	// 검사를 제일 먼저, 결정론으로). 탐지 지연은 첫 증상 시각을 뒤로만
	// 미루므로 이 배제는 안전 방향이다: 진짜 발단 ≤ 탐지 시각 < 변경
	// 시각. 조사자의 list_changes 도구는 이와 무관하게 전체를 본다.
	LateChanges []ChangeEvent
}

// ScanChanges는 Triage 산출물의 증상·후보 대상 전부에 대해 변경을
// 조회한다. 변경은 장애 "직전"이 중요하므로 시간창은 이벤트 시작에서
// lookback만큼 앞으로 넓힌다: [From-lookback, To].
func ScanChanges(ctx context.Context, tri TriageResult, lookback time.Duration, get GetChangesFunc) (ChangeScanResult, error) {
	if get == nil {
		return ChangeScanResult{}, fmt.Errorf("changes: GetChangesFunc 필수")
	}

	seen := map[string]bool{}
	var targets []string
	add := func(t string) {
		if t != "" && !seen[t] {
			seen[t] = true
			targets = append(targets, t)
		}
	}
	add(tri.Symptom.TargetID)
	for _, g := range tri.MemberGroups {
		add(g.TargetID)
	}
	for _, c := range tri.Candidates {
		add(c.TargetID)
	}
	sort.Strings(targets)

	r := ChangeScanResult{From: tri.From.Add(-lookback), To: tri.To, Targets: targets}
	for _, t := range targets {
		evs, err := get(ctx, t, r.From, r.To)
		if err != nil {
			return ChangeScanResult{}, fmt.Errorf("changes: 변경 조회(%s): %w", t, err)
		}
		for _, ev := range evs {
			if ev.At.After(tri.From) {
				r.LateChanges = append(r.LateChanges, ev)
			} else {
				r.Changes = append(r.Changes, ev)
			}
		}
	}
	byTime := func(s []ChangeEvent) func(i, j int) bool {
		return func(i, j int) bool {
			if !s[i].At.Equal(s[j].At) {
				return s[i].At.Before(s[j].At)
			}
			return s[i].TargetID < s[j].TargetID
		}
	}
	sort.SliceStable(r.Changes, byTime(r.Changes))
	sort.SliceStable(r.LateChanges, byTime(r.LateChanges))
	return r, nil
}

// ── [2] 합성 봉투 (§4, §14-1 1b) ───────────────────────────────────
//
// [2]의 산출 ChangeEvent에는 봉투도 ref도 없었다. 이대로면 최빈 정답 유형인
// 배포 회귀 가설이 confirmed될수록 §8.1-2의 ref 실재 검사에서 run이 실패한다
// — 인용할 원본이 아예 없기 때문이다. 그래서 도구 경로의 withEnvelopeRef와
// **같은 방식으로** 합성 봉투를 만들어 evidence store에 적재하고 ref를
// 발급한다. "change는 검사 면제" 같은 예외는 만들지 않는다(면제는 인용
// 무결성의 구멍이다).
//
// B-15 대조 — 합성 원천 `change`는 실행 가능한 도구가 아니다:
// registry에 `change`라는 도구는 없고 `list_changes`만 있다. 1a가 그 둘을
// 타입으로 분리해 뒀으므로(evidence.ObservationSource ↔ ExecutableTool)
// 여기서는 원천 이름만 쓰면 되고, prospective change 술어의 재조회는
// 하네스가 ExecutableTool(SrcChange) = list_changes로 조립한다. 레코드는
// 어느 경로로 왔든 같은 canonical 키를 갖는다.

// changeEnvelope는 한 대상의 변경 조회 결과를 합성 봉투로 만든다.
// 조회 단위가 대상당 1회이므로 봉투도 대상당 1개다.
//
// late는 첫 증상 이후의 변경이다 — [4]의 원인 후보 재료에서는 빠지지만
// 관측으로서는 실재하므로 봉투에는 싣고 딱지만 붙인다. 선후 판정은 §10이
// Window.ChangeFrom으로 한다(딱지가 아니라 시각이 판정 입력이다).
func changeEnvelope(from, to time.Time, evs, late []ChangeEvent) evidence.RawEnvelope {
	env := evidence.RawEnvelope{
		Status:  "normal",
		Summary: fmt.Sprintf("변경 %d건(첫 증상 이후 %d건 포함) — 창 %s/%s", len(evs)+len(late), len(late), from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339)),
		AssessmentBasis: "변경 이력 조회 — 존재 자체가 신호이고 원인 확정은 아니다. " +
			"첫 증상 이후 변경은 late 딱지가 붙는다(원인은 결과보다 먼저여야 한다).",
		ObservedRange: &evidence.RawRange{From: from.UTC(), To: to.UTC()},
	}
	add := func(list []ChangeEvent, isLate bool) {
		for _, ev := range list {
			f := evidence.RawFinding{
				"kind": ev.Kind, "target_id": ev.TargetID,
				"at":     ev.At.UTC().Format(time.RFC3339),
				"detail": ev.Detail,
				// ref는 조회면 지문이다 — 변경 원천에 행 ID가 없으므로
				// (종류, 대상, 시각)이 그 자리를 진다.
				"refs": []any{fmt.Sprintf("change:%s:%s:%s", ev.Kind, ev.TargetID,
					ev.At.UTC().Format(time.RFC3339))},
			}
			if isLate {
				f["late"] = true
			}
			env.Findings = append(env.Findings, f)
		}
	}
	add(evs, false)
	add(late, true)
	if len(env.Findings) > 0 {
		env.Status = "anomalous"
	}
	// 변경 조회는 절단하지 않는다 — 완전 조회 + 0건이면 observed_zero가 되고
	// "이 대상엔 변경이 없었다"가 배제 근거로 선다(§5.1 계약 1).
	env.Scopes = []evidence.RawScope{{
		Class: "change", Total: len(env.Findings), Returned: len(env.Findings), Omitted: 0,
	}}
	return env
}

// StoreChanges는 [2] 산출을 대상별 합성 봉투로 store에 적재하고 index에
// 투영한다. 대상별로 봉투 하나 → ref 하나 → 레코드 여럿(변경 건수 + 범위 행).
//
// domainOf는 대상의 도메인을 돌려준다(§5.1 Domain 열 — 정본은 [1] Triage).
// nil이면 빈 도메인으로 둔다. 배선(누가 언제 부르는가)은 §14-2의 몫이다.
func StoreChanges(r ChangeScanResult, st *evidence.Store, ix *evidence.Index,
	domainOf func(targetID string) string) ([]string, error) {

	if st == nil || ix == nil {
		return nil, fmt.Errorf("changes: evidence store·index 필수")
	}
	byTarget := map[string][]ChangeEvent{}
	lateByTarget := map[string][]ChangeEvent{}
	for _, ev := range r.Changes {
		byTarget[ev.TargetID] = append(byTarget[ev.TargetID], ev)
	}
	for _, ev := range r.LateChanges {
		lateByTarget[ev.TargetID] = append(lateByTarget[ev.TargetID], ev)
	}

	var eids []string
	for _, target := range r.Targets { // 정렬된 조회 대상 순 — 재생 가능한 EID 순서
		env := changeEnvelope(r.From, r.To, byTarget[target], lateByTarget[target])
		body, err := json.Marshal(env)
		if err != nil {
			return nil, fmt.Errorf("changes: 합성 봉투 직렬화(%s): %w", target, err)
		}
		// 적재 도구 이름은 **실행 채널**이다(원천 이름 change가 아니라).
		ref, err := st.Put(evidence.MustExecutableTool(evidence.SrcChange), body)
		if err != nil {
			return nil, err
		}
		args, _ := json.Marshal(map[string]any{
			"target": target,
			"from":   r.From.UTC().Format(time.RFC3339),
			"to":     r.To.UTC().Format(time.RFC3339),
		})
		domain := ""
		if domainOf != nil {
			domain = domainOf(target)
		}
		res, err := evidence.Project(evidence.ProjectRequest{
			Source:       evidence.SrcChange,
			Tool:         evidence.MustExecutableTool(evidence.SrcChange),
			TargetID:     target,
			Domain:       domain,
			WindowClass:  evidence.WindowFull,
			From:         r.From,
			To:           r.To,
			ParamsDigest: evidence.ParamsDigest(args),
			EnvelopeRef:  ref,
		}, env)
		if err != nil {
			return nil, fmt.Errorf("changes: 투영(%s): %w", target, err)
		}
		if len(res.UnknownClasses) > 0 {
			return nil, fmt.Errorf("changes: 사상표에 없는 class %v — 합성 봉투가 표와 어긋났다", res.UnknownClasses)
		}
		got, err := ix.AppendAll(res.Records)
		if err != nil {
			return nil, fmt.Errorf("changes: index 적재(%s): %w", target, err)
		}
		eids = append(eids, got...)
	}
	return eids, nil
}
