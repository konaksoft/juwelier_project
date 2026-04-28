from django.urls import path

from apps.contact_forms.views import *

app_name = 'contact-forms'

urlpatterns = [
    path('index/', contact_forms_view, name='index'),
    path('add/', add_contact_form, name='add'),
    path('delete/', delete, name='delete'),
    path('change-status/', change_status, name='change-status'),
    path('get-all/', get_all, name='get-all'),
    path('iletisim/', contact_page, name='contact-page'),

]
