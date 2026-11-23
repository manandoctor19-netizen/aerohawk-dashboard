"""Aerohawk developer dashboard (team only).

Run locally:  python -m streamlit run dev_app.py --server.port 8502
- Without Firebase secrets: DEMO MODE with a fake robot driving around Taylor Center.
- With secrets: reads robots/aerohawk (sent by telemetry_node.py on the Jetson) and the orders collection.
Password: required. Set dev_password = "..." as the first line of the app's Secrets.
"""
import base64
import datetime as dt
import math
import time

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

st.set_page_config(page_title="Aerohawk Dev", page_icon="🛠️", layout="wide")

ROBOT_ID = "aerohawk"
TAYLOR_CENTER = (32.3690, -86.1770)      # approximate, only used for demo mode
STATUSES = ["new", "queued", "accepted", "picked_up", "delivered", "cancelled"]


# ---------------------------------------------------------------------------
# Firebase (or demo)
# ---------------------------------------------------------------------------
@st.cache_resource
def get_db():
    try:
        creds = dict(st.secrets["firebase"])
    except Exception:
        return None
    import firebase_admin
    from firebase_admin import credentials, firestore
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(creds))
    return firestore.client()


db = get_db()
DEMO = db is None


@st.cache_resource
def live():
    """Firestore listeners shared by everyone viewing the dashboard.

    Firebase's free plan allows 50,000 reads a day. Re-reading every 2-4 s would use that up
    in about an hour, so instead we listen: Firestore sends only what changed, and every
    refresh below reads from memory (0 reads).
    """
    from firebase_admin import firestore

    class Live:
        robot, control, orders, alerts = {}, {}, {}, {}

    L = Live()

    def one_doc(attr):
        def cb(docs, changes, read_time):
            for d in docs:
                setattr(L, attr, d.to_dict() or {})
        return cb

    def many(attr):
        def cb(docs, changes, read_time):
            setattr(L, attr, {d.id: {"id": d.id, **(d.to_dict() or {})} for d in docs})
        return cb

    desc = firestore.Query.DESCENDING
    L.watches = [
        db.collection("robots").document(ROBOT_ID).on_snapshot(one_doc("robot")),
        db.collection("robots").document(f"{ROBOT_ID}_control").on_snapshot(one_doc("control")),
        db.collection("orders").order_by("created_at", direction=desc).limit(40).on_snapshot(many("orders")),
        db.collection("alerts").order_by("at", direction=desc).limit(10).on_snapshot(many("alerts")),
    ]
    return L


