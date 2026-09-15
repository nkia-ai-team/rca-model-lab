// run 상태 파일 — "지금 어느 단계인가"와 "끝났는가"의 내구 기록
// (§15.5-② 크래시 계약, B-3 부분).
//
// 계약:
//
//	· 원자 교체 — staging(tmp)에 완성 + fsync → rename → 디렉토리 fsync.
//	  독자는 완성된 판본만 본다(부분 JSON이 고객 산출물로 보이는 경로 차단).
//	· terminal(completed|failed)은 **정확히 한 번** 전이한다. 이미
//	  terminal인 파일에 다시 쓰면 ErrTerminal이다.
//	· publication 순서 — terminal 전이는 **산출물을 다 쓰고 fsync한 뒤**
//	  마지막에 한다. 그래야 "result.json은 있는데 상태는 running"(재기동이
//	  crash로 판정 → 재주행)은 있어도, 반대(상태는 completed인데 산출물이
//	  부분)는 없다. 실패 쪽으로 기우는 이 비대칭이 fail-closed다.
//	· stale 판별 재료 — PID·Hostname·StartedAt·UpdatedAt을 싣는다.
//	  **회수 동작(고아 스캔·crash 선언·재실행 허용)은 §14-6 몫**이다.
//	  재료를 여기서 못 박는 이유는 나중에 필드를 못 붙이기 때문이고,
//	  Hostname이 있는 이유는 공유 스토리지에서 남의 호스트 PID를 자기
//	  프로세스로 오인하면 살아 있는 run을 고아로 회수하기 때문이다.
package ledger

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// StateName은 run 디렉토리 안의 상태 파일 이름이다.
const StateName = "run-state.json"

// StatePath는 run 디렉토리의 상태 파일 경로다.
func StatePath(runDir string) string { return filepath.Join(runDir, StateName) }

// RunStatus는 run의 진행 상태다. completed·failed가 terminal이다.
type RunStatus string

const (
	StatusRunning   RunStatus = "running"
	StatusCompleted RunStatus = "completed"
	StatusFailed    RunStatus = "failed"
)

// ErrTerminal은 terminal 전이가 두 번 일어났을 때다.
var ErrTerminal = errors.New("run 상태가 이미 terminal — 전이는 정확히 한 번")

// RunState는 상태 파일의 내용이다.
type RunState struct {
	RunID string `json:"run_id"`
	// Incident — 같은 인시던트 동시 실행 거부(§15.2-7, 6b)의 판정 재료.
	Incident string    `json:"incident,omitempty"`
	Stage    string    `json:"stage"`
	Status   RunStatus `json:"status"`

	// stale 판별 재료(§14-6이 소비).
	PID       int       `json:"pid"`
	Hostname  string    `json:"hostname"`
	StartedAt time.Time `json:"started_at"`
	UpdatedAt time.Time `json:"updated_at"`

	// LastSeq — 상태 갱신 시점의 수첩 마지막 seq. journal 기록이 상태
	// 갱신보다 앞서므로 journal의 마지막 seq ≥ 이 값이다.
	LastSeq int `json:"last_seq"`

	// Failure — Status=failed일 때의 선언(§15.5). 선언 정본은 별도
	// 파일(failure.go)이고 여기 실리는 것은 같은 값의 사본이다 —
	// 재기동 주체가 상태 파일 하나만 읽고도 사유를 알 수 있게.
	Failure *Declaration `json:"failure,omitempty"`
}

// IsTerminal은 이 run이 끝났는지다.
func (s RunState) IsTerminal() bool {
	return s.Status == StatusCompleted || s.Status == StatusFailed
}

// StateFileError는 상태 파일 쓰기 실패다 — journal과 같은 사상(§15.5
// 8행 store_failure).
type StateFileError struct {
	Op   string // create · write · rename · syncdir
	Path string
	Err  error
}

func (e *StateFileError) Error() string {
	return fmt.Sprintf("run 상태 파일 %s(%s): %v", e.Op, e.Path, e.Err)
}
func (e *StateFileError) Unwrap() error { return e.Err }

// StateFile은 원자 교체로 갱신되는 상태 파일 핸들이다.
type StateFile struct {
	mu   sync.Mutex
	dir  string
	path string
	st   RunState
}

