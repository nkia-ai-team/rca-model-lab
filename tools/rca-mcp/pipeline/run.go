// 파이프라인 조립 — seed에서 리포트까지 [1]~[6]을 한 줄로 잇는다
// (설계 §2). 각 단계의 계약은 이미 그어져 있고, 여기는 배선만 한다:
// 주입 자리(Screener·CandidateSource·Grouper·Verify)는
// 전부 Config로 받는다 — walking skeleton은 mock으로 걷고, 실 어댑터는
// 같은 자리에 끼운다. [3]만은 이제 LLM 자리가 아니다(스펙 §4 — 기계
// 스크리닝 배터리, LLM 토큰 0).
//
// [6]의 작문 단계(narrate.go)까지 여기서 잇는다: seed 원천 필드는
// 결정론 전사, 산문은 Writer(LLM 자리 6번째) — 작문은 §8.1 인용 게이트를
// 통과하거나 run을 실패시킨다(§15.5, 5c — 종전 WriterErr 관용은 폐기).
package pipeline

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/ledger"
	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/seed"
)

// Config는 파이프라인의 주입 지점 전부다. Sources에서 빼는 것이 출처
// ablation, Screener·Verify 대체가 각각 조사·검증 ablation이다
// (설계 §11.1 — 코드 수정 없이 구성으로).
type Config struct {
	Meta       TargetMetaFunc // [1] 도메인 해석의 정본
	GetChanges GetChangesFunc // [2]
	Lookback   time.Duration  // [2] 변경 조회를 앞으로 넓히는 폭

	// Store·Index — evidence 적재 경로(§5). **[2]와 [3]이 같은 것을
	// 쓴다**: 변경 합성 봉투와 스크리닝 봉투가 다른 index에 앉으면 [4]
	// 입력이 둘로 갈린다. store는 run 디렉토리에 매이므로 호출자(cmd)가
	// 만들어 넣는다.
	Store *evidence.Store
	Index *evidence.Index

	Screener Screener // [3] 스크리닝 배터리(스펙 §4 — 기계, LLM 0)

	Sources []CandidateSource // [4] 출처 모듈들
	Grouper Grouper           // [4]
	Gen     GenerateConfig    // [4]

	// Verify — [5] 검증 루프(§7). **라이브 경로다**: 실행 주체가 LLM에서
	// 하네스로 옮겨졌으므로(§7.1) 루프는 "조사자를 부르는 규칙"이 아니라
	// 그 자체가 주입 부품이다. 구현체는 loop 패키지.
	Verify VerifyLoop
	// AdoptAuditor — §7.7 심사점 A(5c). nil이면 confirmed 불가(fail-closed).
	AdoptAuditor AdoptAuditor

	Writer      Writer    // [6] 산문 작문 — nil이면 산문 빈 값
	RequestedAt time.Time // [6] 리포트의 요청 시각

	// Ledger — run 수첩. §14-6 6a: cmd가 journal(내구 기록)·usage 배선을
	// 부착한 수첩을 넘기는 자리다. nil이면 여기서 새로 만든다(구 동작 —
	// 시험·mock 경로 무수정).
	Ledger *ledger.Ledger
	// OnStage — 단계 전이 훅(§14-6 6a). run 상태 파일(SetStage)이 소비한다.
	// 실패 선언의 stage 필드는 이 훅이 아니라 StageError가 나른다 — 훅은
	// best-effort 진행 표시이고 선언은 오류 사슬이 정본이다.
	OnStage func(stage string)

	// StageTimeouts — 단계별 wall-clock 상한(§15.2-1 3층의 가운데 층,
	// 6b 골격). 항목이 없거나 0이면 그 단계는 run 전체 상한만 진다.
	// **값은 §14-7(§13) 실측에서 역산한다** — 여기는 집행 배관만.
	StageTimeouts map[string]time.Duration

	// §15.4 전제 블록 재료(6d) — cmd가 채운다. ClockSkew 빈 값은
	// unknown으로 정직하게 조립된다(fail-closed).
	ClockSkew    evidence.ClockSkew
	ReplaySource string
}

