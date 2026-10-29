# Capture verification 2 — batch C (F60-P, F60-R, F63-P, F64-P, F64-R, F65-R)

All work was read-only on `/data/eval-cases`. Times are UTC; KST = UTC+9 and is shown only on the phase lines. Spans are sampled at about 10%, so span counts are roughly a tenth of real traffic (error spans appear to be kept at a higher rate). Log counts are not sampled. "Normal" means the 120-minute normal phase. Helper scripts are in `scratchpad/verify2-C/`.

"Anomaly opens" are rows in `lucida_events_local` with `class=anomaly` and `transition=open`. The change-detect `context` rows, mostly disk trait changes, are counted separately.

---

## 1. case-f60-p-v3-04d37516: cancelled index backfill renames `fooddelivery.dispatch_outbox_events` into a hold table

Phases:
- normal 00:08:49–02:09:02
- buffer to 02:19:16
- **injection 02:19:16–02:34:43** (KST 11:19–11:34)
- cooldown to 03:04:43

Preflight `clean`: user_5xx 0.0, entry p95 0.008 s, 0 alarms, 0 open incidents. No companion load.

### 1.1 Injection executed: YES (shown by the DPM inventory; the DDL text itself is not captured)
- **`dpm.mysql.database.table_count{db_name=fooddelivery}`:** 16 in every normal and buffer sample, **17 from 02:19:19**, back to 16 at 02:35:38.
- **`dpm.mysql.database.index_count{fooddelivery}`:** 31, then **34** over exactly the same samples.
  - The shadow table plus its new index explain both changes. A rollout cannot change either value.
- **First** `SQL Error: 1146, SQLState: 42S02` from dispatch: 02:19:32.924. **Last**: 02:34:40.71. This brackets the rename and the cleanup.
- syslog and KCM show no harness command. The executor ran through the MySQL client inside the pod, which does not reach node syslog.

### 1.2 Load ran and impact is visible: YES
- **Load:**
  - food-order `Created order|Order rejected` logs: normal 36.4/min (largest gap 15 s), injection 14.0/min (largest gap 12 s), cooldown 48.1/min.
  - Restaurant SERVER spans: largest gap 11.5 s in injection.
  - Order POST spans per minute are unchanged through the window, at 2–11 sampled per minute.
- **Impact, every order fails:**
  - Order logs from 02:20 to 02:33: 10–15 `Created order` per minute and **0 committed**. Every created order ends in `Order fan-out dispatch failed` (146 with `CircuitBreaker 'dispatch' is OPEN`, 26 with `Dispatch service failed: 500`).
  - Normal: 4210 created and 4210 committed, 0 failures.
  - Order `POST /api/orders`:
    - normal: 407 × 200, 14 × 400, 0 × 5xx.
    - injection: **65 × 503** (p50 614 ms), 1 × 200, 5 × 400.
    - 49 of the 65 503s have no call to dispatch: they were rejected at the capacity check while the breaker was open. This matches the metadata.
- **Things that should keep working do keep working:**
  - Dispatch `GET /api/deliveries/capacity`: 17 × 200 (p50 6 ms).
  - Dispatch `GET /api/deliveries`: 24 × 200 (p50 8 ms).
  - Restaurant: 48.3 spans/min, 0 × 5xx.
  - No other service has a 5xx in any phase.
  - Payment receives no calls: 0 `Processed payment` from 02:20 to 02:33, so nobody is charged.

### 1.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| dispatch WARN `SQL Error: 1146, SQLState: 42S02` plus ERROR `Table 'fooddelivery.dispatch_outbox_events' doesn't exist` | **present** | 10,474 WARN and 10,910 ERROR, all in injection. 0 in normal, buffer and cooldown. |
| Relay poll every 2 s: scheduler ERROR `Unexpected error occurred in scheduled task` with that table in the exception | **present** | 465 rows (30.2/min, i.e. every 2 s). The exception shows `select ... from dispatch_outbox_events ... where published_at is null`. |
| WARN `Failed to record dispatch outbox event (non-critical)`, then in the same trace `UnexpectedRollbackException ... rollback-only` and 500, although `Dispatched courier=` is logged | **present** | 364 SEVERE `Servlet.service() ... UnexpectedRollbackException`. **All 364 traces** also contain the outbox WARN, the 1146, and `Dispatched courier=`. Dispatch `POST /api/deliveries/dispatch`: 34 × 500 (p50 19 ms), 0 in normal. 0 `rollback-only` lines outside injection. |
| Expiry batch logs `delivered=N` but the same candidates return | **present** | `delivered=` climbs 21 → 41 → 83 → … → 651 (02:34:34), then 669 at 02:35:05, then 25 at 02:35:36. Normal values are 12–36. |
| Split on the same DB: capacity and delivery reads 200, browsing 200, payment untouched | **present** | See 1.2. |
| DPM table_count +1 and index_count +3 in the same minute | **present** | See 1.1 (02:19:19). |
| Not F38-R read-only / not instance-wide | **present (absence confirmed)** | No 1290 or `read-only`. No other service logs SQL errors. |
| No rollout or restart in rca-testbed-food | **present (absence confirmed)** | KCM has only the 35 recurring `FailedToRetrieveImagePullSecret` rows. `kcm.pod.container_restart_count` is unchanged for every food pod. |
| Not a pool or capacity problem: no Hikari pending, MySQL at baseline | **present** | dispatch `db.client.connections.pending_requests` is 0 in every phase. `dpm.mysql.session.active_session` mean 1.03 → 1.31 (max 3). `avg_query_response_time` 1.10 → 1.09. |

Metadata accuracy: everything checked holds.

### 1.4 Incident seed: YES
- **31 anomaly opens in [t1, t2)** (2.0/min against 1.16/min in normal):
  - stream-anomaly 15, log-anomaly 16; 17 critical.
  - Targets: food-delivery-dispatch 11, MySQL-fooddelivery 10, food-delivery-order 8.
- The first opens are log-anomaly `new_template` events on the outbox WARN and on `Table ... doesn't exist` (02:19:33), plus the MySQL rollback_count, update_count and redo metrics (02:20:22).
- `incidents.csv` has 5 active food incidents. One is titled "food-delivery-dispatch 에서 테이블 부재로 인한 오류" (errors in dispatch caused by a missing table).

