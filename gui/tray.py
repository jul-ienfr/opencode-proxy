import pystray

from .icon import running_icon, stopped_icon
from .window import DashboardWindow


def notify_geo(message: str, title: str = "OpenCode — repli géo"):
    """Module-level helper: try to notify via running tray, else no-op."""
    try:
        # Try to find running tray instance via global (if any)
        import gc as _gc

        for obj in _gc.get_objects():
            try:
                if isinstance(obj, TrayApp) and obj._icon is not None:
                    obj.notify_geo(message, title)
                    return
            except Exception:
                continue
    except Exception:
        pass


class TrayApp:
    """System tray icon with menu to control the proxy and open the dashboard."""

    def __init__(self, server_manager, host, port, web_port):
        self.server_manager = server_manager
        self.host = host
        self.port = port
        self.dashboard = DashboardWindow(port)
        self._icon = None
        self._geo_last_time = ""
        self._geo_poll_thread = None

    def _api_url(self):
        return f"http://{self.host}:{self.port}"

    def _build_menu(self):
        if self.server_manager.is_running:
            proxy_item = pystray.MenuItem("Arreter le proxy", self._on_stop_proxy)
        else:
            proxy_item = pystray.MenuItem("Demarrer le proxy", self._on_start_proxy)

        return pystray.Menu(
            proxy_item,
            pystray.MenuItem("Ouvrir le dashboard", self._on_open_dashboard),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Copier l'adresse API", self._on_copy_api_url),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quitter", self._on_quit),
        )

    def _update_icon(self):
        if self._icon is None:
            return
        self._icon.icon = running_icon() if self.server_manager.is_running else stopped_icon()
        self._icon.title = (
            f"OpenCode Proxy — {self._api_url()}"
            if self.server_manager.is_running
            else "OpenCode Proxy — Arrete"
        )
        self._icon.menu = self._build_menu()

    # ── Tray callbacks ──

    def _on_start_proxy(self, icon, item):
        # [plan v10 §14.3.28] start/stop sont LENTS (boot proxy complet) —
        # exécutés dans la boucle pystray ils gèlent icône + menu. Offload
        # thread daemon (même pattern que _on_quit), l'icône se rafraîchit
        # au retour via le poll d'état.
        import threading

        threading.Thread(target=self._start_bg, daemon=True).start()

    def _start_bg(self):
        try:
            self.server_manager.start()
        finally:
            self._update_icon()

    def _on_stop_proxy(self, icon, item):
        import threading

        threading.Thread(target=self._stop_bg, daemon=True).start()

    def _stop_bg(self):
        try:
            self.server_manager.stop()
        finally:
            self._update_icon()

    def _on_open_dashboard(self, icon, item):
        self.dashboard.open()

    def _on_copy_api_url(self, icon, item):
        import subprocess
        import sys

        url = self._api_url()
        try:
            kwargs = {}
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(["clip"], input=url.encode(), check=True, capture_output=True, **kwargs)
        except Exception:
            pass

    def _on_quit(self, icon, item):
        self.dashboard.close()
        # Stop servers in background; don't block the tray message loop.
        import threading

        threading.Thread(target=self.server_manager.stop, daemon=True).start()
        try:
            icon.stop()
        except Exception:
            pass

    def notify_geo(self, message: str, title: str = "OpenCode — repli géo"):
        """Show Windows tray balloon (pystray) — throttling is backend-side."""
        try:
            if self._icon is not None and hasattr(self._icon, "notify"):
                # pystray: notify(msg, title)
                try:
                    self._icon.notify(message, title)
                except TypeError:
                    self._icon.notify(message)
        except Exception:
            pass

    def _geo_poll_loop(self):
        """Poll logs/geo_notifications.json every 10s and notify on new entry."""
        import json as _js
        import os as _os
        import time as _tm

        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        p = _os.path.join(root, "logs", "geo_notifications.json")
        while True:
            try:
                if _os.path.exists(p):
                    with open(p, encoding="utf-8") as _f:
                        data = _js.load(_f) or []
                    if data:
                        last = data[-1]
                        t = last.get("time", "")
                        if t and t != self._geo_last_time:
                            self._geo_last_time = t
                            msg = last.get("message", "") or "Repli géo direct→VPN"
                            self.notify_geo(msg)
            except Exception:
                pass
            _tm.sleep(10)

    # ── Entry point ──

    def run(self):
        # start geo poll thread (daemon)
        try:
            import threading as _th

            if self._geo_poll_thread is None or not self._geo_poll_thread.is_alive():
                self._geo_poll_thread = _th.Thread(target=self._geo_poll_loop, daemon=True)
                self._geo_poll_thread.start()
        except Exception:
            pass
        self._icon = pystray.Icon(
            "opencode",
            running_icon() if self.server_manager.is_running else stopped_icon(),
            f"OpenCode Proxy — {self._api_url()}"
            if self.server_manager.is_running
            else "OpenCode Proxy — Arrete",
            menu=self._build_menu(),
        )
        self._icon.run()
