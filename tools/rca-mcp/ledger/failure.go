// 실패 선언 — run은 완주하거나 실패하고, 실패한 run의 산출물은 선언
// 하나뿐이다(§15.5. 리포트도, 부분 리포트도, 등급도 없다).
//
// 이 파일이 지는 것은 **스키마와 쓰기 계약**이다. reason→운영자 안내
// 고정 문구표·킬스위치·재기동 주체는 §14-6 몫이지만, 필드가 없으면
// 나중에 못 붙이므로(B-8) 자리를 여기서 만든다.
//
// # B-2 — 자기 자신을 쓸 수 없는 실패
//
// 디스크 풀·쿼터·권한이 run 디렉토리를 막으면 store도 journal도 **선언도**
// 못 쓴다. 그래서 계약이 3단이다:
//
//	① 선언은 여러 경로에 쓴다 — run 디렉토리 + (설정 시) RCA_FAILURE_DIR.
//	   하나만 성공해도 선언은 존재한다(경로 분리).
//	② 전부 실패하면 **stderr 한 줄 JSON + exit code**가 최후 계약이다.
//	   "선언이 생성되거나, 불가 시 exit code·stderr가 계약대로인가"가
//	   §13.1의 불변식이 된다(B-2 ④).
//	③ run 시작 시 여유 공간을 선행 검사한다(CheckFreeSpace) — 디스크가
//	   이미 꽉 찬 상태로 출발해 선언조차 못 쓰는 경우를 앞에서 막는다.
package ledger

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/evidence"
)

// FailureReason은 §15.5 reason enum 11종이다.
type FailureReason string

const (
	ReasonParseFailure               FailureReason = "parse_failure"
	ReasonHypothesisAdmissionFailure FailureReason = "hypothesis_admission_failure"
	ReasonWallClock                  FailureReason = "wall_clock"
	ReasonBackendDown                FailureReason = "backend_down"
	ReasonLLMError                   FailureReason = "llm_error"
	ReasonStoreFailure               FailureReason = "store_failure"
	ReasonProjectorFailure           FailureReason = "projector_failure"
	ReasonInputInvalid               FailureReason = "input_invalid"
	ReasonCancelled                  FailureReason = "cancelled"
	ReasonCitationViolation          FailureReason = "citation_violation"
	ReasonCrash                      FailureReason = "crash"
)

// AllFailureReasons — 전수(사상표 11행 주입 시험의 분모, §13.1).
var AllFailureReasons = []FailureReason{
	ReasonParseFailure, ReasonHypothesisAdmissionFailure, ReasonWallClock,
	ReasonBackendDown, ReasonLLMError, ReasonStoreFailure, ReasonProjectorFailure,
	ReasonInputInvalid, ReasonCancelled, ReasonCitationViolation, ReasonCrash,
}

func ValidFailureReason(r FailureReason) bool {
	for _, x := range AllFailureReasons {
		if x == r {
			return true
		}
	}
	return false
}

// FailureName은 실패 선언 파일 이름이다.
const FailureName = "failure.json"

// FailureDirEnv는 선언의 **분리 경로**를 지정하는 환경변수다(B-2 ①).
// 미설정이면 출력 루트의 failures/가 기본 사본 경로다(§14-6 6a 사용자
// 결정 2026-08-11 — 같은 파일시스템이지만 "run 디렉토리 생성 자체가
// 실패한" 계열을 건진다. 파일시스템 분리는 운영자 설정 몫).
const FailureDirEnv = "RCA_FAILURE_DIR"

// FailuresDirName은 기본 사본 경로의 디렉토리 이름이다(출력 루트 하위).
const FailuresDirName = "failures"

// exit code 계약(B-2 ②). 0은 완주에만 쓴다.
const (
	// ExitFailure — 실패했고 **선언이 어딘가에 쓰였다**.
	ExitFailure = 1
	// ExitDeclarationUnwritable — 실패했고 **선언을 쓸 수 없었다**.
	// stderr의 한 줄이 유일한 산출물이다. 운영자·상위 자동화는 이
	// 코드를 "저장 계층이 죽었다"로 읽는다.
	ExitDeclarationUnwritable = 3
)

