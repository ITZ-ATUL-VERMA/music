from flask import Flask, request, jsonify, Response
from dotenv import load_dotenv
from pymongo import MongoClient
import gridfs
from bson.objectid import ObjectId
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
PHONE_RE = re.compile(r"^\d{10}$")
PBKDF2_ROUNDS = 150_000
LOCK_AFTER = 5
LOCK_SECONDS = 60

DB_SAFE_LIMIT_MB = 490.0
GMAIL_DAILY_LIMIT = 485

PRIMARY_URI = os.getenv("MONGO_URI", "").strip()
if not PRIMARY_URI:
    raise ValueError("MONGO_URI missing in .env or Render Environment!")

primary_client = MongoClient(PRIMARY_URI, serverSelectionTimeoutMS=5000)
system_db = primary_client["vybex_system_db"]
fs = gridfs.GridFS(system_db)


def normalize_phone(p):
    digits = re.sub(r"\D", "", str(p or ""))
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def mask_email(em):
    if "@" not in str(em):
        return em
    u, dom = em.split("@", 1)
    if len(u) <= 2:
        return f"{u[0]}***@{dom}"
    return f"{u[:2]}***{u[-1]}@{dom}"


def get_mongo_nodes():
    saved_dbs = list(system_db["db_clusters"].find({}, {"_id": 0}))
    if not saved_dbs:
        saved_dbs = [{"name": "DB_1", "uri": PRIMARY_URI}]
        system_db["db_clusters"].insert_one(saved_dbs[0])

    nodes = []
    for item in saved_dbs:
        try:
            cli = MongoClient(item["uri"], serverSelectionTimeoutMS=4000)
            nodes.append({"name": item["name"], "client": cli, "db": cli["vybex_music_db"]})
        except Exception as e:
            print(f"Cluster {item['name']} error: {e}")
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


def find_user_by_identifier(ident):
    ident = (ident or "").strip().lower()
    if "@" in ident:
        return find_user_across_dbs({"email": ident})
    phone = normalize_phone(ident)
    if PHONE_RE.match(phone):
        return find_user_across_dbs({"phone": phone})
    return None, None


def get_smtp_pool():
    saved_gmails = list(system_db["gmail_pool"].find({}, {"_id": 0}))
    if not saved_gmails:
        e0 = os.getenv("SMTP_EMAIL", "").strip().replace("@email.com", "@gmail.com")
        p0 = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
        if e0 and p0:
            saved_gmails = [{"slot": "GMAIL_1", "email": e0, "password": p0}]
            system_db["gmail_pool"].insert_one(saved_gmails[0])
    return saved_gmails


def get_ad_pool():
    ads = list(system_db["ad_pool"].find({}, {"_id": 0}))
    if not ads:
        cfg = system_db["app_config"].find_one({"_id": "global"}, {"_id": 0}) or {}
        default_url = cfg.get("ad_video_url") or "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4"
        default_ad = {
            "ad_id": "AD_1",
            "title": cfg.get("ad_title", "VYBEX Sponsored Video"),
            "banner_text": cfg.get("ad_banner_text", "Upgrade to VYBEX Premium for 100% Ad-Free Music!"),
            "video_url": default_url,
            "duration": int(cfg.get("ad_duration", 15)),
            "file_id": "",
            "created": time.time(),
        }
        system_db["ad_pool"].insert_one(dict(default_ad))
        ads = [default_ad]
    return ads


def get_today_otp_count(sender_email):
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    return system_db["otp_logs"].count_documents({
        "sender_gmail": sender_email,
        "date": today_str,
        "status": "sent",
    })


