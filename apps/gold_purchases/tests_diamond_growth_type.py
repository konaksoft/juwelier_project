"""Lab / Natural pırlanta kökeni (DiamondDetail.growth_type) test paketi.

Kapsam:
  1. Model — enum değerleri, kısa etiket mapping'i (NAT/LAB/''), boş/None
     davranışı, geçersiz değerin full_clean ile reddi.
  2. Migration güvenliği — alan null=True + default YOK (eski kayıt NATURAL'a
     dönüşmez); choices NATURAL/LAB_GROWN.
  3. Etiket veri çözücü (_resolve_diamond_label_data) — HTML ve ZPL akışlarının
     ORTAK kaynağı; NATURAL→NAT, LAB_GROWN→LAB, boş→'' (NAT'a zorlanmaz),
     pırlanta-olmayan üründe alan üretilmez.
  4. Create view (multi_material_product_add) — POST'tan gelen köken doğru
     kaydedilir; geçersiz değer NATURAL'a düşer; boş DiamondDetail (kökeni
     bilinmeyen) NATURAL'a çevrilmez.

Çalıştırma (izole SQLite, CANLI DB'ye dokunmaz):
    DJANGO_SETTINGS_MODULE=<izole_test_settings> \
    python manage.py test apps.gold_purchases.tests_diamond_growth_type
"""
from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, Client
from django.db import connection
from django.db.migrations.loader import MigrationLoader

from django.contrib.auth import get_user_model

from apps.products.models import Products, DiamondDetail, DiamondStone, MaterialType
from apps.stores.models import Stores, Company
from apps.gold_purchases.models import GoldPurchases
from apps.settings.models import StoreLabelSettings, default_diamond_small_config
from apps.gold_purchases.views import _resolve_diamond_label_data

User = get_user_model()


def _make_store():
    company = Company.objects.create()
    return Stores.objects.create(store_id='TESTGT01', company=company, title='Test Mağaza')


def _make_diamond_product(store, growth_type, *, name='Pırlanta Yüzük'):
    """material_type=DIAMOND bir Products + DiamondDetail üretir.

    NOT: Products.save() sonrası ensure_detail_extension sinyali boş bir
    DiamondDetail yaratabilir; bu yüzden update_or_create kullanıyoruz.
    growth_type=None geçilirse alan boş (bilinmeyen) bırakılır.
    """
    product = Products.objects.create(
        name=name,
        material_type=MaterialType.DIAMOND,
        store=store,
        buy_price_eur=Decimal('1000.00'),
    )
    dd, _ = DiamondDetail.objects.update_or_create(
        product=product,
        defaults=dict(
            sale_currency='EUR',
            sale_price=Decimal('2500.00'),
            growth_type=growth_type,
        ),
    )
    # Products.save() sinyali önce boş bir DiamondDetail (growth_type=None)
    # yaratıp instance'a cache'lemiş olabilir. Etiket çözücü ürünü DB'den taze
    # okuduğu için (get_print_data/print_barcode_normal select_related ile),
    # testte de bayat cache'i temizlemek için ürünü yeniden çekiyoruz.
    fresh_product = Products.objects.get(pk=product.pk)
    return fresh_product, dd


