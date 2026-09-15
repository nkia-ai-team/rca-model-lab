package ledger

import (
	"bufio"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// 실동작 크래시 시험 — **진짜 SIGKILL**로 죽인 프로세스의 journal에서
// 이벤트 손실 없이 복원되는가(§14-1 완료 기준 4번).
//
// 자식은 이 시험 바이너리를 환경변수와 함께 재실행한 것이다: TestMain이
// 그 변수를 보면 시험 대신 자식 몫(journal에 계속 쓰기)을 돌린다.
// 자식은 **Append가 반환한 뒤에만** seq를 stdout에 흘리므로, 부모가 읽은
// 마지막 seq까지는 반드시 디스크에 있어야 한다 — 그것이 순서 계약이다.

const crashChildEnv = "RCA_LEDGER_CRASH_CHILD_DIR"

func TestMain(m *testing.M) {
	if dir := os.Getenv(crashChildEnv); dir != "" {
		crashChild(dir)
		return
	}
	os.Exit(m.Run())
}

func crashChild(dir string) {
	j, err := OpenJournal(dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	sf, err := CreateRunState(dir, "RUN-CRASH", "")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(2)
	}
	l := New()
	l.SetJournal(j)
	for i := 1; i <= 5000; i++ {
		e := hypo(fmt.Sprintf("H%d", i), PriorMedium, 1)
		ev, err := l.Append(ActorRule, time.Now().UTC(), HypothesisCreated{Entry: e})
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
		if i%10 == 0 {
			// 단계 전이 갱신도 섞는다 — 상태 파일 원자 교체 중의
			// SIGKILL도 시험 범위에 든다.
			_ = sf.SetStage("examine", ev.Seq)
		}
		fmt.Println(ev.Seq) // Append 반환 후에만 알린다
		time.Sleep(time.Millisecond)
	}
	os.Exit(0)
}

func TestCrashKill9RecoversWithoutLoss(t *testing.T) {
	if testing.Short() {
		t.Skip("short 모드")
	}
	dir := t.TempDir()
	cmd := exec.Command(os.Args[0])
	cmd.Env = append(os.Environ(), crashChildEnv+"="+dir)
	out, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}

	const killAfter = 25
	last := 0
	sc := bufio.NewScanner(out)
	for sc.Scan() && last < killAfter {
		n, cerr := strconv.Atoi(strings.TrimSpace(sc.Text()))
		if cerr != nil {
			t.Fatalf("자식 출력 %q: %v", sc.Text(), cerr)
		}
		if n != last+1 {
			t.Fatalf("자식 seq 건너뜀: %d 다음 %d", last, n)
		}
		last = n
	}
	if last < killAfter {
		t.Fatalf("자식이 %d개만 알리고 끝났다", last)
	}
	if err := cmd.Process.Kill(); err != nil { // SIGKILL
		t.Fatal(err)
	}
	state, _ := cmd.Process.Wait()
	t.Logf("자식 종료: %v (알림받은 마지막 seq=%d)", state, last)

	// ── 재기동 복원 ──
	rec, err := RecoverRun(dir)
	if err != nil {
		t.Fatalf("복원: %v", err)
	}
	if rec.LastSeq() < last {
		t.Fatalf("이벤트 손실: journal 마지막 seq %d < 반환된 %d", rec.LastSeq(), last)
	}
	for i, ev := range rec.Events {
		if ev.Seq != i+1 {
			t.Fatalf("journal 구멍: %d번째 줄의 seq=%d", i+1, ev.Seq)
		}
		if _, ok := ev.Payload.(HypothesisCreated); !ok {
			t.Fatalf("payload 복원 실패: %T", ev.Payload)
		}
	}
	rl, err := rec.Ledger()
	if err != nil {
		t.Fatalf("재생: %v", err)
	}
	if _, ok := rl.Hypothesis(fmt.Sprintf("H%d", last)); !ok {
		t.Fatalf("마지막으로 반환된 가설 H%d가 복원 상태에 없다", last)
	}

	// 상태 파일: terminal이 아니다 → 재기동 주체가 crash 선언을 만든다.
	if rec.State.IsTerminal() {
		t.Fatalf("SIGKILL로 죽은 run이 terminal로 보인다: %+v", rec.State)
	}
	if rec.State.PID == 0 || rec.State.Hostname == "" {
		t.Errorf("stale 판별 재료가 빈다: %+v", rec.State)
	}
	d := rec.CrashDeclaration()
	if err := d.Validate(); err != nil {
		t.Fatalf("조립한 crash 선언이 계약 위반: %v", err)
	}
	if d.Reason != ReasonCrash || d.Stage == "" {
		t.Fatalf("crash 선언 내용: %+v", d)
	}
	if code := Declare(dir, "", d, os.Stderr); code != ExitFailure {
		t.Fatalf("선언 쓰기 exit %d", code)
	}
	t.Logf("복원 %d 이벤트(꼬리 잘림=%v), 상태=%s 단계=%s, 선언 reason=%s",
		len(rec.Events), rec.TruncatedTail, rec.State.Status, rec.State.Stage, d.Reason)

	if _, err := os.Stat(filepath.Join(dir, FailureName)); err != nil {
		t.Fatalf("실패 선언 파일 없음: %v", err)
	}
}

// 부분 쓰기 꼬리가 붙어도 복원이 계속되는가 — SIGKILL이 write 중간에서
// 일어난 경우의 재현(실제로 그 순간을 맞히기는 어려우므로 파일에 직접
// 반쪽 줄을 붙여 같은 상태를 만든다).
func TestRecoverTolerantOfPartialTail(t *testing.T) {
	dir := t.TempDir()
	sf, err := CreateRunState(dir, "RUN-P", "")
	if err != nil {
		t.Fatal(err)
	}
	sf.SetStage("triage", 0)
	j, err := OpenJournal(dir)
	if err != nil {
		t.Fatal(err)
	}
	l := New()
	l.SetJournal(j)
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 1)})
	j.Close()

	f, err := os.OpenFile(JournalPath(dir), os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		t.Fatal(err)
	}
	line, _ := EncodeEvent(Event{Seq: 2, Time: ts(1), Actor: ActorRule,
		Type: EvHypothesisCreated, Payload: HypothesisCreated{Entry: hypo("H2", PriorLow, 1)}})
	f.Write(line[:len(line)/2]) // 개행 없는 반쪽 줄
	f.Close()

	rec, err := RecoverRun(dir)
	if err != nil {
		t.Fatalf("복원: %v", err)
	}
	if !rec.TruncatedTail || len(rec.Events) != 1 {
		t.Fatalf("꼬리 처리: trunc=%v events=%d", rec.TruncatedTail, len(rec.Events))
	}
	if _, err := rec.Ledger(); err != nil {
		t.Fatalf("재생: %v", err)
	}
}
