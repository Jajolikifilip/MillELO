import threading
import time
import os
import sys

print(f"[startup] Beginning at {time.time():.1f}", flush=True)

from flask import Flask
from flask_socketio import SocketIO

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or 'milLELO-secret-key-2024'

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}

@app.route('/')
def health():
    return '<html><head><meta http-equiv="refresh" content="2"></head><body><p>Loading MillELO...</p></body></html>', 200

@app.route('/health')
def health_check():
    return 'OK', 200

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

print(f"[startup] Minimal app created at {time.time():.1f}", flush=True)

def load_main_app():
    try:
        t = time.time()
        print("[startup] Loading main application...", flush=True)
        import main as main_module
        app.wsgi_app = main_module.app.wsgi_app
        print(f"[startup] Main app loaded and swapped in {time.time()-t:.1f}s", flush=True)
    except Exception as e:
        print(f"[startup] Error loading main app: {e}", flush=True)
        import traceback
        traceback.print_exc()

threading.Thread(target=load_main_app, daemon=True).start()

if __name__ == '__main__':
    print(f"[startup] Starting server on port 5000 at {time.time():.1f}", flush=True)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
