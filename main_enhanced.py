"""Local web entrypoint (the same UI and Flask app as the desktop launcher)."""
import os

from backend.app_enhanced import app


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("KLINE_PORT", "8766")), debug=False)
