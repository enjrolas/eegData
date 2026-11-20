from django.urls import path
from . import views

urlpatterns = [
    path('<int:trial_id>', views.viewer, name='edfviewer'),
    path('api/<int:trial_id>/channels', views.channel_list, name='edfviewer_channels'),
    path('api/<int:trial_id>/signals', views.signal_data, name='edfviewer_signals'),
    path('api/<int:trial_id>/allsignals', views.all_signal_data, name='edfviewer_allsignals'),
]
