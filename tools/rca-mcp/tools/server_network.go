// server·network 특화 도구 (도구 계약 §4).
//
//	get_processes  — 어느 프로세스가 리소스를 먹나 (VM sms.process.*,
//	                 차원 라벨은 name·pid — 실측)
//	get_snmp_traps — 장비가 직접 원인 신호를 보냈나 (CH snmp_traps_local)
package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"github.com/nkia-ai-team/rca-model-lab/tools/rca-mcp/llm"
)

// snmpTrapCap은 trap 그룹 반환 상한이다 — 조회는 +1로 해서 절단 경계를
// 구분한다(§5.1 계약 2).
const snmpTrapCap = 20

// sort_by → VM 지표 매핑(실측 sms.process.* 목록).
var processSortMetrics = map[string]string{
	"cpu":      "sms.process.cpu_utilization",
	"memory":   "sms.process.memory_rss_size",
	"io_read":  "sms.process.io_read_bytes",
	"io_write": "sms.process.io_write_bytes",
}

// NewProcessesTool은 get_processes 도구를 만든다.
func NewProcessesTool(vm *VM) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"host":    map[string]string{"type": "string", "description": "server 대상의 target_id(UUID)"},
			"from":    map[string]string{"type": "string", "description": "UTC RFC3339"},
			"to":      map[string]string{"type": "string", "description": "UTC RFC3339"},
			"sort_by": map[string]string{"type": "string", "description": "cpu | memory | io_read | io_write (기본 cpu)"},
			"top_n":   map[string]string{"type": "integer", "description": "상위 N(기본 5)"},
		},
		"required": []string{"host", "from", "to"},
	})
	return llm.Tool{
		Name:        "get_processes",
		Description: "호스트에서 리소스를 많이 쓰는 프로세스 상위 N(프로세스명·pid 차원).",
		Parameters:  params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var full struct {
				Host   string `json:"host"`
				From   string `json:"from"`
				To     string `json:"to"`
				SortBy string `json:"sort_by"`
				TopN   int    `json:"top_n"`
			}
			if err := json.Unmarshal(args, &full); err != nil {
				return nil, fmt.Errorf(`인자 오류: {"host": "<uuid>", "from": …, "to": …} 필요`)
			}
			if !uuidRe.MatchString(full.Host) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — server 대상의 순수 UUID 필요", full.Host)
			}
			from, e1 := time.Parse(time.RFC3339, full.From)
			to, e2 := time.Parse(time.RFC3339, full.To)
			if e1 != nil || e2 != nil || !from.Before(to) {
				return nil, fmt.Errorf("시간창 오류: from·to는 UTC RFC3339, from < to 필요")
			}
			if full.SortBy == "" {
				full.SortBy = "cpu"
			}
			metric, ok := processSortMetrics[full.SortBy]
			if !ok {
				keys := make([]string, 0, len(processSortMetrics))
				for k := range processSortMetrics {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				return nil, fmt.Errorf("sort_by %q 미지원 — 유효 값: %v", full.SortBy, keys)
			}
			if full.TopN <= 0 {
				full.TopN = 5
			}
			return queryProcesses(ctx, vm, full.Host, full.SortBy, metric, from, to, full.TopN)
		},
	}
}

