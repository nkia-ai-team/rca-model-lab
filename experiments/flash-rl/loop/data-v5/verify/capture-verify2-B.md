# Capture verification — batch B (F51-P, F53-H, F54-P, F57-P, F59-P, F60-H)

All work was read-only on `/data/eval-cases`. Times are UTC (KST = UTC+9). Spans are sampled, so span counts are about 10% of real traffic. Log counts are not sampled. "Normal" means the 120-minute normal phase. Helper scripts are in `scratchpad/verify2-B/` (`h.py` helpers; `xsvc.py`, `routes.py`, `gaps.py`, `errlogs.py`, `logpat.py`, `events.py`, `kcm.py`, `syslog.py`, `k6.py`, `vm.py`, `mysess.py`, `mytop.py`, `clock.py`, `incidents.py`).

"Anomaly opens" below are `lucida_events_local` rows with `class='anomaly'` and `transition='open'` (change-detect `context` rows are excluded).

---

## 1. case-f51-p-v3-1c6b181c — re-dispatch correction with an empty order-id filter re-opens 2,506 delivered dispatches as ASSIGNED

Phases: normal 19:48:55–21:48:58, buffer to 21:59:09, **injection 21:59:09–22:08:23** (9.2 min), cooldown to 22:38:23. Preflight `clean` (user_5xx 0.0, entry p95 0.015 s, diurnal band 3). No companion load (by design).

### 1.1 Injection executed: YES (proxy; the SQL itself is not captured)
- The `CREATE TABLE ... dispatches_redispatch_backup` and `UPDATE ... JOIN` statements are not in the capture. MySQL top SQL holds about 16 digests per minute and has 0 rows for `redispatch`, `CREATE TABLE`, or any statement with rowAffected ≥ 500. DPM sessions show no executor session. syslog has no `Container exec` line.
- **Proxy 1, DPM inventory.** `dpm.mysql.database.table_count{db_name=fooddelivery}` is 16 in all 128 normal and buffer samples. It is **17 from 21:59:35 to 22:07:43** (9 samples), then 16 again at 22:08:44. `dpm.mysql.database.row_count{fooddelivery}` is constant at 11,366,622 through normal and buffer, **11,369,128 (+2,506)** from 21:59:35 to 22:07:43, and back to 11,366,622 at 22:08:44.
- **Proxy 2, capacity count.** The capacity query `SELECT COUNT(d1_0.id) FROM dispatches d1_0 WHERE d1_0.status = ?` never appears in normal or buffer top SQL, because it is too cheap. In injection it appears every minute, with avg rowExamined 1,220 at 21:59 (a partial minute), **2,780 at 22:00**, falling to 2,693 at 22:07.
- The rejection logs give assigned=2,787 at 21:59:13.6, falling to 2,682 at 22:08:21.8. 2,787 − 2,506 = 281 genuinely ASSIGNED rows. That fits about 12 orders/min with a 15–34 min ETA.

### 1.2 Load ran and impact is visible: YES
- **Load.** The food baseline k6 runs at 1 it/s; its cycle from 21:21:56 to 22:21:54 covers the window. Order attempts from unsampled logs:
  - before injection (21:53–21:58): 4–13/min
  - injection: 8–16 rejections/min (115 total)
  - cooldown: 18.7 created/min
- Restaurant SERVER spans run at 9–15/min in injection, with a max gap of 30.5 s and no zero minute. commerce-gateway max gap is 8.0 s.
- **Impact.** "Created order" runs at 12.0/min in normal. In injection there is 1 (21:59:10, before the first rejection at 21:59:13.6), then **0 from 22:00 to 22:07**, resuming with 11 at 22:08.
- "Order rejected: courier pool exhausted (assigned=2682–2787, max=2000)" appears 115 times, all in injection. It appears 0 times in normal, buffer and cooldown.
- Sampled order `POST /api/orders`:
  - normal: 175 × 200, 6 × 400, 0 × 5xx
  - injection: 1 × 200, **5 × 503** (12–22 ms)
- **What keeps working.**
  - dispatch `GET /api/deliveries/capacity` is 6 × 200, p50 4.6 ms (normal 4.2 ms).
  - restaurant has 0 × 5xx.
  - The expiry batch ran 18 times with a mean of 5.8 delivered (normal 6.0, max 12).
  - No other service has a 5xx in any phase.
- **Not verifiable:** "payment and order lookups keep working". Payment is called only once in injection, because orders stop before payment, and the capture has no order-lookup route at all.

### 1.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| order INFO "Order rejected: courier pool exhausted (assigned=<thousands>, max=2000)" from the minute of the run | **present** | 115 lines, assigned 2,682–2,787, max=2000 in all of them. First at 21:59:13.6, 4 s after t1. |
| No rejection at max=2000 in the baseline | **present** (capture scope) | 0 in normal (1,442 orders) and 0 in buffer. |
| dispatch "Dispatched courier=" at 720–4,130/h in the same hours | **present** | 1,442 in the 120-min normal phase = 721/h (low end, night band). |
| Expiry batch keeps its normal small counts | **present** | 18 runs, sum 105, mean 5.8 vs 6.0 in normal. |
| "Dispatched courier" and "Created order" stop during the outage | **present** | 1 each at 21:59:10, then 0 from 22:00 to 22:07. |
| table_count +1 and row_count jump by about the re-opened rows; both return | **present** | 16→17, +2,506, from 21:59:35 to 22:07:43, both back at 22:08:44. |
| ASSIGNED jumps in one minute instead of creeping | **present** (proxy) | The COUNT query is absent before t1 and examines about 2,780 rows from the first full minute. |
| No Kubernetes object changes; max stays 2000 | **present** (absence confirmed) | KCM has only 34 × `FailedToRetrieveImagePullSecret` (kcm-monitoring). kcm_resources_history has 0 rows. All 115 rejections say max=2000. |
| dispatch answers 200 within milliseconds | **present** | capacity 6/6 × 200, p50 4.6 ms. |
| Not a load surge | **present** | Order attempts 8–16/min vs 4–13/min just before. Restaurant 11.5/min vs 11.6/min in buffer. |
| 503 body "No courier available (capacity full)" | **partial** | Status 503 is on 5 spans. The body text is in 0 logs and 0 span messages. |

Metadata accuracy: "about 2,600 rows at night" is 2,506 here (fine). "About 300 to 1,700 ASSIGNED at any time" is about 281 at this hour (slightly low). No material errors.

