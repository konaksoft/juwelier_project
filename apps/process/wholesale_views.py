import json
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from apps.products.models import Products
from apps.process.models import Process
from apps.definitions.categories.models import Categories
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import IntegerField, Sum, DecimalField, F, Q
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.invoices.models import *
from apps.process.models import Process
from apps.process.views import generate_process_no, update_product_stock

# --- FAZ 3: StockService ve StockSnapshot entegrasyonu ---
from apps.stock_management.services.stock_service import StockService
from apps.stock_management.models import StockSnapshot, StockLedger
from apps.stock_management.services.price_service import PriceService
from apps.products.models import Products
from apps.suppliers.models import Suppliers, SupplierLedger
from apps.scraps.models import Scraps
from apps.bracelets.models import Bracelets
from apps.helpers.numbers import parse_decimal_locale

RAW_CODES = {
    'AUDTRY': 'AUD', 'CADTRY': 'CAD', 'CHFTRY': 'CHF', 'EURTRY': 'EUR',
    'EURKG': 'EURKG',
    'EURUSD': 'EURUSD',
    'GBPTRY': 'GBP', 'JPYTRY': 'JPY', 'QARTRY': 'QAR', 'SARTRY': 'SAR',
    'USDTRY': 'USD',
    'USDKG': 'USDKG',
    'Has Altın': 'HS',
    'TRY - Türk Lirası': 'TRY',
}

TOLERANCE = Decimal('0.05')
HS_PREC = Decimal('0.001')
HS_ADJUST = Decimal('0.995')


# --- YARDIMCI FONKSİYON ---
def to_decimal(val):
    """
    Gelen string değeri (örn: "1.250,50" veya "1,25") güvenli bir şekilde
    Python Decimal formatına çevirir ("1250.50" veya "1.25").
    """
    if val is None or val == '':
        return Decimal('0')

    val = str(val).strip()

    # Binlik ayracı (.) var ve ondalık ayracı (,) var ise -> 1.500,50
    if '.' in val and ',' in val:
        val = val.replace('.', '').replace(',', '.')
    # Sadece virgül varsa -> 15,50 -> 15.50
    elif ',' in val:
        val = val.replace(',', '.')

    try:
        return Decimal(val)
    except InvalidOperation:
        return Decimal('0')


