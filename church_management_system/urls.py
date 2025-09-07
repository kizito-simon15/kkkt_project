from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from accounts.views import login_view  # ← import login view (not welcome)
from django.conf.urls.i18n import i18n_patterns

urlpatterns = [
    path('admin/', admin.site.urls),

    # Root → login
    path('', login_view, name='root_login'),

    # App routes
    path('accounts/', include('accounts.urls')),
    path('settings/', include('settings.urls')),
    path('members/', include('members.urls')),
    path('leaders/', include('leaders.urls')),
    path('news/', include('news.urls')),
    path('finance/', include('finance.urls')),
    path('sacraments/', include('sacraments.urls')),
    path('properties/', include('properties.urls')),
    path('notifications/', include('notifications.urls')),
    path('sms/', include('sms.urls')),
    path('churchmember/', include('churchmember.urls')),
    path('secretary/', include('secretary.urls')),
    path('accountant/', include('accountant.urls')),
    path('analysis/', include('analysis.urls')),
    path('i18n/', include('django.conf.urls.i18n')),
    path('languages/', include('languages.urls')),
    path('pastor/', include('pastor.urls')),
    path('evengelist/', include('evangelist.urls')),  # (typo kept if intentional)
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
