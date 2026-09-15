// payload 디코더 — 저장된 이벤트를 concrete payload로 복원한다
// (§15.5-④ 크래시 계약. "없으면 파일이 있어도 복구 불능"이 이 파일의
// 존재 이유다).
//
// Event.Payload는 인터페이스라 encoding/json이 되돌리지 못한다:
// json.Unmarshal은 인터페이스 필드에 map[string]any를 넣거나 거부한다.
// 복원은 Type으로 concrete 타입을 고르는 등록표(payloadDecoders)를
// 거쳐야만 가능하고, 등록표에 빠진 타입이 있으면 그 이벤트가 실린 run은
// 재기동 복원이 불가능해진다 — 그래서 전수 검사를 시험이 진다
// (TestDecoderCoversEveryEventType).
package ledger

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"time"
)

// wireEvent는 이벤트의 디스크 표현이다. 필드 이름은 **구 ledger.jsonl과
// 같다**(Go 기본 이름) — 기존 run 산출물(runs/*/ledger.jsonl)이 그대로
// 이 디코더로 읽혀야 하기 때문이다. 태그를 새로 붙이면 과거 산출물이
// 읽히지 않는다.
type wireEvent struct {
	Seq     int
	Time    time.Time
	Actor   Actor
	Type    EventType
	Payload json.RawMessage
}

// EncodeEvent는 이벤트 한 줄을 직렬화한다(개행 없음). journal과 구
// ledger.jsonl dump가 같은 바이트를 내도록 여기 한 곳에서 만든다.
func EncodeEvent(ev Event) ([]byte, error) {
	return json.Marshal(ev)
}

// DecodeEvent는 한 줄을 Event로 복원한다. Type이 등록표에 없거나
// payload가 그 타입의 스키마와 어긋나면 오류다(fail-closed — 조용히
// 빈 payload로 복원하면 재생 상태가 실제 run과 갈린다).
func DecodeEvent(line []byte) (Event, error) {
	var w wireEvent
	if err := json.Unmarshal(line, &w); err != nil {
		return Event{}, fmt.Errorf("이벤트 겉면 디코드: %w", err)
	}
	dec, ok := payloadDecoders[w.Type]
	if !ok {
		return Event{}, fmt.Errorf("이벤트 타입 %q 디코더 없음 — 등록표 누락", w.Type)
	}
	p, err := dec(w.Payload)
	if err != nil {
		return Event{}, fmt.Errorf("seq %d %s payload 디코드: %w", w.Seq, w.Type, err)
	}
	return Event{Seq: w.Seq, Time: w.Time, Actor: w.Actor, Type: w.Type, Payload: p}, nil
}

// decodeAs는 타입 하나의 디코더다. **DisallowUnknownFields를 쓴다** —
// 같은 바이너리가 쓰고 읽는 것이 크래시 복원의 전제이므로, 모르는 필드는
// 스키마 드리프트(다른 버전이 쓴 journal)의 신호이고 조용히 흘리면
// 복원한 상태가 죽기 직전 상태와 다르다.
func decodeAs[T Payload](raw json.RawMessage) (Payload, error) {
	var p T
	if len(raw) == 0 {
		return nil, errors.New("payload 없음")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&p); err != nil {
		return nil, err
	}
	return p, nil
}

// payloadDecoders — 이벤트 타입 전수(구 16종 + 신 5종). 새 이벤트 타입을
// 만들면 여기에 한 줄을 더해야 하고, 잊으면 전수 시험이 실패한다.
var payloadDecoders = map[EventType]func(json.RawMessage) (Payload, error){
	// 구 16종 (event.go)
	EvHypothesisCreated:            decodeAs[HypothesisCreated],
	EvHypothesisRejectedAtCreation: decodeAs[HypothesisRejectedAtCreation],
	EvProbePredicted:               decodeAs[ProbePredicted],
	EvProbeExecuted:                decodeAs[ProbeExecuted],
	EvDiscriminationExhausted:      decodeAs[DiscriminationExhausted],
	EvEvidenceProposed:             decodeAs[EvidenceProposed],
	EvEvidenceLinkPassed:           decodeAs[EvidenceLinkPassed],
	EvEvidenceLinkRejected:         decodeAs[EvidenceLinkRejected],
	EvHypothesisRefuted:            decodeAs[HypothesisRefuted],
	EvHypothesisDimensionSpecified: decodeAs[HypothesisDimensionSpecified],
	EvObligationUpdated:            decodeAs[ObligationUpdated],
	EvHypothesisAdopted:            decodeAs[HypothesisAdopted],
	EvHypothesisMarkedInferior:     decodeAs[HypothesisMarkedInferior],
	EvLoopTerminated:               decodeAs[LoopTerminated],
	EvReportAssembled:              decodeAs[ReportAssembled],

	// 신 5종 (event_v2.go)
	EvPredicateAudited:          decodeAs[PredicateAudited],
	EvChainClaimAsserted:        decodeAs[ChainClaimAsserted],
	EvTemporalJudged:            decodeAs[TemporalJudged],
	EvAdoptionAudited:           decodeAs[AdoptionAudited],
	EvRequiredViewsExtended:     decodeAs[RequiredViewsExtended],
	EvRequiredViewNotApplicable: decodeAs[RequiredViewNotApplicable],

	// 신 Loop 2종 (event_loop_v2.go — §14-4 4b)
	EvPriorLowered:    decodeAs[PriorLowered],
	EvLoopStepDecided: decodeAs[LoopStepDecided],

	// 신 Loop 1종 (event_loop_v2.go — §14-4 4d widening 실행)
	EvWideningExecuted: decodeAs[WideningExecuted],
}

