from django.urls import path
from . import views
app_name = "dataEndpoint" 
urlpatterns = [
  path('', views.home, name='home'),
]