### 1.4 Incident seed: YES, but thin
There are **7 anomaly opens** in [t1, t2), out of 12 anomaly events:
- log-anomaly on food-delivery-order, "Order rejected: courier pool exhausted", warning at 21:59:24.7
- stream-anomaly on MySQL-fooddelivery:
  - `redo_write_bytes` at 21:59:35
  - `update_count` at 22:00:36 (21.6 vs 70.9)
  - `rollback_count`, critical, at 22:01:37 (18.7 vs 4.1)
- log-anomaly on food-delivery-notify, rate_drop, at 22:00:41
- 2 critical stream opens on PostgreSQL-commerce (`update_count` 915 and `tbl_updates`) at 22:00:43. These are an unrelated hourly batch: commerce update_count spikes at 20:00 (689), 21:00 (688) and 22:00 (915).

The pipeline produced 1 incident: main food-delivery-order, first event 21:59:13. Its title says "a few rejection logs, service metrics normal", so the pipeline under-rated it. There is no alarm on dispatch.

### 1.5 Contamination and clocks
- The only 5xx in the capture are the 5 order 503s. Other WARN/ERROR lines are baseline: banking "Insufficient balance" (16 in normal), ledger mismatch (7 in normal), and sms-agent containerd noise.
- The PostgreSQL-commerce hourly batch alarm at 22:00:43 falls inside the window. It recurs every hour and is unrelated.
- Harness footprint: tb-runner `sudo journalctl -u loadgen-commerce` at 21:59:04 (preflight). tb-runner nkia SSH session opens run at **16.3/min in injection** vs 0.0 in normal and 0.4 in cooldown. This is harness polling, on the load-runner host only.
- Clocks: 8,513 of 8,515 logs that carry a span_id fall inside that span. KCM collected-minus-event lag has p50 1.5 s and max 2.8 s. The DPM inventory step shows on the first sample after t1 (21:59:35). The first rejection is at 21:59:13.6 and the first log-anomaly at 21:59:24.7. The sources agree.

### 1.6 Verdict: **USABLE**
- **Root:** MySQL-fooddelivery **`target:c8c558e5-711f-4e6e-b00f-1adeb097c795`**. The fault is a data change on `fooddelivery.dispatches`. The counting component is food-delivery-dispatch `target:1f1eb9b2-a368-4a70-a003-5fe70b1a983b`. The symptom service is food-delivery-order `target:fa8b5cc4-6c43-45bf-b388-a4b245fade2d`.
- **Identifiable:** yes, but a model has to join the DPM inventory step (table_count +1, row_count +2,506 in the same minute) with assigned=2,787 > max=2000 in the order log. No alarm names the DB table.
- **Caveats:**
  - The SQL is not captured.
  - The alarm seed is thin: 1 alarm on the symptom service, none on dispatch.
  - Impact traffic is low (about 12 orders/min, 5 sampled 503s).
  - The "payment and order lookups keep working" claims cannot be checked, because that traffic is absent.

---

## 2. case-f53-h-v3-d20267b1 — `ALTER TABLE payments ADD COLUMN` queued behind a slow export holding the metadata lock

Phases: normal 17:18:14–19:18:20, buffer to 19:28:34, **injection 19:28:34–19:34:47** (6.2 min), cooldown to 20:04:47. Preflight `clean` (user_5xx 0, entry p95 0.012 s, band 2). No companion load.

### 2.1 Injection executed: YES (direct, from DB sessions; statement texts not captured)
- **DPM MySQL sessions.** The first injection snapshot at 19:28:55 shows:
  - 1 × `fooddelivery@localhost`, state `executing` on fooddelivery, queryTime 17.4 s, so it started at about 19:28:38
  - 1 × `root@localhost`, `Waiting for table metadata lock`, 16.7 s
  - 10 × `fooddelivery@10.244.2.42`, `Waiting for table metadata lock`
- The export's queryTime grows minute by minute: 17.4 → 47.5 → 108.6 → 169.4 → 230.4 → 291.4 → **352.6 s** (19:34:30).
- Waiters from 10.244.2.42 go 10 → 15 (19:29:25–19:33:29) → **24** at 19:34:30. Root waiters go 1 → 2. The DPM account lucida_mon also waits.
- 10.244.2.42 is testbed-payment-78b789d675-64wr5, according to the KCM probe URLs.
- By 19:35:31 all of these sessions are gone. Normal has 0 MDL-wait rows in 118 snapshots.
- syslog on tb-w3 has containerd `Container exec ... stdin closed` at 19:28:37. This is the executor's exec into testbed-mysql-0, and the only exec in the capture.
- The export SELECT and `ALTER ... pg_transaction_id` texts have 0 rows in top SQL. DPM keeps only about 16 digests per minute.

### 2.2 Load ran and impact is visible: YES
- **Load.** The food baseline k6 runs at 1 it/s. Its hourly cycle ended at 19:33:52, inside injection, with the summary "order 200/400/503 92% ✓692 ✗58", and restarted at 19:33:54, a 2 s gap.
- "Created order" continues every minute: 69 in injection (11.1/min vs 12.5/min in buffer). "Dispatched courier" is also 69. Restaurant spans run at 10–16/min, max gap 21 s.
- **Impact.** All **69 of 69** orders created in injection got "Order fan-out payment failed ... status=502". The last good order was at 19:28:23 and the first good one after the outage at 19:34:51.98.
- Sampled order `POST /api/orders`: **7 × 502** in injection. The first, at 19:28:47, took **20.36 s**: two payment calls of 10.01 s each. The rest took 0.34 s.
- "Processed payment" runs at 13.3/min in normal and is 0 from 19:28:23 to 19:34:45.
- **Drain.** Between 19:34:45.70 and 19:34:45.99, 9 payments were "Processed ... APPROVED" for orders 5770062–5770073. All 9 orders had already failed with 502. They were charged after being rolled back (the user_impact says at most 15).
- **What keeps working:**
  - restaurant has 0 × 5xx
  - dispatch capacity and dispatch calls are 200 (7 + 7 sampled)
  - no 5xx in any other service