func queryProcesses(ctx context.Context, vm *VM, host, sortBy, metric string, from, to time.Time, topN int) (any, error) {
	w := windowSeconds(from, to)
	expr := fmt.Sprintf(`sort_desc(avg by(name, pid)(avg_over_time(%s[%s])))`,
		selector(metric, host), w)
	samples, err := vm.InstantQuery(ctx, expr, to.Add(-time.Second))
	if err != nil {
		return nil, fmt.Errorf("get_processes 조회: %w", err)
	}
	if len(samples) == 0 {
		reason := NoDataUnknown
		summary := "창 내 프로세스 지표 관측 0건 — 프로세스별 배제 근거로 해석하지 말고 수집 상태를 확인하라."
		// A successful empty query is not enough to claim zero process
		// observations: distinguish an entirely uncollected target from a
		// target whose other metrics exist but process metrics do not.
		if names, e := vm.LabelValues(ctx, "__name__", fmt.Sprintf(`{target_id=%q}`, host), from, to); e == nil {
			if len(names) == 0 {
				reason = NoDataNotCollected
				summary = "대상에 창 내 메트릭 시계열이 없음 — SMS 수집 대상이 아니거나 수집기 결손이다. get_data_coverage로 확인하라."
			} else {
				found := false
				for _, n := range names {
					if n == metric {
						found = true
						break
					}
				}
				if !found {
					reason = NoDataMetricUnregistered
					summary = fmt.Sprintf("대상에 다른 메트릭 %d개는 있으나 프로세스 지표 %q가 등록되지 않았다.", len(names), metric)
				}
			}
		}
		return Envelope{
			Status:       "no_data",
			NoDataReason: reason,
			Summary:      summary,
		}, nil
	}

	var total float64
	for _, s := range samples {
		total += s.Value
	}
	truncated := len(samples) > topN
	shown := samples
	if truncated {
		shown = samples[:topN]
	}
	var findings []Finding
	var refs []string
	for _, s := range shown {
		name, pid := s.Labels["name"], s.Labels["pid"]
		ref := fmt.Sprintf("vm:%s{target_id=%s,name=%s,pid=%s}:%s/%s", metric, host, name, pid,
			from.UTC().Format(time.RFC3339), to.UTC().Format(time.RFC3339))
		refs = append(refs, ref)
		share := 0.0
		if total > 0 {
			share = s.Value / total
		}
		findings = append(findings, Finding{
			"process": name, "pid": pid, "avg": s.Value, "share": share,
			"refs": []string{ref},
		})
	}
	topShare, _ := findings[0]["share"].(float64)
	status := "normal"
	if topShare >= 0.5 {
		status = "anomalous"
	}
	return Envelope{
		Status: status,
		Summary: fmt.Sprintf("%s 기준 프로세스 상위 %d개(전체 %d개). 최대 %s(pid %s) 점유 %.0f%%.",
			sortBy, len(shown), len(samples), findings[0]["process"], findings[0]["pid"], topShare*100),
		AssessmentBasis: "최대 프로세스 점유율 50% 이상 = 지배 소비자 존재(임시 판정식)",
		Findings:        findings,
		Refs:            refs,
		Truncated:       truncated,
		ObservedRange:   &TimeRange{From: from.UTC(), To: to.UTC()},
		// top-N 밖 프로세스 수 — 개체 축 절단(§5.1 계약 2).
		Scopes: []QueryScope{qscope("process", len(samples), len(shown))},
	}, nil
}

// NewSnmpTrapsTool은 get_snmp_traps 도구를 만든다.
func NewSnmpTrapsTool(ch *CH) llm.Tool {
	params, _ := json.Marshal(map[string]any{
		"type": "object",
		"properties": map[string]any{
			"device": map[string]string{"type": "string", "description": "network 대상의 target_id(UUID)"},
			"from":   map[string]string{"type": "string", "description": "UTC RFC3339"},
			"to":     map[string]string{"type": "string", "description": "UTC RFC3339"},
		},
		"required": []string{"device", "from", "to"},
	})
	return llm.Tool{
		Name:        "get_snmp_traps",
		Description: "네트워크 장비가 보낸 SNMP trap(linkDown 등 직접 원인 신호)을 조회한다.",
		Parameters:  params,
		Call: func(ctx context.Context, args json.RawMessage) (any, error) {
			var in struct{ Device, From, To string }
			if err := json.Unmarshal(args, &in); err != nil {
				return nil, fmt.Errorf(`인자 오류: {"device": "<uuid>", "from": …, "to": …} 필요`)
			}
			if !uuidRe.MatchString(in.Device) {
				return nil, fmt.Errorf("%q는 target_id가 아님 — network 대상의 순수 UUID 필요", in.Device)
			}
			from, e1 := time.Parse(time.RFC3339, in.From)
			to, e2 := time.Parse(time.RFC3339, in.To)
			if e1 != nil || e2 != nil || !from.Before(to) {
				return nil, fmt.Errorf("시간창 오류: from·to는 UTC RFC3339, from < to 필요")
			}
			return querySnmpTraps(ctx, ch, in.Device, from, to)
		},
	}
}

