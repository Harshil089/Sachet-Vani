from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session, Response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import uuid
from datetime import datetime, timedelta
import requests
from PIL import Image
import json
import math
import hashlib
import copy
from collections import defaultdict, Counter
import statistics
from sqlalchemy import inspect
from sqlalchemy.orm import selectinload
import cloudinary
import cloudinary.uploader
import cloudinary.api
from cloudinary.utils import cloudinary_url
import threading
import time
from functools import lru_cache
try:
    import redis
except Exception:
    redis = None

from config import Config

# Import messaging utilities for Telegram/Discord alerts
try:
    from utils.messaging import send_telegram_alert, send_discord_alert, broadcast_alert
    MESSAGING_AVAILABLE = True
except ImportError:
    MESSAGING_AVAILABLE = False
    print("⚠️ Messaging utilities not available")

# Initialize Flask app
app = Flask(__name__)
app.config.from_object(Config)

# Get the base directory
basedir = os.path.abspath(os.path.dirname(__file__))

# Ensure upload directories exist for local development
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'photos'), exist_ok=True)
os.makedirs(os.path.join(app.config['UPLOAD_FOLDER'], 'audio'), exist_ok=True)

# Create error templates directory
os.makedirs(os.path.join(basedir, 'templates', 'errors'), exist_ok=True)

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'admin_login'

# In-memory tracking for failed admin login attempts and lockouts
FAILED_ADMIN_LOGINS = {}
ML_CASE_CACHE = {}
_ml_case_cache_lock = threading.Lock()
_ML_CACHE_MAX_ENTRIES = 500
_ML_CACHE_TTL_SECONDS = int(os.environ.get('ML_CACHE_TTL_SECONDS', '86400'))
_redis_client = None
IS_SERVERLESS_ENV = bool(os.environ.get('RENDER') or os.environ.get('VERCEL'))


def _get_redis_client():
    """Create and memoize Redis client for shared cache (optional)."""
    global _redis_client

    if _redis_client is not None:
        return _redis_client

    redis_url = os.environ.get('REDIS_URL')
    if not redis_url or redis is None:
        return None

    try:
        _redis_client = redis.from_url(redis_url, decode_responses=True, socket_timeout=2)
        _redis_client.ping()
        print("✅ Redis cache connected")
        return _redis_client
    except Exception as e:
        print(f"⚠️ Redis unavailable, using local cache fallback: {e}")
        _redis_client = None
        return None


def _ml_cache_key(report_id):
    return f"ml_case_cache:{report_id}"

def _get_client_ip():
    try:
        # Only trust X-Forwarded-For if we are in a trusted proxy environment (e.g. Render)
        if os.environ.get('RENDER'):
            forwarded_for = request.headers.get('X-Forwarded-For', '')
            if forwarded_for:
                return forwarded_for.split(',')[0].strip()
    except Exception:
        pass
    return request.remote_addr or 'unknown'

def _is_locked_out(key: str) -> bool:
    record = FAILED_ADMIN_LOGINS.get(key)
    if not record:
        return False
    lock_until = record.get('lock_until')
    if lock_until and datetime.utcnow() < lock_until:
        return True
    # Expire lock if past time
    if lock_until and datetime.utcnow() >= lock_until:
        FAILED_ADMIN_LOGINS.pop(key, None)
    return False

def _register_failed_attempt(key: str):
    settings_max = app.config.get('ADMIN_MAX_FAILED_ATTEMPTS', 5)
    lock_minutes = app.config.get('ADMIN_LOCKOUT_MINUTES', 15)
    record = FAILED_ADMIN_LOGINS.get(key, {'count': 0, 'lock_until': None})
    record['count'] = record.get('count', 0) + 1
    if record['count'] >= int(settings_max):
        record['lock_until'] = datetime.utcnow() + timedelta(minutes=int(lock_minutes))
    FAILED_ADMIN_LOGINS[key] = record

def _reset_failed_attempts(key: str):
    FAILED_ADMIN_LOGINS.pop(key, None)

# Return 404 instead of redirect when unauthorized, to hide admin routes
@login_manager.unauthorized_handler
def handle_unauthorized():
    if request.path.startswith('/admin'):
        return render_template('errors/404.html'), 404
    return render_template('errors/404.html'), 404

@app.before_request
def enforce_admin_scope_logout_on_public_pages():
    try:
        path = request.path or ''
        # If logged in and navigating explicitly to the public home page, log out
        if current_user.is_authenticated:
            # Consider both root path and the named endpoint for index
            if path == '/' or request.endpoint == 'index':
                logout_user()
                return None
    except Exception:
        # Fail closed is not necessary here; allow request to proceed
        return None

@app.before_request
def guard_admin_routes():
    try:
        path = request.path or ''
        if path.startswith('/admin'):
            # Allow reaching the login endpoint (token gate will still apply inside)
            if request.endpoint == 'admin_login':
                return None
            if not current_user.is_authenticated:
                return render_template('errors/404.html'), 404
    except Exception:
        # On any error, fail closed
        return render_template('errors/404.html'), 404

# Initialize Cloudinary
def init_cloudinary():
    # Prefer single-URL configuration if provided (e.g., CLOUDINARY_URL=cloudinary://api_key:api_secret@cloud_name)
    cloudinary_url_env = os.environ.get('CLOUDINARY_URL')
    if cloudinary_url_env:
        try:
            cloudinary.config(
                cloudinary_url=cloudinary_url_env,
                secure=True
            )
            print("✅ Cloudinary configured via CLOUDINARY_URL")
            return True
        except Exception as e:
            print(f"⚠️ Failed to configure Cloudinary via CLOUDINARY_URL: {str(e)}")

    cloud_name = app.config.get('CLOUDINARY_CLOUD_NAME')
    api_key = app.config.get('CLOUDINARY_API_KEY')
    api_secret = app.config.get('CLOUDINARY_API_SECRET')
    if cloud_name and api_key and api_secret:
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True
        )
        print("✅ Cloudinary configured successfully")
        return True
    else:
        missing = []
        if not cloud_name:
            missing.append('CLOUDINARY_CLOUD_NAME')
        if not api_key:
            missing.append('CLOUDINARY_API_KEY')
        if not api_secret:
            missing.append('CLOUDINARY_API_SECRET')
        print(f"⚠️ Cloudinary not fully configured (missing: {', '.join(missing)}) - using local storage")
        return False

CLOUDINARY_ENABLED = init_cloudinary()

# PREDEFINED DEMO PHONE NUMBERS (Replace with your verified Twilio numbers)
DEMO_PHONE_NUMBERS = [
    '+919960846194',
    '‭+917758926422‬',
    '+919920846982',
    '+919370831887',
    '+917387350049',
    '+917028826639'
]

# Area-wise keyword filters and number routing
# Keys are canonical area ids, values are keyword lists to match in free-text locations
AREA_KEYWORDS = {
    'magarpatta': ['magarpatta'],
    'mit_loni': ['mit loni', 'loni', 'mit'],
    'pcmc': ['pcmc', 'pimpri', 'chinchwad', 'pimpri-chinchwad'],
    'koregaon_park': ['koregaon park', 'kp'],
    'seasons_mall': ["season's mall", 'seasons mall', 'season mall', 'seasons'],
    'pune_airport': ['pune airport', 'lohegaon', 'lohgaon', 'pnq']
}

# Assign one phone number per area in declaration order using DEMO_PHONE_NUMBERS
def build_area_number_mapping():
    area_ids = list(AREA_KEYWORDS.keys())
    mapping = {}
    for idx, area_id in enumerate(area_ids):
        try:
            mapping[area_id] = DEMO_PHONE_NUMBERS[idx]
        except IndexError:
            # Fallback: if fewer numbers than areas, reuse the first number
            mapping[area_id] = DEMO_PHONE_NUMBERS[0] if DEMO_PHONE_NUMBERS else None
    return mapping

AREA_TO_NUMBER = build_area_number_mapping()

LOCATION_TYPE_KEYWORDS = [
    ('Parks/Playgrounds', ('park', 'playground')),
    ('Educational', ('school', 'university')),
    ('Commercial', ('mall', 'store', 'shop')),
    ('Residential', ('home', 'house', 'residence')),
]

RISK_ZONE_RADIUS_KM = 2.0
RISK_ZONE_BUCKET_DEG = 0.02

CITY_CENTERS = {
    'Mumbai': (19.0761, 72.8775),
    'Pune': (18.5203, 73.8567),
    'Nagpur': (21.1497, 79.0806),
    'Nashik': (19.9975, 73.7898),
}

def select_numbers_for_location(location_text):
    """Return a list of phone numbers to alert based on free-text location.

    Matching is case-insensitive and uses simple substring checks against
    configured AREA_KEYWORDS. If multiple areas match, all corresponding
    numbers are returned (de-duplicated). If none match, returns the default
    DEMO_PHONE_NUMBERS list.
    """
    if not location_text:
        return DEMO_PHONE_NUMBERS

    location_lower = location_text.strip().lower()
    selected_numbers = []

    for area_id, keywords in AREA_KEYWORDS.items():
        if any(keyword in location_lower for keyword in keywords):
            # Special-case: demo needs MIT Loni to broadcast to all numbers
            if area_id == 'mit_loni':
                return DEMO_PHONE_NUMBERS
            number = AREA_TO_NUMBER.get(area_id)
            if number:
                selected_numbers.append(number)

    # De-duplicate while preserving order
    seen = set()
    filtered = []
    for n in selected_numbers:
        if n not in seen:
            seen.add(n)
            filtered.append(n)

    return filtered if filtered else DEMO_PHONE_NUMBERS


