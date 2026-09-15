package ledger

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestRunStateCreateAndStages(t *testing.T) {
	dir := t.TempDir()
	f, err := CreateRunState(dir, "RUN-1", "")
	if err != nil {
		t.Fatal(err)
	}
	st, err := ReadRunState(dir)
	if err != nil {
		t.Fatal(err)
	}
	if st.Status != StatusRunning || st.PID != os.Getpid() || st.Hostname == "" || st.StartedAt.IsZero() {
		t.Fatalf("stale 판별 재료가 빈다: %+v", st)
	}
	if err := f.SetStage("examine", 12); err != nil {
		t.Fatal(err)
	}
	st, _ = ReadRunState(dir)
	if st.Stage != "examine" || st.LastSeq != 12 {
		t.Fatalf("단계 전이 미기록: %+v", st)
	}
	if st.IsTerminal() {
		t.Error("running이 terminal로 보인다")
	}
}

// terminal은 정확히 한 번이다(B-3).
func TestRunStateTerminalOnce(t *testing.T) {
	dir := t.TempDir()
	f, _ := CreateRunState(dir, "RUN-1", "")
	if err := f.Complete(30); err != nil {
		t.Fatal(err)
	}
	st, _ := ReadRunState(dir)
	if st.Status != StatusCompleted || !st.IsTerminal() {
		t.Fatalf("완주 전이 실패: %+v", st)
	}
	if err := f.Complete(31); err != ErrTerminal {
		t.Errorf("두 번째 완주 전이가 %v — ErrTerminal이어야", err)
	}
	if err := f.Fail(Declaration{RunID: "RUN-1", Stage: "report", Reason: ReasonCrash}); err != ErrTerminal {
		t.Errorf("terminal 후 실패 전이가 %v — ErrTerminal이어야", err)
	}
	if err := f.SetStage("late", 40); err != ErrTerminal {
		t.Errorf("terminal 후 단계 전이가 %v — ErrTerminal이어야", err)
	}
	st2, _ := ReadRunState(dir)
	if st2.Status != StatusCompleted || st2.LastSeq != 30 {
		t.Errorf("terminal 이후 파일이 변했다: %+v", st2)
	}
}

func TestRunStateFailCarriesDeclaration(t *testing.T) {
	dir := t.TempDir()
	f, _ := CreateRunState(dir, "RUN-1", "")
	f.SetStage("generate", 5)
	d := Declaration{RunID: "RUN-1", Stage: "generate", Reason: ReasonLLMError,
		Source: "llm", Backend: "llm", Detail: "http_5xx", Retryable: true}
	if err := f.Fail(d); err != nil {
		t.Fatal(err)
	}
	st, _ := ReadRunState(dir)
	if st.Status != StatusFailed || st.Failure == nil || st.Failure.Reason != ReasonLLMError {
		t.Fatalf("실패 선언 사본 없음: %+v", st)
	}
}

// 원자 교체 — 갱신 중 어느 시점에 읽어도 완성된 JSON만 보인다.
// tmp 파일이 남지 않고, 상태 파일 자체가 잘린 판본을 갖지 않는다.
func TestRunStateAtomicReplace(t *testing.T) {
	dir := t.TempDir()
	f, _ := CreateRunState(dir, "RUN-1", "")
	for i := 0; i < 50; i++ {
		if err := f.SetStage(strings.Repeat("stage", i%7+1), i); err != nil {
			t.Fatal(err)
		}
		b, err := os.ReadFile(StatePath(dir))
		if err != nil {
			t.Fatalf("읽기(%d): %v", i, err)
		}
		var st RunState
		if err := json.Unmarshal(b, &st); err != nil {
			t.Fatalf("부분 JSON이 보였다(%d): %v", i, err)
		}
	}
	names, _ := filepath.Glob(filepath.Join(dir, StateName+".tmp-*"))
	if len(names) != 0 {
		t.Errorf("staging 파일이 남았다: %v", names)
	}
}
