# Capture verification 2 — batch A (F16-P, F35-P, F36-H, F37-H, F39-H, F41-H)

All work was read-only on `/data/eval-cases`. Times are UTC (KST = UTC+9). Spans are sampled (about 10%), so span counts are a fraction of real traffic; log counts are not sampled. "Normal" is the 120-minute normal phase. "Release window" is the time the faulty pod actually served traffic, taken from the KCM events: from when the old pod was killed to when the release pod was killed. Helper scripts are in `scratchpad/verify2-A/`: `ov.py`, `kcm.py`, `g.py`, `spans.py`, `children.py`, `tl.py`, `vm.py`, `vmsum.py`, `ev*.py`, `k6*.py`, `xcorr.py` and `podspan.py`.

**Cross-case clock finding (read first).** The KCM pod and container metrics (`kcm.pod.*`, `kcm.container.*`) for pods on node **tb-w2** are stamped about **6 minutes early** in all six captures. That node runs all banking pods, including Oracle. Pods on tb-w1 and tb-w3 are not affected. Evidence:
- **Cross-correlation with APM.** Correlating `kcm.pod.cpu_usage` with APM `jvm.cpu.recent_utilization` per pod gives the best lag:
  - tb-w2 pods: **+6 min**, r 0.80–0.998, in every case (F16, F35, F36, F37, F39, F41).
  - tb-w1 and tb-w3 pods: lag 0, r 0.68–0.99.
- **Pod lifecycles.** The F41 release pod was created at 18:45:46, but its `kcm.pod.*` series starts at 18:40:00. The F37 release pod's series sits entirely in the buffer phase (15:59–16:04), before its rollout at 16:04:51.
- **What is not affected.** KCM events, `kcm.replica_set.*`, APM, DPM, logs, spans and syslog are aligned. KCM events lag by p50 1.5–1.9 s (max 4.1 s), and log observed−timestamp is at most 2.7 s.

The shift matters most in F41, where the restart-count series is shifted. It is a minor caveat for F35, F37 and F39.

---

## 1. case-f16-p-v3-e40ec615 — commerce notification-service release 1.3.0 floods user-service with full member-list reads

Phases:
- normal 09:43:45–11:43:53
- buffer to 11:54:09
- **injection 11:54:09–12:03:30 (9.3 min)**
- cooldown to 12:33:30

Preflight `clean`. The release pod served from 11:55:39 to 12:03:20 (7.7 min).

### 1.1 Injection executed: YES (direct)
- KCM events:
  - 11:54:29 `ScalingReplicaSet` `Scaled up replica set testbed-notification-5785dcd4b4 to 1`.
  - Pod `testbed-notification-5785dcd4b4-dpvdl` `Pulled: Container image "commerce-notification:1.3.0" already present on machine` (tb-w1).
  - 11:55:39: the old pod ts84f was killed and RS 65b4ccc9d9 scaled to 0.
  - Rollback: 12:02:19 RS 65b4ccc9d9 scaled back up with `:latest`; 12:03:20 the release pod was killed.
- `kcm_resources_history.csv`: RS and pod 5785dcd4b4 carry `commerce-notification:1.3.0`; 65b4ccc9d9 carries `:latest`.
- syslog on tb-w1 (executor footprint):
  - 11:54:20 `ctr -n k8s.io images import -`
  - 12:03:29 `ctr ... images rm docker.io/library/commerce-notification:1.3.0` (injection phase)
  - 12:03:31 `rm sha256:a4d4…` (cooldown)

### 1.2 Load ran and impact is visible: YES
- **Load:**
  - Largest gap between any two SERVER spans in the capture: 3.5 s.
  - commerce-gateway: 0 empty minutes in every phase; largest gap in injection 2.6 s.
  - k6 baseline progress lines: largest gap 9 s.
  - The 3 rps companion is visible: gateway SERVER spans go from 122.9/min (buffer) to 165.8/min (injection); notification order events from about 0.23/s to 0.84/s.
- **Impact on gateway writes** (normal vs injection):

