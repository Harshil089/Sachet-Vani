import os
from dotenv import load_dotenv

basedir = os.path.abspath(os.path.dirname(__file__))

# Only load .env in development
if not os.environ.get('RENDER'):
    load_dotenv(os.path.join(basedir, '.env'))

class Config:
    # Security: No default secret key in production
    SECRET_KEY = os.environ.get('SECRET_KEY')
    if not SECRET_KEY and not os.environ.get('RENDER'):
        SECRET_KEY = 'dev-secret-key-change-in-production'
    
    # Database Configuration - Use persistent storage
    DATABASE_URL = os.environ.get('DATABASE_URL')
    
    if DATABASE_URL:
        # Use provided database URL (for production)
        if DATABASE_URL.startswith('postgres://'):
            DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        SQLALCHEMY_DATABASE_URI = DATABASE_URL
        print(f"✅ Using persistent database (production)")
    else:
        # Fallback to SQLite for local development
        SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(basedir, 'missing_children.db')
        print(f"⚠️  Using SQLite database (development only - data will reset on deployment)")
    
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
    if not ADMIN_PASSWORD and not os.environ.get('RENDER'):
        ADMIN_PASSWORD = 'admin123'
        
    # Admin security
    ADMIN_ACCESS_TOKEN = os.environ.get('ADMIN_ACCESS_TOKEN')  # optional secret to reach login
    ADMIN_MAX_FAILED_ATTEMPTS = int(os.environ.get('ADMIN_MAX_FAILED_ATTEMPTS', '5'))
    ADMIN_LOCKOUT_MINUTES = int(os.environ.get('ADMIN_LOCKOUT_MINUTES', '15'))
    
    # Police access token (for authorized personnel to report missing children)
    POLICE_ACCESS_TOKEN = os.environ.get('POLICE_ACCESS_TOKEN')
    if not POLICE_ACCESS_TOKEN and not os.environ.get('RENDER'):
        POLICE_ACCESS_TOKEN = 'police123'  # Default for development only
    
    # Police credentials (for login portal)
    POLICE_USERNAME = os.environ.get('POLICE_USERNAME', 'police')
    POLICE_PASSWORD = os.environ.get('POLICE_PASSWORD')
    if not POLICE_PASSWORD and not os.environ.get('RENDER'):
        POLICE_PASSWORD = 'police123'  # Default for development only
    
    # Environment
    ENV = os.environ.get('FLASK_ENV', 'production')
    DEBUG = ENV == 'development'

    @staticmethod
    def check_production_security():
        """Check for security misconfigurations in production"""
        if os.environ.get('RENDER'):
            if not os.environ.get('SECRET_KEY'):
                print("🚨 CRITICAL: SECRET_KEY not set in production!")
            if not os.environ.get('ADMIN_PASSWORD'):
                print("🚨 CRITICAL: ADMIN_PASSWORD not set in production!")

