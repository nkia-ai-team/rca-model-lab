// 고아 run 회수 — "재기동해 보니 terminal이 아니었다"의 crash 판정
// (§15.5 재기동 주체, §14-6 6a. 재료는 runstate.go가 §14-1에서 만들었고
// 여기가 그 소비자다).
//
// 판정 규율(fail-closed 방향 — 살아 있는 run을 고아로 회수하는 것이
// 최악이므로 확신 없으면 건너뛴다):
//
//   - 상태 파일이 없는 디렉토리는 run이 아니다 — 건너뛴다.
//   - terminal이면 끝난 run이다 — 건너뛴다.
//   - Hostname이 다르면 남의 호스트 run이다 — PID 생사를 판정할 수
//     없으므로 건너뛴다(공유 스토리지에서 남의 run 회수 금지, runstate.go).
//   - 같은 호스트인데 PID가 살아 있으면 진행 중이다 — 건너뛴다.
//   - 같은 호스트 + PID 사망 + 비terminal = **crash**다.
package ledger

import (
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"syscall"
)

// CrashedRun은 crash로 판정된 고아 run 하나다.
type CrashedRun struct {
	Dir   string
	State RunState
}

// ScanCrashedRuns는 출력 루트 바로 아래의 run 디렉토리들을 훑어 crash
// 판정 대상을 돌려준다(결정론 — 이름 순). 읽기 실패한 디렉토리는
// 건너뛴다(스캔은 조기 회수이지 게이트가 아니다 — 새 run을 막지 않는다).
func ScanCrashedRuns(root string) []CrashedRun {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	host, _ := os.Hostname()
	var out []CrashedRun
	for _, e := range entries {
		if !e.IsDir() || e.Name() == FailuresDirName {
			continue
		}
		dir := filepath.Join(root, e.Name())
		st, err := ReadRunState(dir)
		if err != nil || st.IsTerminal() {
			continue
		}
		if st.Hostname == "" || st.Hostname != host {
			continue
		}
		if pidAlive(st.PID) {
			continue
		}
		out = append(out, CrashedRun{Dir: dir, State: st})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Dir < out[j].Dir })
	return out
}

// DeclareCrashed는 고아 run 하나에 crash 선언을 낸다: journal tail을
// 복원해 선언을 조립(decode.go CrashDeclaration)하고, 선언 파일을 쓰고,
// 상태 파일을 failed로 전이한다. 반환은 조립된 선언이다.
//
// 판단 지점(6a 검증 소견 — §15.2-7 동시 실행 정책과 함께 재정): 동시
// 기동 2프로세스가 같은 고아를 중복 회수할 수 있다 — 선언 쓰기는 원자
// rename이라 무해하고(마지막 쓰기 승리, 내용 동일), 상태 전이도 같은
// failed라 충돌이 없다. 프로세스 간 락은 admission 정책이 파일 락을
// 얻는 시점(§14-7 이후)에 함께 들인다.
func DeclareCrashed(c CrashedRun, outRoot string, stderr io.Writer) Declaration {
	rec, err := RecoverRun(c.Dir)
	if err != nil {
		// journal이 손상돼도 선언은 낸다 — 상태 파일만으로 조립한다.
		rec = Recovery{State: c.State}
	}
	d := rec.CrashDeclaration()
	Declare(c.Dir, outRoot, d, stderr)
	// 상태 파일 전이 — 기존 판본을 이어받아 failed를 publish한다.
	// (CreateRunState는 running으로 새로 쓰므로 쓰지 않는다.)
	f := &StateFile{dir: c.Dir, path: StatePath(c.Dir), st: c.State}
	f.Fail(d)
	return d
}

// ScanRunningRuns는 출력 루트에서 **진행 중인** run들을 돌려준다 —
// 같은 호스트 + PID 생존 + 비terminal(§15.2-7 동시 실행 admission의
// 재료). 타 호스트 run은 생사 판정이 불가라 제외한다(그 run과의 동시
// 제한은 공유 스토리지 배포의 운영자 몫 — 판단 지점).
func ScanRunningRuns(root string) []RunState {
	entries, err := os.ReadDir(root)
	if err != nil {
		return nil
	}
	host, _ := os.Hostname()
	var out []RunState
	for _, e := range entries {
		if !e.IsDir() || e.Name() == FailuresDirName {
			continue
		}
		st, err := ReadRunState(filepath.Join(root, e.Name()))
		if err != nil || st.IsTerminal() || st.Hostname != host {
			continue
		}
		if st.PID == os.Getpid() || !pidAlive(st.PID) {
			continue
		}
		out = append(out, st)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].RunID < out[j].RunID })
	return out
}

// pidAlive는 같은 호스트의 PID 생사다. signal 0은 죽이지 않고 존재만
// 검사한다. EPERM은 "존재하지만 권한 없음"이므로 살아 있다로 본다.
func pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	err := syscall.Kill(pid, 0)
	if err == nil {
		return true
	}
	return errors.Is(err, syscall.EPERM)
}
