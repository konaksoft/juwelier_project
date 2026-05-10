"""
FAZ R-3 / R-4: Rapor Rollup Hesaplama Servisi
================================================

DailyStoreReport ve DailyEmployeeReport tablolarını
Process + Payment + StockSnapshot verilerinden hesaplar.

Kurallar:
    - Mevcut tablolara dokunmaz (sadece SELECT).
    - UPSERT mantığı ile çalışır (update_or_create).
    - Toplu sorgu (aggregate) kullanır — Python loop minimalize.
    - Canlı işlem tablolarını kilitlemez.

FAZ C — Çoklu Maden/Ürün Entegrasyonu (2026-04-21):
    - Stok değerleri Products.material_type filtresiyle "conditional aggregation"
      üzerinden tek sorguda çekilir (N+1 yok).
    - DailyStoreReport'a FAZ A'da eklenen 7 yeni alan burada doldurulur.
    - get_store_assets_summary() Altın/Gümüş/Pırlanta/Saat breakdown'ı döner;
      mevcut 'total_has_value', 'total_gram' vb. anahtarlar korunur.
    - Kasa/POS bakiye özeti (cash_total_by_currency) dinamik olduğu için
      yeni HG (Has Gümüş) para birimi KOD DEĞİŞİKLİĞİ OLMADAN desteklenir.
"""

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import (
    Case, Count, DecimalField, F, IntegerField, Q, Sum, Value, When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

logger = logging.getLogger('dashboard.reports')

ZERO = Decimal('0')
ZERO2 = Decimal('0.00')
ZERO4 = Decimal('0.0000')


# ============================================================================
# FAZ C YARDIMCILARI: Conditional Aggregation için ortak material_type Q'ları
# ============================================================================

_Q_GOLD    = Q(product__material_type='GOLD')
_Q_SILVER  = Q(product__material_type='SILVER')
_Q_DIAMOND = Q(product__material_type='DIAMOND')
_Q_WATCH   = Q(product__material_type='WATCH')


def _build_multi_material_stock_aggregate():
    """
    Tüm material_type breakdown'ı için tek SQL sorgusunda kullanılacak
    aggregate dict'i üretir. Dönüş, `.aggregate(**...)` içinde doğrudan
    expand edilebilir.

    Dönen anahtarlar:
        gold_value_hs, gold_value_tl,
        silver_gram, silver_value_hg, silver_value_tl,
        diamond_pieces, diamond_value_tl,
        watch_pieces, watch_value_tl

    Önemli: Bu fonksiyon veritabanına dokunmaz; yalnızca aggregate ifade
    sözlüğünü döner. Caller queryset üzerinde `.aggregate(**result)` çağırır.
    """
    return {
        # --- ALTIN (GOLD) ---
        'gold_value_hs': Coalesce(
            Sum(
                F('stock_gram') * F('weighted_avg_cost_hs'),
                filter=_Q_GOLD,
                output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
            ZERO4, output_field=DecimalField(max_digits=20, decimal_places=4),
        ),
        'gold_value_tl': Coalesce(
            Sum(
                F('stock_gram') * F('weighted_avg_cost_eur'),
                filter=_Q_GOLD,
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            ZERO2, output_field=DecimalField(max_digits=20, decimal_places=2),
        ),

        # --- GÜMÜŞ (SILVER) ---
        'silver_gram': Coalesce(
            Sum('stock_gram', filter=_Q_SILVER),
            ZERO4, output_field=DecimalField(max_digits=14, decimal_places=4),
        ),
        'silver_value_hg': Coalesce(
            Sum(
                F('stock_gram') * F('weighted_avg_cost_hs'),
                filter=_Q_SILVER,
                output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
            ZERO4, output_field=DecimalField(max_digits=20, decimal_places=4),
        ),
        'silver_value_tl': Coalesce(
            Sum(
                F('stock_gram') * F('weighted_avg_cost_eur'),
                filter=_Q_SILVER,
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            ZERO2, output_field=DecimalField(max_digits=20, decimal_places=2),
        ),

        # --- PIRLANTA (DIAMOND) --- adet bazlı
        'diamond_pieces': Coalesce(
            Sum('stock_pieces', filter=_Q_DIAMOND),
            Value(0), output_field=IntegerField(),
        ),
        'diamond_value_tl': Coalesce(
            Sum(
                F('stock_pieces') * F('weighted_avg_cost_eur'),
                filter=_Q_DIAMOND,
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            ZERO2, output_field=DecimalField(max_digits=20, decimal_places=2),
        ),

        # --- SAAT (WATCH) --- adet bazlı
        'watch_pieces': Coalesce(
            Sum('stock_pieces', filter=_Q_WATCH),
            Value(0), output_field=IntegerField(),
        ),
        'watch_value_tl': Coalesce(
            Sum(
                F('stock_pieces') * F('weighted_avg_cost_eur'),
                filter=_Q_WATCH,
                output_field=DecimalField(max_digits=20, decimal_places=2),
            ),
            ZERO2, output_field=DecimalField(max_digits=20, decimal_places=2),
        ),
    }


# ============================================================================
# ANA FONKSİYONLAR
# ============================================================================

def compute_daily_store_report(store, report_date):
    """
    Tek bir mağaza-tarih çifti için DailyStoreReport hesapla ve UPSERT et.
    Dönüş: DailyStoreReport instance.

    FAZ C: Stok aggregate sorgusu Gümüş/Pırlanta/Saat için genişletildi.
    Tek sorguda tüm material_type'ların değerleri hesaplanır.
    """
    from apps.process.models import Process, Payment
    from apps.stock_management.models import StockSnapshot
    from apps.dashboard.models import DailyStoreReport

    now = timezone.now()

    # ─── 1. Process Aggregates (tek sorgu) ───────────────────
    process_agg = (
        Process.objects
        .filter(
            store=store,
            is_deleted=False,
            is_status='COMPLETED',
            date__date=report_date,
        )
        .aggregate(
            # Satış
            sale_count=Count('id', filter=Q(transaction_type='SALE')),
            total_sales_eur=Coalesce(
                Sum('amount', filter=Q(transaction_type='SALE')),
                ZERO2, output_field=DecimalField(),
            ),
            total_sales_hs=Coalesce(
                Sum('price_hs', filter=Q(transaction_type='SALE')),
                ZERO4, output_field=DecimalField(),
            ),
            total_gross_profit=Coalesce(
                Sum('gross_profit', filter=Q(transaction_type='SALE')),
                ZERO2, output_field=DecimalField(),
            ),
            total_net_profit=Coalesce(
                Sum('net_profit', filter=Q(transaction_type='SALE')),
                ZERO2, output_field=DecimalField(),
            ),
            unique_customers=Count(
                'customer', filter=Q(transaction_type='SALE'), distinct=True,
            ),
            # Alış
            purchase_count=Count(
                'id', filter=Q(transaction_type='PURCHASE'),
            ),
            total_purchases_eur=Coalesce(
                Sum('amount', filter=Q(transaction_type='PURCHASE')),
                ZERO2, output_field=DecimalField(),
            ),
            total_purchases_hs=Coalesce(
                Sum('price_hs', filter=Q(transaction_type='PURCHASE')),
                ZERO4, output_field=DecimalField(),
            ),
            # İade
            return_count=Count('id', filter=Q(transaction_type='RETURN')),
            total_returns_eur=Coalesce(
                Sum('amount', filter=Q(transaction_type='RETURN')),
                ZERO2, output_field=DecimalField(),
            ),
            # Toplam
            transaction_count=Count('id'),
        )
    )

    # ─── 2. Payment Aggregates (tek sorgu) ───────────────────
    # Payment -> ProcessGroup -> store ilişkisi kullanılıyor
    payment_agg = (
        Payment.objects
        .filter(
            process_group__store=store,
            date__date=report_date,
            is_cancelled=False,
            is_approved=True,
        )
        .aggregate(
            cash_in=Coalesce(
                Sum('amount', filter=Q(payment_type='CASH', is_output=False)),
                ZERO2, output_field=DecimalField(),
            ),
            cash_out=Coalesce(
                Sum('amount', filter=Q(payment_type='CASH', is_output=True)),
                ZERO2, output_field=DecimalField(),
            ),
            card_in=Coalesce(
                Sum('amount', filter=Q(payment_type='CREDIT_CARD', is_output=False)),
                ZERO2, output_field=DecimalField(),
            ),
            card_out=Coalesce(
                Sum('amount', filter=Q(payment_type='CREDIT_CARD', is_output=True)),
                ZERO2, output_field=DecimalField(),
            ),
            transfer_in=Coalesce(
                Sum('amount', filter=Q(payment_type='TRANSFER', is_output=False)),
                ZERO2, output_field=DecimalField(),
            ),
            transfer_out=Coalesce(
                Sum('amount', filter=Q(payment_type='TRANSFER', is_output=True)),
                ZERO2, output_field=DecimalField(),
            ),
            commission_total=Coalesce(
                Sum('commission_amount', filter=Q(commission_amount__isnull=False)),
                ZERO2, output_field=DecimalField(),
            ),
        )
    )

    # ─── 3. Stok Değeri — FAZ C: Çoklu Maden Conditional Aggregation ─
    # Tek SQL sorgusunda Altın/Gümüş/Pırlanta/Saat kırılımı.
    # Not: `stock_gram__gt=0` filtresi KALDIRILDI; çünkü Saat/Pırlanta
    # için stock_gram=0'dır. 0 değerler SUM'a sıfır katkı yapar.
    # material_type='GOLD' haricinde weighted_avg_cost_hs alanı HG birimidir.
    stock_value_eur = ZERO2
    stock_value_hs = ZERO4
    silver_stock_gram = None
    silver_stock_value_hg = None
    silver_stock_value_eur = None
    diamond_stock_pieces = None
    diamond_stock_value_eur = None
    watch_stock_pieces = None
    watch_stock_value_eur = None

    if report_date >= date.today() - timedelta(days=1):
        # FAZ 34: product__is_deleted=False filtresi paritte icin eklendi
        # (get_store_assets_summary ile ayni davranis).
        stock_agg = (
            StockSnapshot.objects
            .filter(store=store, product__is_deleted=False)
            .exclude(product__is_currency=True)
            .aggregate(**_build_multi_material_stock_aggregate())
        )

        # ALTIN değerleri: mevcut stock_value_eur/hs alanlarını besler
        # (FAZ öncesi semantiği korunur — altın için aynı iş).
        stock_value_eur = stock_agg['gold_value_tl'] or ZERO2
        stock_value_hs = stock_agg['gold_value_hs'] or ZERO4

        # Gümüş
        silver_stock_gram = stock_agg['silver_gram'] or ZERO4
        silver_stock_value_hg = stock_agg['silver_value_hg'] or ZERO4
        silver_stock_value_eur = stock_agg['silver_value_tl'] or ZERO2

        # Pırlanta
        diamond_stock_pieces = int(stock_agg['diamond_pieces'] or 0)
        diamond_stock_value_eur = stock_agg['diamond_value_tl'] or ZERO2

        # Saat
        watch_stock_pieces = int(stock_agg['watch_pieces'] or 0)
        watch_stock_value_eur = stock_agg['watch_value_tl'] or ZERO2

    # ─── 4. UPSERT ──────────────────────────────────────────
    report, _ = DailyStoreReport.objects.update_or_create(
        store=store,
        report_date=report_date,
        defaults={
            'total_sales_eur': process_agg['total_sales_eur'],
            'total_sales_hs': process_agg['total_sales_hs'],
            'sale_count': process_agg['sale_count'],
            'unique_customers': process_agg['unique_customers'],
            'total_purchases_eur': process_agg['total_purchases_eur'],
            'total_purchases_hs': process_agg['total_purchases_hs'],
            'purchase_count': process_agg['purchase_count'],
            'total_returns_eur': process_agg['total_returns_eur'],
            'return_count': process_agg['return_count'],
            'total_gross_profit': process_agg['total_gross_profit'],
            'total_net_profit': process_agg['total_net_profit'],
            'cash_in': payment_agg['cash_in'],
            'cash_out': payment_agg['cash_out'],
            'card_in': payment_agg['card_in'],
            'card_out': payment_agg['card_out'],
            'transfer_in': payment_agg['transfer_in'],
            'transfer_out': payment_agg['transfer_out'],
            'commission_total': payment_agg['commission_total'],
            'transaction_count': process_agg['transaction_count'],
            # Altın (mevcut alanlar — backwards compatible)
            'stock_value_eur': stock_value_eur,
            'stock_value_hs': stock_value_hs,
            # FAZ A/C: Gümüş
            'silver_stock_gram': silver_stock_gram,
            'silver_stock_value_hg': silver_stock_value_hg,
            'silver_stock_value_eur': silver_stock_value_eur,
            # FAZ A/C: Pırlanta
            'diamond_stock_pieces': diamond_stock_pieces,
            'diamond_stock_value_eur': diamond_stock_value_eur,
            # FAZ A/C: Saat
            'watch_stock_pieces': watch_stock_pieces,
            'watch_stock_value_eur': watch_stock_value_eur,
            'computed_at': now,
        },
    )

    return report


def compute_daily_employee_reports(store, report_date):
    """
    Tek bir mağaza-tarih çifti için tüm personellerin DailyEmployeeReport'unu hesapla.

    FAZ C NOTU: Personel performansı için material_type kırılımı yapılmaz —
    bir personelin satış/alış rakamları Process.amount / Process.price_hs
    üzerinden TL/HS biriminde toplanır. Bu davranış değişmedi.
    """
    from apps.process.models import Process
    from apps.dashboard.models import DailyEmployeeReport

    now = timezone.now()

    # Personel bazlı aggregate
    employee_data = (
        Process.objects
        .filter(
            store=store,
            is_deleted=False,
            is_status='COMPLETED',
            date__date=report_date,
            employee__isnull=False,
        )
        .values('employee_id')
        .annotate(
            sale_count=Count('id', filter=Q(transaction_type='SALE')),
            total_sales_eur=Coalesce(
                Sum('amount', filter=Q(transaction_type='SALE')),
                ZERO2, output_field=DecimalField(),
            ),
            total_sales_hs=Coalesce(
                Sum('price_hs', filter=Q(transaction_type='SALE')),
                ZERO4, output_field=DecimalField(),
            ),
            total_gross_profit=Coalesce(
                Sum('gross_profit', filter=Q(transaction_type='SALE')),
                ZERO2, output_field=DecimalField(),
            ),
            purchase_count=Count('id', filter=Q(transaction_type='PURCHASE')),
            total_purchases_eur=Coalesce(
                Sum('amount', filter=Q(transaction_type='PURCHASE')),
                ZERO2, output_field=DecimalField(),
            ),
            transaction_count=Count('id'),
        )
    )

    for row in employee_data:
        DailyEmployeeReport.objects.update_or_create(
            store=store,
            employee_id=row['employee_id'],
            report_date=report_date,
            defaults={
                'sale_count': row['sale_count'],
                'total_sales_eur': row['total_sales_eur'],
                'total_sales_hs': row['total_sales_hs'],
                'total_gross_profit': row['total_gross_profit'],
                'purchase_count': row['purchase_count'],
                'total_purchases_eur': row['total_purchases_eur'],
                'transaction_count': row['transaction_count'],
                'computed_at': now,
            },
        )

    return employee_data.count()


def compute_reports_for_all_stores(report_date):
    """Tüm aktif mağazalar için günlük raporları hesapla."""
    from apps.stores.models import Stores

    stores = Stores.objects.filter(is_active=True)
    total = 0

    for store in stores:
        try:
            compute_daily_store_report(store, report_date)
            compute_daily_employee_reports(store, report_date)
            total += 1
        except Exception as e:
            logger.error(f"Rapor hesaplama hatası: store={store}, date={report_date}, err={e}")

    return total


def get_dashboard_summary(store, target_date=None):
    """
    Dashboard KPI kartları için optimize edilmiş veri sağlayıcı.
    Önce DailyStoreReport'tan bakar; yoksa canlı hesaplar ve cache'e yazar.

    Dönüş: dict — tüm KPI metrikleri.

    FAZ C: Gümüş/Pırlanta/Saat stok özetleri de dict'e eklendi.
    Mevcut altın alanları ('stock_value_eur', 'stock_value_hs') dokunulmadı.
    """
    from apps.dashboard.models import DailyStoreReport
    from django.core.cache import cache

    if target_date is None:
        target_date = date.today()

    cache_key = f"dashboard_kpi:{store.id}:{target_date.isoformat()}"

    # Redis'te varsa direkt dön (5 dakika TTL)
    cached = cache.get(cache_key)
    if cached:
        return cached

    # DailyStoreReport'tan oku
    try:
        report = DailyStoreReport.objects.get(
            store=store, report_date=target_date,
        )
    except DailyStoreReport.DoesNotExist:
        # Henüz hesaplanmamış → canlı hesapla
        report = compute_daily_store_report(store, target_date)

    result = {
        'report_date': target_date.isoformat(),
        # Satış
        'total_sales_eur': report.total_sales_eur,
        'total_sales_hs': report.total_sales_hs,
        'sale_count': report.sale_count,
        'unique_customers': report.unique_customers,
        # Alış
        'total_purchases_eur': report.total_purchases_eur,
        'total_purchases_hs': report.total_purchases_hs,
        'purchase_count': report.purchase_count,
        # İade
        'total_returns_eur': report.total_returns_eur,
        'return_count': report.return_count,
        # Kâr
        'total_gross_profit': report.total_gross_profit,
        'total_net_profit': report.total_net_profit,
        # Kasa
        'cash_in': report.cash_in,
        'cash_out': report.cash_out,
        'card_in': report.card_in,
        'card_out': report.card_out,
        'transfer_in': report.transfer_in,
        'transfer_out': report.transfer_out,
        'commission_total': report.commission_total,
        'net_cash_flow': report.net_cash_flow,
        # Stok (Altın — mevcut)
        'stock_value_eur': report.stock_value_eur,
        'stock_value_hs': report.stock_value_hs,
        # FAZ C: Stok (Gümüş)
        'silver_stock_gram': report.silver_stock_gram,
        'silver_stock_value_hg': report.silver_stock_value_hg,
        'silver_stock_value_eur': report.silver_stock_value_eur,
        # FAZ C: Stok (Pırlanta)
        'diamond_stock_pieces': report.diamond_stock_pieces,
        'diamond_stock_value_eur': report.diamond_stock_value_eur,
        # FAZ C: Stok (Saat)
        'watch_stock_pieces': report.watch_stock_pieces,
        'watch_stock_value_eur': report.watch_stock_value_eur,
        # Meta
        'transaction_count': report.transaction_count,
        'computed_at': report.computed_at.isoformat() if report.computed_at else None,
    }

    # Redis'e yaz (5 dakika)
    cache.set(cache_key, result, timeout=300)

    return result


def get_date_range_summary(store, start_date, end_date):
    """
    Tarih aralığı için toplam KPI metrikleri.
    DailyStoreReport'tan aggregate ile çeker — milisaniyeler.

    FAZ C: Gümüş/Pırlanta/Saat alanları da aggregate edilir.
    Mevcut alanların semantiği bozulmaz.
    """
    from apps.dashboard.models import DailyStoreReport

    agg = (
        DailyStoreReport.objects
        .filter(store=store, report_date__gte=start_date, report_date__lte=end_date)
        .aggregate(
            total_sales_eur=Coalesce(Sum('total_sales_eur'), ZERO2, output_field=DecimalField()),
            total_sales_hs=Coalesce(Sum('total_sales_hs'), ZERO4, output_field=DecimalField()),
            total_purchases_eur=Coalesce(Sum('total_purchases_eur'), ZERO2, output_field=DecimalField()),
            total_purchases_hs=Coalesce(Sum('total_purchases_hs'), ZERO4, output_field=DecimalField()),
            total_returns_eur=Coalesce(Sum('total_returns_eur'), ZERO2, output_field=DecimalField()),
            total_gross_profit=Coalesce(Sum('total_gross_profit'), ZERO2, output_field=DecimalField()),
            total_net_profit=Coalesce(Sum('total_net_profit'), ZERO2, output_field=DecimalField()),
            sale_count=Coalesce(Sum('sale_count'), Value(0)),
            purchase_count=Coalesce(Sum('purchase_count'), Value(0)),
            transaction_count=Coalesce(Sum('transaction_count'), Value(0)),
            cash_in=Coalesce(Sum('cash_in'), ZERO2, output_field=DecimalField()),
            cash_out=Coalesce(Sum('cash_out'), ZERO2, output_field=DecimalField()),
            card_in=Coalesce(Sum('card_in'), ZERO2, output_field=DecimalField()),
            card_out=Coalesce(Sum('card_out'), ZERO2, output_field=DecimalField()),
            transfer_in=Coalesce(Sum('transfer_in'), ZERO2, output_field=DecimalField()),
            transfer_out=Coalesce(Sum('transfer_out'), ZERO2, output_field=DecimalField()),
            commission_total=Coalesce(Sum('commission_total'), ZERO2, output_field=DecimalField()),
            # FAZ C: Çoklu Maden stok alanları (null=True alanlara SUM uygular;
            # NULL değerler SUM tarafından ihmal edilir, eski satırlarda sorun yaratmaz).
            silver_stock_gram=Coalesce(Sum('silver_stock_gram'), ZERO4, output_field=DecimalField()),
            silver_stock_value_hg=Coalesce(Sum('silver_stock_value_hg'), ZERO4, output_field=DecimalField()),
            silver_stock_value_eur=Coalesce(Sum('silver_stock_value_eur'), ZERO2, output_field=DecimalField()),
            diamond_stock_pieces=Coalesce(Sum('diamond_stock_pieces'), Value(0)),
            diamond_stock_value_eur=Coalesce(Sum('diamond_stock_value_eur'), ZERO2, output_field=DecimalField()),
            watch_stock_pieces=Coalesce(Sum('watch_stock_pieces'), Value(0)),
            watch_stock_value_eur=Coalesce(Sum('watch_stock_value_eur'), ZERO2, output_field=DecimalField()),
        )
    )

    # Net hesaplamalar
    agg['net_sales_tl'] = agg['total_sales_eur'] - agg['total_purchases_eur'] - agg['total_returns_eur']
    total_in = agg['cash_in'] + agg['card_in'] + agg['transfer_in']
    total_out = agg['cash_out'] + agg['card_out'] + agg['transfer_out']
    agg['net_cash_flow'] = total_in - total_out

    return agg


def get_store_assets_summary(store):
    """
    Mağazanın o anki fiziksel ve finansal varlık özeti.

    Performans:
        - Toplam 4 SQL sorgusu (kasa bakiye + stok breakdown + kategori dağılımı + ayar dağılımı).
        - N+1 yok; tüm toplamlar veritabanında aggregate/annotate ile yapılır.
        - Redis cache'i view katmanında uygulanır.

    Kurallar:
        - Kasa bakiyeleri Payment tablosundan SSOT kuralıyla okunur
          (BankAccount üzerinde saklanan statik alan yerine hareket toplamı).
        - Döviz ürünleri (is_currency=True) stok toplamlarına dahil edilmez;
          bu ürünler Kasa Yönetimi üzerinden takip edilir.

    FAZ C Yenilikleri:
        - Stok aggregate'i Conditional Aggregation ile Altın/Gümüş/Pırlanta/Saat
          breakdown'ı üretir. Tek SQL sorgusu.
        - 'stock_summary' dict'ine yeni anahtarlar eklenir:
            gold_value_hs, gold_value_tl,
            silver_gram, silver_value_hg, silver_value_tl,
            diamond_pieces, diamond_value_tl,
            watch_pieces, watch_value_tl.
        - Mevcut 'total_has_value' artık material_type='GOLD' filtresi ile
          netleştirildi (HS ve HG karışımı engellendi). Altın-öncesi kayıtlar
          default='GOLD' olduğundan mevcut raporlarda sayısal sonuç DEĞİŞMEZ.
        - 'cash_total_by_currency' dinamik; HG (Has Gümüş) kasa hesabı
          oluşturulursa KOD DEĞİŞİKLİĞİ OLMADAN otomatik kırılım verir.
    """
    from apps.banking.models import BankAccount
    from apps.process.models import Payment
    from apps.stock_management.models import StockSnapshot

    # ─── 1. Kasa / POS / Banka bakiyeleri (tek SQL, GROUP BY hesap) ──
    # NOT (FAZ C teyidi): Aşağıdaki .values(..., 'bank_account__currency')
    # ifadesi BankAccount.currency alanını dinamik olarak GROUP BY yapar.
    # Dolayısıyla gelecekte 'HG' veya başka bir currency koduna sahip bir
    # BankAccount oluşturulursa, otomatik olarak cash_accounts listesinde ve
    # cash_total_by_currency kırılımında yer alır. Ek kod gerekmez.
    account_rows = (
        Payment.objects
        .filter(
            bank_account__store=store,
            bank_account__is_deleted=False,
            bank_account__is_active=True,
            is_cancelled=False,
        )
        .values(
            'bank_account__id',
            'bank_account__name',
            'bank_account__bank_name',
            'bank_account__account_type',
            'bank_account__currency',
        )
        .annotate(
            balance=Coalesce(
                Sum(
                    Case(
                        When(is_output=False, then=F('amount')),
                        When(is_output=True, then=-F('amount')),
                        output_field=DecimalField(),
                    )
                ),
                ZERO2,
                output_field=DecimalField(),
            )
        )
    )

    # Hareketsiz hesapları da listeye dahil et (bakiyesi 0)
    existing_ids = {row['bank_account__id'] for row in account_rows}
    all_accounts = BankAccount.objects.filter(
        store=store, is_deleted=False, is_active=True,
    ).values('id', 'name', 'bank_name', 'account_type', 'currency')

    cash_accounts = []
    pos_bank_accounts = []
    cash_total_by_currency = {}

    def _push(entry):
        if entry['account_type'] == 'CASH':
            cash_accounts.append(entry)
            cur = entry['currency']
            cash_total_by_currency[cur] = cash_total_by_currency.get(cur, 0.0) + entry['balance']
        else:
            pos_bank_accounts.append(entry)

    for row in account_rows:
        _push({
            'id': str(row['bank_account__id']),
            'name': row['bank_account__name'],
            'bank_name': row['bank_account__bank_name'] or '',
            'account_type': row['bank_account__account_type'],
            'currency': row['bank_account__currency'] or 'TRY',
            'balance': float(row['balance'] or 0),
        })

    for acc in all_accounts:
        if acc['id'] in existing_ids:
            continue
        _push({
            'id': str(acc['id']),
            'name': acc['name'],
            'bank_name': acc['bank_name'] or '',
            'account_type': acc['account_type'],
            'currency': acc['currency'] or 'TRY',
            'balance': 0.0,
        })

    # ─── 2. Stok Değeri — Çoklu Maden Conditional Aggregation (TEK SQL) ──
    # Döviz ürünleri (is_currency=True) stok hesabından hariç tutulur.
    # stock_gram__gt=0 filtresi KALDIRILDI: Saat/Pırlanta stock_gram=0'dır;
    # 0 değerler SUM'a sıfır katkı yapar, performans etkisi önemsizdir.
    #
    # FAZ 34 (2026-05-01) — soft-delete filtresi eklendi.
    # Silinmis (is_deleted=True) urunlerin StockSnapshot kayitlari, append-only
    # mimari geregi DB'de kalir; ancak Dashboard ozetinde gosterilmemeleri
    # gerekir. Bu filtre olmadan silinen urunler hem 'Stok HAS Degeri' hem
    # 'Toplam Stok Gram' kartlarina sayisal katki yapiyordu (saha bulgusu:
    # uretim sonrasi silinen ozel urunlerin stok_gram x stale_WAC carpiminin
    # 582.85 HS gibi imkansiz degerleri tetikledi).
    # FAZ 26.2 ayni filtreyi yeni assets_v2 akisina eklemisti; eski endpoint
    # ise atlanmisti — bu duzeltme paritteyi tamamlar.
    stock_base_qs = (
        StockSnapshot.objects
        .filter(store=store, product__is_deleted=False)
        .exclude(product__is_currency=True)
    )

    # Altın breakdown + Gümüş/Pırlanta/Saat (tek sorguda)
    multi_material = _build_multi_material_stock_aggregate()
    stock_agg = stock_base_qs.aggregate(
        # Mevcut özet alanlar (backwards compatible)
        total_has_value=Coalesce(
            Sum(
                F('stock_gram') * F('weighted_avg_cost_hs'),
                filter=_Q_GOLD,  # FAZ C: HS semantiği korunsun — sadece GOLD
                output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
            ZERO4,
            output_field=DecimalField(max_digits=20, decimal_places=4),
        ),
        total_gram=Coalesce(Sum('stock_gram'), ZERO4, output_field=DecimalField()),
        total_pieces=Coalesce(Sum('stock_pieces'), Value(0)),
        # FAZ C: Çoklu Maden breakdown
        **multi_material,
    )

    # ─── 3. Kategori Bazlı Gramajlar (tek SQL, GROUP BY kategori) ────
    category_rows = (
        stock_base_qs
        .values('product__category__name')
        .annotate(
            total_gram=Coalesce(Sum('stock_gram'), ZERO4, output_field=DecimalField()),
            total_pieces=Coalesce(Sum('stock_pieces'), Value(0)),
        )
        .order_by('product__category__name')
    )
    categories = [
        {
            'name': row['product__category__name'] or 'Kategorisiz',
            'total_gram': float(row['total_gram'] or 0),
            'total_pieces': int(row['total_pieces'] or 0),
        }
        for row in category_rows
    ]

    # ─── 4. Ayar / Özel Tip Bazlı Kırılım (tek SQL aggregate) ────────
    # 14 Ayar, 22 Ayar, 18 Ayar vb. Products.name üzerinde yakalanır;
    # Hurda is_scrap bayrağından, Bilezik ise kategori adından okunur.
    # FAZ C: Bu kırılım ALTIN'a özgüdür; material_type='GOLD' filtresi
    # açıkça eklenmiştir — Gümüşün 925/835 değerleri buraya karışmaz.
    karat_agg = stock_base_qs.aggregate(
        k14_gram=Coalesce(
            Sum('stock_gram', filter=(
                Q(product__name__icontains='14 Ayar') &
                Q(product__is_scrap=False) &
                _Q_GOLD
            )),
            ZERO4, output_field=DecimalField(),
        ),
        k18_gram=Coalesce(
            Sum('stock_gram', filter=(
                Q(product__name__icontains='18 Ayar') &
                Q(product__is_scrap=False) &
                _Q_GOLD
            )),
            ZERO4, output_field=DecimalField(),
        ),
        k22_gram=Coalesce(
            Sum('stock_gram', filter=(
                Q(product__name__icontains='22 Ayar') &
                Q(product__is_scrap=False) &
                _Q_GOLD
            )),
            ZERO4, output_field=DecimalField(),
        ),
        scrap_gram=Coalesce(
            Sum('stock_gram', filter=(
                Q(product__is_scrap=True) & _Q_GOLD
            )),
            ZERO4, output_field=DecimalField(),
        ),
        bracelet_gram=Coalesce(
            Sum('stock_gram', filter=(
                Q(product__category__name__iexact='Bilezik') & _Q_GOLD
            )),
            ZERO4, output_field=DecimalField(),
        ),
    )

    return {
        'cash_accounts': cash_accounts,
        'pos_bank_accounts': pos_bank_accounts,
        'cash_total_by_currency': {k: float(v) for k, v in cash_total_by_currency.items()},
        'stock_summary': {
            # Mevcut alanlar (backwards compatible)
            'total_has_value': float(stock_agg['total_has_value'] or 0),
            'total_gram': float(stock_agg['total_gram'] or 0),
            'total_pieces': int(stock_agg['total_pieces'] or 0),
            'categories': categories,
            # FAZ C: Çoklu Maden breakdown (yeni anahtarlar)
            'gold_value_hs': float(stock_agg['gold_value_hs'] or 0),
            'gold_value_tl': float(stock_agg['gold_value_tl'] or 0),
            'silver_gram': float(stock_agg['silver_gram'] or 0),
            'silver_value_hg': float(stock_agg['silver_value_hg'] or 0),
            'silver_value_tl': float(stock_agg['silver_value_tl'] or 0),
            'diamond_pieces': int(stock_agg['diamond_pieces'] or 0),
            'diamond_value_tl': float(stock_agg['diamond_value_tl'] or 0),
            'watch_pieces': int(stock_agg['watch_pieces'] or 0),
            'watch_value_tl': float(stock_agg['watch_value_tl'] or 0),
        },
        'karat_breakdown': {
            'k14_gram': float(karat_agg['k14_gram'] or 0),
            'k18_gram': float(karat_agg['k18_gram'] or 0),
            'k22_gram': float(karat_agg['k22_gram'] or 0),
            'scrap_gram': float(karat_agg['scrap_gram'] or 0),
            'bracelet_gram': float(karat_agg['bracelet_gram'] or 0),
        },
        'generated_at': timezone.now().isoformat(),
    }


# ============================================================================
# FAZ 26 (2026-05-01): Patron Odaklı Dashboard — TAB 1 (Mağaza Varlıkları)
# ============================================================================
# Yeni 3 sekmeli dashboard mimarisinde TAB 1 ("Mağaza Varlıkları ve Stok"),
# patronun günlük operasyonel kartlardan önce görmek istediği "dükkanın net
# röntgeni"ni sunar:
#
#   1) Fiziksel Stok HAS (StockSnapshot.gold_value_hs)
#   2) Tedarikçi Borcu HAS (SupplierLedger üzerinden NET)
#   3) NET HAS = (1) − (2)
#
# Ayrıca kategori bazlı stok detayları (22/18/14 Ayar, Bilezik, Hurda,
# Sarrafiye) HER SATIRDA gram + has + adet + WAC bilgisiyle döner.
#
# Sarrafiye İstisnası (Patronun Kararı):
#   Sarrafiye ürünleri (Yeni/Eski Çeyrek, Yarım, Tam, Ata, Gremse, 5li Ata)
#   has hesabında WAC ile değil, ürünün sabit "gold_dry" çarpanıyla yapılır.
#   1 Çeyrek için gold_dry≈1.6 gibi piyasada kabul görmüş has karşılığı
#   kullanılır. Has = stock_pieces × gold_dry.
#
# Tedarikçi Borç Kapsamı:
#   SupplierLedger.store alanı yoktur; mağaza kapsamı Process üzerinden
#   bağlanan supplier_id listesiyle filtrelenir. Yalnızca currency='HS' ve
#   is_active=True satırlar dikkate alınır. ENTRY=borç, EXIT=alacak.
#
# Cache:
#   View katmanında (assets_v2_view) Redis 5dk TTL ile uygulanır. Bu fonksiyon
#   doğrudan cache yönetmez; her çağrıda canlı SQL çalıştırır.
# ============================================================================

# Sarrafiye Has hesabında özel davranış uygulanan ürün adları.
# Live Board _coin_targets ile simetrik tutulmalıdır
# (apps/live_board/views.py:211).
SARRAFIYE_PRODUCT_NAMES = [
    'Yeni Çeyrek', 'Eski Çeyrek',
    'Yeni Yarım',  'Eski Yarım',
    'Yeni Tam',    'Eski Tam',
    'Yeni Ata',    'Eski Ata',
    'Yeni 5li Ata','Eski 5li Ata',
    'Yeni Gremse', 'Eski Gremse',
]


def _compute_supplier_debt_hs(store):
    """
    Mağazaya bağlı tedarikçilerin SupplierLedger üzerinden NET HS borcunu
    döner. Detay listesi de birlikte üretilir (accordion için).

    Dönüş:
        {
            'net_debt_hs': Decimal,       # ENTRY toplamı − EXIT toplamı
            'total_entry_hs': Decimal,    # Borç toplamı (mağaza alınanlar)
            'total_exit_hs': Decimal,     # Alacak toplamı (ödenenler)
            'per_supplier': [
                {
                    'supplier_id': str,
                    'company_name': str,
                    'account_type': 'SUPPLIER' | 'CANTACI',
                    'borc_hs': float,
                    'alacak_hs': float,
                    'net_hs': float,        # >0 ise bizim borcumuz var
                },
                ...
            ],
        }

    Mağaza kapsamı: Process tablosunda bu mağazaya yazılmış (is_deleted=False)
    en az bir kaydı olan tedarikçiler. SupplierLedger'da store alanı yok;
    bu indirekt kapsamlama tek doğru yol.
    """
    from apps.suppliers.models import SupplierLedger
    from apps.process.models import Process

    store_supplier_ids = list(
        Process.objects
        .filter(store=store, supplier__isnull=False, is_deleted=False)
        .values_list('supplier_id', flat=True)
        .distinct()
    )

    if not store_supplier_ids:
        return {
            'net_debt_hs': ZERO4,
            'total_entry_hs': ZERO4,
            'total_exit_hs': ZERO4,
            'per_supplier': [],
        }

    # Toplam (tek aggregate sorgusu)
    totals = (
        SupplierLedger.objects
        .filter(
            supplier_id__in=store_supplier_ids,
            is_active=True,
            currency='HS',
        )
        .aggregate(
            total_entry_hs=Coalesce(
                Sum('amount_value', filter=Q(transaction_type='ENTRY')),
                ZERO4, output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
            total_exit_hs=Coalesce(
                Sum('amount_value', filter=Q(transaction_type='EXIT')),
                ZERO4, output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
        )
    )
    net_debt = (totals['total_entry_hs'] or ZERO4) - (totals['total_exit_hs'] or ZERO4)

    # Tedarikçi bazlı detay (tek GROUP BY sorgusu)
    per_rows = (
        SupplierLedger.objects
        .filter(
            supplier_id__in=store_supplier_ids,
            is_active=True,
            currency='HS',
        )
        .values(
            'supplier_id',
            'supplier__company_name',
            'supplier__account_type',
        )
        .annotate(
            borc_hs=Coalesce(
                Sum('amount_value', filter=Q(transaction_type='ENTRY')),
                ZERO4, output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
            alacak_hs=Coalesce(
                Sum('amount_value', filter=Q(transaction_type='EXIT')),
                ZERO4, output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
        )
    )

    per_supplier = []
    for row in per_rows:
        borc = row['borc_hs'] or ZERO4
        alacak = row['alacak_hs'] or ZERO4
        net = borc - alacak
        # Net=0 olan tedarikçiyi listeleme (gürültü); ama borç/alacak
        # asimetrisi varsa görünmesi için net != 0 koşulu yeterli.
        if borc == ZERO4 and alacak == ZERO4:
            continue
        per_supplier.append({
            'supplier_id': str(row['supplier_id']),
            'company_name': row['supplier__company_name'] or '-',
            'account_type': row['supplier__account_type'] or 'SUPPLIER',
            'borc_hs': float(borc),
            'alacak_hs': float(alacak),
            'net_hs': float(net),
        })

    # Net borç pozitiften negatife sıralı (en çok borçlu üstte)
    per_supplier.sort(key=lambda x: x['net_hs'], reverse=True)

    return {
        'net_debt_hs': net_debt,
        'total_entry_hs': totals['total_entry_hs'] or ZERO4,
        'total_exit_hs': totals['total_exit_hs'] or ZERO4,
        'per_supplier': per_supplier,
    }


def _compute_karat_breakdown_with_has(store):
    """
    Ayar/tip bazlı stok kırılımını HAS değerleriyle birlikte üretir.

    Mevcut get_store_assets_summary'deki karat_breakdown sadece gramaj veriyor.
    Bu fonksiyon her satır için ek olarak:
        - has_value:    SUM(stock_gram × weighted_avg_cost_hs)
        - pieces:       SUM(stock_pieces)
    bilgisini de döndürür. Material_type='GOLD' filtresi ile sınırlıdır;
    Sarrafiye ürünleri ayrı bir satır olarak hesaplanır
    (_compute_sarrafiye_has) ve buradaki gruplara dahil değildir
    (Bilezik ve isimle eşleşen ayar gruplarına çift sayım olmaması için
    SARRAFIYE_PRODUCT_NAMES ürünleri DIŞARIDA bırakılır).

    Dönüş:
        [
            {'tip': '22 Ayar',  'gram': float, 'has': float, 'pieces': int, 'wac_hs': float},
            {'tip': '18 Ayar',  ...},
            {'tip': '14 Ayar',  ...},
            {'tip': 'Bilezik',  ...},
            {'tip': 'Hurda',    ...},
        ]
    Boş satırlar (gram=0 ve pieces=0) atlanır.
    """
    from apps.stock_management.models import StockSnapshot

    base_qs = (
        StockSnapshot.objects
        .filter(store=store, product__is_deleted=False)
        .exclude(product__is_currency=True)
        .exclude(product__name__in=SARRAFIYE_PRODUCT_NAMES)
    )

    # Tek aggregate ile 5 satırı üret
    F_GRAM = F('stock_gram')
    F_WAC = F('weighted_avg_cost_hs')
    HAS_EXPR = F_GRAM * F_WAC

    DEC = DecimalField(max_digits=20, decimal_places=4)

    # 5 ana isim kalıbı + bunların DIŞINDA kalan GOLD ürünler için "Diğer Altın"
    # satırı (FAZ 26 BUG 3b düzeltmesi). Böylece adı 22/18/14 Ayar, Bilezik veya
    # Hurda kalıbına uymayan altın ürünler de detay tabloda görünür ve hero kart
    # toplamı ile detay tablo toplamı tutar.
    q_22 = Q(product__name__icontains='22 Ayar') & Q(product__is_scrap=False)
    q_18 = Q(product__name__icontains='18 Ayar') & Q(product__is_scrap=False)
    q_14 = Q(product__name__icontains='14 Ayar') & Q(product__is_scrap=False)
    q_bilezik = Q(product__category__name__iexact='Bilezik')
    q_hurda = Q(product__is_scrap=True)
    # "Diğer Altın": GOLD ama yukarıdaki hiçbir kategoriye girmeyen ürünler.
    q_diger = _Q_GOLD & ~(q_22 | q_18 | q_14 | q_bilezik | q_hurda)

    karat_filters = [
        ('22 Ayar',     q_22 & _Q_GOLD),
        ('18 Ayar',     q_18 & _Q_GOLD),
        ('14 Ayar',     q_14 & _Q_GOLD),
        ('Bilezik',     q_bilezik & _Q_GOLD),
        ('Hurda',       q_hurda & _Q_GOLD),
        ('Diğer Altın', q_diger),
    ]

    def _label_to_key(lbl):
        """Etiketten ORM aggregate alias key'i üret. Türkçe karakter normalize."""
        s = lbl.replace(' ', '_').lower()
        return (
            s.replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u')
             .replace('ş', 's').replace('ç', 'c').replace('ö', 'o')
        )

    agg_kwargs = {}
    for label, q in karat_filters:
        key = _label_to_key(label)  # '22 Ayar' -> '22_ayar', 'Diğer Altın' -> 'diger_altin'
        agg_kwargs[f'{key}_gram'] = Coalesce(
            Sum('stock_gram', filter=q), ZERO4, output_field=DEC,
        )
        agg_kwargs[f'{key}_has'] = Coalesce(
            Sum(HAS_EXPR, filter=q, output_field=DEC), ZERO4, output_field=DEC,
        )
        agg_kwargs[f'{key}_pieces'] = Coalesce(
            Sum('stock_pieces', filter=q), Value(0), output_field=IntegerField(),
        )

    agg = base_qs.aggregate(**agg_kwargs)

    rows = []
    for label, q in karat_filters:
        key = _label_to_key(label)
        gram = agg[f'{key}_gram'] or ZERO4
        has  = agg[f'{key}_has']  or ZERO4
        pcs  = int(agg[f'{key}_pieces'] or 0)
        if gram == ZERO4 and pcs == 0:
            continue
        wac = float(has / gram) if gram and gram != ZERO4 else 0.0
        row = {
            'tip': label,
            'gram': float(gram),
            'has': float(has),
            'pieces': pcs,
            'wac_hs': round(wac, 4),
        }

        # FAZ 26.3: "Diğer Altın" ve WAC anomali içeren satırlar için ürün
        # bazlı detay liste. Patron WAC > 1.0 (matematiksel olarak imkansız —
        # saf altın bile 1.0 HS/gr'dır) bir satır gördüğünde hangi ürünün veri
        # bütünlüğünü bozduğunu hızlıca tespit edebilsin.
        if label == 'Diğer Altın' or wac > 1.05:
            details_qs = (
                base_qs.filter(q)
                .values(
                    'product__id',
                    'product__name',
                    'product__category__name',
                )
                .annotate(
                    p_gram=Coalesce(Sum('stock_gram'), ZERO4, output_field=DEC),
                    p_has=Coalesce(Sum(HAS_EXPR, output_field=DEC), ZERO4, output_field=DEC),
                    p_pieces=Coalesce(Sum('stock_pieces'), Value(0), output_field=IntegerField()),
                )
                .order_by('-p_has')
            )
            details = []
            for d in details_qs:
                d_gram = d['p_gram'] or ZERO4
                d_has = d['p_has'] or ZERO4
                d_pcs = int(d['p_pieces'] or 0)
                if d_gram == ZERO4 and d_pcs == 0:
                    continue
                d_wac = float(d_has / d_gram) if d_gram and d_gram != ZERO4 else 0.0
                details.append({
                    'product_id': str(d['product__id']),
                    'product_name': d['product__name'] or '-',
                    'category': d['product__category__name'] or '-',
                    'gram': float(d_gram),
                    'has': float(d_has),
                    'pieces': d_pcs,
                    'wac_hs': round(d_wac, 4),
                    'is_anomaly': d_wac > 1.05,  # Altın için WAC > 1 imkansız
                })
            row['details'] = details

        rows.append(row)
    return rows


def get_tab1_assets_data(store):
    """
    Patron Odaklı Dashboard — TAB 1 (Mağaza Varlıkları ve Stok) tek-shot veri.

    UI sözleşmesi (templates/management/dashboard/index.html → tab-assets):
        net_summary       : 3 hero kart için NET HAS hesabı
        kasa              : Para birimine göre nakit toplamları
        banka_pos_toplam_try : Banka/POS hesaplarının TRY bakiyesi
        stok_kirilimlari  : Karat/tip bazlı detay tablo satırları
        tedarikci_borclari: Accordion için tedarikçi bazlı NET HS borç listesi

    Performans:
        Toplam ~6 SQL sorgusu — N+1 yok, hepsi aggregate.

    Dış bağımlılıklar:
        - apps.banking.models.BankAccount
        - apps.process.models.Payment, Process
        - apps.stock_management.models.StockSnapshot
        - apps.suppliers.models.SupplierLedger
        - apps.products.models.Products (gold_dry alanı Sarrafiye için)

    Mağaza yoksa (store=None) tüm değerler sıfır/boş olarak döner; UI'ın
    çökmemesi için defansif yapı korunur.
    """
    from apps.banking.models import BankAccount
    from apps.process.models import Payment
    from apps.stock_management.models import StockSnapshot

    if not store:
        return {
            'net_summary': {
                'stok_has':            0.0,
                'stok_gram':           0.0,
                'tedarikci_borcu_hs':  0.0,
                'net_has':             0.0,
                'net_has_pozitif':     True,
            },
            'kasa': {},
            'banka_pos_toplam_try': 0.0,
            'stok_kirilimlari': [],
            'tedarikci_borclari': [],
            'generated_at': timezone.now().isoformat(),
        }

    # ─── 1. Stok HS özet (GOLD + Sarrafiye birleşik) ─────────────────
    # FAZ 26 düzeltmeleri:
    #   BUG 1+3a: total_gram'a _Q_GOLD filtresi eklendi — hero kartı yalnızca
    #             altın gramını göstersin. Önceden Gümüş/Pırlanta/Saat gramları
    #             da toplam gram'a giriyordu.
    #   BUG 3c:   Sarrafiye ürünleri stock_agg'dan hariç tutuldu — çift sayım
    #             önleme. _compute_sarrafiye_has() ayrıca pieces × gold_dry
    #             üzerinden hesaplıyor; eğer Sarrafiye ürünlerinde stock_gram>0
    #             varsa hem WAC üzerinden hem gold_dry üzerinden iki kez
    #             sayılma riski vardı. Artık WAC sayımı yapılmıyor.
    stock_agg = (
        StockSnapshot.objects
        .filter(store=store, product__is_deleted=False)
        .exclude(product__is_currency=True)
        .exclude(product__name__in=SARRAFIYE_PRODUCT_NAMES)
        .aggregate(
            gold_value_hs=Coalesce(
                Sum(
                    F('stock_gram') * F('weighted_avg_cost_hs'),
                    filter=_Q_GOLD,
                    output_field=DecimalField(max_digits=20, decimal_places=4),
                ),
                ZERO4,
                output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
            total_gram=Coalesce(
                Sum('stock_gram', filter=_Q_GOLD),
                ZERO4,
                output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
        )
    )
    total_stok_has  = stock_agg['gold_value_hs'] or ZERO4
    total_stok_gram = stock_agg['total_gram'] or ZERO4

    # ─── 2. Karat/tip bazlı kırılım (HAS değerleriyle) ───────────────
    rows = _compute_karat_breakdown_with_has(store)

    # ─── 3. Kasa / Banka / POS bakiyeleri ────────────────────────────
    account_rows = (
        Payment.objects
        .filter(
            bank_account__store=store,
            bank_account__is_deleted=False,
            bank_account__is_active=True,
            is_cancelled=False,
        )
        .values(
            'bank_account__id',
            'bank_account__account_type',
            'bank_account__currency',
        )
        .annotate(
            balance=Coalesce(
                Sum(
                    Case(
                        When(is_output=False, then=F('amount')),
                        When(is_output=True,  then=-F('amount')),
                        output_field=DecimalField(),
                    )
                ),
                ZERO2, output_field=DecimalField(),
            )
        )
    )
    existing_ids = {r['bank_account__id'] for r in account_rows}
    all_accounts = BankAccount.objects.filter(
        store=store, is_deleted=False, is_active=True,
    ).values('id', 'account_type', 'currency')

    kasa = {}
    banka_pos_toplam_try = 0.0
    for r in account_rows:
        cur = (r['bank_account__currency'] or 'TRY').upper()
        bal = float(r['balance'] or 0)
        if r['bank_account__account_type'] == 'CASH':
            kasa[cur] = kasa.get(cur, 0.0) + bal
        else:
            # Banka/POS: yalnızca TRY toplanır (mevcut UI tek alan)
            if cur == 'TRY':
                banka_pos_toplam_try += bal
    for acc in all_accounts:
        if acc['id'] in existing_ids:
            continue
        cur = (acc['currency'] or 'TRY').upper()
        if acc['account_type'] == 'CASH':
            kasa.setdefault(cur, 0.0)

    # ─── 4. Tedarikçi NET HS borcu + detay liste ─────────────────────
    debt = _compute_supplier_debt_hs(store)
    tedarikci_borcu_hs = float(debt['net_debt_hs'])

    # ─── 5. NET HAS = Stok Has − Tedarikçi Borcu Has ─────────────────
    stok_has_f = float(total_stok_has)
    net_has = stok_has_f - tedarikci_borcu_hs

    # ─── 6. FAZ 48.7: Emanet Stoğu KPI ──────────────────────────────
    # custody_gram: müşteri emaneti (satışa AÇIK DEĞİL, yansıma yok)
    emanet_agg = (
        StockSnapshot.objects
        .filter(store=store, product__is_deleted=False)
        .aggregate(
            emanet_gram=Coalesce(
                Sum('custody_gram'),
                ZERO4,
                output_field=DecimalField(max_digits=20, decimal_places=4),
            ),
            emanet_pieces=Coalesce(
                Sum('custody_pieces'),
                0,
            ),
        )
    )
    emanet_gram_f = float(emanet_agg['emanet_gram'] or 0)
    emanet_pieces_i = int(emanet_agg['emanet_pieces'] or 0)

    return {
        'net_summary': {
            'stok_has':           round(stok_has_f, 4),
            'stok_gram':          round(float(total_stok_gram), 4),
            'tedarikci_borcu_hs': round(tedarikci_borcu_hs, 4),
            'net_has':            round(net_has, 4),
            'net_has_pozitif':    net_has >= 0,
            # FAZ 48.7: Emanet havuzu (satışa katkısı yok, bilgilendirme amaçlı)
            'emanet_gram':        round(emanet_gram_f, 4),
            'emanet_pieces':      emanet_pieces_i,
        },
        'kasa': {k: round(v, 2) for k, v in kasa.items()},
        'banka_pos_toplam_try': round(banka_pos_toplam_try, 2),
        'stok_kirilimlari': rows,
        'tedarikci_borclari': debt['per_supplier'],
        'generated_at': timezone.now().isoformat(),
    }


def get_employee_performance(store, start_date, end_date):
    """
    Personel performans raporu: Tarih aralığında personel bazında satış özeti.
    DailyEmployeeReport'tan aggregate ile çeker.

    FAZ C NOTU: Personel raporu material_type kırılımı içermez; değişmedi.
    """
    from apps.dashboard.models import DailyEmployeeReport

    return (
        DailyEmployeeReport.objects
        .filter(
            store=store,
            report_date__gte=start_date,
            report_date__lte=end_date,
        )
        .values(
            'employee_id',
            'employee__first_name',
            'employee__last_name',
        )
        .annotate(
            total_sale_count=Sum('sale_count'),
            total_sales_eur=Sum('total_sales_eur'),
            total_sales_hs=Sum('total_sales_hs'),
            total_gross_profit=Sum('total_gross_profit'),
            total_purchase_count=Sum('purchase_count'),
            total_purchases_eur=Sum('total_purchases_eur'),
            total_transaction_count=Sum('transaction_count'),
        )
        .order_by('-total_sales_eur')
    )
