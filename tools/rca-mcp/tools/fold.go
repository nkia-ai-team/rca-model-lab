// grade 접기 — scan_metrics·read_timeseries 공유 1단 접기(§5.6).
// grade는 vmalert 룰 매칭용 알람 등급별 의도 복제라 값이 같아야 하고,
// 같은 (그룹,버킷)의 값 불일치는 가정 위반으로 정확 계수한다.
// PromQL 집계로 접지 않는 이유(§5.6.1-1): max without(grade)가 __name__을
// 버려 라벨셋이 같은 다른 지표를 합쳐버린다 — 접기는 코드가 한다.
package tools

import (
	"math"
	"sort"
	"strings"
	"time"
)

// foldedSeries는 grade만 접힌 시리즈다 — 실차원 라벨은 남아 있다.
type foldedSeries struct {
	Name    string
	Labels  map[string]string // grade·__name__ 제외
	Buckets map[int64]float64 // unix초 → 값(grade 간 max)
}

// foldGrade는 raw 시리즈들을 grade 제외 라벨셋 그룹으로 접는다(버킷별
// max). 반환 mismatch = 같은 (그룹,버킷)에서 값이 갈린 횟수.
func foldGrade(series []VMSeries) (folded []*foldedSeries, mismatch int) {
	byKey := map[string]*foldedSeries{}
	var keys []string
	for _, s := range series {
		name := s.Labels["__name__"]
		if name == "" {
			continue
		}
		labelKeys := make([]string, 0, len(s.Labels))
		for k := range s.Labels {
			if k != "grade" && k != "__name__" {
				labelKeys = append(labelKeys, k)
			}
		}
		sort.Strings(labelKeys)
		var sb strings.Builder
		sb.WriteString(name)
		labels := make(map[string]string, len(labelKeys))
		for _, k := range labelKeys {
			sb.WriteByte(0)
			sb.WriteString(k)
			sb.WriteByte('=')
			sb.WriteString(s.Labels[k])
			labels[k] = s.Labels[k]
		}
		key := sb.String()
		g := byKey[key]
		if g == nil {
			g = &foldedSeries{Name: name, Labels: labels, Buckets: map[int64]float64{}}
			byKey[key] = g
			keys = append(keys, key)
		}
		for i, ts := range s.Times {
			u, v := ts.Unix(), s.Values[i]
			if prev, ok := g.Buckets[u]; ok {
				if math.Abs(prev-v) > 1e-9*math.Max(math.Abs(prev), math.Abs(v)) {
					mismatch++
				}
				if v > prev {
					g.Buckets[u] = v
				}
			} else {
				g.Buckets[u] = v
			}
		}
	}
	sort.Strings(keys) // 결정론적 출력 순서
	for _, k := range keys {
		folded = append(folded, byKey[k])
	}
	return folded, mismatch
}

// bucketAvg는 접힌 그룹들을 버킷별 avg로 합쳐 시간 정렬 시계열 하나로
// 만든다(§5.6 2단 접기 — 실차원 집계).
func bucketAvg(groups []*foldedSeries) (times []time.Time, values []float64) {
	sums := map[int64]float64{}
	counts := map[int64]int{}
	for _, g := range groups {
		for u, v := range g.Buckets {
			sums[u] += v
			counts[u]++
		}
	}
	if len(sums) == 0 {
		return nil, nil
	}
	ts := make([]int64, 0, len(sums))
	for u := range sums {
		ts = append(ts, u)
	}
	sort.Slice(ts, func(i, j int) bool { return ts[i] < ts[j] })
	for _, u := range ts {
		times = append(times, time.Unix(u, 0).UTC())
		values = append(values, sums[u]/float64(counts[u]))
	}
	return times, values
}
