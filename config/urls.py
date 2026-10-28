from django.urls import path
from planner import views

urlpatterns = [
    path("", views.index, name="index"),
    path("health/", views.health, name="health"),
    path("api/route/", views.plan_route, name="plan-route"),
    path("api/schema/", views.schema, name="schema"),
    path("api/coverage/", views.coverage, name="coverage"),
    path("assets/us-states.geojson", views.state_basemap, name="state-basemap"),
    path("assets/leaflet-1.9.4/<str:asset>", views.leaflet_asset, name="leaflet-asset"),
    path("api/plans/<uuid:plan_id>/map/", views.route_map, name="route-map"),
]
