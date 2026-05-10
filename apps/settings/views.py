from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from apps.stores.models import Stores
from apps.settings.models import StoreConfiguration, StoreLabelSettings

from apps.activity_logs.views import write_log
from decimal import Decimal

import json

# Beyaz listeler — model seviyesindeki choices ile tutarlı olmalı
VALID_LANGUAGE_CODES = {'tr', 'de', 'en'}
VALID_BASE_SPOT_CURRENCIES = {'USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'CHF'}
VALID_BASE_SPOT_UNITS = {'OZ', 'GRAM', 'KILO', 'TOLA'}


@login_required
@require_POST
def update_configuration(request):
    store_id = request.POST.get('store_id')
    key = request.POST.get('key')
    value = request.POST.get('value')

    ALLOWED_FIELDS = [
        # --- Dil & Bölgesel (juwelier_plus port) ---
        'language_code',
        'base_spot_currency',
        'base_spot_unit',

        'price_margin_percent',
        'use_average_labor',
        'use_manual_has_calculation',
        'use_manual_currency_rate',  # T3 (2026-04-29): Manuel Kur switch
        'active_pricing_chamber_id',

        'enforce_cash_limit',
        'enforce_invoice_customer',
        'enforce_masak_identity',

        'is_safe_approval_required',  # FAZ 18: Onaylı Kasa

        # --- Müşteri Validasyon Senkronizasyon Fazı ---
        'enforce_customer_always',
        'require_customer_phone',
        'require_customer_tckn',

        # --- FAZ 38: Cari borç birim modu + fazla tahsilat varsayılanı ---
        'debt_currency_mode',          # 'HS' | 'TL' (choice field)
        'allow_overpayment_default',   # boolean

        'notify_email_2fa',
        'notify_email_password_reset',
        'notify_email_contact_verify',
        'notify_email_staff_login',
        'notify_email_new_store_register',
        'notify_email_welcome',
        'notify_email_ops',
        'notify_email_workshops',
        'notify_email_repair_updates',
        'notify_email_reports'
    ]

    if key not in ALLOWED_FIELDS:
        return JsonResponse({'success': False, 'error': f'Geçersiz ayar parametresi: {key}'})

    try:
        if request.user.is_superuser:
            store = Stores.objects.get(id=store_id)
        else:
            store = request.user.store
            if not store or str(store.id) != str(store_id):
                return JsonResponse({'success': False, 'error': 'Yetkisiz işlem.'})

        config, created = StoreConfiguration.objects.get_or_create(store=store)

        if key == 'language_code':
            _v = (value or '').strip().lower()
            if _v not in VALID_LANGUAGE_CODES:
                return JsonResponse({'success': False, 'error': f'Geçersiz dil kodu: {_v}'})
            config.language_code = _v
            # Dil cache'ini temizle (StoreLanguageMiddleware yeniden okusun)
            try:
                from django.core.cache import cache
                cache.delete(f'store_lang_{store.id}')
            except Exception:
                pass

        elif key == 'base_spot_currency':
            _v = (value or '').strip().upper()
            if _v not in VALID_BASE_SPOT_CURRENCIES:
                return JsonResponse({'success': False, 'error': f'Geçersiz spot para birimi: {_v}'})
            config.base_spot_currency = _v

        elif key == 'base_spot_unit':
            _v = (value or '').strip().upper()
            if _v not in VALID_BASE_SPOT_UNITS:
                return JsonResponse({'success': False, 'error': f'Geçersiz spot birimi: {_v}'})
            config.base_spot_unit = _v

        elif key == 'price_margin_percent':
            try:
                clean_val = str(value).replace(',', '.')
                if not clean_val.strip():
                    clean_val = '0.00'
                setattr(config, key, Decimal(clean_val))
            except Exception:
                return JsonResponse({'success': False, 'error': 'Geçersiz sayı formatı.'})

        elif key == 'active_pricing_chamber_id':
            if value and value.strip():
                setattr(config, key, value)
            else:
                setattr(config, key, None)

        elif key == 'debt_currency_mode':
            # FAZ 38 — Borç birim modu choice field. Yalnızca 'HS' veya 'TL'.
            valid = {c[0] for c in StoreConfiguration.DEBT_MODE_CHOICES}
            if value not in valid:
                return JsonResponse({
                    'success': False,
                    'error': f'Geçersiz borç modu: {value} (beklenen: HS / TL).',
                })
            setattr(config, key, value)

        else:
            new_bool = (str(value).lower() == 'true')

            # --- MİNİMUM VERİ GÜVENLİK KURALI ---
            # require_customer_phone ve require_customer_tckn ikisi birden
            # kapatılamaz. Kullanıcı birini kapatmaya çalışıyor ve diğeri zaten
            # kapalıysa işlem reddedilir; frontend toggle'ı eski haline döner.
            if key in ('require_customer_phone', 'require_customer_tckn') and not new_bool:
                other_key = 'require_customer_tckn' if key == 'require_customer_phone' else 'require_customer_phone'
                other_val = getattr(config, other_key, False)
                if not other_val:
                    return JsonResponse({
                        'success': False,
                        'error': (
                            'Telefon ve T.C./Vergi Kimlik zorunluluklarının ikisini birden '
                            'kapatamazsınız. En az birinin açık kalması gerekir.'
                        ),
                    })

            setattr(config, key, new_bool)

        config.save()

        write_log(request, "Ayarlar", f"Ayar güncellendi: {key} -> {value}")
        return JsonResponse({'success': True})

    except Stores.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Mağaza bulunamadı.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


# ============================================================================
# T3 (2026-04-29): Manuel Kur (Döviz) Değerlerini Toplu Güncelleme
# ============================================================================
# Frontend, "Manuel Kur Ayarı" switch'i açıkken döviz ürünleri için alış/satış
# TL inputlarını gösterir. Kullanıcı "Kaydet" deyince burası çağrılır.
#
# POST data:
#   store_id: <uuid>
#   rates:    JSON string -> { "<product_uuid>": {"buy_tl": "45.20", "sell_tl": "46.50"} }
#
# Backend StoreConfiguration.manual_currency_rates JSONField'ına yazar.
# is_currency=True ürünlerinde StockSnapshot ölü veri olduğu için bu yapıya yazma
# StockLedger / WAC / FXBalance hiçbir hesabı etkilemez — sadece view'da
# fiyat üretimi için override sağlar.
# ============================================================================

@login_required
@require_POST
def update_manual_currency_rates(request):
    store_id = request.POST.get('store_id')
    raw = request.POST.get('rates') or '{}'

    try:
        rates = json.loads(raw)
        if not isinstance(rates, dict):
            raise ValueError('rates dict olmalı')
    except (ValueError, json.JSONDecodeError) as e:
        return JsonResponse({'success': False, 'error': f'Geçersiz JSON: {e}'})

    try:
        if request.user.is_superuser:
            store = Stores.objects.get(id=store_id)
        else:
            store = request.user.store
            if not store or str(store.id) != str(store_id):
                return JsonResponse({'success': False, 'error': 'Yetkisiz işlem.'})

        # Veri sanitize: pozitif Decimal değerleri string olarak sakla
        clean = {}
        for pid, entry in rates.items():
            if not isinstance(entry, dict):
                continue
            try:
                buy = Decimal(str(entry.get('buy_tl') or '0').replace(',', '.'))
                sell = Decimal(str(entry.get('sell_tl') or '0').replace(',', '.'))
            except Exception:
                continue
            if buy < 0 or sell < 0:
                continue
            clean[str(pid)] = {
                'buy_tl': f'{buy:.4f}',
                'sell_tl': f'{sell:.4f}',
            }

        config, _ = StoreConfiguration.objects.get_or_create(store=store)
        config.manual_currency_rates = clean
        config.save(update_fields=['manual_currency_rates', 'updated_at'])

        write_log(request, "Ayarlar", f"Manuel kur override güncellendi ({len(clean)} ürün)")
        return JsonResponse({'success': True, 'count': len(clean)})

    except Stores.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Mağaza bulunamadı.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_POST
def update_store_display(request):
    """
    Mağazanın ekran adı (title) ve barkod adını (barcode_title) günceller.
    Canlı ekran ve etiket yazdırmada bu isim kullanılır.

    POST: { store_id, title, barcode_title }
    """
    store_id = request.POST.get('store_id')
    title = request.POST.get('title', '').strip()
    barcode_title = request.POST.get('barcode_title', '').strip()

    try:
        if request.user.is_superuser:
            store = Stores.objects.get(id=store_id)
        else:
            store = request.user.store
            if not store or str(store.id) != str(store_id):
                return JsonResponse({'success': False, 'error': 'Yetkisiz işlem.'})

        update_fields = ['updated_at']

        if title is not None:
            store.title = title or None
            update_fields.append('title')

        if barcode_title is not None:
            store.barcode_title = barcode_title or None
            update_fields.append('barcode_title')

        store.save(update_fields=update_fields)

        write_log(request, "Ayarlar", f"Mağaza ekran adı güncellendi: {title}")
        return JsonResponse({
            'success': True,
            'title': store.title or '',
            'barcode_title': store.barcode_title or '',
        })

    except Stores.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Mağaza bulunamadı.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


@login_required
@require_POST
def update_label_settings(request):
    try:
        if request.user.is_superuser and request.POST.get('store_id'):
            store = Stores.objects.get(id=request.POST.get('store_id'))
        else:
            store = request.user.store

        settings, _ = StoreLabelSettings.objects.get_or_create(store=store)

        # Firma Adı (barcode_title) → Stores modeline kaydedilir
        barcode_title = request.POST.get('barcode_title')
        if barcode_title is not None:
            store.barcode_title = barcode_title.strip()
            store.save(update_fields=['barcode_title'])

        active_size = request.POST.get('active_size')
        if active_size in ['small', 'large']:
            settings.active_size = active_size

        # Sol Alt Köşe Verisi (Milyem / Maliyet)
        bottom_left = request.POST.get('label_bottom_left_type')
        if bottom_left in ['MILYEM', 'MALIYET']:
            settings.label_bottom_left_type = bottom_left

        # Etiket Düzeni (Standart / Yansımalı / Yan Yana)
        layout_mode = request.POST.get('label_layout_mode')
        if layout_mode in ['STANDARD', 'REFLECTED', 'SIDE_BY_SIDE']:
            settings.label_layout_mode = layout_mode

        # Yan Yana Etiket Genişliği (%) — 30-70 arası int
        lwp = request.POST.get('label_width_percentage')
        if lwp is not None:
            try:
                lwp_val = int(lwp)
                if 30 <= lwp_val <= 70:
                    # Her iki boyut config'ine de yaz (aktif boyut hangisiyse o kullanılacak)
                    for design_attr in ('small_design', 'large_design'):
                        d = getattr(settings, design_attr) or {}
                        d['label_width_percentage'] = lwp_val
                        setattr(settings, design_attr, d)
            except (ValueError, TypeError):
                pass

        # Barkod Çizgileri Pixel Genişliği — 10-200 arası int (tablo Genişlik sütunu)
        blw = request.POST.get('barcode_lines_width')
        if blw is not None:
            try:
                blw_val = int(blw)
                if 10 <= blw_val <= 200:
                    # Her iki boyut config'ine de yaz
                    for design_attr in ('small_design', 'large_design'):
                        d = getattr(settings, design_attr) or {}
                        d['barcode_lines_width'] = blw_val
                        setattr(settings, design_attr, d)
            except (ValueError, TypeError):
                pass

        # Barkod Çizgileri Ayarları
        bl_x = request.POST.get('barcode_lines_x')
        bl_y = request.POST.get('barcode_lines_y')
        bl_h = request.POST.get('barcode_lines_height')
        bl_vis = request.POST.get('barcode_lines_visible')
        if bl_x is not None:
            settings.barcode_lines_x = int(bl_x)
        if bl_y is not None:
            settings.barcode_lines_y = int(bl_y)
        if bl_h is not None:
            settings.barcode_lines_height = int(bl_h)
        if bl_vis is not None:
            settings.barcode_lines_visible = bl_vis in ('true', 'True', '1', 'on')

        # RFID Yazıcı Modu (standart / RFID'li yazıcı ayrımı)
        rfid_mode = request.POST.get('rfid_mode')
        if rfid_mode is not None:
            settings.rfid_mode = rfid_mode in ('true', 'True', '1', 'on')

        design_type = request.POST.get('design_type')
        design_data_json = request.POST.get('design_data')
        material_type = (request.POST.get('material_type') or 'GOLD').upper()

        # Material → JSONField alan adı eşlemesi (GOLD geriye dönük uyumlu)
        DESIGN_FIELD_MAP = {
            'GOLD':    {'small': 'small_design',         'large': 'large_design'},
            'DIAMOND': {'small': 'diamond_small_design', 'large': 'diamond_large_design'},
            'WATCH':   {'small': 'watch_small_design',   'large': 'watch_large_design'},
        }

        if design_type and design_data_json:
            new_design = json.loads(design_data_json)
            field_map = DESIGN_FIELD_MAP.get(material_type, DESIGN_FIELD_MAP['GOLD'])
            field_name = field_map.get(design_type)

            if field_name:
                current = getattr(settings, field_name) or {}
                current.update(new_design)
                setattr(settings, field_name, current)

        settings.save()
        write_log(request, "Ayarlar", f"Etiket ayarı güncellendi. Boyut: {active_size}")

        return JsonResponse({'success': True})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
