# # ================= Mini Greenhouse – Flask + MQTT + Socket.IO =================
# # Production-ready for Render (Gunicorn -k eventlet). Uses SQLite for logs.
# # ==============================================================================

# # --- eventlet must be patched before other network libs (VERY IMPORTANT) ---
# import eventlet
# eventlet.monkey_patch()

# from flask import Flask, render_template, request, jsonify, send_file
# from flask_mqtt import Mqtt
# from flask_socketio import SocketIO
# import os, json, uuid, time, sqlite3
# from datetime import datetime, timezone

# # ------------------------------------------------------------------------------
# # App & Realtime setup
# # ------------------------------------------------------------------------------
# app = Flask(__name__)

# # Socket.IO using eventlet (works with gunicorn -k eventlet)
# socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

# # ---- MQTT config (can be overridden by ENV on Render) ----
# app.config['MQTT_BROKER_URL'] = os.environ.get('MQTT_BROKER_URL', 'broker.hivemq.com')
# app.config['MQTT_BROKER_PORT'] = int(os.environ.get('MQTT_BROKER_PORT', '1883'))
# app.config['MQTT_KEEPALIVE']   = int(os.environ.get('MQTT_KEEPALIVE', '60'))
# app.config['MQTT_CLIENT_ID']   = os.environ.get('MQTT_CLIENT_ID', f'flask-{uuid.uuid4().hex[:8]}')
# app.config['MQTT_TLS_ENABLED'] = False
# app.config['MQTT_RECONNECT_DELAY'] = 5   # thử reconnect mỗi 5s


# mqtt = Mqtt(app)

# TOPIC_DATA = os.environ.get('MQTT_TOPIC_DATA', 'greenhouse/data')
# TOPIC_CMD  = os.environ.get('MQTT_TOPIC_CMD',  'greenhouse/cmd')

# # ------------------------------------------------------------------------------
# # App state for UI
# # ------------------------------------------------------------------------------
# mode = "auto"
# fan_state = 0
# threshold_temp = 30.0
# schedule_cfg = {"on_min": 5, "off_min": 10}
# last_data = None

# # ------------------------------------------------------------------------------
# # SQLite helpers (supports persistent disk via ENV)
# # ------------------------------------------------------------------------------
# DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "greenhouse.db"))

# def ensure_db_dir():
#     try:
#         os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
#     except Exception:
#         pass

# def db_conn():
#     return sqlite3.connect(DB_PATH, check_same_thread=False)

# def init_db():
#     ensure_db_dir()
#     with db_conn() as conn:
#         c = conn.cursor()
#         c.execute("""
#         CREATE TABLE IF NOT EXISTS logs(
#             id   INTEGER PRIMARY KEY AUTOINCREMENT,
#             ts   INTEGER NOT NULL,     -- epoch seconds (UTC)
#             temp REAL    NOT NULL,
#             hum  REAL    NOT NULL,
#             fan  INTEGER NOT NULL
#         );
#         """)
#         c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);")
#         conn.commit()

# def now_utc_ts() -> int:
#     return int(time.time())

# def iso_from_ts(ts: int) -> str:
#     return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

# def save_row(temp: float, hum: float, fan: int):
#     with db_conn() as conn:
#         conn.execute("INSERT INTO logs(ts,temp,hum,fan) VALUES (?,?,?,?)",
#                      (now_utc_ts(), float(temp), float(hum), int(bool(fan))))
#         conn.commit()

# def cutoff_ts(range_key: str) -> int:
#     now = now_utc_ts()
#     if range_key == "week":  return now - 7*24*3600
#     if range_key == "month": return now - 30*24*3600
#     return now - 24*3600  # default day

# def bucket_seconds(range_key: str) -> int:
#     if range_key == "day":   return 5*60       # 5 minutes
#     if range_key == "week":  return 60*60      # 1 hour
#     if range_key == "month": return 6*60*60    # 6 hours
#     return 5*60

