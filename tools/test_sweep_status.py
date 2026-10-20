#!/usr/bin/env python3
"""Tests for the sweep's failure path (tools/sweep.py, tools/validate.py).
Stdlib only.  python3 tools/test_sweep_status.py"""
import json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEP = os.path.join(HERE, "sweep.py")
VALIDATE = os.path.join(HERE, "validate.py")
TARGET = "qwen3.8-flash-next-offload"
URL = ""


class Stub(BaseHTTPRequestHandler):
    """Just enough of an SGLang server for tools/sweep.py: /server_info, /tokenize, /generate."""
    protocol_version = "HTTP/1.1"
    fail_after = 0      # answer the /generate calls after this one with a 500
    one_token = False   # answer every completion with a single token
    lock = threading.Lock()
    calls = 0

    def log_message(self, *a):
        pass

    @classmethod
    def reset(cls, fail_after=0, one_token=False):
        cls.fail_after = fail_after
        cls.one_token = one_token
        cls.calls = 0

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._json({"context_length": 32768, "server_args": {"context_length": 32768}})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/tokenize":
            self._json({"tokens": [1000 + i for i in range(16)]})
            return
        with Stub.lock:
            Stub.calls += 1
            k = Stub.calls
        if Stub.fail_after and k > Stub.fail_after:
            self._json({"error": "boom"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            time.sleep(0.02)                        # time to first token
            for i in range(1 if Stub.one_token else 4):
                body = {"text": "tok%d" % i, "meta_info": {"completion_tokens": i + 1,
                                                           "finish_reason": {"type": "stop" if i == 3 else None}}}
                chunk = ("data: " + json.dumps(body) + "\n\n").encode()
                self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
                self.wfile.flush()
                time.sleep(0.005)
            chunk = b"data: [DONE]\n\n"
            self.wfile.write(("%x\r\n" % len(chunk)).encode() + chunk + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            pass                                   # the client stopped reading (warm-up)
        self.close_connection = True


def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def last_line(txt):
    lines = [l for l in txt.strip().splitlines() if l.strip()]
    return lines[-1] if lines else "(no output)"


def sweep(tmp, name="sweep.json"):
    best = os.path.join(tmp, "best.json")
    json.dump({}, open(best, "w"))                # no baseline: the sweep must not exit early
    out = os.path.join(tmp, name)
    cmd = [sys.executable, SWEEP, "--url", URL, "--card", "rtx-3090-24gb", "--config", "test",
           "--prefill", "1024", "--conc", "1", "2", "--reps", "1", "--dec-reps", "1",
           "--best", best, "--out", out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r, out


def submission(**speed):
    s = {"schema": "local-ai-recipe-kit/submission/v1", "submitted": "2026-10-20", "handle": "tester",
         "target": TARGET,
         "launch": {"image": "ghcr.io/0xsero/sglang-exl3-flashnext@sha256:" + "a" * 64, "args": [], "env": {}},
         "probe": {"host": {"cpu": "EPYC 7443P"}, "gpus": []},
         "quality": {"panel": {"top1_agreement": 0.9913, "mean_kl_top20": 0.00092, "positions": 3001}},
         "speed": {"status": "DONE", "card": "rtx-3090-24gb",
                   "prefill": {"8192": {"median": 2627, "n": 3}, "16384": {"median": 2785, "n": 3},
                               "32768": {"median": 2846, "n": 3}},
                   "decode": {"C1": {"aggregate": {"median": 62.7}}}},
         "needs_measured": {"samples": 120, "mem_available_start_gb": 82.95, "mem_available_min_gb": 28.49,
                            "host_ram_drop_peak_gb": 54.46, "swap_used_max_gb": 0.0, "gpu_used_peak_mib": 63932.0},
         "notes": ""}
    s["speed"].update(speed)
    return s


def validate(tmp, s):
    d = os.path.join(tmp, "rtx-3090-24gb")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "2026-10-20-tester.json")
    json.dump(s, open(p, "w"))
    r = subprocess.run([sys.executable, VALIDATE, p], capture_output=True, text=True)
    return r


def test_a_failed_request_ends_the_run_with_a_status():
    with tempfile.TemporaryDirectory() as tmp:
        Stub.reset(fail_after=3)                  # the 4th request (one of the two C2 ones) fails
        r, out = sweep(tmp)
        assert r.returncode != 0, "the sweep exited 0 although a request failed"
        assert "SWEEP FAILED" in r.stderr, "the sweep did not say what failed: " + last_line(r.stderr)
        assert "Traceback" not in r.stderr, "the sweep crashed with a traceback"
        d = json.load(open(out))
        assert d["status"] == "FAILED", "status is " + repr(d.get("status"))
        assert "500" in d["error"], "the error does not name the failure: " + repr(d.get("error"))
        assert d["prefill"]["1024"]["median"] > 0, "the measurement taken before the failure is gone"


def test_a_one_token_completion_is_reported_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        Stub.reset(one_token=True)
        r, out = sweep(tmp)
        assert r.returncode != 0, "the sweep exited 0 although nothing could be measured"
        assert "SWEEP FAILED" in r.stderr, "the sweep did not say what failed: " + last_line(r.stderr)
        assert "Traceback" not in r.stderr, "the sweep crashed with a traceback"
        d = json.load(open(out))
        assert d["status"] == "FAILED", "status is " + repr(d.get("status"))
        assert d["error"], "no error recorded"
        assert d["prefill"]["1024"]["median"] > 0, "the measurement taken before the failure is gone"


def test_a_good_run_is_not_touched():
    with tempfile.TemporaryDirectory() as tmp:
        Stub.reset()
        r, out = sweep(tmp)
        assert r.returncode == 0, r.stdout + r.stderr
        d = json.load(open(out))
        assert d["status"] == "DONE", d
        assert d["decode"]["C1"]["aggregate"]["median"] > 0, d


def test_the_validator_needs_a_measured_prefill():
    with tempfile.TemporaryDirectory() as tmp:
        skipped = {"8192": {"skipped": "> context 8192"}, "16384": {"skipped": "> context 8192"},
                   "32768": {"skipped": "> context 8192"}}
        r = validate(tmp, submission(prefill=skipped))
        assert r.returncode == 1, "the validator accepted a prefill without a measurement"
        assert "no measurement" in r.stdout, r.stdout
        r = validate(tmp, submission())
        assert r.returncode == 0, r.stdout + r.stderr


def test_the_validator_refuses_a_failed_sweep():
    with tempfile.TemporaryDirectory() as tmp:
        r = validate(tmp, submission(status="FAILED", error="decode C2: HTTPError: HTTP Error 500"))
        assert r.returncode == 1, "the validator accepted a failed sweep"
        assert "the sweep failed" in r.stdout, "the validator did not report the failure: " + last_line(r.stdout)


TESTS = [test_a_failed_request_ends_the_run_with_a_status,
         test_a_one_token_completion_is_reported_not_a_crash,
         test_a_good_run_is_not_touched,
         test_the_validator_needs_a_measured_prefill,
         test_the_validator_refuses_a_failed_sweep]

if __name__ == "__main__":
    srv, URL = serve()
    bad = 0
    for t in TESTS:
        try:
            t()
            print("ok   " + t.__name__)
        except Exception as e:
            bad += 1
            print("FAIL " + t.__name__ + ": " + str(e))
    print(f"{len(TESTS) - bad}/{len(TESTS)} passed")
    srv.shutdown()
    sys.exit(1 if bad else 0)