# ─────────────────────────────────────────────────────────────────────────────
# 1) MODEL
# ─────────────────────────────────────────────────────────────────────────────
class GrowthTypeModelTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.store = _make_store()

    def test_save_natural(self):
        _, dd = _make_diamond_product(self.store, 'NATURAL')
        dd.refresh_from_db()
        self.assertEqual(dd.growth_type, 'NATURAL')

    def test_save_lab_grown(self):
        _, dd = _make_diamond_product(self.store, 'LAB_GROWN')
        dd.refresh_from_db()
        self.assertEqual(dd.growth_type, 'LAB_GROWN')

    def test_save_null_stays_null(self):
        """Kökeni bilinmeyen kayıt None kalır; NATURAL'a dönüşmez."""
        _, dd = _make_diamond_product(self.store, None)
        dd.refresh_from_db()
        self.assertIsNone(dd.growth_type)

    def test_field_has_no_default(self):
        """default=NATURAL OLMAMALI — aksi halde migration eski satırları backfill eder."""
        f = DiamondDetail._meta.get_field('growth_type')
        self.assertFalse(f.has_default())
        self.assertTrue(f.null)
        self.assertTrue(f.blank)

    def test_choices_are_natural_and_lab_grown(self):
        vals = {c[0] for c in DiamondDetail.GrowthType.choices}
        self.assertEqual(vals, {'NATURAL', 'LAB_GROWN'})

    def test_short_label_mapping(self):
        _, nat = _make_diamond_product(self.store, 'NATURAL', name='n')
        _, lab = _make_diamond_product(self.store, 'LAB_GROWN', name='l')
        _, unk = _make_diamond_product(self.store, None, name='u')
        self.assertEqual(nat.growth_type_short, 'NAT')
        self.assertEqual(lab.growth_type_short, 'LAB')
        self.assertEqual(unk.growth_type_short, '')  # boş → boş (NAT değil!)

    def test_short_label_never_raw_enum(self):
        _, lab = _make_diamond_product(self.store, 'LAB_GROWN', name='l2')
        self.assertNotIn('LAB_GROWN', lab.growth_type_short)  # ham enum sızmaz

    def test_invalid_value_rejected_by_full_clean(self):
        product, dd = _make_diamond_product(self.store, 'NATURAL', name='inv')
        dd.growth_type = 'SYNTHETIC'  # choices dışı
        with self.assertRaises(ValidationError):
            dd.full_clean()


# ─────────────────────────────────────────────────────────────────────────────
# 1b) MIGRATION GÜVENLİĞİ — 0002 AddField eski kayıtları backfill ETMEZ
# ─────────────────────────────────────────────────────────────────────────────
class GrowthTypeMigrationSafetyTest(TestCase):
    """0002_diamonddetail_growth_type migration'ının AddField operasyonu
    nullable ve default'suz olmalı. Aksi halde uygulanınca mevcut TÜM
    DiamondDetail satırlarını (kökeni bilinmeyen, belki Lab) NATURAL'a yazardı.
    """

    def _get_addfield_op(self):
        loader = MigrationLoader(connection)
        migration = loader.get_migration('products', '0002_diamonddetail_growth_type')
        ops = [o for o in migration.operations
               if getattr(o, 'name', None) == 'growth_type'
               and getattr(o, 'model_name', None) == 'diamonddetail']
        self.assertEqual(len(ops), 1, "Tek bir growth_type AddField beklenir")
        return ops[0]

    def test_migration_field_is_nullable_and_defaultless(self):
        op = self._get_addfield_op()
        field = op.field
        self.assertTrue(field.null, "growth_type null=True olmalı (eski satırlar NULL kalsın)")
        self.assertFalse(
            field.has_default(),
            "growth_type migration'da default OLMAMALI — yoksa eski satırlar backfill olur",
        )

    def test_migration_choices_are_natural_and_lab_grown(self):
        op = self._get_addfield_op()
        vals = {c[0] for c in (op.field.choices or [])}
        self.assertEqual(vals, {'NATURAL', 'LAB_GROWN'})