def send_otp_with_failover(receiver_email, otp, purpose="Verification"):
    pool = get_smtp_pool()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    if not pool:
        system_db["otp_logs"].insert_one({
            "sender_gmail": "DEMO_MODE", "receiver_email": receiver_email,
            "date": today_str, "ts": time.time(), "status": "demo",
        })
        return True, "OTP Generated!", "DEMO_MODE", otp

    for acc in pool:
        sender = acc["email"].replace("@email.com", "@gmail.com")
        if get_today_otp_count(sender) >= GMAIL_DAILY_LIMIT:
            continue

        msg = EmailMessage()
        msg["Subject"] = f"{otp} is your VYBEX {purpose} OTP"
        msg["From"] = f"VYBEX Music <{sender}>"
        msg["To"] = receiver_email
        msg.set_content(f"Your VYBEX {purpose} OTP is: {otp}. Valid for 5 minutes.")
        msg.add_alternative(f"""
        <div style="font-family: Arial, sans-serif; background: #071A16; color: white; padding: 30px; border-radius: 18px; text-align: center; max-width: 420px; margin: auto; border: 1.5px solid #00E676;">
            <h2 style="color: #00E676; margin-bottom: 5px; letter-spacing: 2px;">🎵 VYBEX MUSIC</h2>
            <p style="color: #A7B8C0; font-size: 14px;">{purpose} Code for your VYBEX Account</p>
            <div style="background: #102129; padding: 16px; border-radius: 14px; margin: 22px 0; letter-spacing: 8px; font-size: 30px; font-weight: bold; color: #00E5FF; border: 1px solid #00E5FF;">
                {otp}
            </div>
            <p style="color: #808080; font-size: 12px;">Valid for 5 minutes. Do not share this code.</p>
        </div>
        """, subtype="html")

        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=6) as server:
                server.login(sender, acc["password"])
                server.send_message(msg)

            system_db["otp_logs"].insert_one({
                "sender_gmail": sender, "receiver_email": receiver_email,
                "date": today_str, "ts": time.time(), "status": "sent",
            })
            return True, f"OTP sent to {mask_email(receiver_email)}!", sender, None
        except Exception:
            continue

    return True, f"OTP ready for {mask_email(receiver_email)}!", "CLIENT_RELAY", otp


def hash_password(password, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()


def get_app_settings():
    cfg = system_db["app_config"].find_one({"_id": "global"}, {"_id": 0})
    if not cfg:
        cfg = {
            "ads_enabled": True,
            "ads_every_n_minutes": 5,
            "ad_duration": 15,
            "ad_title": "VYBEX Sponsored Video",
            "ad_banner_text": "Upgrade to VYBEX Premium for Ad-Free Music!",
            "ad_video_url": "https://commondatastorage.googleapis.com/gtv-videos-bucket/sample/ForBiggerBlazes.mp4",
            "premium_price": "₹49 / Month",
            "payment_upi_id": "yourname@upi",
            "payment_qr_url": "https://api.qrserver.com/v1/create-qr-code/?size=250x250&data=upi://pay?pa=yourname@upi",
            "payment_instructions": "UPI par payment karke UTR number submit karein.",
        }
        system_db["app_config"].update_one({"_id": "global"}, {"$set": cfg}, upsert=True)
    cfg["ads_pool"] = get_ad_pool()
    return cfg


@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "VYBEX Dynamic Cloud Backend Running!"})


@app.route("/api/ad-video/<file_id>", methods=["GET"])
def stream_ad_video(file_id):
    try:
        grid_out = fs.get(ObjectId(file_id))
        data = grid_out.read()
        content_type = grid_out.content_type or "video/mp4"
        return Response(
            data,
            mimetype=content_type,
            headers={
                "Content-Length": str(len(data)),
                "Accept-Ranges": "bytes",
                "Cache-Control": "public, max-age=86400",
            },
        )
    except Exception as e:
        return jsonify({"success": False, "msg": f"Video not found: {e}"}), 404


