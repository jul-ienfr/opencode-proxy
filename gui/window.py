import os
import subprocess
import sys
import webbrowser


class DashboardWindow:
    """Manages a pywebview window launched as a subprocess to avoid threading issues."""

    def __init__(self, web_port, host="localhost"):
        self.web_port = web_port
        self.host = host
        self._process = None
        self._script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_webview_main.py")

    def open(self):
        if self._process is not None and self._process.poll() is None:
            return

        url = f"http://{self.host}:{self.web_port}"

        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self._process = subprocess.Popen(
                [sys.executable, self._script, url],
                **kwargs,
            )
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