def _build_case_input_from_child(missing_child, haversine_fn=None):
    case_input = {
        'child_age': missing_child.age or 0,
        'child_gender': missing_child.gender or 'M',
        'latitude': missing_child.last_seen_lat or 0,
        'longitude': missing_child.last_seen_lng or 0,
        'abduction_time': missing_child.abduction_time if missing_child.abduction_time is not None else 12.0,
        'abductor_relation': missing_child.abductor_relation or 'stranger',
        'region_type': missing_child.region_type or 'Urban',
        'population_density': missing_child.population_density or 5000,
        'missing_date': missing_child.missing_date,
    }

    try:
        lat = float(case_input.get('latitude', 0))
        lon = float(case_input.get('longitude', 0))
        if haversine_fn and CITY_CENTERS:
            case_input['dist_to_nearest_city'] = min(
                [haversine_fn(lat, lon, c_lat, c_lon) for c_lat, c_lon in CITY_CENTERS.values()]
            )
        else:
            case_input['dist_to_nearest_city'] = 0
    except Exception:
        case_input['dist_to_nearest_city'] = 0

    return case_input


def _build_sighting_dicts(sightings):
    sighting_dicts = []
    for s in sightings:
        sighting_dicts.append({
            'lat': s.latitude or 0,
            'lon': s.longitude or 0,
            'hours_since': (datetime.utcnow() - s.sighting_time).total_seconds() / 3600,
            'direction_text': s.description or ''
        })
    return sighting_dicts


def _build_ml_cache_signature(missing_child, sightings):
    """Build a deterministic signature for ML inputs per case for cache reuse."""
    child_payload = {
        'report_id': missing_child.report_id,
        'age': missing_child.age,
        'gender': missing_child.gender,
        'last_seen_lat': missing_child.last_seen_lat,
        'last_seen_lng': missing_child.last_seen_lng,
        'abduction_time': missing_child.abduction_time,
        'abductor_relation': missing_child.abductor_relation,
        'region_type': missing_child.region_type,
        'population_density': missing_child.population_density,
        'missing_date': missing_child.missing_date.isoformat() if missing_child.missing_date else None,
    }

    sightings_payload = []
    for s in sightings:
        sightings_payload.append({
            'id': s.id,
            'lat': s.latitude,
            'lon': s.longitude,
            'description': s.description,
            'sighting_time': s.sighting_time.isoformat() if s.sighting_time else None,
        })

    payload = {
        'ml_logic_version': 2,
        'child': child_payload,
        'sightings': sightings_payload,
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha1(raw).hexdigest()


def _evict_one_ml_cache_entry_if_needed():
    if len(ML_CASE_CACHE) < _ML_CACHE_MAX_ENTRIES:
        return
    oldest_key = min(ML_CASE_CACHE, key=lambda k: ML_CASE_CACHE[k].get('cached_at', 0))
    ML_CASE_CACHE.pop(oldest_key, None)


def _invalidate_case_ml_cache(report_id):
    if not report_id:
        return

    redis_client = _get_redis_client()
    if redis_client:
        try:
            redis_client.delete(_ml_cache_key(report_id))
        except Exception as e:
            print(f"⚠️ Redis cache delete failed for {report_id}: {e}")

    with _ml_case_cache_lock:
        ML_CASE_CACHE.pop(report_id, None)


def _get_cached_ml_outputs(report_id, signature):
    redis_client = _get_redis_client()
    if redis_client:
        try:
            raw = redis_client.get(_ml_cache_key(report_id))
            if raw:
                entry = json.loads(raw)
                if entry.get('signature') == signature:
                    return (
                        entry.get('ml_prediction'),
                        entry.get('ml_refined'),
                        entry.get('ml_status'),
                    )
        except Exception as e:
            print(f"⚠️ Redis cache read failed for {report_id}: {e}")

    with _ml_case_cache_lock:
        entry = ML_CASE_CACHE.get(report_id)
        if not entry:
            return None
        if entry.get('signature') != signature:
            return None
        return (
            copy.deepcopy(entry.get('ml_prediction')),
            copy.deepcopy(entry.get('ml_refined')),
            copy.deepcopy(entry.get('ml_status')),
        )


def _store_cached_ml_outputs(report_id, signature, ml_prediction, ml_refined, ml_status):
    entry_payload = {
        'signature': signature,
        'ml_prediction': copy.deepcopy(ml_prediction),
        'ml_refined': copy.deepcopy(ml_refined),
        'ml_status': copy.deepcopy(ml_status),
        'cached_at': time.time(),
    }

    redis_client = _get_redis_client()
    if redis_client:
        try:
            redis_client.setex(
                _ml_cache_key(report_id),
                _ML_CACHE_TTL_SECONDS,
                json.dumps(entry_payload, default=str)
            )
        except Exception as e:
            print(f"⚠️ Redis cache write failed for {report_id}: {e}")

    with _ml_case_cache_lock:
        _evict_one_ml_cache_entry_if_needed()
        ML_CASE_CACHE[report_id] = entry_payload


def _build_known_location_candidates(missing_child, sightings):
    """Collect known labeled map points from case and sightings for fallback naming."""
    candidates = []

    if missing_child and missing_child.last_seen_lat is not None and missing_child.last_seen_lng is not None:
        label = (missing_child.last_seen_location or '').strip() or 'Last Seen Location'
        candidates.append((float(missing_child.last_seen_lat), float(missing_child.last_seen_lng), label))

    for s in sightings or []:
        if s.latitude is None or s.longitude is None:
            continue
        label = (s.location or '').strip()
        if not label:
            continue
        candidates.append((float(s.latitude), float(s.longitude), label))

    return candidates


def _nearest_known_location_label(lat, lng, candidates):
    """Return a nearest labeled place string like 'Near <name> (~x km)' if possible."""
    if lat is None or lng is None or not candidates:
        return None

    nearest = None
    for c_lat, c_lng, c_label in candidates:
        try:
            dist_km = calculate_distance(float(lat), float(lng), float(c_lat), float(c_lng))
            if nearest is None or dist_km < nearest[0]:
                nearest = (dist_km, c_label)
        except Exception:
            continue

    if not nearest:
        return None

    dist_km, label = nearest
    return f"Near {label} (~{dist_km:.1f} km)"


def _attach_location_names_to_ml_outputs(ml_prediction, ml_refined, missing_child=None, sightings=None):
    """Attach human-readable location names to ML outputs for UI summary/popups."""
    fallback_candidates = _build_known_location_candidates(missing_child, sightings)

    if ml_prediction and ml_prediction.get('predicted_latitude') is not None and ml_prediction.get('predicted_longitude') is not None:
        predicted_name = get_location_name_from_coordinates(
            ml_prediction.get('predicted_latitude'),
            ml_prediction.get('predicted_longitude')
        )
        if not predicted_name:
            predicted_name = _nearest_known_location_label(
                ml_prediction.get('predicted_latitude'),
                ml_prediction.get('predicted_longitude'),
                fallback_candidates
            )
        if predicted_name:
            ml_prediction['predicted_location_name'] = predicted_name

    if ml_refined and ml_refined.get('lat') is not None and ml_refined.get('lon') is not None:
        refined_name = get_location_name_from_coordinates(ml_refined.get('lat'), ml_refined.get('lon'))
        if not refined_name:
            refined_name = _nearest_known_location_label(
                ml_refined.get('lat'),
                ml_refined.get('lon'),
                fallback_candidates
            )
        if refined_name:
            ml_refined['location_name'] = refined_name

    return ml_prediction, ml_refined


def _compute_case_ml_outputs(missing_child, sightings, context_label='case', use_cache=True, force_refresh=False):
    ml_prediction = None
    ml_refined = None
    ml_status = {'available': False, 'message': ''}

    if IS_SERVERLESS_ENV:
        msg = f"ML skipped on serverless for {context_label} {missing_child.report_id} (memory constraints)"
        print(msg)
        ml_status['message'] = 'ML is unavailable in this deployment environment.'
        return ml_prediction, ml_refined, ml_status

    cache_signature = _build_ml_cache_signature(missing_child, sightings)
    if use_cache and not force_refresh:
        cached = _get_cached_ml_outputs(missing_child.report_id, cache_signature)
        if cached:
            cached_prediction, cached_refined, cached_status = cached
            if isinstance(cached_status, dict):
                cached_status['from_cache'] = True
            return cached_prediction, cached_refined, cached_status

    try:
        from predictor import predict_initial_case, refine_location_with_sightings, haversine

        case_input = _build_case_input_from_child(missing_child, haversine_fn=haversine)
        sighting_dicts = _build_sighting_dicts(sightings)

        ml_prediction = predict_initial_case(case_input)
        if sighting_dicts:
            rlat, rlon = refine_location_with_sightings(ml_prediction, sighting_dicts, case_input)
            ml_refined = {'lat': rlat, 'lon': rlon}

        ml_prediction, ml_refined = _attach_location_names_to_ml_outputs(
            ml_prediction,
            ml_refined,
            missing_child=missing_child,
            sightings=sightings
        )

        ml_status['available'] = True
        ml_status['from_cache'] = False
        if use_cache and missing_child.report_id:
            _store_cached_ml_outputs(missing_child.report_id, cache_signature, ml_prediction, ml_refined, ml_status)
        return ml_prediction, ml_refined, ml_status
    except Exception as e:
        print(f"ML integration skipped for {context_label} {missing_child.report_id}: {e}")
        ml_status['message'] = str(e)
        return ml_prediction, ml_refined, ml_status

# Telegram Bot directly
import requests

def send_telegram_broadcast(message):
    token = app.config.get('TELEGRAM_BOT_TOKEN')
    chat_id = app.config.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("❌ Telegram credentials not configured")
        return 0

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        print("✅ Telegram alert sent")
        return 1
    except Exception as e:
        print(f"❌ Telegram send error: {str(e)}")
        return 0

def send_telegram_photo(photo_url, caption):
    """Send a photo with caption via Telegram sendPhoto API."""
    token = app.config.get('TELEGRAM_BOT_TOKEN')
    chat_id = app.config.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print("❌ Telegram credentials not configured")
        return 0

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    payload = {
        'chat_id': chat_id,
        'photo': photo_url,
        'caption': caption,
        'parse_mode': 'HTML'
    }
    try:
        response = requests.post(url, json=payload, timeout=15)
        response.raise_for_status()
        print("✅ Telegram photo alert sent")
        return 1
    except Exception as e:
        print(f"❌ Telegram photo send error: {str(e)}, falling back to text")
        return send_telegram_broadcast(caption)

# Database Models
class MissingChild(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.String(100), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    age = db.Column(db.Integer, nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    last_seen_location = db.Column(db.String(200), nullable=False)
    location_subcategory = db.Column(db.String(200))
    last_seen_lat = db.Column(db.Float)
    last_seen_lng = db.Column(db.Float)
    description = db.Column(db.Text, nullable=False)
    photo_filename = db.Column(db.String(500))  # Increased length for URLs
    audio_filename = db.Column(db.String(500))  # Increased length for URLs
    emergency_contact = db.Column(db.String(100))  # Emergency contact phone/email
    date_reported = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='missing')
    
    # ML Features
    abduction_time = db.Column(db.Float, default=12.0) # Hour of day (0-24)
    abductor_relation = db.Column(db.String(50), default='stranger')
    region_type = db.Column(db.String(50), default='Urban')
    population_density = db.Column(db.Integer, default=5000)
    missing_date = db.Column(db.Date) # Date the child went missing
    
    sightings = db.relationship('Sighting', backref='missing_child', lazy=True)

class Sighting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.String(100), db.ForeignKey('missing_child.report_id'), nullable=False)
    location = db.Column(db.String(200), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text)
    reporter_phone = db.Column(db.String(20))
    photo_filename = db.Column(db.String(500))  # Optional photo proof for sighting
    sighting_time = db.Column(db.DateTime, default=datetime.utcnow)
    face_match_score = db.Column(db.Float, nullable=True)  # 0-100 score from face comparison

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)

