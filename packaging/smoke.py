"""Check a built roomfix.exe: its demo starts without a console window, serves the page,
the API, the logo, and runs a measurement against the simulated speakers.

    python packaging/smoke.py dist/roomfix/roomfix.exe
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = 8199


def call(path, body=None, timeout=10):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Roomfix": "1"})
    with urllib.request.urlopen(req, timeout=timeout) as reply:
        return reply.status, reply.headers.get("Content-Type", ""), reply.read()


def main(exe):
    home = tempfile.mkdtemp(prefix="roomfix-smoke-")
    proc = subprocess.Popen([exe, "--demo", "--no-browser", "--port", str(PORT)],
                            env=dict(os.environ, ROOMFIX_HOME=home))
    try:
        end = time.monotonic() + 90  # the first start unpacks nothing (one folder), but is slow on CI
        while True:
            try:
                code, _, body = call("/api/state")
                break
            except OSError:
                if proc.poll() is not None or time.monotonic() > end:
                    log = os.path.join(home, "page.log")
                    sys.exit("roomfix.exe did not start:\n" + (open(log).read() if os.path.exists(log) else ""))
                time.sleep(0.5)
        assert code == 200 and json.loads(body)["demo"] is True
        code, kind, page = call("/")
        assert code == 200 and b'href="logo.png"' in page, "page"
        code, kind, png = call("/logo.png")
        assert code == 200 and png[:4] == b"\x89PNG", "logo"
        call("/api/run", {"action": "measure", "auto_apply": False})
        end = time.monotonic() + 300
        while True:
            job = json.loads(call("/api/state")[2])["job"]
            if not job["running"]:
                break
            if job.get("prompt"):  # the demo's 2-way speakers: "did every sound come from the right driver?"
                call("/api/answer", {"ok": True})
            assert time.monotonic() < end, "measurement takes too long"
            time.sleep(1)
        assert job["error"] is None and job["result"], job
        print("roomfix.exe works:", job["result"])
    finally:
        proc.terminate()
        proc.wait(10)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join("dist", "roomfix", "roomfix.exe"))
