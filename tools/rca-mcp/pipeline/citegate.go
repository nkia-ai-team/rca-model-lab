// §8.1 인용 무결성 게이트 — 문장화 LLM의 출력은 리포트가 되기 전에 이
// 기계 검사를 통과한다(§14-5 5b). 전례: registry.go "ref 지어냄" 실측.
//
// 부품만 여기 있다 — writer 배선(산문 서식에 슬롯·인용을 요구하는 프롬프트,
// 위반 피드백 재시도 1회 → run 실패 §15.5)은 5c의 narrate·writer 재작성이
// 진다. 검사 대상 계약:
//
//  1. EID 실재: 산문에 등장하는 모든 EID(EIX-…)가 index에 실재하고 active.
//     문장화 입력은 active 사영이므로(§8.1-1) 위반 = LLM이 지어낸 EID다.
//  2. ref 실재: 인용 EID의 EnvelopeRef가 evidence store에 실존.
//  3. 수치 불변: 수치는 산문에 직접 쓰지 않고 {EID.경로} 슬롯로 쓰며
//     하네스가 치환한다 — 26B급 반올림·단위 사고(lucida μs→ms 전례) 차단.
//     경로는 닫힌 목록이다(fail-closed). 신뢰도는 목록에 없다 — §8.1
//     "신뢰도 수치는 슬롯 제외, 산문은 등급 문구만".
package pipeline

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// CiteViolationKind는 위반의 종별이다 — 재시도 피드백과 감사가 읽는다.
type CiteViolationKind string

const (
	// CiteUnknownEID — index에 없는 EID(지어냄).
	CiteUnknownEID CiteViolationKind = "unknown_eid"
	// CiteInactiveEID — 실재하나 superseded(active 사영 입력 계약 위반).
	CiteInactiveEID CiteViolationKind = "inactive_eid"
	// CiteMissingRef — 인용 EID의 EnvelopeRef가 store에 없음.
	CiteMissingRef CiteViolationKind = "missing_ref"
	// CiteBadSlot — 허용 목록 밖 슬롯 경로(신뢰도 포함 — 닫힌 목록).
	CiteBadSlot CiteViolationKind = "bad_slot"
	// CiteValueUnavailable — 슬롯 경로는 유효하나 그 레코드에 값이 없음
	// (nil) — 없는 값을 문장이 주장하게 둘 수 없다.
	CiteValueUnavailable CiteViolationKind = "value_unavailable"
)

// CiteViolation 하나가 재시도 피드백 한 줄이 된다.
type CiteViolation struct {
	Kind  CiteViolationKind
	Field string // Prose 필드명
	Token string // 문제의 원문 토큰
}

func (v CiteViolation) String() string {
	return fmt.Sprintf("%s: %s (%s)", v.Kind, v.Token, v.Field)
}

// CiteGate는 §8.1의 검사·치환기다. Store가 nil이면 ref 실재 검사(2)는
// 위반으로 문다 — 검사 불능은 통과가 아니다(fail-closed).
type CiteGate struct {
	Index *evidence.Index
	Store *evidence.Store
}

var (
	// eidToken — 산문에 등장하는 모든 EID 꼴. 슬롯·인용 어느 문법이든
	// 검사 1·2는 전 토큰에 적용한다(괄호 없이 흘려 쓴 EID도 잡는다).
	eidToken = regexp.MustCompile(`EIX-\d+`)
	// slotToken — 수치 슬롯 {EIX-0007.Effect.Magnitude}.
	slotToken = regexp.MustCompile(`\{(EIX-\d+)\.([A-Za-z.]+)\}`)
)

// proseFields는 검사 대상 필드의 순회다 — 필드가 늘면 여기만 는다.
func proseFields(p *Prose) []struct {
	name string
	s    *string
} {
	return []struct {
		name string
		s    *string
	}{
		{"headline", &p.Headline},
		{"symptom_text", &p.SymptomText},
		{"problem_text", &p.ProblemText},
		{"diagnosis_summary", &p.DiagnosisSummary},
		{"conclusion_summary", &p.ConclusionSummary},
	}
}