// StageError는 "어느 단계에서 죽었는가"를 오류 사슬에 싣는다(§15.5 —
// 실패 선언의 stage 필드가 선언의 핵심이다). first-match 판정기(cmd)가
// errors.As로 꺼낸다.
type StageError struct {
	Stage string // triage · changes · screen · generate · loop · report · write · setup
	Err   error
}

func (e *StageError) Error() string { return fmt.Sprintf("run: [%s]: %v", e.Stage, e.Err) }
func (e *StageError) Unwrap() error { return e.Err }

// CiteViolationError는 §8.1 인용 무결성 재위반이다(재시도 1회 소진).
// battery·probe·llm의 오류들과 같은 Reason() 규약.
type CiteViolationError struct {
	Violations []CiteViolation
}

func (e *CiteViolationError) Error() string {
	lines := make([]string, len(e.Violations))
	for i, v := range e.Violations {
		lines[i] = v.String()
	}
	return fmt.Sprintf("인용 무결성 재위반(§8.1-4 — 재시도 1회 소진): %s", strings.Join(lines, " / "))
}

func (e *CiteViolationError) Reason() ledger.FailureReason { return ledger.ReasonCitationViolation }

// Result는 한 인시던트 수사의 전 산출물이다. 중간 산출물을 전부 남기는
// 이유: 평가·감사가 단계별 기여를 추적한다(장부는 event sourcing 정본).
type Result struct {
	Triage  TriageResult
	Changes ChangeScanResult
	// Screen — [3] 산출 **요약**이다. 관측 알맹이는 Config.Index에 있다
	// (스펙 §4 "[3]의 산출은 evidence index가 된다").
	Screen ScreenResult
	// ChangeEIDs — [2] 합성 봉투가 index에 남긴 레코드(§4·1b). 인용
	// 무결성(§8.1)의 원본이 여기서 생긴다.
	ChangeEIDs []string
	Opened     []ledger.HypothesisEntry
	// Admission — [4] 등록 규칙(§6.2)의 기계 판정 보고서다. 딱지·반려 사유가
	// 감사 가능한 구조로 남는다. 소비자: 3b의 R 심사·재요구 루프 · 산출물 감사.
	Admission AdmissionReport
	Decision  ledger.Decision
	// Verify — [5] 루프의 산출 전량(§8 판정·예산 실적·이음새 기록).
	// Decision은 이것의 구 계약 사영이다.
	Verify VerifyResult
	Rca    ledger.RcaResult
	UI     ledger.UIReport
	Ledger *ledger.Ledger


	// UnexplainedAnomalies — 이상 관측 대상 중 어느 가설에도(의심 대상·
	// 사슬 entity) 등장하지 않는 것. 설계 §14 ①의 계측(비게이트) —
	// "전체 설명력"을 의무 축으로 승격할지는 이 실측 축적이 결정한다.
	UnexplainedAnomalies []string

	// ReportMasked — 리포트 산문에서 시크릿 스캔이 가린 필드 목록
	// (§15.3-3, 6c). 소비자: run-meta 기록 + 6d 한계 블록.
	ReportMasked []string
}