### 2.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| App-account session from localhost in **'Sending to client'** with queryTime growing toward about 360 s | **partial** | The session is there and grows from 17 s to 353 s, but its state is **`executing`**. No row has the state 'Sending to client'. |
| root@localhost "Waiting for table metadata lock" | **present** | 1–2 rows in every injection snapshot. |
| Payment pod sessions waiting on the metadata lock (and old pod IPs after restarts) | **present** / IP part n/a | 10.244.2.42: 10 → 15 → 24 sessions. A container restart keeps the pod IP, so no other IPs appear. |
| `active_session` rises from 1; MySQL CPU and data-file writes stay usual; table and index counts unchanged | **present** | active_session 1.09 → 13–29. mysqld CPU 3.8% → 4.6%. data_file_write_bytes 2.56e5 → 1.86e5. table_count 16 and index_count 31 throughout. |
| order "Created order" and dispatch assignments continue | **present** | 69 and 69. |
| order ERROR "Failed to process payment": first "... Read timed out", then "500 ..." | **partial** | 66 ERROR lines: 23 × "Error while extracting response for type [PaymentResponse] and content type [application/octet-stream]" (first at 19:28:50), then 4 × "500 Whitelabel", then 39 × "I/O error ... Connection refused" during the restart. There are **0 "Read timed out"** lines, although spans show the 10 s timeouts. |
| "Order fan-out payment failed: ... status=502" | **present** | 69. |
| payment "Processed payment" stops | **present** | 0 from 19:29 to 19:33. |
| "HikariPool-1 - Connection is not available, request timed out after 3000ms" | **present** | 4 lines, 19:29:24–19:29:43 (total=15, active=15). |
| KCM shows testbed-payment Unhealthy and Killing, no rollout | **present** | 21 Unhealthy (readiness from 19:29:24, plus liveness). Killing at 19:30:29 "failed liveness probe". Started at 19:30:59. No ScalingReplicaSet. |
| Unlike F53-P: no 1146, table count stays 16 | **present** | 0 × 1146 in injection; table_count 16. |
| Unlike PG faults: no "PG /pay failed" | **present** | 0. |
| Clears in the minute the export ends | **present** | Payments drain at 19:34:45; sessions are gone by 19:35:31. |

Metadata inaccuracies:
- The state is `executing`, not 'Sending to client'.
- The first order error is "Error while extracting response ... application/octet-stream", not "Read timed out".
- The payment pod restarted once, at 19:30:29 (Killing). That is about 115 s after t1, longer than "about 75 s".

### 2.4 Incident seed: YES
There are **20 anomaly opens** in [t1, t2), out of 32 events:
- MySQL-fooddelivery, 8 stream opens:
  - critical at 19:28:55: data_sent, row_sent_count, row_examined_count, select_full_join
  - critical at 19:29:25: rollback_count
  - also update_count, redo_request_count, memory_disk_count
- food-delivery-order, 7 log opens (6 × the fan-out payment failure template)
- food-delivery-payment, 4 log opens ("DataSource health check failed", "SQL Error: 0, SQLState: null")
- core-banking-ledger, 1 log open (baseline batch log)

The pipeline produced 3 incidents: two with main food-delivery-order and one with main MySQL-fooddelivery.

### 2.5 Contamination and clocks
- There are no 5xx outside order (and 1 payment health 503). The one ledger log-anomaly in the window is baseline noise.
- Harness footprint: tb-runner session opens at 4.0/min in injection vs 0 in normal. `sudo journalctl` at 19:28:27. containerd exec at 19:28:37.
- Clocks:
  - KCM Killing at 19:30:29 matches containerd StopContainer at 19:30:29.49.
  - The JVM log "process running for 145.335 s" at 19:33:25.09 puts the process start at 19:31:00. KCM Started is at 19:30:59.
  - The export start computed from DPM queryTime is 19:28:38. The containerd exec is at 19:28:37.
  - All 7,562 logs with a span_id fall inside their span.
  - KCM lag has p50 1.7 s and max 2.7 s.

### 2.6 Verdict: **USABLE-WITH-CAVEATS** (amend metadata)
- **Root:** MySQL-fooddelivery **`target:c8c558e5-711f-4e6e-b00f-1adeb097c795`**. The fault is a metadata-lock queue on `fooddelivery.payments`: the export session plus the root ALTER on testbed-mysql-0. Affected services: food-delivery-payment `target:2e6120e3-37fa-4c44-a224-6ab13afdd9ee` and food-delivery-order.
- **Identifiable:** yes. DPM sessions show the long export, the waiting root session, and the payment pod's waiting sessions in the same snapshots.
- **Caveats:** two quoted strings in distinguishing_evidence are not in the capture: the 'Sending to client' state and the first error being "Read timed out". Fix them before using the text as a label.

---

## 3. case-f54-p-v3-fb67a747 — `ALTER USER 'fooddelivery'@'%' WITH MAX_UPDATES_PER_HOUR 500`

Phases: normal 15:44:11–17:44:16, buffer to 17:54:24, **injection 17:54:24–18:09:28** (15.1 min), cooldown to 18:39:28. Preflight `clean`. No companion load.

### 3.1 Injection executed: YES (proxy; the DDL is not recordable)
- The capture has no trace of the ALTER USER itself: no exec line, and DPM does not capture DDL.
- The error text names the exact limit. From **18:01:43.6**, which is **7.3 min after t1**, there are 908 lines of ERROR "User 'fooddelivery' has exceeded the 'max_updates' resource (current value: 500)" and WARN "SQL Error: 1226, SQLState: 42000".
- By service: order 688, dispatch 198, payment 22. There are 0 such lines in normal, buffer and cooldown. The last is at 18:09:27.0, 1 s before t2.

### 3.2 Load ran and impact is visible: YES
- **Load.** The food baseline k6 cycle from 17:21:54 to 18:21:52 runs at 1 it/s. The band stepped from 2 to 1 it/s at 17:21, before the buffer. That explains restaurant at 22.9 spans/min in normal vs 11.3–13.1 later.
- In injection, restaurant runs at 8–17 spans/min with a max gap of 27.6 s. The k6 cycle summary at 18:21:52 reads "order 200/400/503 86% ✓650 ✗98".
- **Impact.** "Created order" runs at 7–14/min from 17:51 to 18:01, including the first 7.3 min of injection, is **0 from 18:02 to 18:08**, and returns with 11 at 18:09.
- **The switch is abrupt:**
  - last successful order: 18:01:42.3
  - first 1226 (dispatch expiry batch): 18:01:43.6
  - first failed order write: 18:01:44.2
- The order Tomcat SEVERE line (the failed `POST /api/orders`) appears 98 times, 11–18 per minute. Sampled order `POST /api/orders` in injection: 5 × 200, all before 18:01:43, and **11 × 500**.
- **What keeps working:**
  - restaurant has 0 × 5xx
  - dispatch capacity is 16 × 200 and deliveries are 5 × 200
  - MySQL `avg_query_response_time` stays flat at 0.96
  - `active_session` stays at 1
  - select_count is about 436/min vs 449 in buffer
