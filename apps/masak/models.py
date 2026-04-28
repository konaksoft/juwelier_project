import uuid
from django.db import models
from apps.stores.models import Stores
from apps.accounts.models import Users
from apps.customers.models import Customers


# --- MEVCUT MODELLER ---
class MasakBlacklist(models.Model):
    # (Buraya dokunmuyoruz, mağazanın özel listesi aynen kalıyor)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='masak_blacklist')
    full_name = models.CharField(max_length=255, verbose_name="Ad Soyad")
    identification_number = models.CharField(max_length=50, verbose_name="TCKN/VKN/Pasaport", db_index=True)
    reason = models.TextField(verbose_name="Sebep", null=True, blank=True)
    created_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'MasakBlacklist'
        unique_together = ('store', 'identification_number')


# --- YENİ EKLENEN RESMİ LİSTE MODELİ ---
class MasakOfficialList(models.Model):
    """
    MASAK veya BMGK tarafından yayınlanan resmi listeler.
    Bu tablo tüm mağazalar için ortaktır ve merkezi olarak güncellenir.
    """
    LIST_SOURCES = [
        ('BMGK', 'BMGK Kararları (5. Madde)'),
        ('FOREIGN', 'Yabancı Ülke Talepleri (6. Madde)'),
        ('INTERNAL', 'İç Dondurma Kararları (7. Madde/FETÖ/PKK vb.)'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Kimlik No (TCKN, VKN, Pasaport). Eşleşme için en kritik alan.
    # CSV'lerde bazen birden fazla numara tek hücrede oluyor, bu yüzden CharField geniş tutuldu.
    identity_info = models.TextField(verbose_name="Kimlik Bilgileri", db_index=True)

    full_name = models.CharField(max_length=500, verbose_name="Ad Soyad / Unvan", db_index=True)

    # Hangi listeden geldiği
    source_type = models.CharField(max_length=20, choices=LIST_SOURCES, default='INTERNAL')

    # Örgüt bilgisi (FETÖ, DEAŞ vb.)
    organization = models.CharField(max_length=255, null=True, blank=True, verbose_name="Bağlantılı Örgüt")

    birth_date = models.CharField(max_length=100, null=True, blank=True, verbose_name="Doğum Tarihi")
    nationality = models.CharField(max_length=100, null=True, blank=True, verbose_name="Uyruk")

    gazette_date = models.CharField(max_length=100, null=True, blank=True, verbose_name="Resmi Gazete Tarihi")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'MasakOfficialList'
        verbose_name = 'MASAK Resmi Liste'
        indexes = [
            models.Index(fields=['identity_info']),
            models.Index(fields=['full_name']),
        ]

    def __str__(self):
        return f"{self.full_name} - {self.organization}"


# --- LOG MODELİ (AYNEN KALIYOR) ---
class MasakQueryLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    store = models.ForeignKey(Stores, on_delete=models.CASCADE, related_name='masak_logs')
    performed_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True)
    customer = models.ForeignKey(Customers, on_delete=models.SET_NULL, null=True, blank=True)
    queried_tckn = models.CharField(max_length=50, null=True, blank=True)
    queried_name = models.CharField(max_length=255, null=True, blank=True)

    result_status = models.CharField(
        max_length=20,
        choices=[('CLEAN', 'Temiz'), ('SUSPICIOUS', 'Şüpheli')],
        default='CLEAN'
    )
    result_message = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'MasakQueryLog'
        ordering = ['-created_at']


# =====================================================================
# MASAK Müşteri Tanı (KYC) Beyan Formu
# =====================================================================
class CustomerMasakDeclaration(models.Model):
    """
    Müşterinin QR ile doldurduğu MASAK Müşteri Bilgi Beyan Formu.
    Hem Bireysel hem Kurumsal müşteri tiplerini destekler.
    """

    CUSTOMER_TYPE_CHOICES = (
        ('BIREYSEL', 'Bireysel'),
        ('KURUMSAL', 'Kurumsal'),
    )

    DOCUMENT_TYPE_CHOICES = (
        ('TC', 'T.C. Kimlik Kartı'),
        ('PASAPORT', 'Pasaport'),
        ('EHLIYET', 'Sürücü Belgesi'),
        ('DIGER', 'Diğer'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    customer = models.OneToOneField(
        Customers,
        on_delete=models.CASCADE,
        related_name='masak_declaration',
        verbose_name='Müşteri'
    )
    store = models.ForeignKey(
        Stores,
        on_delete=models.CASCADE,
        related_name='masak_declarations',
        verbose_name='Mağaza'
    )

    # Müşteri tipi
    customer_type = models.CharField(
        'Müşteri Tipi',
        max_length=10,
        choices=CUSTOMER_TYPE_CHOICES,
        default='BIREYSEL',
    )

    # --- Bireysel: Kimlik ---
    first_name = models.CharField('Adı', max_length=100, null=True, blank=True)
    last_name = models.CharField('Soyadı', max_length=100, null=True, blank=True)
    identity_number = models.CharField('Kimlik/Pasaport No', max_length=30, null=True, blank=True)
    nationality = models.CharField('Uyruğu', max_length=80, null=True, blank=True)
    document_type = models.CharField('Belge Türü', max_length=20, choices=DOCUMENT_TYPE_CHOICES, default='TC', null=True, blank=True)
    document_number = models.CharField('Belge Numarası', max_length=50, null=True, blank=True)

    # --- Bireysel: Doğum / iletişim / meslek ---
    birth_place = models.CharField('Doğum Yeri', max_length=120, null=True, blank=True)
    birth_date = models.DateField('Doğum Tarihi', null=True, blank=True)
    address = models.TextField('Adres', null=True, blank=True)
    email = models.EmailField('E-posta', null=True, blank=True)
    phone = models.CharField('Telefon', max_length=25, null=True, blank=True)
    occupation = models.CharField('İş/Meslek', max_length=150, null=True, blank=True)

    # --- Bireysel: Aile ---
    mother_name = models.CharField('Anne Adı', max_length=100, null=True, blank=True)
    father_name = models.CharField('Baba Adı', max_length=100, null=True, blank=True)

    # --- Bireysel: Medya ---
    id_front_image = models.ImageField('Kimlik Ön Yüz', upload_to='masak/id_front/%Y/%m/', null=True, blank=True)
    id_back_image = models.ImageField('Kimlik Arka Yüz', upload_to='masak/id_back/%Y/%m/', null=True, blank=True)

    # --- Kurumsal: Şirket ---
    company_title = models.CharField('Şirket Unvanı', max_length=250, null=True, blank=True)
    tax_office = models.CharField('Vergi Dairesi', max_length=150, null=True, blank=True)
    tax_number = models.CharField('Vergi Numarası (VKN)', max_length=20, null=True, blank=True)
    mersis_number = models.CharField('MERSİS Numarası', max_length=30, null=True, blank=True)
    trade_registry_number = models.CharField('Ticaret Sicil Numarası', max_length=30, null=True, blank=True)
    activity_field = models.CharField('Faaliyet Konusu', max_length=250, null=True, blank=True)
    company_address = models.TextField('Şirket Merkez Adresi', null=True, blank=True)

    # --- Kurumsal: Yetkili Temsilci ---
    rep_first_name = models.CharField('Yetkili Adı', max_length=120, null=True, blank=True)
    rep_last_name = models.CharField('Yetkili Soyadı', max_length=120, null=True, blank=True)
    rep_identity_number = models.CharField('Yetkili TCKN', max_length=30, null=True, blank=True)
    rep_title = models.CharField('Yetkili Ünvanı', max_length=150, null=True, blank=True)

    # --- Kurumsal: Gerçek Faydalanıcı / Ortak ---
    beneficial_owner_name = models.CharField('Gerçek Faydalanıcı Adı', max_length=250, null=True, blank=True)
    beneficial_owner_identity = models.CharField('Gerçek Faydalanıcı TCKN', max_length=30, null=True, blank=True)
    beneficial_owner_share = models.CharField('Ortaklık Payı', max_length=50, null=True, blank=True)

    # İzinler
    consent_kvkk = models.BooleanField('KVKK Onayı', default=False)
    consent_acik_riza = models.BooleanField('Açık Rıza Onayı', default=False)
    consent_iys_sms = models.BooleanField('İYS SMS', default=False)
    consent_iys_email = models.BooleanField('İYS E-posta', default=False)
    consent_iys_call = models.BooleanField('İYS Arama', default=False)
    consent_timestamp = models.DateTimeField('Onay Zamanı', auto_now_add=True)

    # Audit
    submitted_at = models.DateTimeField('Gönderim', auto_now_add=True)
    updated_at = models.DateTimeField('Güncelleme', auto_now=True)
    ip_address = models.GenericIPAddressField('IP', null=True, blank=True)
    user_agent = models.TextField('User Agent', blank=True, default='')

    class Meta:
        db_table = 'CustomerMasakDeclaration'
        verbose_name = 'MASAK Beyan Formu'
        verbose_name_plural = 'MASAK Beyan Formları'
        ordering = ('-submitted_at',)

    def __str__(self):
        if self.customer_type == 'KURUMSAL':
            return f"MASAK - {self.company_title or '-'} (VKN: {self.tax_number or '-'})"
        return f"MASAK - {(self.first_name or '').strip()} {(self.last_name or '').strip()}".strip()

    @property
    def full_name(self):
        if self.customer_type == 'KURUMSAL':
            return self.company_title or ''
        return f"{self.first_name or ''} {self.last_name or ''}".strip()

    @property
    def display_name(self):
        if self.customer_type == 'KURUMSAL':
            return self.company_title or ''
        return f"{self.first_name or ''} {self.last_name or ''}".strip()

    @property
    def display_identity(self):
        if self.customer_type == 'KURUMSAL':
            return self.tax_number or ''
        return self.identity_number or ''