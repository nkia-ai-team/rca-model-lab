//go:build !linux && !darwin

package ledger

// 여유 공간 조회가 없는 플랫폼 — CheckFreeSpace는 통과시킨다.
func freeBytes(string) (uint64, error) { return 0, errFreeSpaceUnsupported }