// Declaration은 실패 선언이다(§15.5 + B-8 확장). 최소형 {stage, reason,
// run ID}로는 운영자 안내가 갈리지 않는다 — backend_down인데 어느
// 저장소인지, wall_clock인데 어느 층인지, store_failure가 쿼터인지 물리
// 디스크인지가 빠지기 때문이다.
type Declaration struct {
	RunID  string        `json:"run_id"`
	Stage  string        `json:"stage"`
	Reason FailureReason `json:"reason"`

	// Source — 실패를 **시작시킨** 원천(first-match 원리, §15.5).
	// 파생 결과가 아니라 최초 원천을 적는다. 예: "llm" · "journal" ·
	// "tool:scan_metrics" · "restart_scan".
	Source string `json:"source,omitempty"`
	// Backend — 해당 시 ch·vm·pg·llm 중 하나.
	Backend string `json:"backend,omitempty"`
	// Detail — **분류 토큰만**(원문 오류 문자열 금지, §15.3-2).
	// 예: disk_full · quota_exceeded · connect_refused · http_5xx ·
	// stage_timeout · run_timeout.
	Detail string `json:"detail,omitempty"`
	// Retryable — 같은 입력으로 재주행이 의미 있는가.
	Retryable bool `json:"retryable"`
	// Observed·Limit — 수치 실패(wall_clock·쿼터)의 실측과 한도.
	Observed string `json:"observed,omitempty"`
	Limit    string `json:"limit,omitempty"`
	// OperatorAction — 고정 문구표(§14-6)가 채우는 자리. 여기서는
	// 자리만 만든다.
	OperatorAction string    `json:"operator_action,omitempty"`
	DeclaredAt     time.Time `json:"declared_at"`
}

// operatorActions — reason→운영자 안내 고정 문구표(§15.5). LLM 문장화
// 대상이 아니다(§15.4-3과 같은 원리). 표가 여기(선언 생산의 공통 하류)에
// 있는 이유: cmd의 판정기만이 아니라 재기동 스캔(orphan.go)도 선언을
// 생산한다 — 표가 생산자 한 곳에 있으면 다른 생산자가 우회한다
// (6a 검증 D2).
var operatorActions = map[FailureReason]string{
	ReasonParseFailure:               "LLM 서빙 모델·버전 확인 후 재시도 — 출력이 계약을 계속 어기면 모델 구성 점검",
	ReasonHypothesisAdmissionFailure: "선언의 반려 사유를 확인 — 반복되면 seed 품질·수집 범위 점검",
	ReasonWallClock:                  "시간 상한 초과 — 범위를 좁혀 재시도, 반복되면 관측 백엔드 성능 점검",
	ReasonBackendDown:                "관측 백엔드(선언의 backend) 상태 점검 후 재시도",
	ReasonLLMError:                   "LLM 서빙 상태 점검 후 재시도",
	ReasonStoreFailure:               "run 디렉토리 파일시스템(공간·권한·쿼터) 점검",
	ReasonProjectorFailure:           "에이전트 결함 — 선언과 trace를 첨부해 보고",
	ReasonInputInvalid:               "입력·설정을 선언의 detail대로 수정 후 재시도",
	ReasonCancelled:                  "운영자 취소 — 필요 시 재실행",
	ReasonCitationViolation:          "에이전트 결함(작문 인용 위반) — 선언과 trace를 첨부해 보고",
	ReasonCrash:                      "비정상 종료 — journal·trace를 첨부해 보고, 재시도 가능",
}

// OperatorActionFor는 reason의 고정 안내 문구다.
func OperatorActionFor(r FailureReason) string { return operatorActions[r] }

