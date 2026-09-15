//go:build linux || darwin

package ledger

import "syscall"

// freeBytes는 dir이 속한 파일시스템의 **비특권 사용자가 쓸 수 있는**
// 여유다(Bavail — Bfree는 예약 블록을 포함해 과대평가한다).
func freeBytes(dir string) (uint64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(dir, &st); err != nil {
		return 0, err
	}
	return uint64(st.Bavail) * uint64(st.Bsize), nil
}
