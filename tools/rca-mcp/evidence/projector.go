// projector — 도구 봉투를 evidence index 레코드로 투영한다(§5, §14-1 1b).
//
// 이 파일은 **공통 기계**다: class 판별 · 사상표 조회 · Effect/Quality/Window
// 산출 · query_scope 생성 · 패닉 격리. 도구마다 다른 finding 구조의 해석은
// extractors.go의 도구별 함수가 진다.
//
// 설계 규율 3가지:
//
//	① 조용히 버리지 않는다 — 사상표에 없는 class는 meta 레코드로 남기고
//	   이름을 호출자에게 돌려준다(§5.8: projector의 추출 버그는 전 파이프라인의
//	   조용한 오염이다).
//	② 값을 지어내지 않는다 — 봉투에 없는 쪽은 nil이다. 0으로 채우면 "실측 0"과
//	   "원천 부재"가 구분되지 않는다.
//	③ 재평균하지 않는다 — 도구가 이미 낸 판정값(ratio_p50 등)을 승계한다(§5.1).
package evidence

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"time"
)

// ProjectRequest는 봉투 하나를 투영하는 데 필요한 호출 맥락이다.
// 봉투 자신이 모르는 것(누가·무엇을·어떤 창 클래스로 불렀는가)만 든다.
type ProjectRequest struct {
	// Source — 사상표 원천. Tool은 실제 호출된 도구 이름(술어 불가 도구
	// 판별과 감사용)이며 합성 원천에서는 Source와 다르다(change ↔ list_changes).
	Source ObservationSource
	Tool   string
	// TargetID — 도구 호출의 대상 인자. 레코드 TargetID의 정본이다.
	TargetID string
	Domain   string
	// WindowClass — full | onset_narrow. 실 창에서 판정할 수 없으므로
	// 호출자가 준다(§6.0 — 키에 드는 것은 창 enum).
	WindowClass WindowClass
	// From/To — 조회 창. 봉투 observed_range가 있으면 그쪽이 이긴다.
	From, To time.Time
	// ResolutionS — 도구의 스캔 스텝. finding에 bucket_seconds가 있으면 그쪽.
	ResolutionS int
	ClockSkew   ClockSkew
	// collectLag — **호출자가 주지 않는다**(§5.5 원천 결정, 2026-08-04).
	// 지연은 봉투가 받아온 관측 데이터에서 나오므로 산출 지점은 봉투를 손에
	// 쥔 이 층 하나여야 한다 — 공개 필드로 두면 호출자마다 다른 원천으로
	// 채우던 종전 구조(A6 대상별 표)가 그대로 남는다. 값은 project()가
	// CollectLagOf로 채운다.
	collectLag CollectLag
	// ParamsDigest·EnvelopeRef — 감사·인용 무결성(§8.1)의 재료.
	ParamsDigest string
	EnvelopeRef  string
}

// ProjectResult는 투영 산출이다.
type ProjectResult struct {
	Records []EvidenceIndexRecord
	// UnknownClasses — 사상표에도 meta 목록에도 없던 class. 레코드는 meta로
	// 남지만 이 목록이 비어 있지 않다는 것 자체가 사상표 갱신 신호다.
	// §5.8 골든 정합 테스트는 fixture 전수에서 이 목록이 비기를 요구한다.
	UnknownClasses []string
}

const (
	// confMinSample — 이보다 적은 표본은 Confidence=low(§5.1 "표본 부족").
	// 잠정값이며 §13 실측으로 재보정한다.
	confMinSample = 10
	// ratioBaselineEps — |Baseline| ≤ eps면 ratio Magnitude를 만들지 않는다.
	//
	// B-11: scan_metrics는 baseline median 0에서도 shifted finding을 정상
	// 생성한다(MAD·scale 바닥값). 그때 Observed/Baseline은 +Inf가 되어 JSON
	// 직렬화가 죽거나 정렬에서 무한대가 최상위로 올라간다. 세 선택지(안전
	// 분모·별도 Kind·유한 대칭 변화량) 중 **Magnitude 부재**를 택했다: 파생
	// 불능은 관측 부재와 같다는 §5.1 원칙과 같은 처리이고, magnitude_* 술어는
	// 그 레코드에서 inconclusive가 된다(지지도 반증도 아니다).
	// Observed·Baseline 자체는 그대로 실어 사람이 읽을 수 있게 둔다.
	ratioBaselineEps = 1e-12
)

