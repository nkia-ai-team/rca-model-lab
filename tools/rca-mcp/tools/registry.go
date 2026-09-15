// 도구 조립 — 과도기 표면(신 11 + 구 4)을 저장소 클라이언트에 연결해
// 조사자에게 노출할 목록을 만든다. 단계별 배치(계약 §6)는 호출자 몫:
// Triage·[2]의 typed 자리는 cmd/rca가 자체 SQL(TargetMetaFunc)·
// NewChangesFunc로 채우고(이 파일과 무관 — §9 검토 실측), Examiner·
// Investigator는 이 목록을 llm.ChatTools에 넘긴다.
package tools

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// Stores는 세 저장소 접속 묶음이다.
type Stores struct {
	PG *sql.DB
	CH *CH
	VM *VM
}

// withEnvelopeRef는 refs 없는 봉투에 조회면 ref를 보증하는 래퍼다.
// 봉투의 모든 관측은 인용 가능해야 한다 — no_data("조회했으나 없음")
// 뿐 아니라 "trap 0건 = 미발생", 지표 인벤토리 요약 같은 normal 관측도
// 조사자의 근거가 되며, 인용할 ref가 없으면 모델이 ref를 지어내고
// 가드에 막힌다(2026-07-20 재생 주행 실측 — discover_signals normal
// 봉투). ref 형식은 임시(계약 §7 열린 결정): tool:<이름>:<인자>.
func withEnvelopeRef(t llm.Tool) llm.Tool {
	call, name := t.Call, t.Name
	t.Call = func(ctx context.Context, args json.RawMessage) (any, error) {
		out, err := call(ctx, args)
		if env, ok := out.(Envelope); ok && len(env.Refs) == 0 {
			var buf bytes.Buffer
			if json.Compact(&buf, args) != nil {
				buf.Reset()
				buf.Write(args)
			}
			env.Refs = []string{fmt.Sprintf("tool:%s:%s", name, buf.String())}
			return env, nil
		}
		return out, err
	}
	return t
}

