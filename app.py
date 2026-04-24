import os
import re
import json
import logging
import threading
import datetime as dt
import urllib.error
import urllib.parse
import urllib.request

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from decimal import Decimal

from flask import (
    Flask, render_template, redirect, url_for, flash, session,
    request, send_from_directory, abort, jsonify, Response,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user, UserMixin
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import or_, and_, inspect, text
from flask import Blueprint

# Optional Stripe
try:
    import stripe  # type: ignore
except Exception:
    stripe = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Public site URL for sitemap / absolute links (override on Railway preview if needed).
CANONICAL_SITE_URL = (os.environ.get("CANONICAL_URL") or "https://freelance-forge.com").rstrip("/")
_LOG = logging.getLogger(__name__)


def _database_uri() -> str:
    """Railway sets DATABASE_URL (Postgres). Otherwise SQLite under a writable path."""
    url = (os.environ.get("DATABASE_URL") or "").strip()
    if url:
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        return url
    # Prefer legacy root site.db if present (existing installs); else instance/ (common on PaaS)
    legacy = os.path.join(BASE_DIR, "site.db")
    if os.path.isfile(legacy):
        return "sqlite:///" + legacy.replace("\\", "/")
    inst = os.path.join(BASE_DIR, "instance")
    os.makedirs(inst, exist_ok=True)
    path = os.path.join(inst, "site.db").replace("\\", "/")
    return "sqlite:///" + path


app = Flask(__name__)


@app.route("/ping")
def ping():
    """Registered first so it stays available even if later startup work misbehaves."""
    return "pong", 200


app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

# DB
_db_uri = _database_uri()
app.config["SQLALCHEMY_DATABASE_URI"] = _db_uri
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
if _db_uri.startswith("postgresql"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "connect_args": {"connect_timeout": 10},
    }
db = SQLAlchemy(app)

# DB init runs lazily (not at import) so Gunicorn can boot and /ping works even if Postgres is slow.
_db_init_lock = threading.Lock()
_db_init_done = False

# Uploads
UPLOAD_DIR = os.path.join(BASE_DIR, "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Login
login_manager = LoginManager(app)
login_manager.login_view = "login"

# Stripe env
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
try:
    STRIPE_TRIAL_DAYS = max(0, int(os.getenv("STRIPE_TRIAL_DAYS", "15")))
except ValueError:
    STRIPE_TRIAL_DAYS = 15
stripe_enabled = bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID and stripe is not None)
if stripe_enabled:
    stripe.api_key = STRIPE_SECRET_KEY

# -------------------- Models --------------------
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(160), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)  # werkzeug hash or legacy plain (migrated on login)
    display_name = db.Column(db.String(160))
    is_customer = db.Column(db.Boolean, default=True)
    is_contractor = db.Column(db.Boolean, default=False)
    subscription_active = db.Column(db.Boolean, default=False)
    stripe_customer_id = db.Column(db.String(255))
    stripe_subscription_id = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

class ContractorProfile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    display_name = db.Column(db.String(160))
    skills = db.Column(db.Text)
    bio = db.Column(db.Text)
    experience = db.Column(db.Text)
    education = db.Column(db.Text)
    portfolio_url = db.Column(db.String(255))
    location = db.Column(db.String(160))
    remote_ok = db.Column(db.Boolean, default=False)
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(120))
    is_remote = db.Column(db.Boolean, default=False)
    location = db.Column(db.String(160))
    lat = db.Column(db.Float)
    lon = db.Column(db.Float)
    status = db.Column(db.String(40), default="open")  # open, awarded, in_progress, completed, canceled
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    awarded_bid_id = db.Column(db.Integer)
    awarded_contractor_id = db.Column(db.Integer)

class Attachment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    original_name = db.Column(db.String(255))
    stored_path = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