### 1.5 Contamination and clocks
- **No overlapping incident.** ERROR and WARN logs outside dispatch and order are baseline noise at normal rates:
  - core-banking-api `Insufficient balance` 400s
  - ledger `Reconciliation MISMATCH`
  - containerd snapshot errors
- **One ledger-mismatch incident from before capture** (first event 00:06) was closed at 02:19:08, before the first fault line.
- **Collector blip:** SMS agents on tb-w1, w2 and w3 logged `cannot reach the server 192.168.230.128:18090` from 02:20:31 to 02:21:09. `process_snapshot` has no gap, so no data was lost.
- **Clocks:**
  - Every log that has a trace falls inside its span (100%).
  - The DPM inventory change (02:19:19) lines up with the first 1146 (02:19:32).
  - KCM collected-minus-event lag: p50 1.6 s, max 2.8 s.
- The platform-wide skew of tb-w2 `kcm.pod.*` metrics (see Cross-case checks) affects only banking pods here.

### 1.6 Verdict: **USABLE**
The table and index count step, the 1146 errors naming the table, and the rollback-only failure in the same trace are all present. Orders go from 0% to 100% failed, every non-outbox path stays healthy, and nothing else overlaps.

Root target: clearly identifiable. It is the DB, **MySQL-fooddelivery `target:c8c558e5-711f-4e6e-b00f-1adeb097c795`**, table `fooddelivery.dispatch_outbox_events`. The symptom services are food-delivery-dispatch (`1f1eb9b2-…`) and food-delivery-order (`fa8b5cc4-…`).

---

## 2. case-f60-r-v3-22036218: foreign-key migration copies `dispatch_events`; writes wait on the metadata lock

Phases:
- normal 20:01:23–22:01:28
- buffer to 22:12:11
- **injection 22:12:11–22:14:28 (only 2 min 17 s)** (KST 07:12–07:14)
- cooldown to 22:44:28

Preflight `clean`; diurnal band 3 rps. No companion load. Food load is low at this hour: about 12.5 orders/min.

### 2.1 Injection executed: YES (direct: the DDL session is in DPM)
- `dpm_session_local` (MySQL), snapshot 22:12:39:
  - **`root@localhost` / fooddelivery / Query / `copy to tmp table`, queryTime 25.2 s.**
  - Next to it, 10 sessions from the dispatch pod 10.244.2.41 in **`Waiting for table metadata lock`** (8.7–24.1 s).
- Snapshot 22:13:41: the copy session is at **87.8 s**, with the same 10 waiting sessions at 71–87 s.
- Snapshot 22:14:43: the copy session and the waiters are gone.
- So the copy ran from about 22:12:13 to between 22:13:41 and 22:14:43, consistent with ending at t2 (22:14:28): about 2.2 minutes.
- `copy to tmp table` appears in 2 snapshots in total, both in injection. `Waiting for table metadata lock` appears 20 times, all in injection.

### 2.2 Load ran and impact is visible: YES, but the outage runs past t2
- **Load:**
  - food-order `Created|Rejected` logs: normal 12.5/min (largest gap 37 s), buffer 13.3/min.
  - Restaurant spans: largest gap 18.9 s in injection.
  - Dispatch spans have a 265 s gap from 22:12:20 (dispatch was hung, then restarting).
- **Impact timeline:**
  - **22:12:15:** order 5604580 is created.
  - **22:12:20.69, 22:12:25.90, 22:12:31.31:** three dispatch attempts, each failing exactly 5.0 s after the previous one (the read timeout).
  - **22:12:31.3:** 503. The order hung 15.6 s, matching "about 16 s".
  - **22:12:33:** dispatch logs `HikariPool-1 - Connection is not available, request timed out after 3000ms (total=10, active=10, idle=0, waiting=0)`.
  - **From 22:12:33:** order logs `Failed to check dispatch capacity` (1 × 500, then 75 × `Connection refused`).
  - **22:13:38:** kubelet kills the dispatch container on its liveness probe.
  - **22:14:42:** the JVM restarts.
  - **22:16:34:** `Started DispatchServiceApplication in 120.9 seconds`.
  - **22:16:44:** last order failure.
  - **22:16:56:** first committed order.
- **Outage = 22:12:20 → 22:16:44 (4 min 25 s). Only 2 min 17 s of it is inside [t1, t2); 2.3 min falls in cooldown.** The copy had ended, but dispatch was still starting.
- In the outage, 0 orders were committed (normal: 1444 created and 1444 committed). There were 81 failure log lines and 7 sampled `POST /api/orders` 503 spans (p50 614 ms). Normal: 149 × 200, 0 × 5xx.
- **Things that should keep working do keep working:**
  - Restaurant: 66 × 200 in the window.
  - Other services: 0 × 5xx in every phase.
  - Order rows are still written to `orders` during the copy: 5604580–5604584 were created, then rolled back after the dispatch failure.

### 2.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| DPM `root@localhost` `copy to tmp table` with growing query time, plus dispatch sessions `Waiting for table metadata lock` | **present** | See 2.1 (25 s → 88 s). Only 2 snapshots fall inside the copy. |
| "(after dispatch restarts) waiting sessions from the old pod IP" | **not applicable** | The kill was a container restart in the same pod, so the IP is still 10.244.2.41. No snapshot was taken between the restart and the end of the copy. |
| `dpm.mysql.session.active_session` rises from 1 | **present** | **12** at 22:12:38 and 22:13:40. Normal mean 1.05 (max 2). |
| `dpm.mysql.instance.data_file_write_bytes` rises | **present** | 1.8 MB and 5.3 MB/interval at 22:12:38 and 22:13:40, and 4.0 MB at 22:14:43. Normal mean 0.24 MB (max 0.43 MB). |
| fooddelivery table and index counts unchanged | **present** | 16 and 31 throughout. |
| order ERROR `Failed to dispatch courier ... I/O error on POST request ... Read timed out` | **partial: wrong text** | **0 lines with `Read timed out` or `I/O error`.** The 9 actual lines say `Failed to dispatch courier for order N: Error while extracting response for type [DispatchResponse] and content type [application/octet-stream]`, failing exactly 5 s after each attempt. The behaviour (read timeout) matches; the quoted string does not. |
| Then `Failed to check dispatch capacity` and 503 `Dispatch service unavailable` | **present** | 76 capacity failures (1 × 500, 75 × Connection refused) and 5 fan-out 503s. |
| dispatch `HikariPool-1 - Connection is not available` | **present** | 3 rows at 22:12:33. There are also 13 `DataSource health check failed`. |
| KCM testbed-dispatch Unhealthy and Killing, with no rollout or new ReplicaSet | **present** | Readiness and liveness `context deadline exceeded` from 22:12:38. `Killing ... failed liveness probe, will be restarted` at 22:13:38. Pulled/Created/Started with the **same image `:latest`** at 22:14:08. No ScalingReplicaSet. `container_restart_count` 3 → 4 at 22:14. |
| No 1146, no failing statements, reads are fast | **present (absence confirmed)** | 0 lines with 1146. The dispatch errors are pool timeouts only. |
| Other tables on the instance keep taking writes | **present** | Order INSERTs succeed during the copy (see 2.2). Commerce and banking are unaffected. |

