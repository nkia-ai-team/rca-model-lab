package evidence

import (
	"strings"
	"testing"
)

// 패턴 소집합(§15.3-2) — 검출·치환·패턴명.
func TestMaskSecretsPatterns(t *testing.T) {
	cases := []struct {
		name, in   string
		wantHit    string
		mustAbsent string // 치환 후 남으면 안 되는 조각
	}{
		{"할당식", "connect failed password=Adm1nPass retry", "assignment", "Adm1nPass"},
		{"할당식 콜론", "api_key: sk-abcdef1234567890 quota", "assignment", "sk-abcdef"},
		{"authorization", "Authorization: Bearer abc.def.tok123456", "authorization", "abc.def.tok"},
		{"uri 자격증명", "dsn postgres://lucida:s3cr3tpw@10.0.0.1:5432/db fail", "uri_credential", "s3cr3tpw"},
		{"pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "pem_block", "MIIabc"},
		{"jwt", "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4", "jwt", "SflKxw"},
		{"장문 base64", "blob " + strings.Repeat("Qk", 34) + "+A==", "base64_long", "QkQk"},
	}
	for _, c := range cases {
		out, hits := MaskSecrets(c.in)
		if len(hits) == 0 || hits[0] != c.wantHit && !has(hits, c.wantHit) {
			t.Errorf("%s: 패턴 미검출 (hits=%v)", c.name, hits)
		}
		if strings.Contains(out, c.mustAbsent) {
			t.Errorf("%s: 비밀이 남았다: %q", c.name, out)
		}
		if !strings.Contains(out, "[MASKED]") {
			t.Errorf("%s: [MASKED] 표식 없음: %q", c.name, out)
		}
	}
}

// 오탐 가드 — 식별자를 가리면 인용·감사가 죽는다.
func TestMaskSecretsFalsePositives(t *testing.T) {
	for _, s := range []string{
		"sql_hash=" + strings.Repeat("ab12", 16),                  // 64자 헥사 (할당식 키 아님)
		"trace " + strings.Repeat("0f", 32),                       // 헥사 트레이스 id
		"HikariPool-1 - Connection is not available, request timed out", // 로그 원문
		"lucida:disks_utilization_max:max1h",                      // 지표명
	} {
		out, hits := MaskSecrets(s)
		if len(hits) > 0 {
			t.Errorf("오탐: %q → %v (%q)", s, hits, out)
		}
	}
}

// index 삽입점 — canary가 [MASKED]로 치환되고 Masked 목록이 남는다.
func TestIndexAppendMasksSecrets(t *testing.T) {
	ix := NewIndex()
	rec := okRecord()
	rec.EID = "" // Append가 부여
	rec.RawExcerpt = "conn refused password=TopSecret99 at pool"
	eid, err := ix.Append(rec)
	if err != nil {
		t.Fatalf("append: %v", err)
	}
	got, _ := ix.Get(eid)
	if strings.Contains(got.RawExcerpt, "TopSecret99") {
		t.Fatalf("index에 비밀이 남았다: %q", got.RawExcerpt)
	}
	if !has(got.Masked, "raw_excerpt") {
		t.Fatalf("Masked 기록 없음: %v", got.Masked)
	}
}

func has(xs []string, want string) bool {
	for _, x := range xs {
		if x == want {
			return true
		}
	}
	return false
}

// D-2(6c 검증): 인용된 값·JSON 형태 — 지배적 형식이 새면 안 된다.
func TestMaskSecretsQuotedValues(t *testing.T) {
	for _, in := range []string{
		`password="Hunter2"`,
		`"password":"Hunter2"`,
		`token: "sk-abc123def456"`,
		`{"api_key":"AKIAIOSFODNN7"}`,
	} {
		out, hits := MaskSecrets(in)
		if len(hits) == 0 || strings.Contains(out, "Hunter2") ||
			strings.Contains(out, "sk-abc") || strings.Contains(out, "AKIAI") {
			t.Errorf("인용값 누출(D-2): %q → %q (%v)", in, out, hits)
		}
	}
}
