import os
import subprocess
import sys
import threading
import webbrowser


class DashboardWindow:
    """Manages a pywebview window launched as a subprocess to avoid threading issues."""

    def __init__(self, port, host="127.0.0.1"):
        # [P6] le dashboard est servi par le port PRINCIPAL du proxy —
        # l'ancien paramètre web_port pointait vers :8082 où rien n'écoute.
        self.port = port
        self.host = host
        self._process = None
        self._script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_webview_main.py")

    def _log_stderr(self, proc):
        """Read stderr from the subprocess and forward it to the parent's stderr."""
        try:
            for line in iter(proc.stderr.readline, b""):
                if line.strip():
                    print(
                        f"[dashboard] {line.decode('utf-8', errors='replace').strip()}",
                        file=sys.stderr,
                    )
        except ValueError:
            pass  # pipe closed

    def open(self):
        if self._process is not None and self._process.poll() is None:
            return

        url = f"http://{self.host}:{self.port}"

        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(
                [sys.executable, self._script, url],
                stderr=subprocess.PIPE,
                **kwargs,
            )
            threading.Thread(target=self._log_stderr, args=(self._process,), daemon=True).start()
        except Exception:
            webbrowser.open(url)

    def close(self):
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