// detailToken — 분류 토큰의 형식. 공백·대문자·문장부호를 막아 원문
// 오류 문자열이 실리는 것을 기계적으로 차단한다.
var detailToken = regexp.MustCompile(`^[a-z0-9_]{1,64}$`)

// Validate는 선언이 계약을 지키는지 본다.
func (d Declaration) Validate() error {
	if d.RunID == "" {
		return errors.New("실패 선언에 run_id 없음")
	}
	if d.Stage == "" {
		return errors.New("실패 선언에 stage 없음 — 어디서 죽었는지가 선언의 핵심")
	}
	if !ValidFailureReason(d.Reason) {
		return fmt.Errorf("reason %q 미정의 (§15.5 enum 11종)", d.Reason)
	}
	if d.Detail != "" && !detailToken.MatchString(d.Detail) {
		return fmt.Errorf("detail %q는 분류 토큰이 아님 — 원문 오류 문자열 금지(§15.3-2)", d.Detail)
	}
	if d.Backend != "" {
		switch d.Backend {
		case "ch", "vm", "pg", "llm":
		default:
			return fmt.Errorf("backend %q 미정의", d.Backend)
		}
	}
	return nil
}

// Line은 선언의 한 줄 JSON이다(최후 계약의 산출물 형태).
func (d Declaration) Line() []byte {
	b, err := json.Marshal(d)
	if err != nil {
		// 선언 직렬화가 실패해도 아무것도 안 내보내면 안 된다.
		return []byte(fmt.Sprintf(`{"run_id":%q,"stage":%q,"reason":%q}`, d.RunID, d.Stage, d.Reason))
	}
	return b
}

// FailurePrefix는 최후 계약 줄의 고정 접두다 — 로그 더미에서 기계가
// 집어낼 수 있어야 한다.
const FailurePrefix = "RCA-FAILURE "

// Declare는 실패 선언을 쓰고 exit code를 돌려준다(§15.5 + B-2).
//
// 여러 경로에 쓰고, **하나라도 성공하면** ExitFailure다. 전부 실패하면
// stderr에 한 줄을 내고 ExitDeclarationUnwritable이다. 선언 줄은
// 어느 경우든 stderr에 나온다 — 파일을 못 보는 운영자가 있기 때문이다.
//
// outRoot는 run 디렉토리들이 앉는 출력 루트다 — runDir이 비어도(생성
// 실패) 사본 경로의 계산에 쓰인다. 모르는 호출자는 ""를 넘긴다.
//
// 프로세스를 죽이지 않는다(exit는 호출자 몫 — 라이브러리가 os.Exit를
// 부르면 시험이 불가능하다).
func Declare(runDir, outRoot string, d Declaration, stderr io.Writer) int {
	if d.DeclaredAt.IsZero() {
		d.DeclaredAt = time.Now().UTC()
	}
	if d.OperatorAction == "" {
		d.OperatorAction = operatorActions[d.Reason]
	}
	// 시크릿 스캔(§15.3-2, 6c) — 실패 선언도 "밖으로 나가는 바이트"다.
	// Detail은 토큰 정규식이 이미 차단하고, 자유도가 남는 필드만 건다.
	for _, f := range []*string{&d.Stage, &d.Source, &d.Observed, &d.Limit} {
		if masked, hits := evidence.MaskSecrets(*f); len(hits) > 0 {
			*f = masked
		}
	}
	line := d.Line()
	if stderr != nil {
		fmt.Fprintf(stderr, "%s%s\n", FailurePrefix, line)
	}
	wrote := false
	for _, dir := range declarationDirs(runDir, outRoot) {
		if dir != runDir {
			// 사본 경로는 없을 수 있다 — 만들어서라도 쓴다(실패는 무시:
			// 다음 경로 시도가 남는다).
			os.MkdirAll(dir, 0o700)
		}
		if err := writeDeclaration(dir, runDir, d); err == nil {
			wrote = true
		}
	}
	if !wrote {
		return ExitDeclarationUnwritable
	}
	return ExitFailure
}