func querySnmpTraps(ctx context.Context, ch *CH, device string, from, to time.Time) (any, error) {
	var acc truncAcc
	rows, tr, err := ch.Query(ctx, `
		SELECT trap_type, trap_oid, count() AS n,
		       min(received_at) AS first_at, max(received_at) AS last_at,
		       anyLast(toString(varbinds)) AS sample_varbinds
		FROM snmp_traps_local
		WHERE (target_id = {device:String} OR host_target_id = {device:String})
		  AND received_at >= parseDateTime64BestEffort({from:String}, 3)
		  AND received_at <  parseDateTime64BestEffort({to:String}, 3)
		GROUP BY trap_type, trap_oid ORDER BY n DESC LIMIT 21`,
		map[string]string{"device": device, "from": chTime(from), "to": chTime(to)})
	if err != nil {
		return nil, fmt.Errorf("get_snmp_traps 조회: %w", err)
	}
	acc.note(tr)
	if len(rows) == 0 {
		return Envelope{
			Status:          "normal",
			Summary:         "창 내 trap 없음 — 장비가 직접 원인 신호를 보내지 않았다는 관측(trap은 이벤트성이라 0건=미발생).",
			AssessmentBasis: "trap 0건 = 미발생(이벤트성 데이터)",
			QueryTruncated:  bool(acc),
			// 완전 조회 0건 — observed_zero의 원천(§5.1 계약 1).
			Scopes: []QueryScope{qscope("trap", 0, 0)},
		}, nil
	}
	// LIMIT+1 조회로 경계를 구분한다(§5.1 계약 2 — 종전 LIMIT 20은 결과가
	// 정확히 20인지 초과인지 구분 불능이라 절단 사실 자체가 미상이었다).
	// 21번째 행은 "더 있다"의 신호일 뿐이므로 반환하지 않는다.
	snmpTruncated := len(rows) > snmpTrapCap
	if snmpTruncated {
		rows = rows[:snmpTrapCap]
	}
	var findings []Finding
	var refs []string
	total := 0
	for _, r := range rows {
		n := asInt(r["n"])
		total += n
		ref := fmt.Sprintf("ch:snmp_traps_local:%s:%v", device, r["trap_oid"])
		refs = append(refs, ref)
		findings = append(findings, Finding{
			"trap_type": r["trap_type"], "trap_oid": r["trap_oid"], "count": n,
			"first_at": r["first_at"], "last_at": r["last_at"],
			"sample_varbinds": r["sample_varbinds"],
			"refs":            []string{ref},
		})
	}
	return Envelope{
		Status:          "anomalous",
		Summary:         fmt.Sprintf("trap %d건(%d종) — 종류·OID·시각은 findings 참조.", total, len(rows)),
		AssessmentBasis: "trap 존재 자체가 장비 발신 신호",
		Findings:        findings,
		Refs:            refs,
		Truncated:       snmpTruncated,
		QueryTruncated:  bool(acc),
		// 절단 시 총량은 "21 이상"까지만 알 수 있다(LIMIT+1의 한계) —
		// omitted는 하한이며, 0이 아니라는 사실이 absent 자격을 막는 값이다.
		Scopes: []QueryScope{qscope("trap", len(rows)+boolInt(snmpTruncated), len(rows))},
	}, nil
}