# ─────────────────────────────────────────────────────────────────────────────
# 2) ETİKET VERİ ÇÖZÜCÜ (HTML + ZPL ortak kaynağı)
# ─────────────────────────────────────────────────────────────────────────────
class GrowthTypeLabelResolverTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.store = _make_store()

    def test_natural_maps_to_nat(self):
        product, _ = _make_diamond_product(self.store, 'NATURAL')
        self.assertEqual(_resolve_diamond_label_data(product)['growth_type'], 'NAT')

    def test_lab_grown_maps_to_lab(self):
        product, _ = _make_diamond_product(self.store, 'LAB_GROWN')
        self.assertEqual(_resolve_diamond_label_data(product)['growth_type'], 'LAB')

    def test_empty_maps_to_empty_not_nat(self):
        """KRİTİK: kökeni bilinmeyen (None) ürün etikette NAT göstermemeli."""
        product, _ = _make_diamond_product(self.store, None)
        self.assertEqual(_resolve_diamond_label_data(product)['growth_type'], '')

    def test_non_diamond_product_has_empty_growth(self):
        """Pırlanta olmayan üründe (DiamondDetail yok) köken boş döner."""
        gold = Products.objects.create(
            name='Altın Bilezik', material_type=MaterialType.GOLD, store=self.store,
        )
        # ensure_detail_extension DIAMOND olmadığı için diamond_detail yaratmaz
        self.assertFalse(hasattr(gold, 'diamond_detail') and gold.diamond_detail is not None
                         and getattr(gold, 'diamond_detail', None) is not None
                         and gold.diamond_detail.pk and gold.material_type == 'DIAMOND')
        self.assertEqual(_resolve_diamond_label_data(gold)['growth_type'], '')

    def test_html_and_zpl_use_same_resolver_value(self):
        """HTML (print_barcode_normal) ve ZPL (get_print_data) ikisi de bu
        çözücünün 'growth_type' değerini kullanır → parite garanti."""
        product, _ = _make_diamond_product(self.store, 'LAB_GROWN')
        val = _resolve_diamond_label_data(product)['growth_type']
        self.assertEqual(val, 'LAB')
        # Ham enum ASLA görünmez
        self.assertNotIn('GROWN', val)


# ─────────────────────────────────────────────────────────────────────────────
# 3) CREATE VIEW (multi_material_product_add)
# ─────────────────────────────────────────────────────────────────────────────
class GrowthTypeCreateViewTest(TestCase):

    def setUp(self):
        self.store = _make_store()
        self.user = User.objects.create_superuser(
            username='gt_admin', password='pw12345', email='a@b.c',
        )
        self.user.store = self.store
        self.user.save()
        self.client = Client()
        self.client.force_login(self.user)

    def _post(self, growth_value, extra=None):
        data = {
            'material_type': 'DIAMOND',
            'jewelry_type': 'Yüzük',
            'sale_currency': 'EUR',
            'sale_price': '2500,00',
            'buy_price_eur': '1000',
            'mount_gram': '3,50',
            'mount_karat': '18K',
            'stock_pieces': '1',
            # tek taş (D1) — POST-QA guard en az 1 karat>0 taş ister
            'stone_type[]': 'DIAMOND',
            'stone_role[]': 'CENTER',
            'stone_position[]': '1',
            'stone_carat[]': '0,50',
            'stone_color[]': 'F',
            'stone_clarity[]': 'VS1',
            'stone_cut[]': 'EXCELLENT',
            'stone_cert_lab[]': 'GIA',
            'stone_cert_no[]': '',
        }
        if growth_value is not None:
            data['diamond_growth_type'] = growth_value
        if extra:
            data.update(extra)
        return self.client.post('/gold-purchases/multi-material-add', data,
                                HTTP_X_REQUESTED_WITH='XMLHttpRequest')

    def test_create_saves_lab_grown(self):
        resp = self._post('LAB_GROWN')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        body = resp.json()
        self.assertTrue(body.get('result'), body)
        dd = DiamondDetail.objects.get(product__barcode=body['barcode'])
        self.assertEqual(dd.growth_type, 'LAB_GROWN')
        self.assertEqual(dd.growth_type_short, 'LAB')

    def test_create_saves_natural(self):
        resp = self._post('NATURAL')
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd = DiamondDetail.objects.get(product__barcode=resp.json()['barcode'])
        self.assertEqual(dd.growth_type, 'NATURAL')

    def test_invalid_value_falls_back_to_natural(self):
        resp = self._post('SYNTHETIC')  # geçersiz → NATURAL (yeni kayıt)
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd = DiamondDetail.objects.get(product__barcode=resp.json()['barcode'])
        self.assertEqual(dd.growth_type, 'NATURAL')

    def test_missing_field_defaults_natural_on_create(self):
        resp = self._post(None)  # alan hiç gönderilmez → yeni kayıt NATURAL
        self.assertEqual(resp.status_code, 200, resp.content[:400])
        dd = DiamondDetail.objects.get(product__barcode=resp.json()['barcode'])
        self.assertEqual(dd.growth_type, 'NATURAL')


