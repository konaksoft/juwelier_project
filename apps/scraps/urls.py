from django.urls import path
from .views import *

app_name = 'scraps'

urlpatterns = [
    # ── Altın Hurda Yönetimi (default) ──
    path('index', scrap_index, name='index'),

    # ── Gümüş Yönetimi (ONARIM FAZI 4 / ADIM 4 — izole sayfa) ──
    # Aynı endpoint'leri kullanır; template `view_material_type='SILVER'`
    # olarak set edilir → tüm POST/GET çağrıları gümüşe sabitlenir.
    path('silver/index', silver_index, name='silver_index'),

    # Ortak endpoint'ler (material_type query/POST ile filtre yapılır)
    path('add', scrap_add, name='add'),
    path('delete', delete, name='delete'),
    path('get-pool-sources', get_pool_sources, name='get-pool-sources'),
    path('get-pool-contents', get_pool_contents, name='get-pool-contents'),
    path('change-status', change_status, name='change-status'),
    path('get-all', get_all, name='get_all'),

    # ── ONARIM FAZI 9 — Duplicate havuz birleştirme (operatör tetikli) ──
    path('merge-duplicates', merge_scrap_duplicates_view, name='merge_duplicates'),
]
