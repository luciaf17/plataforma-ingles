from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

from core.views import protected_media

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("lessons.urls")),
    path("", include("core.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
else:
    # Lesson audio lives on a volume; in production Django serves it behind login.
    # Single user, small files: fine for v1 (spec 13, module 10).
    urlpatterns += [path("media/<path:path>", protected_media, name="media")]
