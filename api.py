from flask import Flask, request, jsonify
from dotenv import load_dotenv
from pymongo import MongoClient
from email.message import EmailMessage
from datetime import datetime
import random
import time
import smtplib
import hashlib
import hmac
import re
import os

load_dotenv()
app = Flask(__name__)

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "vybex_admin_123")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PBKDF2_ROUNDS = 150_000
LOCK_AFTER = 5
LOCK_SECONDS = 60

DB_SAFE_LIMIT_MB = 490.0
GMAIL_DAILY_LIMIT = 485

# Primary MongoDB connection for system config & logs
PRIMARY_URI = os.getenv("MONGO_URI", "").strip()
if not PRIMARY_URI:
    raise ValueError("❌ MONGO_URI missing in .env ya Render Environment!")

primary_client = MongoClient(PRIMARY_URI, serverSelectionTimeoutMS=5000)
system_db = primary_client["vybex_system_db"]  # System settings yahan rahengi

# --- DYNAMIC DB NODES LOADER ---
def get_mongo_nodes():
    saved_dbs = list(system_db["db_clusters"].find({}, {"_id": 0}))
    if not saved_dbs:
        # Default fallback from .env
        default_uri = PRIMARY_URI
        saved_dbs = [{"name": "DB_1", "uri": default_uri}]
        system_db["db_clusters"].insert_one(saved_dbs[0])

    nodes = []
    for item in saved_dbs:
        try:
            cli = MongoClient(item["uri"], serverSelectionTimeoutMS=4000)
            nodes.append({"name": item["name"], "client": cli, "db": cli["vybex_music_db"]})
        except Exception as e:
            print(f"⚠️ Cluster {item['name']} connect nahi ho paya: {e}")
    return nodes if nodes else [{"name": "DB_1", "client": primary_client, "db": primary_client["vybex_music_db"]}]

def get_db_usage_mb(db_obj):
    try:
        stats = db_obj.command("dbstats")
        storage_b = float(stats.get("storageSize", 0)) + float(stats.get("indexSize", 0))
        data_b = float(stats.get("dataSize", 0))
        return round(max(storage_b, data_b) / (1024 * 1024), 2)
    except Exception:
        return 0.0

def get_writable_node():
    nodes = get_mongo_nodes()
    for node in nodes:
        if get_db_usage_mb(node["db"]) < DB_SAFE_LIMIT_MB:
            return node
    return nodes[-1]

def find_user_across_dbs(query_dict):
    nodes = get_mongo_nodes()
    for node in nodes:
        u = node["db"]["users"].find_one(query_dict)
        if u:
            return u, node
    return None, None

# --- DYNAMIC GMAIL SMTP POOL LOADER ---
def get_smtp_pool():
    saved_gmails = list(system_db["gmail_pool"].find({}, {"_id": 0}))
    if not saved_gmails:
        # Default fallback from .env
        e0 = os.getenv("SMTP_EMAIL", "").strip()
        p0 = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
        if e0 and p0:
            saved_gmails = [{"slot": "GMAIL_1", "email": e0, "password": p0}]
            system_db["gmail_pool"].insert_one(saved_gmails[0])
    return saved_gmails

def get_today_otp_count(sender_email):
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    return system_db["otp_logs"].count_documents({
        "sender_gmail": sender_email,
        "date": today_str,
        "status": "sent",
    })