// Project는 봉투 하나를 레코드 목록으로 투영한다. EID는 붙지 않는다 —
// 부여 주체는 Index.Append다(§5.1 EID 불변 계약).
//
// **패닉은 여기서 error가 된다**: projector 호출 경계에 recover가 없으면
// 추출 버그가 §15.5 사상표 11행(crash)으로 떨어져 9행(projector_failure)을
// 분리한 취지가 무너진다(checklist 말미 지적 채택).
func Project(req ProjectRequest, env RawEnvelope) (res ProjectResult, err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("projector 패닉(%s/%s): %v", req.Source, req.Tool, r)
			res = ProjectResult{}
		}
	}()
	return project(req, env)
}

func project(req ProjectRequest, env RawEnvelope) (ProjectResult, error) {
	var res ProjectResult
	if req.Tool != "" && IsPredicateFreeTool(req.Tool) {
		// 술어 불가 도구 — 봉투는 store에 남고 ref도 발급되지만 index
		// 레코드는 만들지 않는다(§5.1: 사상표 행 없음. Provenance.Source의
		// 어휘가 사상표 원천으로 닫혀 있어 meta 레코드조차 표현할 수 없다).
		return res, nil
	}
	if !ValidSource(req.Source) {
		return res, fmt.Errorf("원천 %q는 사상표에 행이 없음", req.Source)
	}
	if req.TargetID == "" {
		return res, fmt.Errorf("target_id 없음 — 레코드 대상이 미정")
	}
	if !ValidWindowClass(req.WindowClass) {
		return res, fmt.Errorf("window class %q 미정의", req.WindowClass)
	}

	// 수집 지연은 봉투에서 나온다 — 요청 창 끝과 실관측 끝의 간격이다.
	// **from/to를 덮어쓰기 전에** 계산한다: 아래에서 to가 observed_range로
	// 바뀌고 나면 두 시각이 같아져 지연이 항상 0이 된다.
	req.collectLag = CollectLagOf(req.To, env)

	from, to := req.From, req.To
	if env.ObservedRange != nil {
		from, to = env.ObservedRange.From, env.ObservedRange.To
	}
	// 공허한 backend_error 봉투(§14-7 ③ 안 A) — guard가 백엔드 실패를
	// no_data(backend_error)로 사상하면 Findings·Scopes가 둘 다 비어
	// 아래 두 루프가 레코드를 0건 낳는다. 그러면 §5.3-2 "backend_error
	// 전량 유지"·§15.4-2 BackendGaps·재생성 입력 ⑤가 전부 굶는다 —
	// 결손의 존재 자체를 KindMeta 1건으로 남긴다(감사 재료, 술어 판정
	// 대상 아님 — 백엔드 장애 관측으로 가설을 지지/반증하면 안 된다).
	// 조건을 backend_error로 좁히는 이유: 다른 사유의 no_data는 도구가
	// Scopes를 채우는 관행이라 여기서 또 만들면 중복이다.
	if Status(env.Status) == StatusNoData &&
		NoDataReason(env.NoDataReason) == NoDataBackendError &&
		len(env.Findings) == 0 && len(env.Scopes) == 0 {
		res.Records = append(res.Records, backendGapRecord(req, env, from, to))
		return res, nil
	}

	// 구획별 절단 조회 — finding 레코드의 Truncation이 읽는다.
	omittedOf := map[string]int{}
	for _, s := range env.Scopes {
		omittedOf[s.Class] += s.Omitted
	}

	extract := extractorFor(req.Source)
	unknown := map[string]bool{}
	for _, f := range env.Findings {
		class := classKeyOf(req.Source, f)
		rows := classRows(req.Source, class)
		if len(rows) == 0 {
			if !IsMetaClass(req.Source, class) {
				unknown[class] = true
			}
			res.Records = append(res.Records, metaRecord(req, env, f, class, from, to))
			continue
		}
		for _, row := range rows {
			// 절단 수는 **그 finding이 속한 구획**의 것이다 — 행 이름이 아니라
			// 봉투의 절단 단위 이름으로 찾는다(breakdown은 구획별로 자르는데
			// 행은 판정 축별이라 둘이 다르다).
			rec, ok := projectFinding(req, env, row, f, extract, omittedOf[canonClass(req.Source, class)], from, to)
			if !ok {
				continue
			}
			res.Records = append(res.Records, rec)
		}
	}

	res.Records = append(res.Records, scopeRecords(req, env, from, to)...)
	for c := range unknown {
		res.UnknownClasses = append(res.UnknownClasses, c)
	}
	sort.Strings(res.UnknownClasses)
	return res, nil
}

