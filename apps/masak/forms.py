from datetime import date
import re
from django import forms
from .models import CustomerMasakDeclaration


class CustomerMasakPublicForm(forms.Form):
    """QR ile açılan auth'suz public MASAK formu (Bireysel + Kurumsal)."""

    customer_type = forms.ChoiceField(
        label='Müşteri Tipi',
        choices=CustomerMasakDeclaration.CUSTOMER_TYPE_CHOICES,
        initial='BIREYSEL',
        widget=forms.RadioSelect(),
    )

    # ---------------- BIREYSEL ----------------
    first_name = forms.CharField(label='Adı', max_length=100, required=False)
    last_name = forms.CharField(label='Soyadı', max_length=100, required=False)
    identity_number = forms.CharField(label='Kimlik/Pasaport No', max_length=30, required=False)
    nationality = forms.CharField(label='Uyruk', max_length=80, initial='T.C.', required=False)
    document_type = forms.ChoiceField(
        label='Belge Türü',
        choices=CustomerMasakDeclaration.DOCUMENT_TYPE_CHOICES,
        required=False,
    )
    document_number = forms.CharField(label='Belge No', max_length=50, required=False)
    birth_place = forms.CharField(label='Doğum Yeri', max_length=120, required=False)
    birth_date = forms.DateField(
        label='Doğum Tarihi',
        required=False,
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    address = forms.CharField(
        label='Adres',
        required=False,
        widget=forms.Textarea(attrs={'rows': 2}),
    )
    occupation = forms.CharField(label='İş / Meslek', max_length=150, required=False)
    mother_name = forms.CharField(label='Anne Adı', max_length=100, required=False)
    father_name = forms.CharField(label='Baba Adı', max_length=100, required=False)

    # ---------------- KURUMSAL ----------------
    company_title = forms.CharField(label='Şirket Unvanı', max_length=250, required=False)
    tax_office = forms.CharField(label='Vergi Dairesi', max_length=150, required=False)
    tax_number = forms.CharField(label='Vergi Numarası (VKN)', max_length=20, required=False)
    mersis_number = forms.CharField(label='MERSİS No', max_length=30, required=False)
    trade_registry_number = forms.CharField(label='Ticaret Sicil No', max_length=30, required=False)
    activity_field = forms.CharField(label='Faaliyet Konusu', max_length=250, required=False)
    company_address = forms.CharField(
        label='Şirket Merkez Adresi',
        required=False,
        widget=forms.Textarea(attrs={'rows': 2}),
    )
    rep_first_name = forms.CharField(label='Yetkili Adı', max_length=120, required=False)
    rep_last_name = forms.CharField(label='Yetkili Soyadı', max_length=120, required=False)
    rep_identity_number = forms.CharField(label='Yetkili TCKN', max_length=30, required=False)
    rep_title = forms.CharField(label='Yetkili Ünvanı', max_length=150, required=False)
    beneficial_owner_name = forms.CharField(label='Gerçek Faydalanıcı Adı Soyadı', max_length=250, required=False)
    beneficial_owner_identity = forms.CharField(label='Gerçek Faydalanıcı TCKN', max_length=30, required=False)
    beneficial_owner_share = forms.CharField(label='Ortaklık Payı (%)', max_length=50, required=False)

    # ---------------- ORTAK İLETİŞİM ----------------
    email = forms.EmailField(label='E-posta', required=False)
    phone = forms.CharField(label='Telefon', max_length=25, required=False)

    # ---------------- MEDYA (BIREYSEL) ----------------
    id_front_image_data = forms.CharField(required=False, widget=forms.HiddenInput())
    id_front_image_file = forms.ImageField(required=False)
    id_back_image_data = forms.CharField(required=False, widget=forms.HiddenInput())
    id_back_image_file = forms.ImageField(required=False)

    # Hidden base64 aliases (public form)
    id_front_base64 = forms.CharField(required=False, widget=forms.HiddenInput())
    id_back_base64 = forms.CharField(required=False, widget=forms.HiddenInput())

    # ---------------- İZİNLER ----------------
    consent_kvkk = forms.BooleanField(
        label='KVKK Aydınlatma Metnini okudum ve anladım.', required=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    consent_acik_riza = forms.BooleanField(
        label='Açık Rıza Metnini kabul ediyorum.', required=True,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    consent_iys_sms = forms.BooleanField(
        label='SMS ile kampanya/bilgilendirme almak istiyorum.', required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    consent_iys_email = forms.BooleanField(
        label='E-posta ile kampanya/bilgilendirme almak istiyorum.', required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))
    consent_iys_call = forms.BooleanField(
        label='Arama ile kampanya/bilgilendirme almak istiyorum.', required=False,
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.HiddenInput, forms.RadioSelect,
                                   forms.CheckboxSelectMultiple, forms.FileInput, forms.ClearableFileInput)):
                continue
            existing = widget.attrs.get('class', '')
            if isinstance(widget, forms.Select):
                css = 'form-select form-select-lg'
            else:
                css = 'form-control form-control-lg'
            widget.attrs['class'] = (existing + ' ' + css).strip()

    # ---- Yardımcı: TCKN checksum ----
    @staticmethod
    def _validate_tckn(tckn):
        if not tckn or len(tckn) != 11 or not tckn.isdigit() or tckn[0] == '0':
            return False
        d = [int(x) for x in tckn]
        tenth = (sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10
        eleventh = sum(d[:10]) % 10
        return tenth == d[9] and eleventh == d[10]

    @staticmethod
    def _validate_vkn(vkn):
        return bool(vkn) and vkn.isdigit() and len(vkn) == 10

    def clean_phone(self):
        value = (self.cleaned_data.get('phone') or '').strip()
        if value:
            digits = re.sub(r'\D', '', value)
            if len(digits) < 10:
                raise forms.ValidationError('Geçerli bir telefon numarası giriniz.')
        return value

    def clean_birth_date(self):
        value = self.cleaned_data.get('birth_date')
        if value and value > date.today():
            raise forms.ValidationError('Doğum tarihi gelecekte olamaz.')
        if value and value.year < 1900:
            raise forms.ValidationError('Geçerli bir doğum tarihi giriniz.')
        return value

    def clean(self):
        cleaned = super().clean()
        ctype = cleaned.get('customer_type') or 'BIREYSEL'

        if ctype == 'BIREYSEL':
            required_fields = {
                'first_name': 'Ad zorunludur.',
                'last_name': 'Soyad zorunludur.',
                'identity_number': 'Kimlik/Pasaport numarası zorunludur.',
                'birth_place': 'Doğum yeri zorunludur.',
                'birth_date': 'Doğum tarihi zorunludur.',
                'address': 'Adres zorunludur.',
                'phone': 'Telefon zorunludur.',
            }
            for f, msg in required_fields.items():
                if not cleaned.get(f):
                    self.add_error(f, msg)

            idn = (cleaned.get('identity_number') or '').strip()
            if idn and idn.isdigit() and len(idn) == 11:
                if not self._validate_tckn(idn):
                    self.add_error('identity_number', 'Geçersiz T.C. Kimlik Numarası.')
            elif idn and len(idn) < 6:
                self.add_error('identity_number', 'Kimlik/Pasaport numarası en az 6 karakter olmalıdır.')

        elif ctype == 'KURUMSAL':
            required_fields = {
                'company_title': 'Şirket unvanı zorunludur.',
                'tax_office': 'Vergi dairesi zorunludur.',
                'tax_number': 'Vergi numarası (VKN) zorunludur.',
                'company_address': 'Şirket adresi zorunludur.',
                'phone': 'Telefon zorunludur.',
                'rep_first_name': 'Yetkili adı zorunludur.',
                'rep_last_name': 'Yetkili soyadı zorunludur.',
                'rep_identity_number': 'Yetkili TCKN zorunludur.',
                'rep_title': 'Yetkili ünvanı zorunludur.',
            }
            for f, msg in required_fields.items():
                if not cleaned.get(f):
                    self.add_error(f, msg)

            vkn = (cleaned.get('tax_number') or '').strip()
            if vkn and not self._validate_vkn(vkn):
                self.add_error('tax_number', 'VKN 10 haneli rakamlardan oluşmalıdır.')

            rep_tckn = (cleaned.get('rep_identity_number') or '').strip()
            if rep_tckn and not self._validate_tckn(rep_tckn):
                self.add_error('rep_identity_number', 'Geçersiz yetkili T.C. Kimlik Numarası.')

        if not cleaned.get('consent_kvkk'):
            self.add_error('consent_kvkk', 'KVKK onayı zorunludur.')
        if not cleaned.get('consent_acik_riza'):
            self.add_error('consent_acik_riza', 'Açık rıza onayı zorunludur.')

        return cleaned
