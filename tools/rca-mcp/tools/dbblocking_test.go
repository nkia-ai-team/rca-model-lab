// db_blocking 단위 가드 — 실 DB 없이 §13의 결정들이 코드로 지켜지는지.
// 실측에서 온 시나리오를 그대로 넣는다: PG 07-16 다단 체인(짝 지워진
// blocker 5개), Oracle 07-22 루트 이동, Oracle 배경 잡음(2~3폴·1세션).
package tools

import (
	"testing"
	"time"
)

func ts(min, sec int) time.Time {
	return time.Date(2026, 7, 16, 8, min, sec, 0, time.UTC)
}

func poll(min int, blockingN, blockedN int) dbBlkPoll {
	return dbBlkPoll{ts: ts(min, 30), engine: "postgresql", rows: 40,
		blockingN: blockingN, blockedN: blockedN, hasAxis: true}
}

// 결정 1 — 수집된 비블로킹 폴이 끼면 사건이 끊긴다. 폴 부재는 끊지
// 않고 gap_polls로 센다(§13.2).
func TestDBBlkFoldSplitsOnCollectedQuietPoll(t *testing.T) {
	polls := []dbBlkPoll{
		poll(36, 8, 10), poll(37, 8, 10),
		poll(38, 0, 0), // 수집됐고 조용함 → 끊는다
		poll(39, 3, 4), poll(40, 3, 4),
	}
	ev := dbBlkFoldEvents(polls)
	if len(ev) != 2 {
		t.Fatalf("사건 2건이어야 함(조용한 폴이 끊는다), got %d", len(ev))
	}
	if len(ev[0].polls) != 2 || len(ev[1].polls) != 2 {
		t.Fatalf("각 2폴이어야 함: %d, %d", len(ev[0].polls), len(ev[1].polls))
	}
	if ev[0].peak != 10 || ev[1].peak != 4 {
		t.Fatalf("peak 10/4여야 함: %d/%d", ev[0].peak, ev[1].peak)
	}
}

func TestDBBlkFoldGapDoesNotSplit(t *testing.T) {
	// 36·37 관측 후 38·39는 행 자체가 없고 40에 다시 관측 → 한 사건 + gap 2.
	polls := []dbBlkPoll{poll(36, 8, 10), poll(37, 8, 10), poll(40, 8, 12)}
	ev := dbBlkFoldEvents(polls)
	if len(ev) != 1 {
		t.Fatalf("폴 부재는 끊지 않아야 함, got %d 사건", len(ev))
	}
	if ev[0].gapPolls != 2 {
		t.Fatalf("gap_polls 2여야 함(38·39 부재), got %d", ev[0].gapPolls)
	}
	if ev[0].peak != 12 || ev[0].last != 12 {
		t.Fatalf("peak/last 12여야 함: %d/%d", ev[0].peak, ev[0].last)
	}
}

// 결정 1 — 루트가 폴을 넘어 이동해도 한 사건이다(Oracle 07-22 실측).
func TestDBBlkRootMigrationStaysOneEvent(t *testing.T) {
	p1 := dbBlkPoll{ts: ts(28, 38), engine: "oracle", rows: 86, blockingN: 1, blockedN: 6, hasAxis: true}
	p2 := dbBlkPoll{ts: ts(29, 39), engine: "oracle", rows: 89, blockingN: 1, blockedN: 8, hasAxis: true}
	ev := dbBlkFoldEvents([]dbBlkPoll{p1, p2})
	if len(ev) != 1 {
		t.Fatalf("루트 이동은 사건을 쪼개지 않아야 함, got %d", len(ev))
	}
	byPoll := map[string][]dbBlkRow{
		p1.ts.Format(time.RFC3339Nano): {
			{ts: p1.ts, engine: "oracle", sid: 227, blocker: 0, blocking: true},
			{ts: p1.ts, engine: "oracle", sid: 220, blocker: 227},
			{ts: p1.ts, engine: "oracle", sid: 64, blocker: 227},
		},
		p2.ts.Format(time.RFC3339Nano): {
			{ts: p2.ts, engine: "oracle", sid: 220, blocker: 0, blocking: true},
			{ts: p2.ts, engine: "oracle", sid: 227, blocker: 220},
			{ts: p2.ts, engine: "oracle", sid: 64, blocker: 220},
		},
	}
	f, _ := dbBlkFinding("tgt", ev[0], byPoll, "verified")
	if f["root_migrated"] != true {
		t.Fatal("root_migrated=true여야 함(227→220)")
	}
	// 대표 폴은 blocked_n 최대 폴 = p2. 짝은 그 한 폴 소속이어야 하며
	// 방향이 뒤집힌 두 폴의 짝이 섞이면 안 된다(§13.4).
	if f["representative_poll_ts"] != p2.ts.UTC().Format(time.RFC3339) {
		t.Fatalf("대표 폴은 blocked_n 최대 폴이어야 함: %v", f["representative_poll_ts"])
	}
	edges := f["edges"].([][]string)
	for _, e := range edges {
		if e[1] != "220" {
			t.Fatalf("대표 폴(p2)의 blocker는 전부 220이어야 함 — 시점 혼합: %v", edges)
		}
	}
	tl := f["root_timeline"].([]map[string]any)
	if len(tl) != 2 {
		t.Fatalf("root_timeline 2폴이어야 함: %d", len(tl))
	}
}

