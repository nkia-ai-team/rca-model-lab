import importlib.util
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

spec = importlib.util.spec_from_file_location("viewer_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "outputs"
        self.run = self.root / "mcp-eval/experiment/case-a/run1"
        self.run.mkdir(parents=True)
        self.id = "mcp-eval/experiment/case-a/run1/trajectory.jsonl"
        (self.run / "trajectory.jsonl").write_text('{"role":"assistant","reasoning_content":"reason","tool_calls":[]}\n')

    def tearDown(self):
        self.temp.cleanup()

    def test_parser_preserves_valid_lines_and_reports_partial_writer(self):
        parsed = server.parse_trajectory('{"role":"user","content":"hello"}\n\n{bad\n[]\n{"role":"tool","content":"ok"}')
        self.assertEqual([m["_line"] for m in parsed["messages"]], [1, 5])
        self.assertEqual([e["line"] for e in parsed["errors"]], [3, 4])

    def test_catalog_follows_internal_symlink_and_keeps_pending_scores(self):
        target = self.root / "shards/case-b"
        target.mkdir(parents=True)
        (target / "run2").mkdir()
        (target / "run2/trajectory.jsonl").write_text('{"role":"user","content":"test"}\n')
        (self.run.parent.parent / "case-b").symlink_to(target, target_is_directory=True)
        (self.run / "score.json").write_text('{')
        rows = server.catalog(self.root)
        self.assertEqual(len(rows), 2)
        linked = next(row for row in rows if row["case"] == "case-b")
        self.assertEqual(server.load_run(self.root, linked["id"])["messages"][0]["role"], "user")

    def test_root_and_mechanism_scores_remain_separate(self):
        (self.run / "score.json").write_text(json.dumps({"root_f1": 1, "strict_correct": True, "observation_policy": "structured-v1"}))
        (self.run / "mechanism_judge.codex.gpt-6.1-sol.json").write_text(json.dumps({"score": 0, "model": "gpt-6.1-sol"}))
        row = server.catalog(self.root)[0]
        self.assertEqual(row["score"]["root_f1"], 1)
        self.assertEqual(row["judges"][0]["score"], 0)

    def test_path_traversal_and_external_symlink_rejected(self):
        for identifier in ("/etc/passwd", "mcp-eval/../../x/run1/trajectory.jsonl", "mcp-eval/experiment/case-a/run1/score.json"):
            with self.assertRaises(ValueError):
                server.load_run(self.root, identifier)
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "trajectory.jsonl").write_text('{"role":"user"}')
        (self.run.parent / "run2").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            server.load_run(self.root, self.id.replace("run1", "run2"))

    def test_actual_request_snapshots_are_separate_and_bounded(self):
        self.assertEqual(server.load_requests(self.root, self.id)["requests"], [])
        obs = self.run / "observations"
        obs.mkdir()
        (obs / "requests.jsonl").write_text('{"turn":7,"request":{"messages":[{"role":"tool","content":"compressed"}]}}\n{')
        result = server.load_requests(self.root, self.id)
        self.assertEqual(result["requests"][0]["turn"], 7)
        self.assertEqual(result["errors"][0]["line"], 2)

    def test_scenario_descriptions_use_capture_metadata_and_reject_external_paths(self):
        cases = Path(self.temp.name) / "cases"
        (cases / "case-a").mkdir(parents=True)
        (cases / "case-a/meta.json").write_text(json.dumps({
            "scenario_id": "F09-H", "scenario_metadata": {
                "title": "Order JVM heap shrink", "description": "GC pauses delay checkout"}}))
        row = server.catalog(self.root, cases)[0]
        self.assertEqual(row["case_title"], "Order JVM heap shrink")
        self.assertEqual(row["case_description"], "GC pauses delay checkout")
        self.assertEqual(server.scenario_metadata("../outside", cases), {})
        self.assertEqual(server.scenario_metadata("missing", cases), {})

    def test_translation_requires_auth_json_and_same_origin(self):
        class FakeTranslator:
            def translate(self, text, target):
                if target not in {"en", "ko"}:
                    raise ValueError("Invalid language")
                return {"translation": "번역", "model": "gpt-6-luna", "cached": False}
        token = "t" * 32
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_for(self.root, token, FakeTranslator()))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = HTTPConnection(*httpd.server_address)
            body = json.dumps({"text": "hello", "target": "ko"})
            headers = {"Content-Type": "application/json"}
            conn.request("POST", "/api/translate", body, headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            headers["Cookie"] = "rca_session=" + token
            headers["Origin"] = "https://untrusted.example"
            conn.request("POST", "/api/translate", body, headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            del headers["Origin"]
            conn.request("POST", "/api/translate", body, headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["translation"], "번역")
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_http_token_guards_api_and_cookie_bootstrap(self):
        token = "t" * 32
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler_for(self.root, token))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            conn = HTTPConnection(*httpd.server_address)
            conn.request("GET", "/api/catalog")
            response = conn.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            conn.request("GET", "/?token=" + token)
            response = conn.getresponse()
            self.assertEqual(response.status, 303)
            cookie = response.getheader("Set-Cookie").split(";")[0]
            response.read()
            conn.request("GET", "/api/catalog", headers={"Cookie": cookie})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("'self'", response.getheader("Content-Security-Policy"))
            self.assertEqual(len(json.loads(response.read())["runs"]), 1)
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
