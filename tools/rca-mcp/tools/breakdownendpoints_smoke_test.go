// 실 CH 스모크 — RCA_CH_URL 없으면 skip. 단위 가드가 못 보는 것을 본다:
// SQL이 실제로 파싱되는지, 세 구획이 실물에서 채워지는지, 만성 대상이
// 조용한지.
package tools

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func TestBreakdownEndpointsSmoke(t *testing.T) {
	ch := chFromEnv(t)

	// 최근 창에서 트래픽 최다 서비스를 발굴한다(고정 이름에 의존하지 않는다).
	rows, _, err := ch.Query(context.Background(), `
		SELECT service_name, count() AS n FROM otel_traces_local
		WHERE timestamp > now() - INTERVAL 60 MINUTE AND span_kind = 'SERVER'
		GROUP BY 1 ORDER BY n DESC LIMIT 1`, nil)
	if err != nil || len(rows) == 0 {
		t.Skipf("실 서비스 발굴 실패(창에 트래픽 없음): %v", err)
	}
	svc := rows[0]["service_name"].(string)

	now := time.Now().UTC()
	from, to := now.Add(-30*time.Minute), now
	tool := NewBreakdownEndpointsTool(ch, time.Time{}, time.Time{})
	args, _ := json.Marshal(map[string]string{
		"target": svc, "from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339)})
	out, err := tool.Call(context.Background(), args)
	if err != nil {
		t.Fatalf("호출 실패(SQL 파싱 포함): %v", err)
	}
	env := out.(Envelope)
	if _, err := json.Marshal(env); err != nil {
		t.Fatalf("봉투 직렬화 실패: %v", err)
	}
	if env.Status == "no_data" {
		t.Skipf("창에 관측 없음: %s", env.Summary)
	}

	var cov Finding
	sections := map[string]int{}
	for _, f := range env.Findings {
		if f["observation"] == "coverage" {
			cov = f
			continue
		}
		sections[f["section"].(string)]++
		// 계약 가드 — 모든 행이 자기를 지지하는 ref를 든다.
		if refs, ok := f["refs"].([]string); !ok || len(refs) == 0 {
			t.Fatalf("행에 refs가 없다: %+v", f)
		}
		if _, ok := f["axis"]; !ok {
			t.Fatalf("행이 축을 밝히지 않는다: %+v", f)
		}
	}
	if cov == nil {
		t.Fatal("coverage 행이 없다")
	}
	if sections["entry"] == 0 {
		t.Fatalf("SERVER 트래픽이 있는 서비스인데 entry 구획이 비었다: %v", sections)
	}
	lim, _ := cov["limits"].([]string)
	if !strings.Contains(strings.Join(lim, "\n"), "보존은 3일") {
		t.Fatal("TTL 한계는 항상 실려야 한다")
	}

	// 기준선이 유효하면 판정 근거 문장이 기준선 창을 밝혀야 한다.
	if bw, ok := cov["baseline_window"].(map[string]any); ok && bw["state"] == "ok" {
		if !strings.Contains(env.AssessmentBasis, "기준선은") {
			t.Fatalf("assessment_basis가 기준선을 밝히지 않는다: %s", env.AssessmentBasis)
		}
	}
}

// 만성 대상 회귀 스모크 — 거절율이 **안정적인** 행은 그 절대값이
// 40~50%든 rejection_shift로 발화하면 안 된다. §15.4가 통과하지
// 않아야 한다고 명시한 자리이며, 구현 중 실제로 발화했던 결함이다
// (기준선 행에 실패/거절 파생을 돌리지 않아 base.rejRate가 0으로 남았다).
//
// 불변식으로 쓰는 이유: "만성 서비스는 조용하다"로 쓰면 그 서비스에
// 실사건이 나는 순간 테스트가 깨진다 — 첫 주행에서 실제로 그랬다
// (commerce-gateway 409가 10.2%→47.1%로 뛰던 창을 잡았다. 그것은
// 오탐이 아니라 검출이다). 안정성을 조건으로 걸어야 결함만 잡는다.
func TestBreakdownEndpointsChronicRejectionQuiet(t *testing.T) {
	ch := chFromEnv(t)

	rows, _, err := ch.Query(context.Background(), `
		SELECT service_name, count() AS n,
		       countIf(toUInt16OrZero(span_attributes['http.response.status_code']) >= 400
		               AND toUInt16OrZero(span_attributes['http.response.status_code']) < 500) AS r4
		FROM otel_traces_local
		WHERE timestamp > now() - INTERVAL 90 MINUTE AND span_kind = 'SERVER'
		GROUP BY 1 HAVING n >= 300 AND r4/n > 0.2
		ORDER BY n DESC LIMIT 1`, nil)
	if err != nil || len(rows) == 0 {
		t.Skip("만성 4xx 서비스를 창에서 찾지 못함")
	}
	svc := rows[0]["service_name"].(string)

	now := time.Now().UTC()
	tool := NewBreakdownEndpointsTool(ch, time.Time{}, time.Time{})
	args, _ := json.Marshal(map[string]string{"target": svc,
		"from": now.Add(-30 * time.Minute).Format(time.RFC3339),
		"to":   now.Format(time.RFC3339)})
	out, err := tool.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	checked := 0
	for _, f := range out.(Envelope).Findings {
		cur, ok1 := f["rejected_rate"].(float64)
		base, ok2 := f["baseline"].(map[string]any)
		if !ok1 || !ok2 || base["state"] != "ok" {
			continue
		}
		prev, ok3 := base["rejected_rate"].(float64)
		if !ok3 || cur-prev >= 0.05 { // 변화가 있는 행은 발화가 정답이다
			continue
		}
		checked++
		arms, _ := f["verdict_arms"].([]string)
		for _, a := range arms {
			if a == "rejection_shift" {
				t.Fatalf("거절율이 안정적인 행(%v: %.3f → %.3f)이 rejection_shift로 발화했다 "+
					"— 기준선 파생 누락 회귀: %v", f["label"], prev, cur, f["baseline"])
			}
		}
	}
	if checked == 0 {
		t.Skip("거절율이 안정적인 행이 창에 없음")
	}
}
