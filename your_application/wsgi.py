"""WSGI entrypoint compatibility module.

Allows Render start command `gunicorn your_application.wsgi` to boot the app.
"""

from app import app

# Some platforms default to looking for `application` when only a module path is provided.
application = app

__all__ = ["app", "application"]
