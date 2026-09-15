package ledger

import (
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// deadPID는 확실히 죽은 같은 호스트 PID 하나를 만든다 — 자식을 낳고
// 끝나기를 기다린다(wait 완료 = PID 회수 전이지만 kill(0)은 ESRCH).
func deadPID(t *testing.T) int {
	t.Helper()
	cmd := exec.Command("true")
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	pid := cmd.Process.Pid
	cmd.Wait()
	return pid
}

func orphanState(t *testing.T, dir string, st RunState) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		t.Fatal(err)
	}
	f := &StateFile{dir: dir, path: StatePath(dir), st: st}
	if err := f.publish(); err != nil {
		t.Fatal(err)
	}
}

// 같은 호스트 + PID 사망 + 비terminal만 crash다 — 나머지는 전부 건너뛴다
// (살아 있는 run을 고아로 회수하는 것이 최악, fail-closed).
func TestScanCrashedRunsJudgement(t *testing.T) {
	root := t.TempDir()
	host, _ := os.Hostname()
	now := time.Now().UTC()

	orphanState(t, filepath.Join(root, "crashed"), RunState{
		RunID: "R-crash", Stage: "loop", Status: StatusRunning,
		PID: deadPID(t), Hostname: host, StartedAt: now, UpdatedAt: now})
	orphanState(t, filepath.Join(root, "alive"), RunState{
		RunID: "R-alive", Stage: "loop", Status: StatusRunning,
		PID: os.Getpid(), Hostname: host, StartedAt: now, UpdatedAt: now})
	orphanState(t, filepath.Join(root, "other-host"), RunState{
		RunID: "R-other", Stage: "loop", Status: StatusRunning,
		PID: deadPID(t), Hostname: host + "-not-me", StartedAt: now, UpdatedAt: now})
	orphanState(t, filepath.Join(root, "done"), RunState{
		RunID: "R-done", Stage: "report", Status: StatusCompleted,
		PID: deadPID(t), Hostname: host, StartedAt: now, UpdatedAt: now})
	// 상태 파일 없는 디렉토리는 run이 아니다.
	os.MkdirAll(filepath.Join(root, "not-a-run"), 0o700)

	got := ScanCrashedRuns(root)
	if len(got) != 1 || got[0].State.RunID != "R-crash" {
		t.Fatalf("crash 판정 %v — R-crash 하나여야 함", got)
	}
}

// DeclareCrashed는 선언 파일 + 상태 failed 전이를 남기고, 재스캔에서
// 같은 run이 다시 잡히지 않는다(회수는 한 번).
func TestDeclareCrashedWritesAndTerminates(t *testing.T) {
	root := t.TempDir()
	host, _ := os.Hostname()
	dir := filepath.Join(root, "crashed")
	orphanState(t, dir, RunState{
		RunID: "R-crash", Stage: "generate", Status: StatusRunning,
		PID: deadPID(t), Hostname: host,
		StartedAt: time.Now().UTC(), UpdatedAt: time.Now().UTC()})

	runs := ScanCrashedRuns(root)
	if len(runs) != 1 {
		t.Fatal("전제 실패: crash 판정 0건")
	}
	d := DeclareCrashed(runs[0], root, nil)
	if d.Reason != ReasonCrash || d.Stage != "generate" || d.RunID != "R-crash" {
		t.Fatalf("crash 선언 내용: %+v", d)
	}
	// 재기동 스캔 선언도 고정 문구표·retryable을 싣는다(6a 검증 D2).
	if d.OperatorAction == "" || !d.Retryable {
		t.Fatalf("crash 선언 부가 필드: %+v", d)
	}
	if got, err := ReadDeclaration(dir); err != nil || got.Reason != ReasonCrash {
		t.Fatalf("선언 파일: %+v err=%v", got, err)
	}
	st, err := ReadRunState(dir)
	if err != nil || st.Status != StatusFailed || st.Failure == nil {
		t.Fatalf("상태 전이: %+v err=%v", st, err)
	}
	if rest := ScanCrashedRuns(root); len(rest) != 0 {
		t.Fatalf("회수 후 재스캔에 %d건 — terminal이면 잡히면 안 됨", len(rest))
	}
}

// ScanRunningRuns(§15.2-7) — 같은 호스트·생존 PID·비terminal만 "진행
// 중"이다. 자기 자신(현재 PID)은 제외한다.
func TestScanRunningRuns(t *testing.T) {
	root := t.TempDir()
	host, _ := os.Hostname()
	now := time.Now().UTC()
	alive := exec.Command("sleep", "5")
	if err := alive.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { alive.Process.Kill(); alive.Wait() }()

	orphanState(t, filepath.Join(root, "running"), RunState{
		RunID: "R-run", Incident: "inc-1", Stage: "loop", Status: StatusRunning,
		PID: alive.Process.Pid, Hostname: host, StartedAt: now, UpdatedAt: now})
	orphanState(t, filepath.Join(root, "self"), RunState{
		RunID: "R-self", Stage: "loop", Status: StatusRunning,
		PID: os.Getpid(), Hostname: host, StartedAt: now, UpdatedAt: now})
	orphanState(t, filepath.Join(root, "dead"), RunState{
		RunID: "R-dead", Stage: "loop", Status: StatusRunning,
		PID: deadPID(t), Hostname: host, StartedAt: now, UpdatedAt: now})

	got := ScanRunningRuns(root)
	if len(got) != 1 || got[0].RunID != "R-run" || got[0].Incident != "inc-1" {
		t.Fatalf("진행 중 판정 %v — R-run(incident 포함) 하나여야 함", got)
	}
}
