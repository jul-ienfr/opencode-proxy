import pystray
from .icon import running_icon, stopped_icon
from .window import DashboardWindow


class TrayApp:
    """System tray icon with menu to control the proxy and open the dashboard."""

    def __init__(self, server_manager, host, port, web_port):
        self.server_manager = server_manager
        self.host = host
        self.port = port
        self.dashboard = DashboardWindow(web_port)
        self._icon = None

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
        self.server_manager.start()
        self._update_icon()

    def _on_stop_proxy(self, icon, item):
        self.server_manager.stop()
        self._update_icon()

    def _on_open_dashboard(self, icon, item):
        self.dashboard.open()

    def _on_copy_api_url(self, icon, item):
        import subprocess
        url = self._api_url()
        try:
            subprocess.run(["clip"], input=url.encode(), check=True, capture_output=True)
        except Exception:
            pass

    def _on_quit(self, icon, item):
        self.dashboard.close()
        self.server_manager.stop()
        icon.stop()

    # ── Entry point ──

    def run(self):
        self._icon = pystray.Icon(
            "opencode",
            running_icon() if self.server_manager.is_running else stopped_icon(),
            f"OpenCode Proxy — {self._api_url()}" if self.server_manager.is_running else "OpenCode Proxy — Arrete",
            menu=self._build_menu(),
        )
        self._icon.run()