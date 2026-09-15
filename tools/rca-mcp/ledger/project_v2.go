// 장부 → §8 판정 입력(ViewV2)의 사영 — docs/spec-agent-structure.md §8이
// 정본이다.
//
// **왜 ledger에 있는가**(§14-5 5a 이관): 종전에는 loop 패키지 안에만 있었고,
// 리포트 조립(report.go)은 그 자리에 갈 수 없어 구 표면(Rank + Snapshot(View))을
// 계속 읽었다. 구 표면을 폐기하면서 사영을 여기로 올린다 — 판정과 리포트가
// **같은 한 사영**을 읽는 것이 계약이다. 두 곳에서 각자 조립하면 리포트의
// 순위와 루프의 순위가 언젠가 갈린다.
package ledger

import "github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"

// ProjectV2는 장부 + 명제 장부에서 §8 판정 입력을 조립한다.
//
// # 판단 지점 — NewObservationEIDs의 결박 범위
//
// §8의 "신규 관측 ≥1"은 "선언 이후 실행이 낳은 레코드"다(C-9). 실행은
// probe_executed{produced_eids}로 남는데, 그 이벤트는 (가설, probe) 결박을
// 갖는다(구 계약 — §14-4 4c에서 소멸). **결박 가설로 좁히지 않는다**: 한
// 도구 호출이 낳은 레코드는 그 명제를 지목한 **모든** 가설의 술어를 해소하며
// (요청은 관측 키 하나로 묶이고 PredIDs는 가설을 넘나든다 — §7.1),
// 결박은 장부 형식의 잔재이지 관측의 소유권이 아니다. 대신 **개설 시점
// (CreatedSeq) 이후**로 좁혀 "선언 이후"는 그대로 지킨다.
//
// **무효 술어도 판정한다**: Undecidable·AuditedOut의 셈 제외는 Tally(§8
// Count)의 몫이고, 여기서 미리 빼면 감사에서 "왜 안 셌나"가 사라진다.
func ProjectV2(l *Ledger, props *evidence.Ledger, gc GateContext, layerAComplete bool) ViewV2 {
	if l == nil {
		return ViewV2{LayerAComplete: layerAComplete}
	}
	return projectV2(l, l.ActiveHypotheses(), props, gc, layerAComplete)
}

// ProjectV2Terminal은 **종료된 수첩**의 사영이다 — 리포트 조립이 쓴다.
//
// ActiveHypotheses를 쓸 수 없는 이유: 종료 선언이 채택 가설에 adopted,
// 접힌 경쟁에 inferior 딱지를 붙이고 나면 lifecycle=active가 하나도 남지
// 않아 순위가 통째로 빈다. 리포트가 실어야 하는 것은 정확히 그 셋(채택 +
// 열세 + 미해소)이므로 **반증된 것만 뺀다** — 반증 가설의 카드는 리포트가
// 따로 붙인다("왜 아닌지"의 기록).
func ProjectV2Terminal(l *Ledger, props *evidence.Ledger, gc GateContext) ViewV2 {
	if l == nil {
		return ViewV2{}
	}
	var hs []HypothesisRecord
	for _, id := range l.hypoOrder {
		if h := l.hypos[id]; h.Lifecycle != LifeRefuted {
			hs = append(hs, *h)
		}
	}
	return projectV2(l, hs, props, gc, false)
}

func projectV2(l *Ledger, records []HypothesisRecord, props *evidence.Ledger,
	gc GateContext, layerAComplete bool) ViewV2 {

	view := ViewV2{LayerAComplete: layerAComplete}
	produced := producedBySeq(l)
	for _, h := range records {
		js := make([]PredJudgment, 0, len(h.Entry.PredictedSignals))
		for _, p := range h.Entry.PredictedSignals {
			js = append(js, JudgePredicate(p, props, gc))
		}
		hv := HypothesisV2{
			ID: h.Entry.ID, Identity: h.Entry.IdentityKey, CreatedSeq: h.CreatedSeq,
			Judgments: js, Preds: h.Entry.PredictedSignals,
			Chain:              ChainClaimsOf(l, h.Entry.ID),
			NewObservationEIDs: map[string]bool{},
			Temporal:           temporalOf(l, h.Entry.ID),
			// RecoveryDone·DiscriminationExhausted는 외부 판정 입력이다
			// (§8 C-8·§7.2). fail-closed 기본값 false — 회수 의무의 생산은
			// 미배선이다(5c 착공 실측: 대상 산정식(§5.3-6 축약)의 장부
			// 원천이 없다. 사용자 결정 대기 — impl-tracking 5c).
		}
		// §7.7 심사점 A 소비 — AuditAPassed(마지막 라운드 요약) + 탈락
		// 구간의 지지 강등. 둘 다 adoption_audited 이벤트에서 재생된다.
		hv.AuditAPassed = adoptionAuditOf(l, h.Entry.ID, &hv, gc)
		for seq, eids := range produced {
			if seq <= h.CreatedSeq {
				continue
			}
			for _, eid := range eids {
				hv.NewObservationEIDs[eid] = true
			}
		}
		view.Hypotheses = append(view.Hypotheses, hv)
	}
	return view
}

