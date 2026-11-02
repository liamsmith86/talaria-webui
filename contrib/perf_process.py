"""Run the measured server separately from Playwright, pytest and the fake Hermes.

Linux /proc supplies current RSS, proportional/private memory, peak RSS and CPU.
Other platforms can still run browser scenarios, with server metrics omitted.
"""

import os
import socket
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import httpx


class ServerProcess:
    def __init__(self, pid, url):
        self.pid, self.url = pid, url

    def memory(self):
        root = Path(f"/proc/{self.pid}")
        if not root.exists():
            return {}
        fields = {}
        for name in ("status", "smaps_rollup"):
            try:
                lines = (root / name).read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                key, _, value = line.partition(":")
                if key in {"VmRSS", "VmHWM", "Pss", "Private_Clean", "Private_Dirty"}:
                    fields[key] = int(value.split()[0]) / 1024
        if "VmRSS" not in fields:
            return {}
        result = {
            "server_rss_mb": round(fields["VmRSS"], 2),
            "server_peak_rss_mb": round(fields["VmHWM"], 2),
        }
        if "Pss" in fields:
            result.update(
                server_pss_mb=round(fields["Pss"], 2),
                server_private_mb=round(fields["Private_Clean"] + fields["Private_Dirty"], 2),
            )
        return result

    def cpu(self):
        path = Path(f"/proc/{self.pid}/stat")
        # The process name in parentheses can contain spaces.
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
        except OSError:
            return None
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

    def cpu_since(self, before):
        after = self.cpu()
        if before is None or after is None:
            return {}
        return {"server_cpu_ms": round((after - before) * 1000)}


@contextmanager
def server_process(python, arguments):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    # Use the production CLI and interpreter, including its ordinary import cost.
    command = [str(python), "-c", "from talaria.cli import main; main()", *arguments]
    with subprocess.Popen([*command, "--port", str(port)]) as process:
        try:
            deadline = time.monotonic() + 30
            with httpx.Client(trust_env=False) as client:
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"Server exited with status {process.returncode}")
                    try:
                        if client.get(url + "/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Server did not become healthy within 30 seconds")
                    time.sleep(0.02)
            yield ServerProcess(process.pid, url)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