# def query_aggregated(range_key: str):
#     after = cutoff_ts(range_key)
#     step  = bucket_seconds(range_key)
#     with db_conn() as conn:
#         c = conn.cursor()
#         c.execute(f"""
#             SELECT (CAST(ts/{step} AS INTEGER)*{step}) AS bucket,
#                    AVG(temp) AS avg_t,
#                    AVG(hum)  AS avg_h
#             FROM logs
#             WHERE ts >= ?
#             GROUP BY bucket
#             ORDER BY bucket ASC;
#         """, (after,))
#         rows = c.fetchall()
#     times, temps, hums = [], [], []
#     for b, at, ah in rows:
#         times.append(iso_from_ts(int(b)))
#         temps.append(round(float(at), 2))
#         hums.append(round(float(ah), 2))
#     return {"time": times, "temp": temps, "hum": hums}

# # Initialize DB at import time (works under Gunicorn workers)
# init_db()

# @mqtt.on_disconnect()
# def on_mqtt_disconnect():
#     print("[MQTT] Disconnected. Will try to reconnect...")

# @mqtt.on_log()
# def on_mqtt_log(client, userdata, level, buf):
#     print("[MQTT-LOG]", buf)


# # ------------------------------------------------------------------------------
# # Routes
# # ------------------------------------------------------------------------------
# @app.route("/")
# def index():
#     return render_template("dashboard.html")

# @app.route("/history")
# def history_api():
#     r = request.args.get("range", "day")
#     return jsonify(query_aggregated(r))

# @app.route("/export.csv")
# def export_csv():
#     tmp_path = os.path.join(os.path.dirname(__file__), "export.csv")
#     with db_conn() as conn, open(tmp_path, "w", encoding="utf-8") as f:
#         c = conn.cursor()
#         f.write("timestamp_utc,temp,hum,fan\n")
#         for ts, temp, hum, fan in c.execute("SELECT ts,temp,hum,fan FROM logs ORDER BY ts ASC"):
#             f.write(f"{iso_from_ts(ts)},{temp},{hum},{fan}\n")
#     return send_file(tmp_path, as_attachment=True, download_name="greenhouse.csv")

# @app.route("/control", methods=["POST"])
# def control():
#     global mode, fan_state
#     body = request.get_json(silent=True) or {}
#     if "mode" in body:
#         mode = str(body["mode"])
#         socketio.emit("mode", mode)
#         mqtt.publish(TOPIC_CMD, json.dumps({"mode": mode}))
#         print(f"[CMD] mode -> {mode}")

#     if "fan" in body:
#         if mode != "manual":
#             mode = "manual"
#             socketio.emit("mode", mode)
#             mqtt.publish(TOPIC_CMD, json.dumps({"mode": mode}))
#             print("[CMD] force manual before fan")
#         fan_state = int(body["fan"])
#         mqtt.publish(TOPIC_CMD, json.dumps({"fan": fan_state}))
#         print(f"[CMD] fan -> {fan_state}")

#     return jsonify({"ok": True, "mode": mode, "fan": fan_state})

# @app.route("/threshold", methods=["POST"])
# def set_threshold():
#     global threshold_temp
#     body = request.get_json(silent=True) or {}
#     try:
#         threshold_temp = float(body.get("temp", threshold_temp))
#     except Exception:
#         pass
#     socketio.emit("threshold", threshold_temp)
#     mqtt.publish(TOPIC_CMD, json.dumps({"temp": threshold_temp}))
#     print(f"[CMD] threshold -> {threshold_temp}")
#     return jsonify({"ok": True, "threshold": threshold_temp})