- Writes catch up at 18:10:31: insert 342 and update 273.

### 3.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| order, dispatch and payment log WARN "SQL Error: 1226, SQLState: 42000" and ERROR "... exceeded the 'max_updates' resource (current value: 500)" | **present** | 344, 107 and 11 WARN/ERROR pairs respectively. |
| Tomcat "Servlet.service() ... threw exception" for POST /api/orders in order | **present** | 98 SEVERE lines. |
| "Failed to publish outbox event ... will retry next cycle" | **present** | 231 (order). |
| "Outbox purge failed" | **present** | order 15, dispatch 16, payment 11. Plus "Dispatch retention purge failed" 74. |
| "Unexpected error occurred in scheduled task" | **present** | 16 (the dispatch expiry batch, 2/min). Expiry batch "finished" 0 from 18:02 to 18:08. |
| Data-changing statements refused "in four services" | **partial** | Only **3** services (restaurant 0). |
| Gradual onset: a growing share of failures over up to about 10 min | **absent (contradicted)** | 0 failures for 7.3 min, then 100% within about 1 s at 18:01:43. Only payment's purge lags (first at 18:04:20). The onset looks like a single switch. |
| DPM insert_count and update_count fall toward 0, select_count continues, rollback_count rises | **partial** | insert 131.7 → 11.8–21.6. update 76.7 → 29.5. Neither reaches 0. select steady at 397–443. rollback 2.9 → **35–71**. |
| No 1290 / 1142 / 1146 / "command denied" | **present** | 0 in injection. |
| MySQL and food pods Ready and fast; KCM shows no rollout or restart in rca-testbed-food | **present** | KCM has only the kcm-monitoring pull-secret events. Response time and active_session flat. |

Metadata inaccuracies:
- The "gradual onset" claim, and the distinction from F38-R built on it, do not hold for this run.
- "Four services" should be three.
- insert and update counts drop but do not reach 0.

### 3.4 Incident seed: YES (logs only)
There are **8 anomaly opens** in [t1, t2), out of 12 events, all **log-anomaly**:
- dispatch, 3: the novel template "Failed to record dispatch outbox event" at 18:01:43.7, plus 2 frequency rules
- order, 3: frequency rules, and "Failed to publish outbox event" at 18:01:47
- payment, 1: at 18:04:21
- core-banking-ledger, 1: baseline "Ledger reconciliation MISMATCH", critical, at 18:05:24

There are **0 stream (metric) opens** in injection, even though rollback_count went from 3 to 71. No alarm sits on MySQL. The pipeline produced an incident at 18:07:31 with main food-delivery-dispatch, titled "DB resource limit, errors in 2 services". The first 7.3 min of [t1, t2) have no symptom and no alarm.

### 3.5 Contamination and clocks
- Ledger "Ledger reconciliation MISMATCH" is a baseline WARN on a roughly 10-minute batch cadence. It appears at 18:05 inside the window. The pipeline also made ledger incidents at 17:05 and 17:35 (normal) and 18:15 and 18:35 (cooldown).
- A closed RSS incident on dev-svr-200-57 ran 16:57–17:05, in normal.
- There are no 5xx outside order.
- Harness footprint: tb-runner session opens at 12.9/min in injection vs 0 in normal. `sudo journalctl` at 17:54:19 and 17:54:21.
- Clocks: 11,427 of 11,431 logs fall inside their span. KCM lag has p50 1.6 s and max 3.3 s. The DPM rollback step appears on the first sample after the first 1226 (18:02:23).

### 3.6 Verdict: **USABLE-WITH-CAVEATS**
- **Root:** MySQL-fooddelivery **`target:c8c558e5-711f-4e6e-b00f-1adeb097c795`**. The fault is a resource limit on the account `'fooddelivery'@'%'`.
- **Identifiable:** yes. The error text names the account, the resource and the value, and it hits 3 services on many tables at once.
- **Caveats:**
  - The metadata's onset description is wrong (a 7.3 min latency, then an abrupt switch).
  - Nearly half of [t1, t2) has no symptom.
  - There is no metric or DB alarm in the seed.
  - "Four services" should be three.

---

## 4. case-f57-p-v3-2b7db69d — commerce PostgreSQL move leaves `inventory_movements_id_seq` 100,000 behind; read-only switch replayed

Phases: normal 23:25:04–01:25:06, buffer to 01:35:22, **injection 01:35:22–01:50:48** (15.4 min), cooldown to 02:20:48. Preflight `clean` (band 6). Companion load: commerce surge, 3 rps.

### 4.1 Injection executed: YES (direct, from the PostgreSQL server log collected by the sms agent from 192.168.200.136)
- The server log shows:
  - **112 × "FATAL: terminating connection due to administrator command"**, in two rounds: 56 received at 01:35:31 and 56 at 01:35:37
  - server-side "ERROR: cannot execute UPDATE/INSERT in a read-only transaction", first at 01:35:28.626
  - 498 server-side duplicate-key errors
- The `ALTER DATABASE ... default_transaction_read_only`, `pg_terminate_backend` and `setval` statements themselves appear 0 times. This is expected: the executor uses track=none sessions.

### 4.2 Load ran and impact is visible: YES
- **Load.** commerce-gateway runs at 130–166 spans/min in injection (surge on top of a baseline of about 96/min), max gap 3.5 s. commerce-order runs at 4–19/min. The baseline k6 cycles turned over at 01:20 and 02:20, both outside the window.
- **Impact.** Sampled checkout `POST /api/orders/checkout`:
  - normal: 282 × 200
  - injection: **170 × 502, 0 × 200**
  - cooldown: 101 × 200
- "Cleared cart after checkout" runs at 22/min in normal and 29.9/min in buffer. It is **0 from 01:36 to 01:49**, 5 at 01:50, and 33 at 01:51.
- inventory `POST /api/inventory/reserve`: 403 × 200 in normal, **48 × 500** in injection.
- Commerce settlement transfers (`from=commerce-settlement`) drop from 40.7/min to 0/min between 01:36 and 01:49.
- **What keeps working:**
  - product GET: 717 × 200 (404s at the baseline rate)
  - cart: 222 × 200 POST and 181 × 200 DELETE
  - users: 105 × 200
  - pricing: 0 × 5xx
  - inventory GET: 47 × 200
  - Only 2 cart 500s occurred, both during the switch.