def send_otp_with_failover(receiver_email, otp):
    pool = get_smtp_pool()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    if not pool:
        system_db["otp_logs"].insert_one({
            "sender_gmail": "DEMO_MODE", "receiver_email": receiver_email,
            "date": today_str, "ts": time.time(), "status": "demo",
        })
        return True, f"Demo Mode OTP: {otp} (Add Gmail in Admin Panel)", "DEMO_MODE"

    last_err = "All Gmail accounts reached daily limit!"
    for acc in pool:
        sender = acc["email"]
        if get_today_otp_count(sender) >= GMAIL_DAILY_LIMIT:
            continue

        msg = EmailMessage()
        msg["Subject"] = f"{otp} is your VYBEX verification code"
        msg["From"] = f"VYBEX Music <{sender}>"
        msg["To"] = receiver_email
        msg.set_content(f"Your VYBEX verification OTP is: {otp}. Valid for 5 minutes.")
        msg.add_alternative(f"""
        <div style="font-family: Arial, sans-serif; background: #071A16; color: white; padding: 30px; border-radius: 18px; text-align: center; max-width: 420px; margin: auto; border: 1.5px solid #00E676;">
            <h2 style="color: #00E676; margin-bottom: 5px; letter-spacing: 2px;">🎵 VYBEX</h2>
            <p style="color: #A7B8C0; font-size: 14px;">Verify your Gmail address to activate your VYBEX account</p>
            <div style="background: #102129; padding: 16px; border-radius: 14px; margin: 22px 0; letter-spacing: 8px; font-size: 30px; font-weight: bold; color: #00E5FF; border: 1px solid #00E5FF;">
                {otp}
            </div>
            <p style="color: #808080; font-size: 12px;">This verification code expires in 5 minutes.</p>
        </div>
        """, subtype="html")

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=12) as server:
                server.login(sender, acc["password"])
                server.send_message(msg)

            system_db["otp_logs"].insert_one({
                "sender_gmail": sender, "receiver_email": receiver_email,
                "date": today_str, "ts": time.time(), "status": "sent",
            })
            return True, "OTP sent successfully!", sender
        except Exception as e:
            last_err = f"{sender} failed: {str(e)[:60]}"
            continue

    return False, f"SMTP Error: {last_err}", None

def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()

def get_app_settings():
    cfg = system_db["app_config"].find_one({"_id": "global"}, {"_id": 0})
    if not cfg:
        cfg = {
            "ads_enabled": True, "ads_every_n_songs": 3,
            "ad_title": "VYBEX Premium Advertisement", "ad_banner_text": "Upgrade for Ad-Free Music!",
            "ad_audio_url": "https://luan.xyz/files/audio/ambient_c_motion.mp3", "ad_duration": 15,
            "premium_price": "₹49 / Month", "payment_upi_id": "yourname@upi",
            "payment_qr_url": "https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=upi://pay?pa=yourname@upi",
            "payment_instructions": "UPI par payment karke UTR number submit karein.",
        }
        system_db["app_config"].update_one({"_id": "global"}, {"$set": cfg}, upsert=True)
    return cfg

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "VYBEX Dynamic Cloud Backend Running!"})

# =====================================================================
# USER ENDPOINTS
# =====================================================================
@app.route("/api/send-otp", methods=["POST"])
def send_otp():
    data = request.json or {}
    receiver_email = (data.get("email") or "").strip().lower()
    if not EMAIL_RE.match(receiver_email):
        return jsonify({"success": False, "msg": "Invalid email format."}), 400

    existing_u, _ = find_user_across_dbs({"email": receiver_email})
    if existing_u:
        return jsonify({"success": False, "msg": "Account already exists. Please sign in."}), 400

    otp = str(random.randint(100000, 999999))
    system_db["otps"].update_one({"email": receiver_email}, {"$set": {"otp": otp, "expires": time.time() + 300}}, upsert=True)

    ok, msg_txt, sender_used = send_otp_with_failover(receiver_email, otp)
    if ok:
        return jsonify({"success": True, "msg": msg_txt, "sender_used": sender_used})
    return jsonify({"success": False, "msg": msg_txt}), 500

@app.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    data = request.json or {}
    email, entered_otp = (data.get("email") or "").strip().lower(), (data.get("otp") or "").strip()
    record = system_db["otps"].find_one({"email": email})
    if not record or time.time() > record.get("expires", 0) or record.get("otp") != entered_otp:
        return jsonify({"success": False, "msg": "Invalid or expired OTP."}), 400
    system_db["otps"].delete_one({"email": email})
    return jsonify({"success": True, "msg": "Verified!"})

@app.route("/api/register", methods=["POST"])
def register():
    data = request.json or {}
    name, email, password = (data.get("name") or "").strip(), (data.get("email") or "").strip().lower(), data.get("password") or ""
    if len(name) < 2 or not EMAIL_RE.match(email) or len(password) < 6:
        return jsonify({"success": False, "msg": "Invalid details."}), 400

    existing_u, _ = find_user_across_dbs({"email": email})
    if existing_u:
        return jsonify({"success": False, "msg": "Email already exists."}), 400

    target_node = get_writable_node()
    salt = os.urandom(16).hex()
    uid = hashlib.sha1(email.encode("utf-8")).hexdigest()[:12]
    target_node["db"]["users"].insert_one({
        "uid": uid, "name": name, "email": email, "salt": salt,
        "hash": hash_password(password, salt), "is_premium": False,
        "db_node": target_node["name"], "created": time.time(),
    })
    target_node["db"]["user_data"].update_one({"uid": uid}, {"$setOnInsert": {"uid": uid, "fav_songs": [], "custom_playlists": [], "history": []}}, upsert=True)
    return jsonify({"success": True, "user": {"uid": uid, "name": name, "email": email, "is_premium": False}})

