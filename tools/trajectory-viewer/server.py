"""Local RCA trajectory explorer with optional cached Luna translation."""
import argparse
import hmac
import json
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

MAX_BYTES = 32 * 1024 * 1024
STATIC = Path(__file__).parent


def contained(root, path):
    root, path = root.resolve(), path.resolve()
    if not path.is_relative_to(root):
        raise ValueError("Path is outside outputs")
    return path


def read_json(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("File exceeds 32 MiB")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_trajectory(text):
    messages, errors = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("role") not in {"system", "user", "assistant", "tool"}:
                raise ValueError("Expected message object with system/user/assistant/tool role")
            if "tool_calls" in item and not isinstance(item["tool_calls"], list):
                raise ValueError("tool_calls must be a list")
            messages.append({**item, "_line": number})
        except (ValueError, TypeError) as exc:
            errors.append({"line": number, "error": str(exc)})
    return {"messages": messages, "errors": errors}


def scenario_metadata(case_name, cases_root):
    if cases_root is None or Path(case_name).name != case_name:
        return {}
    try:
        path = contained(cases_root, cases_root / case_name / "meta.json")
        meta = read_json(path)
        scenario = meta.get("scenario_metadata", {})
        if not isinstance(scenario, dict):
            return {}
        return {"case_title": scenario.get("title", ""),
                "case_description": scenario.get("description", ""),
                "scenario_id": meta.get("scenario_id", ""),
                "scenario_source": "capture meta.json (viewer only)"}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def catalog(root, cases_root=Path("/data/eval-cases")):
    base = contained(root, root / "mcp-eval")
    records = []
    if not base.exists():
        return records
    # Fixed depth follows case symlinks without recursive cycles.
    for experiment in sorted(base.iterdir(), reverse=True):
        try:
            contained(root, experiment)
            if not experiment.is_dir():
                continue
            for case in sorted(experiment.iterdir()):
                try:
                    contained(root, case)
                except ValueError:
                    continue
                if not case.is_dir():
                    continue
                for run in sorted(case.glob("run*")):
                    try:
                        contained(root, run)
                        trajectory = contained(root, run / "trajectory.jsonl")
                    except ValueError:
                        continue
                    if not trajectory.is_file():
                        continue
                    try:
                        score_path = contained(root, run / "score.json")
                        score = read_json(score_path) if score_path.exists() else {}
                        if not isinstance(score, dict):
                            score = {}
                    except (ValueError, OSError):
                        score = {}  # A live writer may not have finished this file.
                    judges = []
                    for judge in sorted(run.glob("mechanism_judge*.json")):
                        try:
                            judges.append({"file": judge.name, **read_json(contained(root, judge))})
                        except (ValueError, OSError, TypeError):
                            pass
                    records.append({"id": str((run / "trajectory.jsonl").relative_to(root)),
                                    "experiment": experiment.name, "case": case.name, "run": run.name,
                                    **scenario_metadata(case.name, cases_root),
                                    "score": score, "judges": judges,
                                    "modified": trajectory.stat().st_mtime})
        except (ValueError, OSError, TypeError):
            continue
    return sorted(records, key=lambda x: x["modified"], reverse=True)


def run_path(root, identifier):
    # Restrict the API to catalog-addressable trajectories, never arbitrary files.
    parts = Path(identifier).parts
    if len(parts) != 5 or parts[0] != "mcp-eval" or parts[-1] != "trajectory.jsonl" or not parts[3].startswith("run"):
        raise ValueError("Expected mcp-eval/experiment/case/run/trajectory.jsonl")
    return contained(root, root / identifier)


def load_requests(root, identifier):
    path = contained(root, run_path(root, identifier).parent / "observations/requests.jsonl")
    if not path.exists():
        return {"requests": [], "errors": []}
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Request snapshot exceeds 32 MiB")
    rows, errors = [], []
    for line, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            item = json.loads(text)
            if not isinstance(item, dict) or not isinstance(item.get("request"), dict):
                raise TypeError("Invalid request record")
            rows.append(item)
        except (ValueError, TypeError) as exc:
            errors.append({"line": line, "error": str(exc)})
    return {"requests": rows, "errors": errors}


def load_run(root, identifier):
    path = run_path(root, identifier)
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Trajectory exceeds 32 MiB")
    result = parse_trajectory(path.read_text(encoding="utf-8"))
    result["id"] = identifier
    for name in ("score.json", "result.json", "observation-runtime.json"):
        companion = contained(root, path.parent / name)
        if companion.exists():
            try:
                result[name] = read_json(companion)
            except ValueError:
                result[name] = {"error": "Invalid or oversized JSON"}
    return result


def handler_for(root, token=None, translator=None, cases_root=Path("/data/eval-cases")):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Query strings can carry the one-time session bootstrap token.
            pass

        def do_POST(self):
            if urlsplit(self.path).path != "/api/translate":
                self.send_error(404)
                return
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            session = cookie.get("rca_session")
            if token and (not session or not hmac.compare_digest(session.value, token)):
                self.send_error(401, "Session token required")
                return
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", "") and origin != "https://" + self.headers.get("Host", ""):
                self.send_error(403, "Same-origin request required")
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise ValueError("Expected application/json")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 150000:
                    raise ValueError("Translation request is too large or empty")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise TypeError("Expected JSON object")
                if translator is None:
                    raise RuntimeError("Translation is not configured")
                payload = translator.translate(data.get("text"), data.get("target"))
                status = 200
            except (ValueError, TypeError, UnicodeError) as exc:
                payload, status = {"error": str(exc)}, 400
            except RuntimeError as exc:
                payload, status = {"error": str(exc), "code": getattr(exc, "code", "unavailable")}, getattr(exc, "status", 503)
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                if token:
                    supplied = parse_qs(url.query).get("token", [""])[0]
                    cookie = SimpleCookie()
                    cookie.load(self.headers.get("Cookie", ""))
                    session = cookie.get("rca_session")
                    if supplied and hmac.compare_digest(supplied, token):
                        self.send_response(303)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", f"rca_session={token}; HttpOnly; SameSite=Strict; Path=/")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Referrer-Policy", "no-referrer")
                        self.end_headers()
                        return
                    if not session or not hmac.compare_digest(session.value, token):
                        self.send_error(401, "Session token required")
                        return
                if url.path == "/api/catalog":
                    body, mime = json.dumps({"runs": catalog(root, cases_root)}, ensure_ascii=False).encode(), "application/json"
                elif url.path == "/api/run":
                    identifier = parse_qs(url.query).get("id", [""])[0]
                    body, mime = json.dumps(load_run(root, identifier), ensure_ascii=False).encode(), "application/json"
                elif url.path == "/api/requests":
                    identifier = parse_qs(url.query).get("id", [""])[0]
                    body, mime = json.dumps(load_requests(root, identifier), ensure_ascii=False).encode(), "application/json"
                elif url.path in {"/", "/app.js", "/style.css"}:
                    file = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}[url.path]
                    body = (STATIC / file).read_bytes()
                    mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[file]
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, OSError, UnicodeError) as exc:
                self.send_error(400, str(exc))
    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", type=Path, default=STATIC.resolve().parents[1] / "outputs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18130)
    parser.add_argument("--token-file", type=Path, help="Optional private file containing a session token (at least 24 URL-safe characters)")
    parser.add_argument("--cases", type=Path, default=Path("/data/eval-cases"))
    args = parser.parse_args()
    token = args.token_file.read_text().strip() if args.token_file else None
    if token and (len(token) < 24 or not all(c.isalnum() or c in "-_" for c in token)):
        parser.error("Token must contain at least 24 URL-safe characters")
    print(f"RCA Replay: http://{args.host}:{args.port} (outputs={args.outputs.resolve()})", flush=True)
    from translation import Translator
    translator = Translator(args.outputs.resolve() / "replay-translations")
    ThreadingHTTPServer((args.host, args.port), handler_for(args.outputs.resolve(), token, translator, args.cases)).serve_forever()
