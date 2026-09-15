package ledger

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

// failWriter는 쓰기 실패 주입기다.
type failWriter struct {
	err     error
	syncErr error
	n       int
}

func (w *failWriter) Write(b []byte) (int, error) {
	w.n++
	if w.err != nil {
		return 0, w.err
	}
	return len(b), nil
}
func (w *failWriter) Sync() error  { return w.syncErr }
func (w *failWriter) Close() error { return nil }

// journal에 쓴 이벤트가 파일에서 그대로 복원되는가.
func TestJournalRoundTrip(t *testing.T) {
	dir := t.TempDir()
	j, err := OpenJournal(dir)
	if err != nil {
		t.Fatal(err)
	}
	l := New()
	l.SetJournal(j)
	mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 1)})
	mustAppend(t, l, ActorInvestigator, ts(1), probeExec("H1-P1", ProbeDone))
	if err := j.Close(); err != nil {
		t.Fatal(err)
	}

	evs, trunc, err := ReadJournal(JournalPath(dir))
	if err != nil || trunc {
		t.Fatalf("읽기: err=%v trunc=%v", err, trunc)
	}
	if len(evs) != 2 {
		t.Fatalf("이벤트 %d개 — 2개여야", len(evs))
	}
	rl, err := Replay(evs)
	if err != nil {
		t.Fatalf("재생: %v", err)
	}
	// 재생 정합의 잣대는 가설 상태 그 자체다 — probe 진행 상태(구 Probes)는
	// §14-4 4c에서 폐기됐고, 실행의 산출은 이제 index 레코드다.
	if h, ok := rl.Hypothesis("H1"); !ok || h.Lifecycle != LifeActive || len(h.Entry.PredictedSignals) != 1 {
		t.Fatalf("재생 상태가 어긋남: %+v", h)
	}
	if fi, err := os.Stat(JournalPath(dir)); err != nil || fi.Mode().Perm() != 0o600 {
		t.Errorf("journal 권한 %v (0600이어야 — §15.3-5)", fi.Mode().Perm())
	}
}

// 순서 계약: journal 기록이 실패하면 **상태가 바뀌지 않는다**.
// 이것이 "journal에 없는 이벤트가 상태에 반영된 적 없다"의 근거다.
func TestJournalWriteFailureLeavesStateUnchanged(t *testing.T) {
	fw := &failWriter{err: syscall.ENOSPC}
	l := New()
	l.SetJournal(newJournal(fw, "/injected", true))

	_, err := l.Append(ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 1)})
	if err == nil {
		t.Fatal("쓰기 실패인데 Append가 성공했다")
	}
	if !IsStoreFailure(err) {
		t.Errorf("§15.5 8행(store_failure)으로 사상되지 않음: %v", err)
	}
	if d := StoreFailureDetail(err); d != "disk_full" {
		t.Errorf("detail %q — ENOSPC는 disk_full", d)
	}
	if len(l.Events()) != 0 {
		t.Errorf("실패한 이벤트가 수첩에 남았다 (%d)", len(l.Events()))
	}
	if _, ok := l.Hypothesis("H1"); ok {
		t.Error("실패한 이벤트의 상태가 적용됐다")
	}
	// 같은 seq로 재시도가 가능해야 한다(상태가 안 변했으므로).
	l.SetJournal(nil)
	ev := mustAppend(t, l, ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 1)})
	if ev.Seq != 1 {
		t.Errorf("재시도 seq %d — 1이어야(실패가 번호를 소비하면 안 됨)", ev.Seq)
	}
}

// fsync 실패도 같은 계약이다 — 쓰기는 됐지만 내구성이 없으므로 실패다.
func TestJournalFsyncFailureIsStoreFailure(t *testing.T) {
	fw := &failWriter{syncErr: syscall.EIO}
	l := New()
	l.SetJournal(newJournal(fw, "/injected", true))
	_, err := l.Append(ActorRule, ts(0), HypothesisCreated{Entry: hypo("H1", PriorHigh, 1)})
	if err == nil || !IsStoreFailure(err) {
		t.Fatalf("fsync 실패가 store_failure가 아니다: %v", err)
	}
	if d := StoreFailureDetail(err); d != "io_error" {
		t.Errorf("detail %q — EIO는 io_error", d)
	}
	if len(l.Events()) != 0 {
		t.Error("fsync 실패 이벤트가 상태에 반영됐다")
	}
}

// 실물 ENOSPC — /dev/full은 커널이 진짜 ENOSPC를 돌려주는 장치다.
// 디스크 풀 시뮬레이션(tmpfs 마운트는 특권 필요)의 대체 실측.
func TestJournalRealENOSPCOnDevFull(t *testing.T) {
	f, err := os.OpenFile("/dev/full", os.O_WRONLY, 0)
	if err != nil {
		t.Skipf("/dev/full 없음: %v", err)
	}
	defer f.Close()
	j := newJournal(f, "/dev/full", true)
	err = j.Append(Event{Seq: 1, Time: ts(0), Actor: ActorPipeline,
		Type: EvReportAssembled, Payload: ReportAssembled{AsOfSeq: 1}})
	if err == nil {
		t.Fatal("/dev/full 쓰기가 성공했다")
	}
	if !errors.Is(err, syscall.ENOSPC) {
		t.Fatalf("실물 오류가 ENOSPC가 아님: %v", err)
	}
	if !IsStoreFailure(err) || StoreFailureDetail(err) != "disk_full" {
		t.Errorf("사상 실패: store=%v detail=%q", IsStoreFailure(err), StoreFailureDetail(err))
	}
	t.Logf("실물 ENOSPC: %v", err)
}

// 이어 쓰기 — 재개된 journal이 기존 줄을 지우지 않는다(append-only).
func TestJournalReopenAppends(t *testing.T) {
	dir := t.TempDir()
	j1, err := OpenJournal(dir)
	if err != nil {
		t.Fatal(err)
	}
	ev := Event{Seq: 1, Time: ts(0), Actor: ActorPipeline, Type: EvReportAssembled, Payload: ReportAssembled{AsOfSeq: 1}}
	if err := j1.Append(ev); err != nil {
		t.Fatal(err)
	}
	j1.Close()

	j2, err := OpenJournal(dir)
	if err != nil {
		t.Fatal(err)
	}
	ev.Seq = 2
	if err := j2.Append(ev); err != nil {
		t.Fatal(err)
	}
	j2.Close()

	evs, _, err := ReadJournal(filepath.Join(dir, JournalName))
	if err != nil || len(evs) != 2 {
		t.Fatalf("이어 쓰기 실패: %d개 err=%v", len(evs), err)
	}
}
