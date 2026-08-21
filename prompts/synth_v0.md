You are generating one supervised fine-tuning example for a root-cause-analysis (RCA) model.

You know the GROUND TRUTH of a fault-injection scenario run on a Kubernetes microservice testbed.
Produce (1) a realistic incident report as an on-call engineer would see it — WITHOUT revealing the
root cause — and (2) the ideal RCA answer an expert would write.

## Scenario (ground truth — do not leak it verbatim into the situation)
- domain: {domain}
- title: {title}
- description: {description}
- root cause: {root_cause}
- propagation: {propagation}
- expected alarms:
{expected_alarms}
- grading rubric for a correct RCA:
{expected_rca_root_cause}

## Injection script (for realism about which components/metrics are involved)
```bash
{script}
```

## Variation seed: {variant}
Vary the time window, alarm ordering, numeric values and phrasing across seeds.

## Output
Return ONLY a JSON object with two string fields:
- "situation": the incident as observed — alarm list with timestamps, key metric deltas, a few
  representative log lines / trace observations, service topology hints. Korean or English is fine
  but be consistent. No root cause stated.
- "analysis": the expert RCA — root cause, propagation chain, decisive signals, and a one-line
  remediation. Must satisfy the grading rubric above.
