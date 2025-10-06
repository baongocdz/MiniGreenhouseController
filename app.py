# app.py  — Flask + Flask-SocketIO(threading) + paho-mqtt + SQLite (Render friendly)
from __future__ import annotations

import os, json, time, sqlite3, threading, socket
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt

# ------------------ CONFIG ------------------
MQTT_HOST = os.getenv("MQTT_HOST", "test.mosquitto.org")   # đổi trong Render nếu cần
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
TOPIC_DATA = os.getenv("TOPIC_DATA", "greenhouse/data")
TOPIC_CMD  = os.getenv("TOPIC_CMD",  "greenhouse/cmd")
DB_PATH    = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__) or ".", "greenhouse.db"))
TTL_DAYS   = int(os.getenv("TTL_DAYS", "14"))

def resolve_ipv4(host: str) -> str:
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(host, None):
            if fam == socket.AF_INET:  # IPv4
                return sockaddr[0]
    except Exception:
        pass
    return host

MQTT_HOST_IP = resolve_ipv4(MQTT_HOST)

# ------------------ APP/WS ------------------
app = Flask(__name__)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ------------------ STATE ------------------
state_lock = threading.Lock()
mode = "auto"
fan_state = 0
threshold_temp = 30.0
schedule_cfg = {"on_min": 5, "off_min": 10}
last_data = None