// Run은 seed 하나를 수사해 리포트까지 만든다.
func Run(ctx context.Context, s seed.IncidentSeed, cfg Config) (Result, error) {
	if cfg.Verify == nil {
		return Result{}, &StageError{Stage: "setup",
			Err: fmt.Errorf("[5] 검증 루프(VerifyLoop) 필수 — §14-4 4b가 구 Investigator·Verifier 쌍을 대체했다")}
	}
	// store·index는 [2]와 [3]의 공용 적재 경로다 — 없으면 [3] 산출(index)이
	// 설 자리가 없으므로 조용히 넘기지 않는다(스펙 §4).
	if cfg.Store == nil || cfg.Index == nil {
		return Result{}, &StageError{Stage: "setup",
			Err: fmt.Errorf("evidence Store·Index 필수 — [3] 산출이 index다")}
	}
	// 단계 전이 훅 + 단계 시한(§15.2-1) — stage()는 새 단계의 ctx를 돌려
	// 준다. 시한 초과는 context.DeadlineExceeded로 죽고 판정기가
	// wall_clock으로 사상한다.
	var stageCancel context.CancelFunc
	stage := func(name string) context.Context {
		if stageCancel != nil {
			stageCancel()
			stageCancel = nil
		}
		if cfg.OnStage != nil {
			cfg.OnStage(name)
		}
		if d := cfg.StageTimeouts[name]; d > 0 {
			var sctx context.Context
			sctx, stageCancel = context.WithTimeout(ctx, d)
			return sctx
		}
		return ctx
	}
	defer func() {
		if stageCancel != nil {
			stageCancel()
		}
	}()

	var r Result
	var err error
	sctx := stage("triage")
	if r.Triage, err = Triage(sctx, s, cfg.Meta); err != nil {
		return Result{}, &StageError{Stage: "triage", Err: err}
	}
	sctx = stage("changes")
	if r.Changes, err = ScanChanges(sctx, r.Triage, cfg.Lookback, cfg.GetChanges); err != nil {
		return Result{}, &StageError{Stage: "changes", Err: err}
	}
	// [2] 합성 봉투 적재(§4) — 배터리는 list_changes를 부르지 않고 이 결과를
	// 재사용하므로, 변경 관측이 index에 오르는 자리는 여기 하나다.
	if r.ChangeEIDs, err = StoreChanges(r.Changes, cfg.Store, cfg.Index,
		func(t string) string { return r.Triage.TargetDomains[t] }); err != nil {
		return Result{}, &StageError{Stage: "changes", Err: err}
	}

	// 장부는 [3] 앞에서 만든다 — 배터리가 required_views_extended를 여기
	// 쓴다(§4 분모 재생 가능성). cmd가 journal 부착 수첩을 넘기면 그것이
	// 정본이다(§14-6 6a).
	r.Ledger = cfg.Ledger
	if r.Ledger == nil {
		r.Ledger = ledger.New()
	}
	sctx = stage("screen")
	deps := ScreenDeps{Store: cfg.Store, Index: cfg.Index, Ledger: r.Ledger}
	if r.Screen, err = RunScreen(sctx, r.Triage, cfg.Screener, deps); err != nil {
		return Result{}, &StageError{Stage: "screen", Err: err}
	}
	// store 쿼터(§15.2-6, 6b 골격) — admission이 대상 어휘를 동결한 직후
	// N이 확정된다. 산식 가안: §15.2-6 역산 구조(N=19에서 ~130대)의 약
	// 2배 여유 — 값 재정은 §14-7 실측 몫. env는 §13.1 경계 주입·운영
	// 조정용(계수만 조정 — 산식 모양 base+per×N은 스펙 계약).
	cfg.Store.SetQuota(quotaEnvInt("RCA_STORE_QUOTA_BASE", 100) +
		quotaEnvInt("RCA_STORE_QUOTA_PER_TARGET", 10)*len(r.Screen.Targets))

	// 명제 장부(§6.0)는 index에 부착된다 — 이후 append가 같은 임계 구역에서
	// 장부를 갱신하므로 supersede 직후에도 미실측으로 보이는 창이 없다
	// (4차 A-3). [3] 산출이 다 실린 뒤 붙이면 기존 레코드는 재생으로 반영된다.
	props := evidence.NewLedger(cfg.Index)
	// 위상 어휘(§6.2-1ⓐ)의 원천은 [3]의 동결 조사 범위다(seed ∪ A0 신규).
	// W3 확장분은 §14-4의 루프가 Extend로 append한다 — append-only이므로
	// 여기서 만든 값이 run 전체의 정본이다.
	// §5.3 payload 축약 누적기(§14-7 계측) — 재생성까지 run 전체 공유.
	its := &ledger.IndexTruncStats{}
	in := GenerateInput{Triage: r.Triage, Changes: r.Changes, Evidence: cfg.Index,
		Vocab: NewTopologyVocab(r.Screen.Targets...), Propositions: props,
		IndexTrunc: its}
	sctx = stage("generate")
	if r.Opened, r.Admission, err = Generate(sctx, in, cfg.Sources, cfg.Grouper, r.Ledger, cfg.Gen); err != nil {
		return Result{}, &StageError{Stage: "generate", Err: err}
	}
	sctx = stage("loop")

	// [5] — 신 검증 루프(§7). 창은 [1]이 확정한 이벤트 시간창이고, onset은
	// 그 시작(첫 증상 시각)이다: onset_narrow ±5분의 중심을 다른 데서 지어낼
	// 근거가 없다(§7.1 — 중심 없는 onset_narrow는 조립 반려다).
	vr, err := cfg.Verify.Verify(sctx, VerifyInput{
		Ledger: r.Ledger, Index: cfg.Index, Propositions: props, Vocab: in.Vocab,
		From: r.Triage.From, To: r.Triage.To, Onset: r.Triage.From,
		GateWindow:     GateRequiredWindow(r.Triage),
		TargetDomains:  r.Triage.TargetDomains,
		SymptomTargets: SymptomTargetsOf(r.Triage),
		Topology:       s.Topology,
		AdoptAuditor:   cfg.AdoptAuditor,
		LayerAComplete: layerAComplete(r.Triage, r.Screen),
		// §7.5 재생성은 [4]와 **같은 부품**으로 돈다 — 다른 설정으로 돌면
		// 재생성 가설만 다른 잣대로 심사된다. 재호출 범위는 index 소비
		// 출처 + Grouper뿐이고(§7.6 43k 모양), 그것이 하나도 없으면
		// NewRegenerator가 nil을 돌려 루프는 재생성을 생략 기록한다.
		Regenerate: NewRegenerator(in, cfg.Sources, cfg.Grouper, r.Ledger, cfg.Gen),
		// W3 신규 대상의 도메인 해석(§7.4) — 정본은 [1]과 같은 get_target_meta다.
		// 여기서 위상 노드의 `type`을 도메인으로 읽지 않는 이유는 battery.freeze와
		// 같다: 실물 값이 "custom_monitor" 따위라 targets.type enum이 아니고,
		// 없는 도메인을 지어내면 그 대상에서 db_* 호출이 전부 실패한다.
		TargetDomainsOf: func(ctx context.Context, id string) string {
			metas, merr := cfg.Meta(ctx, []string{id})
			if merr != nil {
				return ""
			}
			return metas[id].Domain
		},
	})
	if err != nil {
		return Result{}, &StageError{Stage: "loop", Err: err}
	}
	r.Decision, r.Verify = vr.Decision, vr
	r.UnexplainedAnomalies = unexplainedAnomalies(r.Triage, cfg.Index, r.Opened)

	now := cfg.Gen.Now
	if now == nil {
		now = time.Now
	}
	// [6] — next_steps·missing이 기계 조립이라(§14-4 4c) 리포트도 [5]와 같은
	// 판정 문맥을 받는다. 다른 값을 주면 루프가 "판정했다"고 닫은 술어가
	// 리포트에서 미판정으로 되살아난다.
	rc := ledger.ReportContext{
		Propositions: props, Index: cfg.Index,
		Gate: ledger.GateContext{Index: cfg.Index, Now: now(), RequiredWindow: GateRequiredWindow(r.Triage)},
		// §15.4 전제 블록 재료(6d). 기준선 창은 도구 계약(§5.3)의 자동
		// 창 [첫 증상-60m, 첫 증상) — 실제 시각으로 조립한다.
		ClockSkew:    cfg.ClockSkew,
		ReplaySource: cfg.ReplaySource,
		BaselineWindow: fmt.Sprintf("[%s, %s) 자기 기준선(첫 증상 전 60분)",
			r.Triage.From.Add(-60*time.Minute).UTC().Format(time.RFC3339),
			r.Triage.From.UTC().Format(time.RFC3339)),
		IndexTrunc: its,
	}
	sctx = stage("report")
	if r.Rca, r.UI, err = ledger.AssembleReport(r.Ledger, rc, cfg.RequestedAt, now()); err != nil {
		return Result{}, &StageError{Stage: "report", Err: err}
	}
	// §15.4 한계 블록 중 ScreenResult가 원천인 몫(6d) — 계층 B 절단·
	// §5.2 롤업 미조회 관점. confirmed여도 숨기지 않는다.
	r.UI.Limitations.LayerBTruncated = r.Screen.TruncatedLayerB
	for _, k := range r.Screen.Unobserved {
		r.UI.Limitations.UnobservedViews = append(r.UI.Limitations.UnobservedViews,
			string(k.Tool)+"@"+k.TargetID)
	}

	glossary, err := fillSeedFields(sctx, s, r.Triage, cfg.Meta, &r.Rca, &r.UI)
	if err != nil {
		return Result{}, &StageError{Stage: "report", Err: fmt.Errorf("seed 필드: %w", err)}
	}
	if cfg.Writer != nil {
		sctx = stage("write")
		ctx := sctx // 이하 Writer 호출은 write 단계 시한을 진다
		// §8.1 인용 무결성 게이트(5c 배선) + §15.5 관용 폐기 — 작문은
		// 게이트를 통과하거나 run을 실패시킨다. 종전 WriterErr 기록 관용은
		// "실패를 리포트처럼 꾸미지 마라"(사용자 결정)로 폐기됐다.
		gate := CiteGate{Index: cfg.Index, Store: cfg.Store}
		// Title은 LLM-bound taint(§15.1)라 마스킹을 거쳐 보낸다(§15.3-2 —
		// 2a 게이트 D-1: 리포트 전사·표시명 표는 마스킹되는데 Writer 프롬
		// 프트의 제목만 원문이 새던 구멍. 시크릿 canary 시험이 실검출).
		maskedTitle, _ := evidence.MaskSecrets(s.Title)
		win := WriteInput{Title: maskedTitle, Rca: r.Rca, UI: r.UI, Targets: glossary}
		p, werr := cfg.Writer.Write(ctx, win)
		var vs []CiteViolation
		if werr == nil {
			if p, vs = gate.Apply(p); len(vs) > 0 {
				// 재시도 1회 — 위반 목록과 행동 지침이 함께 간다(§8.1-4).
				win.Feedback = FeedbackText(vs)
				if p, werr = cfg.Writer.Write(ctx, win); werr == nil {
					p, vs = gate.Apply(p)
				}
			}
		}
		switch {
		case werr != nil:
			return Result{}, &StageError{Stage: "write",
				Err: fmt.Errorf("작문 실패(§15.5 — 강등 리포트는 만들지 않는다): %w", werr)}
		case len(vs) > 0:
			return Result{}, &StageError{Stage: "write", Err: &CiteViolationError{Violations: vs}}
		}
		applyProse(p, &r.Rca, &r.UI)
	}
	// 리포트 전문 스캔(§15.3-3, 6c) — 진짜 경계는 리포트다: 제목·표시명
	// 경유 유출이 시연됐다(§15.1 taint). **최종 조립본**의 자유문 필드
	// 전수를 건다 — 산문 5필드만 걸던 초판은 LLM 자유 서술(mechanism →
	// Cause.Text·Alternatives[].Text·Hypotheses[].Description)을 놓쳤다
	// (6c 검증 D-1: entity_key 경유 유입 벡터).
	r.ReportMasked = maskReport(&r.Rca, &r.UI)
	return r, nil
}

