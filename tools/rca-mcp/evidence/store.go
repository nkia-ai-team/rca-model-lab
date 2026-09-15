// evidence store + index 적재기 (§3 · §5.1 · §5.7 · §15.3).
//
// store는 **봉투 원문 전량**을 run 디렉토리 아래 파일로 남기고 envelope_ref를
// 발급한다. index는 그 봉투에서 투영된 레코드에 EID를 부여한다. 둘은 다른
// 물건이다 — LLM에 가는 것은 index뿐이고, 원문은 [5] fetch_ref와 §5.8-2
// 런타임 스팟 체크, 사후 감사가 읽는다.
//
// 규율:
//
//	· run 중 폐기 금지(§3) — 이 타입에 삭제 API는 없다.
//	· 마스킹하지 않는다(§15.3-1) — 원문 훼손은 감사 가능성을 죽인다.
//	  대신 접근 통제: 디렉토리 0700 · 파일 0600(§15.3-5와 같은 계약).
//	· EID는 불변이고 레코드는 append-only(§5.7).
package evidence

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// Store는 봉투 원문 저장소다.
type Store struct {
	dir string
	mu  sync.Mutex
	seq int
	// index — ref → 파일 경로. 재조회가 파일 시스템을 훑지 않도록.
	byRef map[string]string
	// quota — run당 봉투 수 상한(§15.2-6, 6b 골격). 0이면 무제한(구
	// 동작). 초과의 전이는 절단이 아니라 run 실패(store_failure)다 —
	// "admission을 통과한 정상 경로는 쿼터로 실패하지 않는다"가 §13.1
	// 불변식이고, 산식(N의 함수) 재정은 §14-7 실측 몫.
	quota int
}

// SetQuota는 봉투 수 상한을 정한다(admission 후 N이 동결된 시점에 부른다).
func (s *Store) SetQuota(n int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.quota = n
}

// QuotaError는 쿼터 초과다 — §15.5 store_failure(quota_exceeded)로
// 사상된다(ledger.IsStoreFailure가 아니라 cmd 판정기의 Reason 규약).
type QuotaError struct{ Used, Quota int }

func (e *QuotaError) Error() string {
	return fmt.Sprintf("evidence store 쿼터 초과: %d/%d 봉투(§15.2-6)", e.Used, e.Quota)
}

// NewStore는 run 디렉토리 아래 evidence/ 를 연다(없으면 만든다).
func NewStore(runDir string) (*Store, error) {
	dir := filepath.Join(runDir, "evidence")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("evidence store 생성: %w", err)
	}
	return &Store{dir: dir, byRef: map[string]string{}}, nil
}

// Dir는 store 디렉토리다(감사·검사용).
func (s *Store) Dir() string { return s.dir }

// Put은 봉투 원문을 적재하고 envelope_ref를 발급한다. body는 도구 응답의
// JSON 원문이며 **가공하지 않는다**.
//
// ref 서식은 "EST-0007:<tool>"이다 — 사람이 로그에서 도구를 알아볼 수 있게
// 도구 이름을 붙이되, 유일성은 앞의 일련번호가 진다.
func (s *Store) Put(tool string, body []byte) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.quota > 0 && s.seq >= s.quota {
		return "", &QuotaError{Used: s.seq, Quota: s.quota}
	}
	s.seq++
	ref := fmt.Sprintf("EST-%04d:%s", s.seq, tool)
	name := fmt.Sprintf("%04d-%s.json", s.seq, safeName(tool))
	path := filepath.Join(s.dir, name)
	if err := os.WriteFile(path, body, 0o600); err != nil {
		return "", fmt.Errorf("봉투 적재(%s): %w", ref, err)
	}
	s.byRef[ref] = path
	return ref, nil
}

// Get은 적재된 봉투 원문을 돌려준다(§8.1 인용 무결성 게이트·§5.8-2 스팟
// 체크·[5] fetch_ref의 입력). run 중 폐기가 없으므로 발급된 ref는 항상 산다.
func (s *Store) Get(ref string) ([]byte, error) {
	s.mu.Lock()
	path, ok := s.byRef[ref]
	s.mu.Unlock()
	if !ok {
		return nil, fmt.Errorf("envelope_ref %q 미발급 — store에 없음", ref)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("봉투 읽기(%s): %w", ref, err)
	}
	return b, nil
}