# ─────────────────────────────────────────────────────────────────────────────
# 4) ZPL RENDER (fiziksel yazıcı yok — ZPL string üzerinden doğrulama)
# ─────────────────────────────────────────────────────────────────────────────
class GrowthTypeZplRenderTest(TestCase):
    """get_print_data'nın ürettiği ZPL string'inde köken alanının doğru
    (NAT/LAB) göründüğünü, boş kökende ise HİÇ görünmediğini doğrular.

    Köken alanı default gizli olduğu için testte küçük etiket config'inde
    growth_type görünürlüğü açılır (mağaza Etiket Tasarımı ekranının yaptığı iş).
    """

    def setUp(self):
        self.store = _make_store()
        self.user = User.objects.create_superuser(
            username='gt_zpl', password='pw12345', email='z@b.c',
        )
        self.user.store = self.store
        self.user.save()
        self.client = Client()
        self.client.force_login(self.user)

        # Köken alanını GÖRÜNÜR yap (küçük etiket)
        cfg = default_diamond_small_config()
        cfg['growth_type']['visible'] = True
        StoreLabelSettings.objects.update_or_create(
            store=self.store,
            defaults=dict(active_size='small', diamond_small_design=cfg),
        )

    def _gp_with_growth(self, growth_type):
        """Doğrudan (view'siz) bir pırlanta + GoldPurchases kaydı üretir.
        growth_type=None → kökeni bilinmeyen (migrasyonla NULL kalmış eski kayıt)."""
        product, _dd = _make_diamond_product(self.store, growth_type)
        return GoldPurchases.objects.create(
            product=product, store=self.store, created_by=self.user, is_status=True,
        )

    def _zpl_for(self, gp):
        resp = self.client.get('/gold-purchases/get-print-data', {'ids': str(gp.id)})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        body = resp.json()
        self.assertTrue(body.get('result'), body)
        return body['labels'][0]['zpl']

    def test_zpl_lab_grown_prints_lab(self):
        zpl = self._zpl_for(self._gp_with_growth('LAB_GROWN'))
        self.assertIn('^FDLAB^FS', zpl)          # kısa metin basılır
        self.assertNotIn('^FDLAB_GROWN^FS', zpl)  # ham enum ASLA
        self.assertNotIn('^FDNAT^FS', zpl)

    def test_zpl_natural_prints_nat(self):
        zpl = self._zpl_for(self._gp_with_growth('NATURAL'))
        self.assertIn('^FDNAT^FS', zpl)
        self.assertNotIn('^FDLAB^FS', zpl)

    def test_zpl_unknown_prints_no_origin(self):
        """Kökeni bilinmeyen (NULL) eski kayıt → etikette NAT de LAB da YOK."""
        zpl = self._zpl_for(self._gp_with_growth(None))
        self.assertNotIn('^FDNAT^FS', zpl)
        self.assertNotIn('^FDLAB^FS', zpl)

    # ── HTML önizleme (print_barcode_normal) — ZPL ile aynı köken sonucu ──
    def _html_for(self, gp):
        resp = self.client.get('/gold-purchases/print-barcode-normal', {'ids': str(gp.id)})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        return resp.content.decode('utf-8')

    def test_html_lab_grown_shows_lab(self):
        html = self._html_for(self._gp_with_growth('LAB_GROWN'))
        self.assertIn('LAB', html)          # kısa metin görünür (büyük harf)
        self.assertNotIn('LAB_GROWN', html)  # ham enum ASLA

    def test_html_unknown_shows_no_origin(self):
        """Boş köken → HTML'de NAT/LAB köken metni render edilmez."""
        html = self._html_for(self._gp_with_growth(None))
        # Büyük-harf NAT/LAB (köken kısa metni) HTML'de bulunmamalı.
        # (Şablondaki 'certificate_lab'/'label' küçük harf olduğu için çakışmaz.)
        self.assertNotIn('NAT', html)
        self.assertNotIn('>LAB<', html)