// Toolset은 전체 도구 목록을 만든다. incidentID 바인딩은 없다 —
// 마지막 소비자였던 get_topology가 expand_topology로 교체되면서
// 박제(incidents.topology)를 읽는 도구가 사라졌다(§12.9). 박제는
// seed 브리핑이 "증상 시점의 그림"으로 계속 싣는다. firstEvent는
// seed의 첫 증상 시각 — 신 표면 도구(scan_metrics·read_timeseries)의
// 기준선 자동 창 [firstEvent-60m, firstEvent) 기준점(재설계 §5.3).
//
// 과도기 표면(2026-07-27 합의): 신 도구가 역할을 대체하는 구 도구는
// 병존시키지 않고 뺀다 — 익숙한 도구로의 선택 편중(F08-H metric_series
// 16회 실측)이 신 도구 실측을 무산시키는 것을 막기 위해서다.
// scan_metrics ← discover_signals, read_timeseries ← get_metric_series
// + get_metric_dimensions(차원 분해는 group_by 파라미터가 흡수, §3.3 Q7).
// db_blocking ← get_db_sessions — 세션 볼륨·대기 분포는 승계하지 않는다
// (옴니버스 금지, read_timeseries의 몫 — §13.13).
// db_slow_queries ← get_slow_queries — 플랜 전문·튜닝 권고는 승계하지
// 않는다(플랜은 존재·안정성 요약 + 드릴다운만 — §14.7).
// lastEvent는 인시던트 창 끝 — list_changes 기본 창 [firstEvent-24h,
// lastEvent]의 재료다(§10.6). zero면 도구가 now로 폴백하고 밝힌다.
func Toolset(s Stores, firstEvent, lastEvent time.Time) []llm.Tool {
	all := []llm.Tool{
		NewDescribeDataSourcesTool(),
		NewSearchTargetsTool(s.PG),
		NewProcessSnapshotTool(s.CH),
		NewTraceSummaryTool(s.CH),
		NewTraceSpansTool(s.CH),
		NewTraceActivityTool(s.CH),
		NewHostObservationsTool(s.CH),
		NewClusterProjectionTool(s.CH),
		// 신 표면(spec-tool-redesign.md §3) 구현분 11종.
		NewScanMetricsTool(s.VM, s.PG, firstEvent),
		NewReadTimeseriesTool(s.VM, s.PG, firstEvent, nil),
		NewSampleLogsTool(s.CH, firstEvent),
		NewListEventsTool(s.CH, s.PG, nil),
		// describe_target ← get_target_meta 자리 교체(§9 — 정체·소속·
		// 한도 카탈로그 상세. 일괄 스크리닝 몫은 Examiner 브리핑이 흡수).
		NewDescribeTargetTool(s.PG, s.VM, firstEvent),
		// list_changes ← get_changes 자리 교체(§10 — 창 전역 반환·사건
		// 단위 교차 접기. 대상은 필터가 아니라 정렬 힌트).
		NewListChangesTool(s.PG, firstEvent, lastEvent, nil),
		// compare_peers ← get_cohort 자리 교체(§11 — 또래 명단이 본체.
		// 판정은 각자 자기 기준선 대비 이탈, 시계열·선후는 read_timeseries 몫).
		NewComparePeersTool(s.PG, s.VM, firstEvent, nil),
		// expand_topology ← get_topology + get_runtime_connections 자리
		// 교체(§12 — 박제가 아니라 원천에서 즉석 조립. 계측 밖 의존은
		// 소켓이 아니라 호출자 CLIENT span이 지목한다: 소켓 원천은 앱
		// 프로세스 0건이라 "봤는데 없더라"는 거짓 안심만 줬다).
		NewExpandTopologyTool(s.PG, s.CH, firstEvent, lastEvent, nil),
		// db_blocking ← get_db_sessions 자리 교체(§13 — 창 전역 스캔·사건
		// 단위 접기. 교체된 도구는 마지막 스냅샷 10초만 봐서 희박 사건을
		// 구조적으로 놓쳤다. 세션 볼륨은 read_timeseries 몫).
		NewDBBlockingTool(s.CH, firstEvent, lastEvent, nil),
		// db_slow_queries ← get_slow_queries 자리 교체(§14 — 창 전역 2단
		// 접기·단위 정규화·직전 24시간 자기 기준선 대비 3배 판정. 교체된
		// 도구는 마지막 폴 10초만 보고 Oracle μs를 ms 필드에 담았다.
		// 순위는 워크로드 상수라 판정과 분리했다).
		NewDBSlowQueriesTool(s.CH, s.PG, firstEvent, lastEvent, nil),
		// breakdown_endpoints ← get_trace_breakdown + get_slow_endpoints
		// 자리 교체(§15 — 두 도구를 하나가 승계한 두 번째 사례).
		// 교체된 둘은 SERVER span만 봐서 메시지 구동 4개 서비스를
		// /actuator/health 한 줄로 보고했고, status_code='ERROR'만 세어
		// 404 84,386건을 0으로 셌으며, 기준선이 없어 만성과 사건을
		// 구분하지 못했다. SERVER 대비 비율은 스케줄러 트레이스가
		// 분모 밖이라 성립하지 않는다 — child_share가 그 자리를 대신한다.
		NewBreakdownEndpointsTool(s.CH, firstEvent, lastEvent),
		// 메타 — 관측 증명.
		NewDataCoverageTool(s.PG, s.CH, s.VM),
		// 도메인 특화 3종(APM 2종은 breakdown_endpoints가 승계).
		NewProcessesTool(s.VM),
		NewSnmpTrapsTool(s.CH),
		NewK8sStateTool(s.VM, s.PG),
	}
	for i := range all {
		all[i] = withEnvelopeRef(withBackendGuard(all[i]))
	}
	return all
}

// withBackendGuard는 백엔드 실패(BackendError)를 no_data(backend_error)
// 봉투로 사상한다(§15.2-3, 6b) — [3] 부분 실패 ≠ run 실패: 관점 하나의
// 결손은 봉투로 기록되고 run은 계속된다. ctx 취소는 봉투가 아니라 오류
// 그대로다(run이 죽는 것이지 저장소가 죽은 것이 아니다 — §15.2-5).
//
// 봉투에는 분류 토큰만 실린다. 원문 오류(접속 정보가 섞일 수 있다)는
// 반환 전에 stderr가 아니라 error 사슬을 타고 trace에만 남았다(§15.3-2).
func withBackendGuard(t llm.Tool) llm.Tool {
	call := t.Call
	t.Call = func(ctx context.Context, args json.RawMessage) (any, error) {
		out, err := call(ctx, args)
		var be *BackendError
		if err != nil && errors.As(err, &be) && ctx.Err() == nil {
			return Envelope{
				Status: "no_data", NoDataReason: NoDataBackendError,
				Backend: be.Backend, BackendDetail: be.Detail,
				Summary: fmt.Sprintf("관측 백엔드 %s 실패(%s) — 이 관점은 결손이며 배제 근거가 아니다(§5.5)",
					be.Backend, be.Detail),
			}, nil
		}
		return out, err
	}
	return t
}