# =====================================================================
# AUTHENTICATION & USER ENDPOINTS
# =====================================================================
@app.route("/api/send-otp", methods=["POST"])
def send_otp():
    data = request.get_json(silent=True) or {}
    receiver_email = (data.get("email") or "").strip().lower().replace("@email.com", "@gmail.com")
    phone = normalize_phone(data.get("phone") or "")

    if not EMAIL_RE.match(receiver_email):
        return jsonify({"success": False, "msg": "Please enter a valid Gmail address."}), 400
    if not PHONE_RE.match(phone):
        return jsonify({"success": False, "msg": "Please enter a valid 10-digit phone number."}), 400

    if find_user_across_dbs({"email": receiver_email})[0]:
        return jsonify({"success": False, "msg": "This Gmail is already registered! Please Sign In."}), 400
    if find_user_across_dbs({"phone": phone})[0]:
        return jsonify({"success": False, "msg": "This Phone Number is already linked with another Gmail!"}), 400

    otp = str(random.randint(100000, 999999))
    system_db["otps"].update_one(
        {"email": receiver_email},
        {"$set": {"otp": otp, "phone": phone, "verified": False, "expires": time.time() + 300}},
        upsert=True,
    )

    ok, msg_txt, sender_used, relay_otp = send_otp_with_failover(receiver_email, otp, "Signup")
    if ok:
        return jsonify({"success": True, "msg": msg_txt, "sender_used": sender_used, "relay_otp": relay_otp})
    return jsonify({"success": False, "msg": msg_txt}), 500


@app.route("/api/verify-otp", methods=["POST"])
def verify_otp():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower().replace("@email.com", "@gmail.com")
    phone = normalize_phone(data.get("phone") or "")
    entered_otp = (data.get("otp") or "").strip()

    record = system_db["otps"].find_one({"email": email})
    if not record or time.time() > record.get("expires", 0) or record.get("otp") != entered_otp:
        return jsonify({"success": False, "msg": "Invalid or expired OTP!"}), 400
    if phone and record.get("phone") and record.get("phone") != phone:
        return jsonify({"success": False, "msg": "Phone number mismatch with OTP request!"}), 400

    system_db["otps"].update_one({"email": email}, {"$set": {"verified": True, "expires": time.time() + 600}})
    return jsonify({"success": True, "msg": "OTP Verified! Now create your password."})


@app.route("/api/register", methods=["POST"])
def register():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower().replace("@email.com", "@gmail.com")
    phone = normalize_phone(data.get("phone") or "")
    password = data.get("password") or ""

    if len(name) < 2 or not EMAIL_RE.match(email) or not PHONE_RE.match(phone) or len(password) < 6:
        return jsonify({"success": False, "msg": "Invalid registration details."}), 400

    if find_user_across_dbs({"email": email})[0]:
        return jsonify({"success": False, "msg": "This Gmail is already registered."}), 400
    if find_user_across_dbs({"phone": phone})[0]:
        return jsonify({"success": False, "msg": "This Phone Number is already linked to another Gmail."}), 400

    system_db["otps"].delete_one({"email": email})

    target_node = get_writable_node()
    salt = os.urandom(16).hex()
    uid = hashlib.sha1(email.encode("utf-8")).hexdigest()[:12]
    target_node["db"]["users"].insert_one({
        "uid": uid, "name": name, "email": email, "phone": phone,
        "salt": salt, "hash": hash_password(password, salt),
        "is_premium": False, "db_node": target_node["name"], "created": time.time(),
    })
    target_node["db"]["user_data"].update_one(
        {"uid": uid},
        {"$setOnInsert": {"uid": uid, "fav_songs": [], "custom_playlists": [], "history": []}},
        upsert=True,
    )
    return jsonify({"success": True, "user": {"uid": uid, "name": name, "email": email, "phone": phone, "is_premium": False}})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or data.get("email") or "").strip().lower().replace("@email.com", "@gmail.com")
    password = data.get("password") or ""

    lock_info = system_db["auth_locks"].find_one({"email": identifier}) or {}
    left = max(0, int(lock_info.get("until", 0) - time.time()))
    if left > 0:
        return jsonify({"success": False, "msg": f"Too many attempts. Try again in {left}s."}), 429

    u, node = find_user_by_identifier(identifier)
    if not u:
        return jsonify({"success": False, "msg": "No account found with this Gmail or Phone Number."}), 404

    ok = hmac.compare_digest(hash_password(password, u["salt"]), u["hash"])
    if ok:
        system_db["auth_locks"].delete_one({"email": identifier})
        return jsonify({
            "success": True,
            "user": {
                "uid": u["uid"], "name": u["name"], "email": u["email"],
                "phone": u.get("phone", ""), "is_premium": bool(u.get("is_premium", False)),
            },
        })

    fails = int(lock_info.get("fails", 0)) + 1
    until = (time.time() + LOCK_SECONDS) if fails >= LOCK_AFTER else 0
    system_db["auth_locks"].update_one(
        {"email": identifier},
        {"$set": {"fails": 0 if fails >= LOCK_AFTER else fails, "until": until}},
        upsert=True,
    )
    return jsonify({
        "success": False, "wrong_password": True,
        "msg": "Incorrect password! Click 'Forgot Password?' below to reset.",
    }), 401


