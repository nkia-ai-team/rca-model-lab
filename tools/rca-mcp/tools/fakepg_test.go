// dgFakePG — 최소 PostgreSQL v3 와이어 서버(degrade_test의 산 PG 자리).
// safetygate/fakepg_test.go의 이식이다 — 같은 판단 지점을 승계한다:
// docker 일회용 PG는 스키마 부재로 전 조회가 실패 주입이 되어 기각,
// DSN에 simple_protocol을 강제한다(fake가 SQL을 파싱하지 않아 extended의
// 파라미터 OID를 못 맞힌다). 대본은 "쿼리 부분 문자열 → text 행"이다.
package tools

import (
	"bufio"
	"encoding/binary"
	"fmt"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
)

type dgPGScript struct {
	contains string
	cols     []string
	rows     [][]string
}

type dgFakePG struct {
	ln   net.Listener
	addr string

	mu      sync.Mutex
	scripts []dgPGScript
}

func newDgFakePG(t *testing.T) *dgFakePG {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("dgFakePG listen: %v", err)
	}
	p := &dgFakePG{ln: ln, addr: ln.Addr().String()}
	go p.serve()
	t.Cleanup(func() { ln.Close() })
	return p
}

func (p *dgFakePG) dsn() string {
	return fmt.Sprintf("postgres://dg:dg@%s/lucida?sslmode=disable&default_query_exec_mode=simple_protocol", p.addr)
}

func (p *dgFakePG) script(contains string, cols []string, rows [][]string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.scripts = append(p.scripts, dgPGScript{contains: contains, cols: cols, rows: rows})
}

func (p *dgFakePG) match(sql string) *dgPGScript {
	p.mu.Lock()
	defer p.mu.Unlock()
	for i := range p.scripts {
		if strings.Contains(sql, p.scripts[i].contains) {
			return &p.scripts[i]
		}
	}
	return nil
}

func (p *dgFakePG) serve() {
	for {
		c, err := p.ln.Accept()
		if err != nil {
			return
		}
		go p.handle(c)
	}
}

// ── 와이어 부품 ─────────────────────────────────────────────────

func fpgWriteMsg(w io.Writer, typ byte, payload []byte) {
	buf := make([]byte, 5+len(payload))
	buf[0] = typ
	binary.BigEndian.PutUint32(buf[1:5], uint32(4+len(payload)))
	copy(buf[5:], payload)
	w.Write(buf)
}

func fpgCstr(s string) []byte { return append([]byte(s), 0) }