// Has는 ref 실재 검사다(§8.1 게이트가 인용 하나하나에 부르는 값싼 경로).
func (s *Store) Has(ref string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, ok := s.byRef[ref]
	return ok
}

// Count는 적재된 봉투 수다.
func (s *Store) Count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.byRef)
}

func safeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '_', r == '-':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	return b.String()
}

// ParamsDigest는 도구 인자의 정규화 해시다(§5.1 Provenance.ParamsDigest —
// 요청 동일성의 감사·재조회 근거). JSON을 키 순서에 무관하게 정규화한 뒤
// 해시하므로 같은 요청은 표기가 달라도 같은 값이다.
func ParamsDigest(args []byte) string {
	var v any
	if err := json.Unmarshal(args, &v); err != nil {
		sum := sha256.Sum256(args)
		return "PD-" + hex.EncodeToString(sum[:])[:12]
	}
	canon, err := json.Marshal(v) // Go의 map 직렬화는 키 정렬이 보장된다
	if err != nil {
		canon = args
	}
	sum := sha256.Sum256(canon)
	return "PD-" + hex.EncodeToString(sum[:])[:12]
}

// ── index ──────────────────────────────────────────────────────────

// Index는 evidence index다 — 레코드의 append-only 장부.
//
// supersede 계약 본체는 supersede.go(§5.7)에 있고, 여기에는 EID 부여·조회·
// active 뷰가 있다. 명제 장부(§6.0)가 부착돼 있으면 append와 **같은 임계
// 구역 안에서** 갱신된다 — 그것이 "supersede 시 영향 명제를 후계 관측값으로
// 원자적 재계산"의 실물이다.
type Index struct {
	mu    sync.Mutex
	recs  []EvidenceIndexRecord
	byEID map[string]int
	// supersededBy — 대체된 EID → 대체한 EID.
	supersededBy map[string]string
	// heads — 관측 키 → active head EID들(§5.7). 복수 허용(Heads 주석 참조).
	heads map[ObservationKey][]string
	// ledger — 부착된 명제 장부(§6.0). nil이면 index 단독으로 동작한다.
	ledger *Ledger
}

func NewIndex() *Index {
	return &Index{
		byEID:        map[string]int{},
		supersededBy: map[string]string{},
		heads:        map[ObservationKey][]string{},
	}
}

// Append는 레코드에 EID를 부여하고 검증한 뒤 싣는다. 부여 서식은
// "EIX-%04d"이며 EID는 그 뒤로 불변이다(§5.7).
//
// Validate를 통과하지 못한 레코드는 index에 들어가지 못한다 — projector의
// 계약 위반(비유한 수치·상한 초과 Headline·class 없는 finding)이 조용히
// 흐르지 않게 하는 관문이다.
func (ix *Index) Append(r EvidenceIndexRecord) (string, error) {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	if r.EID != "" {
		return "", fmt.Errorf("EID는 index가 부여한다 — 레코드가 %q를 들고 왔다", r.EID)
	}
	eid := fmt.Sprintf("EIX-%04d", len(ix.recs)+1)
	r.EID = eid
	// 시크릿 스캔(§15.3-2, 6c) — LLM-bound 자유문 2필드. EntityKey는
	// 관측 키(§6.0)의 구성원이라 치환이 정체성을 깨고, 사상표상 해시·
	// 개체 정본 키라 자유문이 아니다 — 스캔 제외(판단 지점, 6c 기록).
	// store 원문은 건드리지 않는다(§15.3-1) — 여기는 나가는 사본이다.
	if masked, hits := MaskSecrets(r.Headline); len(hits) > 0 {
		r.Headline = clipRunes(masked, headlineMax)
		r.Masked = append(r.Masked, "headline")
	}
	if masked, hits := MaskSecrets(r.RawExcerpt); len(hits) > 0 {
		// 치환이 원문보다 길어질 수 있다("pw=x"→"pw=[MASKED]") — 상한을
		// 다시 지킨다(Validate가 마스킹 결과로 죽으면 관측이 통째 유실).
		r.RawExcerpt = clipRunes(masked, rawExcerptMax)
		r.Masked = append(r.Masked, "raw_excerpt")
	}
	if err := r.Validate(); err != nil {
		return "", fmt.Errorf("%s 검증: %w", eid, err)
	}
	key := r.Key()
	if err := ix.checkLinksLocked(r, key); err != nil {
		return "", err
	}
	ix.byEID[eid] = len(ix.recs)
	ix.recs = append(ix.recs, r)
	ix.linkLocked(r, key)
	// 명제 장부 갱신은 여기서 일어난다 — append와 나뉘면 supersede 직후
	// 그 관측 키가 잠깐 미실측으로 보이는 창이 생기고, 등록 규칙 3의
	// 부활 문이 그 창에서 무단으로 열린다(4차 A-3).
	if ix.ledger != nil {
		ix.ledger.apply(r, key)
	}
	return eid, nil
}