@app.route("/api/forgot-send-otp", methods=["POST"])
def forgot_send_otp():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip().lower().replace("@email.com", "@gmail.com")
    if not identifier:
        return jsonify({"success": False, "msg": "Enter your registered Gmail or Phone Number."}), 400

    u, _ = find_user_by_identifier(identifier)
    if not u:
        return jsonify({"success": False, "msg": "No account found with this Gmail or Phone Number!"}), 404

    linked_email = u["email"]
    otp = str(random.randint(100000, 999999))
    system_db["reset_otps"].update_one(
        {"email": linked_email},
        {"$set": {"otp": otp, "verified": False, "expires": time.time() + 300}},
        upsert=True,
    )

    ok, _, sender_used, relay_otp = send_otp_with_failover(linked_email, otp, "Password Reset")
    masked = mask_email(linked_email)
    return jsonify({
        "success": True, "email": linked_email, "masked_email": masked,
        "msg": f"OTP sent to your linked Gmail ({masked})!",
        "sender_used": sender_used, "relay_otp": relay_otp,
    })


@app.route("/api/forgot-verify-otp", methods=["POST"])
def forgot_verify_otp():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip().lower().replace("@email.com", "@gmail.com")
    entered_otp = (data.get("otp") or "").strip()

    u, _ = find_user_by_identifier(identifier)
    if not u:
        return jsonify({"success": False, "msg": "Account not found."}), 404

    linked_email = u["email"]
    rec = system_db["reset_otps"].find_one({"email": linked_email})
    if not rec or time.time() > rec.get("expires", 0) or rec.get("otp") != entered_otp:
        return jsonify({"success": False, "msg": "Invalid or expired OTP!"}), 400

    system_db["reset_otps"].update_one({"email": linked_email}, {"$set": {"verified": True, "expires": time.time() + 600}})
    return jsonify({"success": True, "email": linked_email, "msg": "OTP Verified! Enter your new password."})


@app.route("/api/forgot-reset-password", methods=["POST"])
def forgot_reset_password():
    data = request.get_json(silent=True) or {}
    identifier = (data.get("identifier") or "").strip().lower().replace("@email.com", "@gmail.com")
    new_password = data.get("new_password") or ""

    if len(new_password) < 6:
        return jsonify({"success": False, "msg": "Password must be at least 6 characters."}), 400

    u, node = find_user_by_identifier(identifier)
    if not u or not node:
        return jsonify({"success": False, "msg": "Account not found."}), 404

    linked_email = u["email"]
    salt = os.urandom(16).hex()
    node["db"]["users"].update_one(
        {"uid": u["uid"]},
        {"$set": {"salt": salt, "hash": hash_password(new_password, salt)}},
    )
    system_db["reset_otps"].delete_one({"email": linked_email})
    system_db["auth_locks"].delete_one({"email": identifier})
    system_db["auth_locks"].delete_one({"email": linked_email})
    return jsonify({"success": True, "msg": "Password updated successfully! Please Sign In."})


