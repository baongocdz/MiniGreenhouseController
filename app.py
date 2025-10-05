# ============================================================
# 🌿 Mini Greenhouse Controller – Flask + Socket.IO + MQTT (IPv6 Ready for Render)
# ============================================================

from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
import os, json, uuid, time, sqlite3
from datetime import datetime, timezone

# --- MQTT (paho-mqtt) ---
import socket
import paho.mqtt.client as mqtt

# ============================================================
# ⚙️ Bắt buộc: ép MQTT dùng IPv6 (Render là IPv6-only)
# ============================================================
_orig_create_conn = socket.create_connection
def _v6_create_conn(address, *args, **kwargs):
    host, port = address
    # ép family=AF_INET6 để tạo socket IPv6
    return _orig_create_conn((host, port, 0, 0), *args, family=socket.AF_INET6)
socket.create_connection = _v6_create_conn

# ============================================================
# 🌐 Flask & SocketIO setup
# ============================================================
app = Flask(__name__)
socketio = SocketIO(app, async_mode="eventlet", cors_allowed_origins="*")

# ============================================================
# 🔌 MQTT cấu hình
# ============================================================
MQTT_HOST = os.environ.get("MQTT_BROKER_URL", "test.mosquitto.org")
MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", "1883"))
MQTT_KEEPALIVE = int(os.environ.get("MQTT_KEEPALIVE", "60"))
TOPIC_DATA = os.environ.get("MQTT_TOPIC_DATA", "greenhouse/data")
TOPIC_CMD  = os.environ.get("MQTT_TOPIC_CMD",  "greenhouse/cmd")

mqtt_client_id = os.environ.get("MQTT_CLIENT_ID", f"flask-{uuid.uuid4().hex[:8]}")
mqtt = mqtt.Client(client_id=mqtt_client_id, transport="tcp")
mqtt.reconnect_delay_set(min_delay=1, max_delay=60)

# ============================================================
# 🌡️ Trạng thái ứng dụng
# ============================================================
mode = "auto"
fan_state = 0
threshold_temp = 30.0
schedule_cfg = {"on_min": 5, "off_min": 10}
last_data = None
mqtt_connected = False

# ============================================================
# 🗄️ SQLite Database
# ============================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "greenhouse.db")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            temp REAL NOT NULL,
            hum REAL NOT NULL,
            fan INTEGER NOT NULL
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);")
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

# ============================================================
# 🌍 Flask Routes
# ============================================================
@app.route("/")
def index():
    return "🌿 Mini Greenhouse online – Flask MQTT ready!"

@app.route("/history")
def history_api():
    r = request.args.get("range","day")
    return jsonify(query_aggregated(r))

@app.route("/export.csv")
def export_csv():
    p = os.path.join(os.path.dirname(__file__), "export.csv")
    with db_conn() as conn, open(p, "w", encoding="utf-8") as f:
        f.write("timestamp_utc,temp,hum,fan\n")
        for ts, t, h, fan in conn.execute("SELECT ts,temp,hum,fan FROM logs ORDER BY ts"):
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

@app.route("/mqtt_status")
def mqtt_status():
    return jsonify({"connected": mqtt_connected, "broker": MQTT_HOST, "port": MQTT_PORT})

@app.route("/mqtt_test_pub")
def mqtt_test_pub():
    mqtt.publish(TOPIC_CMD, json.dumps({"ping": int(time.time())}))
    return "published"

# ============================================================
# 📡 MQTT callbacks
# ============================================================
def on_mqtt_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected
    print(f"[MQTT] Connected rc={rc} -> host={MQTT_HOST}:{MQTT_PORT}")
    mqtt_connected = (rc == 0)
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
    global mqtt_connected
    mqtt_connected = False
    print(f"[MQTT] Disconnected rc={rc}. Reconnecting...")

def on_mqtt_log(client, userdata, level, buf):
    print("[MQTT-LOG]", buf)

# ============================================================
# 🔌 Socket.IO events
# ============================================================
@socketio.on("connect")
def on_ws_connect():
    if last_data:
        socketio.emit("update", last_data)
    socketio.emit("mode", mode)
    socketio.emit("threshold", threshold_temp)
    socketio.emit("schedule", schedule_cfg)

# ============================================================
# 🚀 Boot
# ============================================================
init_db()
mqtt.on_connect = on_mqtt_connect
mqtt.on_message = on_mqtt_message
mqtt.on_disconnect = on_mqtt_disconnect
mqtt.on_log = on_mqtt_log
print(f"[MQTT] Connecting to {MQTT_HOST}:{MQTT_PORT} ...")
mqtt.connect_async(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
mqtt.loop_start()  # chạy nền

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[APP] http://localhost:{port}")
    socketio.run(app, host="0.0.0.0", port=port)