// Apply는 검사와 슬롯 치환을 한 번에 수행한다. 위반이 하나라도 있으면
// 치환 전 원문과 위반 목록을 돌려준다 — 부분 치환된 산문을 내보내지
// 않는다(재시도 입력은 원문이어야 피드백이 좌표를 가리킨다).
func (g CiteGate) Apply(p Prose) (Prose, []CiteViolation) {
	var vs []CiteViolation
	out := p
	for _, f := range proseFields(&out) {
		vs = append(vs, g.checkEIDs(f.name, *f.s)...)
		sub, svs := g.substitute(f.name, *f.s)
		vs = append(vs, svs...)
		*f.s = sub
	}
	if len(vs) > 0 {
		return p, vs
	}
	return out, nil
}

// checkEIDs — 검사 1·2: 전 EID 토큰의 실재·active·ref.
func (g CiteGate) checkEIDs(field, text string) []CiteViolation {
	var vs []CiteViolation
	seen := map[string]bool{}
	for _, eid := range eidToken.FindAllString(text, -1) {
		if seen[eid] {
			continue
		}
		seen[eid] = true
		rec, ok := g.Index.Get(eid)
		if !ok {
			vs = append(vs, CiteViolation{CiteUnknownEID, field, eid})
			continue
		}
		if heir, err := g.Index.ActiveHeir(eid); err != nil || heir != eid {
			vs = append(vs, CiteViolation{CiteInactiveEID, field, eid})
			continue
		}
		if g.Store == nil || !g.Store.Has(rec.Provenance.EnvelopeRef) {
			vs = append(vs, CiteViolation{CiteMissingRef, field, eid})
		}
	}
	return vs
}

// substitute — 검사 3: 슬롯 치환. 경로는 닫힌 목록이고 값 부재(nil)는
// 위반이다. 치환 값의 서식은 결정론이다(재현 계약).
func (g CiteGate) substitute(field, text string) (string, []CiteViolation) {
	var vs []CiteViolation
	sub := slotToken.ReplaceAllStringFunc(text, func(tok string) string {
		m := slotToken.FindStringSubmatch(tok)
		rec, ok := g.Index.Get(m[1])
		if !ok {
			// 검사 1이 이미 unknown_eid로 물었다 — 중복 위반은 내지 않는다.
			return tok
		}
		val, ok := slotValue(rec, m[2])
		if !ok {
			vs = append(vs, CiteViolation{CiteBadSlot, field, tok})
			return tok
		}
		if val == "" {
			vs = append(vs, CiteViolation{CiteValueUnavailable, field, tok})
			return tok
		}
		return val
	})
	return sub, vs
}

// slotValue는 허용 경로의 값을 결정론 서식으로 뽑는다. (값, 경로 유효)를
// 돌려주며 값 ""는 "경로는 맞으나 그 레코드에 없음"이다.
func slotValue(rec evidence.EvidenceIndexRecord, path string) (string, bool) {
	num := func(f *float64) string {
		if f == nil {
			return ""
		}
		return strconv.FormatFloat(*f, 'g', -1, 64)
	}
	ts := func(t *time.Time) string {
		if t == nil {
			return ""
		}
		return t.UTC().Format(time.RFC3339)
	}
	switch path {
	case "Effect.Magnitude":
		return num(rec.Effect.Magnitude), true
	case "Effect.Observed":
		return num(rec.Effect.Observed), true
	case "Effect.Baseline":
		return num(rec.Effect.Baseline), true
	case "Window.ChangeFrom":
		return ts(rec.Window.ChangeFrom), true
	case "Window.ChangeTo":
		return ts(rec.Window.ChangeTo), true
	case "Quality.SampleN":
		if rec.Quality.SampleN == nil {
			return "", true
		}
		return strconv.Itoa(*rec.Quality.SampleN), true
	}
	return "", false
}

// FeedbackText는 재시도 1회의 피드백 본문이다(§8.1-4) — 위반 목록과 행동
// 지침이 함께 간다. 재위반 처분(run 실패)은 호출자(5c) 몫이다.
func FeedbackText(vs []CiteViolation) string {
	var b strings.Builder
	b.WriteString("인용 무결성 위반 — 아래를 고쳐 다시 써라. 목록에 없는 EID·수치 직접 기입 금지.\n")
	for _, v := range vs {
		b.WriteString("- " + v.String() + "\n")
	}
	return b.String()
}