// declarationDirs — 선언을 시도할 경로들(B-2 ① 경로 분리). 사본 경로는
// env가 이기고, 미설정이면 출력 루트의 failures/다(사용자 결정 08-11).
func declarationDirs(runDir, outRoot string) []string {
	var dirs []string
	if runDir != "" {
		dirs = append(dirs, runDir)
	}
	alt := os.Getenv(FailureDirEnv)
	if alt == "" && outRoot != "" {
		alt = filepath.Join(outRoot, FailuresDirName)
	}
	if alt != "" && alt != runDir {
		dirs = append(dirs, alt)
	}
	return dirs
}

// writeDeclaration은 한 경로에 원자적으로 쓴다. 이름 충돌(같은 사본
// 경로에 여러 run)은 run ID를 파일명에 넣어 피한다 — run 디렉토리가
// 아닌 모든 경로가 사본 경로다(env·기본 failures/ 공통).
func writeDeclaration(dir, runDir string, d Declaration) error {
	name := FailureName
	if dir != "" && d.RunID != "" && dir != runDir {
		name = safeRunID(d.RunID) + "-" + FailureName
	}
	b, err := json.MarshalIndent(d, "", "  ")
	if err != nil {
		return err
	}
	b = append(b, '\n')
	tmp, err := os.CreateTemp(dir, name+".tmp-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return err
	}
	tmp.Chmod(0o600)
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return err
	}
	if err := os.Rename(tmpName, filepath.Join(dir, name)); err != nil {
		os.Remove(tmpName)
		return err
	}
	return syncDir(dir)
}

func safeRunID(s string) string {
	out := make([]rune, 0, len(s))
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			out = append(out, r)
		default:
			out = append(out, '_')
		}
	}
	return string(out)
}

// ReadDeclaration은 run 디렉토리의 선언을 읽는다(시험·감사용).
func ReadDeclaration(runDir string) (Declaration, error) {
	b, err := os.ReadFile(filepath.Join(runDir, FailureName))
	if err != nil {
		return Declaration{}, err
	}
	var d Declaration
	if err := json.Unmarshal(b, &d); err != nil {
		return Declaration{}, err
	}
	return d, nil
}

// ── 여유 공간 선행 검사 (B-2 ③) ─────────────────────────────────

// ErrInsufficientSpace는 시작 시점에 이미 여유가 부족하다는 뜻이다.
var ErrInsufficientSpace = errors.New("여유 공간 부족 — run 산출물·선언을 쓸 수 없다")

// errFreeSpaceUnsupported — 여유 공간 조회가 없는 플랫폼의 표식.
var errFreeSpaceUnsupported = errors.New("여유 공간 조회 미지원 플랫폼")

// DefaultReserveBytes는 run 하나가 쓰기를 요구하는 최소 여유다.
//
// **잠정값이다**(판단 지점): §15.2-6이 run당 봉투 상한을 ~130~137개로
// 역산했고 실측 봉투 중앙값이 수십 KB 규모이므로 봉투 원문 수십 MB +
// journal·trace를 합쳐 64MiB를 하한으로 잡는다. 실측 기반 확정은
// §14-7 재주행에서 봉투 바이트 분포를 재고 정한다.
const DefaultReserveBytes uint64 = 64 << 20

// CheckFreeSpace는 dir이 속한 파일시스템의 여유가 need 이상인지 본다.
// 여유를 알 수 없는 플랫폼에서는 통과시킨다(검사가 없다고 run을 막지
// 않는다 — 이 검사는 조기 경고이지 게이트가 아니다).
func CheckFreeSpace(dir string, need uint64) error {
	free, err := freeBytes(dir)
	if err != nil {
		if errors.Is(err, errFreeSpaceUnsupported) {
			return nil
		}
		return err
	}
	if free < need {
		return fmt.Errorf("%w: %s 여유 %d바이트 < 요구 %d바이트", ErrInsufficientSpace, dir, free, need)
	}
	return nil
}