### 4.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| The switch shows in every commerce service at the same second: 57P01, "marked as broken", "Failed to validate connection", "terminating connection due to administrator command" | **present** | "Failed to validate connection": 106 lines in 6 services, 01:35:28–01:35:37. "marked as broken": 6 (product 4, inventory 2). "SQL Error: 0, SQLState: 57P01": 8 (product). App-side FATAL: 4, plus 112 server-side. |
| About 6 s of WARN 25006 / "cannot execute ... in a read-only transaction" while reads succeed | **present** | 20 × SQLState 25006 between 01:35:28.6 and 01:35:34.9 (shipping, cart, user, order, payment). Text variants: INSERT 30, UPDATE 20. The "SELECT FOR NO KEY UPDATE" variant is absent. |
| A second round of cut connections | **present** | 49 "Failed to validate" at 01:35:35; 56 server FATALs at 01:35:37. |
| None of these strings in normal | **present** | 0 in normal, buffer and cooldown. |
| inventory WARN 23505 + ERROR "duplicate key ... inventory_movements_pkey", "Key (id)=(N) already exists" | **present** | 498 + 498. First at 01:35:38.3, 1 s after the second cut; last at 01:50:42.7. About 32/min. |
| SEVERE servlet line: DataIntegrityViolationException, `insert into inventory_schema.inventory_movements (...)`, constraint [inventory_movements_pkey] | **present** | 482. |
| N grows by one per attempt and no attempt succeeds | **present** | 498 distinct keys forming the contiguous range 10,114,342–10,114,839. The range size equals the count, so each attempt used exactly one key. |
| Reconciliation batch fails ("Unexpected error occurred in scheduled task") | **present** | 16 in inventory, 1/min. |
| DPM rollback_count rises; movement INSERT drops out of top SQL (supporting only) | **partial / present** | rollback 3.9 → 4.5, max 4.87 vs a normal max of 4.13. Weak. The movement INSERT is in top SQL in 35 of 120 normal minutes and **0 of 15** injection minutes. |
| "Cleared cart after checkout" stops; settlement transfers stop | **present** | See 4.2. |
| checkout 502 "Product service unavailable for product N" | **partial** | The 502 is present. The text is in 0 logs, and spans carry no exception message. |
| No pod rolled out or restarted | **present** (note) | KCM shows only 2 inventory Unhealthy events (readiness and liveness 503 at 01:35:28, during the switch) and no Killing. |

### 4.4 Incident seed: YES (strong)
There are **187 anomaly opens** in [t1, t2), out of 222 events:
- **alarm-bridge, 145** on PostgreSQL-commerce: the Polestar policy "PostgreSQL 서버 로그 이벤트 탐지" (PostgreSQL server-log event detection), whose messages quote the duplicate-key and terminating-connection lines (10 critical, 135 warning)
- log-anomaly, 28: commerce services, on the read-only and duplicate-key templates
- stream-anomaly, 14:
  - PostgreSQL backend_read_bytes, blk_hits, eviction and row_fetches, critical, at 01:35:26
  - 4 minor opens on MySQL-fooddelivery, unrelated

The pipeline produced 6 incidents: main PostgreSQL-commerce, commerce-order (2), product, a shipping aggregate, and an Oracle warning (see 4.5).

### 4.5 Contamination and clocks
- An Oracle-corebanking warning incident ("execution count surge") has events from 01:06 to 01:23. That is in the normal phase and ends before the buffer. It is metric-only, with 0 banking 5xx.
- The 4 MySQL-fooddelivery stream opens inside the window come with 0 food 5xx. They are noise.
- Harness footprint: tb-runner session opens at 15.8/min in injection.
- Clocks:
  - The PostgreSQL server log body time 01:35:28.626 matches the app's first 25006 at 01:35:28.628.
  - The sms-agent receipt lags by about 3 s.
  - 18,677 of 18,683 logs fall inside their span.
  - KCM lag has p50 1.6 s and max 3.6 s.

### 4.6 Verdict: **USABLE**
- **Root:** PostgreSQL-commerce **`target:cf97076f-e72d-4c9e-b731-649592cfc5bf`**. The fault is the sequence `inventory_schema.inventory_movements_id_seq`. The first failing service is commerce-inventory `target:414db206-d121-43b1-886c-78b240d23a7a`.
- **Identifiable:** yes. The DB alarm and the 23505 lines with consecutive keys point straight at it.
- **Caveats (minor):**
  - The 502 message text is not captured.
  - The rollback_count rise is weak.
  - The "SELECT FOR NO KEY UPDATE" read-only variant is absent.

---

## 5. case-f59-p-v3-6fd09c9b — Job `ledger-statement-backfill` (48 curl workers) full-scans TRANSFERS and holds the transfer Hikari pool

Phases: normal 03:13:33–05:13:35, buffer to 05:23:45, **injection 05:23:45–05:34:09** (10.4 min), cooldown to 06:04:09. Preflight `clean` (band 9). Companion load: transfer-heavy-surge, 5 rps.

### 5.1 Injection executed: YES (direct)
- **KCM in rca-testbed-banking:**
  - 05:23:48: Job ledger-statement-backfill `SuccessfulCreate` (pod jc99d), and `Scheduled` to tb-w2
  - 05:23:49: `Pulled` "core-banking-ledger:latest" already present, `Created`, `Started` container backfill
  - 05:34:04: `Killing` "Stopping container backfill" (cleanup)
- kcm_resources_history has the Job row (activeDeadlineSeconds 1800, backoffLimit 0) and pod rows at 05:23:48.
- **process_meta on 192.168.200.137 (tb-w2):** 319 command lines of the form `curl -s -o /work/page-N.json -w %{http_code} --max-time 60 http://testbed-transfer:8082/api/transfers?fromAccount=...&page=...&size=500`, seen from 05:24:47 to 05:33:47.
- **transfer `GET /api/transfers` SERVER spans:**
  - injection: **434 from client 10.244.1.36 with user agent `curl/8.5.0`**, size=500, page up to 785
  - normal: 678 spans, all from `Java/17.0.18` at 10.244.1.32 (api), size=20, page=0, over 7 accounts

