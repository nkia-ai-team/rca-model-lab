# RCA Replay

Dependency-free read-only viewer for RCA evaluations. Run from the repository:

```sh
python3 tools/trajectory-viewer/server.py --port 18130
```

Open http://127.0.0.1:18130. The catalog scans `outputs/mcp-eval/<experiment>/<case>/run*/trajectory.jsonl`, including internal outputs symlinks. Refresh discovers newly completed runs. Missing/partially written score files remain unscored.

The Korean interface provides experiment/model/case search, chat bubbles with collapsed thinking, readable tool arguments/results, and final RCA answers, complete raw messages, turn-by-turn previous/next/autoplay, speed/seek, restart and full-conversation controls, content filters, two-run comparison, local JSONL upload, and shareable current-run/event links. Comparison uses event ordinal, **not semantic alignment**. Uploads remain in browser memory. Original texts and tool JSON are never replaced by translation.

Root exact match (`root_f1 == 1`), deterministic rule score (`strict_correct`, visible in raw scores), and separate LLM mechanism judgments are different. The mechanism card reports the independent judge score; it does not invent an unscored result or equate deterministic strict with mechanism success. GPT-6.1-sol cache is preferred when present. Harness policy is visible for fair comparisons.

The trajectory records the conversation; it is **not necessarily the compressed input sent to the model**. Expand actual-input snapshots to load `observations/requests.jsonl` and inspect the exact recorded request per turn. Missing snapshots are reported, never reconstructed. Each API file is limited to 32 MiB. Tool output originally truncated by evaluation cannot be recovered by this viewer.

## Translation

Message translation uses authenticated server-side Codex CLI with `gpt-6-luna`, so it also works on private LAN HTTP. Requests use the existing Codex account allowance; Luna is not universally free. No new API key is exposed to the browser. Korean and English targets are supported, original text is preserved, and identical translations are cached under `outputs/replay-translations`. Requests are limited to 24,000 characters, 120 seconds and two concurrent translations. Model calls disable tools and run outside the repository. Errors remain visible and can be retried.

## Private LAN access

Localhost is the default. For an explicitly intended private-LAN deployment use `--host 0.0.0.0 --token-file /private/path/token`. The token file must contain at least 24 URL-safe characters and should have mode 0600. Visit `http://server:18130/?token=TOKEN` once to set an HttpOnly SameSite cookie; the server redirects to a token-free URL and protects every route. Requests are not logged. Share the bootstrap token privately. Plain LAN HTTP is not encrypted. Translation runs on the server and does not depend on the browser Translator API. This is a local development viewer, not a public multi-user service.

Paths are constrained to outputs and specific companion files; external symlinks and traversal are rejected. The translation POST API writes only translation cache; evaluation artifacts remain read-only. Do not expose it publicly without deployment-grade authentication/TLS.

## Verification

```sh
python3 -m unittest discover -s tools/trajectory-viewer -p 'test_*.py' -v
node --check tools/trajectory-viewer/app.js
```

## Conversation display

Runs open as full conversations. User alarm seeds show the affected services, time window and alarm count; long system instructions and reasoning are collapsed. Tool outputs show recorded status, identity and collection counts with progressively expandable human-readable fields. Final `submit_rca` calls render their summary and root causes as an answer. Raw JSON is available only under technical details; display summaries are deterministic, not LLM-generated interpretations. Playback advances through complete assistant turns and their tool responses. Existing deep links still use the recorded event index.

## Readability update

Scenario titles and descriptions come from `/data/eval-cases/<case>/meta.json` (`--cases` overrides this source) and are viewer-only metadata. IDs remain in collapsed details. Tools, tool results, system prompts and raw JSON are hidden by default; enable “도구 · 기술 정보 보기” to inspect them. Final `submit_rca` calls remain visible as human-readable RCA answers.