// projectFinding은 finding 하나 × 사상표 행 하나 → 레코드 하나다.
// 한 finding이 행 여럿에 걸리는 도구가 있어(breakdown_endpoints의 지연·실패율)
// 이 단위가 레코드 단위다.
func projectFinding(req ProjectRequest, env RawEnvelope, row ClassRow,
	f RawFinding, extract extractFn, omitted int,
	from, to time.Time) (EvidenceIndexRecord, bool) {

	ex, ok := extract(req, row, f)
	if !ok {
		// 그 class의 판정 축이 이 finding에 없다 — 레코드를 만들지 않는다
		// (예: 표본 0인 진입점에는 failed_rate 자체가 없다). 지어내지 않는 쪽.
		return EvidenceIndexRecord{}, false
	}
	rec := EvidenceIndexRecord{
		TargetID:     req.TargetID,
		Domain:       req.Domain,
		Aspect:       row.Aspect,
		RecordKind:   KindFinding,
		FindingClass: row.FindingClass(),
		EntityKey:    ex.EntityKey,
		Headline:     clip(ex.Headline, headlineMax),
		RawExcerpt:   sanitizeExcerpt(ex.RawExcerpt),
		Effect:       ex.Effect,
		Window: TimeWindow{
			From: from.UTC(), To: to.UTC(),
			ResolutionS: resolutionOf(req, f),
			Class:       req.WindowClass,
			CollectLag:  req.collectLag,
			ClockSkew:   req.ClockSkew,
		},
		Provenance: Provenance{
			Source:       req.Source,
			ParamsDigest: req.ParamsDigest,
			EnvelopeRef:  req.EnvelopeRef,
		},
		DrilldownRefs: f.refs(),
	}
	if rec.EntityKey == "" {
		rec.EntityKey = entityKeyOf(req, row, f)
	}
	// 변화구간 — 산출 가능한 행에서만(§5.1 가용성 계약).
	if row.Onset {
		cf, ct := ex.ChangeFrom, ex.ChangeTo
		if cf == nil && ct == nil {
			cf, ct = onsetOf(row, f)
		}
		rec.Window.ChangeFrom, rec.Window.ChangeTo = cf, ct
	}
	rec.Quality = qualityOf(env, ex, omitted, rec.Effect)
	// env.QueryTruncated 합류(§14-7 D-1) — CH 캡 절단은 Total 미상이라
	// Scopes omitted로 못 오고 봉투 bool로만 온다. 안 실으면 절단된 조회의
	// 0건이 observed_zero·반증 자격을 얻는다(§5.1 계약 1 위반). 표시 쿼터
	// 절단(env.Truncated)은 합류하지 않는다 — 조회는 완전하다.
	rec.Truncation = Truncation{Truncated: omitted > 0 || env.QueryTruncated, OmittedN: omitted}
	rec.Provenance.LineageID = lineageOf(req, rec)
	return rec, true
}

// ── class 판별 ─────────────────────────────────────────────────────

// classKeyOf는 finding에서 class 값을 읽는다. 도구마다 그 값이 실린 키가
// 다르고(class·section·kind·signal·relation·observation), 아예 없는 도구도
// 있다(read_timeseries 시계열 행·db_blocking 사건 행·get_processes) —
// **키 부재도 하나의 class**이며 사상표가 그 이름을 정한다.
func classKeyOf(src ObservationSource, f RawFinding) string {
	// 공통 탐색이 위험한 원천은 볼 키를 좁힌다 — change의 `kind`는 변경
	// 종류(policy_deploy…)이지 finding class가 아니라서, 공통 목록에 맡기면
	// 변경 종류마다 "사상표에 없는 class"가 된다.
	keys := classKeys[src]
	if keys == nil {
		keys = []string{"class", "section", "kind", "signal", "relation", "observation"}
	}
	for _, k := range keys {
		if v := f.str(k); v != "" {
			return v
		}
	}
	return defaultClass[src]
}

// defaultClass — class 키가 아예 없는 도구의 기본 class(사상표 행 이름).
var defaultClass = map[ObservationSource]string{
	SrcReadTimeseries: "series",
	SrcDBBlocking:     "event",
	SrcProcesses:      "process",
	SrcChange:         "change",
}

