from flask import Flask, redirect
from threading import Thread
import os
from routes.admin import admin_bp
from routes.api import api_bp, set_announcement_handler
from routes.settings import settings_bp

app = Flask(__name__)

# Register Blueprints
app.register_blueprint(admin_bp)
app.register_blueprint(api_bp)
app.register_blueprint(settings_bp)

@app.route('/')
def home():
    return redirect('/admin/data')

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, threaded=True)

def keep_alive(handler_callback=None):
    if handler_callback:
        set_announcement_handler(handler_callback)
    t = Thread(target=run, daemon=True)
    t.start()
