// 시크릿 스캔 게이트 (§15.3-2, §14-6 6c) — "밖으로 나가는 모든 바이트"에
// 적용하는 패턴 스캐너. 검출은 [MASKED] 치환 + 패턴명 목록이며, 원문은
// 훼손하지 않는다(store 원문 비마스킹 §15.3-1 — 스캔은 LLM-bound·리포트·
// 실패 선언 등 **나가는 사본**에만 적용된다).
//
// 패턴은 §15.3-2의 소집합이다: 할당식(password=… 계열) · Authorization
// (Bearer/Basic) · URI 자격증명(scheme://user:pass@) · PEM 블록 · JWT ·
// 장문 base64. 오탐 가드: 64자 헥사(sql_hash·trace id)는 base64 패턴에서
// 배제한다 — 식별자를 가리면 인용·감사가 죽는다.
package evidence

import (
	"regexp"
	"strconv"
	"strings"
)

const maskedToken = "[MASKED]"

type secretPattern struct {
	name string
	re   *regexp.Regexp
	// keepGroup — 치환 시 보존할 접두 그룹 번호(0이면 전체 치환).
	keepGroup int
}

var secretPatterns = []secretPattern{
	// PEM 블록 — BEGIN~END 전체.
	{name: "pem_block", re: regexp.MustCompile(`-----BEGIN [A-Z ]+-----[\s\S]*?-----END [A-Z ]+-----`)},
	// Authorization 헤더.
	{name: "authorization", re: regexp.MustCompile(`(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+`), keepGroup: 1},
	// URI 자격증명 — scheme://user:pass@ 의 비밀 부분만.
	{name: "uri_credential", re: regexp.MustCompile(`([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@`), keepGroup: 1},
	// 할당식 소집합 — 키는 남기고 값만 가린다. 값 클래스가 인용 형태
	// ("x y"·'x'·JSON "key":"value")를 덮어야 한다 — 첫판 [^\s,;'"]+는
	// 따옴표로 시작하는 값을 매치 자체를 못 해 지배적 형식(JSON·인용
	// 로그)이 통째로 샜다(6c 검증 D-2, 실측 4형 전부 누출).
	{name: "assignment", re: regexp.MustCompile(`(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret)["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s,;'"]+)`), keepGroup: 1},
	// JWT — eyJ 접두 3분절.
	{name: "jwt", re: regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b`)},
}

// base64Long — 장문 base64. 헥사 전용 문자열([0-9a-fA-F]만)은 해시·트레이스
// ID이므로 별도 검사로 배제한다.
// 끝 \b를 두지 않는다 — '='는 비단어 문자라 "…==" 꼬리에서 경계가 성립
// 하지 않아 패딩된 base64를 통째로 놓친다(시험이 잡은 함정).
var base64Long = regexp.MustCompile(`\b[A-Za-z0-9+/]{64,}={0,2}`)
var hexOnly = regexp.MustCompile(`^[0-9a-fA-F]+$`)

// MaskSecrets는 s의 시크릿 패턴을 [MASKED]로 치환하고 검출 패턴명을
// 돌려준다(중복 제거·안정 순서). 검출 0이면 s 그대로·nil이다.
func MaskSecrets(s string) (string, []string) {
	if s == "" {
		return s, nil
	}
	var hits []string
	hit := func(name string) {
		for _, h := range hits {
			if h == name {
				return
			}
		}
		hits = append(hits, name)
	}
	for _, p := range secretPatterns {
		if !p.re.MatchString(s) {
			continue
		}
		hit(p.name)
		if p.keepGroup > 0 {
			s = p.re.ReplaceAllString(s, "${"+strconv.Itoa(p.keepGroup)+"}"+maskedToken)
		} else {
			s = p.re.ReplaceAllString(s, maskedToken)
		}
	}
	// 장문 base64 — 헥사 전용은 배제(오탐 가드).
	s = base64Long.ReplaceAllStringFunc(s, func(m string) string {
		if hexOnly.MatchString(strings.TrimRight(m, "=")) {
			return m
		}
		hit("base64_long")
		return maskedToken
	})
	return s, hits
}

// clipRunes는 rune 기준 상한으로 자른다(마스킹 후 상한 재준수용 —
// 치환이 원문보다 길어질 수 있다).
func clipRunes(s string, max int) string {
	r := []rune(s)
	if len(r) <= max {
		return s
	}
	return string(r[:max])
}
