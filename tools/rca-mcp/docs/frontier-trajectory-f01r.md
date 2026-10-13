# Frontier RCA Trajectory — f01r

**Symptom:** 2026-07-21 ~06:41–06:44 UTC. Checkout failing — commerce-order requests timing out, stock/inventory reservation stalling.
**Anchors:** commerce-order `93df2d34-b054-4ecd-8f8e-bafa098dc6cd` (user-facing), commerce-inventory `414db206-d121-43b1-886c-78b240d23a7a` (reservation path).
**Method:** blind, tool-only. Baseline windows chosen before 06:41.

---

## A. Step-by-step trajectory

**1. `discover_signals` on commerce-order** (06:38–06:45, baseline auto = prior equal window)
- Reason (pre-result): anchor is the failing service; get its metric inventory + top anomalies to orient.
- Key fact: error_rate 0→31%, **db.client.connections.pending_requests 0→4.67**, db_responsetime 7.7µs→405µs (52×), p99 latency 72µs→2254µs (30×), error_count up. Signature is DB-bound, not CPU/mem.
- Decision: extend causal story toward the DB path; confirm where request time actually goes.

**2. `get_trace_breakdown` service=commerce-order** (06:40–06:45)
- Reason: separate self-time vs downstream (own DB vs a downstream service call).
- Key fact: 137 req, avg 2258ms, p95 6003ms, **127 errors**; self 81%. Own `postgresql` span fast (0.8ms), but an outgoing **POST** downstream is slow (avg 357ms, p95 3005ms). Order's *completed* DB calls are cheap.
- Decision: the slow POST is the reservation call to inventory; the symptom names reservation stalling → widen to commerce-inventory. **(Widening #1)**

**3. `discover_signals` on commerce-inventory** (06:38–06:45)
- Reason: is inventory the epicenter of the DB problem?
- Key fact: **db.client.connections.timeouts newly appears = 113**, **pending_requests 0→14**, p95/p99 latency up ~350–420× (→2.4ms), db_responsetime up 94×, error_rate up. Far more severe than order.
- Decision: inventory's DB connection pool is starving. Find WHY — is it the shared DB, or inventory-specific? Need the DB target + a peer comparison.

**4. `get_topology` -incident, target=commerce-inventory, hops=1, then hops=2**
- Reason: locate inventory's database and neighbors to pivot DB-side.
- Key fact: inventory→`db:postgresql` (apm_db). Same `db:postgresql` placeholder is shared by commerce-order, -product, -user, -shipping, -payment, -pricing, -cart (8+ services → one PG). DB node has **no UUID** (synthetic APM label). Callers of inventory: commerce-order + commerce-product.
- Decision: shared DB → must test "DB-wide saturation" vs "inventory-local." Need DB UUID for DB-side tools; try to resolve it.

**5. `get_target_meta` on all commerce neighbors + `db:postgresql`**
- Reason: identify which neighbor (if any) is type=database; get its UUID.
- Key fact: all neighbors are type=application (user/shipping/payment/pricing/cart/notification/gateway/product). `db:postgresql` rejected: "not a target_id." The DPM-monitored DB target is **not present** in the APM topology.
- Decision: DB UUID unavailable via topology. Try events/error-recovery paths.

**6. `get_events` on inventory & order** (06:35–06:46)
- Reason: any external DB alarm (would carry DB target_id) or onset ordering.
- Key fact: only platform anomaly detectors — inventory trace_anomaly first_at **06:35:39**, jvm.memory.used level_shift (critical) 06:39:10, jvm.thread.count level_shift 06:42:30; order jvm.memory/thread level_shifts 06:39:49+. No DB target exposed. Thread-count rise = threads piling up (blocked). Inventory slightly precedes order.
- Decision: onset direction inventory→order supported. Still need DB view; probe error-recovery.

**7. Probe `get_db_sessions` with dummy all-zero UUID**
- Reason: task said errors carry recovery info (valid id lists).
- Key fact: returns `no_data` (accepts any UUID, queries by it) — **no id list**. So the DB target is a specific unknown UUID; no enumeration offered.
- Decision: try to bridge via changes/coverage/logs instead.

**8. `get_changes` inventory & order (06:00–06:46) + `get_data_coverage` inventory**
- Reason: rule in/out a deploy/config trigger; coverage may surface a linked DB collector; confirm no_data semantics.
- Key fact: **zero changes** on both (policy/collector/audit all 0) → not a deploy-triggered regression. Coverage: inventory has only polestar_apm collector (last_status pending), 1863 vm series / 563 logs / 14 events present. No DB target surfaced.
- Decision: organic/injected DB-side fault (not a change). Read the logs for the actual failure.

**9. `get_logs` inventory** (06:38–06:45)
- Reason: app logs will name the concrete failure mode.
- Key fact (decisive): **`HikariPool-1 - Connection is not available, request timed out after 30000ms (total=10, active=10, idle=0, waiting=161)` ×170**; `CannotCreateTransactionException: Could not open JPA EntityManager` ×168; `DataSourceHealthIndicator (db) took 30002ms`. Pool max=10, all active, none idle, 161 queued, 30s timeout. Connections are **held, not returned**.
- Decision: 10 connections held ≥30s with fast completed queries ⇒ they are blocked *inside* the DB (lock wait). The lock holder is the root cause. Need DB sessions; also test whether it is DB-wide.

**10. `get_cohort` pending_requests, inventory vs peers** (06:40–06:45)  *(ran before #9 in practice; decisive discriminator)*
- Reason: is pool starvation DB-wide (shared-resource saturation) or inventory-local?
- Key fact: inventory avg 20.2, commerce-order 6.5, **all 6 other services on the same PG = 0**.
- Decision: **NOT DB-wide.** PG globally healthy; contention is on objects only inventory contends for (stock/reservation rows). Kills the "PG saturated / max_connections" hypothesis. order's 6.5 = cascade.

**11. Probe `get_runtime_connections` with app UUID + `get_logs filter=lock`**
- Reason: last attempts to (a) reach a host/DB endpoint, (b) find explicit lock text.
- Key fact: runtime_connections no_data (app is not an SMS host target); no "lock" lines (HikariCP masks the DB-side cause — it only reports acquisition timeout).
- Decision: DB-side blocker is not observable from app side. Confirm the checkout-path timing, then conclude.

**12. `get_slow_endpoints` inventory & order + `get_metric_series` pool usage inventory**
- Reason: confirm the reservation endpoint hits the 30s timeout and pin pool size.
- Key fact: inventory top endpoint avg 2234ms, **p95 30003ms** (= the 30s Hikari timeout) count 121; order checkout endpoint 127/135 errors, p95 6036ms; inventory `db.client.connections.usage` max=10 (pool size confirmed).
- Decision: chain complete end-to-end. Conclude (provisional on the exact DB lock holder).

---

## B. Causal graph

Facts (ref):
- F1 inventory pool exhausted: active=10/idle=0/waiting=161, timeouts=113, usage.max=10 — `ch:lucida_logs_local:414db206…:tmpl:772884968814269338`, `vm:db.client.connections.timeouts{…414db206…}`, `vm:db.client.connections.usage{…414db206…}`
- F2 inventory reservation endpoint blocks full 30s — `get_slow_endpoints commerce-inventory` p95 30003ms
- F3 inventory cannot even begin JPA transaction — `…tmpl:13294294273314588015` (CannotCreateTransactionException)
- F4 contention is inventory-local, not DB-wide — `get_cohort db.client.connections.pending_requests` (peers all 0)
- F5 order checkout errors/timeouts — `get_trace_breakdown commerce-order` (127/137 err), `get_slow_endpoints commerce-order`
- F6 order's slow segment is the downstream POST to inventory; order's own completed DB spans are fast — `get_trace_breakdown commerce-order`
- F7 order has secondary pool wait (pending 6.5) — `get_cohort` self-row order
- F8 no deploy/config change — `get_changes` both = 0
- F9 onset inventory (06:35–39) ≤ order (06:39+) — `get_events` both

Arrows:
- (DB-side lock/transaction holds rows on inventory's reservation objects) → F1 inventory pool held/exhausted — **supported** (held ≥30s + fast completed queries + inventory-local; holder itself unseen)
- F1 → F2 reservation stalls (30s block) — **verified**
- F1 → F3 new txns can't open — **verified**
- F2 → F6 inventory POST slow to order — **verified** (trace)
- F6 → F5 order checkout times out — **verified**
- F5 + (order holds its txn connection while awaiting inventory) → F7 order pool secondary drain — **supported**
- F4 ⇒ NOT(PG-global saturation) — **verified** (contradicts DB-wide hypothesis)
- F8 ⇒ NOT(deploy/config regression) — **verified**

Mechanism: **DB-side lock/transaction contention on commerce-inventory's stock-reservation rows → inventory's 10 HikariCP connections all held by transactions blocked in the DB → pool exhausted (161 waiting, 113 timeouts, 30s) → reservation endpoint stalls → commerce-order's reservation POST times out (and order's own checkout txn holds its connection while waiting, draining order's pool too) → checkout fails.**

---

## C. Forks considered & closed

1. **DB-wide / shared-PG saturation (max_connections, CPU, IO)** — opened at step 4 (8+ services share one PG). Closed at step 10 `get_cohort`: only inventory(+order) show pool waits; other 6 peers flat 0 ⇒ PG globally healthy. Killed.
2. **commerce-order own-DB problem** — plausible from step 1 (order db_responsetime up, pending 4.67). Closed at step 2: order's completed postgres spans are 0.8ms and its slow segment is the downstream POST, not its own DB; order's pool wait is a cascade (F6/F7).
3. **JVM memory / GC pause in inventory as root** — jvm.memory.used level_shift was "critical" (step 6). Parked then closed: the signature (10 connections held, health check exactly 30002ms, timeouts metric) is connection-acquisition blocking, not GC; memory/thread rise is explained by 161 queued request threads (consequence). GC wouldn't produce db.client.connections.timeouts.
4. **Deploy/config change trigger** — closed at step 8 `get_changes` = 0 on both.
5. **Kafka involvement** (producer io_wait, heartbeat blips in discover_signals) — parked as noise: magnitudes small, no consumer lag anomaly dominates, and the pool/lock evidence is far stronger and self-consistent.

---

## D. Widening moments (what pulled me to each new target)

- **#1 order → inventory (step 2→3):** the `get_trace_breakdown` **outgoing POST segment** (p95 3005ms) — order's own DB was fast, so the latency lived in a downstream call; the symptom text ("reservation stalling") named inventory, and the POST matched a reservation call. The *trace edge*, not a metric name, made inventory attractive.
- **inventory → (attempted) DB (step 3→4):** inventory's **`db.client.connections.timeouts` / `pending_requests`** metric names — a pool-acquisition failure points at the tier below the pool. The metric semantics forced the DB pivot.
- **inventory → peer cohort (step 10):** the topology fact that **8+ services share one `db:postgresql`** made "is this shared or local?" the pivotal question; a *shared dependency* attracted the cohort comparison, which then re-localized the fault back onto inventory.
- The DB pivot **failed to land** (no DB UUID) — see F.

---

## E. Conclusion

**Most-likely root cause:** DB-side lock/transaction contention on commerce-inventory's stock-reservation rows, holding all 10 of inventory's HikariCP connections until they time out. This exhausts inventory's connection pool (161 waiting, 113 timeouts, 30s acquisition timeout), stalling stock reservation; commerce-order's reservation POST then times out (order additionally holds its own transaction connection while awaiting inventory, draining its pool), so checkout fails. Blast radius is confined to the inventory reservation path — the shared PG and all other commerce services are healthy.

**Confidence:** High (~0.8) that the mechanism is *inventory connection-pool exhaustion from connections blocked inside the DB* (verified end-to-end from app metrics, logs, traces, endpoints, cohort). **Provisional** on the *specific* DB blocker — i.e., whether it is self-inflicted hot-row serialization among concurrent reservations vs. an external long/idle-in-transaction session or explicit lock holding the reservation rows. Both are "DB row-lock contention"; the evidence (connections held ≥30s, completed queries fast, strictly inventory-local, no deploy change) fits an external/holding lock on inventory-owned rows best, but I could not view the lock graph.

**Stop reason:** Out of discriminating moves within the instrument. The one move that would upgrade provisional→verified — `get_db_sessions` (blocking chain) / `get_slow_queries` on the PostgreSQL DPM target — is unreachable because the DB target UUID cannot be resolved (see F). All app-side discriminators are exhausted and mutually consistent.

**Missing evidence to confirm:** the DB session/lock snapshot showing (a) the blocking session and its SQL/lock object, (b) whether the holder is idle-in-transaction or a long UPDATE/SELECT…FOR UPDATE on the stock table, (c) whether the waiters are inventory's own reservation queries.

---

## F. Tool / legibility friction

1. **No bridge from APM topology to the DPM database target (the big one).** Every DB edge collapses to a synthetic `db:postgresql` label with no UUID. `get_db_sessions`/`get_slow_queries` require a database target_id, but nothing in the reachable graph (topology, target_meta, events, changes, data_coverage, runtime_connections) exposes it, and error messages don't enumerate valid DB targets. Result: with a textbook DB-contention incident, the two tools purpose-built to confirm it were unusable. A `db:postgresql`→DPM-target-UUID mapping (or a topology node carrying the DB's real UUID) would have closed the case.
2. **`no_data` is ambiguous across "wrong id" vs "not collected."** Both the dummy-UUID `get_db_sessions` and app-UUID `get_runtime_connections` returned generic `no_data` that just redirect to `get_data_coverage` — no "valid ids are …" recovery list, so I couldn't discover correct ids by probing.
3. **HikariCP masks the DB cause.** App logs show only "Connection is not available … waiting=161" and `SQLState: null`; the actual lock/blocker is invisible from the app tier, and there is no tool that lifts app-side pool starvation to the DB session that caused it without the DB UUID.
4. **`discover_signals` change-scores are unnormalized** (e.g., change_score 3e10 for a 0→31% move), so ranking by score over-weights any-metric-from-zero; I had to re-read absolute current_avg to judge severity.
5. **`get_metric_series` gives window summary, not a per-bucket series**, so pinning exact onset/lead-lag between inventory and order relied on coarse `get_events` timestamps rather than aligned curves.
6. **No target directory / search.** Every pivot needs a UUID you already possess; there is no way to list "all database targets" or "all hosts," which is exactly what a blind investigator lacks.
