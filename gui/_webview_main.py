"""Standalone script that opens a pywebview window. Launched as a subprocess."""
import os
import sys

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8082"
    debug = os.getenv("PYWEBVIEW_DEBUG", "").lower() in ("1", "true")

    # Try backends in order: edgechromium (WebView2) → mshtml (IE) → browser
    backends = ["edgechromium", "mshtml"]

    for gui_backend in backends:
        try:
            import webview

            webview.create_window(
                "OpenCode Dashboard", url,
                width=1200, height=800, min_size=(800, 600),
                text_select=True,
                background_color='#1a1a2e',
            )
            webview.start(gui=gui_backend, debug=debug)
            break  # success, window was shown and closed normally
        except Exception as e:
            print(f"[dashboard] pywebview backend '{gui_backend}' failed: {e}", file=sys.stderr)
            continue
    else:
        # All pywebview backends failed — open in system browser
        import webbrowser

        webbrowser.open(url)
