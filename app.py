from flask import Flask
from flask_compress import Compress
from datetime import timedelta
import os
import sys

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or 'milLELO-secret-key-2024'

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL") or "sqlite:///milLELO.db"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}
app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'text/javascript', 'application/javascript', 'application/json']
app.config['COMPRESS_MIN_SIZE'] = 500
Compress(app)

@app.route('/health')
def health():
    return 'OK', 200

main_file = getattr(sys.modules.get('__main__'), '__file__', '')
if not main_file or not main_file.endswith('main.py'):
    import main