# ------------------ SQLite helpers ------------------
def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA synchronous=NORMAL;")
        c.execute("""
        CREATE TABLE IF NOT EXISTS logs(
          id   INTEGER PRIMARY KEY AUTOINCREMENT,
          ts   INTEGER NOT NULL,
          temp REAL    NOT NULL,
          hum  REAL    NOT NULL,
          fan  INTEGER NOT NULL
        );""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);")
        conn.commit()
init_db()

def now_ts() -> int: return int(time.time())
def iso_from_ts(ts: int) -> str: return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

def save_row(temp: float, hum: float, fan: int):
    with db_conn() as conn:
        conn.execute("INSERT INTO logs(ts,temp,hum,fan) VALUES (?,?,?,?)",
                     (now_ts(), float(temp), float(hum), int(bool(fan))))
        conn.commit()

def cutoff_ts(r: str) -> int:
    now = now_ts()
    if r == "week":  return now - 7*24*3600
    if r == "month": return now - 30*24*3600
    return now - 24*3600

def bucket_seconds(r: str) -> int:
    return {"day": 5*60, "week": 60*60, "month": 6*60*60}.get(r, 5*60)

def query_aggregated(r: str):
    after = cutoff_ts(r); step = bucket_seconds(r)
    with db_conn() as conn:
        c = conn.cursor()
        c.execute(f"""
          SELECT (CAST(ts/{step} AS INTEGER)*{step}) AS bucket,
                 AVG(temp), AVG(hum)
          FROM logs WHERE ts >= ?
          GROUP BY bucket ORDER BY bucket ASC
        """, (after,))
        rows = c.fetchall()
    times, temps, hums = [], [], []
    for b, at, ah in rows:
        times.append(iso_from_ts(int(b)))
        temps.append(round(float(at), 2))
        hums.append(round(float(ah), 2))
    return {"time": times, "temp": temps, "hum": hums}

def vacuum_and_ttl(days=TTL_DAYS):
    try:
        with db_conn() as conn:
            conn.execute("DELETE FROM logs WHERE ts < ?", (now_ts() - days*24*3600,))
            conn.execute("VACUUM;"); conn.commit()
    except Exception as e:
        print("[DB] TTL/VACUUM error:", e)

# ------------------ MQTT ------------------
mqtt_client = mqtt.Client(client_id=os.getenv("MQTT_CLIENT_ID", f"greenhouse_web_{now_ts()}"))

def on_mqtt_log(client, userdata, level, buf):
    print("[MQTT-LOG]", buf)

def on_mqtt_connect(client, userdata, flags, rc):
    print(f"[MQTT] CONNACK rc={rc}")
    if rc == 0:
        client.subscribe(TOPIC_DATA, qos=0)
        print(f"[MQTT] Subscribed {TOPIC_DATA}")

def on_mqtt_message(client, userdata, msg):
    global last_data, fan_state
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        t = float(data.get("temp")); h = float(data.get("hum")); f = 1 if int(data.get("fan", 0)) else 0
    except Exception as e:
        print("[MQTT] Parse error:", e, msg.payload[:100]); return
    fan_state = f
    last_data = {"temp": t, "hum": h, "fan": f}
    save_row(t, h, f)
    socketio.emit("update", last_data)

mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message
mqtt_client.on_log     = on_mqtt_log

def connect_mqtt():
    print(f"[MQTT] Connecting to {MQTT_HOST} ({MQTT_HOST_IP}):{MQTT_PORT} ...")
    mqtt_client.connect(MQTT_HOST_IP, MQTT_PORT, keepalive=30)
    mqtt_client.loop_start()

def mqtt_watch():
    while True:
        if not mqtt_client.is_connected():
            try:
                connect_mqtt()
            except Exception as e:
                print("[MQTT] connect failed:", e)
        time.sleep(5)

connect_mqtt()
threading.Thread(target=mqtt_watch, daemon=True).start()
threading.Thread(target=lambda: (time.sleep(10), vacuum_and_ttl()), daemon=True).start()

# ------------------ Routes ------------------
@app.route("/")
def index(): return render_template("dashboard.html")

@app.get("/history")
def history_api():
    return jsonify(query_aggregated(request.args.get("range", "day")))

def mqtt_publish(obj: dict):
    mqtt_client.publish(TOPIC_CMD, json.dumps(obj, ensure_ascii=False), qos=0, retain=False)

@app.post("/control")
def control():
    global mode, fan_state
    body = request.get_json(silent=True) or {}
    with state_lock:
        if "mode" in body:
            mode = str(body["mode"]).lower()
            if mode not in ("auto","manual","schedule"): mode = "auto"
            socketio.emit("mode", mode); mqtt_publish({"mode": mode})
            print(f"[CMD] mode -> {mode}")
        if "fan" in body:
            if mode != "manual":
                mode = "manual"; socketio.emit("mode", mode); mqtt_publish({"mode":"manual"})
                print("[CMD] force manual before fan")
            fan_state = 1 if int(body["fan"]) else 0
            mqtt_publish({"fan": bool(fan_state)})
            print(f"[CMD] fan -> {fan_state}")
    return jsonify({"ok": True, "mode": mode, "fan": fan_state})

@app.post("/threshold")
def set_threshold():
    global threshold_temp
    body = request.get_json(silent=True) or {}
    try: threshold_temp = float(body.get("temp", threshold_temp))
    except Exception: pass
    socketio.emit("threshold", threshold_temp); mqtt_publish({"temp": threshold_temp})
    print(f"[CMD] threshold -> {threshold_temp}")
    return jsonify({"ok": True, "threshold": threshold_temp})

@app.post("/schedule")
def set_schedule():
    global schedule_cfg
    body = request.get_json(silent=True) or {}
    try:
        on_min  = int(body.get("on_min",  schedule_cfg["on_min"]))
        off_min = int(body.get("off_min", schedule_cfg["off_min"]))
        schedule_cfg = {"on_min": on_min, "off_min": off_min}
    except Exception: pass
    socketio.emit("schedule", schedule_cfg); mqtt_publish({"mode":"schedule", **schedule_cfg})
    print(f"[CMD] schedule -> {schedule_cfg}")
    return jsonify({"ok": True, **schedule_cfg})

@socketio.on("connect")
def on_ws_connect():
    sid = request.sid
    print("[WS] client", sid, "connected")
    if last_data: socketio.emit("update", last_data, to=sid)
    socketio.emit("mode", mode, to=sid)
    socketio.emit("threshold", threshold_temp, to=sid)
    socketio.emit("schedule", schedule_cfg, to=sid)

# (no __main__ block; Render runs via gunicorn)
