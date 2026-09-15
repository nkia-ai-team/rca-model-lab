// CH Truncation 절단 계약(§14-7 — 6b cap_exceeded 실패 골격의 승격) 단위
// 시험. httptest로 JSONEachRow를 흘려 캡 경계를 검사한다.
package tools

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func chForTest(url string) *CH {
	return &CH{BaseURL: url, User: "u", Pass: "p", Database: "d"}
}

// 캡 도달 = 실패가 아니라 절단이다: 모은 행 반환 + 표식, 오류 nil,
// 브레이커 무산입.
func TestCHQueryTruncation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var b strings.Builder
		for i := 0; i < 10; i++ {
			fmt.Fprintf(&b, `{"n":%d}`+"\n", i)
		}
		w.Write([]byte(b.String()))
	}))
	defer srv.Close()

	t.Setenv("RCA_CH_CAP_ROWS", "3")

	rows, tr, err := chForTest(srv.URL).Query(context.Background(), "SELECT 1", nil)
	if err != nil {
		t.Fatalf("절단이 오류로 나왔다: %v", err)
	}
	if len(rows) != 3 {
		t.Errorf("행 %d — 캡 3까지 모은 행을 반환해야 함", len(rows))
	}
	if tr == nil {
		t.Fatal("절단 표식 nil — 캡 도달인데 완전 조회로 보인다")
	}
	if !tr.ByRows || tr.Rows != 3 {
		t.Errorf("표식 %+v — ByRows·Rows=3이어야 함", tr)
	}
	// 절단은 백엔드 실패가 아니다 — 브레이커가 열리면 안 된다.
	if err := defaultBreaker.Allow("ch"); err != nil {
		t.Errorf("절단 후 브레이커 차단: %v — 절단은 실패 산입 금지", err)
	}
}

// 캡 미만 완전 조회 = 표식 nil.
func TestCHQueryNoTruncation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"n":1}` + "\n" + `{"n":2}` + "\n"))
	}))
	defer srv.Close()

	rows, tr, err := chForTest(srv.URL).Query(context.Background(), "SELECT 1", nil)
	if err != nil {
		t.Fatalf("조회 실패: %v", err)
	}
	if len(rows) != 2 || tr != nil {
		t.Errorf("행 %d·표식 %v — 완전 조회는 2행·표식 nil", len(rows), tr)
	}
}

// 바이트 캡 절단 — ByRows=false.
func TestCHQueryByteCapTruncation(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for i := 0; i < 5; i++ {
			fmt.Fprintf(w, `{"pad":%q}`+"\n", strings.Repeat("x", 100))
		}
	}))
	defer srv.Close()

	t.Setenv("RCA_CH_CAP_BYTES", "250") // 행당 ~110B — 3행째에서 초과

	rows, tr, err := chForTest(srv.URL).Query(context.Background(), "SELECT 1", nil)
	if err != nil {
		t.Fatalf("절단이 오류로 나왔다: %v", err)
	}
	if tr == nil || tr.ByRows {
		t.Fatalf("표식 %+v — 바이트 캡 절단(ByRows=false)이어야 함", tr)
	}
	if len(rows) == 0 || len(rows) >= 5 {
		t.Errorf("행 %d — 일부만 모여야 함", len(rows))
	}
}
