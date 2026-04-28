from django.conf.urls.static import static
from django.urls import path, include
from rest_framework.authtoken.views import obtain_auth_token

from apps.accounts.views import login_view
from apps.contact_forms.views import *
from apps.dashboard.views import index_view as portal_index
from apps.kuyumplus.views import index_view as theme_index

urlpatterns = [
    path('admin/', portal_index, name='admin'),
    path('api/auth/token/', obtain_auth_token),
    path('', theme_index, name=''),
    path('whatsapp/', include('apps.whatsapp.urls'), name='whatsapp'),

    path('login/', login_view, name='login'),
    path('accounts/', include('apps.accounts.urls'), name='accounts'),
    path('suppliers/', include('apps.suppliers.urls'), name='suppliers'),
    path('dashboard/', include('apps.dashboard.urls'), name='dashboard'),
    path('kuyumplus/', include('apps.kuyumplus.urls'), name='kuyumplus'),
    path('locations/', include('apps.definitions.locations.urls'), name='locations'),
    path('categories/', include('apps.definitions.categories.urls'), name='categories'),
    path('sms-profiles/', include('apps.definitions.sms_profiles.urls'), name='sms-profiles'),
    path('email-profiles/', include('apps.definitions.email_profiles.urls'), name='email-profiles'),
    path('contracts/', include('apps.definitions.contracts.urls'), name='contracts'),
    path('stores/', include('apps.stores.urls'), name='stores'),
    path('currencies/', include('apps.definitions.currencies.urls'), name='currencies'),
    path('brands/', include('apps.definitions.brands.urls'), name='brands'),
    path('activity-logs/', include('apps.activity_logs.urls'), name='activity-logs'),
    path('roles/', include('apps.roles.urls'), name='roles'),
    path('customers/', include('apps.customers.urls'), name='customers'),
    path('products/', include('apps.products.urls'), name='products'),
    path('rates/', include('apps.definitions.rates.urls'), name='rates'),
    path('packages/', include('apps.crm.packages.urls'), name='packages'),
    path('gold-purchases/', include('apps.gold_purchases.urls'), name='gold-purchases'),
    path('scraps/', include('apps.scraps.urls'), name='scraps'),
    path('bracelets/', include('apps.bracelets.urls'), name='bracelets'),
    path('inventories/', include('apps.inventories.urls'), name='inventories'),
    path('transactions-board/', include('apps.transactions_board.urls'), name='transactions-board'),
    path('process/', include('apps.process.urls'), name='process'),
    path('repairs/', include('apps.repairs.urls'), name='repairs'),
    path('workshops/', include('apps.workshops.urls'), name='workshops'),
    path('counts/', include('apps.counts.urls'), name='counts'),
    path('custody/', include('apps.custody.urls'), name='custody'),
    path('contact-forms/', include('apps.contact_forms.urls'), name='contact-forms'),
    path('iletisim/', contact_page, name='contact-page'),
    path('leads/', include('apps.crm.leads.urls'), name='leads'),
    path('proposals/', include('apps.crm.proposals.urls'), name='proposals'),
    path('devices/', include('apps.crm.devices.urls'), name='devices'),
    path('invoices/', include('apps.invoices.urls'), name='invoices'),
    path('orders/', include('apps.orders.urls'), name='orders'),
    path('pavo/', include('apps.pavo.urls'), name='pavo'),
    path('backups/', include('apps.backups.urls'), name='backups'),
    path('testimonials/', include('apps.testimonials.urls'), name='testimonials'),
    path('settings/', include('apps.settings.urls'), name='settings'),
    path('masak/', include('apps.masak.urls'), name='masak'),
    path('live_board/', include('apps.live_board.urls'), name='live_board'),
    path('supports/', include('apps.supports.urls'), name='supports'),
    path('chambers/', include('apps.chambers.urls'), name='chambers'),
    path('banking/', include('apps.banking.urls'), name='banking'),
    path('banks/', include('apps.banking.bank_urls'), name='bank-management'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
