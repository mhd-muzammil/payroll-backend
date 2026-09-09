"""Remember which build of the app each phone is running.

The APK is passed around as a file -- there is no store, and nothing on this
server ever sees an install -- so "who has the new version" was unanswerable.
The app now names its build on every request it makes, and this writes that
down against the person making it.

Written on CHANGE only. An engineer's phone talks to this server every thirty
seconds while they are on duty, and a row we already know the answer for is not
worth a write; the timestamp then means "since when on this build", which is
the more useful reading anyway.

Never breaks a request. This is bookkeeping about the app, and no page should
fail to load because a version string could not be saved.
"""

import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

HEADER = "HTTP_X_PAYROLL_APP_VERSION"
# Long enough for "1.4 (5)" many times over, short enough that a junk header
# cannot fill a column.
MAX_LENGTH = 40


class RecordAppVersion:
    """Middleware: note the app build a signed-in engineer is calling from.

    Placed on the way OUT rather than in: the user is resolved by the view's
    authentication, so on the way in there is nobody to attribute it to.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._remember(request)
        except Exception:  # noqa: BLE001 - never cost anybody a page
            logger.exception("could not record the app version")
        return response

    @staticmethod
    def _remember(request):
        version = (request.META.get(HEADER) or "").strip()[:MAX_LENGTH]
        if not version:
            return
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return
        if getattr(user, "app_version", None) == version:
            return
        # update(), not save(): no signals, no race with whatever else the
        # request wrote to this row, and one statement.
        type(user).objects.filter(pk=user.pk).update(
            app_version=version, app_version_at=timezone.now()
        )