### 5.2 Load ran and impact is visible: YES
- **Load.** Banking "Submitting transfer" in the unsampled api logs runs at 24.6/min in normal, **137/min in injection**, and 24.7/min in cooldown. The companion surge ramps with the Job: 35 at 05:23, 82 at 05:24.
- In injection, transfer spans run at 41–63/min (max gap 11 s) and api spans at 17–30/min. The baseline k6 cycles turned over at 04:19 and 05:19, outside the window.
- **Impact.** Sampled spans:
  - api `POST /api/transfers`: normal 287 × 200 and 14 × 400; injection **128 × 502**, 12 × 200 and 4 × 400 (89% 502)
  - transfer `POST /api/transfers`: 63 × 500 and 18 × 200
  - account `POST /api/accounts/transfer`: 38 × 502 and 12 × 200
  - history lookups, api `GET /api/transfers`: 92 × 502 and 3 × 200
- transfer logs "HikariPool-1 - Connection is not available, request timed out after 3000ms" **4,391 times** (422/min), from 05:23:53.3 to 05:34:07.6.
- "Transfer COMPLETED" falls from 73.5/min to 20.9/min. Settlement transfers fall from 40.7/min to 6.1/min.
- commerce-order logs "Checkout payment failed, releasing reserved stock ... paymentClient" 361 times. commerce checkout returns 40 × 5xx.
- **What keeps working:**
  - account `GET /api/accounts` 66 × 200 and `/{id}` 253 × 200, 0 × 5xx
  - transfer is not restarted: 26 readiness Unhealthy events, no Killing
  - food has 0 × 5xx

### 5.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM Job events (SuccessfulCreate, Scheduled to tb-w2, Pulled "core-banking-ledger:latest", Created and Started backfill) right before the failures | **present** | 05:23:48–05:23:49. The first Hikari error is at 05:23:53.3. |
| DPM top SQL byb2y2hv7p4ag "many times more often than in normal operation (about 15–30 executions per minute)" at about 0.25 s CPU and about 100k logical reads | **partial** | Executions per snapshot: normal **60.0** (max 77) → injection **80.5** (max 99), only about 1.3× because Oracle is CPU-bound. avgCpu 0.46 s → 0.68 s; avgLogicalReads about 109k, flat. The normal rate is already about 60/min, not about 15. |
| An offset form of the page statement that nothing sends in normal | **present** | New sql_id `35vxdaqwrs31x`, only in injection (13 rows, 45 executions per row, avgLR 62k, avgCpu 0.95 s) plus 1 cooldown row. In db_sql_plan it is `TABLE ACCESS FULL TRANSFERS` under a `rowlimit > :11` (OFFSET) window. |
| Oracle CPU, DB time and active sessions rise | **present** | cpu_time 0.54 → **1.94** (about 2 CPUs). db_time 0.56 → **15.4**. active_session 0.46 → **15–16**. Oracle process CPU 47% → 158%. |
| transfer spans with url.query fromAccount=<**hundreds of different accounts**>&page=<n>&size=500, the backfill pod as client, user agent curl | **partial** | User agent curl (434), client 10.244.1.36, size=500 and page ≤785 are all present. But there are only **4 distinct accounts**: ACC-1002 347, ACC-2002 60, ACC-9002 23, ACC-1001 4. |
| transfer Hikari "Connection is not available ... 3000ms" | **present** | 4,391. |
| account "Transfer service call failed" and circuit-open messages | **present** | 297, plus 8,189 circuit lines (api 7,306, account 1,061). |
| api 502; "Transfer COMPLETED" thins | **present** | See 5.2. |
| No rollout, no image change, no `enq: TX`, transfer does not restart | **present** | KCM shows only the Job/pod events and transfer readiness. blocked_session 0. enqueue_wait_time flat (2.3e-6 vs 1.4e-6). 0 "enq: TX". |

Metadata inaccuracies:
- "Hundreds of different accounts" is 4 here.
- "15–30 executions per minute ... many times more often" is wrong for this run: about 60 → 80 per minute.

### 5.4 Incident seed: YES
There are **35 anomaly opens** in [t1, t2), out of 55 events:
- transfer, 10: the Hikari novel template at 05:23:53.6, "SQL Error: 0, SQLState: null", "DataSource health check failed"
- api, 9
- Oracle-corebanking, 6 stream opens at 05:24:59: **db_time critical, 15.4 vs 0.61**, plus logical_reads, physical_reads and execution_count
- commerce-order, 5
- account, 5

The pipeline produced 9 incidents. 7 of them cover transfer pool, api, account and commerce-order, with first events from 05:23:53 to 05:31.

### 5.5 Contamination and clocks
- **The companion surge starts with t1.** api log rate rises 5.6×, and the pipeline titles the api and account incidents as "log surge". A model must not pick the surge as the root. The Job evidence (KCM, curl spans, process_meta, Oracle db_time) separates the two.
- **Two pipeline incidents carry misleading first-event times.** Their first_event_at is 04:18:17 and 04:25:17, in normal, but they were created at 05:28 and 05:43, and their summaries quote injection-time numbers (37–56% errors, p95 4.3 s). Telemetry from 04:15 to 04:35 is clean: api p95 0.49–0.55 s and 0 × 5xx. This is a pipeline merge artifact from the baseline "Insufficient balance" 400s, not a second incident.
- Ledger mismatch (baseline) appears at 05:25, inside the window.
- Harness footprint: tb-runner session opens at 16.5/min. `sudo journalctl` at 05:23:39 and 05:23:41.
- Clocks:
  - KCM SuccessfulCreate at 05:23:48 matches containerd RunPodSandbox at 05:23:49.10.
  - KCM Killing at 05:34:04 matches the last Hikari error at 05:34:07.6.
  - 34,419 of 34,432 logs fall inside their span (max offset 1 ms).
  - KCM lag has p50 1.6 s and max 2.8 s.

### 5.6 Verdict: **USABLE-WITH-CAVEATS**
- **Root:** the Kubernetes Job **ledger-statement-backfill** (pod ledger-statement-backfill-jc99d, namespace rca-testbed-banking, node tb-w2). KCM rows carry target_id a072cd3d-4a33-4542-832d-b4beec88609f. **No topology graph snapshot contains the Job or its pod.**
- Affected: core-banking-transfer `target:f0ce5940-a7e2-44a2-843f-196de379d898` (pool) and Oracle-corebanking `target:00149c2f-8a97-43bf-8907-c25029973e51` (CPU).
- **Identifiable:** yes, through KCM, the curl spans and process_meta.
- **Caveats:**
  - The root is not a graph node, so an evaluator must accept the workload by name.
  - Two metadata figures are wrong: the account count and the execution rate.
  - The companion surge coincides with t1.
  - Two stale pipeline incidents carry wrong first-event times.