// classKeys — 공통 탐색 대신 이 키만 보는 원천.
var classKeys = map[ObservationSource][]string{
	// change 봉투에서 class를 가르는 것은 section(summary_meta)뿐이다.
	SrcChange: {"section"},
}

// ── 공통 산출 ──────────────────────────────────────────────────────

// entityKeyOf는 사상표 EntityKey 정본 열을 조립한다. "$target"은 도구 호출의
// 대상 인자다(봉투에 없지만 개체 식별에 필요한 열 — db_blocking의 사건 ID,
// snmp의 대상). 열이 비어 있는 행(metric 계열)은 빈 값이며 Metric이 식별을
// 겸한다(§5.1 "—" 행).
func entityKeyOf(req ProjectRequest, row ClassRow, f RawFinding) string {
	if len(row.EntityCols) == 0 {
		return ""
	}
	parts := make([]string, 0, len(row.EntityCols))
	for _, c := range row.EntityCols {
		if c == "$target" {
			parts = append(parts, req.TargetID)
			continue
		}
		parts = append(parts, f.str(c))
	}
	return strings.Join(parts, "|")
}

// onsetOf는 변화구간을 읽는다. 행이 지목한 키만 본다 — 조회 창으로 채우는
// 경로 자체를 만들지 않는다(§5.1 구현 함정 명시).
func onsetOf(row ClassRow, f RawFinding) (*time.Time, *time.Time) {
	read := func(spec string) (time.Time, bool) {
		if spec == "" {
			return time.Time{}, false
		}
		// "a|b" — 앞 키가 없으면 뒤 키(에피소드의 open_at ↔ derived_open_at).
		for _, key := range strings.Split(spec, "|") {
			if strings.HasSuffix(key, ".interval") {
				head := strings.TrimSuffix(key, ".interval")
				if s := f.sub(head).str("interval"); s != "" {
					if a, _, ok := parseInterval(s); ok {
						return a, true
					}
				}
				continue
			}
			if t, ok := parseAt(f.str(key)); ok {
				return t, true
			}
		}
		return time.Time{}, false
	}
	// interval 표기는 한 키에 시작·끝이 같이 있다.
	if strings.HasSuffix(row.OnsetFrom, ".interval") {
		head := strings.TrimSuffix(row.OnsetFrom, ".interval")
		if s := f.sub(head).str("interval"); s != "" {
			if a, b, ok := parseInterval(s); ok {
				return &a, &b
			}
		}
		return nil, nil
	}
	a, okA := read(row.OnsetFrom)
	if !okA {
		return nil, nil
	}
	b, okB := read(row.OnsetTo)
	if !okB {
		// 끝이 없는 구간(단건 이벤트·미해소)은 시작만 싣는다 — 없는 끝을
		// 창 끝으로 채우면 §10이 겹침 판정으로 기울어진다.
		return &a, nil
	}
	return &a, &b
}

// resolutionOf는 버킷 해상도(초)다 — finding이 실었으면 그것, 아니면 호출 맥락.
func resolutionOf(req ProjectRequest, f RawFinding) int {
	if v := f.num("bucket_seconds"); v != nil {
		return int(*v)
	}
	return req.ResolutionS
}

// magnitudeOf는 §5.1 Kind별 정의식이다. 세 소비자(정렬·truth 파생·수치 치환)가
// 서로 다른 값을 만들지 않도록 산식은 여기 한 곳이다.
func magnitudeOf(kind EffectKind, observed, baseline, toolValue *float64) (*float64, bool) {
	switch kind {
	case EffectCategorical:
		return nil, false
	case EffectRatio:
		if toolValue != nil {
			return toolValue, false // 도구 판정값 승계 — 재계산 금지
		}
		if observed == nil || baseline == nil {
			return nil, false
		}
		if *baseline > -ratioBaselineEps && *baseline < ratioBaselineEps {
			return nil, true // B-11: 분모 0 — 부재 + 품질 강등
		}
		m := *observed / *baseline
		return &m, false
	default: // count · duration · share — Magnitude = Observed
		return observed, false
	}
}

// directionOf는 기준선 대비 방향이다. 기준선이 없으면 n/a — "변화 없음"(flat)과
// "비교 대상 없음"은 다르다.
func directionOf(observed, baseline *float64) Direction {
	if observed == nil || baseline == nil {
		return DirNA
	}
	switch {
	case *observed > *baseline:
		return DirUp
	case *observed < *baseline:
		return DirDn
	}
	return DirFlt
}

