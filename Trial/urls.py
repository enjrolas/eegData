from django.contrib import admin
from django.urls import path, include
from . import views

urlpatterns = [
  path('', views.home, name='home'),
  path('jigsaw', views.jigsaw, name='jigsaw'),
  path('data', views.jsonRecorder, name='jsonRecorder'),
  path('newSubject', views.newSubject, name='newSubject'),
]
