"""WSGI entrypoint compatibility module.

Allows Render start command `gunicorn your_application.wsgi` to boot the app.
"""

from app import app

__all__ = ["app"]