// AppendAll은 투영 산출 전량을 싣고 부여된 EID를 순서대로 돌려준다.
func (ix *Index) AppendAll(recs []EvidenceIndexRecord) ([]string, error) {
	out := make([]string, 0, len(recs))
	for _, r := range recs {
		eid, err := ix.Append(r)
		if err != nil {
			return out, err
		}
		out = append(out, eid)
	}
	return out, nil
}

// Get은 EID로 레코드를 찾는다.
func (ix *Index) Get(eid string) (EvidenceIndexRecord, bool) {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	return ix.recordLocked(eid)
}

func (ix *Index) recordLocked(eid string) (EvidenceIndexRecord, bool) {
	i, ok := ix.byEID[eid]
	if !ok {
		return EvidenceIndexRecord{}, false
	}
	return ix.recs[i], true
}

// All은 superseded 포함 전 레코드다(감사·재생).
func (ix *Index) All() []EvidenceIndexRecord {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	return append([]EvidenceIndexRecord(nil), ix.recs...)
}

// supersededOf는 그 EID를 대체한 EID다(장부 재생이 죽은 레코드를 건너뛰는
// 데 쓴다).
func (ix *Index) supersededOf(eid string) (string, bool) {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	by, ok := ix.supersededBy[eid]
	return by, ok
}

// attachLedger는 명제 장부를 붙인다 — 이후의 append가 같은 임계 구역에서
// 장부를 갱신한다(§6.0 "supersede 즉시 재판정"의 원자성).
func (ix *Index) attachLedger(l *Ledger) {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	ix.ledger = l
}

// Active는 대체되지 않은 레코드만 돌려준다(§5.7 active 뷰). [4] 입력·
// §5.5 자격 게이트·§8 판정 게이트·§8.1 인용 게이트가 전부 이것만 소비한다.
func (ix *Index) Active() []EvidenceIndexRecord {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	out := make([]EvidenceIndexRecord, 0, len(ix.recs))
	for _, r := range ix.recs {
		if _, dead := ix.supersededBy[r.EID]; dead {
			continue
		}
		out = append(out, r)
	}
	return out
}

// ActiveHeir는 EID의 active 후계를 따라간다 — 채택 가설의 근거 EID가
// superseded면 게이트가 후계로 치환해 판정한다(§5.7, 실패가 아니다).
// cycle은 여기서 죽지 않고 오류가 된다.
func (ix *Index) ActiveHeir(eid string) (string, error) {
	ix.mu.Lock()
	defer ix.mu.Unlock()
	return ix.activeHeirLocked(eid)
}

