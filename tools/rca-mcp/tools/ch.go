// ClickHouse HTTP 접속 — 로그·이벤트·트레이스·DPM·netflow 원천.
// 파라미터는 CH 서버측 바인딩({name:Type} + param_*)만 쓴다 — 문자열
// 조립 금지(주입 방지 + 쿼리 재현성).
package tools

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// CH는 ClickHouse HTTP 인터페이스 클라이언트다. 예:
// CH{BaseURL: "http://192.168.230.119:18123", User: "lucida", Pass: "lucida123", Database: "lucida"}
type CH struct {
	BaseURL  string
	User     string
	Pass     string
	Database string
	HTTP     *http.Client
}

// Query는 SQL을 실행해 JSONEachRow 행 목록을 돌려준다. params는
// 서버측 바인딩({k:String} 등)의 param_k 값이다.
// CH 응답 누적 캡 가안(§15.2-2, §14-7 p99 역산 예정). limited reader를
// CH에 걸면 JSONEachRow가 줄 중간에서 잘려 디코드 실패가 되므로, 집행은
// scanner 루프 안의 행·바이트 카운터다(스펙 ② 원천별 처방). 초과 =
// 실패가 아니라 Truncation 절단 계약이다(§14-7 이관 완료 — 6b의
// cap_exceeded 실패 골격을 대체): 모은 행을 그대로 반환하고 절단
// 표식(*CHTrunc)을 시그니처로 나른다. 호출부는 표식을 봉투 Truncated로
// 올려야 한다 — 절단된 관측은 §5.5 반증 자격·observed_zero에서 기계가
// 걸러낸다(verdict ④·delta complete).
const (
	chCapRows  = 200_000  // RCA_CH_CAP_ROWS
	chCapBytes = 16 << 20 // RCA_CH_CAP_BYTES
)

// CHTrunc는 캡 도달 절단 표식이다. nil이면 완전 조회. Total(절단 전
// 총량)은 다 읽지 않았으므로 알 수 없다 — 추정치를 싣지 않는다.
type CHTrunc struct {
	Rows      int   // 반환한 행 수
	BytesRead int64 // 절단 시점까지 읽은 바이트
	ByRows    bool  // true=행 캡, false=바이트 캡
}

// chOut — guardedCall[T] 단일 반환 제약을 넘기 위한 내부 묶음.
type chOut struct {
	rows  []map[string]any
	trunc *CHTrunc
}

// truncAcc는 한 도구 호출 안의 CH 절단 표식 누적기다. 헬퍼 사슬을
// 관통시켜 마지막에 봉투 Truncated로 올린다 — 표식을 조용히 버리는
// 호출부가 계약 위반이다.
type truncAcc bool

func (t *truncAcc) note(tr *CHTrunc) {
	if tr != nil {
		*t = true
	}
}

// Query — 브레이커+bulkhead 가드(§15.2-3·7)를 두른 유일한 CH choke
// point. 반환 오류는 BackendError로 분류되고 원문 오류 본문은 error
// 사슬에만 남는다(§15.3-2).
func (c *CH) Query(ctx context.Context, sql string, params map[string]string) ([]map[string]any, *CHTrunc, error) {
	q := url.Values{}
	q.Set("database", c.Database)
	q.Set("default_format", "JSONEachRow")
	// UInt64를 문자열로 감싸는 기본값 해제 — 숫자는 숫자로 받는다.
	q.Set("output_format_json_quote_64bit_integers", "0")
	// HTTP 200 부분 응답 차단(§15.2-3 fail-closed) — 샤드 일부 장애의
	// 반쪽 0건이 배제 근거로 둔갑하는 경로 차단.
	q.Set("skip_unavailable_shards", "0")
	for k, v := range params {
		q.Set("param_"+k, v)
	}

	out, err := guardedCall(ctx, "ch", func() (chOut, error) {
		req, err := http.NewRequestWithContext(ctx, "POST", c.BaseURL+"/?"+q.Encode(), strings.NewReader(sql))
		if err != nil {
			return chOut{}, &BackendError{Backend: "ch", Detail: "connect_failed", Err: err}
		}
		req.Header.Set("X-ClickHouse-User", c.User)
		req.Header.Set("X-ClickHouse-Key", c.Pass)

		hc := c.HTTP
		if hc == nil {
			// 호출당 시한(§15.2-1 3층의 바닥) — 가안, env는 §13.1 주입용.
			hc = &http.Client{Timeout: time.Duration(envInt("RCA_CH_HTTP_TIMEOUT_S", 30)) * time.Second}
		}
		resp, err := hc.Do(req)
		if err != nil {
			return chOut{}, &BackendError{Backend: "ch", Detail: classifyTransportErr(err), Err: err}
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			body, _ := io.ReadAll(io.LimitReader(resp.Body, 500))
			return chOut{}, &BackendError{Backend: "ch", Detail: classifyHTTPStatus(resp.StatusCode),
				Err: fmt.Errorf("HTTP %d: %s", resp.StatusCode, body)}
		}

		capRows := envInt("RCA_CH_CAP_ROWS", chCapRows)
		capBytes := int64(envInt("RCA_CH_CAP_BYTES", chCapBytes))
		var rows []map[string]any
		var bytesRead int64
		sc := bufio.NewScanner(resp.Body)
		sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
		for sc.Scan() {
			line := sc.Bytes()
			if len(line) == 0 {
				continue
			}
			bytesRead += int64(len(line))
			if len(rows) >= capRows || bytesRead > capBytes {
				// 절단: 모은 행까지가 관측이다. 나머지 본문은 읽지 않고
				// 연결을 버린다(Body Close가 중단 처리). 브레이커에는
				// 실패로 세지 않는다 — 백엔드는 건강하고 응답이 클 뿐.
				return chOut{rows: rows, trunc: &CHTrunc{
					Rows: len(rows), BytesRead: bytesRead,
					ByRows: len(rows) >= capRows,
				}}, nil
			}
			var row map[string]any
			if err := json.Unmarshal(line, &row); err != nil {
				return chOut{}, &BackendError{Backend: "ch", Detail: "decode_failed", Err: err}
			}
			rows = append(rows, row)
		}
		if err := sc.Err(); err != nil {
			return chOut{}, &BackendError{Backend: "ch", Detail: "read_failed", Err: err}
		}
		return chOut{rows: rows}, nil
	})
	return out.rows, out.trunc, err
}

// chTime은 CH DateTime64 파라미터용 표기다(UTC).
func chTime(t time.Time) string { return t.UTC().Format("2006-01-02 15:04:05.000000000") }