def _newest_first(rows, field):
    far_past = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    return sorted(rows, key=lambda r: r.get(field) or far_past, reverse=True)


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def get_robot():
    if DEMO:
        t = time.time()
        a = (t / 40) % (2 * math.pi)
        states = ["IDLE", "GO_TO_PICKUP", "WAIT_FOR_ORDER", "GO_TO_DROPOFF", "BLOCKED"]
        return {
            "state": states[int(t // 8) % len(states)],
            "lat": TAYLOR_CENTER[0] + 0.0006 * math.sin(a),
            "lon": TAYLOR_CENTER[1] + 0.0008 * math.cos(a),
            "gps_fix": True, "speed": 0.6, "current_order": "demo0001",
            "last_seen": now_utc(), "camera_jpeg": None,
            "mode": st.session_state.get("demo_mode", "MANUAL"),
        }
    return dict(live().robot) or None


def get_orders(limit=15):
    if DEMO:
        if "demo_orders" not in st.session_state:
            st.session_state.demo_orders = [
                {"id": "demo0001", "name": "Jack", "restaurant": "Pi Pizza Co.", "pickup": "Taylor Center",
                 "dropoff": "Goodwyn Hall", "status": "accepted", "kitchen_status": "preparing", "order_number": 17,
                 "pickup_code": "4821", "total": 9.97, "notes": "extra napkins",
                 "items": [{"name": "Pepperoni slice", "qty": 2, "price": 3.99},
                           {"name": "Fountain soda", "qty": 1, "price": 1.99}]},
                {"id": "demo0002", "name": "Joe", "restaurant": "Books & Beans Coffee Shop",
                 "pickup": "Taylor Center", "dropoff": "Library", "status": "queued", "kitchen_status": "new", "order_number": 18,
                 "pickup_code": "7305", "total": 6.70, "notes": "",
                 "items": [{"name": "Iced coffee", "qty": 1, "price": 3.75},
                           {"name": "Blueberry muffin", "qty": 1, "price": 2.95}]},
            ]
        return st.session_state.demo_orders
    return _newest_first(list(live().orders.values()), "created_at")[:limit]


def set_status(order_id, status):
    if DEMO:
        for o in st.session_state.demo_orders:
            if o["id"] == order_id:
                o["status"] = status
        return
    from firebase_admin import firestore
    db.collection("orders").document(order_id).update(
        {"status": status, "updated_at": firestore.SERVER_TIMESTAMP, "manual_override": True})


FINISHED = ("delivered", "cancelled")


def delete_order(order_id):
    if DEMO:
        st.session_state.demo_orders = [o for o in st.session_state.demo_orders if o["id"] != order_id]
        return
    db.collection("orders").document(order_id).delete()


def clear_finished_orders():
    """Delete every delivered or cancelled order. Returns how many were removed."""
    if DEMO:
        before = len(st.session_state.demo_orders)
        st.session_state.demo_orders = [o for o in st.session_state.demo_orders if o.get("status") not in FINISHED]
        return before - len(st.session_state.demo_orders)
    from google.cloud.firestore_v1.base_query import FieldFilter
    n, batch = 0, db.batch()
    for d in db.collection("orders").where(filter=FieldFilter("status", "in", list(FINISHED))).stream():
        batch.delete(d.reference)
        n += 1
        if n % 400 == 0:           # Firestore batches hold up to 500 writes
            batch.commit()
            batch = db.batch()
    batch.commit()
    return n


def set_kitchen(order_id, kitchen_status):
    """Kitchen progress (new -> preparing -> ready). The robot's status is separate and unchanged."""
    if DEMO:
        for o in st.session_state.demo_orders:
            if o["id"] == order_id:
                o["kitchen_status"] = kitchen_status
        return
    from firebase_admin import firestore
    db.collection("orders").document(order_id).update(
        {"kitchen_status": kitchen_status, "kitchen_updated_at": firestore.SERVER_TIMESTAMP})


def order_label(o):
    n = o.get("order_number")
    return f"#{n}" if n else f"#{o['id'][:5].upper()}"


def items_text(o):
    return ", ".join(f"{i['qty']}× {i['name']}" for i in o.get("items", [])) or "(no items listed)"


def set_mode(mode):
    """Ask the robot to switch mode. telemetry_node.py on the Jetson picks this up."""
    if DEMO:
        st.session_state.demo_mode = mode
        return
    from firebase_admin import firestore
    db.collection("robots").document(f"{ROBOT_ID}_control").set(
        {"mode": mode, "set_at": firestore.SERVER_TIMESTAMP}, merge=True)


def send_drive(cmd, speed):
    st.session_state.last_drive = f"{cmd} · {speed} m/s · {time.strftime('%H:%M:%S')}"
    if DEMO:
        return
    from firebase_admin import firestore
    db.collection("robots").document(f"{ROBOT_ID}_control").set(
        {"drive": {"cmd": cmd, "speed": speed, "seq": time.time_ns(), "at": firestore.SERVER_TIMESTAMP}},
        merge=True)


def send_say(text):
    text = (text or "").strip()[:120]
    if not text:
        return
    st.session_state.last_say = text
    if DEMO:
        return
    from firebase_admin import firestore
    db.collection("robots").document(f"{ROBOT_ID}_control").set(
        {"say": {"text": text, "seq": time.time_ns(), "at": firestore.SERVER_TIMESTAMP}}, merge=True)


def get_requested_mode():
    if DEMO:
        return st.session_state.get("demo_mode", "MANUAL")
    return live().control.get("mode", "MANUAL")


# ---------------------------------------------------------------------------
# Password gate
# ---------------------------------------------------------------------------
def password_ok():
    if DEMO or st.session_state.get("authed"):
        return True
    try:
        expected = st.secrets.get("dev_password")
    except Exception:
        expected = None
    st.title("🛠️ Aerohawk developer dashboard")
    if not expected:
        # This code is public, so there is no built-in password: it must be set in Secrets.
        st.error('No team password set. Add  dev_password = "..."  as the first line of this app\'s Secrets.')
        return False
    pw = st.text_input("Team password", type="password")
    if pw:
        if pw == expected:
            st.session_state.authed = True
            st.rerun()
        st.error("Wrong password.")
    return False


if not password_ok():
    st.stop()

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
st.title("🛠️ Aerohawk developer dashboard")
if DEMO:
    st.info("Demo mode: fake robot data. Connect Firebase to see the real robot.", icon="🧪")


def demo_camera_jpeg():
    """Fake camera frame for demo mode, so the camera space is visible."""
    import io
    from PIL import Image, ImageDraw
    w, h = 640, 360
    img = Image.new("RGB", (w, h), (28, 31, 38))
    d = ImageDraw.Draw(img)
    x = int((time.time() * 120) % w)
    d.rectangle([x - 40, 0, x, h], fill=(45, 50, 60))
    d.text((20, 20), "DEMO CAMERA", fill=(240, 169, 91))
    d.text((20, h - 30), time.strftime("%H:%M:%S"), fill=(200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


CAMERA_PLACEHOLDER = """
<div style="aspect-ratio:16/9;background:#1c1f26;border-radius:12px;display:flex;
align-items:center;justify-content:center;color:#8a8f99;text-align:center;padding:20px">
<div><div style="font-size:2.5rem">📷</div>Waiting for camera…<br>
<span style="font-size:.85rem">Start telemetry_node.py on the Jetson</span></div></div>
"""


def stream_html(url):
    """Live MJPEG video in the page; retries every 3 s if the robot can't be reached."""
    return f"""
<style>html,body{{margin:0;background:transparent}}</style>
<div style="position:relative;height:396px;background:#1c1f26;border-radius:12px;overflow:hidden">
  <img id="v" src="{url}" style="width:100%;height:100%;object-fit:contain;display:block">
  <div id="m" style="display:none;position:absolute;inset:0;color:#8a8f99;font-family:sans-serif;
       align-items:center;justify-content:center;text-align:center;padding:20px">
    <div><div style="font-size:2.2rem">📷</div>Live stream not reachable, retrying…<br>
    <small>Robot off, or try another option in Stream settings</small></div></div>
  <span id="live" style="position:absolute;top:10px;left:10px;background:#c0392b;color:#fff;
       font:600 12px sans-serif;padding:3px 8px;border-radius:6px">● LIVE</span>
</div>
<script>
const v=document.getElementById('v'), m=document.getElementById('m'), live=document.getElementById('live');
const base="{url}";
v.onerror=()=>{{ m.style.display='flex'; v.style.display='none'; live.style.display='none';
  setTimeout(()=>{{ v.style.display='block'; v.src=base+'&r='+Date.now(); }}, 3000); }};
v.onload=()=>{{ m.style.display='none'; live.style.display='inline'; }};
</script>"""


def pick_stream_url(r):
    custom = st.session_state.get("custom_stream", "").strip()
    if custom:
        return custom
    pub, lan = r.get("stream_url_public"), r.get("stream_url_lan")
    via = st.session_state.get("stream_via", "Auto")
    if via == "Internet":
        return pub
    if via == "Same Wi-Fi":
        return lan
    return pub or lan


@st.fragment(run_every=2)
def snapshot_panel():
    """Backup when there's no live stream: the 2-second snapshot."""
    r = get_robot() or {}
    img = demo_camera_jpeg() if DEMO else r.get("camera_jpeg")
    if img:
        st.image(base64.b64decode(img), use_container_width=True)
        cam_ts = now_utc() if DEMO else r.get("camera_ts")
        if cam_ts:
            cam_age = (now_utc() - cam_ts).total_seconds()
            st.caption(f"Snapshot mode (no live stream yet) · {cam_age:.0f}s old"
                       + ("  ⚠️ stale" if cam_age > 10 else ""))
    else:
        st.markdown(CAMERA_PLACEHOLDER, unsafe_allow_html=True)


@st.fragment(run_every=5)
def stream_watcher(shown_url):
    """If the robot restarts it gets a new stream link: reload the video automatically."""
    if pick_stream_url(get_robot() or {}) != shown_url:
        st.rerun()


@st.fragment(run_every=2)
def status_panel():
    r = get_robot() or {}
    last = r.get("last_seen")
    age = (now_utc() - last).total_seconds() if last else None
    online = age is not None and age < 10
    st.subheader("Robot status")
    st.metric("Connection", "🟢 Online" if online else "🔴 Offline",
              f"seen {age:.0f}s ago" if age is not None else "never seen", delta_color="off")
    st.metric("State", str(r.get("state", "unknown")).replace("_", " ").title())
    a, b = st.columns(2)
    a.metric("Speed", f"{r.get('speed', 0):.2f} m/s")
    b.metric("GPS", "Fix ✅" if r.get("gps_fix") else "No fix ❌")
    if r.get("state") == "BLOCKED":
        st.error("Safety stop: something is in front of the robot.")
    if r.get("current_order"):
        st.caption(f"Current order: `{r['current_order']}` "
                   + (f"({r['current_order_status']})" if r.get("current_order_status") else ""))


@st.fragment(run_every=2)
def map_panel():
    r = get_robot() or {}
    st.subheader("📍 Location")
    if r.get("lat") is not None and r.get("lon") is not None:
        st.map(pd.DataFrame([{"lat": r["lat"], "lon": r["lon"]}]), zoom=16, size=6, height=300)
        st.caption(f"{r['lat']:.6f}, {r['lon']:.6f}  ·  GPS is accurate to about 2-5 m")
    else:
        st.info("No GPS position yet (needs open sky).")


@st.fragment
def drive_panel():
    """Arrow buttons: each tap = a ~0.6 s nudge, then the robot stops by itself."""
    st.subheader("🕹️ Remote drive")
    manual = get_requested_mode() == "MANUAL"
    if not manual:
        st.info("Switch to 🎮 Manual (top bar) to drive from here.")
    speed = 0.3 if st.radio("Speed", ["Slow", "Normal"], horizontal=True, key="drive_speed") == "Slow" else 0.5
    off = not manual
    # Keep the arrow cross side by side on phones (Streamlit stacks columns on narrow screens)
    st.markdown("""<style>
      .st-key-drivepad [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: .5rem !important; }
      .st-key-drivepad [data-testid="stColumn"] { min-width: 0 !important; width: auto !important; flex: 1 1 0 !important; }
      .st-key-drivepad button { min-height: 3rem; font-size: 1.4rem; }
    </style>""", unsafe_allow_html=True)
    pad = st.container(key="drivepad")
    _, up, _ = pad.columns(3)
    up.button("⬆️", key="d_fwd", on_click=send_drive, args=("forward", speed), disabled=off,
              use_container_width=True, help="Forward")
    lt, stp, rt = pad.columns(3)
    lt.button("⬅️", key="d_left", on_click=send_drive, args=("left", speed), disabled=off,
              use_container_width=True, help="Forward + turn left")
    stp.button("⏹️", key="d_stop", on_click=send_drive, args=("stop", speed),
               use_container_width=True, help="Stop now")
    rt.button("➡️", key="d_right", on_click=send_drive, args=("right", speed), disabled=off,
              use_container_width=True, help="Forward + turn right")
    _, dn, _ = pad.columns(3)
    dn.button("⬇️", key="d_back", on_click=send_drive, args=("back", speed), disabled=off,
              use_container_width=True, help="Backward")
    st.caption("Each tap moves the robot about half a second, then it stops. "
               "Commands take 0.5–2 s to arrive, so watch the camera and tap gently. "
               + (f"Last sent: {st.session_state['last_drive']}" if st.session_state.get("last_drive") else ""))

    st.markdown("**📢 Make the robot speak**")
    q1, q2, q3 = st.columns(3)
    q1.button("Excuse me!", key="say1", on_click=send_say, args=("Excuse me, coming through!",), use_container_width=True)
    q2.button("Food's here", key="say2", on_click=send_say, args=("Your food is here!",), use_container_width=True)
    q3.button("Hello 👋", key="say3", on_click=send_say, args=("Hello! I am Aerohawk, your campus delivery robot.",),
              use_container_width=True)
    with st.form("say_form", clear_on_submit=True, border=False):
        a, b = st.columns([3, 1], vertical_alignment="bottom")
        text = a.text_input("Say something", max_chars=120, placeholder="Type a message…")
        if b.form_submit_button("Say", use_container_width=True):
            send_say(text)
    if st.session_state.get("last_say"):
        st.caption(f"Last said: “{st.session_state['last_say']}”")


def camera_and_status():
    cam_col, info_col = st.columns([2.2, 1], gap="large")
    with cam_col:
        st.subheader("📷 Live camera")
        url = pick_stream_url(get_robot() or {})
        if url:
            components.html(stream_html(url), height=400)
        else:
            snapshot_panel()
        stream_watcher(url)
        with st.expander("Stream settings"):
            st.radio("Connect via", ["Auto", "Internet", "Same Wi-Fi"], key="stream_via", horizontal=True,
                     help="Auto uses the internet link if the robot has one, otherwise same Wi-Fi.")
            st.text_input("Custom stream link (optional)", key="custom_stream",
                          placeholder="paste a link printed by camera_stream.py")
            st.button("🔄 Reconnect stream")
            st.caption("**Internet** works from anywhere (needs cloudflared on the Jetson). "
                       "**Same Wi-Fi** needs this laptop and the Jetson on the same network or hotspot, "
                       "and only works when the dashboard runs on your laptop (not the online version).")
    with info_col:
        status_panel()
        st.divider()
        drive_panel()
    map_panel()


MODE_LABELS = {"AUTO": "🤖 Autonomous", "MANUAL": "🎮 Manual", "STOP": "🛑 STOPPED"}


@st.fragment(run_every=2)
def control_panel():
    r = get_robot() or {}
    requested = get_requested_mode()
    reported = r.get("mode")
    with st.container(border=True):
        info, b1, b2, b3 = st.columns([2.2, 1, 1, 1], vertical_alignment="center")
        info.markdown(f"**Mode:** {MODE_LABELS.get(reported, 'unknown (robot offline?)')}")
        if reported != requested:
            info.caption(f"Sent {MODE_LABELS.get(requested, requested)} · waiting for the robot to confirm…")
        elif reported == "AUTO":
            info.caption("Robot drives itself to the next order's waypoints.")
        elif reported == "MANUAL":
            info.caption("Robot only moves with keyboard teleop. Orders wait.")
        else:
            info.caption("All motion blocked by the safety node.")
        if b1.button("🤖 Autonomous", use_container_width=True, disabled=requested == "AUTO"):
            set_mode("AUTO")
            st.rerun(scope="fragment")
        if b2.button("🎮 Manual", use_container_width=True, disabled=requested == "MANUAL"):
            set_mode("MANUAL")
            st.rerun(scope="fragment")
        if b3.button("🛑 STOP", type="primary", use_container_width=True, disabled=requested == "STOP"):
            set_mode("STOP")
            st.rerun(scope="fragment")
    st.caption("⚠️ Remote STOP goes through the internet and can take 1–3 s. "
               "Someone must always stand near the robot with the keyboard or power switch.")


@st.fragment(run_every=4)
def orders_panel():
    st.subheader("All orders")
    st.caption("Override buttons change the status the student sees. Use them if the robot gets stuck during the demo. "
               "At delivery, check the student's pickup code against this list.")
    all_orders = get_orders(40)
    n_done = sum(1 for o in all_orders if o.get("status") in FINISHED)
    a, b = st.columns([1, 1], vertical_alignment="center")
    show_done = a.toggle(f"Show finished orders ({n_done})", value=False, key="show_done",
                         help="Delivered and cancelled orders are hidden so the list stays short.")
    if b.button(f"🧹 Delete finished orders ({n_done})", disabled=n_done == 0,
                help="Permanently deletes every delivered and cancelled order. Active orders stay."):
        removed = clear_finished_orders()
        st.toast(f"Removed {removed} finished order(s)")
        st.rerun(scope="fragment")
    orders = all_orders if show_done else [o for o in all_orders if o.get("status") not in FINISHED]
    if not orders:
        st.info("No active orders right now." if n_done else "No orders yet.")
        return
    pending = st.session_state.get("pending_delete")
    for o in orders:
        with st.container(border=True):
            a, b = st.columns([3, 2])
            a.markdown(f"**{order_label(o)}** · **{o.get('restaurant', '?')}** → **{o.get('dropoff', '?')}** · code **{o.get('pickup_code', '-')}**  \n"
                       f"{o.get('name', '')}: {items_text(o)}  \n"
                       f"`{o['id']}` · robot: **{o.get('status')}** · kitchen: **{o.get('kitchen_status', '-')}**"
                       + ("  ⚠️ manual" if o.get("manual_override") else "")
                       + ("  ✅ code entered on robot" if o.get("delivered_by") == "pickup_code" else "")
                       + (f"  🚨 {o['wrong_code_attempts']} wrong code tries" if o.get("wrong_code_attempts", 0) >= 3 else ""))
            cur = o.get("status")
            new = b.selectbox("Set status", STATUSES, index=STATUSES.index(cur) if cur in STATUSES else 0,
                              key=f"sel_{o['id']}", label_visibility="collapsed")
            c1, c2 = b.columns(2)
            if c1.button("Apply", key=f"btn_{o['id']}", disabled=(new == cur), use_container_width=True):
                set_status(o["id"], new)
                st.rerun(scope="fragment")
            if pending != o["id"]:
                if c2.button("🗑️ Remove", key=f"del_{o['id']}", use_container_width=True):
                    st.session_state.pending_delete = o["id"]
                    st.rerun(scope="fragment")
            else:
                active = cur not in FINISHED
                b.warning("Remove this order?" + (" It's still active: the student's page will say the order "
                                                   "can't be found." if active else ""))
                y, n = b.columns(2)
                if y.button("Yes, remove", key=f"yes_{o['id']}", type="primary", use_container_width=True):
                    delete_order(o["id"])
                    st.session_state.pending_delete = None
                    st.rerun(scope="fragment")
                if n.button("Cancel", key=f"no_{o['id']}", use_container_width=True):
                    st.session_state.pending_delete = None
                    st.rerun(scope="fragment")


@st.fragment(run_every=3)
def kitchen_panel():
    st.subheader("🍕 Kitchen")
    st.caption("Press **Start preparing** when you begin, and **Ready for robot** when the food is bagged. "
               "The student's phone updates within ~3 s.")
    active = [o for o in get_orders(30)
              if o.get("status") not in ("delivered", "cancelled", "picked_up")]
    active.sort(key=lambda o: str(o.get("created_at", "")))   # oldest first
    if not active:
        st.success("No orders waiting. 🎉")
        return
    for o in active:
        k = o.get("kitchen_status", "new")
        badge = {"new": "🆕 NEW", "preparing": "👩‍🍳 PREPARING", "ready": "✅ READY, waiting for robot"}.get(k, k)
        with st.container(border=True):
            st.markdown(f"#### {order_label(o)} · {badge} · {o.get('restaurant', '?')}")
            for i in o.get("items", []):
                st.markdown(f"- **{i['qty']}×** {i['name']}")
            if o.get("notes"):
                st.markdown(f"📝 _{o['notes']}_")
            st.caption(f"For {o.get('name', '')} · deliver to {o.get('dropoff', '?')} · ${o.get('total', 0):.2f}")
            if k == "new":
                if st.button("👩‍🍳 Start preparing", key=f"kp_{o['id']}", use_container_width=True):
                    set_kitchen(o["id"], "preparing")
                    st.rerun(scope="fragment")
            elif k == "preparing":
                if st.button("✅ Ready for robot", key=f"kr_{o['id']}", type="primary", use_container_width=True):
                    set_kitchen(o["id"], "ready")
                    st.rerun(scope="fragment")


# ---------------------------------------------------------------------------
# 🚨 Alerts (someone hit the robot, etc.): always on top, checked every 3 s
# ---------------------------------------------------------------------------
def get_alerts():
    if DEMO:
        return st.session_state.setdefault("demo_alerts", [])
    return _newest_first(list(live().alerts.values()), "at")


def ack_alert(alert_id):
    if DEMO:
        for a in st.session_state.demo_alerts:
            if a["id"] == alert_id:
                a["acknowledged"] = True
        return
    db.collection("alerts").document(alert_id).update({"acknowledged": True})


def send_test_alert():
    data = {"type": "test", "message": "Test alert from the dashboard", "acknowledged": False}
    if DEMO:
        st.session_state.setdefault("demo_alerts", []).insert(0, {**data, "id": f"t{time.time_ns()}", "at": now_utc()})
        return
    from firebase_admin import firestore
    db.collection("alerts").add({**data, "at": firestore.SERVER_TIMESTAMP})


@st.fragment(run_every=3)
def alerts_panel():
    try:
        open_alerts = [a for a in get_alerts() if not a.get("acknowledged")]
    except Exception:
        return
    seen = st.session_state.setdefault("seen_alerts", set())
    for a in open_alerts:
        if a["id"] not in seen:
            seen.add(a["id"])
            st.toast(f"🚨 {a.get('message', 'Robot alert')}", icon="🚨")
    for a in open_alerts:
        at = a.get("at")
        when = f"{(now_utc() - at).total_seconds() / 60:.0f} min ago" if at else ""
        with st.container(border=True):
            st.error(f"🚨 **{a.get('message', 'Robot alert')}**  ·  {when}"
                     + (f"  ·  robot was: {a['robot_state']}" if a.get("robot_state") else ""))
            if a.get("camera_jpeg"):
                st.image(base64.b64decode(a["camera_jpeg"]), caption="Camera at the moment of the alert", width=320)
            if a.get("lat") and a.get("lon"):
                st.caption(f"Location: {a['lat']:.6f}, {a['lon']:.6f}")
            if st.button("✅ Handled", key=f"ack_{a['id']}"):
                ack_alert(a["id"])
                st.rerun(scope="fragment")


alerts_panel()

tab_robot, tab_kitchen = st.tabs(["🤖 Robot", "🍕 Kitchen"])
with tab_robot:
    control_panel()
    camera_and_status()
    st.divider()
    orders_panel()
with tab_kitchen:
    kitchen_panel()

with st.expander("🔧 Test the alert system"):
    st.caption("Sends a fake alert so you can check the red banner works. Click ✅ Handled to clear it.")
    st.button("Send test alert", on_click=send_test_alert)
