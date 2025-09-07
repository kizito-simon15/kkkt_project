# accounts/middleware.py
from django.urls import reverse
from django.utils.deprecation import MiddlewareMixin

from .models import LoginHistory

def _safe_reverse(name):
    try:
        return reverse(name)
    except Exception:
        return None

def _ignored_paths():
    # Keep in sync with accounts.views:get_ignored_paths, but avoid importing views to prevent cycles
    names = ["login", "request_account", "forgot_password", "welcome", "public_news_list"]
    return {p for p in (_safe_reverse(n) for n in names) if p}

class LastPathMiddleware(MiddlewareMixin):
    """
    Stores the user's last visited path in:
      - session['last_visited_path']
      - their latest LoginHistory.last_visited_path

    Skips auth pages, static/media, and non-GETs to avoid noise.
    """
    def process_response(self, request, response):
        try:
            user = getattr(request, "user", None)
            path = getattr(request, "path", "")

            if (
                user
                and user.is_authenticated
                and request.method == "GET"
                and 200 <= getattr(response, "status_code", 200) < 400
                and path
                and not path.startswith("/static/")
                and not path.startswith("/media/")
                and path not in _ignored_paths()
            ):
                # Save in session for login redirection
                request.session["last_visited_path"] = path

                # Update the latest login record
                lh = (
                    LoginHistory.objects
                    .filter(user=user)
                    .order_by("-login_time")
                    .first()
                )
                if lh and lh.last_visited_path != path:
                    lh.last_visited_path = path
                    lh.save(update_fields=["last_visited_path"])
        except Exception:
            # Never block responses due to logging issues
            pass

        return response