// maskReport는 최종 리포트의 자유문 필드 전수에 시크릿 스캐너를 적용한다.
// 기계 조립 식별자(target_id·EID·guards 토큰)는 대상이 아니다.
func maskReport(rca *ledger.RcaResult, ui *ledger.UIReport) []string {
	var masked []string
	scan := func(name string, s *string) {
		if out, hits := evidence.MaskSecrets(*s); len(hits) > 0 {
			*s = out
			masked = append(masked, name)
		}
	}
	scan("headline", &rca.Headline)
	scan("symptom_text", &rca.Symptom.Text)
	scan("problem_text", &rca.Problem.Text)
	scan("cause_text", &rca.Cause.Text)
	for i := range rca.Alternatives {
		scan(fmt.Sprintf("alternatives[%d].text", i), &rca.Alternatives[i].Text)
	}
	scan("diagnosis_summary", &ui.Diagnosis.Summary)
	scan("conclusion_summary", &ui.ConclusionSummary)
	for i := range ui.Hypotheses {
		scan(fmt.Sprintf("hypotheses[%d].description", i), &ui.Hypotheses[i].Description)
	}
	return masked
}

// layerAComplete는 §8 요건 "계층 A 미조사 증상 멤버 없음"의 기계 판정이다.
//
// 분모는 [3]이 확정한 필수 관점 집합(§4)이고, 그중 관측을 못 남긴 키
// (ScreenResult.Unobserved)가 **증상 멤버 대상**에 하나라도 있으면 미충족이다.
// 증상 멤버로 좁히는 것이 §8 문면이다 — 이웃까지 요구하면 계층 B 절단이
// 곧바로 confirmed를 막는다.
func layerAComplete(tri TriageResult, sc ScreenResult) bool {
	members := map[string]bool{}
	for _, g := range tri.MemberGroups {
		members[g.TargetID] = true
	}
	for _, k := range sc.Unobserved {
		if members[k.TargetID] {
			return false
		}
	}
	return true
}

