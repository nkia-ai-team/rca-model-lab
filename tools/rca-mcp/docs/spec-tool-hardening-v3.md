# RCA tool hardening v3

Scope: repair the confirmed v2 evidence-contract defects before comparing RCA
quality again. Preserve all prior runs; do not equate table access with faithful
data access. No new dependency or service deployment is required.

Acceptance criteria:

- process keys retain UInt64 precision, creation times are explicit UTC/epoch,
  metadata does not come from after the observed process snapshot; peaks are
  identified as interval maxima rather than simultaneous latest state.
- host filters are applied or rejected explicitly; page continuation and count
  lower bounds replace fabricated exact totals; different records have different refs.
- Kubernetes absent metrics are distinct from observed zeros; pod namespace is
  retained; collector snapshots do not certify historical absence of a fault.
- topic/group observation identities survive graph assembly without duplicated
  counts from repeated traversal.
- summary rows have distinguishing refs and pagination; exemplar trace IDs can
  be followed through a bounded raw-span read tool.
- a trace activity view exposes per-bucket observations, including baseline-only
  operations and empty buckets, without claiming broker backlog or loss solely
  from producer/consumer count differences.
- catalog lists actual registered tools and supported filter axes.

Validation: focused hermetic tests first, full Go tests/vet, then a fresh isolated
capture restore and representative tool/Claude checks. Only after these pass,
run a new 45-case directory. Reports must distinguish implementation validation
from measured RCA accuracy improvement. Existing blind filtering stays active.

## Implemented and verified (2026-09-11)

- Process snapshots cast UInt64 keys/epoch times explicitly, expose creation UTC,
  join metadata as-of the latest snapshot, and offer key/PID filters and offset pages.
- Host search applies connection text/port/direction filters, supports RFC syslog
  severity_max, rejects incompatible inputs, and uses complete-record hashes for refs.
- Summary refs include full record content (signature/hash/window/computed_at),
  offer signature/offset selection, and disclose that counts cover original windows.
- get_trace_spans follows exemplar IDs to full span attributes/events/links; exact
  nanosecond duration is encoded as a decimal string.
- get_trace_activity returns current/baseline bucket arrays over their group union,
  including baseline-only operations. Zero recorded spans do not prove no processing;
  producer/consumer totals do not certify delivery/backlog. Backend caps are explicit.
- Messaging edges preserve topic/group/span-kind and source aggregation dimensions;
  repeated traversal no longer multiplies already aggregated counts.
- K8s absent signals have no observed-zero scopes; partial signal coverage is machine
  marked degraded; positives survive missing running observations. Pod namespace remains.
- Collector metadata no longer grants blanket historical absence/exclusion claims.
- Catalog registration tests require actual process and trace drilldown paths.

Focused regression and full `go test ./... -count=1` plus `go vet ./...` passed.
Two isolated restores were checked against source Parquet via
`scripts/verify_tool_hardening.py`; 10 checks passed for each F04-R and F05-P.
Artifacts: `runs/tool-hardening-v3/{f04r,f05p}/checks/verification.json` and raw
tool responses. No original capture or v1/v2 evaluation result was edited.

F04-R consumer counts in the incident window were `[5,0,0,0,0,0,0,0,0,207,491]`:
the new view makes the stop/recovery sequence visible despite the total of 703.
This is a tool evidence check, not a claim that the model has correctly used it.

## RCA evaluation now running

`scripts/start_hardening_v3_evaluation.py` runs F04-R and F05-P first against the
verified backends. Each pilot must complete successfully, yield parseable RCA and
nonempty evidence, and clean up its dedicated DB before proceeding. Failure stops
the gate. The remaining 43 pending captures then use dynamic per-case ports via
resume_claude_batch.py. This gate checks execution/data contracts, not correctness
against the hidden answer. Full scoring is performed separately after completion.

New output: `runs/claude-opus5-hardening-v3-20260911`.
Launch log: `runs/tool-hardening-v3/evaluation.log`.
The manifest records cases and source file hashes; previous versions remain for
comparison. The v3 accuracy change has not yet been measured.
