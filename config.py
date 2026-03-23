import os
from urllib.parse import quote_plus
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))
IS_CLOUD_ENV = bool(os.environ.get('RENDER') or os.environ.get('VERCEL'))

# Only load .env in development
if not IS_CLOUD_ENV:
    load_dotenv(os.path.join(basedir, '.env'))


def _read_env(*names):
    """Read env var by priority and normalize optional surrounding quotes."""
    for name in names:
        value = os.environ.get(name)
        if value is None:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1].strip()
        if value:
            return value
    return None


def _sanitize_postgres_url(db_url):
    """Remove non-libpq query params that break psycopg2 DSN parsing."""
    if not db_url:
        return db_url

    try:
        parsed = urlsplit(db_url)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)

        # Common provider metadata params that psycopg2/libpq does not accept.
        blocked_params = {'supa', 'pgbouncer'}
        cleaned_query_items = [(k, v) for (k, v) in query_items if k.lower() not in blocked_params]

        if len(cleaned_query_items) == len(query_items):
            return db_url

        cleaned_query = urlencode(cleaned_query_items)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, cleaned_query, parsed.fragment))
    except Exception:
        # Fallback to original URL if parsing fails.
        return db_url

class Config:
    # Security: No default secret key in production
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY and not IS_CLOUD_ENV:
        SECRET_KEY = 'dev-secret-key-change-in-production'
    
    # Database Configuration - Use persistent storage
    DATABASE_URL = _read_env(
        'DATABASE_URL',
        'POSTGRES_URL',
        'POSTGRES_PRISMA_URL',
        'POSTGRES_URL_NON_POOLING',
    )

    # Build URL from Supabase Postgres parts if full URL vars are not present.
    if not DATABASE_URL:
        pg_host = _read_env('POSTGRES_HOST')
        pg_user = _read_env('POSTGRES_USER')
        pg_password = _read_env('POSTGRES_PASSWORD')
        pg_database = _read_env('POSTGRES_DATABASE') or 'postgres'
        pg_port = _read_env('POSTGRES_PORT') or '5432'

        if pg_host and pg_user and pg_password:
            safe_user = quote_plus(pg_user)
            safe_password = quote_plus(pg_password)
            safe_database = quote_plus(pg_database)
            DATABASE_URL = (
                f"postgresql://{safe_user}:{safe_password}@{pg_host}:{pg_port}/{safe_database}"
                "?sslmode=require"
            )
    
    if DATABASE_URL:
        # Use provided database URL (for production)
        if DATABASE_URL.startswith('postgres://'):
            DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        DATABASE_URL = _sanitize_postgres_url(DATABASE_URL)
        SQLALCHEMY_DATABASE_URI = DATABASE_URL
        print(f"✅ Using persistent database (production)")
    else:
        # Fallback to SQLite. In serverless/cloud, use /tmp because /var/task is read-only.
        if IS_CLOUD_ENV:
            sqlite_tmp_path = '/tmp/missing_children.db'
            SQLALCHEMY_DATABASE_URI = f'sqlite:///{sqlite_tmp_path}'
            print(
                "⚠️ DATABASE_URL not set in cloud env; using ephemeral SQLite at /tmp/missing_children.db "
                "(data will reset)."
            )
        else:
            SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'missing_children.db')
            print("⚠️  Using SQLite database (development only)")
    
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        'pool_recycle': 300,
        'pool_pre_ping': True,
    } if DATABASE_URL else {}
    
    # Cloudinary Configuration
    CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME')
    CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY')
    CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET')
    
    # Removed Twilio Configuration (Switched to Telegram)
    
    # Google Maps API (optional - for better geocoding)
    GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY')
    
    # Telegram Bot (optional - for free alerts)
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
    
    # Discord Webhook (optional - for free alerts)
    DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')
    
    # File Upload Configuration
    UPLOAD_FOLDER = os.path.join(basedir, 'static', 'uploads')
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB max file size
    
    # Admin Credentials
    ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
    # Security: No default password in production
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
    if not ADMIN_PASSWORD and not IS_CLOUD_ENV:
        ADMIN_PASSWORD = 'admin123'
        
    # Admin security
    ADMIN_ACCESS_TOKEN = os.environ.get('ADMIN_ACCESS_TOKEN')  # optional secret to reach login
    ADMIN_MAX_FAILED_ATTEMPTS = int(os.environ.get('ADMIN_MAX_FAILED_ATTEMPTS', '5'))
    ADMIN_LOCKOUT_MINUTES = int(os.environ.get('ADMIN_LOCKOUT_MINUTES', '15'))
    
    # Police access token (for authorized personnel to report missing children)
    POLICE_ACCESS_TOKEN = os.environ.get('POLICE_ACCESS_TOKEN')
    if not POLICE_ACCESS_TOKEN and not IS_CLOUD_ENV:
        POLICE_ACCESS_TOKEN = 'police123'  # Default for development only
    
    # Police credentials (for login portal)
    POLICE_USERNAME = os.environ.get('POLICE_USERNAME', 'police')
    POLICE_PASSWORD = os.environ.get('POLICE_PASSWORD')
    if not POLICE_PASSWORD and not IS_CLOUD_ENV:
        POLICE_PASSWORD = 'police123'  # Default for development only
    
    # Environment
    ENV = os.environ.get('FLASK_ENV', 'production')
    DEBUG = ENV == 'development'

    @staticmethod
    def check_production_security():
        """Check for security misconfigurations in production"""
        if IS_CLOUD_ENV:
            if not os.environ.get('SECRET_KEY'):
                print("🚨 CRITICAL: SECRET_KEY not set in production!")
            if not os.environ.get('ADMIN_PASSWORD'):
                print("🚨 CRITICAL: ADMIN_PASSWORD not set in production!")

