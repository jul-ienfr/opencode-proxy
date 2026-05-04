"""Standalone script that opens a pywebview window. Launched as a subprocess."""
import sys

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8082"
    try:
        import webview
        webview.create_window(
            "OpenCode Dashboard", url,
            width=1200, height=800, min_size=(800, 600),
        )
        webview.start()
    except Exception:
        import webbrowser
        webbrowser.open(url)