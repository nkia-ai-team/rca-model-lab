// PG 저장소 접속 — 도구 계약의 PG 원천(targets, 변경 3테이블,
// alert_history, asset_tree_members, kcm_resource_targets …)을 직접
// 친다. API(:18080) 경유가 아닌 직접 접속: 기존 UI API의 조회 취향을
// 상속하지 않고 조회 설계를 우리 계약에서 도출한다(불참조 원칙,
// ref-tool-data-access.md §1) + 캡처/재생의 격리 DB 복원과 정합.
package tools

import (
	"database/sql"
	"fmt"
	"net/url"
	"strings"

	_ "github.com/jackc/pgx/v5/stdlib"
)

// pgStatementTimeout은 PG 세션 statement_timeout이다. ctx 취소가 닿지
// 않는 서버측 실행(클라이언트가 끊겨도 서버가 계속 도는 구간)을 서버
// 스스로 끊게 하는 두 번째 가드다(스펙 §14-0·§15.2).
//
// 가안 — §13 재주행 실측 역산 전(스펙 §15.2-1이 값을 실측 역산으로
// 정하기로 함).
const pgStatementTimeout = "60s"

// OpenPG는 lucida PG에 접속한다. DSN 예:
// postgres://lucida:lucida123@192.168.230.119:15432/lucida?sslmode=disable
func OpenPG(dsn string) (*sql.DB, error) {
	db, err := sql.Open("pgx", withStatementTimeout(dsn))
	if err != nil {
		return nil, fmt.Errorf("tools: PG 접속: %w", err)
	}
	// bulkhead(§15.2-7) — 현행 무제한 풀 폐기. 가안(§14-7 실측 역산 전).
	db.SetMaxOpenConns(envInt("RCA_PG_MAX_CONNS", defaultBackendConcurrency))
	if err := db.Ping(); err != nil {
		db.Close()
		return nil, fmt.Errorf("tools: PG ping: %w", err)
	}
	return db, nil
}

// withStatementTimeout은 DSN에 statement_timeout 세션 설정을 얹는다.
// 이미 지정돼 있으면 건드리지 않는다(호출자 지정 우선).
func withStatementTimeout(dsn string) string {
	if strings.Contains(dsn, "statement_timeout") {
		return dsn
	}
	u, err := url.Parse(dsn)
	if err != nil || (u.Scheme != "postgres" && u.Scheme != "postgresql") {
		// key=value 형식 DSN.
		return dsn + " options='-c statement_timeout=" + pgStatementTimeout + "'"
	}
	q := u.Query()
	q.Set("options", strings.TrimSpace(q.Get("options")+" -c statement_timeout="+pgStatementTimeout))
	u.RawQuery = q.Encode()
	return u.String()
}