def to_dec(val: str, default=Decimal('0')):
    if val is None:
        return default
    txt = str(val).strip()
    if ',' in txt:  # TR biçimi
        txt = txt.replace('.', '').replace(',', '.')
    else:  # EN biçimi
        txt = txt.replace(',', '')
    try:
        return Decimal(txt)
    except (InvalidOperation, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════
# FAZ 11 / CAT-02 — MİLYEM VALİDASYONU (2026-04-24)
# ══════════════════════════════════════════════════════════════════════════
def validate_mileage(value, *, required=True, field_label='Milyem'):
    """
    Milyem değeri validasyonu — Immutable Ledger bütünlüğünü korur.

    Geçerli aralık: 1 <= milyem <= 1000
    (Türkiye piyasası: 14K=585, 18K=750, 22K=916, 24K=999)

    required=True  → boş/sıfır/geçersiz değer reddedilir (gramlı ürünler için)
    required=False → boş/sıfır kabul edilir; doluysa aralık kontrolü uygulanır
                     (adetli ürünler: saat/elmas için)

    Amaç:
        Sıfır veya geçersiz milyem → (gram * mileage / 1000) = 0 → has değer
        sıfır → SupplierLedger'da hatalı/eksik kayıt. Bu zinciri baştan engeller.

    Returns:
        (is_valid: bool, error_msg: str | None, normalized_value: Decimal)
    """
    if value is None or (isinstance(value, str) and value.strip() == ''):
        if required:
            return False, f'{field_label} girilmelidir (1-1000 arası).', Decimal('0')
        return True, None, Decimal('0')

    try:
        val = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return False, f'{field_label} geçersiz formatta.', Decimal('0')

    if val < 0:
        return False, f'{field_label} negatif olamaz.', val

    if val == 0:
        if required:
            return False, f'{field_label} sıfır olamaz (1-1000 arası girilmelidir).', Decimal('0')
        return True, None, Decimal('0')

    if val < Decimal('1'):
        return False, f'{field_label} 1\'den küçük olamaz (geçerli aralık: 1-1000).', val

    if val > Decimal('1000'):
        return False, f'{field_label} 1000\'den büyük olamaz (geçerli aralık: 1-1000).', val

    return True, None, val


def book_supplier_tx(*, supplier, transaction_type, amount_value, currency, process_no,
                     description='', product=None, quantity_piece=0, quantity_gram=Decimal('0'),
                     auto_setoff: bool = True):
    """
    Cari kayıt açar; auto_setoff=True ise OB hariç karşı taraftaki açıkları FIFO kapatır.
    """
    # Tutar 0 veya negatifse işlem yapma (Opsiyonel güvenlik)
    if amount_value <= 0:
        return None

    new_row = SupplierLedger.objects.create(
        supplier=supplier,
        product=product,
        transaction_type=transaction_type,
        quantity_piece=quantity_piece,
        quantity_gram=quantity_gram,
        amount_value=amount_value,
        currency=(currency or 'HS').upper(),
        process_no=process_no,
        description=description,
        is_active=True
    )

    if not auto_setoff:
        return new_row

    rest = amount_value
    opposite = 'EXIT' if transaction_type == 'ENTRY' else 'ENTRY'

    qs = (SupplierLedger.objects
          .select_for_update()
          .filter(
        supplier=supplier,
        currency=new_row.currency,
        transaction_type=opposite,
        is_active=True
    )
          .exclude(process_no__startswith='OB')
          .order_by('created_on', 'id'))

    for row in qs:
        if rest <= 0:
            break
        use = min(row.amount_value, rest)
        row.amount_value -= use
        rest -= use
        if row.amount_value <= 0:
            row.amount_value = Decimal('0')
            row.is_active = False
        row.save(update_fields=['amount_value', 'is_active'])

    if rest <= 0:
        new_row.amount_value = Decimal('0')
        new_row.is_active = False
    else:
        new_row.amount_value = rest
    new_row.save(update_fields=['amount_value', 'is_active'])
    return new_row


def book_supplier_product_tx(*,
                             supplier,
                             product,
                             process_no: str,
                             tx_type: str,  # ENTRY | EXIT
                             piece: int = 0,
                             gram: Decimal = Decimal('0'),
                             tl_total: Decimal = Decimal('0.00'),
                             price_hs: Decimal = Decimal('0.000'),
                             user=None):
    """
    Ürün hareketlerinden doğru para birimi & tutarla cari kayıt açar.
    """
    cur_code = (product.currency or '').upper()

    if cur_code == 'TRY':
        led_amt = tl_total
        cur = 'TRY'
    elif price_hs and cur_code in ('', 'HS'):
        led_amt = price_hs
        cur = 'HS'
    else:
        led_amt = gram if product.is_gram_bullion else piece
        cur = cur_code

    return book_supplier_tx(
        supplier=supplier,
        product=product,
        process_no=process_no,
        transaction_type=tx_type,
        amount_value=led_amt,
        currency=cur,
        quantity_piece=piece,
        quantity_gram=gram,
        auto_setoff=True
    )


# ----------------------------- Görünümler



@login_required
@transaction.atomic
def add_scrap_to_wholesale_process(request):
    """
    Toptan ekranından hızlı Hurda Girişi.
    Perakende ekranındaki gibi aynı milyemdeki hurdalar tek havuzda toplanır.

    ÖNEMLİ (FAZ 6 Düzeltmesi — Çift Kayıt Bug Fix):
    IN_PROGRESS aşamasında StockService.record_entry() ÇAĞRILMAZ.
    Stok hareketi yalnızca complete_process_wholesale() tarafından yapılır.
    Bu, add_bracelet_to_wholesale_process ile tutarlıdır.
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek'}, status=405)

    try:
        store = request.user.store

        # Form verileri bölümüne ekleyin
        process_no = request.POST.get('wholesale_process_no') or generate_process_no()
        product_id = request.POST.get('product_id')
        name = request.POST.get('scrap_name')

        gram = parse_decimal_locale(request.POST.get('gram'), default="0", places=3)
        raw_mileage = parse_decimal_locale(request.POST.get('product_mileage'), default="0")

        if gram <= 0:
            return JsonResponse({'result': False, 'error_msg': 'Gram 0 dan büyük olmalı.'}, status=400)

        # FAZ 11 / CAT-02: Milyem validasyonu (zorunlu — hurda her zaman gramlı)
        ok, err, validated_mileage = validate_mileage(
            raw_mileage, required=True, field_label='Hurda milyemi'
        )
        if not ok:
            return JsonResponse({'result': False, 'error_msg': err}, status=400)
        # Hurda milyemi tam sayı olarak saklanır (14K=585, 22K=916 vb.)
        product_mileage = Decimal(int(validated_mileage))

        # 1. Kategori Bul veya Oluştur
        category = Categories.objects.filter(name__icontains='Hurda').first()
        if not category:
            category = Categories.objects.create(name='Hurda', store=store)

        # Birim has hesabı
        unit_buy_price_hs = round(product_mileage / Decimal('1000'), 3)
        total_hs = (gram * product_mileage) / Decimal('1000')

        # ----------------------------------------------------------------------
        # 2. HURDA HAVUZU KONTROLÜ VE STOK GÜNCELLEMESİ
        # ----------------------------------------------------------------------
        # ONARIM FAZI 9 — Havuz anahtarı KULLANICININ SEÇTİĞİ ayar adıdır
        # (`scrap_name`, ör. "14 Ayar"); milyem değerinden TÜRETİLMEZ.
        # Kullanıcı "14 Ayar" seçip 595/605/995 milyem girse de aynı havuza
        # düşer. Milyem ağırlıklı ortalama hesabı complete_process_wholesale
        # aşamasında stok snapshot oluştururken uygulanır.
        from apps.scraps.views import (
            find_scrap_pool_by_selected_karat,
            extract_scrap_karat_label,
        )  # lokal import: döngüsel bağımlılığı engelle

        # Form'dan gelen ayar adı (raw). Canonical etiket havuz aramasında
        # ve yeni havuz oluştururken `Products.name` olarak kullanılır.
        canonical_karat_label = extract_scrap_karat_label(
            scrap_name=name,
            fallback_mileage=product_mileage,
            material_type='GOLD',
        )

        existing_product = None

        if product_id:
            existing_product = Products.objects.filter(id=product_id, store=store).first()

        if not existing_product:
            # ONARIM FAZI 9 — yeni finder: kullanıcı seçimini esas alır.
            # ONARIM FAZI 7 / ADIM 3 — material_type='GOLD' explicit (strict izolasyon).
            existing_product = find_scrap_pool_by_selected_karat(
                store=store, category=category,
                scrap_name=name,
                fallback_mileage=product_mileage,
                is_scrap=True, material_type='GOLD',
            )

        if existing_product:
            # HAVUZ BULUNDU: Ürün referansını al, stok hareketi complete'de yapılacak
            product = existing_product

            # ------------------------------------------------------------------
            # ONARIM FAZI 7 / ADIM 3 — TOPTAN-HURDA KÖPRÜSÜ (Scraps reset)
            # ------------------------------------------------------------------
            # Perakende `scrap_add` BUG 1 düzeltmesinin toptan eşleniği. Daha
            # önce silinmiş havuza yeniden giriş yapıldığında Scraps satırı
            # is_deleted=True / is_active=False kalır → hurda listesinde
            # görünmez ve kullanıcı "ekledim ama kaybolmuş" der. Aynı zamanda
            # Scraps satırı hiç açılmamış olabilir (legacy havuzlar).
            # ------------------------------------------------------------------
            scrap_record, _ = Scraps.objects.get_or_create(
                store=store, product=product,
                defaults={'created_by': request.user},
            )
            _scrap_reset_fields = []
            if scrap_record.is_deleted:
                scrap_record.is_deleted = False
                _scrap_reset_fields.append('is_deleted')
            if scrap_record.is_active is False:
                scrap_record.is_active = True
                _scrap_reset_fields.append('is_active')
            if _scrap_reset_fields:
                scrap_record.save(update_fields=_scrap_reset_fields)

            # Ürün de soft-delete edilmiş olabilir; aktif hale getir.
            if product.is_active is False:
                Products.objects.filter(id=product.id).update(is_active=True)
                product.is_active = True

            # ------------------------------------------------------------------
            # ONARIM FAZI 7 / ADIM 3 — REVIVAL RESET (BUG 6 toptan eşleniği)
            # ------------------------------------------------------------------
            # Eski silme/iptal stok kalıntısı bırakmış olabilir. Yeni giriş
            # tamamen taze sayılır: stok 0'a çekilir; legacy Products.gram /
            # product_mileage temizlenir; complete_process_wholesale aşamasında
            # gelen yeni hurda tek belirleyici olur.
            # ------------------------------------------------------------------
            # ONARIM FAZI 9 — Revival koşulu DARALTILDI:
            # Önceki sürüm `_scrap_reset_fields`'i de revival sayıyordu; bu,
            # her bayrak resetinde `product_mileage`'ı 0'a çekip sonraki
            # girişlerde havuz aramayı sabote ediyordu. Artık yalnızca
            # GERÇEKTEN soft-deleted/pasif olan havuzlar revival kapsamına
            # girer (taze açılan Scraps satırları DEĞİL).
            was_revival = (
                scrap_record.is_deleted is True
                or scrap_record.is_active is False
                or product.is_active is False
            )
            # Revival reset YALNIZCA stok kalıntısı varsa çalışır. Stok 0 ise
            # `product_mileage` sıfırlanmaz → havuz adı (canonical "X Ayar")
            # ve eşleşme stabil kalır.
            if was_revival:
                _stale_gram = Decimal('0')
                _stale_pieces = 0
                try:
                    _stale_snap = (
                        StockSnapshot.objects
                        .filter(product=product, store=store)
                        .first()
                    )
                    _stale_gram = (
                        Decimal(str(_stale_snap.stock_gram))
                        if (_stale_snap and _stale_snap.stock_gram is not None)
                        else Decimal('0')
                    )
                    _stale_pieces = (
                        int(_stale_snap.stock_pieces or 0) if _stale_snap else 0
                    )
                    if _stale_gram > 0 or _stale_pieces > 0:
                        StockService.adjustment(
                            product=product,
                            store=store,
                            actual_gram=Decimal('0'),
                            actual_pieces=0,
                            ref_id=f"wholesale_scrap_revival_{product.id}_{process_no}",
                            user=request.user,
                            notes=(
                                "Toptan hurda havuzu yeniden açılışı: önceki "
                                "silme/iptal sonrası kalan stok temizlendi"
                            ),
                        )
                except Exception:
                    pass
                # Sadece gerçekten stok kalıntısı varsa legacy alanları sıfırla.
                if _stale_gram > 0 or _stale_pieces > 0:
                    Products.objects.filter(id=product.id).update(
                        gram=Decimal('0'),
                        product_mileage=Decimal('0'),
                    )
                    product.gram = Decimal('0')
                    product.product_mileage = Decimal('0')

        else:
            # HAVUZ YOK: Sıfırdan oluştur.
            # ONARIM FAZI 9 — Yeni havuzun ismi canonical "X Ayar" etiketidir
            # (kullanıcının seçimi). Bu, sonraki girişlerde
            # `find_scrap_pool_by_selected_karat`'in havuzu doğru bulmasını
            # garanti eder.
            final_scrap_name = (
                canonical_karat_label
                or (name.strip() if isinstance(name, str) and name.strip() else '')
                or f"{int(product_mileage)} Milyem Hurda"
            )

            product = Products.objects.create(
                store=store,
                category=category,
                name=final_scrap_name,
                gram=Decimal('0'),
                product_mileage=product_mileage,
                buy_price_hs=unit_buy_price_hs,
                sale_price_hs=unit_buy_price_hs,
                is_scrap=True,
                is_gram_bullion=True,
                material_type='GOLD',
                created_by=request.user,
                created_on=timezone.now()
            )

            # Scraps tablosuna kayıt
            Scraps.objects.create(store=store, product=product, created_by=request.user)

        # FAZ 6: StockSnapshot'ı hazırla (stok 0, complete_process_wholesale'de artacak)
        StockSnapshot.objects.get_or_create(
            product=product,
            store=store,
            defaults={
                'stock_gram': Decimal('0.0000'),
                'stock_pieces': 0,
                'weighted_avg_cost_hs': Decimal('0.0000'),
                'weighted_avg_cost_tl': Decimal('0.00'),
            }
        )
        # ----------------------------------------------------------------------

        # 3. Process Kaydı Oluştur (IN_PROGRESS)
        Process.objects.create(
            store=store,
            process_no=process_no,
            process_type='WHOLESALE',
            transaction_type='PURCHASE',  # Hurda Giriş = Alış
            product=product,
            employee=request.user,
            piece=0,
            gram=gram,
            process_mileage=str(product_mileage),
            price_hs=total_hs,
            unit_price=Decimal('0'),
            amount=Decimal('0'),
            is_status='IN_PROGRESS'
        )

        return JsonResponse({
            'result': True,
            'wholesale_process_no': process_no,
            'message': 'Hurda listeye eklendi. Stok, işlem tamamlandığında güncellenecek.'
        })

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


@login_required
@transaction.atomic
def add_bracelet_to_wholesale_process(request):
    """
    Toptan ekranından hızlı Bilezik Girişi.

    ════════════════════════════════════════════════════════════════════════
    B-FAZ 5 (2026-04-27) — HAVUZ ENTEGRASYONU + REVIVAL RESET
    ════════════════════════════════════════════════════════════════════════
    Eski davranış: Her giriş YENİ bir Products kaydı açıyordu (toptan-perakende
    senkronizasyon kopukluğu, BUG B-2). Yeni davranış (hurda Faz 7 ADIM 3
    paraleli):
      - `find_bracelet_pool_by_name` ile aynı isimli AKTİF havuz aranır.
      - Aktif yoksa pasif/silinmiş havuz taranır → revival reset uygulanır
        (snapshot 0'a çekilir, legacy alanlar temizlenir).
      - Hiçbiri yoksa yeni Products + Bracelets açılır.
    Stok hareketi YİNE complete_process_wholesale aşamasında işlenir
    (IN_PROGRESS aşamasında stok artmaz — çift kayıt önlenir). WAC milyem
    güncellemesi de complete_process_wholesale içinde yapılır.

    ÖNEMLİ (FAZ 6 Düzeltmesi — Çift Kayıt Bug Fix):
    IN_PROGRESS aşamasında StockService.record_entry() ÇAĞRILMAZ.
    Stok hareketi yalnızca complete_process_wholesale() tarafından yapılır.
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek'}, status=405)

    try:
        store = request.user.store

        # Form Verileri
        process_no = request.POST.get('wholesale_process_no') or generate_process_no()
        name = request.POST.get('bracelet_name') or "Bilezik"

        gram = parse_decimal_locale(request.POST.get('gram'), default="0", places=3)
        raw_mileage = parse_decimal_locale(request.POST.get('product_mileage'), default="0")

        # ════════════════════════════════════════════════════════════════════
        # FAZ 11 / CAT-05 — BİLEZİK GİRİŞ MODU (Gram / Adet) (2026-04-24)
        # ════════════════════════════════════════════════════════════════════
        entry_mode = (request.POST.get('entry_mode') or 'GRAM').strip().upper()
        if entry_mode not in ('GRAM', 'PIECE'):
            entry_mode = 'GRAM'

        is_gram_mode = (entry_mode == 'GRAM')

        if is_gram_mode and gram <= 0:
            return JsonResponse(
                {'result': False, 'error_msg': 'Gram modu için gram 0 dan büyük olmalı.'},
                status=400,
            )

        piece_count = 1
        if not is_gram_mode:
            try:
                piece_count = int(request.POST.get('piece') or 1)
            except (ValueError, TypeError):
                piece_count = 1
            if piece_count <= 0:
                return JsonResponse(
                    {'result': False, 'error_msg': 'Adet modu için adet 0 dan büyük olmalı.'},
                    status=400,
                )

        # FAZ 11 / CAT-02: Milyem validasyonu (zorunlu — bilezik gram bazlı satılır)
        ok, err, mileage = validate_mileage(
            raw_mileage, required=True, field_label='Bilezik milyemi'
        )
        if not ok:
            return JsonResponse({'result': False, 'error_msg': err}, status=400)

        # 1. Kategori
        category = Categories.objects.filter(name__icontains='Bilezik').first()
        if not category:
            category = Categories.objects.create(name='Bilezik', store=store)

        unit_hs_price = Decimal(mileage) / Decimal('1000')

        # ════════════════════════════════════════════════════════════════════
        # B-FAZ 5 — HAVUZ ARAMA + REVIVAL RESET
        # ════════════════════════════════════════════════════════════════════
        # Lazy import: döngüsel bağımlılığı engelle.
        from apps.bracelets.views import find_bracelet_pool_by_name

        # 2a. Aktif havuzu ara (sadece isim).
        # Adet modu (PIECE) bilezikler ayrı bir akıştır — havuzlama yalnızca
        # GRAM modunda anlamlı (aynı isim altında ağırlıklı ortalama). Adet
        # modunda her giriş ayrı kayıt olarak kalır (mamul takı gibi).
        existing_pool = None
        revival_pool = None
        if is_gram_mode:
            existing_pool = find_bracelet_pool_by_name(
                store=store, category=category, name=name,
            )

            # 2b. Aktif yoksa pasif/silinmiş havuzu tara
            if existing_pool is None and name:
                norm_name = (name or '').strip().lower()
                if norm_name:
                    candidates = Products.objects.filter(
                        store=store, category=category,
                    ).filter(Q(is_deleted=True) | Q(is_active=False))
                    for cp in candidates:
                        if ((cp.name or '').strip().lower()) == norm_name:
                            revival_pool = cp
                            break

        if existing_pool is not None or revival_pool is not None:
            # ─── HAVUZ BULUNDU (aktif veya revival) ───
            product = existing_pool or revival_pool
            was_revival = (existing_pool is None)

            # Bracelets satırı reset / get_or_create
            bracelet_row, _b_created = Bracelets.objects.get_or_create(
                store=store, product=product,
                defaults={'created_by': request.user},
            )
            _bf = []
            if bracelet_row.is_deleted:
                bracelet_row.is_deleted = False
                _bf.append('is_deleted')
            if bracelet_row.is_active is False:
                bracelet_row.is_active = True
                _bf.append('is_active')
            if _bf:
                bracelet_row.save(update_fields=_bf)

            # Products bayrak reset
            if product.is_active is False or product.is_deleted is True:
                Products.objects.filter(id=product.id).update(
                    is_active=True, is_deleted=False
                )
                product.is_active = True
                product.is_deleted = False

            # ─── REVIVAL RESET (Hurda Faz 6 BUG 6 / Faz 7 ADIM 3 deseni) ───
            # Yalnızca gerçekten pasif/silinmiş havuza yeni giriş için.
            if was_revival:
                _stale_gram = Decimal('0')
                _stale_pieces = 0
                try:
                    _stale_snap = (
                        StockSnapshot.objects
                        .select_for_update()
                        .filter(product=product, store=store)
                        .first()
                    )
                    _stale_gram = (
                        Decimal(str(_stale_snap.stock_gram))
                        if (_stale_snap and _stale_snap.stock_gram is not None)
                        else Decimal('0')
                    )
                    _stale_pieces = (
                        int(_stale_snap.stock_pieces or 0) if _stale_snap else 0
                    )
                    if _stale_gram > 0 or _stale_pieces > 0:
                        StockService.adjustment(
                            product=product, store=store,
                            actual_gram=Decimal('0'),
                            actual_pieces=0,
                            ref_id=f"wholesale_bracelet_revival_{product.id}_{process_no}",
                            user=request.user,
                            notes=(
                                "Toptan bilezik havuzu yeniden açılışı: önceki "
                                "silme/iptal sonrası kalan stok temizlendi"
                            ),
                        )
                except Exception:
                    pass

                if _stale_gram > 0 or _stale_pieces > 0:
                    Products.objects.filter(id=product.id).update(
                        gram=Decimal('0'),
                        product_mileage=Decimal('0'),
                    )
                    product.gram = Decimal('0')
                    product.product_mileage = Decimal('0')

        else:
            # ─── HAVUZ YOK: Yeni kayıt (eski davranışla uyumlu) ───
            product = Products.objects.create(
                store=store,
                category=category,
                name=name,
                gram=gram if not is_gram_mode else Decimal('0'),
                product_mileage=str(mileage),
                buy_price_hs=unit_hs_price,
                sale_price_hs=unit_hs_price,
                # CAT-05: Modu Product seviyesinde işle — Has muhasebesine
                # uygun ledger açılması için.
                is_gram_bullion=is_gram_mode,
                created_by=request.user,
                created_on=timezone.now()
            )
            Bracelets.objects.create(
                store=store, product=product, created_by=request.user
            )

        # 3. StockSnapshot hazırla (stok 0, complete'de artacak)
        StockSnapshot.objects.get_or_create(
            product=product,
            store=store,
            defaults={
                'stock_gram': Decimal('0.0000'),
                'stock_pieces': 0,
                'weighted_avg_cost_hs': Decimal('0.0000'),
                'weighted_avg_cost_tl': Decimal('0.00'),
            }
        )

        # 4. Process (İşlem Sepeti) Kaydı
        if is_gram_mode:
            total_hs = (gram * mileage) / Decimal('1000')
            row_piece = 1
            row_gram = gram
        else:
            total_hs = (gram * mileage) / Decimal('1000') * Decimal(piece_count) if gram > 0 else Decimal('0')
            row_piece = piece_count
            row_gram = gram * Decimal(piece_count) if gram > 0 else Decimal('0')

        Process.objects.create(
            store=store,
            process_no=process_no,
            process_type='WHOLESALE',
            transaction_type='PURCHASE',
            product=product,
            employee=request.user,
            piece=row_piece,
            gram=row_gram,
            process_mileage=str(mileage),
            price_hs=total_hs,
            unit_price=Decimal('0'),
            amount=Decimal('0'),
            is_status='IN_PROGRESS'
        )

        return JsonResponse({
            'result': True,
            'wholesale_process_no': process_no,
            'message': 'Bilezik toptan listesine eklendi.'
        })

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


@login_required
def get_sales_wholesale(request):
    store = request.user.store
    order_col = request.GET.get('order_column', 'date')
    if request.GET.get('order_direction') == 'desc':
        order_col = f'-{order_col}'

    data = list(
        Process.objects
        .filter(
            is_deleted=False,
            is_status='IN_PROGRESS',
            process_type='WHOLESALE',
            store=store,
            employee=request.user,
        )
        .values(
            'id', 'product__id', 'product__name',
            'product__currency',
            'gram', 'piece', 'process_mileage',
            'transaction_type', 'process_type', 'process_no',
            'unit_price', 'amount', 'date', 'price_hs',
            # FAZ 23: Kasa kalemi alanları
            'bank_account__id', 'bank_account__name',
            'bank_account__currency', 'payment_currency',
        )
        .order_by(order_col)
    )
    return JsonResponse({'data': data}, safe=False)


@login_required
def get_process_details(request):
    process_no = request.GET.get('process_no')
    if not process_no:
        return JsonResponse({'result': False, 'error_msg': 'İşlem numarası belirtilmedi.'}, status=400)

    try:
        processes = Process.objects.filter(process_no=process_no).values(
            'id', 'product__name', 'gram', 'piece', 'process_mileage',
            'transaction_type', 'employee__first_name', 'employee__last_name',
            'process_type', 'unit_price', 'amount', 'date', 'price_hs',
            'customer__first_name', 'customer__last_name', 'customer__phone'
        )
        all_processes = Process.objects.filter(process_no=process_no)

        if not processes.exists():
            return JsonResponse({'result': False, 'error_msg': 'İşlem bulunamadı.'}, status=404)

        total_amount_sale = processes.filter(transaction_type='SALE').aggregate(Sum('amount'))['amount__sum'] or 0
        total_amount_purchase_return = processes.filter(
            transaction_type__in=['PURCHASE', 'RETURN']
        ).aggregate(Sum('amount'))['amount__sum'] or 0

        total_amount = total_amount_sale - total_amount_purchase_return
        if total_amount == 0:
            for process in all_processes:
                total_amount += process.amount

        return JsonResponse({
            'result': True,
            'processes': list(processes),
            'total_amount_sale': total_amount_sale,
            'total_amount_purchase_return': total_amount_purchase_return,
            'total_amount': total_amount
        })

    except Exception as e:
        return JsonResponse({'result': False, 'error_msg': str(e)}, status=500)


@login_required
def get_parities(request):
    data = {}

    # FAZ 3: Has Altın fiyatını PriceService'den al, fallback olarak Products tablosundan
    hs = (Products.objects
          .filter(name='Has Altın 24 Ayar', is_active=True, is_deleted=False)
          .values('id', 'buy_price_tl', 'sale_price_tl')
          .first())

    hs_buy_tl = 0
    hs_sale_tl = 0
    try:
        _hs_data = PriceService.get_price('GOLD_24K')
        hs_buy_tl = _hs_data.get('buy_tl', 0)
        hs_sale_tl = _hs_data.get('sell_tl', 0)
    except Exception:
        pass

    if hs:
        data['HS'] = {
            'prod_id': hs['id'],
            'tl_buy': hs_buy_tl if hs_buy_tl else (hs['buy_price_tl'] or 0),
            'tl_sale': hs_sale_tl if hs_sale_tl else (hs['sale_price_tl'] or 0),
        }

    # TRY (nakit)
    tl = (Products.objects
          .filter(name='TRY - Türk Lirası', is_active=True, is_deleted=False)
          .values('id')
          .first())
    if tl:
        data['TRY'] = {'prod_id': tl['id'], 'tl_buy': 1, 'tl_sale': 1}

    # Diğer kodlar
    rows = (Products.objects
            .filter(name__in=RAW_CODES.keys(), is_deleted=False)
            .values('id', 'name', 'buy_price_tl', 'sale_price_tl'))

    for r in rows:
        key = RAW_CODES[r['name']]
        obj = data.setdefault(key, {})
        obj.setdefault('prod_id', r['id'])

        raw_buy = r['buy_price_tl'] or 0
        raw_sell = r['sale_price_tl'] or 0
        # Bazı sağlayıcılar (Harem Altın) USDTRY için yalnızca sell yayınlar; buy=null gelir.
        # Dönüşüm modalı `tl_buy=0` görünce kullanıcıyı kilitlemesin diye sell'i yedek olarak kullan.
        buy = raw_buy or raw_sell or 0
        sell = raw_sell or raw_buy or 0

        if key.endswith('KG'):
            obj['gold_buy'] = buy
            obj['gold_sale'] = sell
        elif r['name'].endswith('TRY'):
            obj['tl_buy'] = buy
            obj['tl_sale'] = sell
        else:
            obj['cross_buy'] = buy
            obj['cross_sale'] = sell

    return JsonResponse(data)


@transaction.atomic
@login_required
def open_binding(request):
    sup = get_object_or_404(Suppliers, id=request.POST['supplier_id'])
    p_from = get_object_or_404(Products, id=request.POST['from_product_id'])
    p_to = get_object_or_404(Products, id=request.POST['to_product_id'])

    qty_from = to_dec(request.POST.get('ob_from_amount'))
    rate_f = to_dec(request.POST.get('from_rate'))
    qty_to = to_dec(request.POST.get('ob_to_amount'))
    rate_t = to_dec(request.POST.get('to_rate'))

    if min(qty_from, rate_f, rate_t) <= 0:
        return JsonResponse({'error': True, 'error_msg': 'Miktar / kur 0 olamaz.'})

    cur_from = (p_from.currency or '').upper()
    cur_to = (p_to.currency or '').upper()

    CROSS_USDEUR = {'USD', 'EUR'}

    def is_cross_case(f, t):
        return {f, t} == CROSS_USDEUR and rate_t < Decimal('10')

    if is_cross_case(cur_from, cur_to):
        tl_per_to = rate_f * rate_t if cur_from == 'USD' else rate_f / rate_t
        qty_to = (qty_from * rate_f / tl_per_to).quantize(Decimal('0.001'))
    elif cur_to == 'TRY':
        qty_to = (qty_from * rate_f).quantize(Decimal('0.01'))

    tl_total = (qty_from * rate_f).quantize(Decimal('0.01'))

    def ledger_amount(prod, qty):
        pc = (prod.currency or '').upper()
        if pc == 'TRY':
            return tl_total
        if pc == 'HS':
            return qty.quantize(Decimal('0.001'))
        return qty.quantize(Decimal('0.001'))

    amt_exit = ledger_amount(p_from, qty_from)
    amt_entry = ledger_amount(p_to, qty_to)

    # FAZ TOPTAN 0.995: HS↔Döviz (USD/EUR) dönüşümünde HS tarafına 995 milyem
    # (0.995) fire çarpanı uygulanır. Türkiye piyasa standardıdır.
    # ÖRNEK (HS→USD): Kullanıcı 100 HS + 150.220 USD/kg girer.
    #   USD tarafı ham hesap = (100/1000)*150220 = 15.022 USD (değişmez)
    #   HS tarafı fire sonrası = 100 * 0.995 = 99.500 HS (tedarikçiye 99.5 HS
    #   alacak olarak kaydedilir; 0.5 gr fire payı)
    # NOT: JS calcForward/calcBackward USD tarafını HAM hesaplar, 0.995
    # çarpmaz. Adjustment yalnızca burada (backend ledger yazımında) yapılır —
    # böylece çift uygulama (0.995 * 0.995) yaşanmaz.
    if {'HS', 'USD'} <= {cur_from, cur_to} or {'HS', 'EUR'} <= {cur_from, cur_to}:
        if cur_from == 'HS':
            amt_exit = (amt_exit * HS_ADJUST).quantize(Decimal('0.001'))
        else:
            amt_entry = (amt_entry * HS_ADJUST).quantize(Decimal('0.001'))

    proc_no = f'OB{timezone.now():%Y%m%d%H%M%S}'
    meta = {
        'from_cur': cur_from,
        'from_amt': str(amt_exit),
        'to_cur': cur_to,
        'to_amt': str(amt_entry),
        'rate_from_tl': str(rate_f),
        'rate_to': str(rate_t),
    }
    desc = 'OB|' + json.dumps(meta, separators=(',', ':'))

    book_supplier_tx(
        supplier=sup, transaction_type='EXIT',
        amount_value=amt_exit, currency=cur_from,
        product=p_from, quantity_piece=0, quantity_gram=Decimal('0'),
        process_no=proc_no, description=desc, auto_setoff=False
    )
    book_supplier_tx(
        supplier=sup, transaction_type='ENTRY',
        amount_value=amt_entry, currency=cur_to,
        product=p_to, quantity_piece=0, quantity_gram=Decimal('0'),
        process_no=proc_no, description=desc, auto_setoff=False
    )
    return JsonResponse({'result': True})


@transaction.atomic
@login_required
def convert_debt(request):
    """
    Borç/Alacak çeviri (C…)
    """
    s = get_object_or_404(Suppliers, id=request.POST['supplier_id'])

    f_cur = request.POST['from_currency'].upper()
    t_cur = request.POST['to_currency'].upper()
    tx_type = request.POST['transaction_type']  # 'EXIT' | 'ENTRY'

    amt = to_dec(request.POST.get('amount'))
    rate = to_dec(request.POST.get('rate'))

    # EKRANDAKİ HEDEF MİKTARI AL (Hassasiyet sorunu çözümü)
    # Eğer JS bu veriyi gönderirse, çarpma işlemi yapmak yerine doğrudan bunu kullanacağız.
    target_amt_val = to_dec(request.POST.get('to_amount'))

    if tx_type not in ('ENTRY', 'EXIT') or amt <= 0 or rate <= 0 or f_cur == t_cur:
        return JsonResponse({'error': True, 'error_msg': 'Veri hatalı'})

    total_active = (SupplierLedger.objects
                    .filter(supplier=s,
                            currency=f_cur,
                            transaction_type=tx_type,
                            is_active=True)
                    .aggregate(total=Sum('amount_value'))['total'] or Decimal('0'))

    if (amt - total_active) > TOLERANCE:
        return JsonResponse({'error': True,
                             'error_msg': f'{f_cur} içinde çevrilebilir tutar yetersiz.'})

    no = f'C{timezone.now():%Y%m%d%H%M%S}'

    # --- HESAPLAMA MANTIĞI GÜNCELLEMESİ ---
    if target_amt_val > 0:
        # Ekranda görünen net rakamı kullan (Kuruş hatasını önler)
        new_amt = target_amt_val
    else:
        # Eski yöntem (Yedek)
        new_amt = (amt * rate).quantize(Decimal('0.001'))

    meta = {
        'kind': 'CONV',
        'tx_type': tx_type,
        'from_cur': f_cur,
        'from_amt': str(amt),
        'to_cur': t_cur,
        'to_amt': str(new_amt),
        'rate': str(rate),
    }
    desc = 'CONV|' + json.dumps(meta, separators=(',', ':'))

    opposite = 'ENTRY' if tx_type == 'EXIT' else 'EXIT'
    closer_row = book_supplier_tx(
        supplier=s,
        transaction_type=opposite,
        amount_value=amt,
        currency=f_cur,
        process_no=no,
        description=desc,
        auto_setoff=True
    )

    rest = closer_row.amount_value or Decimal('0')
    if rest > Decimal('0.000'):
        ob_qs = (SupplierLedger.objects
                 .select_for_update()
                 .filter(supplier=s,
                         currency=f_cur,
                         transaction_type=tx_type,
                         is_active=True,
                         process_no__startswith='OB')
                 .order_by('created_on', 'id'))

        for l in ob_qs:
            if rest <= Decimal('0.000'):
                break
            use = min(l.amount_value, rest)
            l.amount_value -= use
            if l.amount_value <= 0:
                l.amount_value = Decimal('0')
                l.is_active = False
            l.save(update_fields=['amount_value', 'is_active'])
            rest -= use
            closer_row.amount_value -= use

        if rest <= Decimal('0.000'):
            closer_row.amount_value = Decimal('0')
            closer_row.is_active = False

        closer_row.save(update_fields=['amount_value', 'is_active'])

    book_supplier_tx(
        supplier=s,
        transaction_type=tx_type,
        amount_value=new_amt,
        currency=t_cur,
        process_no=no,
        description=desc,
        auto_setoff=False
    )

    return JsonResponse({'result': True})


# ──────────────────────────────────────────────────────
# FAZ 23: KASA KALEMİNİ SEPETE EKLEME
# ──────────────────────────────────────────────────────
@login_required(login_url='login')
@transaction.atomic
def add_wholesale_cash_item(request):
    """
    Toptancı sepetine kasa/ödeme kalemi ekler.
    Ürün yerine bir BankAccount seçilmiştir. Process kaydı product=None,
    bank_account=seçilen kasa olarak oluşturulur.

    POST Parametreleri:
        wholesale_process_no : str   — Mevcut işlem numarası (yoksa yeni üretilir)
        bank_account_id      : uuid  — Seçilen banka hesabı
        amount               : str   — Ödeme tutarı (Türk veya döviz)
        currency             : str   — Para birimi (TRY, USD, EUR vb.)
        cash_select          : str   — '1' = Giriş (PURCHASE), '0' = Çıkış (SALE)

    Bakiye Kontrolü:
        Sadece ÇIKIŞ (SALE / cash_select='0') işlemlerinde uygulanır.
        GİRİŞ (PURCHASE / cash_select='1') → Tedarikçiden alış, borçlanma —
        kasada para olması gerekmez.
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=400)

    try:
        from apps.banking.models import BankAccount
        from apps.banking.bank_views import get_bank_balance_qs

        store = request.user.store

        process_no = request.POST.get('wholesale_process_no') or generate_process_no()
        bank_account_id = request.POST.get('bank_account_id')
        amount = to_decimal(request.POST.get('amount'))
        currency = (request.POST.get('currency') or 'TRY').strip().upper()
        cash_select = request.POST.get('cash_select', '0')

        if not bank_account_id:
            return JsonResponse({'error': True, 'error_msg': 'Kasa seçilmedi.'}, status=400)
        if amount <= 0:
            return JsonResponse({'error': True, 'error_msg': 'Tutar sıfırdan büyük olmalıdır.'}, status=400)

        # Kasa kontrolü
        ba = BankAccount.objects.select_for_update().get(
            id=bank_account_id, store=store, is_deleted=False, is_active=True
        )

        # İşlem yönü
        transaction_type = 'PURCHASE' if str(cash_select) == '1' else 'SALE'

        # --- BAKİYE KONTROLÜ (SADECE ÇIKIŞ İÇİN) ---
        # GİRİŞ (PURCHASE): Tedarikçi kasaya para getiriyor, bakiye kontrolü gereksiz.
        # ÇIKIŞ (SALE): Kasadan para çıkışı, bakiye yeterli olmalı.
        if transaction_type == 'SALE':
            qs = get_bank_balance_qs(store).filter(id=ba.id)
            acc_row = qs.first()
            if acc_row:
                total_in = acc_row.total_in or Decimal('0')
                total_out = acc_row.total_out or Decimal('0')
                balance = total_in - total_out
                if amount > balance:
                    return JsonResponse({
                        'error': True,
                        'error_msg': f'Yetersiz bakiye! Kasa bakiyesi: {balance:.2f}, '
                                     f'Girilen tutar: {amount:.2f}'
                    }, status=400)

        Process.objects.create(
            store=store,
            process_no=process_no,
            process_type='WHOLESALE',
            transaction_type=transaction_type,
            product=None,
            bank_account=ba,
            payment_currency=currency,
            employee=request.user,
            piece=0,
            gram=Decimal('0'),
            process_mileage='0',
            price_hs=Decimal('0'),
            unit_price=Decimal('0'),
            amount=amount,
            is_status='IN_PROGRESS',
        )

        return JsonResponse({
            'result': True,
            'wholesale_process_no': process_no,
            'message': f'{ba.name} kasasından {amount} {currency} ödeme sepete eklendi.'
        })

    except BankAccount.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Kasa bulunamadı veya pasif.'}, status=404)
    except Exception as e:
        return JsonResponse({'error': True, 'error_msg': f'Sunucu Hatası: {str(e)}'}, status=500)


@login_required(login_url='login')
@transaction.atomic
def add_wholesale_process(request):
    """
    Toptan İşlem Ekleme Fonksiyonu
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=400)

    try:
        store = request.user.store

        # --- VERİ ALIMI ---
        process_no = request.POST.get('wholesale_process_no') or generate_process_no()
        product_id = request.POST.get('product_id')

        # Helper fonksiyon ile güvenli dönüşüm
        qty = to_decimal(request.POST.get('qty'))
        weight = to_decimal(request.POST.get('weight'))
        price_hs = to_decimal(request.POST.get('price_hs'))  # Birim Has
        product_mileage = to_decimal(request.POST.get('product_mileage'))

        # TL Tutarı (Cari için gerekli)
        total_tl = to_decimal(request.POST.get('total_tl'))

        # Nakit durumu (Giriş: 1, Çıkış: 0)
        cash_select = request.POST.get('cash_select')

        if not product_id:
            return JsonResponse({'error': True, 'error_msg': 'Ürün seçilmedi.'}, status=400)

        product = Products.objects.get(id=product_id)

        # ------------------------------------------------------------------
        # ONARIM FAZI 3 / ADIM 1 — WATCH/DIAMOND PIECE FALLBACK (Toptan)
        # ------------------------------------------------------------------
        # Toptan sepete ekleme akisinda UI WATCH/DIAMOND icin adet input'u
        # tasimiyor olabilir. Backend defansif olarak: urun Saat/Pirlanta ise
        # ve qty<=0 gelirse qty=1 atanir. is_gram_bullion=False urunu
        # zaten adetli (else) bransina alir; gram sifirlanir.
        # ------------------------------------------------------------------
        try:
            _mat_type = getattr(product, 'material_type', 'GOLD') or 'GOLD'
            if _mat_type in ('WATCH', 'DIAMOND'):
                if qty is None or qty <= 0:
                    qty = Decimal('1')
                    import logging as _lg
                    _lg.getLogger(__name__).info(
                        f"wholesale add_process_wholesale: WATCH/DIAMOND qty "
                        f"fallback uygulandi (product_id={product_id}, qty=1)"
                    )
                # Gram bilgisi gonderilmisse sifirla: saat/pirlanta adet bazli
                weight = Decimal('0')
        except Exception:
            # Fallback sessiz olmali — mevcut akisi bozmamaliyiz
            pass

        # --- HESAPLAMA MANTIĞI ---
        calculated_gram = Decimal('0')
        calculated_piece = 0
        total_hs = Decimal('0')

        # Milyem (Process Mileage) Hesabı - Veritabanı NULL kabul etmez
        final_process_mileage = '0'

        # Gramlı Ürün Kontrolü (Hurda, Bilezik vb.)
        if product.is_gram_bullion:
            if weight <= 0:
                return JsonResponse({'error': True, 'error_msg': 'Gram bilgisi girilmelidir.'}, status=400)

            calculated_gram = weight
            # Eğer milyem formdan geldiyse kullan, yoksa ürünün kendi milyemini al
            candidate_mileage = product_mileage if product_mileage > 0 else (product.product_mileage or 0)

            # FAZ 11 / CAT-02: Gramlı ürünlerde milyem zorunlu ve 1-1000 arası olmalıdır.
            # Bu kontrol, (gram * mileage / 1000) hesabında has değerin sıfıra
            # düşmesini ve SupplierLedger'da hatalı/eksik cari kaydı oluşmasını engeller.
            ok, err, validated_mileage = validate_mileage(
                candidate_mileage, required=True, field_label='Milyem'
            )
            if not ok:
                return JsonResponse({'error': True, 'error_msg': err}, status=400)
            used_mileage = validated_mileage

            # Veritabanı için milyem değeri (string)
            final_process_mileage = str(used_mileage)

            # Toplam Has = Gram * Milyem / 1000
            total_hs = (calculated_gram * used_mileage) / Decimal('1000')

        else:
            # Adetli Ürün (Çeyrek, Ziynet vb.)
            if qty <= 0:
                return JsonResponse({'error': True, 'error_msg': 'Adet bilgisi girilmelidir.'}, status=400)

            calculated_piece = int(qty)
            calculated_gram = product.gram * calculated_piece  # Tahmini gram

            # Adetli ürünlerde milyem
            final_process_mileage = str(product.product_mileage) if product.product_mileage else '0'

            # FAZ S6 (PIVOT 2026-04-23): WATCH/DIAMOND için price_hs=0 garantisi.
            # Frontend yanlışlıkla price_hs gönderse bile material_type kontrolü
            # ile sıfıra çekilir — Has muhasebesine sızma engellenir.
            if _mat_type in ('WATCH', 'DIAMOND'):
                total_hs = Decimal('0')
                price_hs = Decimal('0')
            else:
                # price_hs (Birim Has) * Adet
                total_hs = price_hs * qty

        # İşlem Yönü (Alış / Satış)
        transaction_type = 'PURCHASE' if str(cash_select) == '1' else 'SALE'

        # Kayıt Oluşturma
        Process.objects.create(
            store=store,
            process_no=process_no,
            process_type='WHOLESALE',
            transaction_type=transaction_type,
            product=product,
            employee=request.user,
            piece=calculated_piece,
            gram=calculated_gram,
            process_mileage=final_process_mileage,
            price_hs=total_hs,
            unit_price=price_hs,

            # TL ürünleri için carinin işlemesi adına tutarı kaydediyoruz
            amount=total_tl if total_tl > 0 else Decimal('0'),

            is_status='IN_PROGRESS'
        )

        return JsonResponse({
            'result': True,
            'wholesale_process_no': process_no,
            'message': 'Ürün eklendi.'
        })

    except Products.DoesNotExist:
        return JsonResponse({'error': True, 'error_msg': 'Ürün bulunamadı.'}, status=404)
    except Exception as e:
        print(f"Hata detayı: {str(e)}")
        return JsonResponse({'error': True, 'error_msg': f'Sunucu Hatası: {str(e)}'}, status=500)


@login_required
@transaction.atomic
def complete_process_wholesale(request):
    """
    Toptan işlemi tamamlar, stokları düşer, cariyi işler VE
    ALIŞ işlemleri için otomatik Gider Pusulası oluşturur.
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=405)

    try:
        proc_no = request.POST.get('wholesale_process_no')
        supplier_id = request.POST.get('supplier_id')

        if not supplier_id:
            return JsonResponse({'error': True, 'error_msg': 'Tedarikçi seçilmedi.'}, status=400)

        supplier = get_object_or_404(Suppliers, pk=supplier_id)

        # 1. İşlenecek satırları bul ve kilitle
        if proc_no == 'ALL':
            base_qs = Process.objects.filter(
                process_type='WHOLESALE', is_status='IN_PROGRESS',
                is_deleted=False, store=request.user.store)
        else:
            if not proc_no:
                return JsonResponse({'error': True, 'error_msg': 'İşlem numarası yok.'}, status=400)
            base_qs = Process.objects.filter(
                process_no=proc_no, process_type='WHOLESALE',
                is_status='IN_PROGRESS', is_deleted=False,
                store=request.user.store, employee=request.user)

        locked_ids = list(base_qs.select_for_update().order_by('date', 'id').values_list('id', flat=True))
        if not locked_ids:
            return JsonResponse({'error': True, 'error_msg': 'Tamamlanacak işlem bulunamadı.'}, status=404)

        rows = Process.objects.filter(id__in=locked_ids).select_related(
            'product', 'bank_account',
        ).order_by('date', 'id')

        # FAZ 23: Kasa kalemleri adet/gram 0 kontrolünden muaf tutulmalı
        # Bu yüzden yukarıdaki kontrol sadece ürün satırları için geçerli olmalı
        if Process.objects.filter(
            id__in=locked_ids, bank_account__isnull=True, piece__lte=0, gram__lte=0
        ).exists():
            return JsonResponse(
                {'error': True, 'error_msg': 'Adet/gram 0 olan ürün satırları var. Lütfen düzeltin.'},
                status=400
            )

        # Fatura oluşturmak için işlenen satırları bellekte tutacağız
        processed_rows = []

        # ════════════════════════════════════════════════════════════════════
        # FAZ 11 / CAT-03 — KISMİ BAŞARI ŞEFFAFLIĞI (2026-04-24)
        # ════════════════════════════════════════════════════════════════════
        # Her kalem işlenirken hangi satırın hangi tipte olduğunu loglar.
        # Hata anında 'failed_line' ile hangi satırın bozduğu mesajda görünür.
        # ════════════════════════════════════════════════════════════════════
        import logging as _cat03_log
        _logger = _cat03_log.getLogger('apps.process.wholesale')
        _processing_index = 0
        _processing_summary = []  # [{'idx', 'process_no', 'kind', 'product_id', 'status'}, ...]

        # 2. Döngü: Stok ve Cari İşlemleri
        for p in rows:
            _processing_index += 1
            _kind = 'CASH_PAYMENT' if p.bank_account_id is not None else 'PRODUCT'
            _logger.info(
                f"[wholesale-complete] idx={_processing_index} "
                f"process_no={p.process_no} kind={_kind} "
                f"product_id={getattr(p.product, 'id', None)} "
                f"piece={p.piece} gram={p.gram} amount={p.amount}"
            )

            # ═══ FAZ 23: KASA / ÖDEME KALEMİ ═══
            if p.bank_account_id is not None:
                from apps.process.models import Payment

                pay_currency = p.payment_currency or (
                    p.bank_account.currency if p.bank_account else 'TRY'
                )
                is_fx = (p.bank_account.currency == 'FX') if p.bank_account else False

                # 1) Payment kaydı (kasadan çıkış)
                _pay_extra = {}
                if is_fx and pay_currency != 'TRY':
                    _FX_SENTINEL_RATES = {
                        'USD': Decimal('0.01'), 'EUR': Decimal('0.02'),
                        'GBP': Decimal('0.03'), 'CHF': Decimal('0.04'),
                        'CAD': Decimal('0.05'), 'AUD': Decimal('0.06'),
                        'JPY': Decimal('0.07'), 'QAR': Decimal('0.08'),
                        'SAR': Decimal('0.09'),
                    }
                    _pay_extra['currency_amount'] = p.amount
                    _pay_extra['exchange_rate'] = _FX_SENTINEL_RATES.get(
                        pay_currency, Decimal('0.09')
                    )

                # GİRİŞ (PURCHASE) → kasaya para GİRER → is_output=False
                # ÇIKIŞ (SALE)     → kasadan para ÇIKAR → is_output=True
                is_cash_out = (p.transaction_type != 'PURCHASE')

                Payment.objects.create(
                    process_no=p.process_no,
                    payment_type='CASH',
                    amount=p.amount,
                    is_output=is_cash_out,
                    bank_account=p.bank_account,
                    is_approved=True,
                    is_cancelled=False,
                    reconciliation_status='NOT_REQUIRED',
                    reference=f'[{pay_currency}] Tedarikçi: {supplier.company_name} — Toptan',
                    **_pay_extra,
                )

                # 2) SupplierLedger kaydı
                # GİRİŞ (PURCHASE) → Tedarikçi kasaya para getiriyor → ENTRY (alacak)
                # ÇIKIŞ (SALE)     → Tedarikçiye ödeme yapılıyor   → EXIT  (borç)
                ledger_dir = 'ENTRY' if p.transaction_type == 'PURCHASE' else 'EXIT'

                book_supplier_tx(
                    supplier=supplier,
                    transaction_type=ledger_dir,
                    amount_value=p.amount,
                    currency=pay_currency,
                    process_no=p.process_no,
                    description=f'Toptan kasa ödemesi ({pay_currency})',
                    auto_setoff=True,
                )

                # 3) Process durumunu güncelle (stok/fatura yok)
                p.supplier_id = supplier.id
                p.is_status = 'COMPLETED'
                p.save(update_fields=['supplier_id', 'is_status'])

                # Kasa kalemleri processed_rows'a eklenmez (fatura oluşmaz)
                _processing_summary.append({
                    'idx': _processing_index, 'process_no': p.process_no,
                    'kind': 'CASH_PAYMENT', 'status': 'OK',
                })
                continue

            # ═══ ÜRÜN KALEMİ (mevcut akış, değişmez) ═══
            # Giriş mi Çıkış mı?
            mv = 'ENTRY' if p.transaction_type in ('PURCHASE', 'STOCK_IN', 'RETURN', 'ORDER_IN') else 'EXIT'

            # --- YENİ: Birim Has Maliyeti Hesaplama ---
            qty_for_cost = Decimal(str(p.gram)) if p.gram > 0 else Decimal(str(p.piece))
            unit_cost_hs = Decimal(str(p.price_hs)) / qty_for_cost if qty_for_cost > 0 else Decimal('0.000')

            # --- FAZ HURDA-WAC: Hurda havuz milyemini ağırlıklı ortalama ile güncelle ---
            # Yalnızca hurda (is_scrap=True) ve GİRİŞ (ENTRY) kalemlerinde uygulanır.
            # update_product_stock'tan ÖNCE çağrılır; formül fiilî mevcut snapshot
            # gramını kullandığı için yeni gram eklenmeden hesaplanmalıdır.
            if p.product and bool(getattr(p.product, 'is_scrap', False)) and mv == 'ENTRY' and Decimal(str(p.gram or 0)) > 0:
                try:
                    from apps.scraps.views import update_scrap_pool_weighted_mileage
                    _new_mileage_raw = p.process_mileage or p.product.product_mileage or 0
                    update_scrap_pool_weighted_mileage(
                        product=p.product, store=request.user.store,
                        new_gram=Decimal(str(p.gram)),
                        new_mileage=Decimal(str(_new_mileage_raw)),
                    )
                except Exception:
                    # Milyem WAC güncellemesi başarısız olsa bile stok hareketi devam eder.
                    pass

            # --- B-FAZ 5: Bilezik havuz milyemini ağırlıklı ortalama ile güncelle ---
            # Hurda WAC bloğuyla aynı paradigma; tetikleyici: kayıtın bir
            # Bracelets satırı varsa (havuz bilezik). is_scrap olanlar hariç
            # (zaten yukarıda işlendi). update_product_stock'tan ÖNCE çağrılır.
            elif (p.product and mv == 'ENTRY' and Decimal(str(p.gram or 0)) > 0
                    and not bool(getattr(p.product, 'is_scrap', False))):
                _is_bracelet = Bracelets.objects.filter(
                    product=p.product, store=request.user.store,
                ).exists()
                if _is_bracelet:
                    try:
                        from apps.bracelets.views import update_bracelet_pool_weighted_mileage
                        _new_mileage_raw = p.process_mileage or p.product.product_mileage or 0
                        update_bracelet_pool_weighted_mileage(
                            product=p.product, store=request.user.store,
                            new_gram=Decimal(str(p.gram)),
                            new_mileage=Decimal(str(_new_mileage_raw)),
                        )
                    except Exception:
                        # WAC güncellemesi başarısız olsa bile stok hareketi devam eder.
                        pass

            # A) Stok Güncelleme (unit_cost_hs parametresi eklendi)
            update_product_stock(
                p.product, mv, p.piece, p.gram,
                False, request.user, 'Toptan işlem', p.process_no,
                unit_cost_hs=unit_cost_hs
            )

            # ── FAZ 9.6: Tekil barkodlu ürünler için is_completed güncelleme ──
            prod = p.product
            if prod and prod.barcode and not bool(getattr(prod, 'is_scrap', False)):
                prod.is_completed = (mv == 'EXIT')
                prod.save(update_fields=['is_completed'])

            # B) Cari Hareket (Ledger) - Değişmedi
            book_supplier_product_tx(
                supplier=supplier, product=p.product, process_no=p.process_no,
                tx_type=mv, piece=p.piece, gram=Decimal(str(p.gram or 0)),
                tl_total=Decimal(str(p.amount or 0)),
                price_hs=Decimal(str(p.price_hs or 0)),
                user=request.user,
            )

            # C) Process Durumunu Güncelle - Değişmedi
            p.supplier_id = supplier.id
            p.is_status = 'COMPLETED'
            p.save(update_fields=['supplier_id', 'is_status'])

            # Listeye ekle
            processed_rows.append(p)

            # FAZ 11 / CAT-03: kalemin başarıyla işlendiğini özet listesine ekle
            _processing_summary.append({
                'idx': _processing_index, 'process_no': p.process_no,
                'kind': 'PRODUCT', 'status': 'OK',
                'product_id': str(getattr(p.product, 'id', '') or ''),
            })

        # 3. OTOMATİK FATURA / GİDER PUSULASI OLUŞTURMA
        generated_invoice_id = None

        # Alış (Gider Pusulası) gerektirenleri filtrele — sadece ürün kalemleri
        purchase_items = [
            x for x in processed_rows
            if x.transaction_type in ('PURCHASE', 'STOCK_IN', 'RETURN')
        ]

        if purchase_items:
            # ARTIK YENİ FONKSİYONU KULLANIYORUZ
            try:
                invoice = create_expense_voucher_from_processes(
                    processes=purchase_items,
                    user=request.user,
                    note=f"Toptan İşlem No: {proc_no}"
                )

                generated_invoice_id = str(invoice.id)

                # Oluşan Fatura Numarasını Process satırlarına geri yaz
                for pp in purchase_items:
                    pp.invoice_no = invoice.invoice_no
                    pp.save(update_fields=['invoice_no'])

            except Exception as e:
                # Fatura oluşmazsa bile işlemi durdurma, logla
                print(f"Fatura oluşturma hatası: {e}")

        return JsonResponse({
            'result': True,
            'message': 'İşlem tamamlandı, cariye işlendi ve gider pusulası oluştu.',
            'invoice_id': generated_invoice_id,
            'has_invoice': bool(generated_invoice_id)
        })

    except ValidationError as e:
        msg = e.message if hasattr(e, 'message') else str(e)
        if hasattr(e, 'messages'):
            msg = '<br>'.join(e.messages)
        # FAZ 11 / CAT-03: Hata anında hangi kalemde olduğunu mesaja ekle
        try:
            _failed_idx = _processing_index if '_processing_index' in locals() else None
            if _failed_idx:
                msg = f'[Kalem #{_failed_idx}] {msg}'
        except Exception:
            pass
        return JsonResponse({'error': True, 'error_msg': msg}, status=400)

    except Exception as e:
        # FAZ 11 / CAT-03: Loglara hangi kalemde patlandığı + özet liste yazılır
        import logging as _err_log
        _err_logger = _err_log.getLogger('apps.process.wholesale')
        try:
            _failed_idx = _processing_index if '_processing_index' in locals() else 'N/A'
            _err_logger.error(
                f"[wholesale-complete] FAILED at idx={_failed_idx} — "
                f"summary={_processing_summary if '_processing_summary' in dir() else []} — "
                f"error={e}"
            )
        except Exception:
            _err_logger.error(f"[wholesale-complete] FAILED — error={e}")
        return JsonResponse({
            'error': True,
            'error_msg': f'Sunucu Hatası: {str(e)}',
            # Hangi kalemin patlattığı kullanıcıya da gösterilir
            'failed_at_index': _processing_index if '_processing_index' in locals() else None,
        }, status=500)


# ──────────────────────────────────────────────────────
# FAZ 21: TEDARİKÇİ NAKİT ÖDEME (Kasa Entegrasyonu)
# ──────────────────────────────────────────────────────

@login_required
@transaction.atomic
def supplier_cash_payment(request):
    """
    FAZ 21: Tedarikçiye nakit (kasa) ödeme yapar.

    Bu işlem:
    1. Tedarikçi cari hesabından (SupplierLedger) borç düşer
    2. İlgili kasadan (BankAccount) para çıkışı (Payment) oluşturur

    POST parametreleri:
        supplier_id    — Supplier UUID
        amount         — Ödeme tutarı (pozitif)
        currency       — Para birimi (TRY, USD, EUR vb.)
        bank_account_id — Ödemenin yapılacağı kasa UUID
        notes          — Açıklama (opsiyonel)
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'}, status=405)

    from apps.banking.models import BankAccount
    from apps.process.models import Payment

    store = getattr(request.user, 'store', None)
    if not store:
        return JsonResponse({'error': True, 'error_msg': 'Mağaza bulunamadı.'})

    supplier_id = request.POST.get('supplier_id', '').strip()
    amount_str = request.POST.get('amount', '').strip()
    currency = (request.POST.get('currency', '') or 'TRY').strip().upper()
    bank_account_id = request.POST.get('bank_account_id', '').strip()
    notes = request.POST.get('notes', '').strip()

    if not supplier_id:
        return JsonResponse({'error': True, 'error_msg': 'Tedarikçi seçilmedi.'})
    if not amount_str:
        return JsonResponse({'error': True, 'error_msg': 'Tutar girilmelidir.'})

    try:
        amount = Decimal(amount_str.replace(',', '.'))
    except (InvalidOperation, ValueError):
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz tutar formatı.'})

    if amount <= 0:
        return JsonResponse({'error': True, 'error_msg': 'Tutar 0 veya negatif olamaz.'})

    supplier = get_object_or_404(Suppliers, pk=supplier_id)

    # Kasa kontrolü (opsiyonel — belirtilmemişse otomatik bul)
    ba = None
    if bank_account_id:
        ba = BankAccount.objects.filter(
            id=bank_account_id, store=store, is_deleted=False, is_active=True,
        ).first()

    if not ba:
        # Otomatik kasa bul (FAZ 20 Merkez Kasa mantığı)
        from apps.process.fast_views import _resolve_or_create_cash_account
        ba, _ = _resolve_or_create_cash_account(store, currency if currency != 'HS' else 'TRY')

    if not ba:
        return JsonResponse({'error': True, 'error_msg': f'{currency} kasası bulunamadı.'})

    # 1. Cari kaydı — Tedarikçiye ödeme (borç azaltma)
    process_no = generate_process_no()
    book_supplier_tx(
        supplier=supplier,
        transaction_type='EXIT',  # Tedarikçiye ödeme = EXIT (biz ödüyoruz)
        amount_value=amount,
        currency=currency,
        process_no=process_no,
        description=notes or f'Nakit ödeme ({currency})',
    )

    # 2. Kasa kaydı — Para çıkışı (Payment)
    _pay_extra = {}
    acct_currency = getattr(ba, 'currency', 'TRY') or 'TRY'
    if acct_currency == 'FX' and currency != 'TRY':
        # Merkez Döviz Kasası — sentinel rate + reference prefix
        _FX_SENTINEL_RATES = {
            'USD': Decimal('0.01'), 'EUR': Decimal('0.02'), 'GBP': Decimal('0.03'),
            'CAD': Decimal('0.04'), 'QAR': Decimal('0.05'),
        }
        _pay_extra['currency_amount'] = amount
        _pay_extra['exchange_rate'] = _FX_SENTINEL_RATES.get(currency, Decimal('0.09'))

    Payment.objects.create(
        process_no=process_no,
        payment_type='CASH',
        amount=amount,
        is_output=True,  # Kasadan ÇIKIŞ (tedarikçiye ödeme)
        bank_account=ba,
        reconciliation_status=Payment.ReconciliationStatus.NOT_REQUIRED,
        is_approved=True,
        reference=f'[{currency}] Tedarikçi: {supplier.company_name or supplier.first_name} — {notes}' [:100],
        **_pay_extra,
    )

    return JsonResponse({
        'result': True,
        'message': f'{amount} {currency} tedarikçiye ödendi. Kasa ve cari güncellendi.',
        'process_no': process_no,
    })


# ============================================================================
# ÇOKLU HURDA GİRİŞİ (Multi-Row Scrap Entry)
# ============================================================================

@login_required
@transaction.atomic
def add_scrap_multi_to_wholesale_process(request):
    """
    Tek bir POST isteği ile birden fazla Milyem-Gram çifti gönderilerek
    toptan ekranında çoklu hurda girişi yapılmasını sağlar.

    Mevcut add_scrap_to_wholesale_process mantığını satır bazında tekrarlar.
    StockService çağrısı yapılmaz (FAZ 6 kuralı: complete_process_wholesale'de yapılır).
    """
    if request.method != 'POST':
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz istek'}, status=405)

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'result': False, 'error_msg': 'Geçersiz JSON verisi'}, status=400)

    rows = body.get('rows', [])
    if not rows:
        return JsonResponse({'result': False, 'error_msg': 'En az bir satır girilmelidir'}, status=400)

    store = request.user.store
    process_no = body.get('wholesale_process_no') or generate_process_no()

    # Kategori
    category = Categories.objects.filter(name__icontains='Hurda').first()
    if not category:
        category = Categories.objects.create(name='Hurda', store=store)

    created_count = 0
    skipped_rows = []  # FAZ 11 / CAT-02: Hatalı satırlar burada raporlanır

    for idx, row in enumerate(rows, start=1):
        raw_gram = str(row.get('gram', '0')).strip()
        raw_mileage = str(row.get('product_mileage', '0')).strip()
        name = row.get('scrap_name', '')

        gram = to_decimal(raw_gram)

        if gram <= 0:
            skipped_rows.append({'row': idx, 'reason': 'Gram 0 veya negatif'})
            continue

        # FAZ 11 / CAT-02: Milyem validasyonu (her satır için zorunlu)
        ok, err, validated_mileage = validate_mileage(
            raw_mileage, required=True, field_label=f'Satır {idx} milyemi'
        )
        if not ok:
            skipped_rows.append({'row': idx, 'reason': err})
            continue

        # Milyem tam sayı olarak saklanır (14K=585, 22K=916 vb.)
        product_mileage = Decimal(int(validated_mileage))

        unit_buy_price_hs = round(product_mileage / Decimal('1000'), 3)
        total_hs = (gram * product_mileage) / Decimal('1000')

        # ONARIM FAZI 9 — Havuz anahtarı KULLANICI SEÇİMİ ('14 Ayar' vb.);
        # milyem değerinden TÜRETİLMEZ. Milyem ağırlıklı ortalama
        # complete_process_wholesale aşamasında güncellenir.
        from apps.scraps.views import (
            find_scrap_pool_by_selected_karat,
            extract_scrap_karat_label,
        )
        canonical_karat_label = extract_scrap_karat_label(
            scrap_name=name,
            fallback_mileage=product_mileage,
            material_type='GOLD',
        )
        # ONARIM FAZI 7 / ADIM 3 — material_type='GOLD' explicit
        existing_product = find_scrap_pool_by_selected_karat(
            store=store, category=category,
            scrap_name=name,
            fallback_mileage=product_mileage,
            is_scrap=True, material_type='GOLD',
        )

        if existing_product:
            product = existing_product

            # ------------------------------------------------------------------
            # ONARIM FAZI 7 / ADIM 3 — TOPTAN-HURDA KÖPRÜSÜ (Scraps reset)
            # ------------------------------------------------------------------
            scrap_record, _ = Scraps.objects.get_or_create(
                store=store, product=product,
                defaults={'created_by': request.user},
            )
            _scrap_reset_fields = []
            if scrap_record.is_deleted:
                scrap_record.is_deleted = False
                _scrap_reset_fields.append('is_deleted')
            if scrap_record.is_active is False:
                scrap_record.is_active = True
                _scrap_reset_fields.append('is_active')
            if _scrap_reset_fields:
                scrap_record.save(update_fields=_scrap_reset_fields)

            if product.is_active is False:
                Products.objects.filter(id=product.id).update(is_active=True)
                product.is_active = True

            # ------------------------------------------------------------------
            # ONARIM FAZI 9 — Revival koşulu DARALTILDI: yalnızca gerçekten
            # soft-deleted/pasif havuzlar revival sayılır. Bayrak reseti
            # (taze açılan Scraps satırı) revival değildir. Ayrıca legacy
            # alan sıfırlama YALNIZCA stok kalıntısı varsa çalışır.
            # ------------------------------------------------------------------
            was_revival = (
                scrap_record.is_deleted is True
                or scrap_record.is_active is False
                or product.is_active is False
            )
            if was_revival:
                _stale_gram = Decimal('0')
                _stale_pieces = 0
                try:
                    _stale_snap = (
                        StockSnapshot.objects
                        .filter(product=product, store=store)
                        .first()
                    )
                    _stale_gram = (
                        Decimal(str(_stale_snap.stock_gram))
                        if (_stale_snap and _stale_snap.stock_gram is not None)
                        else Decimal('0')
                    )
                    _stale_pieces = (
                        int(_stale_snap.stock_pieces or 0) if _stale_snap else 0
                    )
                    if _stale_gram > 0 or _stale_pieces > 0:
                        StockService.adjustment(
                            product=product,
                            store=store,
                            actual_gram=Decimal('0'),
                            actual_pieces=0,
                            ref_id=f"wholesale_scrap_revival_{product.id}_{process_no}_{idx}",
                            user=request.user,
                            notes=(
                                "Toptan hurda havuzu yeniden açılışı: önceki "
                                "silme/iptal sonrası kalan stok temizlendi"
                            ),
                        )
                except Exception:
                    pass
                if _stale_gram > 0 or _stale_pieces > 0:
                    Products.objects.filter(id=product.id).update(
                        gram=Decimal('0'),
                        product_mileage=Decimal('0'),
                    )
                    product.gram = Decimal('0')
                    product.product_mileage = Decimal('0')
        else:
            # ONARIM FAZI 9 — Yeni havuzun ismi canonical "X Ayar" etiketi.
            final_scrap_name = (
                canonical_karat_label
                or (name.strip() if isinstance(name, str) and name.strip() else '')
                or f"{int(product_mileage)} Milyem Hurda"
            )
            product = Products.objects.create(
                store=store,
                category=category,
                name=final_scrap_name,
                gram=Decimal('0'),
                product_mileage=product_mileage,
                buy_price_hs=unit_buy_price_hs,
                sale_price_hs=unit_buy_price_hs,
                is_scrap=True,
                is_gram_bullion=True,
                material_type='GOLD',
                created_by=request.user,
                created_on=timezone.now()
            )
            Scraps.objects.create(store=store, product=product, created_by=request.user)

        # StockSnapshot hazırla
        StockSnapshot.objects.get_or_create(
            product=product,
            store=store,
            defaults={
                'stock_gram': Decimal('0.0000'),
                'stock_pieces': 0,
                'weighted_avg_cost_hs': Decimal('0.0000'),
                'weighted_avg_cost_tl': Decimal('0.00'),
            }
        )

        # Process kaydı (IN_PROGRESS)
        Process.objects.create(
            store=store,
            process_no=process_no,
            process_type='WHOLESALE',
            transaction_type='PURCHASE',
            product=product,
            employee=request.user,
            piece=0,
            gram=gram,
            process_mileage=str(product_mileage),
            price_hs=total_hs,
            unit_price=Decimal('0'),
            amount=Decimal('0'),
            is_status='IN_PROGRESS'
        )

        created_count += 1

    if created_count == 0:
        return JsonResponse({
            'result': False,
            'error_msg': 'Geçerli satır bulunamadı',
            'skipped': skipped_rows,  # FAZ 11 / CAT-02: Hatalı satır detayı
        }, status=400)

    # Kısmi başarı durumunda hem oluşturulan hem atlanan satır sayısı döner
    msg = f'{created_count} hurda satırı listeye eklendi.'
    if skipped_rows:
        msg += f' ({len(skipped_rows)} satır atlandı — milyem/gram hatalı.)'

    return JsonResponse({
        'result': True,
        'wholesale_process_no': process_no,
        'rows_count': created_count,
        'skipped_rows_count': len(skipped_rows),
        'skipped': skipped_rows,
        'message': msg,
    })
