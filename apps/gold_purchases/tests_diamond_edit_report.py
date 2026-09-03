"""Pırlanta düzenleme + Detaylı Rapor regresyon paketi (2026-09-01).

Kapsam (görev maddeleri A–M):
    A. Almanya mağazasında (primary_currency=EUR) yeni Pırlanta → EUR default
    B. Yapılandırması olmayan / TRY mağazada varsayılan davranış
    C. USD kayıtlı Pırlanta → düzenlemede USD KORUNUR (zorla EUR'ya çevrilmez)
    D. Maliyet düzenlemede gelir ve kaydetmede korunur
    E. Düzenleme: taşlar yüklenir, ikinci ürün/barkod/stok OLUŞMAZ
    F. Düzenleme tedarikçi carisine İKİNCİ borç yazmaz
    G. Detaylı Rapor ürün grubu filtresi (material_type — string eşleşme DEĞİL)
    H. Tarih filtresi: başlangıç ve bitiş günü DAHİL
    I. Satılan + tarih: GERÇEK satış işlemi tarihi (Process.date) kullanılır
    J. Filtre kombinasyonu
    K. PDF ekranla AYNI filtre sonucunu üretir
    L. Maliyet yetkisi olmayan personel UI/API/PDF'de maliyet ALAMAZ
    M. Multi-tenant: başka mağazanın kaydı edit/report'tan GELMEZ

Çalıştırma (izole SQLite; CANLI DB'ye dokunmaz):
    PYTHONPATH=<scratch>:<repo> DJANGO_SETTINGS_MODULE=<sqlite_settings> \
    python manage.py test apps.gold_purchases.tests_diamond_edit_report
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone

from apps.gold_purchases.models import GoldPurchases
from apps.gold_purchases.views import (
    _report_day_range,
    summarize_cost_by_currency,
    user_can_view_cost,
)
from apps.process.models import Process
from apps.products.models import (
    DiamondDetail,
    DiamondStone,
    MaterialType,
    Products,
    WatchDetail,
)
from apps.roles.models import Permission, RoleDetail, Roles
from apps.settings.currency import (
    get_store_primary_currency,
    resolve_default_sale_currency,
)
from apps.settings.models import StoreConfiguration
from apps.stock_management.models import StockSnapshot
from apps.stores.models import Company, Stores
from apps.suppliers.models import SupplierLedger, Suppliers

User = get_user_model()

ADD_URL = '/gold-purchases/multi-material-add'
UPDATE_URL = '/gold-purchases/multi-material-update'
DETAILS_URL = '/gold-purchases/get-details'
REPORT_URL = '/gold-purchases/detailed-report'
PDF_URL = '/gold-purchases/export-detailed-report-pdf'

# Barkodlu Ürünler ekranının menü yetki kodu (role_required Katman 2).
GP_ABC_CODE = 'ABC1007D'


# ─────────────────────────────────────────────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────────────────────────────────────────────
def _make_store(store_id, primary_currency=None, title='Test Mağaza'):
    """Mağaza + StoreConfiguration üretir.

    NOT: apps/stores/signals.py mağaza oluşturulunca StoreConfiguration'ı
    OTOMATİK açar (primary_currency model default'u 'EUR'). Bu yüzden
    primary_currency=None geçildiğinde config satırı SİLİNİR — gerçekten
    yapılandırmasız mağaza senaryosunu kurmak için tek yol budur.
    """
    company = Company.objects.create()
    store = Stores.objects.create(store_id=store_id, company=company, title=title)
    if primary_currency is None:
        StoreConfiguration.objects.filter(store=store).delete()
    else:
        StoreConfiguration.objects.update_or_create(
            store=store, defaults={'primary_currency': primary_currency}
        )
    return store


def _make_superuser(store, username):
    user = User.objects.create_superuser(
        username=username, password='pw12345', email=f'{username}@test.local',
    )
    user.store = store
    user.save()
    return user


def _client_for(user):
    c = Client()
    c.force_login(user)
    return c


def _diamond_post_data(**overrides):
    data = {
        'material_type': 'DIAMOND',
        'jewelry_type': 'Yüzük',
        'mount_karat': '18K',
        'mount_gram': '3,50',
        'sale_price': '1200,00',
        'buy_price_eur': '800,00',
        'profit': '50',
        'stock_pieces': '1',
        'diamond_growth_type': 'NATURAL',
        'stone_type[]': 'DIAMOND',
        'stone_role[]': 'CENTER',
        'stone_position[]': '1',
        'stone_carat[]': '0,26',
        'stone_color[]': 'F',
        'stone_clarity[]': 'VS1',
        'stone_cut[]': 'EXCELLENT',
        'stone_cert_lab[]': 'GIA',
        'stone_cert_no[]': '',
    }
    data.update(overrides)
    return data


def _make_diamond(store, *, sale_currency='USD', sale_price='1000.00',
                  buy_price_eur='700.00', jewelry_type='Yüzük',
                  stones=2, created_on=None, price_currency=None,
                  is_sold=False, name='Pırlanta Yüzük'):
    """Doğrudan ORM ile kayıtlı bir Pırlanta ürünü + GoldPurchases üretir."""
    product = Products.objects.create(
        name=name,
        jewelry_type=jewelry_type,
        material_type=MaterialType.DIAMOND,
        store=store,
        barcode=f'PIR{Products.objects.count() + 1:05d}',
        buy_price_eur=Decimal(buy_price_eur),
        sale_price_eur=Decimal('0.00'),
        price_currency=(price_currency or sale_currency),
        is_completed=False,
    )
    dd, _ = DiamondDetail.objects.update_or_create(
        product=product,
        defaults=dict(
            sale_currency=sale_currency,
            sale_price=Decimal(sale_price),
            mount_karat='18K',
            mount_gram=Decimal('3.500'),
            growth_type='NATURAL',
        ),
    )
    for i in range(stones):
        DiamondStone.objects.create(
            diamond_detail=dd,
            stone_type='DIAMOND',
            role='CENTER' if i == 0 else 'SIDE',
            position=i + 1,
            carat_weight=Decimal('0.26') if i == 0 else Decimal('0.05'),
            shape='ROUND',
            color_grade='F',
            clarity_grade='VS1',
        )
    gp = GoldPurchases.objects.create(
        product=product, store=store,
        created_by=User.objects.filter(store=store).first(),
        is_status=not is_sold,
    )
    if created_on is not None:
        GoldPurchases.objects.filter(pk=gp.pk).update(created_on=created_on)
        gp.refresh_from_db()
    return product, dd, gp


def _make_gold(store, *, jewelry_type='Bilezik', gram='10.000',
               mileage='916', buy_price_hs='9.160', created_on=None,
               is_sold=False):
    product = Products.objects.create(
        name=jewelry_type,
        jewelry_type=jewelry_type,
        material_type=MaterialType.GOLD,
        store=store,
        barcode=f'ALT{Products.objects.count() + 1:05d}',
        gram=Decimal(gram),
        product_mileage=Decimal(mileage),
        buy_price_hs=Decimal(buy_price_hs),
        is_completed=False,
    )
    gp = GoldPurchases.objects.create(
        product=product, store=store,
        created_by=User.objects.filter(store=store).first(),
        is_status=not is_sold,
    )
    if created_on is not None:
        GoldPurchases.objects.filter(pk=gp.pk).update(created_on=created_on)
        gp.refresh_from_db()
    return product, gp


def _make_watch(store, *, jewelry_type='Saat', sale_currency='CHF'):
    product = Products.objects.create(
        name='Saat', jewelry_type=jewelry_type,
        material_type=MaterialType.WATCH, store=store,
        barcode=f'SAT{Products.objects.count() + 1:05d}',
        buy_price_eur=Decimal('500.00'),
        price_currency=sale_currency,
    )
    WatchDetail.objects.update_or_create(
        product=product,
        defaults=dict(brand='Rolex', sale_currency=sale_currency,
                      sale_price=Decimal('9000.00')),
    )
    gp = GoldPurchases.objects.create(
        product=product, store=store,
        created_by=User.objects.filter(store=store).first(), is_status=True,
    )
    return product, gp


# ═════════════════════════════════════════════════════════════════════════════
# A / B — YENİ KAYITTA PARA BİRİMİ VARSAYILANI (SSOT)
# ═════════════════════════════════════════════════════════════════════════════
class DefaultSaleCurrencyTest(TestCase):
    """Varsayılan, StoreConfiguration.primary_currency'den gelir.
    Ülke→para birimi hard-code'u YOKTUR."""

    def test_helper_reads_store_configuration(self):
        de_store = _make_store('DE0001', 'EUR')
        tr_store = _make_store('TR0001', 'TRY')
        no_cfg_store = _make_store('NC0001', None)

        self.assertEqual(get_store_primary_currency(de_store), 'EUR')
        self.assertEqual(get_store_primary_currency(tr_store), 'TRY')
        # Yapılandırma satırı yoksa çağıranın verdiği güvenli varsayılan döner.
        self.assertEqual(get_store_primary_currency(no_cfg_store, default='USD'), 'USD')
        self.assertEqual(get_store_primary_currency(None, default='USD'), 'USD')

    def test_resolver_falls_back_to_legacy_when_unsupported(self):
        # CHF, DiamondDetail.SaleCurrency listesinde YOK → eski varsayılan USD.
        chf_store = _make_store('CH0001', 'CHF')
        self.assertEqual(
            resolve_default_sale_currency(chf_store, ('USD', 'EUR', 'GBP', 'TRY')),
            'USD',
        )
        # Saat seçicisi CHF destekler → CHF uygulanır.
        self.assertEqual(
            resolve_default_sale_currency(chf_store, ('USD', 'EUR', 'GBP', 'CHF', 'TRY')),
            'CHF',
        )

    def test_A_germany_store_new_diamond_defaults_to_eur(self):
        store = _make_store('DE0002', 'EUR')
        user = _make_superuser(store, 'de_user')
        client = _client_for(user)

        data = _diamond_post_data()
        data.pop('sale_currency', None)  # form hiç göndermezse bile
        resp = client.post(ADD_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        body = resp.json()
        self.assertTrue(body.get('result'), body)

        dd = DiamondDetail.objects.get(product__barcode=body['barcode'])
        self.assertEqual(dd.sale_currency, 'EUR')
        self.assertEqual(dd.product.price_currency, 'EUR')

    def test_B_store_without_configuration_keeps_legacy_usd(self):
        """Yapılandırma satırı hiç yoksa modülün ESKİ varsayılanı (USD) korunur."""
        store = _make_store('NC0002', None)
        user = _make_superuser(store, 'nocfg_user')
        client = _client_for(user)

        data = _diamond_post_data()
        data.pop('sale_currency', None)
        resp = client.post(ADD_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd = DiamondDetail.objects.get(product__barcode=resp.json()['barcode'])
        self.assertEqual(dd.sale_currency, 'USD')

    def test_B_try_store_uses_its_own_primary_currency(self):
        store = _make_store('TR0002', 'TRY')
        user = _make_superuser(store, 'tr_user')
        client = _client_for(user)

        data = _diamond_post_data()
        data.pop('sale_currency', None)
        resp = client.post(ADD_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd = DiamondDetail.objects.get(product__barcode=resp.json()['barcode'])
        self.assertEqual(dd.sale_currency, 'TRY')

    def test_explicit_currency_always_wins_over_default(self):
        store = _make_store('DE0003', 'EUR')
        user = _make_superuser(store, 'de_user2')
        client = _client_for(user)

        resp = client.post(ADD_URL, _diamond_post_data(sale_currency='USD'),
                           HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd = DiamondDetail.objects.get(product__barcode=resp.json()['barcode'])
        self.assertEqual(dd.sale_currency, 'USD')


# ═════════════════════════════════════════════════════════════════════════════
# C / D / E / F — DÜZENLEME AKIŞI
# ═════════════════════════════════════════════════════════════════════════════
class DiamondEditFlowTest(TestCase):

    def setUp(self):
        self.store = _make_store('DE1000', 'EUR')
        self.user = _make_superuser(self.store, 'edit_user')
        self.client = _client_for(self.user)

    # ── get_details ─────────────────────────────────────────────────────
    def test_details_returns_material_type_and_diamond_payload(self):
        product, dd, gp = _make_diamond(self.store, sale_currency='USD', stones=3)
        resp = self.client.get(DETAILS_URL, {'id': str(gp.id)})
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        d = resp.json()
        self.assertTrue(d['result'])
        self.assertEqual(d['material_type'], 'DIAMOND')
        self.assertIn('diamond', d)
        self.assertEqual(d['barcode'], product.barcode)

    def test_E_details_returns_all_stone_rows(self):
        product, dd, gp = _make_diamond(self.store, stones=3)
        d = self.client.get(DETAILS_URL, {'id': str(gp.id)}).json()
        stones = d['diamond']['stones']
        self.assertEqual(len(stones), 3)
        self.assertEqual(stones[0]['role'], 'CENTER')
        self.assertEqual(stones[0]['position'], 1)
        self.assertEqual(stones[0]['carat_weight'].replace(',', '.'), '0.26')
        self.assertEqual(stones[1]['role'], 'SIDE')

    def test_C_existing_usd_record_is_not_forced_to_eur_on_edit(self):
        """Mağaza EUR olsa bile KAYITLI USD, düzenlemede USD gelir."""
        product, dd, gp = _make_diamond(self.store, sale_currency='USD')
        d = self.client.get(DETAILS_URL, {'id': str(gp.id)}).json()
        self.assertEqual(d['diamond']['sale_currency'], 'USD')

    def test_C_update_without_currency_keeps_stored_usd(self):
        product, dd, gp = _make_diamond(self.store, sale_currency='USD')
        data = _diamond_post_data(gold_purchase_id=str(gp.id))
        data.pop('sale_currency', None)
        resp = self.client.post(UPDATE_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd.refresh_from_db()
        self.assertEqual(dd.sale_currency, 'USD')

    def test_C_update_with_explicit_currency_changes_it(self):
        product, dd, gp = _make_diamond(self.store, sale_currency='USD')
        resp = self.client.post(
            UPDATE_URL,
            _diamond_post_data(gold_purchase_id=str(gp.id), sale_currency='EUR'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd.refresh_from_db()
        self.assertEqual(dd.sale_currency, 'EUR')

    # ── D: MALİYET ──────────────────────────────────────────────────────
    def test_D_cost_is_returned_on_edit(self):
        product, dd, gp = _make_diamond(self.store, buy_price_eur='1234.56')
        d = self.client.get(DETAILS_URL, {'id': str(gp.id)}).json()
        self.assertTrue(d['can_view_cost'])
        self.assertEqual(Decimal(d['buy_price_eur']), Decimal('1234.56'))
        self.assertEqual(d['cost_currency'], 'EUR')

    def test_D_cost_survives_update_unchanged(self):
        product, dd, gp = _make_diamond(self.store, buy_price_eur='1234.56')
        resp = self.client.post(
            UPDATE_URL,
            _diamond_post_data(gold_purchase_id=str(gp.id), buy_price_eur='1234,56'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        product.refresh_from_db()
        self.assertEqual(product.buy_price_eur, Decimal('1234.56'))

    def test_D_cost_is_not_wiped_when_field_absent(self):
        product, dd, gp = _make_diamond(self.store, buy_price_eur='999.00')
        data = _diamond_post_data(gold_purchase_id=str(gp.id))
        data.pop('buy_price_eur')
        resp = self.client.post(UPDATE_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        product.refresh_from_db()
        self.assertEqual(product.buy_price_eur, Decimal('999.00'))

    # ── E: UPDATE SEMANTİĞİ ─────────────────────────────────────────────
    def test_E_update_does_not_create_second_product_or_barcode(self):
        product, dd, gp = _make_diamond(self.store, stones=2)
        before_products = Products.objects.filter(store=self.store).count()
        before_gp = GoldPurchases.objects.filter(store=self.store).count()
        old_barcode, old_rfid = product.barcode, product.rfid_code

        resp = self.client.post(
            UPDATE_URL,
            _diamond_post_data(gold_purchase_id=str(gp.id), jewelry_type='Kolye'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        body = resp.json()
        self.assertTrue(body.get('updated'))
        self.assertEqual(body['product_id'], str(product.id))

        self.assertEqual(Products.objects.filter(store=self.store).count(), before_products)
        self.assertEqual(GoldPurchases.objects.filter(store=self.store).count(), before_gp)

        product.refresh_from_db()
        self.assertEqual(product.barcode, old_barcode)
        self.assertEqual(product.rfid_code, old_rfid)
        self.assertEqual(product.jewelry_type, 'Kolye')

    def test_E_update_does_not_touch_stock(self):
        product, dd, gp = _make_diamond(self.store)
        StockSnapshot.objects.update_or_create(
            product=product, store=self.store,
            defaults={'stock_pieces': 1},
        )
        resp = self.client.post(
            UPDATE_URL,
            _diamond_post_data(gold_purchase_id=str(gp.id), stock_pieces='5'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        snap = StockSnapshot.objects.get(product=product, store=self.store)
        self.assertEqual(snap.stock_pieces, 1)  # stock_pieces=5 YOK SAYILIR

    def test_E_update_replaces_stones_without_duplicating(self):
        product, dd, gp = _make_diamond(self.store, stones=2)
        self.assertEqual(DiamondStone.objects.filter(diamond_detail=dd).count(), 2)

        data = _diamond_post_data(gold_purchase_id=str(gp.id))
        data['stone_type[]'] = ['DIAMOND', 'DIAMOND', 'DIAMOND']
        data['stone_role[]'] = ['CENTER', 'SIDE', 'SIDE']
        data['stone_position[]'] = ['1', '2', '3']
        data['stone_carat[]'] = ['0,30', '0,10', '0,10']
        data['stone_color[]'] = ['D', 'F', 'F']
        data['stone_clarity[]'] = ['VVS1', 'VS1', 'VS1']
        data['stone_cut[]'] = ['EXCELLENT', 'GOOD', 'GOOD']
        data['stone_cert_lab[]'] = ['GIA', 'NONE', 'NONE']
        data['stone_cert_no[]'] = ['', '', '']

        resp = self.client.post(UPDATE_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        stones = DiamondStone.objects.filter(diamond_detail=dd).order_by('position')
        self.assertEqual(stones.count(), 3)
        self.assertEqual(stones[0].carat_weight, Decimal('0.300'))

    def test_E_update_rejects_empty_stone_payload(self):
        product, dd, gp = _make_diamond(self.store, stones=2)
        data = _diamond_post_data(gold_purchase_id=str(gp.id))
        data['stone_carat[]'] = '0'
        resp = self.client.post(UPDATE_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 400)
        # Mevcut taşlar SİLİNMEDİ
        self.assertEqual(DiamondStone.objects.filter(diamond_detail=dd).count(), 2)

    def test_E_update_cannot_change_material_type(self):
        product, dd, gp = _make_diamond(self.store)
        resp = self.client.post(
            UPDATE_URL,
            _diamond_post_data(gold_purchase_id=str(gp.id), material_type='WATCH'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 400)
        product.refresh_from_db()
        self.assertEqual(product.material_type, MaterialType.DIAMOND)

    def test_update_endpoint_rejects_gold_product(self):
        product, gp = _make_gold(self.store)
        resp = self.client.post(
            UPDATE_URL,
            {'material_type': 'DIAMOND', 'gold_purchase_id': str(gp.id)},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 400)

    def test_growth_type_unknown_is_not_overwritten_by_invalid_post(self):
        product, dd, gp = _make_diamond(self.store)
        DiamondDetail.objects.filter(pk=dd.pk).update(growth_type=None)
        data = _diamond_post_data(gold_purchase_id=str(gp.id))
        data['diamond_growth_type'] = ''  # form "Belirtilmemiş" gönderir
        resp = self.client.post(UPDATE_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd.refresh_from_db()
        self.assertIsNone(dd.growth_type)  # NATURAL'a ÇEVRİLMEDİ

    # ── F: TEDARİKÇİ CARİSİ ─────────────────────────────────────────────
    def test_F_update_does_not_post_second_supplier_ledger(self):
        supplier = Suppliers.objects.create(company_name='Test Tedarikçi', store=self.store)
        create_resp = self.client.post(
            ADD_URL,
            _diamond_post_data(
                supplier_id=str(supplier.id),
                process_supplier_ledger='on',
                sale_currency='EUR',
            ),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(create_resp.status_code, 200, create_resp.content[:400])
        gp_id = create_resp.json()['gold_purchase_id']
        product_id = create_resp.json()['product_id']

        ledger_before = SupplierLedger.objects.filter(product_id=product_id).count()
        process_before = Process.objects.filter(product_id=product_id).count()

        upd = self.client.post(
            UPDATE_URL,
            _diamond_post_data(
                gold_purchase_id=gp_id,
                supplier_id=str(supplier.id),
                process_supplier_ledger='on',  # işaretli GELSE BİLE
                sale_currency='EUR',
            ),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(upd.status_code, 200, upd.content[:400])
        self.assertEqual(SupplierLedger.objects.filter(product_id=product_id).count(),
                         ledger_before)
        self.assertEqual(Process.objects.filter(product_id=product_id).count(),
                         process_before)

    def test_has_supplier_ledger_flag_exposed(self):
        supplier = Suppliers.objects.create(company_name='T2', store=self.store)
        resp = self.client.post(
            ADD_URL,
            _diamond_post_data(supplier_id=str(supplier.id),
                               process_supplier_ledger='on', sale_currency='EUR'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        gp_id = resp.json()['gold_purchase_id']
        d = self.client.get(DETAILS_URL, {'id': gp_id}).json()
        self.assertTrue(d['has_supplier_ledger'])


# ═════════════════════════════════════════════════════════════════════════════
# M — MULTI-TENANT İZOLASYON
# ═════════════════════════════════════════════════════════════════════════════
class MultiTenantIsolationTest(TestCase):

    def setUp(self):
        self.store_a = _make_store('MTA001', 'EUR', title='A Mağaza')
        self.store_b = _make_store('MTB001', 'EUR', title='B Mağaza')
        self.user_a = _make_superuser(self.store_a, 'mt_user_a')
        self.user_b = _make_superuser(self.store_b, 'mt_user_b')
        self.client_a = _client_for(self.user_a)

    def test_M_details_of_other_store_returns_404(self):
        _p, _dd, gp_b = _make_diamond(self.store_b)
        resp = self.client_a.get(DETAILS_URL, {'id': str(gp_b.id)})
        self.assertEqual(resp.status_code, 404)

    def test_M_update_of_other_store_is_rejected(self):
        product_b, dd_b, gp_b = _make_diamond(self.store_b, jewelry_type='Yüzük')
        resp = self.client_a.post(
            UPDATE_URL,
            _diamond_post_data(gold_purchase_id=str(gp_b.id), jewelry_type='ÇALINDI'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 404)
        product_b.refresh_from_db()
        self.assertEqual(product_b.jewelry_type, 'Yüzük')

    def test_M_update_by_product_id_of_other_store_is_rejected(self):
        product_b, dd_b, gp_b = _make_diamond(self.store_b)
        data = _diamond_post_data(product_id=str(product_b.id))
        resp = self.client_a.post(UPDATE_URL, data, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(resp.status_code, 404)

    def test_M_report_excludes_other_store_products(self):
        _make_diamond(self.store_a, jewelry_type='A-Yüzük')
        _make_diamond(self.store_b, jewelry_type='B-Yüzük')
        rows = self.client_a.get(REPORT_URL).json()['data']
        cats = {r['category'] for r in rows}
        self.assertIn('A-Yüzük', cats)
        self.assertNotIn('B-Yüzük', cats)

    def test_M_gold_add_update_path_is_store_scoped(self):
        """gold_purchase_add UPDATE yolu (IDOR yaması)."""
        product_b, gp_b = _make_gold(self.store_b, jewelry_type='B-Bilezik')
        resp = self.client_a.post('/gold-purchases/add', {
            'gold_purchase_id': str(product_b.id),
            'jewelry_type': 'ÇALINDI',
            'gram': '1',
            'product_mileage': '916',
        })
        self.assertEqual(resp.status_code, 404)
        product_b.refresh_from_db()
        self.assertEqual(product_b.jewelry_type, 'B-Bilezik')


# ═════════════════════════════════════════════════════════════════════════════
# G / H / I / J / K — DETAYLI RAPOR
# ═════════════════════════════════════════════════════════════════════════════
class DetailedReportFilterTest(TestCase):

    def setUp(self):
        self.store = _make_store('RP0001', 'EUR')
        self.user = _make_superuser(self.store, 'report_user')
        self.client = _client_for(self.user)

        self.now = timezone.now()
        self.d_jan = self.now - timedelta(days=200)
        self.d_recent = self.now - timedelta(days=3)

        # Altın (tezgahta, 3 gün önce girdi)
        self.gold_p, self.gold_gp = _make_gold(
            self.store, jewelry_type='Bilezik', created_on=self.d_recent
        )
        # Pırlanta (tezgahta, 3 gün önce girdi), EUR maliyet
        self.dia_p, self.dia_dd, self.dia_gp = _make_diamond(
            self.store, jewelry_type='Pırlanta Yüzük',
            sale_currency='EUR', buy_price_eur='800.00',
            created_on=self.d_recent, price_currency='EUR',
        )
        # Saat (tezgahta)
        self.watch_p, self.watch_gp = _make_watch(self.store, jewelry_type='Kol Saati')

    def _rows(self, **params):
        resp = self.client.get(REPORT_URL, params)
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.json()

    # ── G: ÜRÜN GRUBU ───────────────────────────────────────────────────
    def test_G_group_filter_diamond_only(self):
        rows = self._rows(product_group='DIAMOND')['data']
        self.assertTrue(rows)
        self.assertTrue(all(r['material_type'] == 'DIAMOND' for r in rows))

    def test_G_group_filter_watch_only(self):
        rows = self._rows(product_group='WATCH')['data']
        self.assertTrue(rows)
        self.assertTrue(all(r['material_type'] == 'WATCH' for r in rows))

    def test_G_group_filter_gold_only(self):
        rows = self._rows(product_group='GOLD')['data']
        self.assertTrue(rows)
        self.assertTrue(all(r['material_type'] == 'GOLD' for r in rows))

    def test_G_group_filter_is_not_name_based(self):
        """Adında 'Pırlanta' geçen bir ALTIN ürünü, Pırlanta grubuna GİRMEZ."""
        _make_gold(self.store, jewelry_type='Pırlanta Görünümlü Bilezik',
                   created_on=self.d_recent)
        rows = self._rows(product_group='DIAMOND')['data']
        cats = {r['category'] for r in rows}
        self.assertNotIn('Pırlanta Görünümlü Bilezik', cats)

    def test_G_invalid_group_is_ignored(self):
        all_rows = self._rows()['data']
        bogus = self._rows(product_group='PLATINUM')['data']
        self.assertEqual(len(bogus), len(all_rows))

    # ── H: TARİH (başlangıç/bitiş DAHİL) ────────────────────────────────
    def test_H_day_range_is_inclusive_on_both_ends(self):
        start, end = _report_day_range('01.08.2026', '31.08.2026')
        self.assertEqual(start.strftime('%Y-%m-%d %H:%M'), '2026-08-01 00:00')
        # Bitiş üst sınırı EXCLUSIVE 01.09 00:00 → 31.08 23:59:59 DAHİL
        self.assertEqual(end.strftime('%Y-%m-%d %H:%M'), '2026-09-01 00:00')

    def test_H_shelf_row_included_on_start_day(self):
        day = (self.d_recent).strftime('%d.%m.%Y')
        rows = self._rows(product_group='DIAMOND', date_from=day, date_to=day)['data']
        self.assertTrue(rows, 'Giriş günü (başlangıç=bitiş) DAHİL olmalı')
        self.assertEqual(sum(r['tezgahta_count'] for r in rows), 1)

    def test_H_shelf_row_excluded_outside_range(self):
        far = (self.now - timedelta(days=100)).strftime('%d.%m.%Y')
        rows = self._rows(product_group='DIAMOND', date_from=far, date_to=far)['data']
        self.assertEqual(rows, [])

    # ── I: SATILAN → GERÇEK SATIŞ TARİHİ ────────────────────────────────
    def test_I_sold_uses_real_sale_transaction_date_not_update_time(self):
        """Ocak'ta satılan ürün, Ağustos'ta güncellense bile Ağustos satışı
        gibi görünmemeli."""
        product, dd, gp = _make_diamond(
            self.store, jewelry_type='Satılan Yüzük',
            created_on=self.d_jan, price_currency='EUR',
        )
        Products.objects.filter(pk=product.pk).update(is_completed=True)
        GoldPurchases.objects.filter(pk=gp.pk).update(is_status=False)
        sale_dt = self.d_jan + timedelta(days=1)
        Process.objects.create(
            store=self.store, product=product, process_type='RETAIL',
            transaction_type='SALE', is_status='COMPLETED', is_deleted=False,
            piece=1, amount=Decimal('1200.00'), unit_price=Decimal('1200.00'),
            date=sale_dt,
        )
        # Ürün BUGÜN güncellendi (updated_on/auto_now alanları değişti)
        dd.save()

        sale_day = sale_dt.strftime('%d.%m.%Y')
        rows = self._rows(product_group='DIAMOND', status='satilan',
                          date_from=sale_day, date_to=sale_day)['data']
        self.assertEqual(sum(r['satilan_count'] for r in rows), 1)

        # Bugünkü aralıkta GÖRÜNMEMELİ (updated_at kullanılmıyor)
        today = self.now.strftime('%d.%m.%Y')
        rows_today = self._rows(product_group='DIAMOND', status='satilan',
                                date_from=today, date_to=today)['data']
        self.assertEqual(sum(r['satilan_count'] for r in rows_today), 0)

    def test_I_sold_without_sale_process_is_excluded_from_date_range(self):
        product, dd, gp = _make_diamond(
            self.store, jewelry_type='İşlemsiz Satılan',
            created_on=self.d_jan, price_currency='EUR',
        )
        GoldPurchases.objects.filter(pk=gp.pk).update(is_status=False)
        today = self.now.strftime('%d.%m.%Y')
        rows = self._rows(status='satilan', date_from=today, date_to=today)['data']
        cats = {r['category'] for r in rows}
        self.assertNotIn('İşlemsiz Satılan', cats)

    # ── J: KOMBİNASYON ──────────────────────────────────────────────────
    def test_J_group_status_and_date_combine(self):
        day = self.d_recent.strftime('%d.%m.%Y')
        rows = self._rows(product_group='DIAMOND', status='tezgahta',
                          date_from=day, date_to=day)['data']
        self.assertTrue(rows)
        self.assertTrue(all(r['material_type'] == 'DIAMOND' for r in rows))
        self.assertTrue(all(r['tezgahta_count'] > 0 for r in rows))

    def test_J_category_search_combines_with_group(self):
        rows = self._rows(product_group='DIAMOND', category_q='pırlanta')['data']
        self.assertTrue(rows)
        self.assertTrue(all('Pırlanta' in r['category'] for r in rows))
        empty = self._rows(product_group='GOLD', category_q='pırlanta yüzük')['data']
        self.assertEqual(empty, [])

    # ── MALİYET / PARA BİRİMİ ───────────────────────────────────────────
    def test_diamond_cost_is_reported_in_money_not_has(self):
        payload = self._rows(product_group='DIAMOND')
        row = payload['data'][0]
        self.assertEqual(row['cost_unit'], 'EUR')
        self.assertFalse(row['cost_is_metal'])
        self.assertEqual(row['_raw_tezgahta_maliyet'], 800.0)

    def test_cost_totals_do_not_mix_currencies(self):
        # USD maliyetli ikinci bir pırlanta ekle
        _make_diamond(self.store, jewelry_type='USD Yüzük',
                      sale_currency='USD', price_currency='USD',
                      buy_price_eur='500.00', created_on=self.d_recent)
        totals = self._rows(product_group='DIAMOND')['cost_totals']
        units = {t['unit'] for t in totals}
        self.assertIn('EUR', units)
        self.assertIn('USD', units)
        eur = next(t for t in totals if t['unit'] == 'EUR')
        usd = next(t for t in totals if t['unit'] == 'USD')
        self.assertEqual(eur['_raw_tezgahta'], 800.0)
        self.assertEqual(usd['_raw_tezgahta'], 500.0)

    def test_summarizer_keeps_metal_and_money_separate(self):
        totals = self._rows()['cost_totals']
        units = {t['unit'] for t in totals}
        self.assertIn('HAS', units)   # altın
        self.assertIn('EUR', units)   # pırlanta/saat

    # ── K: PDF = EKRAN ──────────────────────────────────────────────────
    def test_K_pdf_uses_same_filters_as_screen(self):
        params = {'product_group': 'DIAMOND', 'status': 'tezgahta'}
        screen_rows = self._rows(**params)['data']
        resp = self.client.get(PDF_URL, params)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertGreater(len(resp.content), 500)

        # Aynı filtre SSOT'u: view fonksiyonlarının ürettiği satırlar eşit
        from apps.gold_purchases.views import (
            _build_detailed_report_rows, parse_detailed_report_filters,
        )

        class _FakeReq:
            GET = params

        pdf_rows = _build_detailed_report_rows(
            self.store, parse_detailed_report_filters(_FakeReq()), include_cost=True
        )
        self.assertEqual(
            [(r['category'], r['tezgahta_count']) for r in pdf_rows],
            [(r['category'], r['tezgahta_count']) for r in screen_rows],
        )

    def test_K_pdf_group_filter_actually_narrows(self):
        all_rows = self._rows()['data']
        dia_rows = self._rows(product_group='DIAMOND')['data']
        self.assertLess(len(dia_rows), len(all_rows))


# ═════════════════════════════════════════════════════════════════════════════
# L — MALİYET GÖRÜNÜRLÜĞÜ (RBAC)  — AYRICALIKSIZ AKTÖR
# ═════════════════════════════════════════════════════════════════════════════
class CostVisibilityPermissionTest(TestCase):
    """Bu testler superuser ile KOŞULMAZ; is_superuser kısa devresi kapıyı
    baştan bypass eder ve testi anlamsızlaştırır."""

    def setUp(self):
        self.store = _make_store('PM0001', 'EUR')
        self.perm = Permission.objects.create(code=GP_ABC_CODE, name='Barkodlu Ürünler')
        self.role_with = Roles.objects.create(name='Yetkili', category='STORE')
        RoleDetail.objects.create(role=self.role_with, permission=self.perm, status=True)
        self.role_without = Roles.objects.create(name='Yetkisiz', category='STORE')

        self.user_ok = User.objects.create_user(
            username='staff_ok', password='pw12345', email='ok@test.local',
        )
        self.user_ok.store = self.store
        self.user_ok.role = self.role_with
        self.user_ok.is_superuser = False
        self.user_ok.save()

        self.user_no = User.objects.create_user(
            username='staff_no', password='pw12345', email='no@test.local',
        )
        self.user_no.store = self.store
        self.user_no.role = self.role_without
        self.user_no.is_superuser = False
        self.user_no.save()

        _make_diamond(self.store, jewelry_type='Yüzük', buy_price_eur='800.00',
                      price_currency='EUR')

    def test_helper_reflects_role_detail(self):
        self.assertTrue(user_can_view_cost(self.user_ok))
        self.assertFalse(user_can_view_cost(self.user_no))

    def test_L_authorized_staff_sees_cost(self):
        client = _client_for(self.user_ok)
        payload = client.get(REPORT_URL).json()
        self.assertTrue(payload['can_view_cost'])
        self.assertIn('_raw_tezgahta_maliyet', payload['data'][0])

    def test_L_unauthorized_staff_gets_no_cost_from_api(self):
        client = _client_for(self.user_no)
        resp = client.get(REPORT_URL)
        # Ekran yetkisi olmayan personel endpoint'e HİÇ giremez (302 redirect).
        self.assertEqual(resp.status_code, 302)

    def test_L_unauthorized_staff_gets_no_cost_from_pdf(self):
        client = _client_for(self.user_no)
        resp = client.get(PDF_URL)
        self.assertEqual(resp.status_code, 302)
        self.assertNotEqual(resp.get('Content-Type', ''), 'application/pdf')

    def test_L_unauthorized_staff_cannot_read_details(self):
        client = _client_for(self.user_no)
        gp = GoldPurchases.objects.filter(store=self.store).first()
        resp = client.get(DETAILS_URL, {'id': str(gp.id)})
        self.assertEqual(resp.status_code, 302)

    def test_L_cost_fields_absent_when_helper_false(self):
        """include_cost=False → maliyet anahtarları HİÇ üretilmez."""
        from apps.gold_purchases.views import _build_detailed_report_rows

        rows = _build_detailed_report_rows(self.store, {}, include_cost=False)
        self.assertTrue(rows)
        for r in rows:
            self.assertNotIn('_raw_tezgahta_maliyet', r)
            self.assertNotIn('tezgahta_maliyet', r)
        self.assertEqual(summarize_cost_by_currency(rows), [])


# ═════════════════════════════════════════════════════════════════════════════
# ŞABLON RENDER (UI) — para birimi varsayılanı ekranda da doğru mu?
# ═════════════════════════════════════════════════════════════════════════════
class IndexTemplateRenderTest(TestCase):
    """Sayfanın render edildiğini ve para birimi seçicisinin mağaza
    varsayılanıyla açıldığını doğrular.

    DİKKAT: HTML YORUMLARI STRIP EDİLİR. Şablonda açıklama amaçlı 'EUR'/'USD'
    metinleri var; yorumları temizlemeden yapılan assert SAHTE GEÇER.
    """

    @staticmethod
    def _strip_html_comments(html):
        import re
        return re.sub(r'<!--.*?-->', '', html, flags=re.S)

    def _render(self, primary_currency):
        store = _make_store(f'TPL{primary_currency}', primary_currency)
        user = _make_superuser(store, f'tpl_{primary_currency.lower()}')
        client = _client_for(user)
        resp = client.get('/gold-purchases/index')
        self.assertEqual(resp.status_code, 200)
        return self._strip_html_comments(resp.content.decode('utf-8'))

    def test_germany_store_renders_eur_selected(self):
        html = self._render('EUR')
        self.assertIn('<option value="EUR" selected>EUR</option>', html)
        self.assertNotIn('<option value="USD" selected>USD</option>', html)
        # Maliyet etiketi de mağaza para birimini taşır
        self.assertIn('Alış EUR (Maliyet)', html)
        self.assertNotIn('Alış TL (Maliyet)', html)

    def test_try_store_renders_try_selected(self):
        html = self._render('TRY')
        self.assertIn('<option value="TRY" selected>TRY</option>', html)
        self.assertIn('Alış TRY (Maliyet)', html)

    def test_report_modal_has_group_and_date_filters(self):
        html = self._render('EUR')
        self.assertIn('id="drpDetailedGroupFilter"', html)
        self.assertIn('id="dtDetailedFrom"', html)
        self.assertIn('id="dtDetailedTo"', html)
        self.assertIn('id="detailedQuickRanges"', html)

    def test_edit_mode_hidden_inputs_exist(self):
        html = self._render('EUR')
        self.assertIn('id="dia_gold_purchase_id"', html)
        self.assertIn('id="wat_gold_purchase_id"', html)
        self.assertIn('multi-material-update', html)


# ═════════════════════════════════════════════════════════════════════════════
# TÜRKİYE / ALTIN REGRESYONU — mevcut akış bozulmadı mı?
# ═════════════════════════════════════════════════════════════════════════════
class GoldFlowRegressionTest(TestCase):
    """Altın düzenleme akışı (mevcut davranış) aynen çalışmaya devam etmeli."""

    def setUp(self):
        self.store = _make_store('TR9000', 'TRY')
        self.user = _make_superuser(self.store, 'gold_reg_user')
        self.client = _client_for(self.user)

    def test_gold_details_still_return_gold_fields(self):
        product, gp = _make_gold(self.store, jewelry_type='Bilezik',
                                 gram='12.500', mileage='916', buy_price_hs='11.450')
        d = self.client.get(DETAILS_URL, {'id': str(gp.id)}).json()
        self.assertTrue(d['result'])
        self.assertEqual(d['material_type'], 'GOLD')
        self.assertNotIn('diamond', d)
        self.assertEqual(Decimal(d['gram']), Decimal('12.500'))
        self.assertEqual(Decimal(d['product_mileage']), Decimal('916.0000'))
        self.assertEqual(Decimal(d['buy_price_hs']), Decimal('11.450'))

    def test_gold_add_update_still_updates_in_place(self):
        product, gp = _make_gold(self.store, jewelry_type='Bilezik')
        before = Products.objects.filter(store=self.store).count()
        old_barcode = product.barcode

        resp = self.client.post('/gold-purchases/add', {
            'gold_purchase_id': str(product.id),
            'jewelry_type': 'Kolye',
            'gram': '15,000',
            'product_mileage': '916',
            'buy_price_hs': '13,740',
            'sale_price_hs': '15,000',
        })
        self.assertIn(resp.status_code, (200, 302), resp.content[:300])
        self.assertEqual(Products.objects.filter(store=self.store).count(), before)
        product.refresh_from_db()
        self.assertEqual(product.jewelry_type, 'Kolye')
        self.assertEqual(product.barcode, old_barcode)

    def test_gold_rows_keep_has_cost_unit_in_report(self):
        _make_gold(self.store, jewelry_type='Bilezik', gram='10.000',
                   mileage='916', buy_price_hs='9.160')
        rows = self.client.get(REPORT_URL, {'product_group': 'GOLD'}).json()['data']
        self.assertTrue(rows)
        self.assertEqual(rows[0]['cost_unit'], 'HAS')
        self.assertTrue(rows[0]['cost_is_metal'])
        # 1.05 EŞİK KURALI korundu: 9.160 > 1.05 → legacy toplam olarak alınır
        self.assertEqual(rows[0]['_raw_tezgahta_maliyet'], 9.16)

    def test_unfiltered_report_matches_legacy_grouping(self):
        """Filtre yokken satır sayısı (takı tipi × ayar) eski davranışla aynı."""
        _make_gold(self.store, jewelry_type='Bilezik', mileage='916')
        _make_gold(self.store, jewelry_type='Bilezik', mileage='916')
        _make_gold(self.store, jewelry_type='Bilezik', mileage='585')
        rows = self.client.get(REPORT_URL).json()['data']
        bilezik = [r for r in rows if r['category'] == 'Bilezik']
        self.assertEqual(len(bilezik), 2)  # 22 Ayar + 14 Ayar → altın kırılmadı
        counts = sorted(r['tezgahta_count'] for r in bilezik)
        self.assertEqual(counts, [1, 2])


# ═════════════════════════════════════════════════════════════════════════════
# MALİYETİ GÖREMEYEN KULLANICI MALİYETİ EZEMEZ
# ═════════════════════════════════════════════════════════════════════════════
class CostBlindUpdateGuardTest(TestCase):
    """Maliyet görme yetkisi olmayan bir kullanıcı ürünü düzenleyebilse bile
    kayıtlı maliyeti SIFIRLAYAMAZ (sessiz veri kaybı koruması)."""

    def setUp(self):
        self.store = _make_store('CB0001', 'EUR')
        # Konasoft personeli (is_staff) → role_required Katman 3a: RoleDetail
        add_perm = Permission.objects.create(
            code='GOLD_PURCHASES_GOLD_PURCHASE_ADD', name='Ürün Ekle',
        )
        role = Roles.objects.create(name='Sadece Ekleme', category='SYSTEM')
        RoleDetail.objects.create(role=role, permission=add_perm, status=True)
        # DİKKAT: ABC1007D (maliyet kapısı) BU ROLDE YOK.

        self.user = User.objects.create_user(
            username='cost_blind', password='pw12345', email='cb@test.local',
        )
        self.user.store = self.store
        self.user.role = role
        self.user.is_staff = True
        self.user.is_superuser = False
        self.user.save()
        self.client = _client_for(self.user)

    def test_cost_blind_user_cannot_wipe_cost(self):
        self.assertFalse(user_can_view_cost(self.user))
        product, dd, gp = _make_diamond(self.store, buy_price_eur='1850.00')

        resp = self.client.post(
            UPDATE_URL,
            _diamond_post_data(gold_purchase_id=str(gp.id), buy_price_eur='0'),
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        product.refresh_from_db()
        self.assertEqual(product.buy_price_eur, Decimal('1850.00'))