Metadata inaccuracies:
1. The order error string is not `I/O error ... Read timed out` (see the table).
2. "Service comes back by itself when the copy ends": here it came back about 2.5 minutes **after** the copy ended, because the liveness kill at 22:13:38 forced a 2-minute JVM start.

### 2.4 Incident seed: YES (short window)
- **22 anomaly opens in [t1, t2)**:
  - 15 stream-anomaly on MySQL-fooddelivery at 22:12:38 (redo_write_bytes, row_examined, data_file_write_bytes, redo_wait_count, and others).
  - 4 log-anomaly on dispatch (`DataSource health check failed`, `SQL Error: 0`).
  - 2 on order (fan-out failed).
- Another 12 fall in cooldown.
- The normal phase is noisy: 952 opens, 7.9/min, mostly host and switch metrics (dev-svr-200-57, tb-w2, N2024 switches). The seed stands out by target, not by volume.
- `incidents.csv` has 1 active incident: "food-delivery-dispatch 커넥션 풀이 포화…" (the dispatch connection pool is saturated), events 22:12:34–22:16:08.

### 2.5 Contamination and clocks
- **Nothing unrelated overlaps.** Non-food ERROR and WARN logs are baseline noise at normal rates.
- **Prior history in KCM counters.** The KCM Unhealthy counters for this pod already stood at 23 (liveness), 40 (readiness) and 97 (startup), and Killing at count 4. Restart count was 3 at capture start. These are aggregated events from before the capture on the same long-lived pod; a model reading the counts could infer a flapping history.
- **Clocks:**
  - Logs fall inside spans 100% of the time.
  - The DPM copy start (about 22:12:13) matches the first order stall (22:12:15).
  - The KCM kill (22:13:38) matches the pending series ending at 22:13:38.
  - KCM lag: p50 1.6 s, max 2.8 s.

### 2.6 Verdict: **USABLE-WITH-CAVEATS**
The DDL session and the metadata-lock waiters are directly visible in DPM, and the impact chain (Hikari 10/10, liveness kill, orders 503) is complete.

Caveats:
1. **[t1, t2) is only 2 min 17 s and covers half of the outage.** Evaluation windows or seeds clipped at t2 miss the restart and recovery half.
2. The order error text differs from the metadata.
3. Only 2 DPM snapshots inside the copy.
4. Low traffic: 7 sampled order 503 spans.

Root target: clearly identifiable. It is **MySQL-fooddelivery `target:c8c558e5-711f-4e6e-b00f-1adeb097c795`**: the `root@localhost` DDL session on `fooddelivery.dispatch_events`. The symptom service is food-delivery-dispatch `1f1eb9b2-…` (pool and restart).

---

## 3. case-f63-p-v3-ddf34faa: dispatch release 1.9.0 leaks one parked thread per lookup, leading to a container OOM

Phases:
- normal 03:28:00–05:28:04
- buffer to 05:38:26
- **injection 05:38:26–05:52:43** (KST 14:38–14:52)
- cooldown to 06:22:43

Preflight `clean`; diurnal band 9 rps. Companion: about 10 rps of delivery list and detail.

### 3.1 Injection executed: YES
- **KCM, 05:38:45:**
  - `Scaled up replica set testbed-dispatch-f695b7d7f to 1`.
  - Pod rbfll: `Pulled: Container image "food-delivery-dispatch:1.9.0" already present on machine` (tb-w3).
- **KCM, 05:41:25:** old pod zn9df killed and its RS scaled to 0.
- **Rollback:** baseline pod tm5vh (`:latest`) at 05:50:36; release RS scaled to 0 at 05:52:26.
- **`kcm_resources_history`:** the RS and pod f695b7d7f carry `food-delivery-dispatch:1.9.0`; 597bfbc784 carries `:latest`.
- **syslog, tb-w3:**
  - 05:38:39 `ctr -n k8s.io images import -`.
  - Cooldown 05:52:45 `ctr images rm docker.io/library/food-delivery-dispatch:1.9.0`.
  - These are the executor's own footprint and are also forwarded into `lucida_logs` by the SMS agent.

### 3.2 Load ran and impact is visible: YES (one OOM window)
- **Load:**
  - food `Created|Rejected` logs: normal 60.7/min (largest gap 9 s), injection 56.9/min (largest gap 37 s, during the outage).
  - Dispatch SERVER spans: 16–18/min outside injection, **62.5/min** in injection (the companion).
  - Restaurant spans: largest gap 8.8 s.
- **Impact:** order failures come in two bursts.
  - 05:41:25–05:41:38: 13 failures at the rollout switchover (cold start of the release pod).
  - **05:50:09–05:52:36: 72 failures** (the OOM, the restart, then the rollback). Orders committed per minute: 70 at 05:49, 13 at 05:50, **0 at 05:51**, 23 at 05:52, 72 at 05:53.
  - Order 503 spans: 12 in injection, 0 in every other phase.
- **Things that should keep working do keep working:**
  - Restaurant, payment and all other services: 0 × 5xx.
  - MySQL `active_session` mean 1.07 in injection (normal 1.03).
  - Delivery tracking fails briefly: 4 × 504 on `GET /api/deliveries` (p50 2.6 s).