// producedBySeq는 probe_executed가 낳은 EID를 이벤트 순번별로 모은다.
func producedBySeq(l *Ledger) map[int][]string {
	out := map[int][]string{}
	for _, ev := range l.Events() {
		if p, ok := ev.Payload.(ProbeExecuted); ok && len(p.ProducedEIDs) > 0 {
			out[ev.Seq] = append(out[ev.Seq], p.ProducedEIDs...)
		}
	}
	return out
}

// adoptionAuditOf는 §7.7 심사점 A의 소비다.
//
//   - 반환 = AuditAPassed: 마지막 라운드 요약(ClaimID="")의 Passed.
//   - 부수 효과 = 탈락 구간 강등: Passed=false인 구간 판정의 ClaimID가
//     아직 active인 사슬 구간이면, 그 구간을 지지하는 판정의 자격을
//     qualified→weak로 강등한다(§7.7 소비 규칙 — "등급만 바꾸면 탈락
//     EID가 passed 수·경쟁 순위에 계속 기여"). 정밀화가 구간을
//     supersede하면 새 ClaimID는 미심사라 구 탈락의 효력은 접힌다.
//
// "구간을 지지하는 판정"의 판별(구현 확정 — 스펙은 링크 모델 문면):
// 판정이 인용한 레코드 중 하나라도 ① terminal 구간이면 EntityKey typed
// 동치 ② 그 외 구간이면 TargetID가 구간 Entity와 일치 — 하면 그 구간의
// 지지로 본다.
func adoptionAuditOf(l *Ledger, hypo string, hv *HypothesisV2, gc GateContext) bool {
	passed := false
	failed := map[string]bool{}
	for _, ev := range l.Events() {
		p, ok := ev.Payload.(AdoptionAudited)
		if !ok || p.Hypothesis != hypo {
			continue
		}
		if p.ClaimID == "" {
			passed = p.Passed // 마지막 요약이 이긴다
			continue
		}
		if !p.Passed {
			failed[p.ClaimID] = true
		}
	}
	if len(failed) == 0 {
		return passed
	}
	for _, c := range hv.Chain { // active 구간만 온다(ChainClaimsOf)
		if !failed[c.ClaimID] {
			continue
		}
		for j := range hv.Judgments {
			if hv.Judgments[j].Verdict != VerdictSupportedQualified {
				continue
			}
			if judgmentBacksClaim(&hv.Judgments[j], c, gc) {
				hv.Judgments[j].Verdict = VerdictSupportedWeak
			}
		}
	}
	return passed
}

// judgmentBacksClaim — 판정 인용 레코드가 그 사슬 구간을 지지하는가.
func judgmentBacksClaim(j *PredJudgment, c ChainClaim, gc GateContext) bool {
	if c.Kind == ChainTerminalEntity {
		want := CanonEntityKey(c.EntityKey)
		for _, k := range j.EntityKeys {
			if k == want {
				return true
			}
		}
		return false
	}
	if gc.Index == nil {
		return false
	}
	for _, eid := range j.EIDs {
		if rec, ok := gc.Index.Get(eid); ok && rec.TargetID == c.EntityKey {
			return true
		}
	}
	return false
}

// temporalOf는 그 가설의 §10 판정 기록을 모은다(판정 본체는 이 단계 밖).
func temporalOf(l *Ledger, hypo string) []TemporalJudged {
	var out []TemporalJudged
	for _, ev := range l.Events() {
		if p, ok := ev.Payload.(TemporalJudged); ok && p.Hypothesis == hypo {
			out = append(out, p)
		}
	}
	return out
}

// ChainClaimsOf는 그 가설의 active 사슬 구간이다(§7.3 — superseded 제외).
// §8 말단 깊이 게이트가 읽는다. 사슬 주장 자체를 만드는 것은 정밀화(§7.3)다.
func ChainClaimsOf(l *Ledger, hypo string) []ChainClaim {
	var claims []ChainClaim
	superseded := map[string]bool{}
	for _, ev := range l.Events() {
		p, ok := ev.Payload.(ChainClaimAsserted)
		if !ok || p.Hypothesis != hypo {
			continue
		}
		claims = append(claims, p.Claim)
		if p.Claim.Supersedes != "" {
			superseded[p.Claim.Supersedes] = true
		}
	}
	out := claims[:0]
	for _, c := range claims {
		if !superseded[c.ClaimID] {
			out = append(out, c)
		}
	}
	return out
}
