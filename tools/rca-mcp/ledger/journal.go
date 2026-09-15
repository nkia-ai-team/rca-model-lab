// 내구 journal — 수첩 이벤트를 append+fsync로 디스크에 남긴다
// (§15.5-①·③ 크래시 계약).
//
// 왜 필요한가: 현행 수첩은 메모리 슬라이스이고 ledger.jsonl은 **성공
// 경로의 dump**에서만 쓰인다 — 크래시하면 산출물이 0이고, 그러면 "어느
// 단계에서 죽었는지 판정해 실패 선언을 만든다"가 불가능하다. journal은
// 실패한 run에도 남는 정본이다.
//
// # 구 ledger.jsonl과의 관계 (실측 2026-08-03)
//
//   - **줄 형식은 같다** — cmd/rca/main.go:275의 dump가 쓰는 것과
//     journal이 쓰는 것은 둘 다 `json.Marshal(Event)` 한 줄이다(실측:
//     runs/*/ledger.jsonl 59줄이 이 디코더로 그대로 읽힌다).
//   - **계약이 다르다** — dump는 성공 경로에서 한 번에 쓰고 fsync가
//     없으며 파일 이름이 ledger.jsonl이다. journal은 Append마다 쓰고
//     fsync하며 실패 경로에도 남는다. 그래서 **공존**시킨다: 파일명을
//     ledger-journal.jsonl로 분리해 구 dump 경로를 무수정으로 둔다
//     (§14-1 규율 "구 ledger 경로는 무수정 공존, journal은 additive").
//   - 통합(dump가 journal을 승계)은 dump 호출부인 cmd/rca를 고치는
//     일이라 §14-5·§14-6 배선 몫이다 — 여기서는 파일 두 개가 같은
//     내용을 갖고 journal이 상위집합이다.
//
// # 쓰기 실패 시 run이 어떻게 죽는가 (B-2가 규정을 요구한 자리)
//
// journal 쓰기·fsync 실패는 **run 실패(store_failure)**다. 순서 계약이
// 이것을 안전하게 만든다: Ledger.Append는 ① 검증 → ② journal
// append+fsync → ③ 메모리 상태 적용 순으로 돌고, ②가 실패하면 ③을
// 하지 않고 오류를 돌려준다. 따라서 **journal에 없는 이벤트가 상태에
// 반영된 적은 없다** — 복원한 수첩은 죽기 직전 수첩과 정의상 같거나,
// 반환되지 않은 Append 하나만큼 짧다.
//
// 실패 선언 자체를 못 쓰는 경우(디스크 풀)는 failure.go의 최후 계약
// (stderr 한 줄 + exit code)이 진다.
package ledger

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"syscall"
)

// JournalName은 run 디렉토리 안의 journal 파일 이름이다.
const JournalName = "ledger-journal.jsonl"

// JournalPath는 run 디렉토리의 journal 경로다.
func JournalPath(runDir string) string { return filepath.Join(runDir, JournalName) }

// journalWriter는 journal이 필요로 하는 최소 표면이다. 시험이 쓰기
// 실패(ENOSPC 등)를 실물로 주입할 수 있도록 인터페이스로 둔다.
type journalWriter interface {
	io.Writer
	Sync() error
	Close() error
}

// Journal은 append-only 내구 기록이다. 동시 Append에 안전하다.
type Journal struct {
	mu    sync.Mutex
	w     journalWriter
	path  string
	n     int
	bytes int64
	fsync bool
}

// OpenJournal은 run 디렉토리에 journal을 연다(있으면 이어 쓴다).
// 파일 생성 자체가 내구화되도록 부모 디렉토리도 fsync한다 — 그러지
// 않으면 크래시 후 파일 엔트리가 없어 "이벤트는 썼는데 파일이 없다"가
// 가능하다.
func OpenJournal(runDir string) (*Journal, error) {
	path := JournalPath(runDir)
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return nil, &JournalError{Op: "open", Path: path, Err: err}
	}
	if err := syncDir(runDir); err != nil {
		f.Close()
		return nil, &JournalError{Op: "syncdir", Path: runDir, Err: err}
	}
	return &Journal{w: f, path: path, fsync: true}, nil
}