### 3.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet for testbed-dispatch, plus `Pulled ... "food-delivery-dispatch:1.9.0"` | **present** | 05:38:45 (see 3.1). All other dispatch Pulled records show `:latest`. |
| Non-daemon `waiting` threads (`jvm.thread.count`, daemon=false, state=waiting) climb in a straight line to thousands | **present** | rbfll: 1 (05:41:19) → 32 → 132 → … → **5,465 at 05:49:59**, about 10.5/s, matching the lookup rate. The baseline pod zn9df sits at 1 (normal mean 1.001, max 2). |
| Heap stays flat while daemon threads stay near usual | **mostly present** | Heap total 64 → 109 → 131 MB (sawtooth), against container working set 120 MB → 1.21 GB. Daemon timed_waiting threads stay at 47–49. |
| tb-w3 kernel lines for the dispatch pod's memcg: `memory: usage ... limit 1048576kB`, kernel_stack tens of MB, `Memory cgroup out of memory: Killed process ... (java)` | **present** | 05:50:09 `http-nio-8082-e invoked oom-killer`. 05:50:14 `memory: usage 1048576kB, limit 1048576kB, failcnt 179` and `kernel_stack 92356608` (88 MB). 05:50:18 `Memory cgroup out of memory: Killed process 2323141 (java) ... anon-rss:872836kB`, plus containerd `TaskOOM`. |
| Container restart of the same pod (Pulled and Created without Scheduled), with Unhealthy timeouts and connection refused before it | **partial** | Pulled cnt 2 and Created cnt 2 at 05:50:10 with no Scheduled; `container_restart_count` 0 → 1 (05:50). **No Unhealthy event before the OOM:** the only rbfll Unhealthy events are the startup-probe failures at 05:38:50–05:40:35. No "Started cnt 2" event. |
| New `service.instance.id` in dispatch logs | **present** | b24ad572… (05:39:22–05:50:08), then 16bf7ae3… (05:50:41–05:52:25). |
| Thread count drops after the restart and climbs again | **partial** | Back to 1 at 05:51:23, then 28 → 100 at 05:52:23–27. The rollback killed the container about 1.5 minutes later. |
| `Failed to check dispatch capacity: I/O error ... Connection refused` and 503 only in these windows | **present** | 69 Connection refused, plus 3 Read timed out, 2 Connect timed out, 1 Connection reset. All in injection; 0 in other phases. |
| Restaurant, payment and MySQL have no rollout, restart or errors | **present** | Confirmed (see 3.2). |
| "Cycle repeats every 11–14 min" | **not observable** | Only 1 OOM. The rollback began 18 s after it (05:50:36). |
| Brief slowdown before the kill (lookups up to 21 s) | **absent** | No slow-lookup WARN between 05:42 and 05:50. The 38 `delivery list/lookup did not finish within 2000ms` WARNs are at 05:41:36–39 (cold start) and 05:52:23–25 (shutdown during the rollback). |

Metadata accuracy:
- `kcm.pod.container_oom_killed` stays **0** for rbfll even though the kernel log shows the OOM kill. Only syslog proves it.
- The "repeats" and "stall before the kill" parts are not exercised in this run.

### 3.4 Incident seed: YES (symptom-side only)
- **29 anomaly opens in [t1, t2)**:
  - MySQL-fooddelivery 10 (DML and redo counters)
  - food-delivery-order 7 (new_template `Failed to check dispatch capacity`, surge)
  - dispatch 3 (`HikariPool Interrupted`, `SQL Error`)
  - baseline noise: ledger 4 (`MISMATCH`), core-banking-api 3 (`Insufficient balance` surge)
- **No alarm fired on dispatch thread count, JVM memory or pod memory at any point**, so the leak itself is not seeded.
- Normal phase: 912 opens (7.6/min), mostly host and switch metrics.
- `incidents.csv` has 2 active incidents on food-delivery-order "dispatch 호출 실패" (failed calls to dispatch), from 05:50:19.

### 3.5 Contamination and clocks
- **No unrelated incident.**
- **Harness footprint** in syslog and `lucida_logs`:
  - `journalctl -u loadgen-commerce` on tb-runner at 05:38:19 (buffer).
  - `ctr images import/ls/rm` on tb-w3.
- **Baseline noise:** ledger MISMATCH and banking-api insufficient-balance errors, at normal rates.
- **Clocks:**
  - Logs fall inside spans 100% of the time.
  - Kernel OOM (05:50:18), JVM metric reset (05:50:33), new instance id (05:50:41) and KCM restart (05:50:10) agree within seconds.
  - `kcm.pod.*` for tb-w3 runs about 60–90 s early (pod rbfll series 05:38:00–05:51:00 against APM 05:39:09–05:52:27). That is within the minute-floor tolerance.

### 3.6 Verdict: **USABLE-WITH-CAVEATS**
The release rollout, the straight-line climb of non-daemon parked threads to 5,465, and the kernel memcg OOM kill of java are all unambiguous.

Caveats:
1. Only one OOM and restart cycle, and the rollback started 18 s later. The "loop" in the label is not shown, and the user impact lasts about 2.5 minutes.
2. No alarm on the leak signals. The seed is order logs and MySQL counters.
3. `kcm.pod.container_oom_killed` = 0 contradicts the kernel log.
4. No Unhealthy or stall before the kill.

Root target: clearly identifiable. It is **food-delivery-dispatch `target:1f1eb9b2-a368-4a70-a003-5fe70b1a983b`**, Deployment `testbed-dispatch` / ns `rca-testbed-food`, release RS testbed-dispatch-f695b7d7f (image 1.9.0).

---

## 4. case-f64-p-v3-7b180ead: banking api env rollout turns on transferClient slow-call blocking (threshold 100 ms)

Phases:
- normal 04:51:27–06:51:37
- buffer to 07:01:49
- **injection 07:01:49–07:13:45** (KST 16:01–16:13)
- cooldown to 07:43:45

Preflight `clean`; diurnal band 10 rps. Companion: 10 rps banking surge.