// KnownEventTypes는 디코더가 아는 타입 전부다(감사·시험용, 결정론 순서
// 아님 — 호출자가 필요하면 정렬한다).
func KnownEventTypes() []EventType {
	out := make([]EventType, 0, len(payloadDecoders))
	for t := range payloadDecoders {
		out = append(out, t)
	}
	return out
}

// ── journal 읽기 ────────────────────────────────────────────────

// DecodeEvents는 줄 단위 JSON 스트림을 복원한다.
//
// **마지막 줄만 잘림을 허용한다**: 크래시는 write 중간에서도 일어나므로
// 마지막 줄이 개행 없이 끊길 수 있다. 그 줄은 "Append가 반환하지 않은
// 이벤트"이므로 버려도 상태가 어긋나지 않는다(journal 쓰기 성공 후에만
// 상태를 바꾸는 Append 순서 계약 — journal.go). 중간 줄이 깨졌다면
// 그것은 잘림이 아니라 손상이므로 오류다.
func DecodeEvents(r io.Reader) (events []Event, truncatedTail bool, err error) {
	br := bufio.NewReader(r)
	for {
		line, rerr := br.ReadBytes('\n')
		if len(line) > 0 && line[len(line)-1] != '\n' {
			// 개행 없이 끝났다 = 부분 쓰기의 잔해.
			return events, true, nil
		}
		line = bytes.TrimRight(line, "\n")
		if len(bytes.TrimSpace(line)) > 0 {
			ev, derr := DecodeEvent(line)
			if derr != nil {
				return events, false, fmt.Errorf("journal %d번째 줄: %w", len(events)+1, derr)
			}
			events = append(events, ev)
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) {
				return events, false, nil
			}
			return events, false, rerr
		}
	}
}

// ReadJournal은 journal 파일 전체를 복원한다.
func ReadJournal(path string) ([]Event, bool, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, false, err
	}
	defer f.Close()
	return DecodeEvents(f)
}

// ── 재기동 복원 (§15.5 — 조립까지, 선언 주체는 §14-6) ────────────

// Recovery는 죽은 run 하나의 복원 결과다. 재기동 주체(§14-6)가 이것을
// 읽어 실패 선언을 낸다 — 이 패키지는 조립까지만 하고 파일 스캔·데몬화·
// 회수 정책은 지지 않는다.
type Recovery struct {
	State         RunState
	Events        []Event
	TruncatedTail bool // 마지막 줄이 부분 쓰기라 버려졌다
}

// RecoverRun은 run 디렉토리에서 상태 파일 + journal tail을 읽는다.
// 상태 파일이 없으면 오류(그 디렉토리는 아직 run이 아니다), journal이
// 없으면 이벤트 0으로 복원한다(상태 파일 생성 직후 죽은 경우).
func RecoverRun(runDir string) (Recovery, error) {
	st, err := ReadRunState(runDir)
	if err != nil {
		return Recovery{}, err
	}
	evs, trunc, err := ReadJournal(JournalPath(runDir))
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return Recovery{State: st}, err
	}
	return Recovery{State: st, Events: evs, TruncatedTail: trunc}, nil
}

// Ledger는 복원한 이벤트로 수첩을 재생한다. Replay가 Append와 같은
// 검증·적용 경로를 지나므로, 재생 결과는 죽기 직전 상태와 정의상 같다
// (버려진 부분 쓰기 한 줄은 Append가 반환하지 않은 이벤트다).
func (r Recovery) Ledger() (*Ledger, error) { return Replay(r.Events) }

// LastSeq는 journal이 실제로 담고 있는 마지막 seq다. 상태 파일의
// LastSeq는 이보다 작거나 같다 — journal 기록이 상태 갱신보다 앞서므로.
func (r Recovery) LastSeq() int {
	if len(r.Events) == 0 {
		return 0
	}
	return r.Events[len(r.Events)-1].Seq
}

// CrashDeclaration은 "재기동해 보니 terminal이 아니었다"의 실패 선언을
// 조립한다(§15.5 사상표 11행 — 재기동 시 판정 포함). 이미 terminal인
// run에는 부르지 않는다(호출자가 State.IsTerminal로 가른다).
func (r Recovery) CrashDeclaration() Declaration {
	return Declaration{
		RunID:  r.State.RunID,
		Stage:  r.State.Stage,
		Reason: ReasonCrash,
		Source: "restart_scan",
		Detail: "no_terminal_state",
		// crash는 재주행이 의미 있다(문구와 필드의 정합 — 6a 검증 D2).
		Retryable:      true,
		Observed:       fmt.Sprintf("last_seq=%d", r.LastSeq()),
		OperatorAction: OperatorActionFor(ReasonCrash),
		DeclaredAt:     time.Now().UTC(),
	}
}
