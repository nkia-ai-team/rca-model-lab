# Frontier RCA trajectory — commerce-shipping shipment-creation SLA breach (2026-07-21 ~18:29–18:40 UTC)

Anchor: commerce-shipping (452da2b8-…, application). Named upstream: commerce-order (93df2d34-…).
Investigation window worked: 2026-07-21 17:50–18:45 UTC. All tool access via `$TOOL` CLI only.

---

## A. Step-by-step trajectory

**1. `discover_signals` on commerce-shipping (18:20–18:45).**
- Reason: intended starting point — get the target's metric inventory + top anomalies vs baseline.
- Key fact: dominant anomalies were **Kafka consumer rebalance metrics** (`rebalance_latency_avg/max`≈3186, `join_time`≈3002, `partition_assigned_latency`, `sync_time`) all "newly appeared." 201-metric inventory dominated by `kafka.consumer.*` and `kafka.producer.*`. Latency (apm.percentile*) values were microseconds/tiny.
- Decision: shipping is a Kafka consumer (consumes order events → creates shipments). Hypothesis H1 = consumer rebalance stalled consumption. Probe the direct backlog signal next.

**2. `get_metric_series` records_lag_max (18:20–18:45, baseline 17:50–18:15).**
- Reason: rebalance is only meaningful if a backlog actually formed (mechanism to "shipment created late").
- Key fact: lag avg 157, **max 1087**, last 0 → a backlog built and fully drained within the window. Baseline had no observation.
- Decision: confirmed backlog→drain. Now find *why* the consumer stopped. Check platform events + changes.

**3. `get_events` + `get_changes` on shipping.**
- Reason: cheapest broad read of platform-detected anomalies and any deploy/config change.
- Key fact: only a **trace-anomaly (distribution_shift)** at 18:28:38 and 18:29:40–18:34:40 (caution). **Zero changes** (no deploy/collector/config). 
- Decision: rebalance was NOT caused by a shipping deployment. Rule out change-driven. Decompose where shipping spends time.

**4. `get_trace_breakdown` + `get_slow_endpoints` on commerce-shipping.**
- Reason: separate self vs downstream time; find the slow path.
- Key fact: the ONLY SERVER spans are **GET /actuator/health** (17 reqs, 0 errors). Children: postgresql (5892 spans, p95 1.66ms, 0 err) and "commerce.shipping publish" (Kafka producer). So shipment creation is **Kafka-consumer-driven, not HTTP** — invisible to SERVER-span tools. DB fast, no errors.
- Decision: shipping's HTTP/DB path is healthy. Damage is on the consumer side. Fork: is the rebalance shipping-specific or Kafka-broker-wide?

**5. `get_cohort` (rebalance_latency_avg) + `get_topology` (1-hop).**
- Reason: broker-wide event would rebalance many groups; a single-group event is local.
- Key fact: **only shipping** has rebalance metrics; all 9 peers have none in-window → NOT broker-wide. Topology (static): shipping→db:postgresql (apm_db); order→shipping (apm_call). Kafka is not a topology node.
- Decision: local to shipping's consumer group. Identify peers; verify the "baseline-empty" isn't a collector gap.

**6. `get_target_meta` (10 peers) + `get_data_coverage` (shipping).**
- Reason: know the neighborhood; distinguish measured-normal from not-collected.
- Key fact: peers = the 10 commerce microservices (order/user/inventory/product/payment/pricing/gateway/notification/cart). Coverage: shipping has **11,633 logs, 6,766 series, 3 events** in-window (collector "pending" caveat noted). Data is rich, not a gap.
- Decision: trust the data. Read the logs — they should tell the story.

**7. `get_logs` (summary) + drill filters (rebalance/partitions/Consumer/commit/shipment).**
- Reason: logs are the highest-bandwidth explanation.
- Key fact: 11,613 lines, **all INFO, zero WARN/ERROR**. 11,560 mention "shipment." "Consumer/partitions/commit" logs cluster tightly at **18:38:18–18:38:33** (consumer group rejoin / partition-assignment / commit). No "rebalance" text.
- Decision: a consumer group event completed at ~18:38 (near incident end). Timeline forming: stall → rejoin 18:38 → drain. Check JVM + consumer rate to see if the poll loop was frozen.

