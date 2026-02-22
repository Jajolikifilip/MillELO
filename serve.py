from gevent import monkey
monkey.patch_all()

print("Starting server...", flush=True)
import main
from main import socketio, app

print("Serving on 0.0.0.0:5000 with gevent-websocket", flush=True)
socketio.run(app, host='0.0.0.0', port=5000, debug=True, use_reloader=True, log_output=True)