@app.route("/api/login", methods=["POST"])
def login():
    data = request.json or {}
    email, password = (data.get("email") or "").strip().lower(), data.get("password") or ""
    lock_info = system_db["auth_locks"].find_one({"email": email}) or {}
    if max(0, int(lock_info.get("until", 0) - time.time())) > 0:
        return jsonify({"success": False, "msg": "Too many attempts. Try later."}), 429

    u, node = find_user_across_dbs({"email": email})
    ok = hmac.compare_digest(hash_password(password, u["salt"]), u["hash"]) if u else False
    if ok:
        system_db["auth_locks"].delete_one({"email": email})
        return jsonify({"success": True, "user": {"uid": u["uid"], "name": u["name"], "email": u["email"], "is_premium": bool(u.get("is_premium", False))}})

    fails = int(lock_info.get("fails", 0)) + 1
    until = (time.time() + LOCK_SECONDS) if fails >= LOCK_AFTER else 0
    system_db["auth_locks"].update_one({"email": email}, {"$set": {"fails": 0 if fails >= LOCK_AFTER else fails, "until": until}}, upsert=True)
    return jsonify({"success": False, "msg": "Incorrect email or password."}), 401

@app.route("/api/sync-data", methods=["POST"])
def sync_data():
    data = request.json or {}
    uid = data.get("uid")
    if not uid: return jsonify({"success": False}), 400
    _, node = find_user_across_dbs({"uid": uid})
    target_db = node["db"] if (node and get_db_usage_mb(node["db"]) < 510.0) else get_writable_node()["db"]
    update_fields = {k: data[k] for k in ("fav_songs", "custom_playlists", "history") if k in data}
    if update_fields:
        target_db["user_data"].update_one({"uid": uid}, {"$set": update_fields}, upsert=True)
    return jsonify({"success": True})

@app.route("/api/get-data", methods=["GET"])
def get_data():
    uid = request.args.get("uid")
    if not uid: return jsonify({"success": False}), 400
    nodes = get_mongo_nodes()
    for node in nodes:
        doc = node["db"]["user_data"].find_one({"uid": uid}, {"_id": 0})
        if doc: return jsonify({"success": True, "data": doc})
    return jsonify({"success": True, "data": {}})

@app.route("/api/app-config", methods=["GET"])
def user_app_config():
    uid = request.args.get("uid", "")
    u, _ = find_user_across_dbs({"uid": uid}) if uid else (None, None)
    return jsonify({"success": True, "is_premium": bool(u.get("is_premium", False)) if u else False, "config": get_app_settings()})

@app.route("/api/request-premium", methods=["POST"])
def request_premium():
    data = request.json or {}
    uid, email, utr = data.get("uid", ""), data.get("email", ""), (data.get("utr") or "").strip()
    if not uid or len(utr) < 4: return jsonify({"success": False, "msg": "Invalid UTR."}), 400
    system_db["payment_requests"].insert_one({"uid": uid, "email": email, "utr": utr, "status": "pending", "ts": time.time()})
    return jsonify({"success": True, "msg": "Payment submitted successfully!"})

# =====================================================================
# ADMIN MANAGEMENT ENDPOINTS (ADD/EDIT/DELETE DBs & GMAILs)
# =====================================================================
def check_admin(req):
    json_data = req.get_json(silent=True) or {}
    secret = req.headers.get("X-Admin-Secret") or json_data.get("admin_secret") or req.args.get("admin_secret")
    return secret == ADMIN_SECRET