// CreateRunState는 run 시작 시 상태 파일을 만든다(status=running).
// incident는 같은 인시던트 동시 실행 거부(§15.2-7)의 재료다 — 모르면 "".
func CreateRunState(runDir, runID, incident string) (*StateFile, error) {
	host, _ := os.Hostname()
	now := time.Now().UTC()
	f := &StateFile{
		dir:  runDir,
		path: StatePath(runDir),
		st: RunState{
			RunID: runID, Incident: incident, Status: StatusRunning,
			PID: os.Getpid(), Hostname: host,
			StartedAt: now, UpdatedAt: now,
		},
	}
	if err := f.publish(); err != nil {
		return nil, err
	}
	return f, nil
}

// SetStage는 단계 전이를 기록한다. lastSeq는 그 시점의 수첩 마지막 seq다
// (모르면 0을 넘기면 이전 값을 유지한다).
func (f *StateFile) SetStage(stage string, lastSeq int) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.st.IsTerminal() {
		return ErrTerminal
	}
	f.st.Stage = stage
	if lastSeq > f.st.LastSeq {
		f.st.LastSeq = lastSeq
	}
	return f.publish()
}

// Complete는 완주 terminal 전이다 — **산출물을 다 쓴 뒤 마지막에** 부른다.
func (f *StateFile) Complete(lastSeq int) error {
	return f.terminal(StatusCompleted, lastSeq, nil)
}

// Fail은 실패 terminal 전이다. 선언은 상태 파일에도 사본으로 실린다.
func (f *StateFile) Fail(d Declaration) error {
	return f.terminal(StatusFailed, 0, &d)
}

func (f *StateFile) terminal(s RunStatus, lastSeq int, d *Declaration) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.st.IsTerminal() {
		return ErrTerminal
	}
	f.st.Status = s
	if lastSeq > f.st.LastSeq {
		f.st.LastSeq = lastSeq
	}
	f.st.Failure = d
	return f.publish()
}

// Snapshot은 현재 상태의 복사본이다.
func (f *StateFile) Snapshot() RunState {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.st
}

func (f *StateFile) Path() string { return f.path }

// publish는 원자 교체다. 호출자가 잠금을 쥐고 있어야 한다.
func (f *StateFile) publish() error {
	f.st.UpdatedAt = time.Now().UTC()
	b, err := json.MarshalIndent(f.st, "", "  ")
	if err != nil {
		return &StateFileError{Op: "write", Path: f.path, Err: err}
	}
	b = append(b, '\n')

	tmp, err := os.CreateTemp(f.dir, StateName+".tmp-*")
	if err != nil {
		return &StateFileError{Op: "create", Path: f.dir, Err: err}
	}
	tmpName := tmp.Name()
	cleanup := func(e error) error {
		tmp.Close()
		os.Remove(tmpName)
		return e
	}
	if _, err := tmp.Write(b); err != nil {
		return cleanup(&StateFileError{Op: "write", Path: tmpName, Err: err})
	}
	if err := tmp.Sync(); err != nil {
		return cleanup(&StateFileError{Op: "write", Path: tmpName, Err: err})
	}
	if err := tmp.Chmod(0o600); err != nil {
		return cleanup(&StateFileError{Op: "write", Path: tmpName, Err: err})
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return &StateFileError{Op: "write", Path: tmpName, Err: err}
	}
	if err := os.Rename(tmpName, f.path); err != nil {
		os.Remove(tmpName)
		return &StateFileError{Op: "rename", Path: f.path, Err: err}
	}
	// rename 자체를 내구화한다 — 안 하면 크래시 후 옛 판본이 보인다.
	if err := syncDir(f.dir); err != nil {
		return &StateFileError{Op: "syncdir", Path: f.dir, Err: err}
	}
	return nil
}

// ReadRunState는 상태 파일을 읽는다(재기동 주체·감사용).
func ReadRunState(runDir string) (RunState, error) {
	b, err := os.ReadFile(StatePath(runDir))
	if err != nil {
		return RunState{}, err
	}
	var s RunState
	if err := json.Unmarshal(b, &s); err != nil {
		return RunState{}, fmt.Errorf("run 상태 파일 디코드(%s): %w", StatePath(runDir), err)
	}
	return s, nil
}