@app.route("/api/sync-data", methods=["POST"])
def sync_data():
    data = request.get_json(silent=True) or {}
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
    return jsonify({
        "success": True,
        "is_premium": bool(u.get("is_premium", False)) if u else False,
        "config": get_app_settings(),
        "ads_pool": get_ad_pool(),
    })


@app.route("/api/request-premium", methods=["POST"])
def request_premium():
    data = request.get_json(silent=True) or {}
    uid, email, utr = data.get("uid", ""), data.get("email", ""), (data.get("utr") or "").strip()
    if not uid or len(utr) < 4: return jsonify({"success": False, "msg": "Invalid UTR."}), 400
    system_db["payment_requests"].insert_one({"uid": uid, "email": email, "utr": utr, "status": "pending", "ts": time.time()})
    return jsonify({"success": True, "msg": "Payment submitted successfully!"})


# =====================================================================
# ADMIN MANAGEMENT ENDPOINTS (MULTIPLE DBS, GMAILS, & MULTIPLE ADS)
# =====================================================================
def check_admin(req):
    json_data = req.get_json(silent=True) or {}
    secret = (
        req.headers.get("X-Admin-Secret")
        or req.form.get("admin_secret")
        or json_data.get("admin_secret")
        or req.args.get("admin_secret")
    )
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
        "ads_pool": get_ad_pool(),
        "db_clusters_list": list(system_db["db_clusters"].find({}, {"_id": 0})),
        "config": get_app_settings(),
        "users": all_users,
        "payment_requests": list(system_db["payment_requests"].find({}, {"_id": 0}).sort("ts", -1)),
    })