// 결정 2 — 짝 지워진 blocker는 unpaired_blockers로 드러난다(PG 07-16
// 실측: 25039·25049·25051·25058·25059).
func TestDBBlkUnpairedAndOrphan(t *testing.T) {
	at := ts(36, 30)
	p := dbBlkPoll{ts: at, engine: "postgresql", rows: 43, blockingN: 8, blockedN: 10, hasAxis: true}
	rows := []dbBlkRow{
		{ts: at, sid: 25206, blocker: 0, blocking: true, rel: "inventory, inventory_pkey"},
		{ts: at, sid: 25043, blocker: 25206, blocking: true, rel: "inventory"},
		{ts: at, sid: 25039, blocker: 25043, blocking: true, rel: "inventory"},
		{ts: at, sid: 25049, blocker: 25043, blocking: true, rel: "inventory"},
		{ts: at, sid: 25071, blocker: 25043, blocking: true, rel: "inventory_movements"},
		{ts: at, sid: 25074, blocker: 25071, rel: "inventory"},
		{ts: at, sid: 25081, blocker: 25071, rel: "inventory"},
		{ts: at, sid: 25075, blocker: 25043, rel: "inventory"},
		// orphan: 막혔다고 표시됐으나 지목한 상대가 이 폴에 없다.
		{ts: at, sid: 25999, blocker: 30000},
	}
	ev := dbBlkFoldEvents([]dbBlkPoll{p})
	f, refs := dbBlkFinding("tgt", ev[0], map[string][]dbBlkRow{at.Format(time.RFC3339Nano): rows}, "verified")

	unpaired := f["unpaired_blockers"].([]string)
	want := map[string]bool{"25039": true, "25049": true}
	for _, u := range unpaired {
		delete(want, u)
	}
	if len(want) != 0 {
		t.Fatalf("짝 지워진 blocker가 unpaired에 없다: got %v", unpaired)
	}
	for _, u := range unpaired {
		if u == "25206" || u == "25043" || u == "25071" {
			t.Fatalf("짝이 이어진 blocker가 unpaired에 섞였다: %v", unpaired)
		}
	}
	orphan := f["orphan_blocked"].([]string)
	if len(orphan) != 1 || orphan[0] != "25999" {
		t.Fatalf("orphan_blocked는 25999 하나여야 함: %v", orphan)
	}
	roles := f["roles"].(map[string][]string)
	if len(roles["root"]) != 1 || roles["root"][0] != "25206" {
		t.Fatalf("root는 25206 하나여야 함: %v", roles["root"])
	}
	if len(roles["both"]) != 4 {
		t.Fatalf("both(막히며 막음) 4개여야 함: %v", roles["both"])
	}
	// 깊이 하한: 25074 → 25071 → 25043 → 25206 = 4노드.
	if got := f["chain_depth_min_observed"].(int); got != 4 {
		t.Fatalf("깊이 하한 4여야 함: %d", got)
	}
	objs := f["contended_objects"].([]string)
	if len(objs) != 3 {
		t.Fatalf("경합 객체 3종(원문 보존·인덱스 포함)이어야 함: %v", objs)
	}
	if len(refs) < 2 {
		t.Fatalf("refs에 대표 폴 + 루트 세션 좌표가 있어야 함: %v", refs)
	}
}