**8. `get_metric_series` ×6: jvm.cpu, jvm.memory.used, jvm.thread.count, apm.rps, records_consumed_rate, last_poll_seconds_ago.**
- Reason: is this a GC/resource stall?
- Key fact (correction #1): **baseline DID have** `records_consumed_rate`≈0.24/s and `last_poll_seconds_ago`≈3 → the consumer was running before; rebalance metrics were absent only because no rebalance was occurring. During incident: consumed_rate burst to **max 91.8/s** (catch-up), CPU brief 100%, memory/threads flat, `last_poll` max 4s (**not frozen**).
- Decision: a 3s rebalance pause at 0.24/s can't make 1087 lag → the lag must come from **production > consumption**. Pivot upstream to commerce-order.

**9. `discover_signals` on commerce-order.**
- Reason: named upstream; look for a produce burst.
- Key fact: no errors; `record_queue_time_max` 18ms (small → producer not batching/holding). No dramatic single anomaly flagged. rps tiny.
- Decision: order's producer isn't obviously bursting per the ±50% flags; measure `record_send_rate` directly and time-slice the lag.

**10. order `record_send_rate` (baseline vs incident) + shipping `records_lag_max` in 5-min slices.**
- Reason: localize when lag built and whether order produced a burst.
- Key fact: order send **avg 2.59/s, max 17.76/s** vs baseline 0.25 (+934%). Lag was **0 until 18:36**, then **1087 at 18:36–18:41**, then 0.
- Decision: order DID burst. Narrow the co-timing of production vs consumption.

**11. shipping consumed_rate slices + order send slices + order lag.**
- Reason: pin the exact sequence.
- Key fact:
  - order send: 18:26–18:31 = 0.76/s; **18:31–18:36 = 11.8 avg / 17.8 max**; 18:36–18:41 = 0.
  - shipping consumed: 18:31–18:36 = **0**; 18:36–18:38:30 = **0**; 18:38:30–18:41 = 20.5 avg / **91.8 max**; then ~0.
  - order consumer lag = 0/0 (order did not flush its own backlog).
- Decision: sharp timeline — order burst 18:31–18:36 while **shipping consumed nothing until 18:38:30**, then drained. Two coincident things: order burst + shipping ~7-min zero-consume. Investigate why shipping consumed nothing.

**12. shipping consumer internals: last_poll(stall window), assigned_partitions, producer send, commit_rate, rebalance_total, last_rebalance_seconds_ago, connection_count.**
- Reason: test the rebalance hypothesis rigorously.
- Key fact (correction #2 — rebalance hypothesis DEAD): `assigned_partitions`=**constant 3** (never revoked); `rebalance_total`=**flat 1**; `last_rebalance_seconds_ago`≈**180h ago** (no rebalance during incident); `last_poll_seconds_ago` max 4s (kept polling). The rebalance_latency gauge from step 1 was **stale/startup**, not an in-window rebalance.
- Decision: consumer held partitions and kept polling but delivered 0 records while lag grew → a **fetch/delivery** problem, not a rebalance. Check the fetch path.

**13. shipping fetch metrics: fetch_rate, fetch_latency_avg/max, fetch_size_avg, bytes_consumed_rate, throttle, records_per_request, time_between_poll_max.**
- Reason: is the broker failing to serve fetches?
- Key fact: fetch_rate ~1.9/s (normal), fetch_latency ~500ms (**unchanged**), no throttling. `fetch_size_avg` +17000% and `records_per_request_avg` 171 (max 1196) = the **catch-up** signature (huge batches on resume). time_between_poll_max 9.3s (mildly up).
- Decision: broker fetch is healthy; the big-batch numbers are recovery, not cause. So the consumer *client* is fine — the *application listener* wasn't taking records. Find the burst origin and confirm the listener halt.

**14. record_send_rate across order/payment/cart/gateway/notification/inventory + order consumed_rate + order events.**
- Reason: is this shipping-only or a pipeline-wide surge? Where does it originate?
- Key fact: **order, payment, AND inventory all show the identical burst** (avg ~2.6/s, max ~18/s, baseline ~0.25–0.30); cart/gateway/notification = 0. Order **consumed** 17.7/s and **produced** 17.7/s (1:1 relay). No order events/anomalies.
- Decision: a **synchronized ~70x event surge** cascaded order→payment→inventory→shipping (HTTP rps stayed ~0.05 → Kafka-event surge, not user traffic). Yet only shipping accumulated lag → shipping's consumer is the differentiated failure. Confirm the listener halt via log time-distribution + characterize the pause.

**15. shipping poll_idle_ratio, last_heartbeat, io_wait, commit_latency + log volume in 6 slices.**
- Reason: distinguish "listener busy" vs "listener not running"; timestamp the halt.
- Key fact: heartbeat every ~1s (max 2s), poll_idle/io_wait normal → **Kafka client thread healthy throughout**. Log volume: 18:20–26 = 346, 18:26–31 = 219, **18:31–36 = 0**, 18:36–38:30 = 28, **18:38:30–41 = 7312 (flood)**, 18:41–45 = 3708. Application went **completely silent for ~7 min**, then flooded.
- Decision: listener/processing thread was halted (0 logs) while the consumer client stayed in the group (heartbeat, partitions). Classic paused/blocked-listener signature. Rule out the last plausible blocker — the DB.

**16. shipping DB pool during stall (usage/pending/timeouts/max/idle) + topology hops=2.**
- Reason: a multi-minute synchronous block is usually a DB lock/pool-wait.
- Key fact: during 18:30–18:39 DB pool `usage` avg **1.7 (max 3, LOWER than baseline 2)**, `pending_requests`=**0**, no timeouts, idle.min=3. → listener was **not** blocked on the DB (pool idle). Topology hops=2: shipping's only downstream is db:postgresql; no outbound apm_call to another app.
- Decision: not a DB block, not resource exhaustion, no restart (JVM series continuous), no rebalance, no errors. The listener simply did no work for 7 min → an **in-process consumption pause**. Sufficient mechanism reached; stop.

---

## B. Causal graph (facts → arrows)

Facts (with backing ref):
- F1 shipping records_lag_max → 1087 then 0. `vm:kafka.consumer.records_lag_max{452da2b8}` 18:36–18:41.
- F2 shipping records_consumed_rate = 0 during 18:31–18:38:30, then max 91.8/s. `vm:kafka.consumer.records_consumed_rate{452da2b8}` slices.
- F3 shipping application logs = 0 during 18:31–18:36 (28 to 18:38:30), then 7312 flood. `tool:get_logs{452da2b8}` slices.
- F4 shipping consumer client healthy: assigned_partitions=3 constant, rebalance_total flat, last_rebalance ~180h ago, heartbeat ~1s, last_poll ≤4s. `vm:kafka.consumer.*{452da2b8}`.
- F5 shipping DB pool idle during stall: usage 1.7, pending 0. `vm:db.client.connections.*{452da2b8}`.
- F6 shipping JVM continuous (no restart), DB p95 1.66ms, 0 errors. `vm:apm.agent.otel.java.jvm.*`, `ch:otel_traces_local` breakdown.
- F7 order/payment/inventory synchronized send burst ~0.25→max ~18/s at 18:31–18:36. `vm:kafka.producer.record_send_rate{93df2d34,c3a3587e,414db206}`.
- F8 order consumer lag = 0; order consumed≈produced (1:1). `vm:kafka.consumer.records_lag_max/records_consumed_rate{93df2d34}`.
- F9 HTTP rps stayed ~0.05/s (no user-traffic surge). `vm:apm.agent.otel.java.rps`.
- F10 shipping trace distribution_shift 18:28:38 & 18:29:40–18:34:40. `ch:lucida_events_local:aae10552…`.

Arrows:
- (root) **shipping consumer listener halted ~18:31–18:38:30** →[verified by F2,F3,F4,F5] shipment records not created during halt.
- shipment records not created →[supported] shipment-creation SLA violated + downstream status updates lag (symptom).
- order-pipeline event surge (F7,F8) →[supported] backlog inflated to 1087 while listener halted (F1). Surge is aggravator, not necessary cause of SLA breach (see forks).
- listener resumes ~18:38:30 (F3 flood, step-7 rejoin/commit logs) →[verified] catch-up drain at 91.8/s (F2) → lag→0 by 18:41.
- rebalance →[contradicted by F4] NOT the cause (no in-window rebalance).
- DB lock / pool exhaustion →[contradicted by F5] NOT the cause.
- GC / OOM / pod restart →[contradicted by F6] NOT the cause.
- surge alone →[contradicted] peak 18/s ≪ shipping's 91.8/s capacity and peers held lag 0 (F8) → surge alone cannot breach SLA.

---

## C. Forks considered and how closed

1. **Consumer rebalance stalled consumption** (initial H1). Closed/contradicted: assigned_partitions constant 3, rebalance_total flat, last_rebalance ~180h ago (F4). The step-1 rebalance_latency gauge was stale.
2. **Broker/Kafka-wide outage.** Closed: cohort shows only shipping with rebalance/lag metrics; order lag 0; fetch_latency normal (F4, step-5, step-13).
3. **Deployment/config change to shipping.** Closed: get_changes = 0 across all 3 sources (step-3).
4. **JVM GC / OOM / pod restart.** Closed: memory/thread series flat & continuous, no restart gap (F6).
5. **DB lock / connection-pool exhaustion blocking the listener.** Closed: DB pool idle, pending_requests 0, DB p95 1.66ms during stall (F5, F6).
6. **Upstream (order) delayed/batched event emission (outbox stall).** Parked/weakened: order producer record_queue_time only 18ms (not holding), order lag 0. Order burst is real but is a pipeline-wide surge, and it does not explain shipping's zero-consume-with-idle-DB gap.
7. **Load surge is THE root cause.** Downgraded to aggravator: surge peak 18/s ≪ shipping capacity 91.8/s; peer consumers absorbed the same surge at lag 0. Necessary cause of the breach is shipping's own halt.

---

## D. Widening moments (what made the next target visible)

- **shipping → "Kafka consumer" domain (step 1→2):** `discover_signals` inventory was dominated by `kafka.consumer.*`; rebalance/join metrics were the top anomalies. The metric *names* pulled me from "generic app latency" to "this is a Kafka consumer problem."
- **SERVER-span dead-end → consumer path (step 4):** `get_trace_breakdown` showing only /actuator/health as SERVER spans told me the business work is consumer-driven and invisible to HTTP tools — reframed the whole investigation onto Kafka consumer metrics.
- **shipping → commerce-order (step 8→9):** the arithmetic contradiction (1087 lag impossible from a 3s pause at 0.24/s) forced "production must exceed consumption," and the symptom's named upstream + shipping's own producer child span ("commerce.shipping publish") pointed at the event pipeline. The trigger to widen was a *quantitative impossibility*, not a single metric.
- **order → payment/inventory pipeline-wide surge (step 13→14):** after seeing order's send-rate disproportion (18/s produced vs 0.05/s HTTP rps), I fanned `record_send_rate` across all commerce apps. Finding order/payment/inventory with *identical* burst magnitudes revealed a synchronized cascade — the attractor was the "events-per-request" disproportion.
- **back to shipping as the differentiated victim (step 14→15):** the asymmetry "peers lag 0, shipping lag 1087 under the same surge" is what re-focused the root cause onto shipping's consumer rather than the surge.

---

## E. Conclusion

**Most-likely root cause:** commerce-shipping's Kafka order-event **consumer stopped delivering records to its listener for ~7 minutes (≈18:31–18:38:30 UTC)** — an in-process consumption halt/pause. Throughout the halt the JVM ran continuously, the Kafka client stayed in the group (heartbeating every ~1s, all 3 partitions retained, **no rebalance**), the DB pool sat idle, and the application emitted **zero logs** — i.e., the listener did no work, it was not merely slow. When it resumed at ~18:38:30 it drained the accumulated backlog at up to 91.8 rec/s, flushing 7,300+ log lines, and lag returned to 0 by ~18:41.

**Mechanism chain to the symptom:**
consumer listener halt (18:31–18:38:30) → order-placed events accumulate unconsumed (lag → 1087, amplified by a coincident ~70x order-pipeline event surge) → shipment records not created during the halt → shipment-creation SLA violated and downstream shipment-status updates lag → on resume, catch-up burst (records_consumed_rate 91.8/s, producer send 45/s) shows elevated p95 during recovery. Orders/payments succeeded because order/payment/inventory consumers absorbed the same surge at lag 0 — only shipping's consumer was impaired.

**Confidence:** High on localization and mechanism (shipping consumer halt → backlog → late shipments), backed by F1–F6 and the log time-distribution. Medium on the *precise internal trigger* of the halt.

**Contributing factor:** a synchronized ~70x Kafka event surge across order→payment→inventory→shipping (18:31–18:36) inflated the backlog magnitude, but is not the necessary cause (peers coped; 18/s ≪ 91.8/s capacity).

**Stop reason:** cause sufficient — reached a mechanism from a verified consumer halt to the symptom, and eliminated every alternative blocker (rebalance, DB, GC/restart, broker, deploy). Remaining ambiguity (below) is not resolvable with the available instrument.

**What would confirm the exact trigger (currently missing):** the reason the listener was suspended — e.g., a Spring Kafka container pause/`stop`, an error-handler backoff on a poison record, a `Thread.sleep`/lock in the shipment handler, or a deliberately injected consumer-pause fault. This needs the **INFO log line bodies** at 18:31 and 18:38 (container "pausing"/"resuming"/"Consumer stopped" messages) or the consumer/listener **span** internals — neither is exposed by the current tools.

---

## F. Tool / legibility friction (for redesign)

1. **INFO log bodies are invisible.** `get_logs` only surfaces WARN+ template text; for an all-INFO service it returns counts only. The entire remaining ambiguity (what suspended the listener) lives in INFO lines (`Consumer/partitions/commit` at 18:38, container pause/resume). I could time-localize the halt via log *counts* but never read the decisive lines. A filtered "return N sample raw lines (any level)" mode would have closed the case.
2. **Consumer/listener work is invisible to trace tools.** `get_trace_breakdown`/`get_slow_endpoints` only expose SERVER spans → for a Kafka-driven service they showed only /actuator/health. The actual shipment-creation spans (CONSUMER/INTERNAL) and their latency during the stall were unreachable. A "consumer-span breakdown" or per-message processing-time view is the missing instrument.
3. **`discover_signals` "newly appeared" is misleading for episodic gauges.** Rebalance/join gauges only emit during a rebalance, so they show as "new vs baseline" and dominated the top-anomaly list, seeding a wrong initial hypothesis (rebalance) that took several steps to kill. Flagging "gauge only present during events; not necessarily anomalous" would help.
4. **DB target not addressable.** Topology returns the downstream DB as the literal string `db:postgresql` with no UUID, so `get_db_sessions`/`get_slow_queries` (blocking-chain / lock inspection) could not be run against shipping's DB. I had to substitute the app-side `db.client.connections.*` pool metrics. A resolvable DB target_id in topology edges is needed.
5. **Baseline auto-window returns "no observation" a lot.** Many series reported "baseline empty → conservative normal," forcing manual `baseline_from/to`. When the baseline is genuinely sparse this hides real change (e.g., records_lag_max, rps). An explicit "measured-zero vs no-sample" flag per baseline would prevent mis-reading.
6. **No topic/consumer-group-level lineage.** I inferred the order→shipping event path and the surge cascade from producer/consumer *rates* across services; nothing maps producers→topics→consumer-groups. Establishing "who produces the topic shipping lags on" required cross-service rate correlation instead of a direct lookup. Kafka topic/consumer-group entities in topology would make the widening deterministic instead of inferential.
7. **Env-var strictness.** Every `$TOOL call` needs all three RCA_* vars exported in the same shell; a first call failed silently-ish. Minor, but the error ("전부 필요") could name which were missing.