@app.route("/api/admin/upload-ad-video", methods=["POST"])
def admin_upload_ad_video():
    if not check_admin(request):
        return jsonify({"success": False, "msg": "Unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"success": False, "msg": "No video file sent"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"success": False, "msg": "Empty filename"}), 400

    file_id = fs.put(
        f.read(),
        filename=f.filename,
        content_type=f.content_type or "video/mp4",
        meta_type="ad_video",
    )
    base_url = request.host_url.rstrip("/").replace("http://", "https://")
    video_url = f"{base_url}/api/ad-video/{file_id}"
    return jsonify({
        "success": True,
        "video_url": video_url,
        "file_id": str(file_id),
        "msg": "Video Uploaded! Now click '+ Add to Ad Pool'.",
    })


@app.route("/api/admin/add-ad", methods=["POST"])
def admin_add_ad():
    if not check_admin(request):
        return jsonify({"success": False, "msg": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "VYBEX Sponsored Ad").strip()
    banner_text = (data.get("banner_text") or "Upgrade to VYBEX Premium for Ad-Free Music!").strip()
    video_url = (data.get("video_url") or "").strip()
    file_id = (data.get("file_id") or "").strip()
    try:
        duration = max(5, int(data.get("duration") or 15))
    except ValueError:
        duration = 15

    if not video_url:
        return jsonify({"success": False, "msg": "Video URL or uploaded MP4 required!"}), 400

    ad_id = f"AD_{int(time.time() * 1000) % 1000000}"
    ad_doc = {
        "ad_id": ad_id,
        "title": title,
        "banner_text": banner_text,
        "video_url": video_url,
        "duration": duration,
        "file_id": file_id,
        "created": time.time(),
    }
    system_db["ad_pool"].insert_one(dict(ad_doc))
    return jsonify({"success": True, "ad": ad_doc, "msg": f"Ad '{title}' added to rotation pool!"})


@app.route("/api/admin/delete-ad", methods=["POST"])
def admin_delete_ad():
    if not check_admin(request):
        return jsonify({"success": False, "msg": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    ad_id = (data.get("ad_id") or "").strip()
    doc = system_db["ad_pool"].find_one({"ad_id": ad_id})
    if doc:
        fid = doc.get("file_id")
        if fid:
            try:
                fs.delete(ObjectId(fid))
            except Exception:
                pass
        elif "/api/ad-video/" in str(doc.get("video_url", "")):
            try:
                extracted_id = str(doc["video_url"]).rsplit("/api/ad-video/", 1)[-1].split("?")[0]
                fs.delete(ObjectId(extracted_id))
            except Exception:
                pass
        system_db["ad_pool"].delete_one({"ad_id": ad_id})
    return jsonify({"success": True, "msg": f"Ad {ad_id} deleted!"})


@app.route("/api/admin/update-config", methods=["POST"])
def admin_update_config():
    if not check_admin(request):
        return jsonify({"success": False, "msg": "Unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    cfg_fields = [
        "ads_enabled", "ads_every_n_minutes", "ads_every_n_songs", "ad_duration",
        "ad_title", "ad_banner_text", "ad_video_url", "ad_audio_url",
        "premium_price", "payment_upi_id", "payment_qr_url",
    ]
    update_doc = {k: data[k] for k in cfg_fields if k in data}
    if update_doc:
        system_db["app_config"].update_one({"_id": "global"}, {"$set": update_doc}, upsert=True)
    return jsonify({"success": True, "msg": "Settings Updated!"})


@app.route("/api/admin/add-db", methods=["POST"])
def admin_add_db():
    if not check_admin(request): return jsonify({"success": False}), 401
    data = request.get_json(silent=True) or {}
    name, uri = data.get("name", "").strip(), data.get("uri", "").strip()
    if not name or not uri: return jsonify({"success": False, "msg": "Name and URI required"}), 400
    system_db["db_clusters"].update_one({"name": name}, {"$set": {"name": name, "uri": uri}}, upsert=True)
    return jsonify({"success": True, "msg": f"Database {name} added!"})


@app.route("/api/admin/delete-db", methods=["POST"])
def admin_delete_db():
    if not check_admin(request): return jsonify({"success": False}), 401
    name = (request.get_json(silent=True) or {}).get("name", "")
    system_db["db_clusters"].delete_one({"name": name})
    return jsonify({"success": True, "msg": f"Database {name} removed!"})


@app.route("/api/admin/add-gmail", methods=["POST"])
def admin_add_gmail():
    if not check_admin(request): return jsonify({"success": False}), 401
    data = request.get_json(silent=True) or {}
    email = data.get("email", "").strip().lower().replace("@email.com", "@gmail.com")
    password = data.get("password", "").replace(" ", "").strip()
    if not email or not password: return jsonify({"success": False, "msg": "Email & Password required"}), 400
    slot = f"GMAIL_{system_db['gmail_pool'].count_documents({}) + 1}"
    system_db["gmail_pool"].update_one({"email": email}, {"$set": {"slot": slot, "email": email, "password": password}}, upsert=True)
    return jsonify({"success": True, "msg": f"Gmail {email} added to pool!"})


@app.route("/api/admin/delete-gmail", methods=["POST"])
def admin_delete_gmail():
    if not check_admin(request): return jsonify({"success": False}), 401
    email = (request.get_json(silent=True) or {}).get("email", "")
    system_db["gmail_pool"].delete_one({"email": email})
    return jsonify({"success": True, "msg": f"Gmail {email} removed!"})


@app.route("/api/admin/set-premium", methods=["POST"])
def admin_set_premium():
    if not check_admin(request): return jsonify({"success": False}), 401
    data = request.get_json(silent=True) or {}
    email, is_prem = (data.get("email") or "").strip().lower(), bool(data.get("is_premium", True))
    u, node = find_user_across_dbs({"email": email})
    if not u or not node: return jsonify({"success": False, "msg": "User not found"}), 404
    node["db"]["users"].update_one({"email": email}, {"$set": {"is_premium": is_prem}})
    if data.get("utr"): system_db["payment_requests"].update_one({"utr": data["utr"]}, {"$set": {"status": "approved" if is_prem else "rejected"}})
    return jsonify({"success": True, "msg": f"Premium updated for {email}!"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
