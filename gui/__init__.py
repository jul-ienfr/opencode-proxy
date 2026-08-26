from .tray import TrayApp


def run_gui(server_manager, host, port):
    """Launch the GUI (system tray + native window). Replaces terminal loop."""
    app = TrayApp(server_manager, host, port)
    app.run()