# @app.route("/schedule", methods=["POST"])
# def set_schedule():
#     global schedule_cfg
#     body = request.get_json(silent=True) or {}
#     try:
#         on_min  = int(body.get("on_min",  schedule_cfg["on_min"]))
#         off_min = int(body.get("off_min", schedule_cfg["off_min"]))
#         schedule_cfg = {"on_min": on_min, "off_min": off_min}
#     except Exception:
#         pass
#     socketio.emit("schedule", schedule_cfg)
#     mqtt.publish(TOPIC_CMD, json.dumps({"schedule": schedule_cfg}))
#     print(f"[CMD] schedule -> {schedule_cfg}")
#     return jsonify({"ok": True, **schedule_cfg})

# @app.route("/emit_demo")
# def emit_demo():
#     demo = {"temp": 29.4, "hum": 62.1, "fan": 1}
#     global last_data
#     last_data = demo
#     save_row(demo["temp"], demo["hum"], demo["fan"])
#     socketio.emit("update", demo)
#     return "demo sent"

# @app.route("/mqtt_status")
# def mqtt_status():
#     try:
#         # Flask-MQTT giữ client trong thuộc tính .client
#         ok = getattr(mqtt, "client", None) is not None and mqtt.client.is_connected()
#     except Exception:
#         ok = False
#     return jsonify({"connected": bool(ok),
#                     "broker": app.config['MQTT_BROKER_URL'],
#                     "port": app.config['MQTT_BROKER_PORT']})

# @app.route("/mqtt_test_pub")
# def mqtt_test_pub():
#     try:
#         mqtt.publish(TOPIC_CMD, json.dumps({"ping": int(time.time())}))
#         return "published"
#     except Exception as e:
#         return f"publish failed: {e}", 500

# # ------------------------------------------------------------------------------
# # MQTT handlers
# # ------------------------------------------------------------------------------
# @mqtt.on_connect()
# def on_connect(client, userdata, flags, rc):
#     print(f"[MQTT] Connected rc={rc}")
#     mqtt.subscribe(TOPIC_DATA)
#     print(f"[MQTT] Subscribed {TOPIC_DATA}")

# @mqtt.on_message()
# def handle_mqtt(client, userdata, msg):
#     global fan_state, last_data
#     try:
#         data = json.loads(msg.payload.decode())
#     except Exception as e:
#         print("[MQTT] JSON parse error:", e)
#         return

#     try:
#         t = float(data.get("temp"))
#         h = float(data.get("hum"))
#         f = 1 if int(data.get("fan", 0)) else 0
#     except Exception:
#         return

#     fan_state = f
#     last_data = {"temp": t, "hum": h, "fan": f}
#     save_row(t, h, f)
#     socketio.emit("update", last_data)

# # ------------------------------------------------------------------------------
# # Socket.IO events
# # ------------------------------------------------------------------------------
# @socketio.on("connect")
# def on_ws_connect():
#     print("[WS] client", request.sid, "connected")
#     if last_data:
#         socketio.emit("update", last_data, to=request.sid)
#     socketio.emit("mode", mode, to=request.sid)
#     socketio.emit("threshold", threshold_temp, to=request.sid)
#     socketio.emit("schedule", schedule_cfg, to=request.sid)

# # ------------------------------------------------------------------------------
# # Dev runner (not used by Gunicorn in Render, but handy locally)
# # ------------------------------------------------------------------------------
# if __name__ == "__main__":
#     port = int(os.environ.get("PORT", 5000))
#     print(f"[APP] http://localhost:{port}")
#     socketio.run(app, host="0.0.0.0", port=port)

# ================= Mini Greenhouse – Flask + paho-mqtt + Socket.IO =================
# Production on Render (Gunicorn -k eventlet). MQTT dùng paho.loop_start()
# ================================================================================

# import eventlet
# eventlet.monkey_patch()




from flask_socketio import SocketI

from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
import os, json, uuid, time, sqlite3
from datetime import datetime, timezone

# --- paho-mqtt ---
from paho.mqtt.client import Client as PahoClient

app = Flask(__name__)
socketio = SocketIO(app, async_mode="gevent", cors_allowed_origins="*")