### 4.1 Injection executed: YES
- **KCM:**
  - 07:01:52 `Scaled up replica set testbed-api-56b7f7f69f to 1`; pod xq52m starts on tb-w2.
  - 07:03:18 old RS 7bd465dcd9 scaled to 0 and pod jd6jv killed.
  - Rollback: 07:12:15 7bd465dcd9 scaled up again; 07:13:40 56b7f7f69f scaled to 0.
- **`kcm_resources_history`:** RS testbed-api-56b7f7f69f carries `SPRING_APPLICATION_JSON={"resilience4j":{"circuitbreaker":{"instances":{"transferClient":{"slow-call-duration-threshold":"100ms","slow-call-rate-threshold":50…`. RS 7bd465dcd9 has no such entry.

### 4.2 Load ran and impact is visible: YES
- **Load:**
  - account logs: normal 24.8/min (largest gap 21 s), injection 80/min (largest gap 6 s).
  - api, account and transfer spans: largest gap 6–18 s in injection.
- **Release pod served 07:03:18–07:13:40.** api `GET /api/transfers` in that window:
  - **182 × 502 (85.4%)**, p50 605 ms.
  - 31 × 200, p50 929 ms.
  - Normal: 692 × 200, 0 × 5xx.
  - Both figures match "about 85% … after about 0.6 s".
- **Things that should keep working do keep working:**
  - api `POST /api/transfers`: 84 × 200 and 5 × 400. The 400s are insufficient-balance responses, also seen in normal.
  - transfer `POST /api/transfers`: 157 × 200, 0 × 5xx.
  - account (balance): 57.7 spans/min, 0 × 5xx.
  - commerce-payment: 0 × 5xx.
  - No other service has a 5xx.

### 4.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet for testbed-api, and the new RS spec carries SPRING_APPLICATION_JSON with transferClient 100 ms | **present** | See 4.1. |
| Within about a minute, api ERROR `Transfer service circuit open/exhausted: CircuitBreaker 'transferClient' is OPEN …` | **present** | 5,309 OPEN plus 524 HALF_OPEN rows, all in injection (0 elsewhere). First at 07:03:26, 8 s after the release pod took traffic. About 470–550/min through 07:13:40. |
| No `Transfer service list call failed` (I/O error, Read timed out, 5xx) from api, and no ERROR from transfer | **present (absence confirmed)** | 0 `list call failed`. transfer has 0 WARN and 0 ERROR in every phase. |
| transfer stays Ready and answers history with 200 "in its usual 240–250 ms" | **partial: wrong number** | Ready: no KCM events for transfer and no restart. All 200. But transfer `GET /api/transfers` p50 is **462 ms in normal** and 754 ms in injection, not 240–250 ms. The api CLIENT span to transfer is also 462 ms in normal. It is still far above the 100 ms threshold, so the mechanism holds. |
| api keeps logging `Submitting transfer` with no account or transfer failure | **present** | 72–102 per minute in injection (19–30 in buffer). The only account failures are the baseline `Insufficient balance` 400s (normal 108, injection 55). |
| Balance checks via nginx → account healthy | **present** | account 0 × 5xx. |

Metadata inaccuracy: the transfer history latency figures (240–250 ms, p5 239.6 ms) do not match this capture (p50 462 ms).

### 4.4 Incident seed: YES (thin)
- **13 anomaly opens in [t1, t2)**:
  - core-banking-api 3: two log surges, plus one `new_template` at 07:03:27 naming the transferClient breaker.
  - Oracle-corebanking 5 (volume metrics from the companion load).
  - ledger 4 (baseline MISMATCH).
  - account 1.
- The normal phase is busier: 682 opens, 5.7/min, mostly hosts and switches.
- `incidents.csv` has 4 core-banking-api incidents created 07:10–07:12.

### 4.5 Contamination and clocks
- **No overlapping injection.**
- **Confusable baseline noise on the root service.** core-banking-api logs `Account service circuit open/exhausted for order null: … Insufficient balance` throughout normal (108). The platform raised recurring log-surge anomalies on it (04:52 … 06:46). Two "core-banking-api 오류가 잇따르고" (errors keep occurring in core-banking-api) incidents were built partly from those normal-phase events (first events 04:47:14 and 06:46:05) and are still `active`.
  - The text pattern `circuit open/exhausted` is the same as in the decisive line, but the breaker and cause differ (accountClient with 400 against transferClient OPEN).
- **Clocks:**
  - Logs fall inside spans 100% of the time.
  - KCM lag: p50 1.8 s, max 3.1 s.
  - **`kcm.pod.*` metrics for tb-w2 pods are about 7 minutes early.** Release pod xq52m: kcm series 06:56–07:06 against APM 07:02:15–07:13:35. Rollback pod lszbx: 07:06 against 07:12:39. In VM, the release pod appears **before t1 (in buffer)**.

### 4.6 Verdict: **USABLE-WITH-CAVEATS**
The rollout with the transferClient slow-call config, 5.8k open-breaker errors with no downstream failure, history 0% → 85% 502, and healthy transfer, account and settlement are all present.

Caveats:
1. The metadata latency numbers are wrong (462 ms, not 240–250 ms).
2. tb-w2 `kcm.pod.*` timestamps are about 7 minutes early for the root pod.
3. The same service carries similarly worded baseline `circuit open/exhausted` noise, and pre-existing api incidents.
4. The seed is thin: 3 api events.

Root target: clearly identifiable. It is **core-banking-api `target:b3d586c3-4443-4581-8f2e-be12f3504448`**, Deployment `testbed-api` / ns `rca-testbed-banking`, RS testbed-api-56b7f7f69f (env change).

---

## 5. case-f64-r-v3-9014da8a: commerce gateway env rollout turns on order-breaker slow-call blocking (threshold 30 ms)

Phases:
- normal 16:33:22–18:33:26
- buffer to 18:43:37
- **injection 18:43:37–18:55:38** (KST 03:43–03:55)
- cooldown to 19:25:38

Preflight `clean`; diurnal band 2 rps. Companion: 8 rps commerce surge (50% checkout).

### 5.1 Injection executed: YES
- **KCM:**
  - 18:43:40 `Scaled up replica set testbed-gateway-7b75c5dc54 to 1` (pod hxltc, tb-w1).
  - 18:45:21 old RS 579dd66578 scaled to 0 and pod c4fjz killed.
  - Rollback: 18:53:47 579dd66578 scaled up; 18:55:32 7b75c5dc54 scaled to 0.