// qualityOf는 품질·가용성의 규칙 계산이다(§5.1). LLM 선언이 아니다.
func qualityOf(env RawEnvelope, ex extracted, omitted int, eff Effect) Quality {
	st := Status(env.Status)
	if !ValidStatus(st) {
		st = StatusNormal
	}
	reason := NoDataReason(env.NoDataReason)
	q := Quality{
		SampleN:      ex.SampleN,
		Status:       st,
		NoDataReason: reason,
		// complete에는 질의 계층 절단(env.QueryTruncated — CH 캡 등 Total
		// 미상)도 든다(§14-7 D-1). 절단된 조회의 0건은 observed_zero가
		// 아니다. 표시 쿼터 절단(env.Truncated)은 여기 안 든다.
		Availability: AvailabilityOf(st, reason, omitted == 0 && !env.QueryTruncated),
		Confidence:   ConfOK,
	}
	switch {
	// 의미 강등(2b D-3): 해석 규칙 결손 봉투의 파생 관측은 상태 불문
	// low다 — anomalous도 counter 오판별의 산물일 수 있다(§15.2-3
	// "어떤 class가 low-confidence가 되는지"의 기계 배선).
	case len(env.DegradedSources) > 0:
		q.Confidence = ConfLow
	// §5.4-1: normal인데 못 본 후보가 있으면 그 normal은 판정이 아니다.
	case st == StatusNormal && (omitted > 0 || env.QueryTruncated):
		q.Confidence = ConfLow
	case q.SampleN != nil && *q.SampleN < confMinSample:
		q.Confidence = ConfLow
	case ex.Low:
		q.Confidence = ConfLow
	case eff.Kind == EffectRatio && eff.Magnitude == nil && eff.Observed != nil:
		// 분모 0 등으로 판정 크기를 못 만든 ratio(B-11).
		q.Confidence = ConfLow
	}
	return q
}

// lineageOf는 원천 계보 해시다 — 백엔드 종류 + 물리 데이터셋/시계열 + 대상.
// **조회 창은 빠진다**: 같은 시계열을 full과 onset_narrow로 두 번 읽은 것은
// 같은 계보이고, §8이 중복 파생을 하나로 세는 근거가 이 값이다.
// 원천은 ref의 앞부분이다(시각 조각을 떼어낸 것) — ref가 없으면 원천·대상만.
func lineageOf(req ProjectRequest, rec EvidenceIndexRecord) string {
	base := string(req.Source) + "|" + req.TargetID + "|" + rec.Effect.Metric
	if refs := rec.DrilldownRefs; len(refs) > 0 {
		base = lineageStem(refs[0]) + "|" + req.TargetID + "|" + rec.Effect.Metric
	}
	sum := sha256.Sum256([]byte(base))
	return "LIN-" + hex.EncodeToString(sum[:])[:12]
}

// lineageStem은 ref에서 "백엔드:데이터셋"만 남긴다.
// vm:<metric>{target_id=X}:<from>/<to> → vm:<metric>{target_id=X}
// ch:<table>:<id…>                     → ch:<table>
func lineageStem(ref string) string {
	parts := strings.Split(ref, ":")
	if len(parts) < 2 {
		return ref
	}
	stem := parts[0] + ":" + parts[1]
	if parts[0] == "vm" {
		return stem // vm ref의 두 번째 조각이 이미 시계열 지문이다
	}
	return stem
}

// metaRecord는 술어 판정 대상이 아닌 class의 감사 레코드다(§5.1 RecordKind=meta).
// Effect·EntityKey는 두지 않는다 — 있으면 게이트가 실수로 소비할 수 있다.
func metaRecord(req ProjectRequest, env RawEnvelope, f RawFinding, class string, from, to time.Time) EvidenceIndexRecord {
	st := Status(env.Status)
	if !ValidStatus(st) {
		st = StatusNormal
	}
	reason := NoDataReason(env.NoDataReason)
	return EvidenceIndexRecord{
		TargetID:   req.TargetID,
		Domain:     req.Domain,
		Aspect:     metaAspect(req.Source),
		RecordKind: KindMeta,
		Headline:   clip(fmt.Sprintf("%s meta class %s — 감사 재료(술어 판정 대상 아님)", req.Source, class), headlineMax),
		Window: TimeWindow{From: from.UTC(), To: to.UTC(),
			ResolutionS: req.ResolutionS, CollectLag: req.collectLag, ClockSkew: req.ClockSkew},
		Quality: Quality{Status: st, NoDataReason: reason,
			Availability: AvailabilityOf(st, reason, !env.Truncated && !env.QueryTruncated), Confidence: ConfOK},
		Provenance: Provenance{Source: req.Source, ParamsDigest: req.ParamsDigest,
			EnvelopeRef: req.EnvelopeRef},
		DrilldownRefs: f.refs(),
	}
}