class Bid(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    contractor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

    project = db.relationship("Project", lazy="joined", primaryjoin="Bid.project_id==Project.id")
    contractor = db.relationship("User", lazy="joined", primaryjoin="Bid.contractor_id==User.id")

class Conversation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"))
    user_a_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    user_b_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    conv_id = db.Column(db.Integer, db.ForeignKey("conversation.id"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    read = db.Column(db.Boolean, default=False)

class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    contractor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    rating = db.Column(db.Integer, nullable=False)  # 1..5
    text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)

class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    type = db.Column(db.String(50), nullable=False)  # 'award', 'message', 'status', 'bid'
    text = db.Column(db.String(300), nullable=False)
    link = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=dt.datetime.utcnow)
    read = db.Column(db.Boolean, default=False)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# -------------------- Helpers --------------------
def active_role():
    r = session.get("active_role")
    if r in ("customer", "contractor"):
        return r
    if current_user.is_authenticated:
        r = "contractor" if current_user.is_contractor else "customer"
        session["active_role"] = r
        return r
    return None


# US state names ↔ abbreviations for flexible location text matching (city vs "City, OH")
_US_STATE_PAIRS = (
    ("al", "alabama"), ("ak", "alaska"), ("az", "arizona"), ("ar", "arkansas"), ("ca", "california"),
    ("co", "colorado"), ("ct", "connecticut"), ("de", "delaware"), ("fl", "florida"), ("ga", "georgia"),
    ("hi", "hawaii"), ("id", "idaho"), ("il", "illinois"), ("in", "indiana"), ("ia", "iowa"),
    ("ks", "kansas"), ("ky", "kentucky"), ("la", "louisiana"), ("me", "maine"), ("md", "maryland"),
    ("ma", "massachusetts"), ("mi", "michigan"), ("mn", "minnesota"), ("ms", "mississippi"), ("mo", "missouri"),
    ("mt", "montana"), ("ne", "nebraska"), ("nv", "nevada"), ("nh", "new hampshire"), ("nj", "new jersey"),
    ("nm", "new mexico"), ("ny", "new york"), ("nc", "north carolina"), ("nd", "north dakota"), ("oh", "ohio"),
    ("ok", "oklahoma"), ("or", "oregon"), ("pa", "pennsylvania"), ("ri", "rhode island"), ("sc", "south carolina"),
    ("sd", "south dakota"), ("tn", "tennessee"), ("tx", "texas"), ("ut", "utah"), ("vt", "vermont"),
    ("va", "virginia"), ("wa", "washington"), ("wv", "west virginia"), ("wi", "wisconsin"), ("wy", "wyoming"),
    ("dc", "district of columbia"),
)
US_STATE_SYNS = {}
for _abbr, _name in _US_STATE_PAIRS:
    US_STATE_SYNS[_abbr] = (_abbr, _name)
    US_STATE_SYNS[_name] = (_abbr, _name)


def _state_synonyms(token: str):
    t = token.lower()
    return US_STATE_SYNS.get(t, (t,))


def _location_text_match(column, search_string: str):
    """Match location strings when wording differs (e.g. 'Alliance Ohio' vs 'Alliance, OH 44601')."""
    tokens = re.findall(r"[a-zA-Z0-9]+", (search_string or "").lower())
    if not tokens:
        return None
    parts = []
    for tok in tokens:
        if tok.isdigit() and len(tok) == 5:
            parts.append(column.ilike(f"%{tok}%"))
        else:
            syns = _state_synonyms(tok)
            parts.append(or_(*[column.ilike(f"%{s}%") for s in syns]))
    return and_(*parts)


def geocode_nominatim(query: str):
    """Resolve a US place string to coordinates (Nominatim). Used for map pins and geo filters."""
    q = (query or "").strip()
    if len(q) < 2:
        return None
    try:
        params = urllib.parse.urlencode({"format": "json", "limit": "1", "countrycodes": "us", "q": q})
        url = "https://nominatim.openstreetmap.org/search?" + params
        req = urllib.request.Request(url, headers={"User-Agent": "FreelanceForge/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
        if not data:
            return None
        first = data[0]
        return {
            "lat": float(first["lat"]),
            "lon": float(first["lon"]),
            "display_name": first.get("display_name", q),
        }
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError, TypeError, OSError):
        return None


def _geo_bbox_match(lat_col, lon_col, lat0: float, lon0: float, delta: float = 0.55):
    """Rough bounding box (~30–40 mi); pairs with text match for rows without coordinates."""
    return and_(
        lat_col.isnot(None),
        lon_col.isnot(None),
        lat_col.between(lat0 - delta, lat0 + delta),
        lon_col.between(lon0 - delta, lon0 + delta),
    )


def _password_is_hashed(stored: str) -> bool:
    return bool(stored) and (stored.startswith("pbkdf2:") or stored.startswith("scrypt:"))


def verify_user_password(user: "User", plain: str) -> bool:
    if _password_is_hashed(user.password):
        return check_password_hash(user.password, plain)
    if user.password == plain:
        user.password = generate_password_hash(plain)
        db.session.commit()
        return True
    return False


def ensure_schema():
    """Create tables; apply legacy ALTERs only for old SQLite DBs missing columns."""
    db.create_all()
    if db.engine.dialect.name != "sqlite":
        return
    insp = inspect(db.engine)
    if not insp.has_table("user"):
        return
    cols = {c["name"] for c in insp.get_columns("user")}
    with db.engine.begin() as conn:
        if "stripe_customer_id" not in cols:
            conn.execute(text("ALTER TABLE user ADD COLUMN stripe_customer_id VARCHAR(255)"))
        if "stripe_subscription_id" not in cols:
            conn.execute(text("ALTER TABLE user ADD COLUMN stripe_subscription_id VARCHAR(255)"))


def _stripe_sub_status_ok(status):
    return status in ("active", "trialing")


def _stripe_resource_id(val) -> str | None:
    """Checkout Session fields may be an id string or an expanded Stripe object."""
    if val is None:
        return None
    if isinstance(val, str):
        return val
    return getattr(val, "id", None)


def _checkout_session_user_id(sess) -> str | None:
    """Resolve our user id from Stripe Checkout Session (metadata and/or client_reference_id)."""
    md = getattr(sess, "metadata", None)
    uid = None
    if md is not None:
        try:
            if isinstance(md, dict):
                uid = md.get("user_id")
            elif hasattr(md, "get"):
                uid = md.get("user_id")
            else:
                uid = md["user_id"]
        except Exception:
            uid = None
    if uid:
        return str(uid)
    cref = getattr(sess, "client_reference_id", None)
    return str(cref) if cref else None


def _apply_checkout_session_to_user(sess) -> None:
    """Persist Stripe IDs and subscription flag from a Checkout Session (verified server-side)."""
    uid = _checkout_session_user_id(sess)
    if not uid:
        return
    user = User.query.get(int(uid))
    if not user:
        return
    cust_id = _stripe_resource_id(getattr(sess, "customer", None))
    if cust_id:
        user.stripe_customer_id = cust_id
    sub_raw = getattr(sess, "subscription", None)
    sub_id = _stripe_resource_id(sub_raw)
    if sub_id:
        user.stripe_subscription_id = sub_id
        try:
            if sub_raw is not None and not isinstance(sub_raw, str):
                user.subscription_active = _stripe_sub_status_ok(getattr(sub_raw, "status", None))
            else:
                sub = stripe.Subscription.retrieve(sub_id)
                user.subscription_active = _stripe_sub_status_ok(getattr(sub, "status", None))
        except Exception:
            user.subscription_active = sess.payment_status == "paid"
    else:
        user.subscription_active = sess.payment_status == "paid"
    db.session.commit()


def _sync_subscription_from_stripe_object(sub_obj) -> None:
    if isinstance(sub_obj, dict):
        meta = sub_obj.get("metadata") or {}
        sub_id = sub_obj.get("id")
        status = sub_obj.get("status")
        cust = sub_obj.get("customer")
    else:
        meta = sub_obj.metadata or {}
        sub_id = sub_obj.id
        status = sub_obj.status
        cust = sub_obj.customer
    uid = meta.get("user_id")
    user = User.query.get(int(uid)) if uid else None
    if not user:
        user = User.query.filter_by(stripe_subscription_id=sub_id).first()
    if not user:
        return
    if cust:
        user.stripe_customer_id = cust
    user.stripe_subscription_id = sub_id
    user.subscription_active = _stripe_sub_status_ok(status)
    db.session.commit()

PROJECT_STATUSES = ["open", "awarded", "in_progress", "completed", "canceled"]

def require_subscription_to_bid():
    if stripe_enabled and current_user.is_contractor and not current_user.subscription_active:
        flash("You need an active contractor subscription to place bids. Customers post projects for free.", "error")
        return redirect(url_for("subscribe"))
    return None

def _contractor_rating_stats(user_id: int):
    rows = Review.query.filter_by(contractor_id=user_id).all()
    if not rows:
        return (0, 0.0)
    total = len(rows)
    avg = sum(r.rating for r in rows) / total
    return (total, round(avg, 2))

@app.context_processor
def inject_nav_counts():
    unread = 0
    new_bids = 0
    notif_unread = 0
    if current_user.is_authenticated:
        convs = Conversation.query.filter(
            or_(Conversation.user_a_id == current_user.id,
                Conversation.user_b_id == current_user.id)
        ).all()
        conv_ids = [c.id for c in convs]
        if conv_ids:
            unread = Message.query.filter(
                Message.conv_id.in_(conv_ids),
                Message.sender_id != current_user.id,
                Message.read == False
            ).count()

        my_projects = Project.query.filter_by(owner_id=current_user.id).with_entities(Project.id).all()
        pids = [pid for (pid,) in my_projects] if my_projects else []
        if pids:
            since = dt.datetime.utcnow() - dt.timedelta(days=1)
            new_bids = Bid.query.filter(
                Bid.project_id.in_(pids),
                Bid.created_at >= since
            ).count()

        notif_unread = Notification.query.filter_by(user_id=current_user.id, read=False).count()

    csub = False
    if current_user.is_authenticated and stripe_enabled:
        csub = bool(
            current_user.is_contractor and not current_user.subscription_active
        )

    return dict(
        unread_messages_count=unread,
        new_bids_count=new_bids,
        notifications_unread=notif_unread,
        active_role=active_role(),
        stripe_enabled=stripe_enabled,
        contractor_needs_subscription=csub,
        stripe_trial_days=STRIPE_TRIAL_DAYS if stripe_enabled else 0,
        PROJECT_STATUSES=PROJECT_STATUSES,
    )

# -------------------- Account / Roles --------------------
account_bp = Blueprint("account", __name__)

@account_bp.route("/become-contractor", methods=["GET", "POST"])
@login_required
def become_contractor():
    if not current_user.is_contractor:
        current_user.is_contractor = True
        if not current_user.is_customer:
            current_user.is_customer = True
        prof = ContractorProfile.query.filter_by(user_id=current_user.id).first()
        if not prof:
            prof = ContractorProfile(
                user_id=current_user.id,
                display_name=current_user.display_name or current_user.email
            )
            db.session.add(prof)
        db.session.commit()

    session["active_role"] = "contractor"
    flash("Your account can now bid as a contractor. Set up your profile.", "success")
    return redirect(url_for("edit_profile"))

app.register_blueprint(account_bp)

# Account center
@app.route("/account")
@login_required
def account_center():
    return render_template("account.html", stripe_enabled=stripe_enabled)

@app.route("/account/delete", methods=["POST"])
@login_required
def account_delete():
    uid = current_user.id

    # delete conversations/messages
    convs = Conversation.query.filter(
        (Conversation.user_a_id == uid) | (Conversation.user_b_id == uid)
    ).all()
    conv_ids = [c.id for c in convs]
    if conv_ids:
        Message.query.filter(Message.conv_id.in_(conv_ids)).delete(synchronize_session=False)
        Conversation.query.filter(Conversation.id.in_(conv_ids)).delete(synchronize_session=False)

    # reviews where user involved
    Review.query.filter((Review.contractor_id == uid) | (Review.reviewer_id == uid)).delete(synchronize_session=False)

    # bids by user
    Bid.query.filter(Bid.contractor_id == uid).delete(synchronize_session=False)

    # projects owned by user (+ attachments + bids on them)
    my_projects = Project.query.filter_by(owner_id=uid).all()
    for p in my_projects:
        Attachment.query.filter_by(project_id=p.id).delete(synchronize_session=False)
        Bid.query.filter_by(project_id=p.id).delete(synchronize_session=False)
    Project.query.filter_by(owner_id=uid).delete(synchronize_session=False)

    # contractor profile
    ContractorProfile.query.filter_by(user_id=uid).delete(synchronize_session=False)

    # notifications
    Notification.query.filter_by(user_id=uid).delete(synchronize_session=False)

    # finally user
    u = User.query.get(uid)
    db.session.delete(u)
    db.session.commit()

    logout_user()
    session.pop("active_role", None)
    flash("Your account has been deleted.", "success")
    return redirect(url_for("home"))

# -------------------- Stripe (optional) --------------------
def _abs_url(path):
    base = request.url_root.rstrip("/")
    return f"{base}{path}"

@app.route("/subscribe", methods=["GET", "POST"])
@login_required
def subscribe():
    if not current_user.is_contractor:
        flash("Contractor accounts subscribe to bid on projects. Switch role or become a contractor.", "error")
        return redirect(url_for("dashboard"))
    if not stripe_enabled:
        return render_template("subscribe_not_configured.html"), 200
    if current_user.subscription_active:
        return redirect(url_for("account_center"))

    if request.method == "GET":
        return render_template("subscribe.html")

    success_url = _abs_url(url_for("subscribe_success")) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = _abs_url(url_for("subscribe_cancel"))
    meta = {"user_id": str(current_user.id)}
    sub_data = {"metadata": meta}
    if STRIPE_TRIAL_DAYS > 0:
        sub_data["trial_period_days"] = STRIPE_TRIAL_DAYS
    params = dict(
        mode="subscription",
        line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=str(current_user.id),
        metadata=meta,
        subscription_data=sub_data,
    )
    if current_user.stripe_customer_id:
        params["customer"] = current_user.stripe_customer_id
    else:
        params["customer_email"] = current_user.email

    session_obj = stripe.checkout.Session.create(**params)
    return redirect(session_obj.url, code=303)

@app.route("/billing-portal")
@login_required
def billing_portal():
    if not stripe_enabled:
        return render_template("subscribe_not_configured.html"), 200
    if not current_user.is_contractor:
        flash("Billing applies to contractor subscriptions only.", "error")
        return redirect(url_for("account_center"))

    cust_id = current_user.stripe_customer_id
    if not cust_id:
        cust_list = stripe.Customer.list(email=current_user.email, limit=1)
        cust_id = cust_list["data"][0]["id"] if cust_list and cust_list["data"] else None
        if cust_id:
            current_user.stripe_customer_id = cust_id
            db.session.commit()
    if not cust_id:
        flash("No billing profile found yet. Subscribe first, or contact support.", "error")
        return redirect(url_for("subscribe"))

    portal = stripe.billing_portal.Session.create(
        customer=cust_id,
        return_url=_abs_url(url_for("account_center")),
    )
    return redirect(portal.url, code=303)

@app.route("/subscribe/success")
@login_required
def subscribe_success():
    if stripe_enabled:
        sid = request.args.get("session_id")
        if sid:
            try:
                sess = stripe.checkout.Session.retrieve(sid, expand=["subscription"])
                if _checkout_session_user_id(sess) == str(current_user.id):
                    _apply_checkout_session_to_user(sess)
            except Exception:
                db.session.rollback()
                flash(
                    "We could not verify your checkout session on this page. "
                    "If your Account still shows “Not subscribed” after a minute, check your Stripe keys or webhook; "
                    "otherwise Stripe may still be syncing.",
                    "error",
                )
    return render_template("subscribe_success.html")

@app.route("/subscribe/cancel")
@login_required
def subscribe_cancel():
    return render_template("subscribe_cancel.html")

@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    if not (stripe_enabled and STRIPE_WEBHOOK_SECRET):
        abort(400)
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return str(e), 400

    typ = event.get("type")
    obj = event["data"]["object"]

    if typ == "checkout.session.completed":
        sess = obj
        if sess.get("mode") == "subscription" and (sess.get("metadata") or {}).get("user_id"):
            try:
                full = stripe.checkout.Session.retrieve(sess["id"])
                _apply_checkout_session_to_user(full)
            except Exception:
                uid = (sess.get("metadata") or {}).get("user_id")
                u = User.query.get(int(uid)) if uid else None
                if u:
                    if sess.get("customer"):
                        u.stripe_customer_id = sess["customer"]
                    if sess.get("subscription"):
                        u.stripe_subscription_id = sess["subscription"]
                        try:
                            sub = stripe.Subscription.retrieve(sess["subscription"])
                            u.subscription_active = _stripe_sub_status_ok(getattr(sub, "status", None))
                        except Exception:
                            u.subscription_active = True
                    else:
                        u.subscription_active = sess.get("payment_status") == "paid"
                    db.session.commit()

    elif typ in ("customer.subscription.updated", "customer.subscription.deleted"):
        _sync_subscription_from_stripe_object(obj)

    return jsonify(ok=True)

# -------------------- Auth --------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()
        want_customer = bool(request.form.get("role_customer"))
        want_contractor = bool(request.form.get("role_contractor"))

        if not email or "@" not in email:
            flash("Please enter a valid email.", "error")
            return redirect(url_for("register"))
        if User.query.filter_by(email=email).first():
            flash("Email already registered.", "error")
            return redirect(url_for("register"))

        if not (want_customer or want_contractor):
            want_customer = True

        user = User(
            email=email,
            password=generate_password_hash(password),
            display_name=display_name,
            is_customer=want_customer,
            is_contractor=want_contractor,
        )
        db.session.add(user)
        db.session.commit()

        if want_contractor:
            prof = ContractorProfile(user_id=user.id, display_name=display_name or email)
            db.session.add(prof)
            db.session.commit()

        flash("Registration successful. Please log in.", "success")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        u = User.query.filter_by(email=email).first()
        if u and verify_user_password(u, password):
            login_user(u)
            session["active_role"] = "contractor" if u.is_contractor else "customer"
            flash("Welcome back!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    session.pop("active_role", None)
    flash("Logged out.", "info")
    return redirect(url_for("home"))

# -------------------- Core pages --------------------
@app.route("/health")
def health():
    return "OK"


@app.route("/google59fc0a6306e5df48.html")
def google_site_verification():
    """Google Search Console HTML file verification (file must live in project root)."""
    return send_from_directory(BASE_DIR, "google59fc0a6306e5df48.html")


@app.route("/sitemap.xml")
def sitemap():
    """Static URL list for search engines (submit in Google Search Console)."""
    paths = (
        "/",
        "/projects",
        "/contractors",
        "/login",
        "/register",
        "/about",
        "/contact",
    )
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for path in paths:
        loc = f"{CANONICAL_SITE_URL}{path}"
        parts.append("  <url>")
        parts.append(f"    <loc>{loc}</loc>")
        parts.append("  </url>")
    parts.append("</urlset>")
    return Response("\n".join(parts) + "\n", mimetype="application/xml")


@app.route("/")
def home():
    return render_template("index.html")

@app.route("/about")
def about():
    return render_template("about.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")


# -------------------- Role switching --------------------
@app.route("/switch-role/<to>")
@login_required
def switch_role(to):
    if to not in ("customer", "contractor"):
        flash("Invalid role.", "error")
        return redirect(url_for("dashboard"))
    if to == "contractor" and not current_user.is_contractor:
        flash("You're not a contractor yet.", "error")
        return redirect(url_for("dashboard"))
    if to == "customer" and not current_user.is_customer:
        flash("You're not a customer.", "error")
        return redirect(url_for("dashboard"))
    session["active_role"] = to
    flash(f"Switched to {to.capitalize()}.", "success")
    return redirect(url_for("dashboard"))

# -------------------- Dashboard & Lists --------------------
@app.route("/dashboard")
@login_required
def dashboard():
    role = active_role()
    my_projects = []
    bids = []
    my_bid_count = 0

    if role == "customer":
        my_projects = Project.query.filter_by(owner_id=current_user.id)\
            .order_by(Project.created_at.desc()).all()
        pids = [p.id for p in my_projects]
        if pids:
            bids = Bid.query.filter(Bid.project_id.in_(pids))\
                .order_by(Bid.created_at.desc()).all()
    else:
        my_bid_count = Bid.query.filter_by(contractor_id=current_user.id).count()

    notifs = Notification.query.filter_by(user_id=current_user.id)\
        .order_by(Notification.created_at.desc()).limit(5).all()

    return render_template(
        "dashboard.html",
        role=role,
        me=current_user,
        my_projects=my_projects,
        bids=bids,
        my_bid_count=my_bid_count,
        notifications=notifs
    )

@app.route("/notifications")
@login_required
def notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
        .order_by(Notification.created_at.desc()).all()
    return render_template("notifications.html", notifications=notifs)

@app.route("/notifications/read-all", methods=["POST"])
@login_required
def notifications_read_all():
    Notification.query.filter_by(user_id=current_user.id, read=False).update({"read": True})
    db.session.commit()
    flash("Notifications marked as read.", "success")
    return redirect(url_for("notifications"))

@app.route("/my-projects")
@login_required
def my_projects():
    projs = Project.query.filter_by(owner_id=current_user.id).order_by(Project.created_at.desc()).all()
    return render_template("my_projects.html", projects=projs)

@app.route("/my-bids")
@login_required
def my_bids():
    bids = Bid.query.filter_by(contractor_id=current_user.id).order_by(Bid.created_at.desc()).all()
    return render_template("my_bids.html", bids=bids)

@app.route("/my-jobs")
@login_required
def my_jobs():
    jobs = Project.query.filter_by(awarded_contractor_id=current_user.id)\
        .order_by(Project.created_at.desc()).all()
    status = request.args.get("status", "").strip()
    if status in PROJECT_STATUSES:
        jobs = [j for j in jobs if j.status == status]
    return render_template("my_jobs.html", jobs=jobs, statuses=PROJECT_STATUSES, current=status)

# -------------------- Contractor profile --------------------
def _profile_photo_url_for(user_id: int):
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        fname = f"profile_{user_id}{ext}"
        if os.path.exists(os.path.join(UPLOAD_DIR, fname)):
            return url_for("uploads", name=fname)
    return None

@app.route("/contractor/<int:user_id>")
@login_required
def contractor_profile(user_id):
    u = User.query.get_or_404(user_id)
    prof = ContractorProfile.query.filter_by(user_id=u.id).first()
    photo_url = _profile_photo_url_for(u.id)
    from_models = Review.query.filter_by(contractor_id=u.id).order_by(Review.created_at.desc()).all()
    count = len(from_models)
    avg = round(sum(r.rating for r in from_models) / count, 2) if count else 0.0
    return render_template("contractor_profile.html", user=u, profile=prof, photo_url=photo_url,
                           reviews=from_models, avg_rating=avg, review_count=count)

@app.route("/profile/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    if not current_user.is_contractor:
        flash("Become a contractor to edit a contractor profile.", "error")
        return redirect(url_for("dashboard"))

    prof = ContractorProfile.query.filter_by(user_id=current_user.id).first()
    if not prof:
        prof = ContractorProfile(user_id=current_user.id,
                                 display_name=current_user.display_name or current_user.email)
        db.session.add(prof)
        db.session.commit()

    if request.method == "POST":
        prof.display_name = request.form.get("display_name") or current_user.display_name or current_user.email
        prof.skills = request.form.get("skills")
        prof.bio = request.form.get("bio")
        prof.experience = request.form.get("experience")
        prof.education = request.form.get("education")
        prof.portfolio_url = request.form.get("portfolio_url")
        loc = request.form.get("location")
        prof.location = loc
        prof.remote_ok = True if request.form.get("remote_ok") else False

        lat = request.form.get("lat")
        lon = request.form.get("lon")
        if loc and str(loc).strip() and not (lat and lon):
            g = geocode_nominatim(str(loc).strip())
            if g:
                lat = str(g["lat"])
                lon = str(g["lon"])
        prof.lat = float(lat) if lat else None
        prof.lon = float(lon) if lon else None

        photo = request.files.get("photo")
        if photo and photo.filename:
            ext = os.path.splitext(photo.filename)[1].lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                for old in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                    oldpath = os.path.join(UPLOAD_DIR, f"profile_{current_user.id}{old}")
                    if os.path.exists(oldpath):
                        try: os.remove(oldpath)
                        except Exception: pass
                fname = f"profile_{current_user.id}{ext}"
                save_path = os.path.join(UPLOAD_DIR, fname)
                photo.save(save_path)
                flash("Profile photo updated.", "success")
            else:
                flash("Unsupported image format. Use JPG, PNG, GIF, or WEBP.", "error")

        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("contractor_profile", user_id=current_user.id))

    photo_url = _profile_photo_url_for(current_user.id)
    return render_template("edit_profile.html", profile=prof, photo_url=photo_url)

# -------------------- Projects --------------------
CATEGORIES = [
    "General Contracting", "Remodeling", "Painting", "Plumbing", "Electrical",
    "Landscaping", "HVAC", "CAD/Design", "3D Printing", "Machining", "Welding/Fabrication",
    "Prototyping", "Assembly", "Consulting"
]

@app.route("/projects")
@login_required
def view_projects():
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    category = request.args.get("category", "").strip()
    remote_ok = request.args.get("remote_ok")
    projects = Project.query.filter(Project.status == "open")

    if q:
        like = f"%{q}%"
        projects = projects.filter(or_(Project.title.ilike(like), Project.description.ilike(like)))
    if category:
        projects = projects.filter(Project.category == category)
    search_lat = search_lon = None
    search_label = None
    if location:
        geo = geocode_nominatim(location)
        text_match = _location_text_match(Project.location, location)
        if geo:
            search_lat, search_lon = geo["lat"], geo["lon"]
            search_label = geo["display_name"]
            geo_match = _geo_bbox_match(Project.lat, Project.lon, geo["lat"], geo["lon"])
            if text_match is not None:
                projects = projects.filter(or_(geo_match, text_match))
            else:
                projects = projects.filter(geo_match)
        elif text_match is not None:
            projects = projects.filter(text_match)
    if remote_ok:
        projects = projects.filter(Project.is_remote == True)

    projects = projects.order_by(Project.created_at.desc()).all()
    return render_template(
        "projects.html",
        projects=projects,
        q=q,
        location=location,
        category=category,
        categories=CATEGORIES,
        remote_ok=bool(remote_ok),
        search_lat=search_lat,
        search_lon=search_lon,
        search_label=search_label,
    )

@app.route("/project/post", methods=["GET", "POST"])
@login_required
def post_project():
    if active_role() != "customer":
        flash("Switch to customer role to post a project.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "")
        category = request.form.get("category") or "General Contracting"
        is_remote = True if request.form.get("is_remote") else False
        location = request.form.get("location", "").strip()
        lat = request.form.get("lat")
        lon = request.form.get("lon")
        if location and not (lat and lon):
            g = geocode_nominatim(location)
            if g:
                lat = str(g["lat"])
                lon = str(g["lon"])

        if not title:
            flash("Title is required.", "error")
            return redirect(url_for("post_project"))

        p = Project(
            owner_id=current_user.id,
            title=title,
            description=description,
            category=category,
            is_remote=is_remote,
            location=location or None,
            lat=float(lat) if lat else None,
            lon=float(lon) if lon else None,
            status="open"
        )
        db.session.add(p)
        db.session.commit()

        files = request.files.getlist("attachments")
        for f in files:
            if not f or not f.filename:
                continue
            fname = secure_filename(f.filename)
            stored = os.path.join(UPLOAD_DIR, f"{p.id}_{fname}")
            f.save(stored)
            rel_path = os.path.join("uploads", f"{p.id}_{fname}")
            att = Attachment(project_id=p.id, original_name=fname, stored_path=rel_path)
            db.session.add(att)
        db.session.commit()

        flash("Project posted.", "success")
        return redirect(url_for("project_detail", project_id=p.id))

    return render_template("post_project.html", categories=CATEGORIES)

@app.route("/project/<int:project_id>", methods=["GET", "POST"])
@login_required
def project_detail(project_id):
    project = Project.query.get_or_404(project_id)
    is_owner = (project.owner_id == current_user.id)
    is_awarded_contractor = (current_user.is_authenticated and
                             project.awarded_contractor_id == current_user.id)

    if request.method == "POST":
        # Bids only when open & not by owner
        if project.status != "open" or project.awarded_contractor_id:
            flash("Bidding is closed on this project.", "error")
            return redirect(url_for("project_detail", project_id=project_id))

        if is_owner:
            flash("You cannot bid on your own project.", "error")
            return redirect(url_for("project_detail", project_id=project_id))

        if active_role() != "contractor" or not current_user.is_contractor:
            flash("Switch to contractor role to bid.", "error")
            return redirect(url_for("project_detail", project_id=project_id))

        stop = require_subscription_to_bid()
        if stop:
            return stop

        try:
            price_val = Decimal(request.form.get("price"))
        except Exception:
            flash("Enter a valid price.", "error")
            return redirect(url_for("project_detail", project_id=project_id))
        message = request.form.get("message", "").strip()

        b = Bid(project_id=project.id, contractor_id=current_user.id, price=price_val, message=message)
        db.session.add(b)
        db.session.commit()

        # Notify owner about the bid
        owner = User.query.get(project.owner_id)
        note = Notification(
            user_id=owner.id,
            type="bid",
            text=f"{current_user.display_name or current_user.email} placed a bid (${price_val:.2f}) on “{project.title}”.",
            link=url_for("project_detail", project_id=project.id)
        )
        db.session.add(note)
        db.session.commit()

        flash("Bid submitted.", "success")
        return redirect(url_for("project_detail", project_id=project_id))

    bids = Bid.query.filter_by(project_id=project.id).order_by(Bid.created_at.desc()).all()
    atts = Attachment.query.filter_by(project_id=project.id).all()

    existing_review = None
    if current_user.is_authenticated and is_owner:
        existing_review = Review.query.filter_by(
            project_id=project.id,
            reviewer_id=current_user.id
        ).first()

    role = active_role()
    can_bid = (
        role == "contractor"
        and current_user.is_contractor
        and not is_owner
        and project.status == "open"
        and not project.awarded_contractor_id
    )
    needs_sub_to_bid = bool(stripe_enabled and current_user.is_contractor and not current_user.subscription_active)

    return render_template(
        "project_detail.html",
        project=project,
        bids=bids,
        attachments=atts,
        is_owner=is_owner,
        is_awarded_contractor=is_awarded_contractor,
        active_role=role,
        existing_review=existing_review,
        can_bid=can_bid,
        needs_sub_to_bid=needs_sub_to_bid,
    )

@app.route("/project/<int:project_id>/accept/<int:bid_id>", methods=["POST"])
@login_required
def accept_bid(project_id, bid_id):
    project = Project.query.get_or_404(project_id)
    if project.owner_id != current_user.id:
        abort(403)
    bid = Bid.query.get_or_404(bid_id)
    if bid.project_id != project.id:
        abort(400)

    project.awarded_bid_id = bid.id
    project.awarded_contractor_id = bid.contractor_id
    project.status = "awarded"
    db.session.commit()

    owner = User.query.get(project.owner_id)
    note = Notification(
        user_id=bid.contractor_id,
        type="award",
        text=f"You have a new job awarded by {owner.display_name or owner.email}: “{project.title}”.",
        link=url_for("project_detail", project_id=project.id)
    )
    db.session.add(note)
    db.session.commit()

    flash("Bid accepted. Project awarded.", "success")
    return redirect(url_for("project_detail", project_id=project.id))

@app.route("/project/<int:project_id>/status", methods=["POST"])
@login_required
def update_project_status(project_id):
    project = Project.query.get_or_404(project_id)

    new_status = request.form.get("status")
    if new_status not in PROJECT_STATUSES:
        abort(400)

    if project.awarded_contractor_id:
        if current_user.id != project.awarded_contractor_id:
            abort(403)
        if new_status not in ("in_progress", "completed", "canceled"):
            flash("After award, contractor may set In Progress / Completed / Canceled.", "error")
            return redirect(url_for("project_detail", project_id=project.id))
        project.status = new_status
        db.session.commit()

        owner = User.query.get(project.owner_id)
        note = Notification(
            user_id=owner.id,
            type="status",
            text=f"Your job “{project.title}” is now {new_status.replace('_',' ').title()}.",
            link=url_for("project_detail", project_id=project.id)
        )
        db.session.add(note)
        db.session.commit()

        flash("Status updated.", "success")
        return redirect(url_for("project_detail", project_id=project.id))
    else:
        if project.owner_id != current_user.id:
            abort(403)
        if new_status not in ("open", "canceled"):
            flash("Before award, owner may set Open / Canceled.", "error")
            return redirect(url_for("project_detail", project_id=project.id))
        project.status = new_status
        db.session.commit()
        flash("Status updated.", "success")
        return redirect(url_for("project_detail", project_id=project.id))

@app.route("/project/<int:project_id>/review", methods=["POST"])
@login_required
def submit_review(project_id):
    project = Project.query.get_or_404(project_id)
    if project.owner_id != current_user.id:
        abort(403)
    if project.status != "completed" or not project.awarded_contractor_id:
        flash("You can only review a contractor after marking the project Completed.", "error")
        return redirect(url_for("project_detail", project_id=project.id))

    existing = Review.query.filter_by(project_id=project.id, reviewer_id=current_user.id).first()
    if existing:
        flash("You already left a review for this project.", "error")
        return redirect(url_for("project_detail", project_id=project.id))

    try:
        rating = int(request.form.get("rating", ""))
    except Exception:
        rating = 0
    text = (request.form.get("text") or "").strip()
    if rating < 1 or rating > 5:
        flash("Please select a rating between 1 and 5.", "error")
        return redirect(url_for("project_detail", project_id=project.id))

    rev = Review(
        project_id=project.id,
        contractor_id=project.awarded_contractor_id,
        reviewer_id=current_user.id,
        rating=rating,
        text=text
    )
    db.session.add(rev)
    db.session.commit()
    flash("Thanks! Your review has been posted.", "success")
    return redirect(url_for("contractor_profile", user_id=project.awarded_contractor_id))

# -------------------- Contractors directory --------------------
@app.route("/contractors")
@login_required
def contractors():
    q = request.args.get("q", "").strip()
    location = request.args.get("location", "").strip()
    radius = request.args.get("radius", "").strip()
    remote_ok = bool(request.args.get("remote_ok"))

    query = db.session.query(User, ContractorProfile).join(
        ContractorProfile, ContractorProfile.user_id == User.id, isouter=True
    ).filter(User.is_contractor == True)

    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            User.display_name.ilike(like),
            User.email.ilike(like),
            ContractorProfile.skills.ilike(like),
            ContractorProfile.bio.ilike(like)
        ))
    search_lat = search_lon = None
    search_label = None
    if location:
        geo = geocode_nominatim(location)
        text_match = _location_text_match(ContractorProfile.location, location)
        if geo:
            search_lat, search_lon = geo["lat"], geo["lon"]
            search_label = geo["display_name"]
            geo_match = _geo_bbox_match(ContractorProfile.lat, ContractorProfile.lon, geo["lat"], geo["lon"])
            if text_match is not None:
                query = query.filter(or_(geo_match, text_match))
            else:
                query = query.filter(geo_match)
        elif text_match is not None:
            query = query.filter(text_match)
    if remote_ok:
        query = query.filter(or_(ContractorProfile.remote_ok == True, ContractorProfile.remote_ok.is_(None)))

    rows = query.all()
    contractors = []
    for u, prof in rows:
        u.profile = prof
        cnt, avg = _contractor_rating_stats(u.id)
        u._rating_count = cnt
        u._rating_avg = avg
        contractors.append(u)

    return render_template(
        "contractors.html",
        contractors=contractors,
        q=q,
        location=location,
        radius=radius,
        remote_ok=remote_ok,
        search_lat=search_lat,
        search_lon=search_lon,
        search_label=search_label,
    )

# -------------------- Messaging --------------------
def _get_or_create_conversation(user1_id, user2_id, project_id=None):
    """One logical thread per user pair; reuse the row with the most recent activity."""
    candidates = Conversation.query.filter(
        or_(
            and_(Conversation.user_a_id == user1_id, Conversation.user_b_id == user2_id),
            and_(Conversation.user_a_id == user2_id, Conversation.user_b_id == user1_id),
        )
    ).all()
    if candidates:
        best = None
        best_ts = None
        for c in candidates:
            last = Message.query.filter_by(conv_id=c.id).order_by(Message.created_at.desc()).first()
            ts = last.created_at if last else c.created_at
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best = c
        return best
    c = Conversation(user_a_id=user1_id, user_b_id=user2_id, project_id=project_id)
    db.session.add(c)
    db.session.commit()
    return c

@app.route("/inbox")
@login_required
def inbox():
    convs = Conversation.query.filter(
        or_(Conversation.user_a_id == current_user.id, Conversation.user_b_id == current_user.id)
    ).order_by(Conversation.created_at.desc()).all()

    by_other = {}
    for c in convs:
        other_id = c.user_b_id if c.user_a_id == current_user.id else c.user_a_id
        other = User.query.get(other_id)
        last_msg = Message.query.filter_by(conv_id=c.id).order_by(Message.created_at.desc()).first()
        sort_ts = last_msg.created_at if last_msg else c.created_at
        if other_id not in by_other:
            by_other[other_id] = dict(conv=c, other=other, last=last_msg, _sort=sort_ts)
        else:
            prev = by_other[other_id]
            if sort_ts > prev["_sort"]:
                by_other[other_id] = dict(conv=c, other=other, last=last_msg, _sort=sort_ts)
    items = sorted(by_other.values(), key=lambda x: x["_sort"], reverse=True)
    for it in items:
        it.pop("_sort", None)
    return render_template("inbox.html", conversations=items)

@app.route("/conversation/<int:conv_id>", methods=["GET", "POST"])
@login_required
def conversation(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    if current_user.id not in (conv.user_a_id, conv.user_b_id):
        abort(403)
    other_id = conv.user_b_id if conv.user_a_id == current_user.id else conv.user_a_id
    other = User.query.get_or_404(other_id)

    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if body:
            m = Message(conv_id=conv.id, sender_id=current_user.id, body=body)
            db.session.add(m)
            db.session.commit()
            flash("Message sent.", "success")
            return redirect(url_for("conversation", conv_id=conv.id))

    db.session.query(Message).filter(
        Message.conv_id == conv.id,
        Message.sender_id != current_user.id,
        Message.read == False
    ).update({"read": True})
    db.session.commit()

    msgs = Message.query.filter_by(conv_id=conv.id).order_by(Message.created_at.asc()).all()
    return render_template("conversation.html", conv=conv, messages=msgs, other=other)

@app.route("/message/<int:user_id>/start")
@login_required
def start_conversation(user_id):
    if user_id == current_user.id:
        abort(400)
    conv = _get_or_create_conversation(current_user.id, user_id, None)
    return redirect(url_for("conversation", conv_id=conv.id))

@app.route("/message/start")
@login_required
def start_conversation_qs():
    pid = request.args.get("project_id", type=int)
    contractor_id = request.args.get("contractor_id", type=int)
    if not pid:
        abort(400)
    project = Project.query.get_or_404(pid)
    if current_user.id == project.owner_id:
        if not contractor_id:
            abort(400)
        other_id = contractor_id
    else:
        other_id = project.owner_id
    if other_id == current_user.id or other_id is None:
        abort(400)
    conv = _get_or_create_conversation(current_user.id, other_id, pid)
    return redirect(url_for("conversation", conv_id=conv.id))

# -------------------- Uploads --------------------
@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name)

# -------------------- Run --------------------
def init_database():
    """Create tables once per process. Not run at import — avoids blocking Gunicorn worker boot."""
    global _db_init_done
    with _db_init_lock:
        if _db_init_done:
            return
        try:
            with app.app_context():
                ensure_schema()
        except Exception:
            import traceback

            print("DB INIT FAILED:", flush=True)
            traceback.print_exc()
            _LOG.exception("Database initialization failed (app will still load; fix DB config)")
        finally:
            _db_init_done = True


@app.before_request
def _lazy_init_database():
    """Skip for health probes so /ping and /health never wait on Postgres."""
    # Use path, not endpoint — endpoint can be unset in some phases and would wrongly run DB init on /ping.
    path = (request.path or "").rstrip("/") or "/"
    if path in ("/ping", "/health"):
        return
    if request.endpoint in ("ping", "health"):
        return
    if os.environ.get("SKIP_DB_INIT", "").strip().lower() in ("1", "true", "yes"):
        return
    init_database()


print("Flask app import finished (DB init deferred until first non-ping request)", flush=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # Railway and other hosts inject PORT; bind all interfaces for containers.
    app.run(
        host="0.0.0.0",
        port=port,
        debug=os.environ.get("FLASK_DEBUG") == "1",
        use_reloader=False,
    )