- **`kcm_resources_history`:** RS 7b75c5dc54 carries `SPRING_APPLICATION_JSON … "order":{"slow-call-duration-threshold":"30ms","slow-call-rate-threshold":50`. 579dd66578 has none.

### 5.2 Load ran and impact is visible: YES
- **Load:**
  - commerce-gateway spans: largest gap 5.2 s in injection (16.5 s in normal).
  - `Cleared cart after checkout`: normal 14.0/min, injection 48.3/min (companion).
- **Release pod served 18:45:21–18:55:32.** Gateway `POST /api/orders/**`: **217 × 502 (90.0%)**, p50 307 ms, and 24 × 200. Gateway 5xx in normal: 0.
  - This matches "85–90% … about 0.3 s".
  - Checkouts reaching order drop from 138/min (18:44, before the switch) to about 33/min (18:46–18:54).
- **Things that should keep working do keep working:**
  - products: 370 × 200 / 107 × 404
  - carts: 275 POST × 200, 252 DELETE × 200
  - users: 119 × 200
  - order, payment and banking: 0 × 5xx

### 5.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| testbed-gateway rollout (KCM ScalingReplicaSet) whose new RS spec carries SPRING_APPLICATION_JSON with order slow-call 30 ms | **present** | See 5.1. |
| Within about a minute, gateway WARN `Downstream unavailable for POST /api/orders/checkout: … CallNotPermittedException: CircuitBreaker 'order' is OPEN …` | **present** | 4,373 OPEN plus 16 HALF_OPEN, injection only. First at 18:45:25, 4 s after the switch. About 410–490/min. |
| No `I/O error` or `Read timed out` from the gateway, no ERROR from order | **present (absence confirmed)** | 0 and 0. |
| Order keeps logging `Cleared cart after checkout`, answers 200 "in its usual 37–50 ms", stays Ready | **partial: wrong number** | About 33/min during the release window, all 200, no KCM events for order. But order `POST /api/orders/checkout` p50 is **70 ms in normal** and 55 ms in injection; the gateway CLIENT span to order is 71 ms (normal) and 63 ms (window). Not 37–50 ms, though still above the 30 ms threshold. |
| Other routes and breakers healthy | **present** | See 5.2. |

Metadata inaccuracy: the order-call latency figures (at least about 37 ms, p50 47 ms) do not match this capture (p50 about 63–71 ms).

### 5.4 Incident seed: YES (mostly load-driven)
- **30 anomaly opens in [t1, t2)**:
  - 14 log `surge` alarms at 18:44:01–18:44:10 on *success* templates: `Cleared cart`, `Transfer COMPLETED`, notification, shipment, ledger. These are caused by the companion load, not the fault.
  - 15 stream-anomaly on PostgreSQL-commerce and Oracle volume metrics.
  - **1 alarm naming the fault:** a log-anomaly at 18:45:26 on commerce-gateway, `Downstream unavailable … CircuitBreaker 'order'`.
- Normal: 84 opens, 0.70/min.
- `incidents.csv` has 1 active incident: "commerce-gateway 오류가 늘어나면서…" (errors are rising in commerce-gateway). It was created 18:58 and also groups the hourly 17:33 and 18:33 trace-anomaly `distribution_shift` events from normal.

### 5.5 Contamination and clocks
- **No overlapping injection.**
- **Baseline quirk:** the commerce baseline k6 `cart add 200` and `cart remove 200` checks fail 100% in every hour (k6 summaries in tb-runner syslog). The gateway returns 401 on `POST /api/carts/**`: 116 in normal, 79 in window.
  - This exists before and after the fault and is not 5xx, but a model may notice the 401s.
- **Clocks:**
  - Logs fall inside spans 100% of the time.
  - KCM lag: p50 1.4 s, max 3.0 s.
  - Gateway pods are on tb-w1; their `kcm.pod.*` runs 12–85 s early (minute-floor tolerance).

### 5.6 Verdict: **USABLE**
The rollout with the order-breaker slow-call config is in the RS spec, 4.4k open-breaker WARNs appear with no downstream failure, checkouts go from 0% to 90% 502 while order stays healthy, and nothing else overlaps.

Recommended metadata fix: replace the 37–50 ms latency figure. The seed is dominated by companion-load success-template surges, with only 1 gateway alarm.

Root target: clearly identifiable. It is **commerce-gateway `target:e9f5f307-9ebd-4403-8461-164b989e5d62`**, Deployment `testbed-gateway` / ns `rca-testbed-commerce`, RS testbed-gateway-7b75c5dc54 (env change).

---

## 6. case-f65-r-v3-b5383723: transfer rollout re-applies a stale `DB_SERVICE=XEPDB1`, causing ORA-12514 and no endpoints

Phases:
- normal 21:39:04–23:39:06
- buffer to 23:49:23
- **injection 23:49:23–00:07:08** (KST 08:49–09:07)
- cooldown to 00:37:08

Preflight `clean`; diurnal band 4 rps. Companion: 5 rps transfer-heavy surge.

### 6.1 Injection executed: YES
- **KCM, 23:49:25** (same second):
  - `Scaled down replica set testbed-transfer-8c7fb57cb to 0 from 1`, plus Killing of x8fx5.
  - `Scaled up replica set testbed-transfer-68464f78c5 to 1`.
  - That is maxSurge 0: the old pod goes first.
- **Rollback:** 00:04:33 (8c7fb57cb back, 68464f78c5 to 0).
- **`kcm_resources_history`:** RS 68464f78c5 env has `{"name":"DB_SERVICE","value":"XEPDB1"}`. RS 8c7fb57cb has no `DB_SERVICE`.

### 6.2 Load ran and impact is visible: YES
- **Load:**
  - account logs: normal 10.5/min (largest gap 63 s), injection 140.8/min (largest gap 7 s).
  - api and account spans: largest gap 15–22 s in injection.
  - Transfer spans go silent: gap of 260 s while transfer has no endpoints.