// unexplainedAnomalies — 설계 §14 ①의 계측. 이상 관측(triage anomaly
// 멤버 그룹 + [3] anomalous 관측)의 대상이 개설된 어느 가설에도 등장
// 하지 않으면 "설명 안 된 잔여"다. 판정에 쓰지 않는다 — 기록만.
//
// [3] 몫의 판정 기준은 index 레코드다: RecordKind=finding ∧
// Quality.Status=anomalous(종전 Finding.SourceStatus==SourceAnomalous의
// 자리). query_scope·meta 레코드는 관측이 아니라 계약·감사 재료라 세지
// 않는다. superseded 레코드도 빠진다(Active 뷰 — 재조회로 뒤집힌 이상을
// 잔여로 셈하면 §5.7이 무의미해진다).
func unexplainedAnomalies(tri TriageResult, ix *evidence.Index, opened []ledger.HypothesisEntry) []string {
	covered := map[string]bool{}
	for _, h := range opened {
		covered[h.TargetID] = true
		for _, st := range h.Chain {
			covered[st.Entity] = true
		}
	}
	seen := map[string]bool{}
	var out []string
	add := func(t string) {
		if t != "" && !covered[t] && !seen[t] {
			seen[t] = true
			out = append(out, t)
		}
	}
	for _, g := range tri.MemberGroups {
		if g.HasAnomaly {
			add(g.TargetID)
		}
	}
	if ix != nil {
		for _, rec := range ix.Active() {
			// change 레코드는 제외(2c 독립 검증 Gap 3) — 변경 이벤트는 [2]의
			// 관측이지 [3]의 이상 관측이 아니다. 구 계약(triage anomaly +
			// [3] anomalous 발견)의 범위를 승계한다.
			if rec.Provenance.Source == evidence.SrcChange {
				continue
			}
			if rec.RecordKind == evidence.KindFinding &&
				rec.Quality.Status == evidence.StatusAnomalous {
				add(rec.TargetID)
			}
		}
	}
	return out
}

// quotaEnvInt는 쿼터 산식 계수의 env 조정이다(§13.1 경계 주입·운영) —
// 산식 모양(base + per×N)은 스펙 계약이라 계수만 연다. n>0만 유효.
func quotaEnvInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}
