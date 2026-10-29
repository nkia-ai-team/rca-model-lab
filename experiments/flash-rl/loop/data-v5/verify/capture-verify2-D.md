# Capture verification — batch D (F66-P, F66-R, F67-H, F67-R, F68-R, F69-R)

All work was read-only on `/data/eval-cases`. Times are UTC (KST = UTC+9). Spans are about 10% sampled, so span counts are about 10% of real traffic. Log counts are not sampled. "Normal" means the 120-minute normal phase. DPM `queryTime` values are in milliseconds; the seconds shown below are converted. Helper scripts are in `scratchpad/verify2-D/` (`hD.py`/`h.py` helpers, `xsvc.py`, `routes.py`, `permin.py`, `pgsess3.py`, `kcm.py`, `events.py`, `syslog.py`, `vmx.py`, `clock.py`, `f68*.py`; overview dumps in `ov_<case>.txt`).

Four of the cases (F66-P, F66-R, F67-H, F67-R) are PostgreSQL lock-queue scenarios on the same commerce database. They share two artifacts, described once here and referenced in each section:

- **Harness psql probe in the lock queue.** In each of the four cases, DPM shows one extra session with `program=psql`, `ip=""` (local socket inside testbed-postgres-0) and `user=commerce`. It starts 2.5 to 3 minutes after the blocker and sits `blocked` on a relation lock on the target table behind the blocker or the ALTER. The PIDs are F66-P 11622, F66-R 8049, F67-H 26223 and F67-R 20596. No application uses psql, and the metadata does not mention this session (F66/F67-H say "Both sessions carry the driver's default application name"). It is most likely the executor's probe. It does not change the root, but a model may read it as a second operator.
- **Recovery runs into cooldown.** The victim service is restarted by liveness. Its restarted pod then hangs in startup on the same lock, and the startup probe kills it again at about 300 s, which is close to the moment the lock clears. Each victim therefore becomes Ready again 1.5 to 3 minutes after t2. The DB-side waits clear in the first snapshot after the blocker ends.

---

## 1. case-f66-p-v3-a79bda26 — ADD COLUMN on inventory_schema.outbox_events queued behind a slow export COPY

Phases: normal 08:29:05–10:29:08, buffer to 10:39:24, **injection 10:39:24–10:46:30**, cooldown to 11:16:30. Preflight `clean` (user_5xx 0.0, entry p95 0.0096 s). There is no companion load (by design).

### 1.1 Injection executed: YES (direct trace in DPM sessions; the SQL text is not captured)
- PG Top SQL has no COPY, ALTER or DROP COLUMN text. The utility statements are not recorded there.
- **DPM session chain** (`dpm_session_local`, PostgreSQL target cf97076f), seven snapshots from 10:40:05 to 10:46:11:
  - **Export pid 11344**: `PostgreSQL JDBC Driver`, ip "" (inside the DB pod), `Client/ClientWrite`, `sessionType=blocking`. Its relNames are outbox_events, outbox_events_pkey and the two unpublished-outbox indexes. queryTime is 38.5 s at 10:40:05 (so it started at 10:39:26.6) and 404.5 s at 10:46:11. It is gone at 10:47:12, so it ran for 404 to 465 s.
  - **ALTER pid 11363**: `Lock/relation` on `outbox_events`, `blockingPid=11344`, `sessionType=blocking`. It started at 10:39:26.9, which is 0.35 s after the export (the metadata says "a few seconds later").
- syslog tb-w1 at 10:39:26.7: containerd `Container exec ... stdin closed`. This is the executor's exec into the DB pod, and it matches the export start to 0.1 s.

### 1.2 Load ran and impact is visible: YES
- Load: commerce-gateway SERVER spans run at 128–193/min in normal and 124–152/min in injection, with no zero minutes. The largest gap in injection is 4.8 s. One k6 `Insufficient VUs, reached 50` warning appears at 10:39:51, and offered load was unaffected.
- Product pages: gateway `GET /api/products/**` is 6763 × 200 and 0 × 5xx in normal. In injection it is 355 × 200, **228 × 500** (p50 3.01 s) and 18 × 502. In product-service, `GET /api/products/{id}` is 232 × 500 and reserve-stock is 14 × 500.
- Checkout: gateway `POST /api/orders/**` is 579 × 200 in normal. In injection it is **33 × 502 and 0 × 200**. Order "Recorded outbox event ... PAID" runs at 45.6/min in normal. In injection there are 2 (both at 10:39:24–25), then **0 until 10:48**.
- Inventory has no SERVER spans at all from 10:40 to 10:47 (45/min in normal). Product SEVERE logs run at 347–386/min from 10:40 to 10:48.
- Things that should keep working do keep working. commerce-cart and commerce-user have 0 × 5xx (cart 37 × 200 POSTs in injection). Food and banking services have 0 × 5xx. **Product lists `GET /api/products` and `/api/categories` also keep answering 200 (285 and 22 product-service spans, 0 errors)**. They fail only during the 31 s the gateway circuit was open (see 1.3).
- Recovery: the inventory restart pod logs "Started InventoryServiceApplication" at **10:48:34**, after a second startup-probe kill at 10:46:07 and a new container at 10:46:38. Gateway 500s last until 10:48. User impact therefore lasts from 10:39:38 to 10:48:34, about 9 minutes, against the metadata's "about seven minutes".