@app.route("/api/admin/dashboard", methods=["GET"])
def admin_dashboard():
    if not check_admin(request): return jsonify({"success": False, "msg": "Unauthorized"}), 401
    nodes = get_mongo_nodes()
    db_clusters_info, all_users, total_used = [], [], 0.0

    for node in nodes:
        used = get_db_usage_mb(node["db"])
        total_used += used
        u_list = list(node["db"]["users"].find({}, {"_id": 0, "salt": 0, "hash": 0}))
        for u in u_list: u["db_node"] = node["name"]
        all_users.extend(u_list)
        db_clusters_info.append({"name": node["name"], "used_mb": used, "limit_mb": 512.0, "percent": round((used/512.0)*100, 2), "users_count": len(u_list)})

    pool = get_smtp_pool()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    pipeline = [{"$group": {"_id": "$sender_gmail", "total": {"$sum": 1}, "today": {"$sum": {"$cond": [{"$eq": ["$date", today_str]}, 1, 0]}}}}]
    agg_map = {x["_id"]: x for x in system_db["otp_logs"].aggregate(pipeline)}

    gmail_stats = []
    for acc in pool:
        em = acc["email"]
        st = agg_map.get(em, {"total": 0, "today": 0})
        gmail_stats.append({"email": em, "today": st["today"], "total": st["total"], "slot": acc.get("slot")})

    return jsonify({
        "success": True,
        "mongodb": {"used_mb": round(total_used, 2), "limit_mb": len(nodes)*512.0, "clusters": db_clusters_info, "total_users": len(all_users)},
        "gmail_pool": gmail_stats,
        "db_clusters_list": list(system_db["db_clusters"].find({}, {"_id": 0})),
        "config": get_app_settings(),
        "users": all_users,
        "payment_requests": list(system_db["payment_requests"].find({}, {"_id": 0}).sort("ts", -1)),
    })

# --- Admin DB Manager Endpoints ---
@app.route("/api/admin/add-db", methods=["POST"])
def admin_add_db():
    if not check_admin(request): return jsonify({"success": False}), 401
    data = request.json or {}
    name, uri = data.get("name", "").strip(), data.get("uri", "").strip()
    if not name or not uri: return jsonify({"success": False, "msg": "Name and URI required"}), 400
    system_db["db_clusters"].update_one({"name": name}, {"$set": {"name": name, "uri": uri}}, upsert=True)
    return jsonify({"success": True, "msg": f"Database {name} added!"})

@app.route("/api/admin/delete-db", methods=["POST"])
def admin_delete_db():
    if not check_admin(request): return jsonify({"success": False}), 401
    name = (request.json or {}).get("name", "")
    system_db["db_clusters"].delete_one({"name": name})
    return jsonify({"success": True, "msg": f"Database {name} removed!"})

# --- Admin Gmail Manager Endpoints ---
@app.route("/api/admin/add-gmail", methods=["POST"])
def admin_add_gmail():
    if not check_admin(request): return jsonify({"success": False}), 401
    data = request.json or {}
    email, password = data.get("email", "").strip(), data.get("password", "").replace(" ", "").strip()
    if not email or not password: return jsonify({"success": False, "msg": "Email & Password required"}), 400
    slot = f"GMAIL_{system_db['gmail_pool'].count_documents({}) + 1}"
    system_db["gmail_pool"].update_one({"email": email}, {"$set": {"slot": slot, "email": email, "password": password}}, upsert=True)
    return jsonify({"success": True, "msg": f"Gmail {email} added to pool!"})

@app.route("/api/admin/delete-gmail", methods=["POST"])
def admin_delete_gmail():
    if not check_admin(request): return jsonify({"success": False}), 401
    email = (request.json or {}).get("email", "")
    system_db["gmail_pool"].delete_one({"email": email})
    return jsonify({"success": True, "msg": f"Gmail {email} removed!"})

@app.route("/api/admin/set-premium", methods=["POST"])
def admin_set_premium():
    if not check_admin(request): return jsonify({"success": False}), 401
    data = request.json or {}
    email, is_prem = (data.get("email") or "").strip().lower(), bool(data.get("is_premium", True))
    u, node = find_user_across_dbs({"email": email})
    if not u or not node: return jsonify({"success": False, "msg": "User not found"}), 404
    node["db"]["users"].update_one({"email": email}, {"$set": {"is_premium": is_prem}})
    if data.get("utr"): system_db["payment_requests"].update_one({"utr": data["utr"]}, {"$set": {"status": "approved" if is_prem else "rejected"}})
    return jsonify({"success": True, "msg": f"Premium updated for {email}!"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