- **Outage 23:49:26 → 00:07:01:**
  - first `Connection refused` at 23:49:26.5;
  - last at 00:07:01;
  - first `Transfer COMPLETED` at 00:07:08.9, exactly at t2.
  - The whole outage is inside [t1, t2).
- In the outage, every request in the failing classes failed:
  - api `POST /api/transfers`: **211 × 502**, p50 604 ms
  - api `GET /api/transfers`: **113 × 502**
  - account `POST /api/accounts/transfer`: 56 × 502
  - commerce-gateway checkout: 21 × 502, p50 362 ms
  - commerce-payment `POST /api/payments`: 20 × 502
  - Normal: 0 × 5xx in all of these.
- **Things that should keep working do keep working:**
  - account `GET /api/accounts/{id}`: 280 × 200 (p50 3 ms).
  - account `GET /api/accounts`: 70 × 200.
  - Food and other commerce routes: 0 × 5xx.

### 6.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM rollout (old RS to 0 and new RS to 1 in the same second), and the new RS spec carries `DB_SERVICE=XEPDB1` | **present** | See 6.1. |
| Transfer starts normally, then WARN `SQL Error: 12514, SQLState: 08006` and ERROR `ORA-12514 … Service XEPDB1 is not registered with the listener` | **present** | 747 lines mentioning ORA-12514 or XEPDB1 (164 WARN `SQL Error: 12514`, about 235 ERROR, plus INFO `throwing`), 23:51:02–00:04:26. 0 before. Also HHH000342 `Could not obtain connection to query metadata`, 71 `Unexpected error occurred in scheduled task`, and 76 `DataSource health check failed`. |
| "Within about 30 s" of start | **partial: timing** | Container started at 23:49:26, `Starting TransferServiceApplication` at 23:50:05, first ORA-12514 at 23:51:02 (96 s), `Started … in 132.7 seconds` at 23:52:10. |
| KCM Unhealthy `Startup probe failed: … 503` or context deadline exceeded | **present** | 22 × `connection refused` (cnt 1–22), then `context deadline exceeded` at cnt 28 (23:54:27), cnt 52 (23:59:27) and cnt 79 (00:04:27). Never `statuscode: 503`. |
| KCM Killing `failed startup probe, will be restarted` | **absent in KCM** | No such event. The only Killing is the old pod at 23:49:25. The restarts are proven by the logs instead: `Starting TransferServiceApplication` at 23:50:05, 23:55:13 and 00:00:07, then the rollback pod at 00:05:05. `container_restart_count` reaches 2. |
| account and api log `Connection refused` to `http://testbed-transfer:8082` | **present** | 2,104 lines, 23:49:26–00:07:01. For example, `Transfer service list call failed: I/O error on GET request for "http://testbed-transfer:8082/api/transfers": Connection refused` (536) and `Transfer service call failed … POST … Connection refused` (496). |
| account keeps answering balance lookups; no other banking service logs ORA errors | **present** | Balance lookups: see 6.2. ORA lines come only from core-banking-transfer. |
| "ledger keeps writing" | **inaccurate** | Ledger is healthy (no errors; reconciliation batch still runs) but **idle**: 3 `Recorded outbox event for ledger` in injection against 29/min in normal, because no transfers complete. |
| Commerce checkout fails with 502 because the settlement transfer is refused | **present** | commerce-order logs 337 `Checkout payment failed … Payment service unavailable`. commerce-payment logs `Settlement banking transfer failed … I/O error on POST`. |

Metadata inaccuracies:
1. "ledger keeps writing" (it goes idle).
2. ORA-12514 starts about 96 s after container start, not about 30 s.
3. There is no KCM Killing event for the startup-probe restarts.

### 6.4 Incident seed: YES (strong)
- **48 anomaly opens in [t1, t2)** (2.7/min against 1.22/min in normal):
  - core-banking-transfer 15 (new_template `HHH000342`, `SQL Error`, `Response delay control unavailable`, `DataSource health check failed`)
  - core-banking-api 14
  - core-banking-account 8
  - Oracle 4
  - commerce-order and commerce-payment 5
- `incidents.csv` has active incidents titled:
  - "core-banking-transfer의 DB 연결 실패로 인해…" (because of the DB connection failure in core-banking-transfer…)
  - "Oracle-corebanking 연결 오류로 인해 core-banking-transfer…" (because of the Oracle-corebanking connection error, core-banking-transfer…)

### 6.5 Contamination and clocks
- **No overlapping injection.**
- **Read-only operator commands in normal:** 21:44:54–21:45:10 `kubectl get nodes/pods`, `kubectl top pod -A`, and `kubectl exec testbed-mysql-0 … cat memory.current` on tb-cp. They are harmless but visible in syslog and `lucida_logs`.
- **Pre-existing platform incidents built from normal-phase noise:**
  - "commerce-payment 에서 오류가 잇따르고" (errors keep occurring in commerce-payment), first event 21:33:07 (the hourly trace distribution_shift).
  - "core-banking-api 잔액 부족 관련 로그…" (logs about insufficient balance in core-banking-api), 23:32–23:45 (baseline).
- **Clocks:**
  - Logs fall inside spans (account 98.2%, min −0.41 s; all others 100%).
  - KCM events and app logs agree. The old pod's logs stop at 23:49; the new JVM starts at 23:50:05; APM series for q6kzf start at 23:49:49.
  - **Metric skew:** `kcm.pod.*` for tb-w2 is about 6.5–7 minutes early.
    - q6kzf kcm series runs 23:43–23:57, against the real pod life of 23:49:26–00:04:33.
    - x8fx5 ends at 23:42, against its kill at 23:49:25.
    - `container_restart_count` therefore shows increments at **23:48 (buffer, before t1 and before the pod existed)** and 23:53, where the real restarts were at about 23:54:30 and 23:59:30.

### 6.6 Verdict: **USABLE-WITH-CAVEATS**
The rollout with the explicit `DB_SERVICE=XEPDB1`, transfer's own ORA-12514 naming XEPDB1, empty endpoints with Connection refused in api and account, and healthy balance lookups on the same Oracle are all present. The outage sits entirely inside the window.

Caveats:
1. **tb-w2 `kcm.pod.*` metrics are about 7 minutes early.** Pod existence and restart increments appear before the rollout, which could mislead VM-based timeline reasoning.
2. Metadata inaccuracies: ledger idle, not writing; ORA starts at about 96 s, not 30 s; no KCM Killing event.

