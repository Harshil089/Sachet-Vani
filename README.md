# Missing Child Alert System

🚨 **Missing Child Alert System** is a secure, scalable, and user-friendly web application designed to help communities, law enforcement, and volunteers collaborate in finding missing children quickly and effectively. Using real-time case reporting, sighting updates, SMS alerts, and advanced predictive analytics, this system maximizes the impact of every report to bring children home safely.

---

## Features

- **Report Missing Children**: Easy-to-use form allowing caregivers or officials to report missing children, including photos, voice recordings, and location.
- **Report Sightings**: Community members can report sightings with details and location, helping to locate children faster.
- **Real-time SMS Alerts**: Immediate notifications are sent to verified users via Twilio SMS when new cases or sightings are reported.
- **Cloudinary Integration**: Secure, persistent cloud storage for photos and audio preventing data loss across deployments.
- **PostgreSQL Database**: Robust persistent data storage ensuring all case data remains intact.
- **Admin Portal**: Secure dashboard for managing cases, updating statuses, and deleting records.
- **Predictive Analytics**: Identifies high-risk zones and demographic patterns to proactively focus search efforts.
- **Interactive Maps**: Visualize last known locations and sightings using Leaflet.js with color-coded markers.
- **Responsive & Accessible UI**: Modern, clean, and mobile-friendly interface for ease of use by all demographics.
- **User Authentication**: Admin-only access for sensitive case management and data privacy.
- **Extensible Design**: Built with Flask to allow easy future feature integration.

---

## Demo
> Add your hosted app URL here once deployed.

---

## Tech Stack

- **Backend**: Python, Flask, Flask-Login, Flask-SQLAlchemy
- **Frontend**: Bootstrap 5, Leaflet.js, FontAwesome
- **Database**: PostgreSQL (hosted on Render)
- **Cloud Storage**: Cloudinary for media files
- **SMS Gateway**: Twilio for notifications
- **Deployment**: Render.com
- **Others**: Pillow for image processing, python-dotenv for environment management

---

## Getting Started

### Prerequisites

- Python 3.11+
- PostgreSQL instance or local SQLite (for development)
- Cloudinary account (for image/audio hosting)
- Twilio account (for SMS alerts)
- Git and GitHub account
- Render account for deployment (optional)

### Installation

1. Clone the repo:
    ```
    git clone https://github.com/yourusername/your-repo-name.git
    cd your-repo-name
    ```

2. Create and activate virtual environment:
    ```
    python3 -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3. Install dependencies:
    ```
    pip install -r requirements.txt
    ```

4. Configure environment variables:
    - Copy `.env.example` to `.env`
    - Fill in the required environment variables (database URL, Twilio credentials, Cloudinary keys, admin user, etc.)

5. Initialize the database:
    ```
    flask run  # First run will auto-create SQLite or connect to PostgreSQL tables
    ```

---

## Usage

- **Reporting missing children** available on the homepage.
- **Viewing and reporting sightings** accessible via case detail pages.
- **Admin login** available at `/admin/login` with credentials set in environment variables.
- Admins can **mark cases as found**, **close cases**, and **delete cases**.
- SMS alerts are sent automatically to predefined verified phone numbers.

---

## Deployment

This app is ready to deploy on Render or similar cloud platforms.

- Use the provided `render.yaml` (or equivalent Render dashboard configuration).
- Render dependency set is isolated in `requirements-render.txt`.
- Render environment template is isolated in `.env.render.example`.
- Set Render Build Command to `pip install -r requirements-render.txt`.
- Specify Python version to 3.11 in Render settings or runtime.txt.
- Set all required environment variables in Render dashboard.
- Use PostgreSQL for permanent data persistence.
- Cloudinary setup ensures media files persist securely.

### Vercel + Redis Quick Start

1. Connect the repository to Vercel.
2. Set build/output using the included [vercel.json](vercel.json) and entrypoint [api/index.py](api/index.py).
3. Add environment variables in Vercel project settings:
- `POSTGRES_URL` (preferred), or `POSTGRES_PRISMA_URL`, or `POSTGRES_URL_NON_POOLING`
- Optional URL aliases also supported: `DATABASE_INTERNAL_URL`, `DATABASE_URL`
- If URL vars are not set, the app can build a connection string from: `POSTGRES_HOST`/`PGHOST`, `POSTGRES_USER`/`PGUSER`, `POSTGRES_PASSWORD`/`PGPASSWORD`, `POSTGRES_DATABASE`/`PGDATABASE`, optional `POSTGRES_PORT`/`PGPORT`
- `SECRET_KEY`
- `ADMIN_PASSWORD`
- `POLICE_PASSWORD`
- `CLOUDINARY_URL`
- `REDIS_URL` (preferred) or `KV_URL` (Upstash)
- `ML_CACHE_TTL_SECONDS` (optional, default `86400`)
- `ML_SERVICE_URL` (base URL of external ML API service)
- `ML_SERVICE_TOKEN` (optional shared secret)
- `ML_SERVICE_TIMEOUT_SECONDS` (optional, default `30`)
- `VERCEL=1`
4. Deploy.

Notes:
- ML cache now uses Redis when `REDIS_URL` is available; local in-memory cache is used as fallback.
- This keeps case-level ML predictions reusable across serverless invocations and instances.

External ML service flow on serverless:
- Vercel app first checks ML cache.
- On cache miss, it calls external endpoints:
    - `POST <ML_SERVICE_URL>/api/ml/predict`
    - `POST <ML_SERVICE_URL>/api/ml/refine`
- Returned results are stored in Redis cache and then rendered on case maps/summaries.
- If `ML_SERVICE_TOKEN` is set, external ML endpoints require this token via `Authorization: Bearer <token>` or `X-ML-Service-Token`.

Serverless dependency note:
- Vercel/Lambda has a strict ephemeral dependency size limit.
- Default `requirements.txt` is intentionally slim to keep deployment under limit.
- Full ML dependencies are listed in `requirements-ml.txt` for local or non-serverless environments.

---

## Folder Structure