func (p *dgFakePG) handle(c net.Conn) {
	defer c.Close()
	r := bufio.NewReader(c)

	// startup 국면 — SSLRequest는 'N'으로 거절하고 StartupMessage를 기다린다.
	for {
		var lenBuf [4]byte
		if _, err := io.ReadFull(r, lenBuf[:]); err != nil {
			return
		}
		n := binary.BigEndian.Uint32(lenBuf[:])
		if n < 8 || n > 1<<20 {
			return
		}
		body := make([]byte, n-4)
		if _, err := io.ReadFull(r, body); err != nil {
			return
		}
		code := binary.BigEndian.Uint32(body[:4])
		if code == 80877103 { // SSLRequest
			c.Write([]byte{'N'})
			continue
		}
		if code == 196608 { // StartupMessage v3.0
			break
		}
		return
	}

	fpgWriteMsg(c, 'R', []byte{0, 0, 0, 0}) // AuthenticationOk
	for _, kv := range [][2]string{
		{"server_version", "16.0"}, {"client_encoding", "UTF8"},
		{"DateStyle", "ISO, MDY"}, {"integer_datetimes", "on"},
		{"standard_conforming_strings", "on"}, {"TimeZone", "UTC"},
	} {
		fpgWriteMsg(c, 'S', append(fpgCstr(kv[0]), fpgCstr(kv[1])...))
	}
	fpgWriteMsg(c, 'K', []byte{0, 0, 0, 1, 0, 0, 0, 1}) // BackendKeyData
	fpgWriteMsg(c, 'Z', []byte{'I'})                    // ReadyForQuery

	stmts := map[string]string{}
	portals := map[string]string{}
	for {
		typ, err := r.ReadByte()
		if err != nil {
			return
		}
		var lenBuf [4]byte
		if _, err := io.ReadFull(r, lenBuf[:]); err != nil {
			return
		}
		n := binary.BigEndian.Uint32(lenBuf[:])
		if n < 4 || n > 16<<20 {
			return
		}
		body := make([]byte, n-4)
		if _, err := io.ReadFull(r, body); err != nil {
			return
		}

		switch typ {
		case 'Q': // simple query — 이 fake의 주 경로
			sql := strings.TrimRight(string(body), "\x00")
			if strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(sql), ";")) == "" {
				fpgWriteMsg(c, 'I', nil) // EmptyQueryResponse
			} else if sc := p.match(sql); sc != nil {
				p.sendRows(c, sc)
			} else {
				fpgWriteMsg(c, 'C', fpgCstr("SELECT 0"))
			}
			fpgWriteMsg(c, 'Z', []byte{'I'})
		case 'P': // Parse — extended fallback
			name, rest := fpgSplitCstr(body)
			sql, _ := fpgSplitCstr(rest)
			stmts[name] = sql
			fpgWriteMsg(c, '1', nil)
		case 'B':
			portal, rest := fpgSplitCstr(body)
			stmt, _ := fpgSplitCstr(rest)
			portals[portal] = stmt
			fpgWriteMsg(c, '2', nil)
		case 'D':
			if len(body) < 1 {
				return
			}
			kind := body[0]
			name, _ := fpgSplitCstr(body[1:])
			sql := stmts[name]
			if kind == 'P' {
				sql = stmts[portals[name]]
			}
			if kind == 'S' {
				np := fpgCountParams(sql)
				pd := make([]byte, 2+4*np)
				binary.BigEndian.PutUint16(pd[:2], uint16(np))
				fpgWriteMsg(c, 't', pd) // ParameterDescription — 전부 OID 0
			}
			if sc := p.match(sql); sc != nil {
				fpgWriteMsg(c, 'T', fpgRowDescription(sc.cols))
			} else {
				fpgWriteMsg(c, 'n', nil) // NoData
			}
		case 'E':
			portal, _ := fpgSplitCstr(body)
			sql := stmts[portals[portal]]
			if sc := p.match(sql); sc != nil {
				p.sendRows(c, sc)
			} else {
				fpgWriteMsg(c, 'C', fpgCstr("SELECT 0"))
			}
		case 'S':
			fpgWriteMsg(c, 'Z', []byte{'I'})
		case 'C':
			fpgWriteMsg(c, '3', nil)
		case 'H':
			// flush — 즉시 쓰기 방식이라 무동작
		case 'X':
			return
		}
	}
}

func (p *dgFakePG) sendRows(c net.Conn, sc *dgPGScript) {
	fpgWriteMsg(c, 'T', fpgRowDescription(sc.cols))
	for _, row := range sc.rows {
		var b []byte
		var n [2]byte
		binary.BigEndian.PutUint16(n[:], uint16(len(row)))
		b = append(b, n[:]...)
		for _, v := range row {
			var l [4]byte
			binary.BigEndian.PutUint32(l[:], uint32(len(v)))
			b = append(b, l[:]...)
			b = append(b, v...)
		}
		fpgWriteMsg(c, 'D', b)
	}
	fpgWriteMsg(c, 'C', fpgCstr(fmt.Sprintf("SELECT %d", len(sc.rows))))
}

// fpgRowDescription — 전 컬럼 text(OID 25)·text format.
func fpgRowDescription(cols []string) []byte {
	var b []byte
	var n [2]byte
	binary.BigEndian.PutUint16(n[:], uint16(len(cols)))
	b = append(b, n[:]...)
	for _, name := range cols {
		b = append(b, fpgCstr(name)...)
		b = append(b, 0, 0, 0, 0)             // table OID
		b = append(b, 0, 0)                   // attr no
		b = append(b, 0, 0, 0, 25)            // type OID text
		b = append(b, 0xff, 0xff)             // typlen -1
		b = append(b, 0xff, 0xff, 0xff, 0xff) // typmod -1
		b = append(b, 0, 0)                   // format text
	}
	return b
}

func fpgSplitCstr(b []byte) (string, []byte) {
	for i, c := range b {
		if c == 0 {
			return string(b[:i]), b[i+1:]
		}
	}
	return string(b), nil
}

// fpgCountParams는 $n 최대 번호다(extended fallback용).
func fpgCountParams(sql string) int {
	max := 0
	for i := 0; i+1 < len(sql); i++ {
		if sql[i] != '$' {
			continue
		}
		n := 0
		j := i + 1
		for j < len(sql) && sql[j] >= '0' && sql[j] <= '9' {
			n = n*10 + int(sql[j]-'0')
			j++
		}
		if n > max {
			max = n
		}
	}
	return max
}
