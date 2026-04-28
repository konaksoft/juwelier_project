from django.urls import path

from apps.kuyumplus.views import *
from apps.contact_forms.views import contact_page

app_name = 'kuyumplus'

urlpatterns = [
    path('index', index_view, name='index'),
    path('', index_view, name='index'),
    path('iletisim', contact_page, name='iletisim'),
    path('referanslar', reference_view, name='referanslar'),
    path('hakkimizda', about_view, name='hakkimizda'),
    path('fiyatlar', pricing_view, name='fiyatlar'),
    path('stok-takibi', stock_view, name='stok-takibi'),
    path('satis-yonetimi', order_view, name='satis-yonetimi'),
    path('raporlama', report_view, name='raporlama'),
    path('teknik-destek', support_view, name='teknik-destek'),
    path('egitim', education_view, name='egitim'),
    path('envanter-modul', inventory_view, name='envanter-modul'),

    path('cari-modul', current_view, name='cari-modul'),
    path('barkodlama-modul', barcode_view, name='barkodlama-modul'),
    path('perakende-modul', retail_view, name='perakende-modul'),
    path('toptan-modul', wholesale_view, name='toptan-modul'),
    path('masraf-modul', cost_view, name='masraf-modul'),
    path('kvkk', kvkk_view, name='kvkk'),
    path('kullanimkosullari', kullanimkosullari_view, name='kullanimkosullari'),
    path('referanslar', referanslar_view, name='referanslar'),
    path('efatura', efatura_view, name='efatura'),

    path('cihazlar', devices_view, name='cihazlar'),

    path('basvuru', basvuru_view, name='basvuru'),
    path('basvuru/gonder', basvuru_submit, name='basvuru-gonder'),
]