| Route | Normal | Injection |
|---|---|---|
| POST /api/carts | p50 13 ms | p50 312 ms, p95 3.7 s; **37 × 500** |
| DELETE /api/carts | 0 × 500 | **20 × 500** (p50 4.0 s) |
| POST /api/orders | p50 73 ms | **21 × 500** (p50 4.3 s) |
| POST /api/users/** (login) | 0 errors | 19 × 500, 4 × 502 |
| All gateway 5xx | 1 in 120 min | 101 in 9.3 min |

  - The first gateway 5xx is at 11:56:13, 34 s after the release became Ready.
  - Gateway 5xx come in continuously, 4–11 per 30 s, until 12:03:13.
- **What keeps working:**
  - GET /api/products p50 6.6 ms in both phases; GET /api/categories 4.0 → 4.1 ms; GET /api/carts 3.4 → 3.3 ms.
  - banking and food have 0 × 5xx.
  - notification keeps consuming: 468 order events in injection.

### 1.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet for testbed-notification + `Pulled ... "commerce-notification:1.3.0" already present on machine` | **present** | 11:54:29 (see 1.1). |
| Prior notification RS/pod records carry `:latest` | **present** | kcm_resources_history: 65b4ccc9d9 = latest; only the release 5785dcd4b4 = 1.3.0. |
| notification WARN `Recipient directory refresh failed: I/O error ... Connection refused` | **present** | 24 rows; also 23 `Connect timed out` and 5 `Read timed out`. All in injection, 11:56:36–12:03:19. |
| notification WARN `Recipient directory refresh failed: 500 ...` | **present** | 200 rows (Whitelabel 500 body). |
| `Recipient directory paging stopped at page limit: pages=100` | **absent** | 0 rows. user-service was saturated, so no rebuild ever reached the cap. The metadata calls this the rarer line. |
| DPM Top SQL gains the unfiltered `select u1_0.id,... from user_schema.users u1_0` | **present** | Injection only: 3200 calls and 9,664,000 rows (3020 rows per call). 0 in normal, buffer and cooldown. |
| user-service CPU pinned at its 500m limit | **present** | `kcm.pod.cpu_usage`: 10.2 m in normal, 499.6–500.6 m every minute from 11:57 to 12:03 (utilization_by_limit p50 99.9%). APM recent_utilization p50 0.041 → 0.998. The metadata says the at-rest median is 0.023. |
| notification CLIENT `GET` to testbed-user, and user SERVER `GET /api/users` | **present** | 510 client spans (295 × 200, 197 × 500, 18 timeouts at 10 s) and 508 user SERVER spans, all in injection. At rest user-service serves only verify-token, login and health (0 GET /api/users in normal). |
| New topology edge commerce-notification → commerce-user | **present** | Absent from the 11:50 snapshot. In the 12:00 snapshot: 246 calls, 41.9% errors, p50 4.1 s. |
| notification threads climb from about 40 to over 100 | **present** | Summed jvm.thread.count: old pod 43–47; release pod 98–115 (11:56–12:03). |
| user-service p95 rises from about 2 ms to seconds | **present** | verify-token p50 2.8 → 194 ms, p95 3.9 → 3100 ms; 71 × 500. |
| user Hikari `Connection is not available` | **present** | 3087 ERROR (0 in normal), from 11:56:14. pending_requests mean 0 → 31.8 (max 66); used 10 of 10. |
| user readiness failures | **present** | KCM `Readiness probe failed ... context deadline exceeded` (count up to 34) and `Liveness probe failed` (count 6–13). Health spans show 4 × 503. restart_count stays 2, so there was no restart. |
| Gateway write failures | **present** | See 1.2. SEVERE `HttpServerErrorException` 726 and `ResourceAccessException` 67. |
| user-service has no rollout, config change or lock waits; PostgreSQL has headroom; reads normal | **present** | No KCM rollout for testbed-user. PG blocked_session 0, active_session 1.0 → 1.1, session_utilization 62 → 69%. Reads unchanged (see 1.2). |
| Gateway request volume unchanged (not F07-H) | **partial** | Gateway +15% from the companion. All the new user-service traffic (GET /api/users) comes from notification. |

Metadata claims that do not match the capture:
- The paging-cap line never appears.
- Logins fail mostly with 500 (19), with only 4 × 502 from the circuit.
- The served full-list rate is about 5.7/s (DPM 3200 calls in 9.3 min). That is below "tens to over a hundred per second", although it is still far beyond what user-service could serve.

### 1.4 Incident seed: YES
- 184 events in [t1, t2), of which 99 are opens. 75 of those are change-detect host trait noise, which runs at the same rate in normal. The rest are 14 log-anomaly and 10 stream-anomaly opens.
- Relevant opens:
  - commerce-user `HikariPool ... Connection is not available` new_template and surges (11:56:17).
  - commerce-gateway `Servlet.service()` new_template and surge (11:56:23).
  - commerce-notification `Recipient directory refresh failed` unknown_anomaly (11:56:40) and order-event surges.
  - PostgreSQL-commerce metric anomalies: row_returns 29.5k vs a 17.4k baseline; backend_eviction_count 215 vs 29; insert, commit and index-scan counts.
- One unrelated pair: Oracle bytes sent/received (11:56:01). Banking had no errors.

### 1.5 Contamination and clocks: CLEAN
- **Contamination:**
  - No other injection. 5xx occur only in commerce-gateway and commerce-user.
  - The KCM `Unhealthy` counts on the user pod start at 6 and 11 because the same pod had probe failures on 2026-10-20 (first_timestamp 03:30). That is outside the capture.
  - Baseline noise also present in normal: the commerce k6 baseline fails 44% of requests by design (404 on product variants, 401 on carts); 4–8 core-banking-api `Insufficient balance` ERRORs and commerce-cart SEVERE lines.
- **Clocks:**
  - The event order agrees across sources: KCM release Ready 11:55:39; release app `Started` log 11:55:35; first user GET /api/users span 11:55:47; user CPU minute 11:56; first gateway 5xx 11:56:13; first Hikari error 11:56:14; KCM liveness failure 11:56:26.
  - Neither the root nor the symptom pod is on tb-w2.

### 1.6 Verdict: **USABLE**
The release rollout is in KCM, and the consumer's full-list flood is visible in traces, DPM, threads and the topology edge. user-service saturates without any change of its own, and writes fail while reads stay normal. Nothing overlaps.
- **Root:** commerce-notification **`target:ef0eaed0-045d-48e2-9e3a-280b4fe9b0cc`**, Deployment `testbed-notification` in ns `rca-testbed-commerce` (KCM Deployment target_id 7fc09742-1ba2-4e5d-b154-a66e4a6526b0).
- **Symptom service, not root:** commerce-user `target:252a3a37-77fd-495a-8e12-d79303753fda`.
- **Identifiable:** yes, through the new notification CLIENT spans, the new edge and the rollout.
- **Caveats:** short fault (7.7 min); paging-cap line absent; login code mostly 500, not 502.

---

## 2. case-f35-p-v3-11824edf — DROP TABLE BANKING.ACCOUNTS on the production Oracle

Phases:
- normal 05:59:29–07:59:36
- buffer to 08:09:44
- **injection 08:09:44–08:25:12 (15.5 min)**
- cooldown to 08:55:12

Preflight `clean`. The table was missing from 08:09:51 (first ORA) to 08:25:03 (first COMPLETED after the flashback), about 15.2 min.

### 2.1 Injection executed: YES (by proxy; the DDL itself is not recorded)
- **No direct trace of the DROP.**
  - The DDL runs inside the pod through `kubectl exec ... sqlplus`, and that exec is not in syslog during injection.
  - DPM has no SQL text for Oracle: 0 rows match `DROP`, `FLASHBACK`, `sqlplus` or `ACCOUNTS` in dpm_session or dpm_topsql.
- **Proxy 1 (decisive): ORA-00942 on ACCOUNTS from the first statement.** First at 08:09:51.8, then 540–770 per minute until 08:25:02, then none.
- **Proxy 2: USERS tablespace step.** `dpm.oracle.tablespace.used{tablespace="USERS"}` steps **down −196,608 B at 08:10:26**, the first sample after the drop, and **up +196,608 B at 08:25:14**, after the flashback. Over the whole capture its only other changes are +8,388,608 B steps at 07:01:15 and 08:04:14.

### 2.2 Load ran and impact is visible: YES
- **Load:**
  - Largest gap between any SERVER spans: 2.3 s. k6 progress lines: largest gap 10 s.
  - The 5 rps banking companion is visible: account SERVER spans go from 19.6/min (buffer) to 32.7/min; api from 8.1/min to 23.3/min.
  - account has 0 empty minutes; its largest gap in injection is 10.5 s.
- **Impact:**

| Service | Request | Normal | Injection |
|---|---|---|---|
| account | GET /api/accounts/{id} | 0 × 5xx | **374 × 500** (p50 16 ms) |
| account | GET /api/accounts | 0 × 5xx | **75 × 500** |
| account | POST /api/accounts/transfer | 0 × 5xx | **34 × 500** |
| api | POST /api/transfers | 296 × 200 | **188 × 502** (p50 604 ms) |
| transfer | POST /api/transfers | 0 × 5xx | **48 × 500** |

  - Commerce 5xx: gateway 74, order 74 and payment 32, with 755 `Checkout payment failed` lines.
  - k6 banking hourly summary for 07:39–08:39: **19.6% failed** (2824 of 14401). Failed checks: balance 2046, account list 362, transfer 416; transaction history 0 failures. The previous hour failed 0.3%.
- **What keeps working:** history GET /api/transfers is 145 × 200 in both api and transfer (p50 535 vs 496 ms in normal). Ledger and the outbox keep flowing. Food has 0 × 5xx.

### 2.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| account WARN `SQL Error: 942, SQLState: 42000` | **present** | 4937 rows, injection only. |
| account ERROR `ORA-00942: table or view "BANKING"."ACCOUNTS" does not exist` | **present** | 4937 rows, injection only. |
| account `Servlet.service() ... InvalidDataAccessResourceUsageException ... [select a1_0.id,a1_0.balance,a1_0.holder,a1_0.status from accounts a1_0 where a1_0.id=?]` | **present** | 4106 rows with this statement, plus 831 for the account-list variant (`where (? is null or a1_0.status=?) ...`). |
| transfer ORA-00942 with `... where a1_0.id=? for update` | **present** | 559 rows. |
| `dpm.oracle.tablespace.used{USERS}` steps down by 262,144 B in the same minute and never decreases in the baseline | **partial (magnitude wrong)** | The step is **−196,608 B**, not −262,144, at the first sample after the drop. It is restored by exactly +196,608 B. The baseline only increases. |
| No ORA-00942 or account `Servlet.service()` without a fault | **present** | 0 of each in normal and buffer. |
| api `Account service call failed for order null: 500` and `Account service circuit open/exhausted` | **present** | 499 and 6224 rows (5700 OPEN). In normal api logs only `Insufficient balance` 400s (202). |
| `Transfer COMPLETED` stops | **present** | 7 lines at 08:09:44–51, before the drop; 0 from 08:09:52 to 08:25:02; resumes at 08:25:03. |
| commerce-order `Checkout payment failed, releasing reserved stock` | **present** | 755 rows. |
| payment logs `core-banking transfer call failed: 500` | **partial** | The text appears 149 times, but only nested inside commerce-order's error message. commerce-payment itself logs no WARN or ERROR. |
| Transfer history keeps answering 200 | **present** | 145 × 200, 0 errors. |
| No rollout, restart or config change in rca-testbed-banking | **present (absence confirmed)** | KCM has only 35 `FailedToRetrieveImagePullSecret` rows. |
| Every pool keeps its sessions, no Hikari waiting; no ORA-28000 or ORA-04043 | **present (absence confirmed)** | pending_requests 0 for account, transfer and ledger. 0 rows for `Connection is not available`, ORA-28000 or ORA-04043. |
| Oracle sessions, waits and CPU at baseline; no blocking | **partial** | Blocking and blocked sessions are 0. But CPU goes **0.56 → 0.89** (p50 0.93), active sessions 0.53 → 1.21, and idle sessions 11.1 → 13.4. Stream-anomaly flags logical and physical reads at about 1.9× baseline and execution_count −78%. This is consistent with the companion's extra heavy history queries plus fast-failing statements. It does not look like saturation, but it is not "at baseline". |

### 2.4 Incident seed: YES
- 261 events in [t1, t2), of which 138 are opens: 103 change-detect noise, 28 log-anomaly and 7 stream-anomaly.
- Relevant:
  - ORA-00942 `new_template` in account and transfer (08:09:52–53).
  - api `unknown_anomaly` critical.
  - Surges on account, transfer, api and commerce-order.
  - commerce-order `Checkout payment failed` unknown_anomaly.
  - Oracle execution_count and logical/physical reads anomalies.

### 2.5 Contamination and clocks: MINOR ISSUE
- **Out-of-band DB access in the normal phase.**
  - At **06:08:26, 06:08:34 and 06:10:36**, tb-cp ran `kubectl ... -n rca-testbed-banking exec -i testbed-oracle-0 -- sqlplus -s / as sysdba` (3 SYSDBA sessions).
  - At 06:07:57 tb-runner ran `journalctl ... -u *profile*`.
  - These are an operator or another harness, 2 hours before t1. There was no visible effect: no banking errors beyond the baseline `Insufficient balance`, no tablespace change and no Oracle anomaly in 06:05–06:15.
- **No overlapping incident.** 5xx occur only in the banking chain and the commerce checkout chain.
- **Clocks:**
  - The first ORA (08:09:51.8), the DPM tablespace sample (08:10:26) and the first log-anomaly (08:09:52) agree.
  - The flashback timing agrees across logs (08:25:03) and DPM (08:25:14).
  - kcm.pod metrics for account, transfer and oracle are shifted −6 min (tb-w2). They are not part of this case's evidence.

### 2.6 Verdict: **USABLE-WITH-CAVEATS** (amend metadata)
The table drop is unambiguous from 9874 ORA-00942 text lines naming BANKING.ACCOUNTS, with the matching account-list and FOR UPDATE statements, while history on TRANSFERS works. The impact is large and cleanly bounded.
- **Root:** **Oracle-corebanking `target:00149c2f-8a97-43bf-8907-c25029973e51`** (DB; object BANKING.ACCOUNTS).
- **Symptom services:** core-banking-account `target:c2efb1d9-00ec-4ca7-8fd9-80bd122c9d10`, core-banking-transfer `target:f0ce5940-a7e2-44a2-843f-196de379d898`, core-banking-api `target:b3d586c3-…`, commerce-order and commerce-payment.
- **Identifiable:** yes.
- **Caveats:**
  - The tablespace step is −196,608 B, not −262,144.
  - Oracle CPU and reads are not "at baseline".
  - Payment's own log line does not exist.
  - The DROP is not recorded; the ORA text is the proof.
  - SYSDBA sessions at 06:08–06:10 in the normal phase.

---

## 3. case-f36-h-v3-b671604f — restaurant-service release 1.6.0 reads a dev-only property on every detail request

Phases:
- normal 13:07:58–15:08:05
- buffer to 15:18:25
- **injection 15:18:25–15:36:01 (17.6 min)**
- cooldown to 16:06:01

Preflight `clean`. The release window is 15:20:43–15:35:50 (15.1 min).

### 3.1 Injection executed: YES (direct)
- KCM events:
  - 15:18:43 `Scaled up replica set testbed-restaurant-56dd99c59 to 1 from 0`, and pod 994xh `Pulled ... "food-delivery-restaurant:1.6.0" already present on machine` (tb-w3).
  - 15:20:43: old pod wvqxp killed.
  - Rollback: 15:33:45 RS 69d9f9884c back up with `:latest`; 15:35:50 release pod killed.
- kcm_resources_history: 56dd99c59 = 1.6.0; 69d9f9884c = latest.
- syslog on tb-w3:
  - 15:18:37 `ctr images import -`
  - 15:36:01 and 15:36:02 `ctr images rm ...food-delivery-restaurant:1.6.0` and `sha256:dd95…` (cooldown)

### 3.2 Load ran and impact is visible: YES
- **Load:**
  - Largest gap between any SERVER spans: 3.5 s. restaurant: 0 empty minutes, largest gap in injection 10.8 s. k6 progress: largest gap 5 s.
  - The 2 rps companion is visible: `Created order` goes from 26–30/min to 83–109/min at 15:18–15:19, before the fault.
- **Impact in the release window:**
  - restaurant `GET /api/restaurants/{id}`: **176 × 500** (p50 10.6 ms). In normal it was 1972 × 200 and 0 × 5xx.
  - order `POST /api/orders`: **176 × 502** (p50 605 ms). In normal it was 480 × 200.
  - `Created order`: **0 per minute from 15:21 to 15:34**. It resumes at 15:35 as the rollback pod takes traffic.
- **What keeps working:**
  - menu 335 × 200, list 42 × 200, popular-menu 31 × 200.
  - Order health stays 200.
  - dispatch and payment have 0 × 5xx. commerce and banking have 0 × 5xx.

### 3.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet for testbed-restaurant + `Pulled ... "food-delivery-restaurant:1.6.0" already present on machine` | **present** | 15:18:43. |
| Prior restaurant RS/pods carry `:latest` | **present** | kcm_resources_history (see 3.1). |
| restaurant SEVERE `Servlet.service() ... IllegalStateException: Required key 'restaurant.policy.paused-regions' not found` on every detail request | **present** | 1651 rows, 15:20:46–15:35:50, 78–117 per minute. 0 in any service before injection. |
| No preceding `SQL Error` or driver ERROR | **present (absence confirmed)** | 0 `SQL Error` and 0 `Unknown column` in all phases. |
| Detail server spans end 500 while their DB child spans succeed | **present** | All 176 of the 176 failing spans have a `RestaurantRepository.findById` → `Session.find ...Restaurant` child, all status UNSET (OK). |
| GET /api/restaurants, /menu and /popular-menu stay 200 | **present** | 42, 335 and 31 × 200; 0 errors. |
| order `Failed to fetch restaurant N: 500` and POST /api/orders 502; `Created order` stops | **present** | 491 rows (about 33 per minute); 176 × 502; Created order 0 for 15:21–15:34. |
| dispatch and payment have no rollout or error and stop receiving orders | **present** | No KCM rows and 0 × 5xx. `Dispatched courier` and `Processed payment` equal `Created order` (229 each in injection, all outside the release window). |
| MySQL: no 1054, no schema change, no lock waits | **present (absence confirmed)** | `lock_timeout_count` and `row_lock_count` are 0 in every phase; active_session 1.05 vs 1.05. |
| No node or network change | **present** | Change-detect shows only host trait noise. |

No inaccurate metadata claims found.

### 3.4 Incident seed: YES (thin)
- 239 events in [t1, t2); 122 opens, of which 104 are change noise. Only 18 non-change opens (1.0 per minute; normal runs at 4.1 per minute, mostly switch and host noise).
- Relevant:
  - restaurant `Servlet.service()` new_template and surge (15:20:47).
  - order `Failed to fetch restaurant` new_template and surge (15:20:48).
  - MySQL commit/tps/insert/select up at 15:19:35, which is the companion load.
  - MySQL rollback_count 204 vs an 11.6 baseline (15:21:37), and update_count down.

### 3.5 Contamination and clocks: CLEAN
- The KCM ScalingReplicaSet rows carry `count=2` with first_timestamp 08:57–09:12 the same day. That means an earlier run of the same release, 4 hours before capture start. It is not inside the capture, but the count leaks the history.
- **Clocks:**
  - KCM old-pod kill at 15:20:43, then the first `Required key` at 15:20:46 and the first order failure at 15:20:48.
  - The last failure (15:35:50) is exactly the KCM release kill.
  - restaurant runs on tb-w3, which is aligned (lag 0).

### 3.6 Verdict: **USABLE**
The release appears in KCM. The restaurant detail endpoint fails locally after a successful DB read with a unique error text, the sibling endpoints are fine, and every order fails 502 with 0 created.
- **Root:** food-delivery-restaurant **`target:18a0e363-d253-4712-be40-a9e9e117d1dc`**, Deployment `testbed-restaurant` in ns `rca-testbed-food` (KCM target_id 09bdfa00-9f7b-42e0-ae96-1805d95fd1fe).
- **Symptom service, not root:** food-delivery-order `target:fa8b5cc4-6c43-45bf-b388-a4b245fade2d`.
- **Identifiable:** yes.
- **Caveats:** thin alarm seed; KCM event count=2 hints at an earlier run of the same release.

---

## 4. case-f37-h-v3-a6cd30bd — api-service release 1.6.0 reads a dev-only transfer-limit property

Phases:
- normal 13:54:04–15:54:15
- buffer to 16:04:32
- **injection 16:04:32–16:11:34 (7.0 min)**
- cooldown to 16:41:34

Preflight `clean`. The release window is 16:06:16–16:11:22 (5.1 min).

### 4.1 Injection executed: YES (direct)
- KCM events:
  - 16:04:51 `Scaled up replica set testbed-api-784bc96cf7 to 1 from 0`, and pod jqpl6 `Pulled ... "core-banking-api:1.6.0" already present on machine` (tb-w2).
  - 16:06:16: old pod 9mt97 killed.
  - Rollback: 16:09:57 RS 7bd465dcd9 back up with `:latest`; 16:11:22 release pod killed.
- kcm_resources_history: 784bc96cf7 = 1.6.0; 7bd465dcd9 = latest.
- syslog on tb-w2:
  - 16:04:44 `ctr images import -`
  - 16:11:30 `ctr images rm ...core-banking-api:1.6.0` (injection phase)

### 4.2 Load ran and impact is visible: YES
- **Load:**
  - Largest gap between any SERVER spans: 4.3 s.
  - account and api have 0 empty minutes in injection.
  - k6 progress: largest gap 20 s, at 15:03:50 (normal).
  - The 20 rps companion is visible: api SERVER spans go from 5.5/min to 66.1/min; account from 11.2/min to 61.7/min.
- **Impact in the release window:**
  - api `POST /api/transfers`: **367 × 500** (p50 5.5 ms). In normal it was 125 × 200 and 8 × 400.
  - k6 banking summary for 15:33–16:33: transfer check **101 failures** (1.40% of all requests). Balance, history and account list: 0 failures. Normal hours fail 0.33–0.40%.
- **What keeps working:**
  - api `GET /api/transfers`: 23 × 200.
  - account `GET /api/accounts/{id}` 259 × 200 and `GET /api/accounts` 48 × 200.
  - Settlement `Transfer COMPLETED` continues at 8–18 per 30 s.
  - commerce and food have 0 × 5xx.

### 4.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet for testbed-api + `Pulled ... "core-banking-api:1.6.0" already present on machine` | **present** | 16:04:51. |
| api SEVERE `... IllegalStateException: Required key 'api.policy.single-transfer-limit' not found` on every transfer | **present** | 3477 rows, 16:06:17–16:11:22 (322–356 per 30 s). 0 before; api SEVERE in normal is 0. |
| api POST /api/transfers server spans 500 with no child client span to account | **present** | 367 × 500; **all 367 have no child span**. |
| api `Submitting transfer` drops to zero | **present** | 219 per 30 s before the release, 0 per 30 s from 16:06:30 to 16:10:30. |
| api `Transfer service` history calls continue | **partial** | No log with that text exists in any phase. History is confirmed by spans instead: GET /api/transfers 23 × 200, with api CLIENT `GET` to testbed-transfer 23 × 200. |
| account has no rollout and no error and stops receiving api transfers | **present** | 0 POST /api/accounts/transfer and 0 `Account validation passed` in the release window. 0 account WARN or ERROR. |
| transfer keeps `Transfer COMPLETED` for commerce settlement | **present** | 138 `from=commerce-settlement` lines in injection. |
| No UnknownHostException; no request flood, thread growth or OOM restart | **present (absence confirmed)** | 0 UnknownHost and 0 OOM. Release pod threads 37–44 (old pod 62–65); restart_count 0. |

### 4.4 Incident seed: YES (thin)
- 118 events in [t1, t2); 60 opens, of which 53 are change noise. Only **7 log-anomaly opens and 0 stream-anomaly opens**:
  - api `Servlet.service()` new_template (16:06:53).
  - api surges on `Account service call failed` and the circuit (insufficient balance at the 20 rps companion rate).
  - account `Transfer rejected: insufficient balance` surge.

### 4.5 Contamination and clocks: MINOR ISSUE
- **No overlapping incident.** 5xx occur only in core-banking-api.
- **Event-count history:** the KCM rows carry count 2–4 with first_timestamps 2026-10-19 09:46 and **2026-10-21 12:52**. So the same release was rolled out about 1 hour before capture start. The normal phase is clean (api 0 × 5xx).
- **Clocks:**
  - KCM old-pod kill at 16:06:16 and the first SEVERE at 16:06:17; the last SEVERE at 16:11:22 equals the KCM release kill.
  - tb-w2 kcm.pod shift: the release pod jqpl6's `kcm.pod.*` series lies entirely in the buffer phase (15:59–16:04), before the rollout.

### 4.6 Verdict: **USABLE-WITH-CAVEATS**
The evidence is complete and unique: a release, a config-key exception, 500s with no downstream call, and every other path healthy.
- **Root:** core-banking-api **`target:b3d586c3-4443-4581-8f2e-be12f3504448`**, Deployment `testbed-api` in ns `rca-testbed-banking` (KCM target_id c1887d42-c4f7-40bf-8f8c-6cc7c0bdf582).
- **Identifiable:** yes.
- **Caveats:**
  - Very short fault: 5.1 min, in a 7.0-min injection phase.
  - Alarm seed is log-only, with 7 opens.
  - The `Transfer service` log in the metadata does not exist.
  - kcm.pod metrics for the root pod are shifted −6 min, so they appear before the rollout.

---

## 5. case-f39-h-v3-255629f2 — account-service release 1.6.0 changes the transfer response contract

Phases:
- normal 23:25:16–01:25:24
- buffer to 01:35:37
- **injection 01:35:37–01:53:26 (17.8 min)**
- cooldown to 02:23:26

Preflight `clean`. The release window is 01:38:37–01:53:17 (14.7 min).

### 5.1 Injection executed: YES (direct)
- KCM events:
  - 01:35:56 `Scaled up replica set testbed-account-b455b7c47 to 1`, and 01:35:57 pod m96hv `Pulled ... "core-banking-account:1.6.0" already present on machine` (tb-w2).
  - 01:38:37: old pod bxtxg killed.
  - Rollback: 01:50:47 RS 6fd4f4bd7c back up with `:latest`; 01:53:17 release pod killed.
- kcm_resources_history: b455b7c47 = 1.6.0; 6fd4f4bd7c = latest.
- syslog on tb-w2:
  - 01:35:48 `ctr images import -`
  - 01:53:26 and 01:53:27 `ctr images rm` (cooldown)

### 5.2 Load ran and impact is visible: YES
- **Load:**
  - Largest gap between any SERVER spans: 4.7 s. api and account have 0 empty minutes in injection. k6 progress: largest gap 3 s.
  - The 5 rps companion is visible: api SERVER spans go from 7.0/min to 23.5/min; account from 12.2/min to 31.7/min.
- **Impact in the release window:**
  - api POST /api/transfers: **210 × 502** (p50 604 ms), plus 4 × 400.
  - k6 banking summary for 01:21–02:21: transfer **302 failures** (2.79% of all requests). Balance, history and account list: 0 failures. Normal hours fail 0.06–0.27%.
- **Money still moves:**
  - 479 `Account validation passed` (about 33 per minute) and 571 ACC `Transfer COMPLETED` in the release window.
  - **63 of 412 distinct transfers were booked twice (59) or three times (4) within 3 s.** These are the api retries.
- **What keeps working:**
  - api GET /api/transfers 135 × 200.
  - account GET /api/accounts/{id} 313 × 200 and GET /api/accounts 72 × 200.
  - Settlement keeps flowing: 425 COMPLETED lines.
  - Other domains have 0 × 5xx.

### 5.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet for testbed-account + `Pulled ... "core-banking-account:1.6.0" already present on machine` | **present** | 01:35:56–57. |
| api ERROR `Account service call failed for order null: Error while extracting response for type [com.corebanking.common.dto.TransferResponse] and content type [application/json]` | **present** | 479 rows, plus 479 nested `circuit open/exhausted ... Account service failed: Error while extracting ...`. First at 01:38:44, last at 01:53:15. 0 in normal. |
| api `Account service circuit open/exhausted` | **present** | 5672 OPEN and 39 HALF_OPEN rows. |
| api POST /api/transfers server spans in error with 502 | **present** | 210 × 502. 51 of them have a child CLIENT `POST` with 200. |
| api client spans to testbed-account end 200 | **present** | 52 × 200 (and 4 × 400). |
| account `POST /api/accounts/transfer` server spans end 200 | **present** | 52 × 200 (and 4 × 400). |
| account client spans to testbed-transfer end 200 | **present** | 52 × 200. |
| account keeps logging `Account validation passed`; transfer keeps `Transfer COMPLETED` for ACC accounts | **present** | 479 and 571 rows (see 5.2). |
| account logs no error, stays Ready without restarts | **present** | 2 WARN (Hibernate dialect notice at startup), 0 ERROR. restart_count 0. KCM shows only startup-probe failures during boot. |
| Oracle: no new errors or lock waits | **present** | 0 `ORA-` lines; blocked_session 0. Oracle CPU rises 0.30 → 0.82, in line with the companion load (same pattern as F35). |

No inaccurate metadata claims found. The duplicate booking in user_impact is confirmed.

### 5.4 Incident seed: YES (noisy)
- 518 events in [t1, t2); 279 opens: 160 stream-anomaly, 110 change and 9 log.
- 84 of the opens are host metrics on **tb-w2**: 40 in the t1 minute and 12 around 01:47–01:48. That is the image-import and JVM-start footprint of the rollout and the rollback.
- Relevant:
  - api circuit new_template (01:39:04) and surge (01:39:54).
  - api `Error while extracting response` new_template, which fires **only at 01:45:04**, 6.3 min after onset.
  - account and api jvm.memory anomalies at startup.
  - Oracle reads up (companion).
- The normal phase is also noisy: 10.4 opens per minute, mostly switch and host noise.

### 5.5 Contamination and clocks: CLEAN, apart from the kcm shift
- Normal phase, 00:09:56–00:10:01: read-only `kubectl get nodes/pods` and `exec testbed-mysql-0 -- head -1 /sys/fs/cgroup/memory.stat` on tb-cp. This is an external probe of the food DB with no effect.
- No overlapping incident. Ledger `reconciliation MISMATCH` (2 in injection) is baseline noise; it occurs 4–10 times in normal in every case.
- **Clocks:**
  - KCM old-pod kill at 01:38:37 and the first parse error at 01:38:44; the last error at 01:53:15 versus the KCM release kill at 01:53:17.
  - tb-w2 kcm.pod shift: the release pod's series runs 01:30–01:46, while the pod actually lived 01:35:56–01:53:17.

### 5.6 Verdict: **USABLE-WITH-CAVEATS** (minor)
All decisive items are present. The parse failure in the caller is proven by api 502 spans sitting over 200 client and server spans down the chain, and the duplicate bookings are verified.
- **Root:** core-banking-account **`target:c2efb1d9-00ec-4ca7-8fd9-80bd122c9d10`**, Deployment `testbed-account` in ns `rca-testbed-banking` (KCM target_id bd3f3d59-979e-4fd7-b8ff-4b253315c7ce).
- **Symptom service, not root:** core-banking-api `target:b3d586c3-…`.
- **Identifiable:** yes. The model must go past the failing api to the release of its callee.
- **Caveats:** kcm.pod series of the root pod shifted −6 min; noisy host alarms on tb-w2 in the seed.

---

## 6. case-f41-h-v3-2c392569 — account-service release 1.5.0 disables the health probe sub-paths, so liveness 404 kills the pod

Phases:
- normal 16:35:15–18:35:17
- buffer to 18:45:26
- **injection 18:45:26–19:01:23 (15.9 min)**
- cooldown to 19:31:23

Preflight `clean`. The release pod was Ready from 18:48:11 and was deleted at 19:00:44.

### 6.1 Injection executed: YES (direct)
- KCM events:
  - 18:45:46 `Scaled up replica set testbed-account-7ff6988444 to 1`, and pod hjtts `Pulled ... "core-banking-account:1.5.0" already present on machine` (tb-w2).
  - 18:48:11: old pod bjsfx killed.
  - Rollback: 18:58:23 RS 6fd4f4bd7c up with `:latest`; 19:00:44 release RS scaled to 0.
- kcm_resources_history:
  - Both RSs have identical probes: liveness `/actuator/health/liveness`, readiness and startup `/actuator/health`.
  - Only the image differs: 1.5.0 vs latest.
- syslog on tb-w2:
  - 18:45:41 `ctr images import -`
  - 19:01:23 and 19:01:25 `ctr images rm` (cooldown)

### 6.2 Load ran and impact is visible: YES
- **Load:**
  - Largest gap between any SERVER spans: 7.0 s, at 17:42 (night load of about 1 rps per domain). k6 progress: largest gap 5 s.
  - The 5 rps companion is visible: api SERVER spans go from 4.0/min to 17.6/min; account from 5.8/min to 15.0/min.
- **Restart cycle** (containerd on tb-w2 and the account app logs):

| Up (app `Started` → liveness kill) | Length | Outage that follows | Length |
|---|---|---|---|
| 18:48:02 → 18:49:16 | 74 s | 18:49:16 → 18:51:52 | 156 s |
| 18:51:52 → 18:53:01 | 69 s | 18:53:01 → 18:55:18 | 137 s |
| 18:55:18 → 18:56:31 | 73 s | 18:56:31 → 18:58:48 | 137 s |
| 18:58:48 → 19:00:01 | 73 s | 19:00:01 → 19:00:40 | 39 s (ended by the rollback pod) |

  - Each kill is a containerd `StopContainer`, immediately followed by `CreateContainer ... attempt:N` (N = 1 to 4).
  - Account non-health SERVER spans drop to **0 in exactly those windows** (15-s buckets).
- **Impact:**
  - api POST /api/transfers in the release window: **92 × 502** (p50 604 ms) and 52 × 200. The 502s come with `Account service call failed ... Connection refused` (274) and the circuit OPEN (2818).
  - Balance lookups go nginx → account, and nginx is not traced, so their 502s are not in spans.
  - The k6 banking summary for 18:20–19:20 shows them: **balance check 189 failures, account list 35, transfer 42, transaction history 0** (7.52% failed). The previous hour failed 0.22%.
- **What keeps working:**
  - history GET /api/transfers 78 × 200.
  - Settlement keeps flowing: 151 COMPLETED lines.
  - Other domains have 0 × 5xx.

### 6.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet for testbed-account + `Pulled ... "core-banking-account:1.5.0" already present on machine` | **present** | 18:45:46. |
| KCM `Unhealthy: Liveness probe failed: HTTP probe failed with statuscode: 404` for the release pod | **present (1 row)** | One row at 18:56:01 with count=13 (first_timestamp 18:48:16). Also probe spans `GET /actuator/health/liveness` from `kube-probe/1.31`: 404 at 18:48:16, 18:48:31 and 18:52:46. The old pod's probes were 200 at 18:45:30 and 18:46:30. |
| KCM `Killing: Container account-service failed liveness probe, will be restarted` | **absent** | No such KCM event or KCM-agent log. **Proxy:** containerd StopContainer plus CreateContainer attempt:1–4 at 18:49:16, 18:53:01, 18:56:31 and 19:00:01; 5 `Started AccountServiceApplication` lines in 13 minutes. |
| KCM `BackOff: Back-off restarting failed container` | **absent (and did not happen)** | No BackOff event. `kcm.pod.container_crash_loop_back_off` is 0. Each restart was immediate (CreateContainer within 1 s of the stop). |
| Probe path unchanged; only the image differs | **present** | kcm_resources_history (see 6.1). |
| Tenured Gen stays in band and restarts fresh; no OOM | **present** | Release pod Tenured Gen 17–105 MB (mean 59 MB), restarting low each time; old pod a flat 85 MB; rollback pod 96–101 MB. 0 OutOfMemoryError. |
| No Hikari error logged | **contradicted** | **4** `HikariPool-1 - Connection is not available ... (total=7–10, active=7–10, waiting=6–26)` with SEVERE `CannotCreateTransactionException`. They come right after each restart (18:52:01, 18:55:30, 18:59:00 × 2): the queued backlog hits a cold pool. They trigger log-anomaly opens (`SQL Error` new_template critical, Hikari new_template). |
| account answers between kills; startup and readiness pass, so the pod returns to the endpoints | **present** | Account spans resume within 15 s of each `Started`. KCM shows only startup-probe `connection refused` during boot. |
| Oracle and transfer Ready; history and commerce settlement keep working | **present** | 0 ORA- lines; transfer 0 × 5xx; history 78 × 200; settlement continues. |

Metadata claims that do not match the capture:
- **Outage windows do not grow.** user_impact says they "grow from under a minute to about six minutes". In the capture they are a steady 137–156 s, and the restarts are immediate with no back-off.
- **Boot takes much longer than claimed.** The description says about 40 s; the capture shows 109–127 s from Spring start to `Started`, and the process has been running 136–155 s by then.
- **"No Hikari error is logged" is false.** There are 4 Hikari timeouts after restarts.
- **The Killing and BackOff events are not in the capture.**

### 6.4 Incident seed: YES
- 265 events in [t1, t2); 134 opens: 108 change, 20 log and 6 stream.
- Relevant:
  - account `SQL Error` new_template critical (18:52:01) and unknown_anomaly (18:55:32).
  - account Hikari new_template (18:59:01) and account surges.
  - api surge (18:48:54) and circuit new_template.
  - Oracle reads and executions up (companion).
  - Ledger `reconciliation MISMATCH` (baseline noise, also in normal).
- No alarm is driven by the restarts themselves.

### 6.5 Contamination and clocks: CLOCK ISSUE
- **No overlapping incident.** 5xx occur only in core-banking-api.
- **The restart evidence in metrics is time-shifted.** `kcm.pod.container_restart_count` for hjtts steps 0→1→2→3→4 at **18:43, 18:47, 18:50 and 18:54**. The real kills were 18:49:16, 18:53:01, 18:56:31 and 19:00:01, so the series is about −6 min.
  - The pod's `kcm.pod.*` series starts at 18:40:00, before the pod was created (18:45:46) and inside the buffer phase.
  - `kcm.pod.mem_usage` dips (18:44, 18:47, 18:51, 18:54) are shifted the same way.
  - A model reading these metrics would see restarts before the rollout.
- The KCM-event, log, span and containerd timelines agree within seconds:
  - KCM liveness first_timestamp 18:48:16 = the first 404 probe span.
  - The containerd kill at 18:49:16 = the start of the account span gap at 18:49:15.

### 6.6 Verdict: **USABLE-WITH-CAVEATS** (amend metadata before use)
The mechanism is identifiable: a release with unchanged probes, a liveness 404 (one KCM event plus 404 probe spans), and a healthy JVM killed every about 3.6 min, with recurring account outages and api 502s.
- **Root:** core-banking-account **`target:c2efb1d9-00ec-4ca7-8fd9-80bd122c9d10`**, Deployment `testbed-account` in ns `rca-testbed-banking` (KCM target_id bd3f3d59-979e-4fd7-b8ff-4b253315c7ce).
- **Identifiable:** yes.
- **Caveats:**
  - The capture has 1 of the expected KCM liveness events and none of the Killing or BackOff events.
  - The restart-count metric is shifted −6 min, to before the rollout.
  - The metadata's growing back-off, 40-s boot and no-Hikari claims are wrong for this capture.
  - The balance-lookup 502s are visible only in the k6 summary, because nginx is untraced.

---

## Cross-case checks
- **Load generator never stopped.**
  - k6 baseline progress lines run continuously in every case; the largest gap is 3–20 s, and the 20 s gap is in F37's normal phase.
  - Entry services (commerce-gateway, food-delivery-restaurant) have 0 empty minutes in every phase.
  - Every companion surge is visible as a step at t1.
- **Fault windows are short in F16 and F37.** The fault lasted 7.7 min in F16 and 5.1 min in F37, against 12.5–15.2 min in the others. The cleanup rollback starts inside the injection phase in all release cases, as in earlier captures.
- **Unrelated incidents:** none.
  - 5xx occur only in the target chains.
  - Recurring baseline noise appears in every case and also in normal: commerce k6 baseline failing about 44% by design (404/401); core-banking-api `Insufficient balance` ERRORs; ledger `reconciliation MISMATCH` WARN; SMS-agent containerd snapshot errors; change-detect host trait events (about 6–7 per minute).
- **Harness or operator footprints:**
  - Every release case has `ctr images import/rm` on the target node; some of the rm lines fall inside the injection phase (F16, F37).
  - F35 has 3 SYSDBA `sqlplus` sessions in its normal phase (06:08–06:10).
  - F39 has read-only kubectl probes in normal (00:10).
  - KCM event `count` fields reveal earlier same-day runs of the same release (F36 at 09:10, F37 at 12:52).
- **Clocks:**
  - Logs, spans, KCM events, DPM and syslog agree within seconds in all six cases (KCM event lag p50 1.5–1.9 s, max 4.1 s; log observed-lag at most 2.7 s).
  - **The exception is the tb-w2 KCM pod and container metrics, stamped about 6 min early in every capture.** This needs a pipeline fix: tb-w2's KCM metric timestamps, or the export, should be corrected before these captures are replayed. Otherwise the shifted series should be flagged.
- **DB-side proxies in banking:** Oracle CPU and reads rise 1.6–2.8× whenever the transfer-heavy companion runs, because it includes history queries:
  - F35: CPU 0.56 → 0.89.
  - F39: 0.30 → 0.82.
  - F41: 0.20 → 0.55.
  - F37's companion has no history step, and its Oracle CPU stays flat at 0.30 → 0.31.

  Metadata lines saying "Oracle CPU at baseline" should be softened.

## Summary
| Case | Verdict | Root target | Key caveat |
|---|---|---|---|
| case-f16-p-v3-e40ec615 | **USABLE** | commerce-notification `target:ef0eaed0-045d-48e2-9e3a-280b4fe9b0cc` (Deployment testbed-notification, rca-testbed-commerce) | Fault only 7.7 min; `paging stopped at page limit` absent; logins fail mostly 500 (19), not 502 (4). Root found via the new notification→user calls (510 CLIENT spans, DPM full-table select, threads 45→115). |
| case-f35-p-v3-11824edf | **USABLE-WITH-CAVEATS** | Oracle-corebanking DB `target:00149c2f-8a97-43bf-8907-c25029973e51` (table BANKING.ACCOUNTS) | USERS tablespace step is −196,608 B (metadata says −262,144); Oracle CPU and reads not at baseline (companion); payment's own log line absent; DROP not recorded; SYSDBA sqlplus sessions in the normal phase. |
| case-f36-h-v3-b671604f | **USABLE** | food-delivery-restaurant `target:18a0e363-d253-4712-be40-a9e9e117d1dc` (Deployment testbed-restaurant, rca-testbed-food) | Thin alarm seed (18 non-change opens); KCM count=2 reveals an earlier same-day run of the release. |
| case-f37-h-v3-a6cd30bd | **USABLE-WITH-CAVEATS** | core-banking-api `target:b3d586c3-4443-4581-8f2e-be12f3504448` (Deployment testbed-api, rca-testbed-banking) | Fault only 5.1 min; log-only seed (7 opens); `Transfer service` log in metadata does not exist; root pod kcm.pod series shifted −6 min (appears before the rollout). |
| case-f39-h-v3-255629f2 | **USABLE-WITH-CAVEATS** (minor) | core-banking-account `target:c2efb1d9-00ec-4ca7-8fd9-80bd122c9d10` (Deployment testbed-account, rca-testbed-banking) | Root pod kcm.pod series shifted −6 min; seed dominated by tb-w2 host noise; parse-error new_template alarm comes 6.3 min after onset. Duplicate bookings verified (63 transfers booked 2–3×). |
| case-f41-h-v3-2c392569 | **USABLE-WITH-CAVEATS** (amend metadata) | core-banking-account `target:c2efb1d9-00ec-4ca7-8fd9-80bd122c9d10` (Deployment testbed-account, rca-testbed-banking) | Only 1 KCM liveness-404 event; Killing and BackOff absent (restarts visible only in containerd syslog); restart_count metric shifted −6 min to before the rollout; outages are a steady 137–156 s, not growing; 4 Hikari errors contradict "no Hikari error"; balance 502s visible only in the k6 summary. |