// newJournal은 시험용 생성자다(임의 writer 주입).
func newJournal(w journalWriter, path string, fsync bool) *Journal {
	return &Journal{w: w, path: path, fsync: fsync}
}

// Append는 이벤트 한 줄을 기록하고 fsync한다. 반환이 nil이면 그 줄은
// 디스크에 있다 — 이 보장이 크래시 복원의 전부다.
func (j *Journal) Append(ev Event) error {
	b, err := EncodeEvent(ev)
	if err != nil {
		return &JournalError{Op: "encode", Path: j.path, Err: err}
	}
	b = append(b, '\n')

	j.mu.Lock()
	defer j.mu.Unlock()
	n, werr := j.w.Write(b)
	j.bytes += int64(n)
	if werr != nil {
		return &JournalError{Op: "write", Path: j.path, Err: werr}
	}
	if n != len(b) {
		// io.Writer 계약상 n<len이면 err이 있어야 하지만, 부분 쓰기를
		// 조용히 넘기면 journal에 깨진 줄이 남는다 — 명시적으로 죽는다.
		return &JournalError{Op: "write", Path: j.path,
			Err: fmt.Errorf("부분 쓰기 %d/%d", n, len(b))}
	}
	if j.fsync {
		if err := j.w.Sync(); err != nil {
			return &JournalError{Op: "fsync", Path: j.path, Err: err}
		}
	}
	j.n++
	return nil
}

// Count·Bytes는 기록된 줄 수와 바이트다(§15.2 물리 예산의 계측 표면).
func (j *Journal) Count() int {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.n
}

func (j *Journal) Bytes() int64 {
	j.mu.Lock()
	defer j.mu.Unlock()
	return j.bytes
}

func (j *Journal) Path() string { return j.path }

// Close는 파일을 닫는다. 마지막 fsync는 Append마다 이미 났다.
func (j *Journal) Close() error {
	j.mu.Lock()
	defer j.mu.Unlock()
	if j.w == nil {
		return nil
	}
	err := j.w.Close()
	j.w = nil
	return err
}

// SetJournal은 수첩에 내구 기록을 붙인다(additive — 붙이지 않으면 구
// 동작 그대로). 붙인 뒤의 Append는 journal 기록에 성공한 이벤트만
// 상태에 반영한다.
func (l *Ledger) SetJournal(j *Journal) { l.journal = j }

// ── 오류 분류 (§15.5 사상표 8행) ─────────────────────────────────

// JournalError는 journal 쓰기 실패다. 전부 store_failure로 사상되며,
// 물리 디스크 풀·쿼터·권한·그 외 I/O를 Detail로 가른다(B-2 ③ — 8행을
// 쿼터와 물리 풀로 나누라는 지적의 구현 층 반영. 선언 스키마의 detail
// 토큰이 그 구분을 나른다).
type JournalError struct {
	Op   string // open · syncdir · encode · write · fsync
	Path string
	Err  error
}

func (e *JournalError) Error() string {
	return fmt.Sprintf("journal %s(%s): %v", e.Op, e.Path, e.Err)
}
func (e *JournalError) Unwrap() error { return e.Err }

// StoreFailureDetail은 오류를 선언 스키마의 detail 토큰으로 사상한다.
// 원문 오류 문자열은 선언에 싣지 않는다(§15.3-2).
func StoreFailureDetail(err error) string {
	switch {
	case err == nil:
		return ""
	case errors.Is(err, syscall.ENOSPC):
		return "disk_full"
	case errors.Is(err, syscall.EDQUOT):
		return "quota_exceeded"
	case errors.Is(err, syscall.EROFS), errors.Is(err, os.ErrPermission):
		return "not_writable"
	case errors.Is(err, syscall.EIO):
		return "io_error"
	default:
		var je *JournalError
		if errors.As(err, &je) && je.Op == "encode" {
			return "encode_failed"
		}
		return "write_failed"
	}
}

// IsStoreFailure는 이 오류가 §15.5 사상표 8행(store_failure)인지다.
func IsStoreFailure(err error) bool {
	var je *JournalError
	if errors.As(err, &je) {
		return true
	}
	var se *StateFileError
	return errors.As(err, &se)
}

// syncDir는 디렉토리 엔트리를 내구화한다(파일 생성·rename의 완결).
func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	return d.Sync()
}
