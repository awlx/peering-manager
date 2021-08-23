from rest_framework import routers

from . import views


class ExtrasRootView(routers.APIRootView):
    """
    Extras API root view
    """

    def get_view_name(self):
        return "Extras"


router = routers.DefaultRouter()
router.APIRootView = ExtrasRootView

router.register("_choices", views.ExtrasFieldChoicesViewSet, basename="field-choice")

router.register("ix-api", views.IXAPIViewSet)
router.register("job-results", views.JobResultViewSet)
router.register("webhooks", views.WebhookViewSet)

app_name = "extras-api"
urlpatterns = router.urls