Root target: clearly identifiable. It is **core-banking-transfer `target:f0ce5940-a7e2-44a2-843f-196de379d898`**, Deployment `testbed-transfer` / ns `rca-testbed-banking`, RS testbed-transfer-68464f78c5 (env `DB_SERVICE=XEPDB1`). Oracle-corebanking (`00149c2f-…`) is healthy and is not the root.

---

## Cross-case checks
- **Load generator: never stopped.**
  - The baseline k6 runs restart every hour at about :33. Its progress and summary output is in `syslog_local` (host tb-runner). Restarts cause no gap: the largest gaps between load logs in injection are 3–12 s, except where the service is down by design.
  - Companion loads are visible as rate increases: dispatch ×3.4 (F63-P), banking api and account ×3 (F64-P), commerce checkout ×3.5 (F64-R), banking ×13 log volume (F65-R).
  - No companion script name appears in syslog.
- **Unrelated incidents:** none overlap any window. Recurring baseline noise is present in every case at normal rates:
  - core-banking-api `Insufficient balance` (400) with the same `circuit open/exhausted` wording
  - ledger `Reconciliation MISMATCH`
  - containerd snapshot errors
  - commerce cart 401s (k6 cart checks fail 100% every hour)
  - hourly trace distribution_shift alarms
  - Some platform incidents in `incidents.csv` group these normal-phase events and stay `active` through injection: F64-P (api) and F65-R (commerce-payment, api).
- **Harness footprint** (forwarded into `lucida_logs` by the SMS agent):
  - `ctr images import/rm` on tb-w3 (F63-P)
  - `journalctl -u loadgen-commerce` on tb-runner at about t1 (F63-P, F64-P, F64-R, F65-R)
  - read-only kubectl on tb-cp (F65-R normal)
- **Clocks:**
  - Logs and spans agree in every case: logs fall inside their span 98–100% of the time.
  - KCM collected-minus-event lag: p50 1.4–1.8 s, max 3.4 s.
  - DPM and APM metrics line up with the logs within seconds.
- **Systematic skew in `kcm.pod.*` VM metrics from tb-w2 (all banking pods), in all six captures.**
  - Each banking pod's series ends 376–461 s before its APM series and before capture end. Pods on tb-w1 and tb-w3 end 12–87 s early (minute floor).
  - tb-w2 syslog timestamps are correct, so the offset is in the KCM metric pipeline for that node, not in the host clock.
  - It matters for F64-P and F65-R (banking roots). It should be fixed at the collector, or the affected series shifted by about +7 minutes, before relying on kcm.pod timelines for banking.
- **Seed strength:** the platform alarm stream is noisy in F60-R, F63-P and F64-P (5.7–7.9 opens/min in normal, mostly host and switch metrics). In those cases the seed is distinguished by target, not by volume.

## Summary
| Case | Verdict | Root target | Key caveat |
|---|---|---|---|
| case-f60-p-v3-04d37516 | **USABLE** | MySQL-fooddelivery DB `target:c8c558e5-711f-4e6e-b00f-1adeb097c795` (table `fooddelivery.dispatch_outbox_events`) | None material. The DDL text is not captured; DPM table count 16→17 and index count 31→34 prove the trigger. |
| case-f60-r-v3-22036218 | **USABLE-WITH-CAVEATS** | MySQL-fooddelivery DB `target:c8c558e5-711f-4e6e-b00f-1adeb097c795` (root@localhost `copy to tmp table` on `dispatch_events`) | [t1, t2) is only 2 min 17 s and holds half the 4 min 25 s outage; the rest is in cooldown because of the dispatch liveness kill and a 2-minute JVM start. The order error text is `Error while extracting response …`, not `I/O error … Read timed out`. Only 2 DPM snapshots inside the copy. |
| case-f63-p-v3-ddf34faa | **USABLE-WITH-CAVEATS** | food-delivery-dispatch `target:1f1eb9b2-a368-4a70-a003-5fe70b1a983b` + Deployment `testbed-dispatch` / ns `rca-testbed-food` (RS f695b7d7f, image 1.9.0) | One OOM only (05:50:18), with the rollback 18 s later, so the repeat loop is not shown and impact is about 2.5 minutes. No alarm on the thread or memory signals. `kcm.pod.container_oom_killed` stays 0 despite the kernel OOM line. No Unhealthy or stall before the kill. |
| case-f64-p-v3-7b180ead | **USABLE-WITH-CAVEATS** | core-banking-api `target:b3d586c3-4443-4581-8f2e-be12f3504448` + Deployment `testbed-api` / ns `rca-testbed-banking` (RS 56b7f7f69f, SPRING_APPLICATION_JSON) | The metadata latency is wrong (transfer history p50 462 ms, not 240–250 ms). tb-w2 `kcm.pod.*` is about 7 minutes early, so the release pod appears in buffer. Baseline `Account service circuit open/exhausted` noise and pre-existing api incidents sit on the same service. The seed is thin (3 api events). |
| case-f64-r-v3-9014da8a | **USABLE** | commerce-gateway `target:e9f5f307-9ebd-4403-8461-164b989e5d62` + Deployment `testbed-gateway` / ns `rca-testbed-commerce` (RS 7b75c5dc54, SPRING_APPLICATION_JSON) | The metadata order-call latency is wrong (p50 about 63–71 ms, not 37–50 ms). The seed is dominated by companion-load success-template surges, with 1 gateway alarm. Baseline cart 401s exist. |
| case-f65-r-v3-b5383723 | **USABLE-WITH-CAVEATS** | core-banking-transfer `target:f0ce5940-a7e2-44a2-843f-196de379d898` + Deployment `testbed-transfer` / ns `rca-testbed-banking` (RS 68464f78c5, env DB_SERVICE=XEPDB1) | tb-w2 `kcm.pod.*` is about 7 minutes early, so restart-count increments show at 23:48, before t1 and before the pod existed. KCM has no `Killing … startup probe` events (restarts are proven by the logs). Metadata says ledger keeps writing, but it goes idle; ORA starts at about 96 s, not 30 s. |