// 결정 4 — 지속은 판정에서 빠졌다. Oracle 배경(2~3폴·1세션)은 normal,
// 규모가 큰 사건만 anomalous.
func TestDBBlkThresholdIsMagnitudeOnly(t *testing.T) {
	bg := dbBlkFoldEvents([]dbBlkPoll{
		{ts: ts(10, 0), engine: "oracle", blockedN: 1, hasAxis: true},
		{ts: ts(11, 0), engine: "oracle", blockedN: 1, hasAxis: true},
		{ts: ts(12, 0), engine: "oracle", blockedN: 1, hasAxis: true},
	})
	if len(bg) != 1 || bg[0].peak != 1 {
		t.Fatalf("배경 사건 1건·peak 1이어야 함: %+v", bg)
	}
	if bg[0].peak >= dbBlkPeakThreshold {
		t.Fatalf("3폴 지속이라도 1세션이면 이상이 아니어야 함(peak=%d, 임계=%d)", bg[0].peak, dbBlkPeakThreshold)
	}
	one := dbBlkFoldEvents([]dbBlkPoll{{ts: ts(20, 0), engine: "postgresql", blockedN: 10, hasAxis: true}})
	if one[0].peak < dbBlkPeakThreshold {
		t.Fatalf("단발이라도 10세션이면 이상이어야 함: peak=%d", one[0].peak)
	}
}

// 결정 5 — 판별식 `> 0`이 sentinel 차이(CUBRID -1)를 흡수한다.
func TestDBBlkSentinelAbsorbedByPredicate(t *testing.T) {
	if got := dbBlkPredicate("cubrid"); got != "blockingTid > 0" {
		t.Fatalf("cubrid 판별식: %q", got)
	}
	// -1(없음)과 0(없음)이 모두 blocked가 아니어야 한다.
	for _, v := range []int{-1, 0} {
		r := dbBlkRow{sid: 7, blocker: v, blocking: true}
		if r.role() != "root" {
			t.Fatalf("blocker=%d는 '막히지 않음'이라 root여야 함: %s", v, r.role())
		}
	}
	if dbBlkPredicate("nosuchdb") == "blockingX > 0" {
		t.Fatal("미등록 engine은 짝 키 계약 없음을 밝혀야 함")
	}
}

// 결정 5 — engine이 섞이면 짝 등급은 가장 낮은 쪽으로 내린다.
func TestDBBlkPairingGrade(t *testing.T) {
	cases := []struct {
		engines []string
		want    string
	}{
		{[]string{"postgresql"}, "verified"},
		{[]string{"postgresql", "mysql"}, "unverified"},
		{[]string{"clickhouse"}, "unsupported"},
		{[]string{"postgresql", "clickhouse"}, "unsupported"},
		{[]string{"nosuchdb"}, "unsupported"},
	}
	for _, c := range cases {
		set := map[string]bool{}
		for _, e := range c.engines {
			set[e] = true
		}
		if got, _ := dbBlkPairing(set); got != c.want {
			t.Fatalf("%v → %q, want %q", c.engines, got, c.want)
		}
	}
}

// 결정 1 — 폴 주기는 데이터에서 구한다(하드코딩 금지).
func TestDBBlkPollInterval(t *testing.T) {
	ps := []dbBlkPoll{poll(1, 1, 1), poll(2, 1, 1), poll(3, 1, 1), poll(9, 1, 1)}
	if got := dbBlkPollInterval(ps); got != time.Minute {
		t.Fatalf("폴 주기 1분이어야 함: %v", got)
	}
	if got := dbBlkPollInterval(ps[:1]); got != 0 {
		t.Fatalf("폴 1개면 간격 미정(0): %v", got)
	}
}

// 깊이 계산은 사이클에서 멈춘다(폴 내 사이클은 미관측이나 방어는 둔다).
func TestDBBlkDepthCycleGuard(t *testing.T) {
	if got := dbBlkDepth([][]string{{"a", "b"}, {"b", "a"}}); got < 2 {
		t.Fatalf("사이클에서도 유한값을 내야 함: %d", got)
	}
	if got := dbBlkDepth(nil); got != 0 {
		t.Fatalf("간선 없으면 0: %d", got)
	}
}