# ---------------- MQTT (paho) ----------------
MQTT_HOST = os.environ.get("MQTT_BROKER_URL", "test.mosquitto.org")
MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))
TOPIC_DATA = os.environ.get("MQTT_TOPIC_DATA", "greenhouse/data")
TOPIC_CMD  = os.environ.get("MQTT_TOPIC_CMD",  "greenhouse/cmd")

mqtt_client_id = os.environ.get("MQTT_CLIENT_ID", f"flask-{uuid.uuid4().hex[:8]}")
mqtt = PahoClient(client_id=mqtt_client_id, transport="tcp")

# app state
mode = "auto"
fan_state = 0
threshold_temp = 30.0
schedule_cfg = {"on_min": 5, "off_min": 10}
last_data = None

# ---------------- SQLite ----------------
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "greenhouse.db"))

def ensure_db_dir():
    try: os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    except Exception: pass

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    ensure_db_dir()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            temp REAL NOT NULL,
            hum REAL NOT NULL,
            fan INTEGER NOT NULL
        );
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);")
        conn.commit()

def now_utc_ts(): return int(time.time())
def iso_from_ts(ts): return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def save_row(temp, hum, fan):
    with db_conn() as conn:
        conn.execute("INSERT INTO logs(ts,temp,hum,fan) VALUES (?,?,?,?)",
                     (now_utc_ts(), float(temp), float(hum), int(bool(fan))))
        conn.commit()

def cutoff_ts(r):
    now = now_utc_ts()
    return now - (7*24*3600 if r=="week" else 30*24*3600 if r=="month" else 24*3600)

def bucket_seconds(r):
    return 5*60 if r=="day" else 60*60 if r=="week" else 6*60*60

def query_aggregated(range_key):
    after = cutoff_ts(range_key)
    step  = bucket_seconds(range_key)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(f"""
        SELECT (CAST(ts/{step} AS INTEGER)*{step}) AS bucket, AVG(temp), AVG(hum)
        FROM logs WHERE ts >= ? GROUP BY bucket ORDER BY bucket ASC;
        """, (after,))
        rows = c.fetchall()
    times, temps, hums = [], [], []
    for b, at, ah in rows:
        times.append(iso_from_ts(int(b)))
        temps.append(round(float(at), 2))
        hums.append(round(float(ah), 2))
    return {"time": times, "temp": temps, "hum": hums}

# ---------------- Routes ----------------
@app.route("/")
def index(): return render_template("dashboard.html")

@app.route("/history")
def history_api():
    r = request.args.get("range","day")
    return jsonify(query_aggregated(r))

@app.route("/export.csv")
def export_csv():
    p = os.path.join(os.path.dirname(__file__), "export.csv")
    with db_conn() as conn, open(p, "w", encoding="utf-8") as f:
        c = conn.cursor()
        f.write("timestamp_utc,temp,hum,fan\n")
        for ts, t, h, fan in c.execute("SELECT ts,temp,hum,fan FROM logs ORDER BY ts"):
            f.write(f"{iso_from_ts(ts)},{t},{h},{fan}\n")
    return send_file(p, as_attachment=True, download_name="greenhouse.csv")

@app.route("/control", methods=["POST"])
def control():
    global mode, fan_state
    body = request.get_json(silent=True) or {}
    if "mode" in body:
        mode = str(body["mode"])
        socketio.emit("mode", mode)
        mqtt.publish(TOPIC_CMD, json.dumps({"mode": mode}))
        print(f"[CMD] mode -> {mode}")
    if "fan" in body:
        if mode != "manual":
            mode = "manual"
            socketio.emit("mode", mode)
            mqtt.publish(TOPIC_CMD, json.dumps({"mode": mode}))
            print("[CMD] force manual before fan")
        fan_state = int(body["fan"])
        mqtt.publish(TOPIC_CMD, json.dumps({"fan": fan_state}))
        print(f"[CMD] fan -> {fan_state}")
    return jsonify({"ok": True, "mode": mode, "fan": fan_state})