### 1.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| inventory-pod sessions blocked on `Lock/relation` with relNames including outbox_events (and inventory, inventory_movements), same blockingPid | **present** | 10 sessions from 10.244.3.66 (the pool of 10) in every snapshot. 7 are `relation` waits with `blockingPid=11363`; relNames are `inventory, inventory_movements, inventory_movements_id_seq, …outbox_events` or `outbox_events` alone. |
| a few inventory sessions wait on `transactionid` (same product's stock row) | **present** | 3 sessions: 10978→10965, 11107→10993, 11127→11112. relNames are `inventory, inventory_pkey, inventory_product_id_key`. |
| the blocker is itself waiting on outbox_events, behind one long active session (ClientWrite, queryTime → ~420 s) | **present** | 11363 waits on 11344 (ClientWrite), and queryTime reaches 404.5 s (see 1.1). |
| outbox_events never under a relation-lock wait before | **present (in-capture)** | 0 rows with outbox_events and 0 `Lock` rows in normal, buffer and cooldown. 67 outbox_events rows in injection. The 30-day claim cannot be checked from this capture. |
| inventory `HikariPool-1 ... 30000ms (total=10, active=10, idle=0 ...)` | **present** | 101 ERROR rows, 10:40:06–10:42:10. `db.client.connections.pending_requests` peaks at 98 (0 in normal). |
| commerce-product Servlet errors | **present (mechanism differs)** | 3262 SEVERE. **2515 are `Connect timed out` and 493 are `Connection refused`** to testbed-inventory, because inventory has no Ready endpoint. Only **92 are `Read timed out`** (the 10 s path), all in the first minute. The metadata's "gives up after its 10 s read timeout" describes only the first minute. |
| gateway `Downstream unavailable for GET /api/products...` (CallNotPermittedException) | **present (brief)** | 834 WARN, **only from 10:39:42 to 10:40:13**: 350 product-detail, 203 `/api/products` list, 149 variants and 16 categories, all CB `product` OPEN or HALF_OPEN. After that the gateway logs nothing more. |
| gateway `... /api/orders/checkout` with CallNotPermittedException | **absent** | 0 CallNotPermitted for checkout. There are only 4 `Downstream unavailable for POST /api/orders/checkout: RestClientException: Error while extracting response...`. Checkout fails as order's 502 passed through (33 × 502). |
| KCM testbed-inventory Unhealthy, Killing, restarts, no rollout | **present** | Liveness and readiness fail from 10:39:41. `Killing ... failed liveness probe` at 10:40:41, and the container restarts at 10:41:11. Startup probe failures reach count 64 at 10:44:42. A second kill comes at 10:46:07 (containerd). `kcm.pod.container_restart_count` goes 0 → 2. There is no ScalingReplicaSet. |
| not F57-P (no 23505) | **present (absence confirmed)** | 0 rows containing `23505`. |
| everything clears when the export session disappears | **partial** | The DB clears at once: the 10:47:12 snapshot has no Lock rows, and `dpm.postgresql.session.blocked_session` goes from 7 to 0. The app lags to 10:48:34 because of the restart loop. |

Metadata claims that are inaccurate:
- "product lists and categories fail with 502": true only for about 31 s while the gateway CB was open. After that, lists and categories return 200.
- "first ones after hanging for about 10 s": most failures take 3 s (connect timeout).
- "about seven minutes": the DB lock lasts about 7 minutes; user impact lasts about 9 minutes.
- "a few seconds later": the ALTER starts 0.35 s after the export.

### 1.4 Incident seed: YES
- In [t1, t2) there are **23 anomaly opens**: 12 log-anomaly, 7 stream-anomaly and 4 alarm-bridge.
- By target: PostgreSQL-commerce 9, commerce-inventory 6, commerce-product 4, Oracle-corebanking 2, core-banking-ledger 1, commerce-gateway 1.
- Key signals:
  - alarm-bridge `dpm.postgresql.session.blocked_session` = 7 (10:42:18) and `session_utilization` 75 (10:45:39).
  - stream level shifts on PG seq_scans, transaction_count, insert and update counts.
  - log-anomaly on product Servlet, inventory Hikari, `DataSource health check failed`, and gateway `Downstream unavailable`.
- The Oracle byte drop follows from fewer commerce settlements reaching banking. The ledger "imbalance" log recurs about every 10 minutes in normal and is baseline.

### 1.5 Contamination and clocks
- No overlapping unrelated injection. 5xx occur only in commerce-gateway, product, inventory and order.
- Harness footprints:
  - tb-runner ssh session opens: 8.7/min in injection, 0 in normal.
  - `journalctl -u loadgen-commerce` at 10:39:18–19.
  - containerd exec at 10:39:26.7.
  - the psql probe pid 11622 (see the note at the top).
- Out of window: a burst of 436 host and switch stream-anomaly opens in cooldown from 11:00 (dev-svr-200-57, tb-w2, N2024 switches). This is infrastructure noise and does not touch [t1, t2).
- Clocks: KCM collected-minus-event lag is p50 1.5 s, max 3.1 s. KCM Killing at 10:40:41 matches containerd StopContainer at 10:40:41.009. The DPM-derived export start (10:39:26.6) matches the containerd exec (10:39:26.7). Log observed-minus-timestamp is at most 0.49 s. The first product ERROR log (10:39:37.5) and the first 5xx span (10:39:36.2) agree.

### 1.6 Verdict: **USABLE-WITH-CAVEATS**
The full lock chain (export → ALTER → inventory sessions) is unambiguous and inventory is drained and restarted. However, the metadata user_impact over-claims list and category failures and the 10 s timeout path, impact runs about 2 minutes past t2, and the gateway checkout CallNotPermitted line is absent.

**Root target is clearly identifiable: PostgreSQL-commerce `target:cf97076f-e72d-4c9e-b731-649592cfc5bf`**, table inventory_schema.outbox_events. The blockers are the in-pod export pid 11344 and the ALTER pid 11363. Victims: commerce-inventory `target:414db206-d121-43b1-886c-78b240d23a7a`, then product `766ec1cd…` and gateway `e9f5f307…`.

---

## 2. case-f66-r-v3-346c6170 — ADD COLUMN on user_schema.users queued behind a slow export COPY (users ⋈ auth_tokens)

Phases: normal 01:20:02–03:20:05, buffer to 03:30:21, **injection 03:30:21–03:37:41**, cooldown to 04:07:41. Preflight `clean`. There is no companion load.

### 2.1 Injection executed: YES (direct trace in DPM sessions)
- **Export pid 7830**: JDBC, ip "", `ClientWrite`, `blocking`. Its relNames are `auth_tokens, auth_tokens_pkey, auth_tokens_token_key, idx_auth_tokens_token, users, users_email_key, users_pkey`. queryTime is 27.1 s at 03:30:50 (so it started at 03:30:23.0) and 392.95 s at 03:36:56. It is gone at 03:37:57, so it ran for 393 to 454 s.
- **ALTER pid 7850**: `Lock/relation` on `users`, `blockingPid=7830`. It started 0.4 s after the export.
- containerd exec on tb-w1 at 03:30:23.3. No COPY or ALTER text appears in Top SQL.

### 2.2 Load ran and impact is visible: YES
- Load: gateway runs at 75–140/min in normal and 102–142/min in injection, with no zero minutes. The largest gap in injection is 3.9 s.
- Writes through the gateway:
  - normal: `POST /api/carts/**` 586 × 200, `POST /api/orders/**` 356 × 200, `POST /api/users/**` 87 × 200.
  - injection: carts POST **27 × 500 and 0 × 200**, carts DELETE **19 × 500**, orders POST **21 × 500** (and 1 × 200 at t1), users/login **8 × 502 + 1 × 500, 0 × 200**.
- Orders PAID run at 31.3/min in normal. In injection there are 2 (at 03:30:21), then 0 until 03:40:10.
- Things that should keep working do keep working. Gateway GET products is 353 × 200 (plus 384 × 404, the same share as normal), categories 20 × 200, cart GET 28 × 200. Product, inventory, food and banking services have 0 × 5xx.
- Recovery: the user-service restart pod hung in startup and was killed again at 03:37:23 (startup probe). The next container logs "Started UserServiceApplication" at **03:39:59**. Gateway 5xx last until 03:40. Impact therefore lasts from 03:30:35 to 03:40:07, about 9.5 minutes.

### 2.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| user-service sessions blocked on `Lock/relation` with relNames on users, same blockingPid | **present** | 10 sessions from 10.244.3.72 (8 with `users`, 1 with `auth_tokens, idx_auth_tokens_token, users`, 1 with `auth_tokens…`), all with `blockingPid=7850`. An 11th appears from 03:33:53, the restarted pod's seed INSERT. `blocked_session` is 0 in normal and peaks at 12 in injection. |
| blocker itself waiting on users, behind one long ClientWrite session (queryTime → ~420 s), relNames users and auth_tokens | **present** | 7850 waits on 7830, ClientWrite, 393 s (see 2.1). |
| no other commerce table has waits | **present** | All 85 Lock rows in injection involve `users` (14 also list auth_tokens). There are 0 Lock rows outside injection. |
| database CPU stays low | **present** | testbed-postgres-0 `kcm.pod.cpu_usage` averages 41 m in normal and 65 m in injection (max 73 m). |
| commerce-user `HikariPool-1 ... 3000ms (total=10, active=10, idle=0 ...)` | **present** | 54 ERROR rows, 03:30:43–03:31:4x. Pending peaks at 6. |
| gateway `Servlet.service() ... threw exception` for verify-token | **present (mechanism mostly restart)** | 711 rows: **463 `Connection refused`** and **246 `Connect timed out`** on POST `/api/users/verify-token` (user-service not Ready or restarting), 30 `500 Whitelabel` (the 3 s Hikari path) and 2 `Read timed out`. |
| KCM testbed-user Unhealthy, Killing, restarts, no rollout | **present** | Probe failures from 03:30:46. Killing at 03:31:56, restart at 03:32:26. Startup probe failures reach count 61 at 03:35:48. A second kill comes at 03:37:23. restart_count goes 0 → 2. There is no rollout. |
| Top SQL per-call blocks for the token lookup unchanged | **not verifiable** | The token-lookup SELECT never appears in Top SQL. Only the auth_tokens INSERT does: 17.2 blocks per call in normal and 16.2 in injection, with calls falling from 793 to 8. |
| plain reads of users blocked | **present** | The waiters are verify-token and login reads. Their relNames are `users` only. |
| clears at the same minute the export disappears | **partial** | The DB is clean at 03:37:57. The app lags to 03:39:59. |

Metadata notes:
- "later ones after about 3 s" is a minority of failures (30 of 711). Most failures happen while user-service is down.
- Login fails with 502 via the gateway CB `user` (177 OPEN, 61 HALF_OPEN and 94 I/O WARN lines), not 500.
- This is otherwise consistent with the description.

### 2.4 Incident seed: YES
- In [t1, t2) there are **35 anomaly opens**: 16 log-anomaly, 13 stream-anomaly and 6 alarm-bridge.
- By target: PostgreSQL-commerce 12 (5 × alarm-bridge `blocked_session`), commerce-user 10 (Hikari, DataSource health, SQL Error), MySQL-fooddelivery 7, commerce-gateway 6.
- The MySQL opens (03:31:48 update_count, redo and write_bytes level shifts) come from the **hourly food settlement batch**. The same shape fires at 02:31:46 in normal, so it is recurring noise inside the window, not a fault.

### 2.5 Contamination and clocks
- Only the commerce write path has 5xx.
- Harness footprints:
  - ssh 8.6/min in injection.
  - containerd exec at 03:30:23.3.
  - psql probe pid 8049.
  - an older incident ("commerce-cart … 응답이 밀리고") was closed at 03:30:08, just before t1.
- Clocks: KCM lag is p50 1.4 s, max 2.8 s. KCM Killing at 03:31:56 matches containerd StopContainer at 03:31:56.01. The DPM export start (03:30:23.0) matches the exec (03:30:23.3).

### 2.6 Verdict: **USABLE**
The users lock chain is complete, writes and login fail while browsing works, and user-service is drained and restarted.

Caveats:
- Impact extends 2.5 minutes past t2.
- Most 500s are restart-time connection failures, not Hikari waits.
- The Top SQL claim cannot be checked.
- An hourly MySQL settlement anomaly sits in the window.
- There is a psql probe session.

**Root target is clearly identifiable: PostgreSQL-commerce `target:cf97076f-e72d-4c9e-b731-649592cfc5bf`**, table user_schema.users. The blockers are export 7830 and ALTER 7850. Victim: commerce-user `target:252a3a37-77fd-495a-8e12-d79303753fda`, then gateway.

---

## 3. case-f67-h-v3-53919475 — ADD COLUMN on order_schema.order_items queued behind a slow sales export

Phases: normal 20:40:49–22:40:55, buffer to 22:51:05, **injection 22:51:05–22:58:21**, cooldown to 23:28:21. Preflight `clean`. Companion surge.js at 10 rps.

### 3.1 Injection executed: YES (direct trace in DPM sessions)
- **Export pid 26089**: JDBC, ip "", `ClientWrite`, `blocking`. relNames are `idx_order_items_order, order_items, order_items_pkey`. queryTime is 22.4 s at 22:51:34 (so it started at 22:51:11.9) and 388.6 s at 22:57:40. It is gone at 22:58:41, so it ran for 389 to 450 s, in line with the metadata's "360 to 430 s".
- **ALTER pid 26109**: `Lock/relation` on `order_items`, `blockingPid=26089`.
- containerd exec on tb-w1 at 22:51:11.9. kubelet logged 4 `upgradeaware.go Error proxying data` lines at 22:51:10–11, which is the executor's exec stream.

### 3.2 Load ran and impact is visible: YES
- Load: the companion raises gateway traffic from 21–63/min in normal to 193–211/min in injection. The largest gap is 2.1 s and there are no zero minutes.
- Checkout: gateway `POST /api/orders/**` is 155 × 200 in normal. In injection it is **221 × 502 and 2 × 200** (both at t1), with p50 0.31 s and max 15.38 s. Per minute: 22:51 29 × 502, 22:52 37, 22:53 30, 22:54 40, 22:55 23, 22:56 29, 22:57 22, 22:58 11. The first 200 again is at 23:01.
- commerce-payment `POST /api/payments` has 2 spans in injection (both at the start) and then none until 23:01. Payment logs no errors.
- Things that should keep working do keep working (companion load): cart POST 212 × 200, cart DELETE 209 × 200, users POST 140 × 200, products 343 × 200, all 0 × 5xx.
- Recovery: the order restart pod's startup hung, it was killed at 22:57:52, and a new container started at 22:58:23. "Started OrderServiceApplication" appears at **23:01:07**, 2.8 minutes after t2.

### 3.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| order-pod sessions blocked on `Lock/relation` with relNames including order_items and orders, same blockingPid | **present** | 10 sessions from 10.244.3.73 (pool 10). relNames are `order_items, orders, orders_id_seq`, all with `blockingPid=26109`, in every snapshot from 22:51:34 to 22:57:40. |
| blocker itself waiting on order_items behind one long ClientWrite session (queryTime → 360–430 s) | **present** | 26109 waits on 26089 (see 3.1). |
| after the first order restart the shape changes: the stuck checkout sessions show `sessionType=blocking`, and the restarted pod's startup session waits on relation `orders` with blockingPid = a stuck checkout session | **present (exact)** | From the 22:54:37 snapshot, all 10 stuck sessions (25806…25839) are `blocking` with `blockingPid` still 26109. pid 26309 (same pod IP, queryTime 22.4 s, so it started at 22:54:14.6, matching the startup log `HikariPool-1 - Start completed` at 22:54:14.05) is `Lock/relation` on `orders` with `blockingPid=25828`, which is one of the stuck sessions. |
| order_items never in a session snapshot before | **present (in-capture)** | 0 rows with order_items in normal, buffer and cooldown. 89 rows in injection. |
| commerce-order `HikariPool-1 ... 30000ms (total=10, active=10, idle=0 ...)` | **present** | 68 ERROR rows (and 66 SEVERE `CannotCreateTransactionException`), 22:51:47–22:54:14. Pending peaks at 71 (0 in normal). |
| gateway `Downstream unavailable for POST /api/orders/checkout ...` (read timeout, then CallNotPermitted for `order`) | **partial** | 3930 `CallNotPermittedException: CircuitBreaker 'order' is OPEN`, from 22:51:30. 255 `Connection refused` (while order was restarting). 76 `Error while extracting response`. **No `Read timed out` log line.** The 15 s read timeout shows only as the span max of 15.38 s. |
| KCM testbed-order Unhealthy, Killing, restarts, no rollout | **present** | Probe failures from about 22:51:06 (count 3 at 22:51:26). Killing at 22:52:26, restart at 22:52:56. Startup probe failures reach count 68 at 22:56:27. A second kill comes at 22:57:52. restart_count goes 0 → 2. There is no rollout. |
| payment shows no new errors and payment calls stop | **present** | 0 commerce-payment ERROR or WARN. 2 POST /api/payments spans, both at the start. |
| the order line insert disappears from Top SQL | **not verifiable** | The order_items INSERT never appears in Top SQL, not even in normal. Only `insert into order_schema.orders` does (520 calls in normal, 33 in injection). |
| database CPU stays normal | **inaccurate** | testbed-postgres-0 CPU averages 22.6 m in normal and **127.8 m** in injection (max 137 m). It is still low in absolute terms but up about 5.6×. This is the 6.4M-row COPY plus the companion load. |
| clears at the same minute the export disappears | **partial** | The DB is clean at 22:58:41. The app lags to 23:01:07. |

### 3.4 Incident seed: YES
- In [t1, t2) there are **30 anomaly opens**: 17 stream-anomaly, 7 log-anomaly and 6 alarm-bridge.
- By target: PostgreSQL-commerce 20 (alarm-bridge `blocked_session` 10 at 22:52:49, session_utilization, max_tran_time and blk_reads level shifts), commerce-order 8 (`db.client.connections.usage` 10, pending 7, jvm.thread.count, Hikari and DataSource logs), commerce-gateway 2.

### 3.5 Contamination and clocks
- 5xx occur only in commerce-gateway and commerce-order.
- A periodic trace-anomaly batch at 22:40:35 (in the buffer, 10 minutes before t1) opened about 25 caution signals across commerce and food. One of them is commerce-order `INSERT order_schema.order_items`. As a result, the active incident "commerce-order 응답이 매우 느려지고…" carries `first_event_at` 22:40:35, before t1. This is incident-grouping noise, not a fault.
- Harness footprints: ssh 4.1/min in injection, containerd exec at 22:51:11.9, psql probe pid 26223.
- Clocks: KCM lag is p50 1.5 s, max 3.0 s. The DPM export start (22:51:11.9) matches the exec (22:51:11.88). The DPM startup session start matches the pod log to 0.5 s.

### 3.6 Verdict: **USABLE**
Every structural claim, including the post-restart orders-lock shape, is visible exactly. Checkout fails at 99% while everything else works.

Minor metadata issues:
- "DB CPU normal" (it rose about 5.6×).
- The Top SQL "disappears" claim cannot be checked.
- There is no read-timeout log line.

Impact also runs 2.8 minutes past t2, and there is a psql probe session.

**Root target is clearly identifiable: PostgreSQL-commerce `target:cf97076f-e72d-4c9e-b731-649592cfc5bf`**, table order_schema.order_items. The blockers are export 26089 and ALTER 26109. Victim: commerce-order `target:93df2d34-b054-4ecd-8f8e-bafa098dc6cd`.

---

## 4. case-f67-r-v3-654a133c — operator VACUUM (FULL) on payment_schema.payment_logs (and shipment_events)

Phases: normal 07:04:09–09:04:14, buffer to 09:14:22, **injection 09:14:22–09:20:03 (5.7 min)**, cooldown to 09:50:03. Preflight `clean`. Companion commerce surge at 10 rps.

### 4.1 Injection executed: YES (direct trace in DPM sessions)
- **psql pid 20545**: ip "", `sessionType=blocking`, waits `CPU`/`DataFileRead`. relNames are `payment_logs, pg_toast_16838`, plus `idx_payment_logs_payment` and `payment_logs_pkey` later, the index rebuild. There are parallel workers (pids 20586, 20600, 20612). queryTime is 61.6 s at 09:15:32 (so it started at 09:14:30.4) and 311.3 s at 09:19:42. It is gone at 09:20:43.
- **psql pid 20553**: the same on `shipment_events` (and `idx_shipment_events_shipment`). It reached 249.5 s at 09:18:40 and is gone at 09:19:42.
- The rewrite ended at about **09:19:55**: the restarted payment pod's startup resumes at 09:19:57.8. That makes the payment_logs rewrite about **325 s (5.4 min)**, not the "about three minutes (197 s on a local copy)" in the description.
- No VACUUM text in Top SQL, as expected from "probes untracked".
- testbed-postgres-0 CPU averages 58 m in normal and **428 m** in injection (max 499.8 m, at the 0.5 CPU limit). blkio writes go from **0.12 MB/s to 33 MB/s** (max 77 MB/s).

### 4.2 Load ran and impact is visible: YES
- Load: the companion raises gateway traffic to 303–315/min (130–189 in normal). The largest gap is 1.7 s and there are no zero minutes.
- Checkout: gateway `POST /api/orders/**` is 551 × 200 in normal. In injection it is **195 × 502**, 1 × 200 and 2 × 400. Per minute: 09:14 15 × 502, then 32 to 40 per minute through 09:19. At 09:20 there are 7 × 502 and 3 × 200, and from 09:21 it is all 200. No 503 was seen.
- Order logs 1916 `Checkout payment failed, releasing reserved stock` from 09:14:48 to 09:20:37, with matching `Released stock` lines.
- Things that should keep working do keep working. Cart POST 205 × 200, cart DELETE 198 × 200, users POST 98 × 200, product 0 × 5xx, food and banking 0 × 5xx. Cart writes slowed (p50 13 ms in normal, 98 ms in injection) but did not fail.
- User impact lasts from 09:14:36 (first 5xx span start) to 09:20:37, about **6 minutes**, against the metadata's "three to four minutes".

### 4.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| payment-pod sessions blocked on `Lock/relation` with relNames on payment_logs, all pointing to one blockingPid | **present** | 10 sessions from 10.244.3.69 (pool 10). relNames are `payment_logs, payments, payments_id_seq`, `blockingPid=20545`, in every snapshot from 09:15:32 to 09:19:42. In-capture there are 0 rows with payment_logs outside injection. |
| blocker is a client backend from program psql, `blocking`, active with IO/CPU waits, queryTime growing, relNames payment_logs plus indexes | **present** | pid 20545 (see 4.1). |
| a second psql on shipment_events with the shipping pod's sessions behind it | **present** | pid 20553. Shipping session 20228 (10.244.3.74) is blocked behind it, with relNames shipment_events and shipments. |
| nothing waits on payments itself | **inaccurate (minor)** | From 09:17:38 to 09:19:42, pid 20602 (payment pod IP, after its restart) waits on `Lock/relation` on **payments** with `blockingPid=20146`, a stuck payment transaction. |
| heavy write I/O while DB CPU is busy, then everything clears in one minute | **present** | CPU and blkio as in 4.1. PG stream opens on backend_write_size (842 KB vs 2.6 KB) and wal_write_count. All Lock rows are gone by 09:20:43. |
| order logs `Checkout payment failed ...` with read timeouts, then circuit-open | **partial** | All 1916 lines are present. **The first one (09:14:48) already says CB `paymentClient` OPEN.** There are 0 `Read timed out` lines; the others are 76 `Connection refused` and 5 extraction errors. Order's own pool did fill (pending max 64), but there is no Hikari log on order. |
| KCM testbed-payment (and possibly testbed-order) Unhealthy and Killing, no rollout | **present** | payment: probe failures from 09:14:41, Killing at 09:15:41, restart at 09:16:11, startup probe failures reach count 65 at 09:19:42, restart_count 0 → 1. order: liveness and readiness Unhealthy twice (09:14:41–56) with no kill. There is no rollout. |
| payment Hikari full | **present** | 9 `HikariPool-1 ... 30000ms (total=10, active=10, idle=0, waiting=12…)` lines, 09:15:07–09:15:37. Pending max 13. |

Metadata inaccuracies:
- Rewrite duration and outage length (about 5.4 and 6 minutes, against about 3 and "3–4 minutes").
- "nothing waits on payments".
- There is no read-timeout phase in the order logs.

### 4.4 Incident seed: YES
- In [t1, t2) there are **37 anomaly opens**: 24 stream-anomaly, 10 log-anomaly and 3 alarm-bridge.
- By target: PostgreSQL-commerce 25 (backend_write_size, blk_hits, wal_write_count, row_returns and eviction level shifts; alarm-bridge `blocked_session` 11 at 09:16:49), commerce-payment 5 (Hikari, Servlet, scheduled task), commerce-order 3, Oracle-corebanking 2, core-banking-api 1, core-banking-account 1.
- The Oracle opens are a byte drop caused by fewer commerce settlements, which is downstream and consistent.

### 4.5 Contamination and clocks
- 5xx occur only in gateway, order and payment (1 payment 5xx span). food-delivery-payment's restart_count of 1 is unchanged from normal.
- The normal phase has a high level of infrastructure stream noise: 725 host and switch opens, none in the window.
- Harness footprints: ssh 5.8/min in injection, and the psql probe pid 20596, a second psql session on payment_logs (`blocked`, so it is distinguishable from the `blocking` VACUUM).
- t2 (09:20:03) falls about 8 s after the rewrite ended. Recovery (Started 09:20:30) is in cooldown.
- Clocks: KCM lag is p50 1.6 s, max 3.0 s. KCM Killing at 09:15:41 matches containerd StopContainer at 09:15:41.02.

### 4.6 Verdict: **USABLE-WITH-CAVEATS**
The VACUUM-FULL blocker signature (psql, `blocking`, CPU/IO, payment_logs plus indexes, its twin on shipment_events, and DB CPU at the limit with 33 MB/s writes) is unambiguous, and checkout fails at 99%.

However, the duration claims are wrong (5.4-minute rewrite and 6-minute outage against about 3 and 3–4 minutes), the "nothing waits on payments" and read-timeout claims do not hold, and the injection window ends only about 8 s after the blocker.

**Root target is clearly identifiable: PostgreSQL-commerce `target:cf97076f-e72d-4c9e-b731-649592cfc5bf`**, table payment_schema.payment_logs (operator psql pid 20545). Victims: commerce-payment `target:c3a3587e-215e-4da8-a76c-729732394e40`, then order `93df2d34…`.

---

## 5. case-f68-r-v3-3f6561ef — transfer-service release core-banking-transfer:2.4.0 (one global `transferVelocity` rate limiter)

Phases: normal 16:52:58–18:53:02, buffer to 19:03:11, **injection 19:03:11–19:18:08**, cooldown to 19:48:08. Preflight `clean`. Companion transfer-balance-surge at 7 rps.

### 5.1 Injection executed: YES
- KCM (rca-testbed-banking):
  - 19:03:30 `ScalingReplicaSet` `Scaled up replica set testbed-transfer-86db85ffbc to 1` and `Scaled down … 8c7fb57cb to 0` (maxSurge 0).
  - 19:03:31 Pod 86db85ffbc-dsdq4 `Pulled: Container image "core-banking-transfer:2.4.0" already present on machine` on tb-w2.
  - Rollback at 19:15:26: 8c7fb57cb is scaled up with `:latest` and 86db85ffbc goes to 0.
- `postgres/kcm_resources_history.csv`: the RS and pod for 86db85ffbc carry `core-banking-transfer:2.4.0`; 8c7fb57cb carries `:latest`.
- syslog tb-w2: 19:03:27 `ctr -n k8s.io images import -`, containerd `ImageCreate … core-banking-transfer:2.4.0` at 19:03:29.56, and 19:18:06 `ctr … images rm docker.io/library/core-banking-transfer:2.4.0`. The rm is **2 s before t2**.

### 5.2 Load ran and impact is visible: YES
- Load: core-banking-api runs at 12–30 spans/min in injection (median 3/min in normal), with no zero minutes. The largest gap is 14.6 s. commerce-gateway has no zero minutes either.
- **Phase structure inside [t1, t2):**
  - 19:03:30–19:06:24, no transfer pod. The release pod logs "Started TransferServiceApplication" only at **19:06:24**, about 3 minutes after the rollout; the metadata says about 1 minute.
  - **19:06:24–19:15:26, the release serving traffic (about 9 minutes).**
  - 19:15:26–19:17:53, no pod again during the rollback; the baseline pod started at 19:17:53.
  - About 5.4 minutes of the 15-minute window are therefore "transfer unreachable": api POST transfers 54 + 59 × 502, history GET 5 + 2 × 502, and account `I/O error … Connection refused`.
- Release serving window, 19:06:30–19:15:26:
  - api `POST /api/transfers` is **152 × 502 and 29 × 200 (84% fail)**; normal is 78 × 200.
  - Commerce checkouts: **10 failed of 74 (13.5%)**, 8 of them carrying `core-banking transfer call failed: 500`. Normal has 0 failures.
- Things that should keep working do keep working (serving window): account `GET /api/accounts/{id}` 177 × 200, `GET /api/accounts` 38 × 200, history `GET /api/transfers` 17 × 200. Commerce browsing, carts and login are 0 × 5xx. Food is 0 × 5xx.

### 5.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM ScalingReplicaSet plus `Pulled … "core-banking-transfer:2.4.0" already present on machine` | **present** | 19:03:30–31 (see 5.1). |
| transfer SEVERE `Servlet.service() … RequestNotPermitted: RateLimiter 'transferVelocity' does not permit further calls` | **present** | **331 rows, 19:06:34–19:15:21**, 18–42 per minute. There are 0 `RateLimiter` or `RequestNotPermitted` rows in normal, buffer or cooldown. |
| interleaved `Transfer COMPLETED`; completions never above about 2/s | **present (approximately)** | 354 COMPLETED in the serving window, **0.66/s on average**. Per wall-clock second: 1 in 87 seconds, 2 in 110, 3 in 13, 4 in 2. This is consistent with a 2/s limiter once log timestamps straddle second boundaries. Normal also peaks at 4 per second. |
| refusals hit customer accounts sending well under 1/s and the settlement account alike | **present** | Account `Transfer service call failed … 500` lines joined to `Account validation passed: from=` by trace_id: ACC-1005 54, ACC-1002 47, ACC-1007 47, ACC-1006 43, ACC-1004 39, ACC-1001 38, ACC-1003 38. Plus commerce-settlement: 33 checkouts with `core-banking transfer call failed: 500` overall, 8 in the serving window. Completions also span all accounts (commerce-settlement 64, ACC-1001..1007 33–47 each). |
| transfer server spans POST /api/transfers answer 500 in a few ms without DB spans | **present** | 31 × 500 in the serving window, p50 10.9 ms. **0 of 31 have any CLIENT (DB) descendant.** The 37 × 200 spans each have `SELECT`/`UPDATE freepdb1.accounts` children. |
| account `Transfer service call failed for order null: 500 … Whitelabel` and `Transfer service circuit open/exhausted` | **present** | 306 Whitelabel and 306 matching circuit lines. 1714 `CircuitBreaker 'transferClient' is OPEN`. |
| api 502 for transfers | **present** | 7821 `CircuitBreaker 'accountClient' is OPEN`, 699 `Account service call failed … 502`, plus spans (see 5.2). |
| commerce order `Checkout payment failed, releasing reserved stock` with `core-banking transfer call failed: 500` | **present** | 73 in injection: 33 with the transfer-500 body and 40 with `CircuitBreaker 'paymentClient' is OPEN`. |
| transfer Ready with no restarts, no Hikari waits, no ORA, no lock waits; Oracle CPU and sessions usual | **present** | Release pod restart_count is 0. 0 `Connection is not available`, 0 `ORA-`. Hikari pending is 0. `dpm.oracle.instance.cpu_time` averages 0.18 in normal and 0.13 in injection. Oracle active_session averages 0.07. |
| failure share follows the companion load; before the ramp only bursts fail | **not testable** | The companion starts at t1 (19:03:22 rate_spike), before the release pod is up, so there is no pre-ramp release period. |

Metadata inaccuracies:
- "every transfer fails for about a minute until the new pod is Ready": it is about 3 minutes.
- "about one in five commerce checkouts fails": 13.5% in the serving window.
- The rollback's 2.5-minute no-pod gap also falls inside [t1, t2).

### 5.4 Incident seed: YES
- In [t1, t2) there are **31 anomaly opens**: 24 log-anomaly and 7 stream-anomaly; there is no alarm-bridge.
- By target: core-banking-api 10, core-banking-account 9, commerce-order 3, core-banking-transfer 3 (log-rule freq at 19:06:34 and 19:06:40, the RequestNotPermitted start), Oracle-corebanking 2 (logical_reads drop: 211 against a baseline of 34,039), commerce-notification 2, MySQL 1, ledger 1.

### 5.5 Contamination and clocks
- 5xx occur only in the banking transfer chain and commerce order and payment.
- An hourly PostgreSQL-commerce update surge at 19:00:51 (buffer) is merged into the incident "결제 서비스 장애로 commerce-order…", which gives it `first_event_at` 19:00:51, before t1. This is grouping noise.
- Harness footprints: ssh 11.8/min in injection, `ctr images ls` polling, `ctr import` at 19:03:27, `ctr images rm` at 19:18:06–07, inside the window.
- Clocks: KCM lag is p50 1.6 s, max 3.2 s. ImageCreate (19:03:29.56) matches ScalingReplicaSet (19:03:30). The first RequestNotPermitted (19:06:34) comes 10 s after "Started".

### 5.6 Verdict: **USABLE-WITH-CAVEATS**
The 2.4.0 rollout plus 331 `RateLimiter 'transferVelocity'` refusals spread over every debit account, from a healthy, fast transfer with no DB spans, is decisive.

However, about 5.4 of the 15 minutes of [t1, t2) are transfer-unreachable periods (a 3-minute slow start, and the rollback inside the window) that look like F17/F65 "connection refused". The metadata understates the start-up gap and overstates the commerce failure share.

**Root target is clearly identifiable: core-banking-transfer `target:f0ce5940-a7e2-44a2-843f-196de379d898`**, Deployment `testbed-transfer` / ns `rca-testbed-banking` (KCM Deployment target_id 8d83bd66-d023-4ef7-9c6c-7a0c3fa16d4e), release RS testbed-transfer-86db85ffbc. Symptom services: core-banking-api `b3d586c3…`, account `c2efb1d9…`, commerce-order `93df2d34…`.

---

## 6. case-f69-r-v3-ca74a36b — payment-audit-export Job running mysqldump with default --lock-tables

Phases: normal 20:37:02–22:37:06, buffer to 22:47:24, **injection 22:47:24–22:52:08 (4.7 min)**, cooldown to 23:22:08. Preflight `clean`. There is no companion load.

### 6.1 Injection executed: YES (direct trace)
- KCM rca-testbed-food:
  - 22:47:25 Job `payment-audit-export` `SuccessfulCreate`.
  - Pod `payment-audit-export-j4nf5` `Scheduled` to tb-w3, `Pulled: Container image "mysql:8.0" already present on machine`.
  - 22:47:26 `Created` and `Started container export`.
  - 22:51:37 `Killing` (cleanup).
- `kcm_resources_history`: Job and pod, image mysql:8.0, label `app=payment-audit-export`.
- **process_meta on tb-w3** (22:47:43): `mysqldump --host=testbed-mysql --user=fooddelivery --no-tablespaces fooddelivery payments settlement_summary` (pid 2971728) piped to `xz -6 -T1` (pid 2971729). The bash wrapper writes to `/work/...sql.xz`. xz runs at about 14.5% CPU, and the export pod's CPU sits at 150 m, its limit.
- The dump never finished: the processes were still running at 22:50:43, and the Job was deleted at 22:51:37.

### 6.2 Load ran and impact is visible: YES
- Load:
  - food-delivery-restaurant runs at 18–30 spans/min in injection with no zero minutes; the largest gap is 14 s (49 s in normal).
  - `Created order id=` runs at 12.0/min in normal, 21.6/min in buffer and **25.1/min in injection**.
- Impact:
  - food order `POST /api/orders` is 134 × 200 in normal. In injection it is **17 × 502 and 0 × 200** (p50 0.33 s, max 10.34 s).
  - `Processed payment id=` drops from 21.6/min in buffer to **0 for the whole injection**, then 19 at 22:52.
  - `Order fan-out payment failed … status=502`: 118 rows, 84 of them CB `payment` OPEN.
- Things that should keep working do keep working. Restaurant and dispatch are 0 × 5xx; dispatch runs at 8.0 spans/min against 3.9 in normal (courier requests continue). Orders keep being created. Commerce and banking are 0 × 5xx.

### 6.3 Decisive evidence
| Item | Status | Evidence |
|---|---|---|
| KCM Job SuccessfulCreate plus pod Scheduled tb-w3, `Pulled … "mysql:8.0" already present`, Created and Started `export`, right before the failures | **present** | 22:47:25–26. The first failure log is at 22:47:42. |
| DPM: a fooddelivery session from the export pod's IP (not a service pod) with queryTime rising every minute | **present** | tid 16599 from **10.244.2.54**, an IP seen only in the 4 injection snapshots (payment is .42 and order is .45). queryTime is 47.8 s at 22:48:14 (so it started at 22:47:26.2, matching the container start) and 230.9 s at 22:51:17. It is gone at 22:52:18. |
| …in state 'Sending to client' | **absent** | The state is `executing` in all 4 snapshots. 'Sending to client' appears 0 times in the capture. |
| payment-pod sessions in 'Waiting for table metadata lock' | **present** | 15 sessions from 10.244.2.42 (the pool of 15) in every snapshot. That is 60 rows in injection and 0 in other phases. |
| no session runs or waits with a schema change | **present** | No ALTER or DDL states. Table count (16) and index count (31) for fooddelivery are unchanged. |
| after the dump, Top SQL shows `SELECT /*!40001 SQL_NO_CACHE */ * FROM `payments`` | **absent** | 0 Top SQL rows with SQL_NO_CACHE or settlement_summary. The dump was killed by cleanup before it finished, so this "once the dump ends" evidence cannot exist. |
| payment stops logging `Processed payment id=` and logs Hikari `… 3000ms` | **present** | Processed payments: 0 in injection. Hikari: 29 lines `HikariPool-1 ... 3000ms (total=15, active=15, idle=0 …)` (25 at 22:48, 4 at 22:49). |
| order keeps logging `Created order id=` and adds `Failed to process payment` (Read timed out, then 500) and `Order fan-out payment failed … status=502` | **partial** | Created orders and the fan-out 502s are present. The `Failed to process payment` causes are **73 `Connection refused`** (payment restarting), 15 `Error while extracting response … PaymentResponse`, and 6 `500 Whitelabel`. **There are 0 `Read timed out`.** |
| KCM testbed-payment Unhealthy and Killing, no rollout | **present** | Readiness and liveness fail from 22:48:04. Killing at 22:49:14, restart at 22:49:44. restart_count goes 2 → 3. The pod already had 2 restarts at capture start, and the KCM event counts are lifetime counts: the first event shows Unhealthy count 21. |
| MySQL and order stay Ready; MySQL CPU and data-file writes normal | **present** | No MySQL or order KCM events, and MySQL restart is 0. `testbed-mysql-0` CPU averages 38 m in normal and 53 m in injection (normal max 222 m). `data_file_write_bytes` averages 239 kB in normal and 216 kB in injection. |

### 6.4 Incident seed: YES (small)
- In [t1, t2) there are **13 anomaly opens**: 10 log-anomaly and 3 stream-anomaly; there is no alarm-bridge.
- By target: food-delivery-payment 5 (Hikari, SQL Error, DataSource health), food-delivery-order 5 (`Order fan-out payment failed … BAD_GATEWAY`), MySQL-fooddelivery 3 (rollback_count 22.6 against 4.7, data_sent, row_sent).

### 6.5 Contamination and clocks
- 5xx occur only in food order and payment.
- A MySQL tps/qps shift at 22:41 and an Oracle reads shift and ledger mismatch at 22:42–22:44 in the buffer are recurring baseline.
- The incidents table has an active "food-delivery-dispatch 지연이…" incident with first_event 20:36:49, created at 23:02. This is grouping noise.
- Harness footprints: ssh 12/min in injection and the Job itself. There is no scenario id in the Job, its labels or its script.
- Clocks: KCM lag is p50 1.6 s, max 3.1 s. The DPM session start (22:47:26.2) matches containerd StartContainer (22:47:26.1) and KCM Started (22:47:26). The first order error log (22:47:42.8) and the first 5xx span (22:47:44.3) agree.

### 6.6 Verdict: **USABLE-WITH-CAVEATS**
The Job, its mysqldump/xz command line in process_meta, the single long export session from a new IP, and 15 payment sessions in metadata-lock wait make the root unambiguous. Every food order fails at payment while orders, dispatch and MySQL stay healthy.

Caveats:
- The metadata's 'Sending to client' state and post-dump Top SQL statement are absent (state `executing`; dump killed by cleanup).
- There is no read-timeout phase.
- The window is short (4.7 minutes).
- The payment pod's restart history predates the capture.

**Root target is clearly identifiable: Job `payment-audit-export` / ns `rca-testbed-food`** (pod payment-audit-export-j4nf5 on tb-w3; KCM pod target_id e3c8072e-9c86-4f04-aa8b-9323a2fa31d9), acting on MySQL-fooddelivery `target:c8c558e5-711f-4e6e-b00f-1adeb097c795` (tables payments and settlement_summary). Victims: food-delivery-payment `target:2e6120e3-37fa-4c44-a224-6ab13afdd9ee`, then food-delivery-order `fa8b5cc4…`.

---

## Cross-case checks
- **Load generator:** never stopped. Every case has non-zero per-minute SERVER spans on its entry service in every phase. Companion surges (F67-H, F67-R, F68-R) ran through the whole injection. The only k6 saturation warning is one `Insufficient VUs` line (F66-P, 10:39:51).
- **Unrelated incidents:** none. 5xx SERVER spans occur only in each scenario's own chain. The anomalies in the windows that are not the fault are recurring baselines:
  - the hourly food settlement MySQL shift (F66-R);
  - ledger reconciliation logs (F66-P);
  - Oracle byte or read drops, which follow from fewer commerce settlements (F66-P, F67-R, F68-R).
- **Clock skew:** none. Across the six cases:
  - KCM collected-minus-event lag is p50 1.4–1.6 s, max 2.8–3.2 s.
  - Log observed-minus-timestamp is at most 0.5 s.
  - DPM session starts derived from queryTime match the harness exec or container-start timestamps to within 0.3 s.
- **Harness artifacts that a model could see:**
  - the psql probe session in the PG lock queue (all four DB-lock cases);
  - the `ctr images rm` line 2 s before t2 (F68-R).

## Summary
| Case | Verdict | Root target | Key caveat |
|---|---|---|---|
| case-f66-p-v3-a79bda26 | **USABLE-WITH-CAVEATS** | PostgreSQL-commerce `cf97076f-e72d-4c9e-b731-649592cfc5bf` (inventory_schema.outbox_events: export pid 11344 → ALTER 11363 → 10 inventory sessions); victim commerce-inventory `414db206…` | user_impact over-claims: product lists and categories fail only 31 s (CB open), and most product errors are 3 s connect timeouts, not 10 s. Gateway checkout CallNotPermitted absent. Impact lasts about 9 minutes (to 10:48:34), past t2. psql probe in the lock queue. |
| case-f66-r-v3-346c6170 | **USABLE** | PostgreSQL-commerce `cf97076f…` (user_schema.users: export 7830 → ALTER 7850 → 10 user-service sessions); victim commerce-user `252a3a37…` | Most verify-token 500s are restart-time connection failures (709 of 711). The token Top SQL claim cannot be checked. An hourly MySQL settlement anomaly sits in the window. Recovery 2.5 minutes after t2. psql probe. |
| case-f67-h-v3-53919475 | **USABLE** | PostgreSQL-commerce `cf97076f…` (order_schema.order_items: export 26089 → ALTER 26109 → 10 order sessions; post-restart orders-lock shape exact); victim commerce-order `93df2d34…` | "DB CPU normal" inaccurate (22.6 → 127.8 m). The order_items INSERT is never in Top SQL. No read-timeout log line. Recovery 2.8 minutes after t2. psql probe. |
| case-f67-r-v3-654a133c | **USABLE-WITH-CAVEATS** | PostgreSQL-commerce `cf97076f…` (VACUUM FULL payment_logs by psql pid 20545; twin 20553 on shipment_events); victim commerce-payment `c3a3587e…` | Rewrite about 5.4 minutes and outage about 6 minutes, against the claimed about 3 and 3–4 minutes. "Nothing waits on payments" is false (pid 20602). No read-timeout phase. t2 falls about 8 s after the blocker ends. Second psql (probe) on payment_logs. |
| case-f68-r-v3-3f6561ef | **USABLE-WITH-CAVEATS** | core-banking-transfer `f0ce5940-a7e2-44a2-843f-196de379d898`, Deployment testbed-transfer / rca-testbed-banking (RS 86db85ffbc, image 2.4.0) | About 5.4 of the 15 minutes in [t1, t2) have no transfer pod (3-minute slow start against "about 1 minute", and the rollback inside the window), which looks like connection-refused faults. Commerce failure share 13.5% against "1 in 5". `ctr images rm` 2 s before t2. |
| case-f69-r-v3-ca74a36b | **USABLE-WITH-CAVEATS** | Job payment-audit-export / rca-testbed-food (mysqldump --lock-tables on MySQL-fooddelivery `c8c558e5…`, tables payments and settlement_summary); victim food-delivery-payment `2e6120e3…` | Export session state is `executing`, not 'Sending to client'. No mysqldump SELECT in Top SQL (dump killed by cleanup). No read-timeout phase (connection refused after the payment restart dominates). Short 4.7-minute window. Payment restart history predates the capture. |
