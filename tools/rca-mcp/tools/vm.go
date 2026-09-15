// VictoriaMetrics HTTP 접속 — 메트릭 시계열 원천(:18428).
// MetricsQL 특성: 이름 없는 selector({target_id="x"})에 rollup 함수
// 적용 가능 — discover_signals의 전 지표 일괄 집계가 이것에 의존한다.
package tools

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// VM은 VictoriaMetrics(vmsingle) 클라이언트다.
type VM struct {
	BaseURL string
	HTTP    *http.Client
}

// VMSample은 instant 벡터의 원소다.
type VMSample struct {
	Labels map[string]string
	Value  float64
	At     time.Time
}

// InstantQuery는 특정 시각 기준 벡터를 돌려준다.
func (v *VM) InstantQuery(ctx context.Context, query string, at time.Time) ([]VMSample, error) {
	q := url.Values{}
	q.Set("query", query)
	q.Set("time", strconv.FormatInt(at.Unix(), 10))
	raw, err := v.get(ctx, "/api/v1/query", q)
	if err != nil {
		return nil, err
	}
	var body struct {
		Status string `json:"status"`
		Data   struct {
			Result []struct {
				Metric map[string]string `json:"metric"`
				Value  [2]any            `json:"value"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("tools: VM 응답 디코드: %w", err)
	}
	var out []VMSample
	for _, r := range body.Data.Result {
		val, err := vmValue(r.Value[1])
		if err != nil {
			return nil, err
		}
		ts, _ := r.Value[0].(float64)
		out = append(out, VMSample{Labels: r.Metric, Value: val, At: time.Unix(int64(ts), 0).UTC()})
	}
	return out, nil
}

// LabelValues는 라벨 값 목록을 돌려준다(match selector·시간 범위 필터).
func (v *VM) LabelValues(ctx context.Context, label, match string, from, to time.Time) ([]string, error) {
	q := url.Values{}
	if match != "" {
		q.Set("match[]", match)
	}
	q.Set("start", strconv.FormatInt(from.Unix(), 10))
	q.Set("end", strconv.FormatInt(to.Unix(), 10))
	raw, err := v.get(ctx, "/api/v1/label/"+url.PathEscape(label)+"/values", q)
	if err != nil {
		return nil, err
	}
	var body struct {
		Data []string `json:"data"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("tools: VM 라벨 응답 디코드: %w", err)
	}
	return body.Data, nil
}

// VMSeries는 range 벡터의 한 시리즈다. Times/Values는 같은 길이 —
// 표본 있는 버킷만 온다(query_range는 빈 버킷을 채우지 않는다).
type VMSeries struct {
	Labels map[string]string
	Times  []time.Time
	Values []float64
}

// RangeQuery는 [start, end] 구간을 step 버킷으로 조회한다.
func (v *VM) RangeQuery(ctx context.Context, query string, start, end time.Time, step time.Duration) ([]VMSeries, error) {
	q := url.Values{}
	q.Set("query", query)
	q.Set("start", strconv.FormatInt(start.Unix(), 10))
	q.Set("end", strconv.FormatInt(end.Unix(), 10))
	q.Set("step", strconv.FormatInt(int64(step.Seconds()), 10)+"s")
	raw, err := v.get(ctx, "/api/v1/query_range", q)
	if err != nil {
		return nil, err
	}
	var body struct {
		Data struct {
			Result []struct {
				Metric map[string]string `json:"metric"`
				Values [][2]any          `json:"values"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("tools: VM range 응답 디코드: %w", err)
	}
	var out []VMSeries
	for _, r := range body.Data.Result {
		s := VMSeries{Labels: r.Metric}
		for _, p := range r.Values {
			val, err := vmValue(p[1])
			if err != nil {
				return nil, err
			}
			ts, _ := p[0].(float64)
			s.Times = append(s.Times, time.Unix(int64(ts), 0).UTC())
			s.Values = append(s.Values, val)
		}
		out = append(out, s)
	}
	return out, nil
}

// vmCapBytes — 응답 바이트 캡 가안(§15.2-2, §14-7 p99 역산 예정).
// VM은 JSON 전문 디코드가 필요해 중간 절단이 불가하므로 초과 = 실패
// (cap_exceeded — 조용한 결손이 아니라 명시적 backend_error)다.
const vmCapBytes = 8 << 20 // RCA_VM_CAP_BYTES

// get — 브레이커+bulkhead 가드(§15.2-3·7)를 두른 유일한 VM choke point.
// 반환 오류는 BackendError로 분류되며, HTTP 오류 본문 등 원문 문자열은
// error 사슬에만 남는다(§15.3-2).
func (v *VM) get(ctx context.Context, path string, q url.Values) ([]byte, error) {
	// HTTP 200 부분 응답 차단(§15.2-3 fail-closed) — 클러스터 일부 장애의
	// 반쪽 데이터가 observed_zero·반증으로 둔갑하는 경로를 원천 차단.
	q.Set("deny_partial_response", "1")
	return guardedCall(ctx, "vm", func() ([]byte, error) {
		hc := v.HTTP
		if hc == nil {
			// 호출당 시한(§15.2-1 3층의 바닥) — 가안, env는 §13.1 주입용.
			hc = &http.Client{Timeout: time.Duration(envInt("RCA_VM_HTTP_TIMEOUT_S", 30)) * time.Second}
		}
		req, err := http.NewRequestWithContext(ctx, "GET", v.BaseURL+path+"?"+q.Encode(), nil)
		if err != nil {
			return nil, &BackendError{Backend: "vm", Detail: "connect_failed", Err: err}
		}
		resp, err := hc.Do(req)
		if err != nil {
			return nil, &BackendError{Backend: "vm", Detail: classifyTransportErr(err), Err: err}
		}
		defer resp.Body.Close()
		cap := int64(envInt("RCA_VM_CAP_BYTES", vmCapBytes))
		raw, err := io.ReadAll(io.LimitReader(resp.Body, cap+1))
		if err != nil {
			return nil, &BackendError{Backend: "vm", Detail: "read_failed", Err: err}
		}
		if resp.StatusCode != http.StatusOK {
			return nil, &BackendError{Backend: "vm", Detail: classifyHTTPStatus(resp.StatusCode),
				Err: fmt.Errorf("HTTP %d: %.300s", resp.StatusCode, raw)}
		}
		if int64(len(raw)) > cap {
			return nil, &BackendError{Backend: "vm", Detail: "cap_exceeded",
				Err: fmt.Errorf("응답 > %d바이트", cap)}
		}
		return raw, nil
	})
}

func vmValue(v any) (float64, error) {
	s, ok := v.(string)
	if !ok {
		return 0, fmt.Errorf("tools: VM 값 형식 이상: %v", v)
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0, fmt.Errorf("tools: VM 값 파싱: %w", err)
	}
	return f, nil
}

// windowSeconds는 [from, to) 길이를 MetricsQL 구간 표기로 준다.
func windowSeconds(from, to time.Time) string {
	return strconv.FormatInt(int64(to.Sub(from).Seconds()), 10) + "s"
}