// backendGapRecord는 공허한 backend_error 봉투의 존재 증명이다(§14-7 ③
// 안 A). metaRecord와 같은 계약(KindMeta — Effect·EntityKey 없음)이며,
// Headline에는 분류 토큰(Backend·BackendDetail)만 싣는다 — 원문 오류
// 문자열은 봉투에 애초에 없다(§15.3-2, guard가 원천에서 규율).
func backendGapRecord(req ProjectRequest, env RawEnvelope, from, to time.Time) EvidenceIndexRecord {
	head := "backend_error"
	if env.Backend != "" {
		head += "(" + env.Backend
		if env.BackendDetail != "" {
			head += "/" + env.BackendDetail
		}
		head += ")"
	}
	reason := NoDataReason(env.NoDataReason)
	return EvidenceIndexRecord{
		TargetID:   req.TargetID,
		Domain:     req.Domain,
		Aspect:     metaAspect(req.Source),
		RecordKind: KindMeta,
		Headline: clip(fmt.Sprintf("%s %s — 관측 백엔드 결손(감사 재료, 술어 판정 대상 아님·배제 근거 아님 §5.5)",
			req.Source, head), headlineMax),
		Window: TimeWindow{From: from.UTC(), To: to.UTC(),
			ResolutionS: req.ResolutionS, CollectLag: req.collectLag, ClockSkew: req.ClockSkew},
		Quality: Quality{Status: StatusNoData, NoDataReason: reason,
			Availability: AvailabilityOf(StatusNoData, reason, false), Confidence: ConfOK},
		Provenance: Provenance{Source: req.Source, ParamsDigest: req.ParamsDigest,
			EnvelopeRef: req.EnvelopeRef},
	}
}

// metaAspect는 meta 레코드의 Aspect다 — 그 도구의 Aspect(복수면 첫 값).
// Aspect는 닫힌 enum이라 비워둘 수 없다(Validate가 막는다).
func metaAspect(src ObservationSource) Aspect {
	if as := AspectOf(src); len(as) > 0 {
		return as[0]
	}
	return AspectMetric
}

// ── query_scope ────────────────────────────────────────────────────

