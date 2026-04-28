from decimal import Decimal
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from apps.stores.models import Stores


class StoreConfiguration(models.Model):
    store = models.OneToOneField(Stores, on_delete=models.CASCADE, related_name='config')

    # --- 1. FİNANSAL AYARLAR ---
    price_margin_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('0.00'),
        validators=[MinValueValidator(Decimal('-50.00')), MaxValueValidator(Decimal('50.00'))],
        verbose_name="Fiyat Marjı (%)"
    )
    use_average_labor = models.BooleanField(default=False, verbose_name="Ortalama İşçilik Kullan")
    use_manual_has_calculation = models.BooleanField(
        default=False,
        verbose_name="Manuel Has Hesabı Kullan"
    )

    active_pricing_chamber = models.ForeignKey(
        'chambers.Chambers',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='stores_using_pricing',
        verbose_name="Referans Fiyat Derneği"
    )

    # --- 2. YASAL / UYUMLULUK (MASAK) ---
    enforce_cash_limit = models.BooleanField(default=True, verbose_name="Kısıtlama: 30.000 TL Üzeri Nakit Yasak")

    # --- FAZ 18: ONAYLI KASA (Safe Approval) ---
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

    # 36.000 TL üzeri Müşteri Seçimi Zorunluluğu (Fatura Limiti)
    enforce_invoice_customer = models.BooleanField(default=False,
                                                   verbose_name="Zorunluluk: 36.000 TL Üzeri Müşteri Seçimi")

    # 185.000 TL üzeri Kimlik/TCKN Zorunluluğu (MASAK)
    enforce_masak_identity = models.BooleanField(default=False,
                                                 verbose_name="Zorunluluk: 185.000 TL Üzeri Kimlik Bilgisi")

    # --- MÜŞTERİ ZORUNLULUK AYARLARI (Validasyon Senkronizasyonu Fazı) ---
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

    # Müşteri kayıt formunda TC/VKN zorunluluğu.
    require_customer_tckn = models.BooleanField(
        default=False,
        verbose_name="Müşteri Kayıt: T.C. / Vergi Kimlik Zorunlu",
        help_text="Yeni müşteri kaydında ve güncellemede kimlik numarası zorunlu olsun.",
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
    return {
        "store_name":     {"x": 180, "y": 90,  "font": 20, "visible": True,  "label": "Firma Adı"},
        "barcode_no":     {"x": 180, "y": 145, "font": 20, "visible": True,  "label": "Barkod No"},
        "carat_weight":   {"x": 180, "y": 175, "font": 18, "visible": True,  "label": "Karat (ct)"},
        "color_grade":    {"x": 240, "y": 175, "font": 15, "visible": True,  "label": "Renk"},
        "clarity_grade":  {"x": 290, "y": 175, "font": 15, "visible": True,  "label": "Berraklık"},
        "cut_grade":      {"x": 180, "y": 200, "font": 13, "visible": True,  "label": "Kesim"},
        "mount_karat":    {"x": 320, "y": 200, "font": 13, "visible": False, "label": "Montür Ayarı"},
        "mount_gram":     {"x": 380, "y": 200, "font": 13, "visible": False, "label": "Montür Gramı"},
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