class RiskZone(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    zone_name = db.Column(db.String(200), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    radius_km = db.Column(db.Float, default=1.0)
    risk_score = db.Column(db.Float, default=0.0)
    incident_count = db.Column(db.Integer, default=0)
    last_updated = db.Column(db.DateTime, default=datetime.utcnow)
    is_active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.zone_name,
            'lat': self.latitude,
            'lng': self.longitude,
            'radius': self.radius_km * 1000, # Convert to meters for Leaflet
            'score': self.risk_score,
            'incident_count': self.incident_count
        }

class Analytics(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    analysis_type = db.Column(db.String(50), nullable=False)
    analysis_data = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    insights = db.Column(db.Text)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Utility Functions
def allowed_file(filename, allowed_extensions):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed_extensions

def upload_to_cloudinary(file, folder, public_id):
    """Upload file to Cloudinary"""
    try:
        if not CLOUDINARY_ENABLED:
            return None
            
        result = cloudinary.uploader.upload(
            file,
            folder=folder,
            public_id=public_id,
            overwrite=True,
            resource_type="auto",
            transformation=[
                {'width': 800, 'height': 800, 'crop': 'limit', 'quality': 'auto:good'}
            ] if folder == 'missing_children/photos' else None
        )
        return result['secure_url']
    except Exception as e:
        print(f"Cloudinary upload error: {str(e)}")
        return None

def save_file_locally(file, folder, filename):
    """Save file to local storage with proper organization"""
    try:
        # Create folder if it doesn't exist
        folder_path = os.path.join(app.config['UPLOAD_FOLDER'], folder)
        os.makedirs(folder_path, exist_ok=True)
        
        # Generate unique filename
        file_path = os.path.join(folder_path, filename)
        
        # Save file
        file.save(file_path)
        
        # For images, optimize them
        if folder == 'photos' and file.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
            try:
                with Image.open(file_path) as img:
                    # Convert to RGB if necessary
                    if img.mode in ('RGBA', 'LA', 'P'):
                        img = img.convert('RGB')
                    
                    # Resize if too large
                    img.thumbnail((800, 800), Image.Resampling.LANCZOS)

                    # Save as optimized JPEG and update filename to .jpg
                    base_name, _ext = os.path.splitext(filename)
                    optimized_filename = base_name + '.jpg'
                    optimized_path = os.path.join(folder_path, optimized_filename)
                    img.save(optimized_path, 'JPEG', quality=85, optimize=True)

                    # Remove original if different
                    if optimized_path != file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception:
                            pass

                    filename = optimized_filename
            except Exception as img_error:
                print(f"Image optimization error: {str(img_error)}")
        
        # Return just the filename for static file access
        return filename
    except Exception as e:
        print(f"Local file save error: {str(e)}")
        return None

def upload_audio_to_cloudinary(file, public_id):
    """Upload audio file to Cloudinary"""
    try:
        if not CLOUDINARY_ENABLED:
            return None
            
        result = cloudinary.uploader.upload(
            file,
            folder='missing_children/audio',
            public_id=public_id,
            overwrite=True,
            resource_type="video"  # Cloudinary uses 'video' for audio files
        )
        return result['secure_url']
    except Exception as e:
        print(f"Cloudinary audio upload error: {str(e)}")
        return None

# Rate limiting for Nominatim API
_last_geocode_request = 0
_geocode_lock = threading.Lock()

def _geocode_with_google_maps(location_name):
    """Geocode using Google Maps API (optional, requires API key)"""
    google_api_key = app.config.get('GOOGLE_MAPS_API_KEY') or os.environ.get('GOOGLE_MAPS_API_KEY')

    if not google_api_key:
        return None, None

    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            'address': location_name,
            'key': google_api_key
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data['status'] == 'OK' and data.get('results'):
            location = data['results'][0]['geometry']['location']
            lat = location['lat']
            lng = location['lng']
            print(f"✅ Geocoded '{location_name}' to: {lat}, {lng} (Google Maps)")
            return lat, lng
        else:
            print(f"⚠️ Google Maps: No results for '{location_name}' (status: {data.get('status')})")
            return None, None

    except Exception as e:
        print(f"❌ Google Maps geocoding error: {str(e)}")
        return None, None

def _geocode_with_nominatim(location_name):
    """Geocode using Nominatim API with rate limiting"""
    global _last_geocode_request

    try:
        # Rate limiting: ensure at least 1 second between requests
        with _geocode_lock:
            time_since_last = time.time() - _last_geocode_request
            if time_since_last < 1.0:
                time.sleep(1.0 - time_since_last)
            _last_geocode_request = time.time()

        location_encoded = location_name.strip().replace(' ', '+')
        url = f"https://nominatim.openstreetmap.org/search?format=json&q={location_encoded}&limit=1"

        headers = {
            'User-Agent': 'Sachet-ChildSafety/1.0 (https://sachet.onrender.com)'
        }

        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()

        if data and len(data) > 0:
            lat = float(data[0]['lat'])
            lng = float(data[0]['lon'])
            print(f"✅ Geocoded '{location_name}' to: {lat}, {lng} (Nominatim)")
            return lat, lng
        else:
            print(f"⚠️ Nominatim: No results for '{location_name}'")
            return None, None

    except requests.exceptions.Timeout:
        print(f"❌ Nominatim timeout for '{location_name}'")
        return None, None
    except Exception as e:
        print(f"❌ Nominatim error for '{location_name}': {str(e)}")
        return None, None

@lru_cache(maxsize=100)  # Cache 100 most recent locations
def get_location_coordinates(location_name):
    """Get coordinates from location name using Google Maps (if configured) or Nominatim"""
    if not location_name:
        return None, None

    # Try Google Maps first if API key is configured (more reliable)
    lat, lng = _geocode_with_google_maps(location_name)
    if lat and lng:
        return lat, lng

    # Fallback to Nominatim (free but rate-limited)
    return _geocode_with_nominatim(location_name)


def _reverse_geocode_with_google_maps(lat, lng):
    """Reverse geocode coordinates with Google Maps API if configured."""
    google_api_key = app.config.get('GOOGLE_MAPS_API_KEY') or os.environ.get('GOOGLE_MAPS_API_KEY')
    if not google_api_key:
        return None

    try:
        url = "https://maps.googleapis.com/maps/api/geocode/json"
        params = {
            'latlng': f'{lat},{lng}',
            'key': google_api_key
        }
        response = requests.get(url, params=params, timeout=8)
        response.raise_for_status()
        data = response.json()
        if data.get('status') == 'OK' and data.get('results'):
            return data['results'][0].get('formatted_address')
    except Exception as e:
        print(f"❌ Google reverse geocoding error for ({lat}, {lng}): {str(e)}")
    return None


def _reverse_geocode_with_nominatim(lat, lng):
    """Reverse geocode coordinates with Nominatim (rate limited)."""
    global _last_geocode_request

    try:
        with _geocode_lock:
            time_since_last = time.time() - _last_geocode_request
            if time_since_last < 1.0:
                time.sleep(1.0 - time_since_last)
            _last_geocode_request = time.time()

        url = (
            "https://nominatim.openstreetmap.org/reverse"
            f"?format=jsonv2&lat={lat}&lon={lng}&zoom=16&addressdetails=1"
        )
        headers = {
            'User-Agent': 'Sachet-ChildSafety/1.0 (https://sachet.onrender.com)'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        return data.get('display_name')
    except requests.exceptions.Timeout:
        print(f"❌ Nominatim reverse geocoding timeout for ({lat}, {lng})")
    except Exception as e:
        print(f"❌ Nominatim reverse geocoding error for ({lat}, {lng}): {str(e)}")
    return None


@lru_cache(maxsize=512)
def _reverse_geocode_cached(lat_key, lng_key):
    """Cached reverse geocoding by normalized coordinate keys."""
    try:
        lat = float(lat_key)
        lng = float(lng_key)
    except Exception:
        return None

    location_name = _reverse_geocode_with_google_maps(lat, lng)
    if location_name:
        return location_name

    return _reverse_geocode_with_nominatim(lat, lng)


def get_location_name_from_coordinates(lat, lng):
    """Resolve coordinates to a location name using cached reverse geocoding."""
    if lat is None or lng is None:
        return None

    try:
        lat_key = f"{float(lat):.5f}"
        lng_key = f"{float(lng):.5f}"
    except Exception:
        return None

    return _reverse_geocode_cached(lat_key, lng_key)

def send_sms_alert(message):
    """Fallback function name for existing code, routes to Telegram"""
    return send_telegram_broadcast(message)

def send_sms_alert_to_numbers(message, phone_numbers):
    """Fallback function name for existing code, routes to Telegram"""
    return send_telegram_broadcast(message)

def broadcast_all_alerts(message, phone_numbers=None, photo_url=None):
    """
    Broadcast alert to Telegram
    """
    results = {'sms': 0, 'telegram': False, 'discord': False}
    
    # Send via Telegram
    success = send_telegram_broadcast(message)
    results['telegram'] = bool(success)
    
    # Log summary
    print(f"📢 Alert broadcast: {'1' if success else '0'}/1 channels successful")
    
    return results

# Analytics Functions
def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate distance between two points in kilometers"""
    if not all([lat1, lon1, lat2, lon2]):
        return float('inf')
    
    R = 6371  # Earth's radius in kilometers
    
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    
    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    return R * c


def _risk_bucket(lat, lng):
    return (math.floor(lat / RISK_ZONE_BUCKET_DEG), math.floor(lng / RISK_ZONE_BUCKET_DEG))

def analyze_risk_zones():
    """Analyze historical data to identify high-risk zones"""
    cases = MissingChild.query.filter(
        MissingChild.last_seen_lat.isnot(None),
        MissingChild.last_seen_lng.isnot(None)
    ).all()
    
    if len(cases) < 1:
        return []
    
    zones = []
    processed = set()

    buckets = defaultdict(list)
    for idx, case in enumerate(cases):
        buckets[_risk_bucket(case.last_seen_lat, case.last_seen_lng)].append(idx)

    for i, case in enumerate(cases):
        if i in processed:
            continue
            
        zone_cases = [case]
        processed.add(i)

        base_bucket = _risk_bucket(case.last_seen_lat, case.last_seen_lng)
        candidate_indices = []
        for d_lat in (-1, 0, 1):
            for d_lng in (-1, 0, 1):
                candidate_indices.extend(buckets.get((base_bucket[0] + d_lat, base_bucket[1] + d_lng), []))

        for j in candidate_indices:
            if j <= i:
                continue
            if j in processed:
                continue

            other_case = cases[j]
                
            distance = calculate_distance(
                case.last_seen_lat, case.last_seen_lng,
                other_case.last_seen_lat, other_case.last_seen_lng
            )
            
            if distance <= RISK_ZONE_RADIUS_KM:
                zone_cases.append(other_case)
                processed.add(j)
        
        # Allow single-case zones for demo visibility
        if len(zone_cases) >= 1:
            avg_lat = sum(c.last_seen_lat for c in zone_cases) / len(zone_cases)
            avg_lng = sum(c.last_seen_lng for c in zone_cases) / len(zone_cases)
            risk_score = calculate_risk_score(zone_cases)
            zone_name = f"Zone_{len(zones)+1}"
            
            zones.append({
                'name': zone_name,
                'lat': avg_lat,
                'lng': avg_lng,
                'cases': zone_cases,
                'risk_score': risk_score,
                'incident_count': len(zone_cases)
            })
    
    # Save to database
    RiskZone.query.delete()
    
    for zone in zones:
        risk_zone = RiskZone(
            zone_name=zone['name'],
            latitude=zone['lat'],
            longitude=zone['lng'],
            risk_score=zone['risk_score'],
            incident_count=zone['incident_count'],
            radius_km=RISK_ZONE_RADIUS_KM
        )
        db.session.add(risk_zone)
    
    db.session.commit()
    return zones

def calculate_risk_score(cases):
    """Calculate risk score for a zone based on multiple factors"""
    if not cases:
        return 0
    
    incident_score = min(len(cases) * 10, 50)
    
    now = datetime.utcnow()
    recency_total = 0
    age_total = 0

    for case in cases:
        days_ago = (now - case.date_reported).days
        if days_ago <= 30:
            recency_total += 20
        elif days_ago <= 90:
            recency_total += 15
        elif days_ago <= 365:
            recency_total += 10
        else:
            recency_total += 5

        age = case.age or 0
        if age <= 5:
            age_total += 15
        elif age <= 10:
            age_total += 12
        elif age <= 15:
            age_total += 8
        else:
            age_total += 5

    recency_score = recency_total / len(cases)
    age_score = age_total / len(cases)
    
    total_score = incident_score + recency_score + age_score
    return min(total_score, 100)

def analyze_demographic_patterns():
    """Analyze patterns in demographics"""
    cases = MissingChild.query.all()
    
    if not cases:
        return {}
    
    patterns = {
        'age_groups': Counter(),
        'gender_distribution': Counter(),
        'time_patterns': Counter(),
        'location_types': Counter(),
        'recovery_rates': {}
    }

    found_cases_count = 0
    age_totals = Counter()
    age_found = Counter()
    
    for case in cases:
        age_group = get_age_group(case.age or 0)
        patterns['age_groups'][age_group] += 1
        age_totals[age_group] += 1
        
        patterns['gender_distribution'][case.gender] += 1
        
        hour = case.date_reported.hour
        if 6 <= hour < 12:
            patterns['time_patterns']['Morning (6-12)'] += 1
        elif 12 <= hour < 18:
            patterns['time_patterns']['Afternoon (12-18)'] += 1
        elif 18 <= hour < 24:
            patterns['time_patterns']['Evening (18-24)'] += 1
        else:
            patterns['time_patterns']['Night (0-6)'] += 1
        
        location = (case.last_seen_location or '').lower()
        classified = False
        for label, keywords in LOCATION_TYPE_KEYWORDS:
            if any(word in location for word in keywords):
                patterns['location_types'][label] += 1
                classified = True
                break
        if not classified:
            patterns['location_types']['Other'] += 1

        if case.status == 'found':
            found_cases_count += 1
            age_found[age_group] += 1

    total_cases = len(cases)
    
    if total_cases > 0:
        patterns['recovery_rates']['overall'] = (found_cases_count / total_cases) * 100
        patterns['recovery_rates']['by_age'] = {
            age_group: (age_found[age_group] / count) * 100
            for age_group, count in age_totals.items() if count
        }
    
    return patterns

def get_age_group(age):
    """Helper function to get age group"""
    if age <= 5:
        return '0-5 years'
    elif age <= 10:
        return '6-10 years'
    elif age <= 15:
        return '11-15 years'
    else:
        return '16+ years'

def generate_predictive_insights():
    """Generate human-readable insights from analytics"""
    zones = analyze_risk_zones()
    patterns = analyze_demographic_patterns()
    
    insights = []
    
    if zones:
        high_risk_zones = [z for z in zones if z['risk_score'] > 70]
        medium_risk_zones = [z for z in zones if 40 <= z['risk_score'] <= 70]
        
        if high_risk_zones:
            insights.append(f"🔴 HIGH RISK: {len(high_risk_zones)} zones identified with elevated risk (score >70)")
        if medium_risk_zones:
            insights.append(f"🟡 MEDIUM RISK: {len(medium_risk_zones)} zones require monitoring (score 40-70)")
    
    if patterns.get('age_groups'):
        most_vulnerable = max(patterns['age_groups'].items(), key=lambda x: x[1])
        insights.append(f"👶 DEMOGRAPHICS: {most_vulnerable[0]} age group has highest incident rate ({most_vulnerable[1]} cases)")
    
    if patterns.get('time_patterns'):
        peak_time = max(patterns['time_patterns'].items(), key=lambda x: x[1])
        insights.append(f"⏰ TIMING: Most incidents occur during {peak_time[0]} ({peak_time[1]} cases)")
    
    if patterns.get('location_types'):
        common_location = max(patterns['location_types'].items(), key=lambda x: x[1])
        insights.append(f"📍 LOCATIONS: {common_location[0]} areas account for most incidents ({common_location[1]} cases)")
    
    if patterns.get('recovery_rates', {}).get('overall'):
        recovery_rate = patterns['recovery_rates']['overall']
        if recovery_rate > 80:
            insights.append(f"✅ POSITIVE: High recovery rate of {recovery_rate:.1f}%")
        elif recovery_rate < 50:
            insights.append(f"⚠️ CONCERN: Low recovery rate of {recovery_rate:.1f}% - review response protocols")
    
    return insights

# Routes

@app.route('/admin/delete_case/<report_id>', methods=['POST'])
@login_required
def delete_case(report_id):
    """Delete a missing child case and all associated data"""
    try:
        missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
        
        # Store child name for flash message
        child_name = missing_child.name
        
        # Delete associated sightings first (foreign key constraint)
        sightings = Sighting.query.filter_by(report_id=report_id).all()
        for sighting in sightings:
            db.session.delete(sighting)
        
        # Delete files from Cloudinary if they exist
        if CLOUDINARY_ENABLED:
            try:
                # Delete photo from Cloudinary
                if missing_child.photo_filename and missing_child.photo_filename.startswith('http'):
                    photo_public_id = f"missing_children/photos/{report_id}_photo"
                    cloudinary.uploader.destroy(photo_public_id)
                    print(f"✅ Deleted photo from Cloudinary: {photo_public_id}")
                
                # Delete audio from Cloudinary
                if missing_child.audio_filename and missing_child.audio_filename.startswith('http'):
                    audio_public_id = f"missing_children/audio/{report_id}_audio"
                    cloudinary.uploader.destroy(audio_public_id, resource_type="video")
                    print(f"✅ Deleted audio from Cloudinary: {audio_public_id}")
                    
            except Exception as cloudinary_error:
                print(f"⚠️ Cloudinary deletion error: {str(cloudinary_error)}")
        else:
            # Delete local files if they exist
            try:
                if missing_child.photo_filename and not missing_child.photo_filename.startswith('http'):
                    photo_path = os.path.join(app.config['UPLOAD_FOLDER'], 'photos', missing_child.photo_filename.split('/')[-1])
                    if os.path.exists(photo_path):
                        os.remove(photo_path)
                        print(f"✅ Deleted local photo: {photo_path}")
                
                if missing_child.audio_filename and not missing_child.audio_filename.startswith('http'):
                    audio_path = os.path.join(app.config['UPLOAD_FOLDER'], 'audio', missing_child.audio_filename.split('/')[-1])
                    if os.path.exists(audio_path):
                        os.remove(audio_path)
                        print(f"✅ Deleted local audio: {audio_path}")
                        
            except Exception as file_error:
                print(f"⚠️ Local file deletion error: {str(file_error)}")
        
        # Delete the missing child record
        db.session.delete(missing_child)
        db.session.commit()
        _invalidate_case_ml_cache(report_id)
        
        # No broadcast on delete per requirements
        
        flash(f'Case for {child_name} (ID: {report_id}) has been permanently deleted along with all associated data.', 'success')
        
    except Exception as e:
        db.session.rollback()
        print(f"❌ Error deleting case: {str(e)}")
        flash(f'Error deleting case: {str(e)}', 'error')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_sighting/<int:sighting_id>', methods=['POST'])
@login_required
def delete_sighting(sighting_id):
    sighting = Sighting.query.get_or_404(sighting_id)
    report_id = sighting.report_id
    
    # Delete photo if exists
    if sighting.photo_filename and not sighting.photo_filename.startswith('http'):
        try:
            os.remove(os.path.join(app.config['UPLOAD_FOLDER'], 'photos', sighting.photo_filename))
        except:
            pass
            
    db.session.delete(sighting)
    db.session.commit()
    _invalidate_case_ml_cache(report_id)
    
    # Update risk zones
    try:
        analyze_risk_zones()
    except Exception:
        pass
        
    flash('Sighting report deleted successfully.', 'success')
    return redirect(url_for('admin_case_detail', report_id=report_id))

@app.route('/admin/bulk_delete', methods=['POST'])
@login_required
def bulk_delete_cases():
    """Delete multiple cases at once"""
    try:
        case_ids = request.form.getlist('case_ids')
        
        if not case_ids:
            flash('No cases selected for deletion.', 'warning')
            return redirect(url_for('admin_dashboard'))
        
        deleted_count = 0
        deleted_names = []

        selected_cases = MissingChild.query.filter(MissingChild.report_id.in_(case_ids)).all()
        cases_by_report = {case.report_id: case for case in selected_cases}
        report_ids_to_delete = list(cases_by_report.keys())

        if report_ids_to_delete:
            Sighting.query.filter(Sighting.report_id.in_(report_ids_to_delete)).delete(synchronize_session=False)

        for report_id in case_ids:
            missing_child = cases_by_report.get(report_id)
            if not missing_child:
                continue

            deleted_names.append(missing_child.name)

            # Delete files (same logic as single delete)
            if CLOUDINARY_ENABLED:
                try:
                    if missing_child.photo_filename and missing_child.photo_filename.startswith('http'):
                        photo_public_id = f"missing_children/photos/{report_id}_photo"
                        cloudinary.uploader.destroy(photo_public_id)

                    if missing_child.audio_filename and missing_child.audio_filename.startswith('http'):
                        audio_public_id = f"missing_children/audio/{report_id}_audio"
                        cloudinary.uploader.destroy(audio_public_id, resource_type="video")
                except Exception:
                    pass

            # Delete the record
            db.session.delete(missing_child)
            deleted_count += 1
        
        db.session.commit()
        for rid in report_ids_to_delete:
            _invalidate_case_ml_cache(rid)
        
        # No broadcast on bulk delete per requirements
        
        flash(f'Successfully deleted {deleted_count} cases: {", ".join(deleted_names)}', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error during bulk deletion: {str(e)}', 'error')
    
    return redirect(url_for('admin_dashboard'))


@app.route('/')
def index():
    recent_cases = MissingChild.query.filter_by(status='missing').order_by(MissingChild.date_reported.desc()).limit(5).all()
    return render_template('index.html', recent_cases=recent_cases)

@app.route('/report', methods=['GET', 'POST'])
def report_missing():
    # Check for police authorization - either token or session login
    police_token = request.args.get('token') or session.get('police_token')
    valid_token = app.config.get('POLICE_ACCESS_TOKEN')
    is_police_logged_in = session.get('police_logged_in')
    
    # Allow access if: valid token OR logged in as police
    if not is_police_logged_in and (not valid_token or police_token != valid_token):
        flash('This form is for authorized police personnel only. Please use the public sighting form to report sightings.', 'warning')
        return redirect(url_for('index'))
    
    # Store token in session for form submission (only if using token method)
    if police_token:
        session['police_token'] = police_token
    
    if request.method == 'POST':
        report_id = f"MC{datetime.now().strftime('%Y%m%d')}{str(uuid.uuid4())[:8].upper()}"
        
        name = request.form['name']
        age = request.form['age']
        gender = request.form['gender']
        location = request.form['location']
        location_subcategory = request.form.get('location_subcategory', '').strip() or None
        description = request.form['description']
        emergency_contact = request.form['emergency_contact']
        
        # Parse abduction_time (HH:MM) to float hour (e.g. 14:30 -> 14.5)
        abduction_time_str = request.form.get('abduction_time')
        abduction_time_val = 12.0
        if abduction_time_str:
            try:
                h, m = map(int, abduction_time_str.split(':'))
                abduction_time_val = h + (m / 60.0)
            except ValueError:
                pass
        
        # Parse missing_date
        missing_date_str = request.form.get('missing_date')
        missing_date_val = None
        if missing_date_str:
            try:
                missing_date_val = datetime.strptime(missing_date_str, '%Y-%m-%d').date()
            except ValueError:
                pass
        
        lat, lng = get_location_coordinates(location)
        
        photo_url = None
        audio_url = None
        
        # Handle photo upload
        if 'photo' in request.files:
            photo = request.files['photo']
            if photo and photo.filename and allowed_file(photo.filename, {'png', 'jpg', 'jpeg', 'gif'}):
                if CLOUDINARY_ENABLED:
                    # Upload to Cloudinary
                    photo_url = upload_to_cloudinary(
                        photo,
                        'missing_children/photos',
                        f"{report_id}_photo"
                    )
                    if photo_url:
                        print(f"✅ Photo uploaded to Cloudinary: {photo_url}")
                    else:
                        flash('Photo upload failed, but report was created successfully', 'warning')
                else:
                    # Use improved local storage
                    photo_filename = secure_filename(f"{report_id}_{photo.filename}")
                    photo_url = save_file_locally(photo, 'photos', photo_filename)
                    if photo_url:
                        print(f"✅ Photo saved locally: {photo_url}")
                    else:
                        flash('Photo upload failed, but report was created successfully', 'warning')
        
        # Handle audio upload
        if 'audio' in request.files:
            audio = request.files['audio']
            if audio and audio.filename and allowed_file(audio.filename, {'mp3', 'wav', 'ogg', 'm4a'}):
                if CLOUDINARY_ENABLED:
                    # Upload to Cloudinary
                    audio_url = upload_audio_to_cloudinary(
                        audio,
                        f"{report_id}_audio"
                    )
                    if audio_url:
                        print(f"✅ Audio uploaded to Cloudinary: {audio_url}")
                    else:
                        flash('Audio upload failed, but report was created successfully', 'warning')
                else:
                    # Use improved local storage
                    audio_filename = secure_filename(f"{report_id}_{audio.filename}")
                    audio_url = save_file_locally(audio, 'audio', audio_filename)
                    if audio_url:
                        print(f"✅ Audio saved locally: {audio_url}")
                    else:
                        flash('Audio upload failed, but report was created successfully', 'warning')
        
        # Get ML features from report form
        region_type_val = request.form.get('region_type', 'Urban')
        abductor_relation_val = request.form.get('abductor_relation', 'stranger')
        
        # Create missing child record with URLs instead of filenames
        missing_child = MissingChild(
            report_id=report_id,
            name=name,
            age=age,
            gender=gender,
            last_seen_location=location,
            location_subcategory=location_subcategory,
            abduction_time=abduction_time_val,
            missing_date=missing_date_val,
            region_type=region_type_val,
            abductor_relation=abductor_relation_val,
            last_seen_lat=lat,
            last_seen_lng=lng,
            description=description,
            photo_filename=photo_url,
            audio_filename=audio_url,
            emergency_contact=emergency_contact
        )
        
        db.session.add(missing_child)
        db.session.commit()
        
        # Build rich Telegram alert
        report_url = request.url_root.rstrip('/') + f"/found/{report_id}"
        telegram_caption = (
            f"🚨 <b>MISSING CHILD ALERT</b> 🚨\n\n"
            f"<b>Name:</b> {name}\n"
            f"<b>Age:</b> {age} years\n"
            f"<b>Gender:</b> {gender}\n"
            f"<b>Last Seen:</b> {location}\n\n"
            f"<b>Report sightings:</b> {report_url}"
        )

        # Send photo+caption if photo available, else text-only
        if photo_url and photo_url.startswith('http'):
            sent_count = send_telegram_photo(photo_url, telegram_caption)
        else:
            sent_count = send_telegram_broadcast(telegram_caption)
        
        if sent_count > 0:
            flash(f'Missing child report created successfully! Report ID: {report_id}. Alert sent to {sent_count} subscribers.', 'success')
        else:
            flash(f'Missing child report created successfully! Report ID: {report_id}. However, SMS alerts could not be sent.', 'warning')
        
        # Update risk zones
        try:
            analyze_risk_zones()
        except Exception as e:
            print(f"Risk zone analysis failed: {e}")
        
        return redirect(url_for('case_detail', report_id=report_id))
    
    return render_template('report.html')

@app.route('/found/<report_id>', methods=['GET', 'POST'])
def report_found(report_id):
    missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    
    if request.method == 'POST':
        location = request.form['location']
        description = request.form.get('description', '')
        reporter_phone = request.form.get('reporter_phone', '')
        
        # Get coordinates from form (map picker) or geocode
        lat_form = request.form.get('latitude')
        lng_form = request.form.get('longitude')
        
        if lat_form and lng_form:
            try:
                lat = float(lat_form)
                lng = float(lng_form)
            except ValueError:
                lat, lng = get_location_coordinates(location)
        else:
            lat, lng = get_location_coordinates(location)

        # Optional photo upload for sighting
        sighting_photo_url = None
        if 'photo' in request.files:
            photo = request.files['photo']
            if photo and photo.filename and allowed_file(photo.filename, {'png', 'jpg', 'jpeg', 'gif'}):
                if CLOUDINARY_ENABLED:
                    sighting_photo_url = upload_to_cloudinary(
                        photo,
                        'missing_children/sightings',
                        f"{report_id}_sighting_{int(datetime.utcnow().timestamp())}"
                    )
                else:
                    photo_filename = secure_filename(f"{report_id}_sighting_{photo.filename}")
                    sighting_photo_url = save_file_locally(photo, 'photos', photo_filename)
        
        # Parse sighting date and time from form
        sighting_date_str = request.form.get('sighting_date')
        sighting_time_str = request.form.get('sighting_time')
        
        sighting_datetime = datetime.utcnow()  # Default to now
        hours_since_abduction = None
        
        if sighting_date_str and sighting_time_str:
            try:
                sighting_datetime = datetime.strptime(
                    f"{sighting_date_str} {sighting_time_str}", 
                    "%Y-%m-%d %H:%M"
                )
                
                # Calculate hours since abduction if we have missing_date and abduction_time
                if missing_child.missing_date:
                    # Combine missing_date with abduction_time (stored as hour float)
                    abduction_hour = int(missing_child.abduction_time or 12)
                    abduction_minute = int((missing_child.abduction_time or 12) % 1 * 60)
                    abduction_datetime = datetime.combine(
                        missing_child.missing_date,
                        datetime.min.time().replace(hour=abduction_hour, minute=abduction_minute)
                    )
                    
                    time_diff = sighting_datetime - abduction_datetime
                    hours_since_abduction = time_diff.total_seconds() / 3600
                    if hours_since_abduction < 0:
                        hours_since_abduction = 0  # Can't be negative
                    
            except ValueError as e:
                print(f"Error parsing sighting date/time: {e}")
                sighting_datetime = datetime.utcnow()
        
        sighting = Sighting(
            report_id=report_id,
            location=location,
            latitude=lat,
            longitude=lng,
            description=description,
            reporter_phone=reporter_phone,
            photo_filename=sighting_photo_url,
            sighting_time=sighting_datetime
        )
        
        db.session.add(sighting)
        db.session.commit()
        _invalidate_case_ml_cache(report_id)
        
        # Face comparison (if both photos exist)
        if sighting_photo_url and missing_child.photo_filename:
            try:
                from utils.face_compare import compare_faces, is_available
                if is_available():
                    # Get paths for comparison
                    child_photo = missing_child.photo_filename
                    if not child_photo.startswith('http'):
                        child_photo = os.path.join(app.config['UPLOAD_FOLDER'], 'photos', child_photo)
                    
                    sighting_photo = sighting_photo_url
                    if not sighting_photo.startswith('http'):
                        sighting_photo = os.path.join(app.config['UPLOAD_FOLDER'], 'photos', sighting_photo)
                    
                    match_score = compare_faces(child_photo, sighting_photo)
                    if match_score is not None:
                        sighting.face_match_score = match_score
                        db.session.commit()
                        print(f"✅ Face comparison: {match_score}% match")
            except Exception as e:
                print(f"⚠️ Face comparison skipped: {e}")
        
        # Build rich sighting Telegram alert
        report_url = request.url_root.rstrip('/') + f"/found/{report_id}"
        sighting_caption = (
            f"👁️ <b>SIGHTING REPORTED</b>\n\n"
            f"<b>Child:</b> {missing_child.name}\n"
            f"<b>Spotted At:</b> {location}\n"
            f"<b>Time:</b> {datetime.now().strftime('%I:%M %p')}\n"
            f"<b>Case ID:</b> {report_id}\n\n"
            f"<b>Case updates &amp; details:</b> {report_url}"
        )

        if sighting_photo_url and sighting_photo_url.startswith('http'):
            target_numbers = send_telegram_photo(sighting_photo_url, sighting_caption)
        else:
            target_numbers = send_telegram_broadcast(sighting_caption)
        sent_count = target_numbers
        
        # Update risk zones
        try:
            analyze_risk_zones()
        except Exception as e:
            print(f"Risk zone analysis failed: {e}")
        
        flash('Thank you for reporting the sighting! Alert sent to authorities and subscribers.', 'success')
        return redirect(url_for('report_found', report_id=report_id))
    
    return render_template('found.html', child=missing_child)

@app.route('/case/<report_id>')
def case_detail(report_id):
    missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    sightings = Sighting.query.filter_by(report_id=report_id).order_by(Sighting.sighting_time.asc()).all()

    ml_prediction, ml_refined, ml_status = _compute_case_ml_outputs(missing_child, sightings, context_label='public case')

    return render_template('case_detail.html', child=missing_child, sightings=sightings,
                         ml_prediction=ml_prediction, ml_refined=ml_refined,
                         ml_status=ml_status)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    # Obfuscation: require a valid access token hint via query/header to render login
    required_token = app.config.get('ADMIN_ACCESS_TOKEN')
    provided_token = request.args.get('t') or request.headers.get('X-Admin-Token')
    if required_token and provided_token != required_token:
        # Return 404 to avoid revealing admin endpoint
        return render_template('errors/404.html'), 404

    client_key = f"ip:{_get_client_ip()}"
    if _is_locked_out(client_key):
        flash('Too many attempts. Try again later.', 'error')
        return render_template('admin/login.html'), 429

    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        is_valid = (
            username == app.config['ADMIN_USERNAME'] and
            password == app.config['ADMIN_PASSWORD']
        )

        if is_valid:
            _reset_failed_attempts(client_key)
            user = User.query.filter_by(username=username).first()
            if not user:
                user = User(username=username, password_hash=generate_password_hash(password))
                db.session.add(user)
                db.session.commit()
            login_user(user)
            # Clear police session to prevent role conflicts
            session.pop('police_logged_in', None)
            session.pop('police_username', None)
            return redirect(url_for('admin_dashboard'))
        else:
            _register_failed_attempt(client_key)
            # Generic error message
            flash('Invalid credentials', 'error')

    return render_template('admin/login.html')

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    cases = MissingChild.query.order_by(MissingChild.date_reported.desc()).all()
    total_cases = len(cases)
    active_cases = len([c for c in cases if c.status == 'missing'])
    found_cases = len([c for c in cases if c.status == 'found'])
    
    return render_template('admin/dashboard.html', 
                         cases=cases, 
                         total_cases=total_cases,
                         active_cases=active_cases,
                         found_cases=found_cases)

# Optionally return 404 for unauthorized access to admin routes (already protected by @login_required).

@app.route('/admin/case/<report_id>/update_ml', methods=['POST'])
@login_required
def update_ml_features(report_id):
    child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    
    try:
        child.abduction_time = float(request.form.get('abduction_time', 12.0))
        child.abductor_relation = request.form.get('abductor_relation', 'stranger')
        child.region_type = request.form.get('region_type', 'Urban')
        child.population_density = int(request.form.get('population_density', 5000))
        
        db.session.commit()
        _invalidate_case_ml_cache(report_id)
        flash('ML features updated successfully. Prediction re-calculated.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating features: {str(e)}', 'danger')
        
    return redirect(url_for('admin_case_detail', report_id=report_id))

@app.route('/admin/case/<report_id>')
@login_required
def admin_case_detail(report_id):
    missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    sightings = Sighting.query.filter_by(report_id=report_id).order_by(Sighting.sighting_time.asc()).all()

    heat_data = []
    for sighting in sightings:
        if sighting.latitude and sighting.longitude:
            hours_ago = (datetime.utcnow() - sighting.sighting_time).total_seconds() / 3600
            intensity = max(0.1, 1.0 - (hours_ago / 168))
            heat_data.append([sighting.latitude, sighting.longitude, intensity])

    ml_prediction, ml_refined, ml_status = _compute_case_ml_outputs(missing_child, sightings, context_label='admin case')

    # Fetch active risk zones for the map
    risk_zones = [z.to_dict() for z in RiskZone.query.filter_by(is_active=True).all()]

    return render_template('admin/case_detail.html', 
                         child=missing_child, 
                         sightings=sightings,
                         heat_data=json.dumps(heat_data),
                         ml_prediction=ml_prediction,
                         ml_refined=ml_refined,
                         ml_status=ml_status,
                         risk_zones=risk_zones)

@app.route('/admin/update_status/<report_id>/<status>')
@login_required
def update_case_status(report_id, status):
    missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    old_status = missing_child.status
    missing_child.status = status
    db.session.commit()
    
    if status == 'found' and old_status == 'missing':
        sms_message = f"FOUND: {missing_child.name} ({missing_child.age}yrs) found safe! ID: {report_id}. Thank you for helping!"
        sent_count = send_sms_alert(sms_message)
        flash(f'Case marked as FOUND! Alert sent to {sent_count} subscribers.', 'success')
    elif status == 'missing' and old_status == 'found':
        sms_message = f"URGENT: {missing_child.name} missing again! {missing_child.last_seen_location}. ID: {report_id}"
        target_numbers = select_numbers_for_location(missing_child.last_seen_location)
        sent_count = send_sms_alert_to_numbers(sms_message, target_numbers)
        flash(f'Case marked as MISSING again! Alert sent to {sent_count} subscribers.', 'warning')
    elif status == 'closed':
        sms_message = f"CLOSED: {missing_child.name} case closed. ID: {report_id}"
        sent_count = send_sms_alert(sms_message)
        flash(f'Case closed! Alert sent to {sent_count} subscribers.', 'info')
    else:
        flash(f'Case status updated to {status}', 'success')
    
    return redirect(url_for('admin_case_detail', report_id=report_id))

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('index'))

# ============ POLICE PORTAL ============

@app.route('/police/login', methods=['GET', 'POST'])
def police_login():
    """Police login portal"""
    # Obfuscation: require a valid access token hint via query/header to render login
    required_token = app.config.get('POLICE_ACCESS_TOKEN')
    provided_token = request.args.get('t') or request.headers.get('X-Police-Token')
    if required_token and provided_token != required_token:
        # Return 404 to avoid revealing police endpoint
        return render_template('errors/404.html'), 404
    
    # Block admin users from accessing police login
    if current_user.is_authenticated:
        flash('Admin users cannot access Police Portal. Please use Admin Dashboard.', 'warning')
        return redirect(url_for('admin_dashboard'))
    
    # Check if already logged in as police
    if session.get('police_logged_in'):
        return redirect(url_for('police_dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        
        # Validate credentials
        if (username == app.config.get('POLICE_USERNAME') and 
            password == app.config.get('POLICE_PASSWORD')):
            # Clear any admin session to prevent role conflicts
            logout_user()
            session['police_logged_in'] = True
            session['police_username'] = username
            flash('Welcome to Police Portal', 'success')
            return redirect(url_for('police_dashboard'))
        else:
            flash('Invalid credentials', 'error')
    
    return render_template('police/login.html')

@app.route('/police/dashboard')
def police_dashboard():
    """Police dashboard showing all cases"""
    # Prevent admin from accessing police portal (role separation)
    if current_user.is_authenticated:
        flash('Admin users should use the Admin Dashboard', 'info')
        return redirect(url_for('admin_dashboard'))
    
    if not session.get('police_logged_in'):
        flash('Please login to access the Police Portal', 'warning')
        return redirect(url_for('police_login'))
    
    cases = MissingChild.query.options(selectinload(MissingChild.sightings)).order_by(MissingChild.date_reported.desc()).all()
    
    # Calculate stats in one pass
    active_cases = 0
    resolved_cases = 0
    total_sightings = 0
    high_match_sightings = 0

    for case in cases:
        if case.status == 'missing':
            active_cases += 1
        elif case.status in ['found', 'closed']:
            resolved_cases += 1

        total_sightings += len(case.sightings)
        for sighting in case.sightings:
            if sighting.face_match_score and sighting.face_match_score >= 70:
                high_match_sightings += 1
    
    stats = {
        'active_cases': active_cases,
        'resolved_cases': resolved_cases,
        'total_sightings': total_sightings,
        'high_match_sightings': high_match_sightings
    }
    
    return render_template('police/dashboard.html', cases=cases, stats=stats, config=app.config)

@app.route('/police/case/<report_id>')
def police_case_detail(report_id):
    """Police case detail with face match and ML predictions"""
    # Prevent admin from accessing police portal (role separation)
    if current_user.is_authenticated:
        flash('Admin users should use the Admin Dashboard', 'info')
        return redirect(url_for('admin_case_detail', report_id=report_id))
    
    if not session.get('police_logged_in'):
        flash('Please login to access the Police Portal', 'warning')
        return redirect(url_for('police_login'))
    
    child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    sightings = Sighting.query.filter_by(report_id=report_id).order_by(Sighting.sighting_time.asc()).all()

    # Heat data for sightings
    heat_data = []
    for sighting in sightings:
        if sighting.latitude and sighting.longitude:
            hours_ago = (datetime.utcnow() - sighting.sighting_time).total_seconds() / 3600
            intensity = max(0.1, 1.0 - (hours_ago / 168))
            heat_data.append([sighting.latitude, sighting.longitude, intensity])

    ml_prediction, ml_refined, ml_status = _compute_case_ml_outputs(child, sightings, context_label='police case')

    # Fetch active risk zones for the map
    risk_zones = [z.to_dict() for z in RiskZone.query.filter_by(is_active=True).all()]

    return render_template('police/case_detail.html', child=child, sightings=sightings,
                         heat_data=json.dumps(heat_data),
                         ml_prediction=ml_prediction, ml_refined=ml_refined,
                         ml_status=ml_status,
                         risk_zones=risk_zones)

@app.route('/police/logout')
def police_logout():
    """Police logout"""
    session.pop('police_logged_in', None)
    session.pop('police_username', None)
    flash('Logged out successfully', 'info')
    return redirect(url_for('index'))

@app.route('/admin/analytics')
@login_required
def admin_analytics():
    zones = analyze_risk_zones()
    patterns = analyze_demographic_patterns()
    insights = generate_predictive_insights()
    
    analytics_data = {
        'zones': len(zones),
        'patterns': patterns,
        'timestamp': datetime.utcnow().isoformat()
    }
    
    analytics_record = Analytics(
        analysis_type='comprehensive',
        analysis_data=json.dumps(analytics_data),
        insights='; '.join(insights)
    )
    db.session.add(analytics_record)
    db.session.commit()
    
    return render_template('admin/analytics.html', 
                         zones=zones, 
                         patterns=patterns, 
                         insights=insights)

@app.route('/admin/risk-zones')
@login_required
def admin_risk_zones():
    risk_zones = RiskZone.query.filter_by(is_active=True).all()
    cases = MissingChild.query.filter(
        MissingChild.last_seen_lat.isnot(None),
        MissingChild.last_seen_lng.isnot(None)
    ).all()
    
    return render_template('admin/risk_zones.html', 
                         risk_zones=risk_zones, 
                         cases=cases)

@app.route('/api/geocode')
def geocode():
    location = request.args.get('location', '').strip()
    if not location:
        return jsonify({'error': 'Location parameter is required'}), 400
    
    lat, lng = get_location_coordinates(location)
    if lat and lng:
        return jsonify({
            'lat': lat, 
            'lng': lng,
            'success': True,
            'location': location
        })
    return jsonify({
        'error': 'Location not found',
        'success': False,
        'location': location
    }), 404

@app.route('/api/analytics/update')
@login_required
def update_analytics():
    try:
        zones = analyze_risk_zones()
        patterns = analyze_demographic_patterns()
        insights = generate_predictive_insights()
        
        return jsonify({
            'success': True,
            'zones': len(zones),
            'insights': insights
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/test-sms')
def test_sms():
    """Test route to send a sample SMS - remove in production"""
    if not app.config['DEBUG'] and not current_user.is_authenticated:
        return "Unauthorized", 401
    
    test_message = f"TEST: Child Alert System working. Time: {datetime.now().strftime('%H:%M')}"
    sent_count = send_sms_alert(test_message)
    
    if sent_count > 0:
        return f"✅ Test SMS sent to {sent_count} numbers successfully!"
    else:
        return "❌ Test SMS failed. Check server logs for details."

@app.errorhandler(404)
def not_found_error(error):
    return render_template('errors/404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    db.session.rollback()
    return render_template('errors/500.html'), 500

# Health check endpoint for Render
@app.route('/health')
def health_check():
    return jsonify({'status': 'healthy', 'timestamp': datetime.utcnow().isoformat()})


@app.route('/case/<report_id>/poster')
def download_poster(report_id):
    """Generate and download a missing child poster as PNG image"""
    try:
        from utils.poster_generator import generate_missing_poster, poster_to_bytes
        
        missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
        
        # Generate poster
        base_url = request.url_root.rstrip('/')
        poster = generate_missing_poster(missing_child, base_url=base_url)
        
        # Convert to bytes
        buffer = poster_to_bytes(poster, format='PNG')
        
        return Response(
            buffer.getvalue(),
            mimetype='image/png',
            headers={
                'Content-Disposition': f'attachment; filename=missing_poster_{report_id}.png'
            }
        )
    except ImportError:
        flash('Poster generation not available. Please install qrcode and Pillow.', 'error')
        return redirect(url_for('case_detail', report_id=report_id))
    except Exception as e:
        print(f"❌ Poster generation error: {str(e)}")
        flash(f'Error generating poster: {str(e)}', 'error')
        return redirect(url_for('case_detail', report_id=report_id))


@app.route('/ml')
@login_required
def ml_page():
    """Render a simple ML prediction UI integrated into the main site.

    The form uses JavaScript to call `/api/ml/predict` and `/api/ml/refine` so
    we keep the model functions and paths intact (they live in `predictor.py`).
    Only accessible to logged-in users (admin/police).
    """
    return render_template('ml.html')


@app.route('/api/ml/predict', methods=['POST'])
def api_ml_predict():
    if IS_SERVERLESS_ENV:
        return jsonify({
            'success': False,
            'error': 'ML is disabled in this serverless deployment environment'
        }), 503

    payload = request.get_json() or {}
    try:
        # Lazy import to avoid heavy startup cost if models are not present
        from predictor import predict_initial_case, haversine
    except Exception as e:
        return jsonify({'success': False, 'error': f'Model import failed: {e}'}), 500

    # Accept both form-style payloads and JSON
    data = payload if isinstance(payload, dict) else {}

    # Build case input expected by the predictor
    case_input = {}
    # map possible field names
    mapping = {
        'age': 'child_age', 'child_age': 'child_age', 'gender': 'child_gender', 'child_gender': 'child_gender',
        'abduction_time': 'abduction_time', 'abductor_relation': 'abductor_relation', 'latitude': 'latitude', 'longitude': 'longitude',
        'day_of_week': 'day_of_week', 'region_type': 'region_type', 'population_density': 'population_density', 'transport_hub_nearby': 'transport_hub_nearby'
    }
    for k, v in mapping.items():
        if k in data:
            case_input[v] = data[k]

    # Fallbacks from query parameters if not JSON
    if not case_input:
        for k, v in mapping.items():
            val = request.form.get(k)
            if val is not None:
                case_input[v] = val

    # Compute dist_to_nearest_city used by Stage 2
    CITY_CENTERS = {'Mumbai':(19.0761,72.8775),'Pune':(18.5203,73.8567),'Nagpur':(21.1497,79.0806),'Nashik':(19.9975,73.7898)}
    try:
        lat = float(case_input.get('latitude', 0))
        lon = float(case_input.get('longitude', 0))
        case_input['dist_to_nearest_city'] = min([haversine(lat, lon, c_lat, c_lon) for c_lat, c_lon in CITY_CENTERS.values()]) if CITY_CENTERS else 0
    except Exception:
        case_input['dist_to_nearest_city'] = 0

    try:
        prediction = predict_initial_case(case_input)
        return jsonify({'success': True, 'prediction': prediction, 'case_input': case_input})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/ml/refine', methods=['POST'])
def api_ml_refine():
    if IS_SERVERLESS_ENV:
        return jsonify({
            'success': False,
            'error': 'ML is disabled in this serverless deployment environment'
        }), 503

    payload = request.get_json() or {}
    try:
        from predictor import refine_location_with_sightings
    except Exception as e:
        return jsonify({'success': False, 'error': f'Model import failed: {e}'}), 500

    initial_prediction = payload.get('initial_prediction')
    sightings = payload.get('sightings', [])
    initial_case_input = payload.get('initial_case_input', {})

    if not initial_prediction or not initial_case_input:
        return jsonify({'success': False, 'error': 'initial_prediction and initial_case_input are required'}), 400

    try:
        lat, lon = refine_location_with_sightings(initial_prediction, sightings, initial_case_input)
        return jsonify({'success': True, 'refined_lat': lat, 'refined_lon': lon})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/case/<report_id>/ml/rerun', methods=['POST'])
def api_case_ml_rerun(report_id):
    missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    sightings = Sighting.query.filter_by(report_id=report_id).order_by(Sighting.sighting_time.asc()).all()

    ml_prediction, ml_refined, ml_status = _compute_case_ml_outputs(
        missing_child,
        sightings,
        context_label='case rerun',
        use_cache=True,
        force_refresh=True
    )

    if not ml_status.get('available'):
        return jsonify({
            'success': False,
            'error': ml_status.get('message') or 'ML unavailable',
            'ml_status': ml_status,
            'ml_prediction': ml_prediction,
            'ml_refined': ml_refined,
        }), 503

    return jsonify({
        'success': True,
        'ml_status': ml_status,
        'ml_prediction': ml_prediction,
        'ml_refined': ml_refined,
    })

# ... (keep everything above as is until the create_tables function)

# Initialize database
def create_tables():
    """Create database tables if they don't exist"""
    try:
        with app.app_context():
            # Check if we can connect to database (using newer SQLAlchemy syntax)
            with db.engine.connect() as connection:
                connection.execute(db.text('SELECT 1'))
            print("✅ Database connection successful")
            
            # Create tables if they don't exist
            db.create_all()
            print("✅ Database tables created/verified")
            
            # Check for missing columns and add them
            try:
                with db.engine.connect() as connection:
                    inspector = inspect(db.engine)
                    missing_child_columns = {col['name'] for col in inspector.get_columns('missing_child')}
                    sighting_columns = {col['name'] for col in inspector.get_columns('sighting')}

                    # Check if emergency_contact column exists
                    if 'emergency_contact' not in missing_child_columns:
                        print("🔄 Adding emergency_contact column to missing_child table...")
                        connection.execute(db.text("""
                            ALTER TABLE missing_child 
                            ADD COLUMN emergency_contact VARCHAR(100)
                        """))
                        connection.commit()
                        print("✅ emergency_contact column added successfully")
                    else:
                        print("✅ emergency_contact column already exists")

                    # Check if photo_filename exists on sighting
                    if 'photo_filename' not in sighting_columns:
                        print("🔄 Adding photo_filename column to sighting table...")
                        connection.execute(db.text("""
                            ALTER TABLE sighting 
                            ADD COLUMN photo_filename VARCHAR(500)
                        """))
                        connection.commit()
                        print("✅ photo_filename column added successfully")
                    else:
                        print("✅ photo_filename column already exists on sighting")
                    
                    # Check if location_subcategory exists on missing_child
                    if 'location_subcategory' not in missing_child_columns:
                        print("🔄 Adding location_subcategory column to missing_child table...")
                        connection.execute(db.text("""
                            ALTER TABLE missing_child 
                            ADD COLUMN location_subcategory VARCHAR(200)
                        """))
                        connection.commit()
                        print("✅ location_subcategory column added successfully")
                    else:
                        print("✅ location_subcategory column already exists on missing_child")
                        
            except Exception as migration_error:
                print(f"⚠️ Migration error: {str(migration_error)}")
                # Continue anyway - the column might exist or be added later
            
            # Create admin user if it doesn't exist
            admin_user = User.query.filter_by(username='admin').first()
            if not admin_user:
                from werkzeug.security import generate_password_hash
                admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
                admin_user = User(
                    username='admin',
                    password_hash=generate_password_hash(admin_password)
                )
                db.session.add(admin_user)
                db.session.commit()
                print("✅ Default admin user created")
            
            # Check if we have any existing data
            try:
                case_count = MissingChild.query.count()
                user_count = User.query.count()
                print(f"📊 Database ready with {case_count} cases, {user_count} users")
                
                # Initial risk zone analysis
                analyze_risk_zones()
                print("✅ Initial risk zone analysis complete")
            except Exception as count_error:
                print(f"⚠️ Could not count existing data: {str(count_error)}")
            
    except Exception as e:
        print(f"❌ Database connection error: {str(e)}")
        # If connection fails, try to create tables anyway
        try:
            print("🔄 Attempting to create tables...")
            with app.app_context():
                db.create_all()
            print("✅ Database tables created successfully")
        except Exception as create_error:
            print(f"❌ Failed to create tables: {str(create_error)}")
            # Don't raise error in production - let the app start anyway
            if not app.config.get('DEBUG', False):
                print("⚠️ Continuing without database verification (production mode)")
            else:
                raise create_error
            
# Add this route for debugging image URLs
@app.route('/debug/case/<report_id>')
def debug_case(report_id):
    if not app.config['DEBUG'] and not current_user.is_authenticated:
        return "Unauthorized", 401
    
    missing_child = MissingChild.query.filter_by(report_id=report_id).first_or_404()
    
    debug_info = {
        'report_id': missing_child.report_id,
        'name': missing_child.name,
        'photo_filename': missing_child.photo_filename,
        'photo_is_url': missing_child.photo_filename.startswith('http') if missing_child.photo_filename else False,
        'audio_filename': missing_child.audio_filename,
        'audio_is_url': missing_child.audio_filename.startswith('http') if missing_child.audio_filename else False,
        'cloudinary_enabled': CLOUDINARY_ENABLED
    }
    
    return jsonify(debug_info)

if __name__ == '__main__':
    create_tables()
    port = int(os.environ.get('PORT', 5002))
    app.run(host='0.0.0.0', port=port, debug=app.config['DEBUG'])
else:
    # This runs in production with Gunicorn
    create_tables()
