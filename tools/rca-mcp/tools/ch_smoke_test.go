// 실 CH 스모크 — RCA_CH_URL 없으면 skip.
// 실행: RCA_CH_URL='http://lucida:lucida123@192.168.230.119:18123' \
//       go test ./tools -run Smoke -v
package tools

import (
	"context"
	"encoding/json"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"
)

// chFromEnv는 RCA_CH_URL(유저·비밀번호 포함 URL)로 CH 클라이언트를 만든다.
func chFromEnv(t *testing.T) *CH {
	raw := os.Getenv("RCA_CH_URL")
	if raw == "" {
		t.Skip("RCA_CH_URL 미설정 — 실 CH 스모크 생략")
	}
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatalf("RCA_CH_URL 파싱: %v", err)
	}
	pass, _ := u.User.Password()
	return &CH{
		BaseURL:  u.Scheme + "://" + u.Host,
		User:     u.User.Username(),
		Pass:     pass,
		Database: "lucida",
	}
}

func TestEventsLogsSmoke(t *testing.T) {
	ch := chFromEnv(t)

	// 최근 이벤트가 실존하는 대상을 골라 그 시각 주변 창으로 조회.
	rows, _, err := ch.Query(context.Background(), `
		SELECT target_id, max(occurred_at) AS at FROM lucida_events_local
		WHERE target_id != '' GROUP BY target_id ORDER BY at DESC LIMIT 1`, nil)
	if err != nil || len(rows) == 0 {
		t.Fatalf("실 이벤트 대상 조회: %v (%d행)", err, len(rows))
	}
	target := rows[0]["target_id"].(string)
	at, err := time.Parse("2006-01-02 15:04:05.999999999", rows[0]["at"].(string))
	if err != nil {
		t.Fatalf("이벤트 시각 파싱: %v", err)
	}
	from, to := at.Add(-time.Hour), at.Add(time.Minute)

	evTool := NewListEventsTool(ch, nil, nil) // PG 없음 — kcm 합성은 생략 경로
	args, _ := json.Marshal(map[string]string{
		"target": target, "from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339)})
	out, err := evTool.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env := out.(Envelope)
	if len(env.Findings) == 0 || len(env.Refs) == 0 {
		t.Fatalf("이벤트 봉투 이상: %+v", env)
	}
	if !strings.HasPrefix(env.Refs[0], "ch:lucida_events_local:") {
		t.Fatalf("이벤트 ref 포맷 이상: %v", env.Refs)
	}
	t.Logf("events: status=%s %s", env.Status, env.Summary)

	// 빈 창 → 이벤트 없음은 정상 관측.
	empty, _ := json.Marshal(map[string]string{
		"target": target, "from": "2020-01-01T00:00:00Z", "to": "2020-01-02T00:00:00Z"})
	out, err = evTool.Call(context.Background(), empty)
	if err != nil {
		t.Fatal(err)
	}
	if env := out.(Envelope); env.Status != "normal" || !strings.Contains(env.Summary, "사건 0건") {
		t.Fatalf("빈 창 이벤트 봉투 이상: %+v", env)
	}

	// 로그: 로그가 실존하는 대상으로.
	rows, _, err = ch.Query(context.Background(), `
		SELECT target_id, max(timestamp) AS at FROM lucida_logs_local
		WHERE target_id != '' GROUP BY target_id ORDER BY at DESC LIMIT 1`, nil)
	if err != nil || len(rows) == 0 {
		t.Fatalf("실 로그 대상 조회: %v", err)
	}
	ltarget := rows[0]["target_id"].(string)
	lat, _ := time.Parse("2006-01-02 15:04:05.999999999", rows[0]["at"].(string))

	// sample_logs map — 4구획 지도 + 요약 메타 + template_id.
	logTool := NewSampleLogsTool(ch, lat)
	args, _ = json.Marshal(map[string]string{
		"mode":   "map",
		"target": ltarget, "from": lat.Add(-10 * time.Minute).Format(time.RFC3339),
		"to": lat.Add(time.Minute).Format(time.RFC3339)})
	out, err = logTool.Call(context.Background(), args)
	if err != nil {
		t.Fatal(err)
	}
	env = out.(Envelope)
	if len(env.Findings) == 0 {
		t.Fatalf("map 봉투에 findings 없음: %+v", env)
	}
	var tid string
	var sawMeta bool
	for _, f := range env.Findings {
		if f["section"] == "summary_meta" {
			sawMeta = true
			if _, ok := f["level_distribution"]; !ok {
				t.Fatalf("요약 메타에 레벨 분포 없음: %+v", f)
			}
		} else if tid == "" {
			tid, _ = f["template_id"].(string)
		}
	}
	if !sawMeta {
		t.Fatalf("summary_meta finding 없음: %+v", env.Findings)
	}
	t.Logf("map: status=%s %s", env.Status, env.Summary)

	// grep — map의 template_id를 그대로 드릴다운(깔때기 배선 검증).
	if tid != "" {
		args, _ = json.Marshal(map[string]any{
			"mode": "grep", "template_id": tid, "context_lines": 1,
			"target": ltarget, "from": lat.Add(-10 * time.Minute).Format(time.RFC3339),
			"to": lat.Add(time.Minute).Format(time.RFC3339)})
		out, err = logTool.Call(context.Background(), args)
		if err != nil {
			t.Fatal(err)
		}
		genv := out.(Envelope)
		if len(genv.Findings) == 0 || genv.Findings[0]["relation"] != "match" {
			t.Fatalf("grep 매치 없음(map template_id 왕복 실패): %+v", genv)
		}
		t.Logf("grep: %s", genv.Summary)
	}

	// 빈 창 map → no_data + 사유 unknown + coverage 안내.
	var emptyMap map[string]any
	_ = json.Unmarshal(empty, &emptyMap)
	emptyMap["mode"] = "map"
	emptyArgs, _ := json.Marshal(emptyMap)
	out, err = logTool.Call(context.Background(), emptyArgs)
	if err != nil {
		t.Fatal(err)
	}
	if env := out.(Envelope); env.Status != "no_data" || env.NoDataReason != NoDataUnknown ||
		!strings.Contains(env.Summary, "get_data_coverage") {
		t.Fatalf("빈 창 map 봉투 이상: %+v", env)
	}
}