func (ix *Index) activeHeirLocked(eid string) (string, error) {
	seen := map[string]bool{}
	cur := eid
	for {
		if _, ok := ix.byEID[cur]; !ok {
			return "", fmt.Errorf("EID %q가 index에 없음", cur)
		}
		next, ok := ix.supersededBy[cur]
		if !ok {
			return cur, nil
		}
		if seen[cur] {
			return "", fmt.Errorf("supersede 사슬에 순환(%s)", cur)
		}
		seen[cur] = true
		cur = next
	}
}

// ByKey는 관측 키로 active 레코드를 찾는다(술어 해소·supersede selector).
// 여럿이면 그대로 여럿을 돌려준다 — "복수 해소"는 §6 진리표 행 1이 다룰
// 사실이지 여기서 하나를 골라 감출 것이 아니다.
func (ix *Index) ByKey(k ObservationKey) []EvidenceIndexRecord {
	want := k.Canonical()
	var out []EvidenceIndexRecord
	for _, r := range ix.Active() {
		if r.RecordKind != KindFinding {
			continue
		}
		if r.Key() == want {
			out = append(out, r)
		}
	}
	return out
}

// ScopeFor는 그 키를 덮는 완전 조회 범위 행(query_scope)을 **하나** 찾는다.
//
// **자격 판정의 정본은 여기가 아니라 명제 장부다**(Ledger.Resolve, §6.0).
// 1c 실측: 한 관측 키를 덮는 범위 행이 여럿이고 완전성이 엇갈린다
// (scan_metrics의 shifted omitted=0 · disappeared omitted=19가 같은 키로
// 접힌다). 그때 "완전한 행 하나"를 골라 absent를 해소하면 잘린 형제 구획의
// 꼬리에 그 개체가 있었을 가능성을 덮어쓴다 — 장부는 덮는 행 **전부가**
// 완전할 때만 해소한다. 이 함수는 그 재료를 보는 감사·디버그용이다.
func (ix *Index) ScopeFor(k ObservationKey) (EvidenceIndexRecord, bool) {
	want := k.Canonical()
	for _, r := range ix.Active() {
		if r.RecordKind != KindQueryScope || r.Scope == nil {
			continue
		}
		if r.Scope.Selector.Covers(want) && r.Scope.Complete() {
			return r, true
		}
	}
	return EvidenceIndexRecord{}, false
}

// Rollup은 §5.2 대상별 롤업 행이다 — index 머리에 붙는 기계 계산.
// 미조회 관점 목록은 RequiredViewKeys(§4)가 서는 §14-2의 몫이라 여기서는
// 관측된 것만 센다(없는 분모를 지어내지 않는다).
type Rollup struct {
	TargetID  string
	Anomalous int
	// Weak — normal이면서 Confidence=low(§5.2 "weak 수").
	Weak int
	// NoDataViews — no_data 관점(FindingClass) 목록. 정렬된 유일 목록.
	NoDataViews []string
}

// Rollups는 대상별 롤업을 계산한다.
func (ix *Index) Rollups() []Rollup {
	byTarget := map[string]*Rollup{}
	var order []string
	for _, r := range ix.Active() {
		if r.RecordKind == KindMeta {
			continue
		}
		ru := byTarget[r.TargetID]
		if ru == nil {
			ru = &Rollup{TargetID: r.TargetID}
			byTarget[r.TargetID] = ru
			order = append(order, r.TargetID)
		}
		switch {
		case r.Quality.Status == StatusAnomalous:
			ru.Anomalous++
		case r.Quality.Status == StatusNormal && r.Quality.Confidence == ConfLow:
			ru.Weak++
		case r.Quality.Status == StatusNoData:
			ru.NoDataViews = append(ru.NoDataViews, r.FindingClass)
		}
	}
	sort.Strings(order)
	out := make([]Rollup, 0, len(order))
	for _, t := range order {
		ru := byTarget[t]
		ru.NoDataViews = uniqSorted(ru.NoDataViews)
		out = append(out, *ru)
	}
	return out
}

func uniqSorted(in []string) []string {
	if len(in) == 0 {
		return nil
	}
	sort.Strings(in)
	out := in[:1]
	for _, s := range in[1:] {
		if s != out[len(out)-1] {
			out = append(out, s)
		}
	}
	return out
}
