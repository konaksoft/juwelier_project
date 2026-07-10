from django.db import models
from apps.stores.models import Stores


class StoreConfiguration(models.Model):
    store = models.OneToOneField(Stores, on_delete=models.CASCADE, related_name='config')

    # --- 0. DİL & BÖLGESEL AYARLAR (juwelier_plus port) ---
    LANGUAGE_CHOICES = [
        ('de', 'Almanca (Deutsch)'),
        ('en', 'İngilizce (English)'),
        ('tr', 'Türkçe'),
    ]
    language_code = models.CharField(
        max_length=5,
        choices=LANGUAGE_CHOICES,
        default='tr',
        verbose_name='Mağaza Dili',
        help_text='Bu mağazanın tüm kullanıcıları için uygulama dilini zorlar.',
    )

    # --- 0c. MAĞAZA YEREL SAAT DİLİMİ (FAZ 1 — Almanya/Avrupa Pazarı) ---
    # Çoklu mağaza senaryosunda her mağaza kendi yerel saatini belirleyebilir.
    # Sistem genel TIME_ZONE='Europe/Berlin' default'unu override eder.
    # Ekstre/rapor/fiş zaman damgaları bu mağazaya göre yerelleştirilir.
    # Şimdilik UI'da read-only — ileride per-store middleware ile aktif edilir.
    TIMEZONE_CHOICES = [
        ('Europe/Berlin', 'Berlin (UTC+1 / UTC+2 DST)'),
        ('Europe/Vienna', 'Wien (UTC+1 / UTC+2 DST)'),
        ('Europe/Zurich', 'Zürich (UTC+1 / UTC+2 DST)'),
        ('Europe/Paris', 'Paris (UTC+1 / UTC+2 DST)'),
        ('Europe/Amsterdam', 'Amsterdam (UTC+1 / UTC+2 DST)'),
        ('Europe/Brussels', 'Brüssel (UTC+1 / UTC+2 DST)'),
        ('Europe/London', 'London (UTC+0 / UTC+1 DST)'),
        ('Europe/Istanbul', 'İstanbul (UTC+3)'),
    ]
    timezone_code = models.CharField(
        max_length=64,
        choices=TIMEZONE_CHOICES,
        default='Europe/Berlin',
        verbose_name='Mağaza Saat Dilimi',
        help_text=(
            'Bu mağazanın yerel saat dilimi. Rapor/ekstre/fiş zaman damgaları '
            'bu saat dilimine göre gösterilir. Sistem genel TIME_ZONE varsayılanı '
            'Europe/Berlin (Almanya). Çoklu lokasyon kullanmıyorsanız değiştirmeyin.'
        ),
    )

    # --- 0a. DİNAMİK SPOT FİYATLANDIRMA TABANIS (juwelier_plus port) ---
    BASE_SPOT_CURRENCY_CHOICES = [
        ('USD', 'ABD Doları (USD)'),
        ('EUR', 'Euro (EUR)'),
        ('GBP', 'Sterlin (GBP)'),
        ('CAD', 'Kanada Doları (CAD)'),
        ('AUD', 'Avustralya Doları (AUD)'),
        ('JPY', 'Japon Yeni (JPY)'),
        ('CHF', 'İsviçre Frangı (CHF)'),
    ]
    base_spot_currency = models.CharField(
        max_length=3,
        choices=BASE_SPOT_CURRENCY_CHOICES,
        default='EUR',
        verbose_name='Spot Fiyat Taban Para Birimi',
        help_text='Dinamik spot fiyat ekranında kullanılacak para birimi. Mevcut işlem kayıtlarını etkilemez.',
    )

    BASE_SPOT_UNIT_CHOICES = [
        ('OZ', 'Troy Ons (oz)'),
        ('GRAM', 'Gram (g)'),
        ('KILO', 'Kilogram (kg)'),
        ('TOLA', 'Tola'),
    ]
    base_spot_unit = models.CharField(
        max_length=5,
        choices=BASE_SPOT_UNIT_CHOICES,
        default='OZ',
        verbose_name='Spot Fiyat Taban Birimi',
        help_text='Canlı spot fiyat okurken kullanılacak ağırlık birimi.',
    )

    # --- 0b. MAĞAZA BİRİNCİL PARA BİRİMİ (juwelier_project Almanya pazarı) ---
    # Tüm parasal işlemler bu birim üzerinden yapılır:
    # - Otomatik nakit kasası bu birimde oluşturulur
    # - UI'da fiyat sembolleri bu birime göre gösterilir
    # - Yasal limitler bu birim cinsinden değerlendirilir
    PRIMARY_CURRENCY_CHOICES = [
        ('EUR', 'Euro (€)'),
        ('TRY', 'Türk Lirası (₺)'),
        ('USD', 'ABD Doları ($)'),
        ('GBP', 'Sterlin (£)'),
        ('CHF', 'İsviçre Frangı (CHF)'),
        ('CAD', 'Kanada Doları (C$)'),
        ('AUD', 'Avustralya Doları (A$)'),
        ('JPY', 'Japon Yeni (¥)'),
    ]
    PRIMARY_CURRENCY_SYMBOLS = {
        'EUR': '€',
        'TRY': '₺',
        'USD': '$',
        'GBP': '£',
        'CHF': 'CHF',
        'CAD': 'C$',
        'AUD': 'A$',
        'JPY': '¥',
    }
    primary_currency = models.CharField(
        max_length=3,
        choices=PRIMARY_CURRENCY_CHOICES,
        default='EUR',
        verbose_name='Mağaza Birincil Para Birimi',
        help_text=(
            'Tüm satış/alış/kasa işlemlerinin temel para birimi. '
            'Otomatik nakit kasası bu birimde oluşturulur, fiyat etiketleri '
            've raporlar bu birim sembolüyle gösterilir.'
        ),
    )

    @property
    def primary_currency_symbol(self):
        """Birincil para biriminin gösterim sembolü (€, $, ₺, £ ...)."""
        return self.PRIMARY_CURRENCY_SYMBOLS.get(self.primary_currency, self.primary_currency)

    # --- 2. ONAYLI KASA (Safe Approval) ---
    # Açıldığında tüm Hızlı İşlem / Perakende Payment kayıtları is_approved=False
    # olarak kaydedilir. Bakiye hesaplamalarına dahil edilmez. Yönetici onayı
    # gerekir. Kapalıysa (default) doğrudan is_approved=True kaydedilir.
    is_safe_approval_required = models.BooleanField(
        default=False,
        verbose_name="Onaylı Kasa: Yönetici Onayı Gereksin",
        help_text=(
            "Açıldığında kasaya giriş-çıkış yapan her ödeme kaydı 'Beklemede' "
            "durumunda oluşturulur. Bakiyeye yansıması için yönetici onayı gerekir."
        ),
    )

    # --- MÜŞTERİ ZORUNLULUK AYARLARI ---
    # Tutar bağımsız müşteri seçimi zorunluluğu. Hızlı İşlem ve Perakende
    # ekranlarında Nakit/Kart/Havale butonlarına basıldığında müşteri seçili
    # değilse hem frontend hem backend tarafında işlem reddedilir.
    enforce_customer_always = models.BooleanField(
        default=False,
        verbose_name="Zorunluluk: Her İşlemde Müşteri Seçimi",
        help_text=(
            "Açıldığında Hızlı İşlem ve Perakende ekranlarında her ödemede "
            "(tutar bağımsız) müşteri seçimi zorunlu hale gelir."
        ),
    )

    # Müşteri kayıt formunda telefon zorunluluğu.
    # Varsayılan True — sistem mevcut davranışta müşteri tekilleştirmesini
    # telefon numarası üzerinden yapmaktadır; geriye uyumluluk için açık.
    require_customer_phone = models.BooleanField(
        default=True,
        verbose_name="Müşteri Kayıt: Telefon Zorunlu",
        help_text="Yeni müşteri kaydında ve güncellemede telefon zorunlu olsun.",
    )

    # --- 3. E-POSTA BİLDİRİM AYARLARI ---

    # A) GÜVENLİK VE DOĞRULAMA
    notify_email_2fa = models.BooleanField(default=True, verbose_name="E-posta: İki Faktörlü Doğrulama")

    # B) YÖNETİM VE PERSONEL
    # -------------------------------------------------------------------------
    # Personel mağazaya giriş yaptığında yöneticiye giden mail
    notify_email_staff_login = models.BooleanField(default=True, verbose_name="E-posta: Personel Giriş Bildirimi")

    # C) OPERASYONEL VE MÜŞTERİ
    # -------------------------------------------------------------------------
    # Satış, alış, iade gibi işlemlerin özeti (Fiş/Makbuz yerine geçen mail)
    notify_email_ops = models.BooleanField(default=True, verbose_name="E-posta: Genel İşlem Özetleri")
    notify_email_workshops = models.BooleanField(default=True, verbose_name="Atolye Mailleri")

    # Tamir kaydı oluşturulduğunda veya durumu değiştiğinde giden mail
    notify_email_repair_updates = models.BooleanField(default=True,
                                                      verbose_name="E-posta: Tamir Kaydı ve Durum Güncellemeleri")

    # Atölyelere veya yöneticilere gönderilen toplu raporlar
    notify_email_reports = models.BooleanField(default=True, verbose_name="E-posta: Rapor Bildirimleri")

    # ─────────────────────────────────────────────────────────────
    # FAZ 38 — CARİ DEFTER PARA BİRİMİ TERCİHİ
    # ─────────────────────────────────────────────────────────────
    # Bazı kuyumcular borcu Has (gram) cinsinden tutmak ister; mevcut
    # davranış (HS modu): satışta gram sabit, kur dalgalandıkça TL
    # karşılığı değişir. Diğerleri ise borcun TL cinsinden sabit
    # kalmasını ister; bu durumda satıştaki TL tutar borç olarak
    # yazılır, gram karşılığı bilgi amaçlı gösterilir.
    #
    # Servis katmanı (LedgerService.write_debt) bu alanı okuyup yazım
    # davranışını ayarlar. Tahsilat/önizleme ekranları da seçim'e
    # göre TL veya HS'i birincil göstergeye çıkarır.
    DEBT_MODE_HS = 'HS'
    DEBT_MODE_EUR = 'EUR'
    DEBT_MODE_CHOICES = [
        (DEBT_MODE_HS, 'Has (gram) — borç gram cinsinden sabit'),
        (DEBT_MODE_EUR, 'EUR — borç Euro cinsinden sabit'),
    ]
    debt_currency_mode = models.CharField(
        max_length=4,
        choices=DEBT_MODE_CHOICES,
        default=DEBT_MODE_HS,
        verbose_name='Cari Borç Birim Modu',
        help_text=(
            'HS: Borç gram cinsinden sabit. '
            'EUR: Borç Euro cinsinden sabit (kur dalgalandıkça gram '
            'karşılığı değişir).'
        ),
    )

    # ─────────────────────────────────────────────────────────────
    # FAZ 38 — TAHSİLAT EKRANI: FAZLA TAHSİLATI VARSAYILAN OLARAK KABUL ET
    # ─────────────────────────────────────────────────────────────
    # Tahsilat modalında kullanıcı, müşterinin borcundan fazla tutar
    # girdiğinde "Fazla Tahsilatı Kabul Et" toggle'ını açıyordu. Bazı
    # mağazalar bu işlemi her seferde manuel açmak istemiyor; mağaza
    # ayarından varsayılan davranış kontrol edilir. Toggle UI'da
    # kalır ama varsayılan değer bu alandan gelir.
    allow_overpayment_default = models.BooleanField(
        default=False,
        verbose_name='Tahsilat: Fazla Tahsilatı Varsayılan Kabul Et',
        help_text=(
            'Tahsilat ekranında "Fazla Tahsilatı Kabul Et" toggle\'ı '
            'varsayılan olarak açık gelsin. Kullanıcı yine kapatabilir.'
        ),
    )

    # --- SY-01: BANKA → 24 AYAR ALTIN FATURASI ---
    default_24k_product = models.ForeignKey(
        'products.Products',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='used_as_default_gold',
        verbose_name='Varsayılan 24 Ayar Altın Ürünü',
        help_text=(
            "Banka havalesi → toplu faturaya dönüştürme sırasında "
            "otomatik kalem olarak eklenecek 24 ayar külçe altın ürünü."
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'StoreConfiguration'
        verbose_name = 'Mağaza Ayarı'
        verbose_name_plural = 'Mağaza Ayarları'

    def __str__(self):
        return f"Ayarlar: {self.store}"


def default_small_config():
    return {
        "store_name": {"x": 180, "y": 90, "font": 20, "visible": True, "label": "Firma Adı"},
        "barcode_no": {"x": 180, "y": 145, "font": 20, "visible": True, "label": "Barkod No"},
        "quality_text": {"x": 280, "y": 145, "font": 20, "visible": True, "label": "Ayar (14K)"},
        "price": {"x": 220, "y": 175, "font": 25, "visible": True, "label": "Fiyat"},
        "mileage": {"x": 180, "y": 200, "font": 15, "visible": True, "label": "Milyem"},
        "gram": {"x": 270, "y": 200, "font": 15, "visible": True, "label": "Gram"},
        "jewelry_type": {"x": 220, "y": 220, "font": 15, "visible": True, "label": "Ürün Tipi"},
        "supplier": {"x": 220, "y": 240, "font": 12, "visible": True, "label": "Tedarikçi"},
        "ring_size": {"x": 220, "y": 260, "font": 12, "visible": False, "label": "Alyans No"},
        "label_width_percentage": 50,
        "barcode_lines_width": 70,
    }


def default_large_config():
    return {
        "store_name": {"x": 180, "y": 90, "font": 20, "visible": True, "label": "Firma Adı"},
        "barcode_no": {"x": 180, "y": 145, "font": 20, "visible": True, "label": "Barkod No"},
        "quality_text": {"x": 280, "y": 145, "font": 20, "visible": True, "label": "Ayar (14K)"},
        "price": {"x": 220, "y": 175, "font": 25, "visible": True, "label": "Fiyat"},
        "mileage": {"x": 180, "y": 200, "font": 15, "visible": True, "label": "Milyem"},
        "gram": {"x": 270, "y": 200, "font": 15, "visible": True, "label": "Gram"},
        "jewelry_type": {"x": 220, "y": 220, "font": 15, "visible": True, "label": "Ürün Tipi"},
        "supplier": {"x": 220, "y": 240, "font": 12, "visible": True, "label": "Tedarikçi"},
        "ring_size": {"x": 220, "y": 260, "font": 12, "visible": False, "label": "Alyans No"},
        "label_width_percentage": 50,
        "barcode_lines_width": 70,
    }


# ─── PIRLANTA (DIAMOND) ETİKET KONFİGÜRASYONLARI ───
# Pırlanta ürünlerinde altın bazlı alanlar (Ayar/Milyem/Gram) anlamsızdır.
# Bunun yerine 4C (Carat, Color, Clarity, Cut) + Sertifika alanları gösterilir.
def default_diamond_small_config():
    # NOT (2026-04-28): mount_karat / mount_gram opsiyonel — montürlü pırlantalarda
    # altın saflığı (örn. "18K") ve montür gramı etikete basılır. Default visible=False
    # (mevcut müşteriler bekledikleri etikete sürpriz alan eklenmesin).
    # price.show_currency=True → fiyat suffix'i (₺/$/€/£) gösterilir; isteyen kapatabilir.
    # growth_type (Taş Kökeni — NAT/LAB) aynı sebeple default visible=False; kullanmak
    # isteyen mağaza Etiket Tasarımı ekranından açar.
    return {
        "store_name":     {"x": 180, "y": 90,  "font": 20, "visible": True,  "label": "Firma Adı"},
        "barcode_no":     {"x": 180, "y": 145, "font": 20, "visible": True,  "label": "Barkod No"},
        "carat_weight":   {"x": 180, "y": 175, "font": 18, "visible": True,  "label": "Karat (ct)"},
        "color_grade":    {"x": 240, "y": 175, "font": 15, "visible": True,  "label": "Renk"},
        "clarity_grade":  {"x": 290, "y": 175, "font": 15, "visible": True,  "label": "Berraklık"},
        "cut_grade":      {"x": 180, "y": 200, "font": 13, "visible": True,  "label": "Kesim"},
        "mount_karat":    {"x": 320, "y": 200, "font": 13, "visible": False, "label": "Montür Ayarı"},
        "mount_gram":     {"x": 380, "y": 200, "font": 13, "visible": False, "label": "Montür Gramı"},
        "growth_type":    {"x": 180, "y": 280, "font": 13, "visible": False, "label": "Taş Kökeni"},
        "price":          {"x": 220, "y": 220, "font": 25, "visible": True,  "label": "Fiyat", "show_currency": True},
        "certificate_lab": {"x": 180, "y": 240, "font": 12, "visible": True,  "label": "Sertifika"},
        "certificate_no": {"x": 240, "y": 240, "font": 12, "visible": False, "label": "Sert. No"},
        "supplier":       {"x": 220, "y": 260, "font": 12, "visible": True,  "label": "Tedarikçi"},
        "label_width_percentage": 50,
        "barcode_lines_width": 70,
    }


def default_diamond_large_config():
    return {
        "store_name":     {"x": 180, "y": 90,  "font": 22, "visible": True,  "label": "Firma Adı"},
        "barcode_no":     {"x": 180, "y": 145, "font": 22, "visible": True,  "label": "Barkod No"},
        "carat_weight":   {"x": 180, "y": 175, "font": 20, "visible": True,  "label": "Karat (ct)"},
        "color_grade":    {"x": 240, "y": 175, "font": 17, "visible": True,  "label": "Renk"},
        "clarity_grade":  {"x": 290, "y": 175, "font": 17, "visible": True,  "label": "Berraklık"},
        "cut_grade":      {"x": 180, "y": 200, "font": 15, "visible": True,  "label": "Kesim"},
        "mount_karat":    {"x": 320, "y": 200, "font": 15, "visible": False, "label": "Montür Ayarı"},
        "mount_gram":     {"x": 380, "y": 200, "font": 15, "visible": False, "label": "Montür Gramı"},
        "growth_type":    {"x": 180, "y": 280, "font": 15, "visible": False, "label": "Taş Kökeni"},
        "price":          {"x": 220, "y": 220, "font": 28, "visible": True,  "label": "Fiyat", "show_currency": True},
        "certificate_lab": {"x": 180, "y": 240, "font": 14, "visible": True,  "label": "Sertifika"},
        "certificate_no": {"x": 240, "y": 240, "font": 14, "visible": False, "label": "Sert. No"},
        "supplier":       {"x": 220, "y": 260, "font": 14, "visible": True,  "label": "Tedarikçi"},
        "label_width_percentage": 50,
        "barcode_lines_width": 70,
    }


# ─── SAAT (WATCH) ETİKET KONFİGÜRASYONLARI ───
# Saat ürünlerinde Marka/Model/Referans/Mekanizma/Durum bilgileri gösterilir.
def default_watch_small_config():
    return {
        "store_name":   {"x": 180, "y": 90,  "font": 20, "visible": True,  "label": "Firma Adı"},
        "barcode_no":   {"x": 180, "y": 145, "font": 20, "visible": True,  "label": "Barkod No"},
        "brand":        {"x": 180, "y": 175, "font": 18, "visible": True,  "label": "Marka"},
        "model_name":   {"x": 180, "y": 200, "font": 15, "visible": True,  "label": "Model"},
        "reference_no": {"x": 180, "y": 220, "font": 12, "visible": True,  "label": "Referans No"},
        "movement_type": {"x": 180, "y": 240, "font": 12, "visible": True,  "label": "Mekanizma"},
        "condition":    {"x": 240, "y": 240, "font": 12, "visible": True,  "label": "Durum"},
        "price":        {"x": 220, "y": 260, "font": 25, "visible": True,  "label": "Fiyat"},
        "supplier":     {"x": 220, "y": 280, "font": 12, "visible": True,  "label": "Tedarikçi"},
        "box_papers":   {"x": 180, "y": 280, "font": 11, "visible": False, "label": "Kutu/Belge"},
        "label_width_percentage": 50,
        "barcode_lines_width": 70,
    }


def default_watch_large_config():
    return {
        "store_name":   {"x": 180, "y": 90,  "font": 22, "visible": True,  "label": "Firma Adı"},
        "barcode_no":   {"x": 180, "y": 145, "font": 22, "visible": True,  "label": "Barkod No"},
        "brand":        {"x": 180, "y": 175, "font": 20, "visible": True,  "label": "Marka"},
        "model_name":   {"x": 180, "y": 200, "font": 17, "visible": True,  "label": "Model"},
        "reference_no": {"x": 180, "y": 220, "font": 14, "visible": True,  "label": "Referans No"},
        "movement_type": {"x": 180, "y": 240, "font": 14, "visible": True,  "label": "Mekanizma"},
        "condition":    {"x": 240, "y": 240, "font": 14, "visible": True,  "label": "Durum"},
        "price":        {"x": 220, "y": 260, "font": 28, "visible": True,  "label": "Fiyat"},
        "supplier":     {"x": 220, "y": 280, "font": 14, "visible": True,  "label": "Tedarikçi"},
        "box_papers":   {"x": 180, "y": 280, "font": 13, "visible": False, "label": "Kutu/Belge"},
        "label_width_percentage": 50,
        "barcode_lines_width": 70,
    }


class StoreLabelSettings(models.Model):
    store = models.OneToOneField(Stores, on_delete=models.CASCADE, related_name='label_settings')

    ACTIVE_SIZE_CHOICES = [
        ('small', 'Küçük Etiket'),
        ('large', 'Büyük Etiket'),
    ]
    active_size = models.CharField(max_length=10, choices=ACTIVE_SIZE_CHOICES, default='small',
                                   verbose_name="Aktif Etiket Boyutu")

    small_design = models.JSONField(default=default_small_config, verbose_name="Küçük Etiket Tasarımı")

    large_design = models.JSONField(default=default_large_config, verbose_name="Büyük Etiket Tasarımı")

    # ─── ÇOKLU MADEN ETİKET TASARIMLARI ───
    # Pırlanta ve Saat material_type'larına özel JSON config alanları.
    # null=True → mevcut kayıtlar bozulmaz (zero-regression). View'da default fonksiyonlarıyla merge edilir.
    diamond_small_design = models.JSONField(
        default=default_diamond_small_config, null=True, blank=True,
        verbose_name="Pırlanta Küçük Etiket Tasarımı"
    )
    diamond_large_design = models.JSONField(
        default=default_diamond_large_config, null=True, blank=True,
        verbose_name="Pırlanta Büyük Etiket Tasarımı"
    )
    watch_small_design = models.JSONField(
        default=default_watch_small_config, null=True, blank=True,
        verbose_name="Saat Küçük Etiket Tasarımı"
    )
    watch_large_design = models.JSONField(
        default=default_watch_large_config, null=True, blank=True,
        verbose_name="Saat Büyük Etiket Tasarımı"
    )

    BOTTOM_LEFT_CHOICES = [
        ('MILYEM', 'Milyem'),
        ('MALIYET', 'Maliyet (Has)'),
    ]
    label_bottom_left_type = models.CharField(
        max_length=10, choices=BOTTOM_LEFT_CHOICES, default='MILYEM',
        verbose_name="Sol Alt Köşe Verisi"
    )

    LAYOUT_CHOICES = [
        ('STANDARD', 'Standart (Alt Alta)'),
        ('REFLECTED', 'Yansımalı (Kelebek)'),
        ('SIDE_BY_SIDE', 'Yan Yana (Çiftli)'),
    ]
    label_layout_mode = models.CharField(
        max_length=15, choices=LAYOUT_CHOICES, default='STANDARD',
        verbose_name="Etiket Düzeni"
    )

    # Barkod Çizgileri (Grafik) Koordinat ve Boyut Ayarları
    barcode_lines_x = models.IntegerField(default=200, verbose_name="Barkod Çizgileri X")
    barcode_lines_y = models.IntegerField(default=127, verbose_name="Barkod Çizgileri Y")
    barcode_lines_height = models.IntegerField(default=35, verbose_name="Barkod Çizgileri Yükseklik")
    barcode_lines_visible = models.BooleanField(default=True, verbose_name="Barkod Çizgileri Görünür")

    # RFID Yazıcı Modu — Varsayılan True (mevcut müşteriler RFID yazıcı kullanıyor).
    # Kapatıldığında ZPL çıktısından ^RFW komutu çıkarılır, standart (RFID'siz)
    # Zebra yazıcılarda void etiket / kilitlenme sorunu önlenir.
    rfid_mode = models.BooleanField(default=True, verbose_name="RFID Yazıcı Modu")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'StoreLabelSettings'