---

## 6. case-f60-h-v3-1aece1d2 — `ALTER TABLE dispatches ADD COLUMN` queued behind a slow export holding the metadata lock

Phases: normal 11:54:29–13:54:44, buffer to 14:04:56, **injection 14:04:56–14:11:09** (6.2 min), cooldown to 14:41:09. Preflight `clean` (band 6). No companion load.

### 6.1 Injection executed: YES (direct, from DB sessions)
- **DPM at 14:05:51:**
  - export `fooddelivery@localhost`, `executing`, queryTime 51.3 s, so it started at about 14:05:00
  - `root@localhost`, "Waiting for table metadata lock"
  - **10 × 10.244.2.41** (testbed-dispatch-597bfbc784-zn9df), waiting
- The export's queryTime grows 51.3 → 142.3 → 203.3 → 264.3 → **325.3 s** (14:10:25). Dispatch waiters go 10 → **20** at 14:10:25. Root waiters go 1 → 2. lucida_mon also waits.
- All of these are gone at 14:11:26. Normal has 0 MDL-wait rows.
- DPM has one missed snapshot: the gap runs from 14:05:51 to 14:07:22.
- containerd `Container exec` on tb-w3 at 14:04:59.9.

### 6.2 Load ran and impact is visible: YES (but impact outlives t2)
- **Load.** The food k6 cycle from 13:21:57 to 14:21:53 has no boundary inside the window. Restaurant runs at 33–48 spans/min in injection, max gap 9 s.
- Order attempts continue: 164 "Failed to check dispatch capacity" lines (including retries), plus 21 "restaurant CLOSED" rejections, which are baseline.
- **Impact.** "Created order" runs at 41.8/min in buffer. In injection there are 2 (at 14:05:0x), then **0 from 14:06 to 14:12**, 6 at 14:13 and 48 at 14:14.
- Sampled order `POST /api/orders`:
  - injection: **26 × 503**, 1 × 400, 0 × 200
  - cooldown: **18 × 503**, the last at 14:13:45.2, then 107 × 200
- order ERROR "Failed to check dispatch capacity" breaks down as: 9 × "Read timed out" (first at 14:05:06.5), 20 × "500 Whitelabel", and 209 × "Connection refused" while dispatch restarts. **74 of these fall in cooldown**, the last at 14:13:50.
- dispatch logs "HikariPool-1 - Connection is not available ... 3000ms (total=10, active=10)" 49 times, 14:05:10–14:10:11. Its spans show `GET /api/deliveries` 4 × 500, capacity 1 × 500 and health 503. The expiry batch runs 0 times in injection (normal 2/min).
- **What keeps working:** restaurant has 0 × 5xx. No other service has a 5xx.
- **Dispatch restarts:**
  1. KCM Killing at 14:06:23 ("failed liveness probe"), Started 14:06:53, app "Started" at 14:09:11.9. The new container blocks again: Hikari errors from 14:09:39 to 14:10:11.
  2. containerd StopContainer at 14:10:53, exit at 14:11:07.96, StartContainer at 14:11:08.06, app "Started" at 14:13:40.4. Orders resume at 14:13:57. **KCM did not record this second Killing/Started**; its last dispatch event is the liveness failure at 14:10:23.

### 6.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| App-account session from localhost in **'Sending to client'** with queryTime growing toward about 360 s | **partial** | The session is there and grows from 51 s to 325 s, but its state is **`executing`**. |
| root@localhost "Waiting for table metadata lock" | **present** | 1–2 rows in every injection snapshot. |
| Dispatch pod sessions waiting (and old pod IPs after restarts) | **present** / IP part n/a | 10.244.2.41: 10 → 20. The IP is the same across container restarts. |
| active_session rises from its usual 1 (7-day max 12) | **present** | 1.07 → 13–25. |
| MySQL CPU and data-file writes stay at their usual level; table and index counts unchanged | **present** | mysqld CPU 7.0% → 4.5%. data_file_write_bytes falls from 3.35e5 to 3.07e4 because writes stop; there is no burst. table_count 16, index_count 31. |
| order ERROR "Failed to check dispatch capacity": first "Read timed out", then "500", then 503 | **present** | 9 "Read timed out", then 20 × 500 and 209 × connection refused. 26 order 503 spans. |
| dispatch "HikariPool-1 - Connection is not available ... 3000ms" | **present** | 49. |
| KCM shows testbed-dispatch Unhealthy and Killing, no rollout | **present** (partial for the 2nd restart) | 22 Unhealthy, 1 Killing, no ScalingReplicaSet. The second restart appears only in syslog and the app logs. |
| Unlike F60-R: no "copy to tmp table", no data-file write burst; reads of dispatches blocked too | **present** | No such session state. `GET /api/deliveries` 4 × 500. |
| Unlike F32-P: no 1146, table count stays 16 | **present** | |
| Everything clears in the minute the export ends | **partial** | MDL sessions are gone by 14:11:26. The service outage runs on to **14:13:50** (2.7 min past t2) because of the second liveness restart at 14:10:53. |

Metadata inaccuracies:
- The state is `executing`, not 'Sending to client'.
- "Clears in the minute" does not hold: there is a 2.7 min tail.
- The second restart is missing from KCM.

### 6.4 Incident seed: YES
There are **30 anomaly opens** in [t1, t2), out of 48 events:
- MySQL-fooddelivery, 16 stream opens. At 14:05:51: row_sent, tps, insert_count, update_count, data_sent, qps, rollback_count (critical), redo_wait and others.
- food-delivery-dispatch, 9 log opens: Hikari, "SQL Error: 0, SQLState: null", "DataSource health check failed"
- food-delivery-order, 4
- food-delivery-notify, 1 (rate drop)

The pipeline produced 5 relevant incidents: order at 14:05:07 and 4 on dispatch.

### 6.5 Contamination and clocks
- No unrelated incident overlaps the window. Two unrelated incidents are closed: PostgreSQL-commerce at 10:41 (before the capture) and ledger at 12:23 (normal). Ledger mismatch at 14:04 is baseline.
- Harness footprint: tb-runner session opens at 3.9/min in injection. `sudo journalctl` at 14:04:48 and 14:04:50. containerd exec at 14:04:59.9.
- Clocks:
  - The JVM log "process running for 138.292 s" at 14:09:11.947 puts the process start at 14:06:53.66. KCM Started is at 14:06:53 and containerd StartContainer at 14:06:53.65.
  - For the second start, "152.291 s" at 14:13:40.354 gives 14:11:08.06, which equals containerd's time.
  - The export start computed from DPM is 14:05:00. The containerd exec is at 14:04:59.9.
  - All 23,539 logs with a span_id fall inside their span.
  - KCM lag has p50 1.5 s and max 3.0 s.

