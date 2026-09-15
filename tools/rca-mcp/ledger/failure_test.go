package ledger

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDeclarationSchemaGuards(t *testing.T) {
	base := Declaration{RunID: "R1", Stage: "examine", Reason: ReasonStoreFailure}
	if err := base.Validate(); err != nil {
		t.Fatalf("정상 선언 반려: %v", err)
	}
	bad := base
	bad.Reason = "무슨reason"
	if err := bad.Validate(); err == nil {
		t.Error("미정의 reason이 통과")
	}
	// 원문 오류 문자열 금지(§15.3-2) — 토큰이 아니면 반려한다.
	bad = base
	bad.Detail = `dial tcp 10.0.0.1:5432: connect: connection refused`
	if err := bad.Validate(); err == nil {
		t.Error("원문 오류 문자열이 detail로 통과")
	}
	bad = base
	bad.Stage = ""
	if err := bad.Validate(); err == nil {
		t.Error("stage 없는 선언이 통과")
	}
	if len(AllFailureReasons) != 11 {
		t.Errorf("reason %d종 — §15.5는 11종", len(AllFailureReasons))
	}
}

// 선언이 쓰였으면 exit 1, 못 썼으면 stderr 한 줄 + exit 3 (B-2 ②).
func TestDeclareWritesAndFallsBack(t *testing.T) {
	dir := t.TempDir()
	var errbuf bytes.Buffer
	d := Declaration{RunID: "R1", Stage: "examine", Reason: ReasonBackendDown,
		Source: "tool:scan_metrics", Backend: "vm", Detail: "circuit_open"}
	if code := Declare(dir, "", d, &errbuf); code != ExitFailure {
		t.Fatalf("exit %d — 선언을 썼으면 %d", code, ExitFailure)
	}
	got, err := ReadDeclaration(dir)
	if err != nil || got.Reason != ReasonBackendDown || got.Backend != "vm" {
		t.Fatalf("선언 파일: %+v err=%v", got, err)
	}
	if !strings.HasPrefix(errbuf.String(), FailurePrefix) {
		t.Errorf("stderr 줄 접두 없음: %q", errbuf.String())
	}

	// 쓸 수 없는 경로 — 최후 계약.
	ro := filepath.Join(t.TempDir(), "ro")
	if err := os.Mkdir(ro, 0o500); err != nil {
		t.Fatal(err)
	}
	errbuf.Reset()
	code := Declare(ro, "", d, &errbuf)
	if os.Geteuid() == 0 {
		t.Skip("root라 쓰기 불가 경로를 만들 수 없음")
	}
	if code != ExitDeclarationUnwritable {
		t.Fatalf("exit %d — 선언 불가면 %d", code, ExitDeclarationUnwritable)
	}
	line := strings.TrimPrefix(strings.TrimSpace(errbuf.String()), FailurePrefix)
	if !strings.Contains(line, `"reason":"backend_down"`) {
		t.Errorf("최후 계약 줄에 사유가 없다: %q", line)
	}
}

// 분리 경로(RCA_FAILURE_DIR)가 살아 있으면 run 디렉토리가 죽어도 선언은
// 남는다 — B-2 ①의 핵심.
func TestDeclareSeparatePathSurvivesDeadRunDir(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("root")
	}
	alt := t.TempDir()
	t.Setenv(FailureDirEnv, alt)
	dead := filepath.Join(t.TempDir(), "ro")
	if err := os.Mkdir(dead, 0o500); err != nil {
		t.Fatal(err)
	}
	d := Declaration{RunID: "RUN/1", Stage: "examine", Reason: ReasonStoreFailure, Detail: "disk_full"}
	if code := Declare(dead, "", d, nil); code != ExitFailure {
		t.Fatalf("exit %d — 분리 경로에 썼으면 %d", code, ExitFailure)
	}
	names, _ := filepath.Glob(filepath.Join(alt, "*"+FailureName))
	if len(names) != 1 {
		t.Fatalf("분리 경로 선언 %v", names)
	}
	if strings.ContainsAny(filepath.Base(names[0]), "/") {
		t.Errorf("run ID가 경로로 새어 나갔다: %s", names[0])
	}
}

func TestCheckFreeSpace(t *testing.T) {
	dir := t.TempDir()
	if err := CheckFreeSpace(dir, 1); err != nil {
		t.Fatalf("1바이트 요구가 실패: %v", err)
	}
	huge := uint64(1) << 62
	if err := CheckFreeSpace(dir, huge); err == nil {
		t.Skip("이 플랫폼은 여유 공간 조회가 없어 통과시킨다")
	}
	free, err := freeBytes(dir)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("%s 여유 %d MiB (기본 예약 %d MiB)", dir, free>>20, DefaultReserveBytes>>20)
}

// RCA_FAILURE_DIR 미설정이면 출력 루트의 failures/가 기본 사본 경로다
// (§14-6 6a 사용자 결정) — run 디렉토리 생성 자체가 실패해도(runDir "")
// 선언이 남는다.
func TestDeclareDefaultFailuresDir(t *testing.T) {
	t.Setenv(FailureDirEnv, "")
	root := t.TempDir()
	d := Declaration{RunID: "R-DEF", Stage: "setup", Reason: ReasonInputInvalid}
	if code := Declare("", root, d, nil); code != ExitFailure {
		t.Fatalf("exit %d — 기본 사본 경로에 썼으면 %d", code, ExitFailure)
	}
	name := filepath.Join(root, FailuresDirName, "R-DEF-"+FailureName)
	if _, err := os.Stat(name); err != nil {
		t.Fatalf("기본 사본 경로 선언 없음: %v", err)
	}
	// run 디렉토리가 살아 있으면 양쪽에 남는다(경로 분리).
	dir := filepath.Join(root, "run1")
	if err := os.Mkdir(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	d2 := Declaration{RunID: "R-DEF2", Stage: "loop", Reason: ReasonLLMError}
	if code := Declare(dir, root, d2, nil); code != ExitFailure {
		t.Fatal("run 디렉토리 경로 선언 실패")
	}
	if _, err := os.Stat(filepath.Join(dir, FailureName)); err != nil {
		t.Fatalf("run 디렉토리 선언 없음: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, FailuresDirName, "R-DEF2-"+FailureName)); err != nil {
		t.Fatalf("사본 경로 선언 없음: %v", err)
	}
}
