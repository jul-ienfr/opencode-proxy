"""Standalone script that opens a pywebview window. Launched as a subprocess."""

import json
import os
import sys

STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "window_state.json"
)


def _load_state():
    """Load saved window state from disk."""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_state(window):
    """Save current window bounds to disk."""
    try:
        state = {
            "width": window.width,
            "height": window.height,
            "x": window.x,
            "y": window.y,
        }
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass  # non-critical, ignore errors


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4000"
    debug = os.getenv("PYWEBVIEW_DEBUG", "").lower() in ("1", "true")

    # Load saved window state
    saved = _load_state()
    win_width = (saved or {}).get("width", 1200)
    win_height = (saved or {}).get("height", 800)
    win_x = (saved or {}).get("x")
    win_y = (saved or {}).get("y")

    # Try backends in order: edgechromium (WebView2) → mshtml (IE) → browser
    backends = ["edgechromium", "mshtml"]

    for gui_backend in backends:
        try:
            import webview

            kwargs = dict(
                width=win_width,
                height=win_height,
                min_size=(800, 600),
                text_select=True,
                background_color="#1a1a2e",
            )
            if win_x is not None and win_y is not None:
                kwargs["x"] = win_x
                kwargs["y"] = win_y

            window = webview.create_window(
                "OpenCode Dashboard",
                url,
                **kwargs,
            )

            window.events.closed += lambda: _save_state(window)

            webview.start(gui=gui_backend, debug=debug)
            break  # success, window was shown and closed normally
        except Exception as e:
            print(f"[dashboard] pywebview backend '{gui_backend}' failed: {e}", file=sys.stderr)
            continue
    else:
        # All pywebview backends failed — open in system browser
        import webbrowser

        webbrowser.open(url)