### 6.6 Verdict: **USABLE-WITH-CAVEATS**
- **Root:** MySQL-fooddelivery **`target:c8c558e5-711f-4e6e-b00f-1adeb097c795`**. The fault is a metadata-lock queue on `fooddelivery.dispatches`. Affected services: food-delivery-dispatch `target:1f1eb9b2-a368-4a70-a003-5fe70b1a983b` and food-delivery-order `target:fa8b5cc4-6c43-45bf-b388-a4b245fade2d`.
- **Identifiable:** yes, from the DPM sessions.
- **Caveats:**
  - The 'Sending to client' quote is wrong.
  - The outage extends 2.7 min into cooldown (74 order errors and 18 sampled 503s).
  - KCM is missing the second dispatch restart.

---

## Cross-case checks
- **Load generator:** never stopped in any case.
  - The baseline k6 restarts every hour with a 2–5 s gap. Only F53 has a cycle boundary inside injection (food k6, 19:33:52 → 19:33:54).
  - Every case has non-zero per-minute restaurant and commerce-gateway spans in every injection minute.
  - The unsampled order and transfer logs confirm continuous attempts.
- **Unrelated incidents:** none overlap a target chain. All 5xx sit in the scenario chain, with one exception: F57 has 2 commerce-cart 500s, which happened during the switch the scenario itself replays.
- **Baseline noise present in every case:**
  - banking "Insufficient balance" 400s and api circuit ERRORs
  - "Ledger reconciliation MISMATCH" WARN on a 10-minute cadence. It falls inside the window in F54, F57 and F59, and F54's in-window alarm comes from it.
  - the commerce baseline k6 shows cart add/remove at 0% success (401s, http_req_failed 43–44% in every cycle)
  - sms-agent containerd snapshot errors
  - an hourly commerce PostgreSQL update batch at :00 (it alarms inside F51's window)
- **Alarm warm-up:** in the first 60–75 min of normal (after trainer_reset), stream-anomaly opens run at 113–270 per 15 min. They then drop to 0–33 per 15 min. This never reaches [t1, t2). F53 has no such burst.
- **Harness footprint:**
  - tb-runner nkia SSH session opens rise to 3.9–16.5/min during injection, vs 0–0.06/min in normal.
  - `sudo journalctl -u loadgen-commerce` runs within 10 s before t1 in every case.
  - containerd `Container exec` on tb-w3 at about t1+3 s appears only in F53 and F60.
  - All of this is on hosts other than the root target, but it is time-aligned with the injection.
- **Clock skew:** none. Logs fall inside their spans in more than 99.9% of matches. KCM lag p50 is 1.5–1.7 s and max is 3.6 s. KCM, containerd, JVM start times, DPM session ages and the PostgreSQL server log agree within 1 s.
- **Recurring metadata error (F53, F60):** the DPM state of the export session is `executing` in MySQL 8.0.46, never 'Sending to client'. Both metadata texts should be corrected.

## Summary
| Case | Verdict | Root target | Key caveat |
|---|---|---|---|
| case-f51-p-v3-1c6b181c | **USABLE** | MySQL-fooddelivery `target:c8c558e5-711f-4e6e-b00f-1adeb097c795` (data in `fooddelivery.dispatches`; counting component food-delivery-dispatch) | The SQL is not captured. The proxy is the DPM table_count 16→17 and row_count +2,506 in the same minute as 115 rejections (assigned 2,787 > max 2000). The alarm seed is thin (7 opens, 1 on the symptom service). The payment and order-lookup claims cannot be checked because that traffic is absent. |
| case-f53-h-v3-d20267b1 | **USABLE-WITH-CAVEATS** | MySQL-fooddelivery `target:c8c558e5-...` (metadata lock on `fooddelivery.payments`; affected payment `target:2e6120e3-...`) | DPM sessions are decisive and 69 of 69 orders get 502. But the metadata quotes 'Sending to client' (the actual state is `executing`) and a first error of "Read timed out" (actually "Error while extracting response ... octet-stream"). |
| case-f54-p-v3-fb67a747 | **USABLE-WITH-CAVEATS** | MySQL-fooddelivery `target:c8c558e5-...` (account `'fooddelivery'@'%'` MAX_UPDATES_PER_HOUR 500) | 908 lines of 1226 "max_updates (current value: 500)" in 3 services (not 4). The onset is not gradual: 7.3 min of nothing, then 100% failure within 1 s. The seed has only 8 log alarms and no DB or metric alarm. |
| case-f57-p-v3-2b7db69d | **USABLE** | PostgreSQL-commerce `target:cf97076f-e72d-4c9e-b731-649592cfc5bf` (`inventory_movements_id_seq`; first service commerce-inventory) | Minor. All decisive items are present: two 57P01 rounds, 6 s of 25006, 498 × 23505 with contiguous keys, and 145 PostgreSQL server-log alarms. The 502 text is not captured and the rollback rise is weak. |
| case-f59-p-v3-6fd09c9b | **USABLE-WITH-CAVEATS** | Job `ledger-statement-backfill` (ns rca-testbed-banking, pod jc99d on tb-w2; not a topology node). Affected core-banking-transfer `target:f0ce5940-...` and Oracle `target:00149c2f-...` | The Job events, curl spans and Oracle db_time 0.56→15.4 are clear. But only 4 accounts were touched (metadata says hundreds), byb2y2hv7p4ag executions went only from 60 to 80/min (metadata says 15–30/min, many times normal), and the companion surge plus two stale pipeline incidents are potential distractors. |
| case-f60-h-v3-1aece1d2 | **USABLE-WITH-CAVEATS** | MySQL-fooddelivery `target:c8c558e5-...` (metadata lock on `fooddelivery.dispatches`; affected dispatch `target:1f1eb9b2-...`) | DPM sessions are decisive and orders get 100% 503. But the 'Sending to client' quote is wrong, and the second liveness restart at 14:10:53 is missing from KCM and extends the outage 2.7 min past t2 (74 order errors in cooldown). |
