from django.urls import path

from apps.crm.leads.views import *

app_name = 'leads'

urlpatterns = [
    path('index', leads_view, name='index'),
    path('add', add_lead, name='add'),
    path('delete', delete, name='delete'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get-all'),
    path('get-leads', get_leads, name='get-leads'),
    path('detail/<uuid:lead_id>', lead_detail_view, name='detail'),
    path('detail/<uuid:lead_id>/note/add', add_note, name='note-add'),
    path('detail/<uuid:lead_id>/tag/add', add_tag, name='tag-add'),
    path('detail/<uuid:lead_id>/tag/remove', remove_tag, name='tag-remove'),
    path('detail/<uuid:lead_id>/stage/change', change_stage, name='change-stage'),
    path('detail/<uuid:lead_id>/state/change', change_state, name='change-state'),
    path('import/', import_csv, name='import'),
    path('quick-update', quick_field_update, name='quick-update'),
    path('add-activity/<uuid:lead_id>/', add_activity, name='add-activity'),

    path('applications/', applications_view, name='applications'),
    path('applications/get-all', applications_get_all, name='applications-get-all'),
    path('applications/detail/<uuid:pk>', application_detail_view, name='application-detail'),
    path('applications/update-status', application_update_status, name='application-update-status'),
    path('applications/convert-to-proposal', application_convert_to_proposal, name='application-convert-to-proposal'),
]