// scopeRecords는 봉투의 구조화 절단 필드에서 조회 범위 레코드를 만든다
// (§5.1 계약 1). Findings 유무와 무관하게 생성되며, 이것이 "0건 조회"와
// "잘림"의 유일한 원천이다.
//
// 한 scope가 사상표 행 여럿에 걸리는 도구가 있다(breakdown_endpoints는 구획별로
// 자르는데 판정 축이 지연·실패율 둘이다). 그때 같은 관측 키로 접히는 scope들은
// **합산해서 한 레코드로** 만든다 — 같은 키에 범위 행이 둘이면 §6.0 장부의
// "덮는 범위 행"이 유일하지 않게 되어 진리표 행 1(복수 해소)로 죽는다.
func scopeRecords(req ProjectRequest, env RawEnvelope, from, to time.Time) []EvidenceIndexRecord {
	type acc struct {
		row                     ClassRow
		metric                  string
		total, returned, omit   int
		classes                 []string
		anyIncomplete, anyExist bool
	}
	order := []string{}
	byKey := map[string]*acc{}
	for _, s := range env.Scopes {
		for _, row := range scopeRows(req.Source, s.Class) {
			// 범위 행의 지표 축: 행이 판정 축을 고정한 도구는 그 값으로,
			// 지표가 finding마다 다른 도구(metric 계열·이벤트 속성)는 **열어
			// 둔다** — 닫아 두면 그 범위 행이 자기 개체 행을 하나도 덮지 못해
			// absent 술어 해소 경로가 사라진다(C-3).
			metric := row.MetricFixed
			if metric == "" {
				metric = s.Metric
			}
			if metric == "" {
				metric = EntityAny
			}
			key := row.FindingClass() + "|" + metric
			a := byKey[key]
			if a == nil {
				a = &acc{row: row, metric: metric}
				byKey[key] = a
				order = append(order, key)
			}
			a.total += s.Total
			a.returned += s.Returned
			a.omit += s.Omitted
			a.classes = append(a.classes, s.Class)
			if s.Omitted > 0 {
				a.anyIncomplete = true
			}
			if s.Total > 0 {
				a.anyExist = true
			}
		}
	}
	var out []EvidenceIndexRecord
	for _, key := range order {
		a := byKey[key]
		// 조회 단위의 status는 **그 단위의 결과**다 — 봉투 전체 승계가 아니다.
		// 봉투는 anomalous인데 신호 하나는 0건일 수 있고(k8s 4신호), 그 0건이
		// 바로 absent 술어를 해소하는 관측이기 때문이다.
		st, reason := StatusNormal, NoDataReason("")
		if !a.anyExist {
			st, reason = StatusNoData, NoDataZeroObservations
		}
		// 봉투 전체가 결손이면 그 사유가 이긴다 — 결손 위의 0건은 0건이 아니다.
		if Status(env.Status) == StatusNoData {
			if r := NoDataReason(env.NoDataReason); r != NoDataZeroObservations && ValidNoDataReason(r) {
				st, reason = StatusNoData, r
			}
		}
		scope := QueryScope{
			Selector: ObservationKey{
				Source: req.Source, TargetID: req.TargetID, Aspect: a.row.Aspect,
				Metric: a.metric, EntityKey: EntityAny, Window: req.WindowClass,
			}.Canonical(),
			Total: a.total, Returned: a.returned, Omitted: a.omit,
		}
		rec := EvidenceIndexRecord{
			TargetID:     req.TargetID,
			Domain:       req.Domain,
			Aspect:       a.row.Aspect,
			RecordKind:   KindQueryScope,
			FindingClass: a.row.FindingClass(),
			EntityKey:    EntityAny,
			Headline: clip(fmt.Sprintf("조회 범위 %s(%s): 총 %d건 중 %d건 반환, %d건 미확인",
				a.row.FindingClass(), strings.Join(a.classes, "+"), a.total, a.returned, a.omit), headlineMax),
			Effect: Effect{Kind: EffectCount, Metric: a.metric, Direction: DirNA},
			Window: TimeWindow{From: from.UTC(), To: to.UTC(),
				ResolutionS: req.ResolutionS, Class: req.WindowClass,
				CollectLag: req.collectLag, ClockSkew: req.ClockSkew},
			Quality: Quality{
				Status: st, NoDataReason: reason,
				// 질의 계층 절단(env.QueryTruncated)도 완전성에 든다(§14-7
				// D-1) — CH 캡 절단은 Total 미상이라 omit로 못 온다. 표시
				// 쿼터 절단(env.Truncated)은 조회가 완전하므로 제외.
				Availability: AvailabilityOf(st, reason, a.omit == 0 && !env.QueryTruncated),
				Confidence:   ConfOK,
			},
			Provenance: Provenance{Source: req.Source, ParamsDigest: req.ParamsDigest,
				EnvelopeRef: req.EnvelopeRef},
			Scope:      &scope,
			Truncation: Truncation{Truncated: a.omit > 0 || env.QueryTruncated, OmittedN: a.omit},
		}
		if a.omit > 0 || env.QueryTruncated {
			rec.Quality.Confidence = ConfLow
		}
		rec.Provenance.LineageID = lineageOf(req, rec)
		out = append(out, rec)
	}
	return out
}

// scopeRows는 봉투의 절단 단위 이름을 사상표 행으로 푼다. 기본은 1:1이고,
// 절단 단위와 판정 축이 어긋나는 도구만 예외다.
func scopeRows(src ObservationSource, class string) []ClassRow {
	if src == SrcBreakdownEndpoints {
		// 절단은 구획(entry/step/egress) 단위인데 판정 축은 지연·실패율 둘이다.
		switch class {
		case "entry", "step", "egress":
			var out []ClassRow
			for _, c := range []string{"endpoint_latency", "endpoint_failure"} {
				if r, ok := Row(src, c); ok {
					out = append(out, r)
				}
			}
			return out
		}
	}
	if r, ok := Row(src, class); ok {
		return []ClassRow{r}
	}
	return nil
}