@app.route("/threshold", methods=["POST"])
def set_threshold():
    global threshold_temp
    body = request.get_json(silent=True) or {}
    try: threshold_temp = float(body.get("temp", threshold_temp))
    except: pass
    socketio.emit("threshold", threshold_temp)
    mqtt.publish(TOPIC_CMD, json.dumps({"temp": threshold_temp}))
    print(f"[CMD] threshold -> {threshold_temp}")
    return jsonify({"ok": True, "threshold": threshold_temp})

@app.route("/schedule", methods=["POST"])
def set_schedule():
    global schedule_cfg
    body = request.get_json(silent=True) or {}
    try:
        on_min  = int(body.get("on_min",  schedule_cfg["on_min"]))
        off_min = int(body.get("off_min", schedule_cfg["off_min"]))
        schedule_cfg = {"on_min": on_min, "off_min": off_min}
    except: pass
    socketio.emit("schedule", schedule_cfg)
    mqtt.publish(TOPIC_CMD, json.dumps({"schedule": schedule_cfg}))
    print(f"[CMD] schedule -> {schedule_cfg}")
    return jsonify({"ok": True, **schedule_cfg})

@app.route("/emit_demo")
def emit_demo():
    demo = {"temp": 29.4, "hum": 62.1, "fan": 1}
    global last_data; last_data = demo
    save_row(demo["temp"], demo["hum"], demo["fan"])
    socketio.emit("update", demo)
    return "demo sent"

# --- tiện kiểm tra nhanh ---
@app.route("/mqtt_status")
def mqtt_status():
    try:
        ok = mqtt.is_connected()
    except Exception:
        ok = False
    return jsonify({"connected": bool(ok), "broker": MQTT_HOST, "port": MQTT_PORT})

@app.route("/mqtt_test_pub")
def mqtt_test_pub():
    mqtt.publish(TOPIC_CMD, json.dumps({"ping": int(time.time())}))
    return "published"

# ---------------- paho callbacks ----------------
def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    print(f"[MQTT] Connected rc={rc}")
    client.subscribe(TOPIC_DATA)
    print(f"[MQTT] Subscribed {TOPIC_DATA}")

def on_mqtt_message(client, userdata, msg):
    global fan_state, last_data
    try:
        data = json.loads(msg.payload.decode())
        t = float(data.get("temp"))
        h = float(data.get("hum"))
        f = 1 if int(data.get("fan", 0)) else 0
    except Exception as e:
        print("[MQTT] Bad payload:", e)
        return
    fan_state = f
    last_data = {"temp": t, "hum": h, "fan": f}
    save_row(t, h, f)
    socketio.emit("update", last_data)

def on_mqtt_disconnect(client, userdata, rc, properties=None):
    print(f"[MQTT] Disconnected rc={rc}. Will reconnect...")

def on_mqtt_log(client, userdata, level, buf):
    print("[MQTT-LOG]", buf)

# ---------------- Socket.IO events ----------------
@socketio.on("connect")
def on_ws_connect():
    print("[WS] client", request.sid, "connected")
    if last_data:
        socketio.emit("update", last_data, to=request.sid)
    socketio.emit("mode", mode, to=request.sid)
    socketio.emit("threshold", threshold_temp, to=request.sid)
    socketio.emit("schedule", schedule_cfg, to=request.sid)

# ---------------- Boot ----------------
init_db()

# cấu hình callback & start loop nền
mqtt.enable_logger()          # thêm log từ paho
mqtt.on_connect = on_mqtt_connect
mqtt.on_message = on_mqtt_message
mqtt.on_disconnect = on_mqtt_disconnect
mqtt.on_log = on_mqtt_log
mqtt.connect_async(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
mqtt.loop_start()             # chạy loop trong background thread

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[APP] http://localhost:{port}")
    socketio.run(app, host="0.0.0.0", port=port)
