# ============================================================================
# DOSYA: apps/banking/services.py
# KONUM: Kuyum Plus (jewelery_project)
#
# REVİZYON (v5 — 2026-03-25):
#   - MİMARİ DEĞİŞİKLİK: e-Süreç artık MySoft'a proxy YAPMAZ.
#     ExternalBankingTransactionListView, e-Süreç'in lokal BankTransaction
#     tablosunu doğrudan sorgulayıp camelCase JSON olarak döner.
#     Kuyum Plus tarafında alan eşlemesi (field mapping) DEĞİŞMEDİ —
#     e-Süreç aynı camelCase formatını koruyor, fark şeffaftır.
#   - ESurecBankingClient.fetch_transactions(): Aynı endpoint'i çağırır,
#     ancak yanıtta gelen veri artık MySoft raw değil, e-Süreç lokal DB'den.
#
# ÖNCEKİ (v4 — 2026-03-20):
#   - ESurecClient._request() extra_headers desteği
#   - _parse_response() kaldırıldı — merkezi _request() bunu yapıyor
#
# İÇERİK:
#   1. ESurecBankingClient   — e-Süreç lokal banking verisini çeker
#   2. CariMatchingService   — Banka hareketi ↔ Cari (Customers) eşleştirmesi
#   3. InvoiceAutoService    — Eşleştirilen hareketten otomatik e-fatura oluşturur
#   4. PaymentStatusService  — Kısmi/Fazla ödeme durumu yönetimi
#   5. EsurecHealthCheckService — e-Süreç Tenant durum sorgulama (FAZ 2)
#   6. EsurecProvisioningService — Otomatik Token Üretimi (Automated Provisioning)
# ============================================================================

import logging
import re
import unicodedata
from decimal import Decimal
from typing import Optional

from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.banking.models import BankTransaction, BankAccount
from apps.customers.models import Customers
# ESurecClient ve Invoice kaldırıldı — invoices app Juwelier Plus'ta yok

log = logging.getLogger(__name__)


# ============================================================================
# ÖDEME ↔ BANKA HESABI VALİDASYONU (Faz 2: Mutabakat Altyapısı)
# ============================================================================

class PaymentBankAccountValidator:
    """
    Ödeme tipi (payment_type) ile banka hesabı (BankAccount) uyumunu doğrular.
    View katmanından Payment.objects.create() öncesi çağrılır.

    Kurallar:
        - CREDIT_CARD → BankAccount.AccountType.POS zorunlu
        - TRANSFER    → BankAccount.AccountType.BANK zorunlu
        - CASH        → bank_account gerekmez, None döner
        - COMMISSION  → bank_account gerekmez, None döner

    Kullanım (view katmanında):
        from apps.banking.services import PaymentBankAccountValidator

        bank_account = PaymentBankAccountValidator.validate(
            payment_type='CREDIT_CARD',
            bank_account_id=request.POST.get('bank_account_card'),
            store=request.user.store,
        )
        # ValidationError fırlatmazsa güvenle Payment.objects.create(bank_account=bank_account, ...)
    """

    # payment_type → beklenen BankAccount.account_type eşlemesi
    REQUIRED_ACCOUNT_TYPES = {
        'CASH':        BankAccount.AccountType.CASH,
        'CREDIT_CARD': BankAccount.AccountType.POS,
        'TRANSFER':    BankAccount.AccountType.BANK,
    }

    # Kullanıcı dostu hata mesajları
    _TYPE_LABELS = {
        'CASH':        'Nakit',
        'CREDIT_CARD': 'Kredi Kartı',
        'TRANSFER':    'Havale / EFT',
    }

    @classmethod
    def validate(cls, payment_type, bank_account_id, store):
        """
        Ödeme tipine göre banka hesabını doğrular.

        Args:
            payment_type: 'CASH', 'CREDIT_CARD', 'TRANSFER', 'COMMISSION'
            bank_account_id: UUID string veya None
            store: Stores model instance (mağaza)

        Returns:
            BankAccount instance (CREDIT_CARD/TRANSFER için) veya None (CASH/COMMISSION için)

        Raises:
            django.core.exceptions.ValidationError: Hesap eksik, bulunamadı veya tip uyumsuz
        """
        from django.core.exceptions import ValidationError

        # Nakit ve komisyon için banka hesabı gerekmez
        if payment_type not in cls.REQUIRED_ACCOUNT_TYPES:
            return None

        expected_type = cls.REQUIRED_ACCOUNT_TYPES[payment_type]
        type_label = cls._TYPE_LABELS.get(payment_type, payment_type)

        # Hesap ID'si zorunlu kontrolü
        if not bank_account_id:
            raise ValidationError(
                f"'{type_label}' ödemesi için banka/POS hesabı seçimi zorunludur."
            )

        # Veritabanından doğrulama
        try:
            account = BankAccount.objects.get(
                id=bank_account_id,
                store=store,
                is_active=True,
                is_deleted=False,
            )
        except BankAccount.DoesNotExist:
            raise ValidationError(
                f"Seçilen banka hesabı bulunamadı veya aktif değil."
            )

        # Tip uyumu kontrolü
        if account.account_type != expected_type:
            expected_label = account.get_account_type_display() if account.account_type else '?'
            raise ValidationError(
                f"'{type_label}' ödemesi için '{expected_type}' tipinde bir hesap gerekli, "
                f"ancak seçilen hesap '{expected_label}' tipinde."
            )

        return account

    @classmethod
    def validate_multiple(cls, payments_data, store):
        """
        Parçalı ödeme senaryosu için birden fazla ödemeyi toplu doğrular.

        Args:
            payments_data: [
                {'type': 'CASH', 'amount': Decimal('5000'), 'bank_account_id': None},
                {'type': 'CREDIT_CARD', 'amount': Decimal('3000'), 'bank_account_id': 'uuid-str'},
                {'type': 'TRANSFER', 'amount': Decimal('2000'), 'bank_account_id': 'uuid-str'},
            ]
            store: Stores model instance

        Returns:
            [
                {'type': 'CASH', 'amount': ..., 'bank_account': None},
                {'type': 'CREDIT_CARD', 'amount': ..., 'bank_account': <BankAccount>},
                {'type': 'TRANSFER', 'amount': ..., 'bank_account': <BankAccount>},
            ]

        Raises:
            django.core.exceptions.ValidationError
        """
        result = []
        for pmt in payments_data:
            pmt_type = pmt.get('type', '')
            amount = pmt.get('amount', Decimal('0'))
            ba_id = pmt.get('bank_account_id')

            if amount <= 0:
                continue

            bank_account = cls.validate(
                payment_type=pmt_type,
                bank_account_id=ba_id,
                store=store,
            )
            result.append({
                'type': pmt_type,
                'amount': amount,
                'bank_account': bank_account,
            })

        return result


# ============================================================================
# YARDIMCI: Timezone-aware datetime dönüştürücü
# ============================================================================

def _parse_aware_dt(value):
    """
    Mysoft API'den gelen ham tarih değerini Django'nun beklediği
    timezone-aware datetime nesnesine çevirir.

    Kabul ettiği girdiler:
      - None / boş string → None döner
      - datetime nesnesi (naive) → timezone.make_aware() ile aware yapar
      - datetime nesnesi (aware) → olduğu gibi döner
      - ISO 8601 string ("2024-01-15T10:30:00") → parse edip aware yapar
      - Parse edilemeyen string → None döner (hata loglanır)
    """
    if not value:
        return None

    # Zaten datetime nesnesi ise
    if hasattr(value, 'tzinfo'):
        if value.tzinfo is None:
            return timezone.make_aware(value)
        return value

    # String → datetime parse
    dt = parse_datetime(str(value))
    if dt is not None:
        if dt.tzinfo is None:
            dt = timezone.make_aware(dt)
        return dt

    # Son çare: sadece tarih kısmını almayı dene (YYYY-MM-DD)
    from django.utils.dateparse import parse_date
    d = parse_date(str(value)[:10])
    if d is not None:
        from datetime import datetime as _dt
        naive = _dt.combine(d, _dt.min.time())
        return timezone.make_aware(naive)

    log.warning("[Banking] Tarih parse edilemedi: %r", value)
    return None


# ============================================================================
# 1. ESurecBankingClient
# ============================================================================

class ESurecBankingClient:
    """
    e-Süreç banking endpointlerini çağırır.
    Kimlik doğrulama ESurecClient'taki HMAC-SHA256 mekanizmasıyla yapılır.

    Veri Akışı (MİMARİ v6):
      Kuyum Plus → e-Süreç (HMAC auth) → e-Süreç LOKAL DB → Kuyum Plus
      e-Süreç, kendi BankingIntegrationService.sync_transactions() ile
      MySoft'tan çekip lokal DB'ye kaydettiği verileri camelCase JSON olarak döner.
      Kuyum Plus burada gelen veriyi parse edip kendi DB'sine upsert yapar.
      MySoft'a doğrudan erişim YOKTUR — tüm veri e-Süreç lokal DB'den gelir.

    Güvenlik:
      Banking endpoint'leri SecureExternalView (Katman 1 HMAC + Katman 2 Token) kullanır.
      Bu yüzden her istekte X-Tenant-Token header'ı gönderilmelidir.
      Token, EsurecTenantCredential modelinden okunarak extra_headers ile eklenir.

    Endpoint'ler (e-Süreç'te tanımlı):
      POST /api/v1/external/banking/transactions/
        Response: { success: true, count: N, transactions: [{id, docNo, ...}, ...] }
      POST /api/v1/external/banking/mark-read/
    """

    def __init__(self):
        self._client = ESurecClient()

    def _get_seller_vkn(self, store) -> str:
        """Mağazanın vergi numarasını döner."""
        if store:
            company = getattr(store, 'company', None)
            if company:
                return str(company.tax_number or '')
        return ''

    def _get_tenant_token(self, store) -> str:
        """
        Mağazanın aktif EsurecTenantCredential kaydından Katman 2 token'ını okur.
        Token yoksa veya çözülemezse '' döner (exception fırlatmaz).
        """
        from apps.banking.models import EsurecTenantCredential

        cred = EsurecTenantCredential.objects.filter(
            store=store, is_active=True,
        ).first()
        if not cred:
            log.warning(
                "[Banking] Tenant token bulunamadı: store=%s — aktif credential yok.",
                store.id,
            )
            return ''
        raw_token = cred.tenant_token
        if not raw_token:
            log.warning(
                "[Banking] Tenant token çözülemedi: store=%s — Fernet hatası olabilir.",
                store.id,
            )
        return raw_token

    def fetch_transactions(self, store, start_date: str, end_date: str,
                           iban: str = '', only_new: bool = False) -> dict:
        """
        e-Süreç'in lokal BankTransaction tablosundan banka hareketlerini çeker
        ve Kuyum Plus DB'ye (BankTransaction) upsert yapar.

        NOT: e-Süreç artık MySoft'a proxy YAPMAZ. Kendi DB'sindeki verileri
        camelCase JSON olarak döner. Alan eşlemesi aynı kalmıştır.

        HATA YÖNETİMİ (v6):
          - e-Süreç başarı yanıtı: {"success": true, "count": N, "transactions": [...]}
          - e-Süreç hata yanıtı:  {"success": false, "error": {"message": "..."}}
          - ESurecClient._request() HTTP hatalarında: {"result": false, "error_msg": "..."}
          - Her iki format da desteklenir; token eksikliği veya bağlantı hatası durumunda
            anlamlı hata mesajı döner (retryable bilgisi dahil).

        Döner: {
            'result': bool,
            'msg': str,
            'count': int,   # Kaç kayıt DB'ye yazıldı / güncellendi
        }
        """
        seller_vkn = self._get_seller_vkn(store)
        raw_token = self._get_tenant_token(store)

        # ── Ön koşul: Tenant token yoksa API çağrısı yapma ──
        if not raw_token:
            return {
                'result': False,
                'msg': (
                    'Bu mağaza için e-Süreç açık bankacılık token\'ı bulunamadı. '
                    'Lütfen e-Süreç aktivasyonunu kontrol edin.'
                ),
                'count': 0,
                'retryable': False,
                'error_type': 'integration_inactive',
            }

        payload = {
            'seller_vkn': seller_vkn,
            'start_date': start_date,
            'end_date': end_date,
            'iban': iban,
            'only_new': only_new,
        }

        try:
            resp = self._client._request(
                'POST', '/api/v1/external/banking/transactions/', payload,
                extra_headers={'X-Tenant-Token': raw_token},
            )
        except Exception as e:
            log.error(f"[Banking] fetch_transactions _request exception: {e}")
            return {
                'result': False,
                'msg': 'e-Süreç banka servisi ile bağlantı kurulamadı. Lütfen daha sonra tekrar deneyin.',
                'count': 0,
                'retryable': True,
                'error_type': 'connection_error',
            }

        if not resp:
            return {
                'result': False,
                'msg': 'e-Süreç banking API yanıt vermedi.',
                'count': 0,
                'retryable': True,
                'error_type': 'connection_error',
            }

        # ── Yanıt format tespiti ──
        # e-Süreç başarı yanıtı: {"success": true, "count": N, "transactions": [...]}
        # e-Süreç hata yanıtı:  {"success": false, "error": {"message": "..."}}
        # ESurecClient._request() HTTP hatalarında: {"result": false, "error_msg": "...", "retryable": ...}
        # Her iki formatı da destekle:
        is_success = resp.get('success', False) or resp.get('result', False)
        is_http_error = (resp.get('http_status', 0) >= 400)

        # HTTP hata durumu — _request() wrapper formatı
        if is_http_error or (not is_success and 'error_msg' in resp):
            error_msg = (
                resp.get('error_msg', '')
                or (resp.get('error', {}).get('message', '') if isinstance(resp.get('error'), dict) else '')
                or resp.get('message', '')
                or 'Banka hareketi çekilemedi.'
            )
            retryable = resp.get('retryable', False)
            http_status = resp.get('http_status', 0)

            log.warning(
                f"[Banking] fetch_transactions hata: HTTP {http_status}, "
                f"retryable={retryable}, msg={error_msg[:200]}"
            )

            # HTTP 401/403 → kimlik doğrulama / yetki hatası → entegrasyon aktif değil
            # Diğer HTTP hataları → bağlantı / sunucu hatası
            if http_status in (401, 403):
                _error_type = 'integration_inactive'
            else:
                _error_type = 'connection_error'

            return {
                'result': False,
                'msg': error_msg,
                'count': 0,
                'retryable': retryable,
                'error_type': _error_type,
            }

        # e-Süreç JSON hata formatı — success: false
        if not is_success:
            error_msg = (
                (resp.get('error', {}).get('message', '') if isinstance(resp.get('error'), dict) else '')
                or resp.get('message', '')
                or resp.get('error_msg', '')
                or 'Banka hareketi çekilemedi.'
            )

            # Hata mesajından entegrasyon / yetki / paket sorununu tespit et
            _lower_msg = error_msg.lower()
            _integration_keywords = [
                'entegrasyon', 'integration', 'aktif değil', 'paket',
                'yetki', 'permission', 'token', 'credential',
                'açık bankacılık', 'open banking', 'banka bilgisi eksik',
                'lisanssız', 'license', 'unauthorized',
            ]
            if any(kw in _lower_msg for kw in _integration_keywords):
                _error_type = 'integration_inactive'
            else:
                _error_type = 'api_error'

            return {
                'result': False,
                'msg': error_msg,
                'count': 0,
                'retryable': False,
                'error_type': _error_type,
            }

        # ─── e-Süreç lokal DB'den gelen transaction listesini Kuyum Plus DB'ye upsert yap ───
        transactions = resp.get('transactions', [])
        if not transactions:
            return {
                'result': True,
                'msg': 'Seçilen tarih aralığında yeni banka hareketi bulunamadı.',
                'count': 0,
                'error_type': 'no_data',
            }

        synced = 0
        errors = 0
        try:
            with db_transaction.atomic():
                for txn in transactions:
                    api_id = txn.get('id')
                    if not api_id:
                        continue

                    # FAZ A.1 / GAP-07 — e-Süreç UUID'sini parse et
                    # esurec_id eski sürüm yanıtlarda olmayabilir; UUID
                    # formatı bozuksa sessizce None bırakılır (alan nullable).
                    raw_esurec_id = txn.get('esurec_id') or txn.get('esurecId')
                    parsed_esurec_uuid = None
                    if raw_esurec_id:
                        try:
                            import uuid as _uuid_mod
                            parsed_esurec_uuid = _uuid_mod.UUID(str(raw_esurec_id))
                        except (ValueError, AttributeError, TypeError):
                            log.warning(
                                "[Banking] esurec_id parse edilemedi (api_id=%s, raw=%r)",
                                api_id, raw_esurec_id,
                            )
                            parsed_esurec_uuid = None

                    try:
                        BankTransaction.objects.update_or_create(
                            store=store,
                            api_transaction_id=api_id,
                            defaults={
                                'doc_no':                   txn.get('docNo') or '',
                                'iban':                     txn.get('iban') or '',
                                'api_created_date':         _parse_aware_dt(txn.get('createdDate')),
                                'account_no':               txn.get('accountNo') or '',
                                'account_name':             txn.get('accountName') or '',
                                'bank_name':                txn.get('bankName') or '',
                                'bank_branch_code':         txn.get('bankBranchCode') or '',
                                'bank_branch_name':         txn.get('bankBranchName') or '',
                                'currency_code':            txn.get('currencyCode') or 'TRY',
                                'balance':                  txn.get('balance', 0.0),
                                'doc_date':                 _parse_aware_dt(txn.get('docDate')),
                                'reference':                txn.get('reference') or '',
                                'plus_minus':               txn.get('plusMinus', 1),
                                'amount':                   txn.get('amt', 0.0),
                                'current_balance':          txn.get('currentBalance', 0.0),
                                'note':                     txn.get('note') or '',
                                'other_iban':               txn.get('otherIBAN') or '',
                                'other_vkn_tckn':           txn.get('otherVknTckn') or '',
                                'other_name':               txn.get('otherName') or '',
                                'bank_transaction_code':    txn.get('bankTransactionCode') or '',
                                'bank_transaction_desc':    txn.get('bankTransactionDesc') or '',
                                'mysoft_transaction_type':   txn.get('mysoftTransactionType') or '',
                                'is_succeed':               txn.get('succeed', True),
                                'api_message':              txn.get('message') or '',
                                # FAZ A.1 / GAP-07: e-Süreç iç UUID referansı
                                'esurec_transaction_id':    parsed_esurec_uuid,
                            }
                        )
                        synced += 1
                    except Exception as e:
                        log.warning(f"[Banking] Upsert hatası (api_id={api_id}): {e}")
                        errors += 1
                        # atomic blok içinde — hata fırlatılmazsa tüm batch devam eder
                        continue
        except Exception as e:
            log.error(f"[Banking] Toplu upsert transaction hatası: {e}")
            return {
                'result': False,
                'msg': f'Banka hareketleri DB yazımında hata: {str(e)[:200]}',
                'count': 0,
            }

        return {
            'result': True,
            'msg': f'{synced} banka hareketi eşitlendi.' + (f' ({errors} hata)' if errors else ''),
            'count': synced,
        }

    def mark_read(self, api_transaction_ids: list, store) -> dict:
        """
        Mysoft'ta hareketleri 'okundu' olarak işaretle.
        """
        seller_vkn = self._get_seller_vkn(store)
        raw_token = self._get_tenant_token(store)

        payload = {
            'seller_vkn': seller_vkn,
            'api_transaction_ids': api_transaction_ids,
        }

        resp = self._client._request(
            'POST', '/api/v1/external/banking/mark-read/', payload,
            extra_headers={'X-Tenant-Token': raw_token},
        )
        if not resp:
            return {'result': False, 'msg': 'e-Süreç mark-read yanıt vermedi.'}
        return resp

    # ────────────────────────────────────────────────────────────────────
    # FAZ A.2 / GAP-02 — FATURALANDI BİLDİRİMİ
    # ────────────────────────────────────────────────────────────────────

    def mark_invoiced(
        self,
        store,
        esurec_transaction_ids: list,
        esurec_invoice_id: str = '',
        kp_invoice_id: str = '',
        kp_invoice_no: str = '',
    ) -> dict:
        """
        e-Süreç tarafında belirtilen banka hareketleri için
        BankTransaction.is_invoiced=True bayrağını yazar.

        Bu çağrı, KP'de bir BankTransaction üzerinden Invoice oluşturulduktan
        SONRA yapılmalıdır. e-Süreç tarafının aynı hareketi tekrar
        faturalama adayı olarak görmesini engeller (mükerrer fatura
        koruması — GAP-02).

        Args:
            store: KP Stores instance
            esurec_transaction_ids: e-Süreç BankTransaction.id (UUID) listesi
            esurec_invoice_id: e-Süreç Invoice.id — KP'nin /invoice/send/
                akışında elde edebildiği UUID. Verilirse linked_invoice
                doldurulur. Boş bırakılabilir.
            kp_invoice_id: KP Invoice UUID (audit için, e-Süreç saklamaz)
            kp_invoice_no: KP fatura numarası (audit için)

        Returns:
            { 'result': bool, 'msg': str, 'updated': int, 'already_invoiced': int,
              'linked_invoice': str|None }

        Hatalar fırlatılmaz; başarısızlık durumunda result=False döner.
        """
        if not esurec_transaction_ids:
            return {
                'result': True,
                'msg': 'esurec_transaction_ids listesi boş — bildirim atlandı.',
                'updated': 0,
            }

        seller_vkn = self._get_seller_vkn(store)
        raw_token = self._get_tenant_token(store)

        # Token yoksa sessiz başarısızlık (KP iş akışı bloklanmamalı)
        if not raw_token:
            log.warning(
                "[Banking mark-invoiced] Token yok, atlanıyor: store=%s, ids=%s",
                getattr(store, 'id', '?'), len(esurec_transaction_ids),
            )
            return {
                'result': False,
                'msg': 'Tenant token yok; mark-invoiced bildirimi atlandı.',
                'updated': 0,
                'retryable': False,
            }

        # UUID listesini string'e normalize et
        ids_payload = []
        for raw in esurec_transaction_ids:
            if raw is None:
                continue
            ids_payload.append(str(raw))

        if not ids_payload:
            return {
                'result': True,
                'msg': 'Geçerli esurec_transaction_id bulunamadı.',
                'updated': 0,
            }

        payload = {
            'seller_vkn': seller_vkn,
            'esurec_transaction_ids': ids_payload,
        }
        if esurec_invoice_id:
            payload['esurec_invoice_id'] = str(esurec_invoice_id)
        if kp_invoice_id:
            payload['kp_invoice_id'] = str(kp_invoice_id)
        if kp_invoice_no:
            payload['kp_invoice_no'] = str(kp_invoice_no)

        try:
            resp = self._client._request(
                'POST', '/api/v1/external/banking/mark-invoiced/', payload,
                extra_headers={'X-Tenant-Token': raw_token},
            )
        except Exception as exc:
            log.error(
                "[Banking mark-invoiced] _request exception: %s (store=%s)",
                exc, getattr(store, 'id', '?'),
            )
            return {
                'result': False,
                'msg': f'mark-invoiced çağrısı başarısız: {type(exc).__name__}',
                'updated': 0,
                'retryable': True,
            }

        if not resp:
            return {
                'result': False,
                'msg': 'e-Süreç mark-invoiced yanıt vermedi.',
                'updated': 0,
                'retryable': True,
            }

        # Yanıt formatı: { success: true, count: N, updated: N, ... } veya
        # { success: false, error: {...} }
        is_success = resp.get('success', False) or resp.get('result', False)
        if not is_success:
            error_msg = (
                (resp.get('error', {}).get('message', '') if isinstance(resp.get('error'), dict) else '')
                or resp.get('message', '')
                or resp.get('error_msg', '')
                or 'mark-invoiced başarısız.'
            )
            return {
                'result': False,
                'msg': error_msg,
                'updated': 0,
            }

        return {
            'result': True,
            'msg': resp.get('message', 'mark-invoiced başarılı.'),
            'updated': resp.get('updated', 0),
            'already_invoiced': resp.get('already_invoiced', 0),
            'linked_invoice': resp.get('linked_invoice'),
        }

    def reconcile_transaction(self, bank_txn_id: str, invoice_id: str, store) -> dict:
        """Stub — invoices app bu projede yok."""
        return {'result': False, 'msg': 'Fatura mutabakatı bu projede devre dışı.'}


# ============================================================================
# 2. CariMatchingService
# ============================================================================

def _normalize_name(name: str) -> str:
    """Türkçe karakterleri normalize eder, küçük harfe çevirir, sadece harf/boşluk bırakır."""
    if not name:
        return ''
    # Türkçe → ASCII
    tr_map = str.maketrans('ığüşöçİĞÜŞÖÇ', 'igusocIGUSOC')
    name = name.translate(tr_map)
    # Unicode normalize + harf dışı karakterleri kaldır
    name = unicodedata.normalize('NFD', name)
    name = re.sub(r'[^a-z\s]', ' ', name.lower())
    return ' '.join(name.split())  # çoklu boşlukları temizle


def _token_sort_ratio(a: str, b: str) -> int:
    """
    Token-sort benzerlik oranı (0-100).
    Kelimeler alfabetik sıralanıp karşılaştırılır.
    Örnek: "ALİ YILMAZ" vs "YILMAZ ALİ" → 100
    """
    if not a or not b:
        return 0
    tokens_a = sorted(_normalize_name(a).split())
    tokens_b = sorted(_normalize_name(b).split())

    if not tokens_a or not tokens_b:
        return 0

    # Ortak token sayısına göre skor
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    intersection = set_a & set_b
    union = set_a | set_b

    if not union:
        return 0

    jaccard = len(intersection) / len(union)
    return int(jaccard * 100)


class CariMatchingService:
    """
    Banka hareketini Kuyum Plus müşterileri (cari) ile eşleştirir.

    Algoritma (üç adım, en yüksek skoru kullanır):
      Adım 1 — TCKN/VKN   : other_vkn_tckn = identification_number  → Skor: 100
      Adım 2 — İsim (tam) : token-sort benzerlik ≥ 85                → Skor: eşleşme skoru
      Adım 3 — İsim (kısmi): herhangi bir token tam eşleşirse        → Skor: 60

    Skor < 60 → eşleşme yok (manuel müdahale gerekir).
    """

    MIN_AUTO_SCORE = 60   # Bu skorun üzerinde otomatik eşleştirme yapılır
    HIGH_SCORE = 85       # Bu skorun üzerinde yüksek güven

    # Banka açıklamalarında sıkça görülen ama müşteri adı olmayan gürültü tokenlar.
    # Bu tokenlar isim eşleştirme öncesinde other_name'den temizlenir.
    _NOISE_TOKENS = frozenset({
        'tr', 'eft', 'hav', 'trf', 'gelen', 'giden', 'tahsilat', 'odeme',
        'bank', 'a.s', 'ltd', 'sti', 'havale', 'transfer', 'virman',
    })

    def __init__(self, store):
        self.store = store

    def _customer_qs(self):
        """Bu mağazaya ait aktif müşterileri döner."""
        return Customers.objects.filter(
            store=self.store, is_deleted=False, is_active=True
        )

    def find_match(self, bank_txn: BankTransaction) -> dict:
        """
        Tek bir banka hareketi için en iyi cari eşleşmesini bulur.

        Döner:
          {
            'customer': <Customers|None>,
            'score': int,          # 0-100
            'method': str,         # 'vkn' | 'name' | 'none'
            'details': str,
          }
        """
        # --- Adım 1: TCKN/VKN eşleştirme ---
        if bank_txn.other_vkn_tckn:
            vkn = str(bank_txn.other_vkn_tckn).strip()
            customer = self._customer_qs().filter(
                identification_number=vkn
            ).first()
            if customer:
                return {
                    'customer': customer,
                    'score': 100,
                    'method': 'vkn',
                    'details': f'TCKN/VKN eşleşti: {vkn}',
                }

        # --- Adım 2 & 3: İsim benzerliği ---
        if not bank_txn.other_name:
            return {'customer': None, 'score': 0, 'method': 'none', 'details': 'Karşı taraf adı yok.'}

        best_score = 0
        best_customer = None

        normalized_other = _normalize_name(bank_txn.other_name)
        # Gürültü tokenlarını temizle (EFT, BANK, TR, A.S vb.)
        other_tokens = set(normalized_other.split()) - self._NOISE_TOKENS

        # Anlamlı token sayısı < 2 ise isim eşleştirme güvenilir değil,
        # false positive riski çok yüksek — döngüye sokmadan atla
        if len(other_tokens) < 2:
            return {
                'customer': None,
                'score': 0,
                'method': 'none',
                'details': f'Yetersiz anlamlı isim tokenı ({len(other_tokens)}). Manuel eşleştirme gerekli.',
            }

        for customer in self._customer_qs().only(
            'id', 'first_name', 'last_name', 'identification_number'
        ):
            full_name = f"{customer.first_name} {customer.last_name}"
            score = _token_sort_ratio(bank_txn.other_name, full_name)

            if score > best_score:
                best_score = score
                best_customer = customer

            # Kısmi token eşleşmesi (skor < 85 ama anlamlı token tam eşleşiyorsa 60 ver)
            if score < self.HIGH_SCORE:
                normalized_full = _normalize_name(full_name)
                full_tokens = set(normalized_full.split()) - self._NOISE_TOKENS
                # Gürültü temizlenmiş tokenlar arasında kesişim kontrol et
                if other_tokens & full_tokens:
                    partial_score = 60
                    if partial_score > best_score:
                        best_score = partial_score
                        best_customer = customer

        if best_score >= self.MIN_AUTO_SCORE and best_customer:
            return {
                'customer': best_customer,
                'score': best_score,
                'method': 'name',
                'details': f'İsim benzerliği skoru: {best_score}',
            }

        return {
            'customer': None,
            'score': best_score,
            'method': 'none',
            'details': f'Eşleşme bulunamadı (en yüksek skor: {best_score}).',
        }

    def auto_match_pending(self) -> dict:
        """
        Mağazanın eşleştirilmemiş gelen hareketlerini otomatik eşleştirir.

        Döner: {'matched': int, 'skipped': int}
        """
        pending = BankTransaction.objects.filter(
            store=self.store,
            match_status=BankTransaction.MatchStatus.UNMATCHED,
            plus_minus=BankTransaction.PlusMinus.DEBIT,  # Sadece gelen hareketler
        )

        matched = 0
        skipped = 0

        for txn in pending:
            result = self.find_match(txn)
            if result['customer']:
                with db_transaction.atomic():
                    txn.customer = result['customer']
                    txn.match_score = result['score']
                    txn.match_status = BankTransaction.MatchStatus.AUTO_MATCHED
                    txn.save(update_fields=['customer', 'match_score', 'match_status', 'updated_on'])
                matched += 1
            else:
                skipped += 1

        return {'matched': matched, 'skipped': skipped}


# ============================================================================
# 3. InvoiceAutoService — STUB (invoices app kaldırıldı — Juwelier Plus)
# ============================================================================

class InvoiceAutoService:
    """
    Stub — invoices app Juwelier Plus'ta mevcut değil.
    """

    def __init__(self, store, user=None):
        self.store = store
        self.user = user

    def can_create_invoice(self, bank_txn: BankTransaction) -> tuple:
        """Stub."""
        return False, 'Fatura servisi bu projede devre dışı.'

    def create_and_send(self, bank_txn: BankTransaction,
                        vat_rate: Decimal = None,
                        item_description: str = '',
                        auto_send_to_gib: bool = True) -> dict:
        """Stub — invoices app bu projede yok."""
        return {'result': False, 'msg': 'Fatura servisi bu projede devre dışı.', 'invoice_id': None, 'invoice_no': None, 'esurec_id': None}

    def _create_invoice(self, bank_txn: BankTransaction, vat_rate: Decimal, item_desc: str):
        """Stub."""
        return None

    def _send_to_esurec_and_gib(self, invoice) -> dict:
        """Stub."""
        return {'result': False, 'esurec_id': None, 'msg': 'Fatura servisi bu projede devre dışı.'}


# ============================================================================
# 4. PaymentStatusService — Kısmi/Fazla Ödeme Yönetimi
# ============================================================================

class PaymentStatusService:
    """
    Stub — invoices app Juwelier Plus'ta mevcut değil.
    """

    @staticmethod
    def compute_for_invoice(invoice) -> str:
        """Stub — her zaman 'UNPAID' döner."""
        return 'UNPAID'

    @classmethod
    def update_invoice(cls, invoice):
        """Stub — işlem yapmaz."""
        return 'UNPAID'


# ============================================================================
# 5. EsurecHealthCheckService — e-Süreç Tenant Durum Sorgulama (FAZ 2)
# ============================================================================

class EsurecHealthCheckService:
    """
    Kuyum Plus → e-Süreç tenant-status endpoint'ine S2S istek atar.
    Katman 1 (HMAC) + Katman 2 (X-Tenant-Token) header'larıyla gönderir.
    EsurecTenantCredential.update_health() ile 1 saatlik TTL önbellek günceller.

    v4 DEĞİŞİKLİK:
        Artık doğrudan requests kütüphanesi KULLANILMAZ.
        Tüm S2S istekleri ESurecClient._request() üzerinden geçer.
        X-Tenant-Token header'ı extra_headers parametresiyle eklenir.
        Bu sayede URL oluşturma, HMAC header ekleme, HTTP hata yönetimi
        ve JSON parse işlemleri merkezi metot tarafından yapılır —
        ESurecBankingClient ve EsurecProvisioningService ile aynı pattern.

    Kullanım:
        service = EsurecHealthCheckService(store)
        result = service.check()
        # {'result': True, 'cached': False, 'status': 'OK', 'efatura_active': True, ...}

        result = service.check(force=True)  # Önbelleği atla, canlı sorgu yap
    """

    # ── e-Süreç endpoint path (trailing slash zorunlu — Django kuralı) ──
    TENANT_STATUS_ENDPOINT = '/api/v1/external/tenant-status/'

    def __init__(self, store):
        self.store = store

    @staticmethod
    def _parse_iso_dt(raw):
        """
        ISO 8601 datetime string'ini timezone-aware datetime'a parse eder.
        Geçersizse None döner (exception fırlatmaz).
        """
        if not raw:
            return None
        try:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(str(raw))
            if dt is None:
                return None
            # Naive ise Django timezone aware'e çek
            if timezone.is_naive(dt):
                dt = timezone.make_aware(dt, timezone.get_default_timezone())
            return dt
        except Exception:
            return None

    def check(self, force: bool = False) -> dict:
        """
        e-Süreç tenant durumunu sorgular.

        Args:
            force: True ise TTL önbelleğini atlar ve canlı S2S sorgusu yapar.

        Returns:
            {
                'result': bool,
                'cached': bool,           # Önbellekten mi okundu?
                'status': str,             # OK / ERROR / DEGRADED / NOT_CONFIGURED / TOKEN_INVALID
                'efatura_active': bool,
                'banking_active': bool,
                'dealer_title': str,       # e-Süreç'teki firma adı (canlı sorguda)
                'last_check': str|None,    # ISO 8601 zaman damgası
                'msg': str,
            }
        """
        from apps.banking.models import EsurecTenantCredential

        cred = EsurecTenantCredential.objects.filter(
            store=self.store,
            is_active=True,
        ).first()

        if not cred:
            return {
                'result': False,
                'cached': False,
                'status': 'NOT_CONFIGURED',
                'efatura_active': False,
                'banking_active': False,
                'msg': 'e-Süreç entegrasyon ayarları bulunamadı veya pasif.',
            }

        if not cred.is_token_valid:
            return {
                'result': False,
                'cached': False,
                'status': 'TOKEN_INVALID',
                'efatura_active': False,
                'banking_active': False,
                'msg': 'Tenant token geçersiz veya süresi dolmuş.',
            }

        # ── TTL önbellek kontrolü (1 saat) ────────────────────────────
        if not force and not cred.health_check_stale:
            return {
                'result': True,
                'cached': True,
                'status': cred.last_health_status,
                'efatura_active': cred.efatura_active,
                'banking_active': cred.banking_active,
                'last_check': (
                    cred.last_health_check_at.isoformat()
                    if cred.last_health_check_at else None
                ),
                'msg': 'Önbellekten okundu.',
            }

        # ── Canlı S2S sorgusu ─────────────────────────────────────────
        return self._live_check(cred)

    def _live_check(self, cred) -> dict:
        """
        e-Süreç tenant-status endpoint'ine
        Katman 1 (HMAC) + Katman 2 (X-Tenant-Token) header'larıyla GET isteği atar.

        ESurecClient._request() kullanılır — tüm URL oluşturma, HMAC header ekleme,
        HTTP hata yönetimi ve JSON parse işlemleri merkezi metot tarafından yapılır.

        _request() dönüş yapısı:
            Başarılı (2xx + JSON): e-Süreç'in JSON yanıtı (dict)
            Hata (HTTP/ağ): {'result': False, 'error_msg': '...', 'retryable': bool, ...}
        """
        # ── ESurecClient başlat ───────────────────────────────────────
        try:
            client = ESurecClient()
        except ValueError as e:
            cred.update_health(status='ERROR')
            return {
                'result': False,
                'cached': False,
                'status': 'ERROR',
                'efatura_active': False,
                'banking_active': False,
                'msg': f'ESurecClient yapılandırma hatası: {str(e)[:200]}',
            }

        # ── Katman 2 token kontrolü ──────────────────────────────────
        raw_token = cred.tenant_token
        if not raw_token:
            cred.update_health(status='ERROR')
            return {
                'result': False,
                'cached': False,
                'status': 'ERROR',
                'efatura_active': False,
                'banking_active': False,
                'msg': 'Tenant token çözülemedi (şifreleme hatası olabilir).',
            }

        # ── seller_vkn: GET isteklerinde query param olarak gönderilmeli ──
        # SecureExternalView GET istekleri için body parse etmez;
        # seller_vkn query parametresinden okunur.
        seller_vkn = ''
        company = getattr(self.store, 'company', None)
        if company:
            seller_vkn = str(company.tax_number or '').strip()

        log.debug(
            "[Banking] HealthCheck: endpoint=%s base=%s vkn=%s",
            self.TENANT_STATUS_ENDPOINT, client.base_url, seller_vkn,
        )

        # ── Merkezi _request() üzerinden S2S çağrısı ─────────────────
        # Katman 1 (HMAC): _request() → _auth_headers() otomatik ekler
        # Katman 2 (X-Tenant-Token): extra_headers ile eklenir
        # seller_vkn: GET'te data dict → params olarak query string'e eklenir
        # URL oluşturma: f"{base_url}{endpoint}" — tüm servislerle aynı pattern
        # HTTP hata yönetimi + JSON parse: _request() merkezi yapar
        data = client._request(
            'GET',
            self.TENANT_STATUS_ENDPOINT,
            {'seller_vkn': seller_vkn},
            extra_headers={'X-Tenant-Token': raw_token},
        )

        # ── Boş yanıt kontrolü ───────────────────────────────────────
        if not data:
            cred.update_health(status='ERROR')
            return {
                'result': False,
                'cached': False,
                'status': 'ERROR',
                'efatura_active': False,
                'banking_active': False,
                'msg': 'e-Süreç tenant-status endpoint yanıt vermedi.',
            }

        # ── _request() hata döndüyse ─────────────────────────────────
        # HTTP != 2xx, ConnectionError, Timeout vb. durumlarında
        # _request() → {'result': False, 'error_msg': '...', 'retryable': bool}
        if data.get('result') is False:
            retryable = data.get('retryable', False)
            status = 'DEGRADED' if retryable else 'ERROR'
            cred.update_health(status=status)
            return {
                'result': False,
                'cached': False,
                'status': status,
                'efatura_active': False,
                'banking_active': False,
                'msg': data.get('error_msg', '') or 'e-Süreç bağlantı hatası.',
                'error_code': data.get('http_status', ''),
            }

        # ── Başarılı yanıt (2xx + valid JSON + success=True) ─────────
        if data.get('success'):
            modules = data.get('modules', {})
            dealer_info = data.get('dealer', {})
            token_info = data.get('token', {}) or {}

            efatura = modules.get('efatura_active', False)
            banking = modules.get('banking_active', False)
            esurec_uuid = dealer_info.get('id')

            # ── FAZ B.3 / GAP-05 — token bloğunu parse et ────────────
            remote_status = (token_info.get('status') or '').upper().strip() or None
            remote_susp = token_info.get('suspension_count')
            remote_expires_at = self._parse_iso_dt(token_info.get('expires_at'))
            remote_status_changed_at = self._parse_iso_dt(
                token_info.get('last_status_change_at')
            )

            cred.update_health(
                status='OK',
                efatura=efatura,
                banking=banking,
                esurec_uuid=esurec_uuid,
                remote_token_status=remote_status,
                remote_suspension_count=remote_susp,
                remote_status_changed_at=remote_status_changed_at,
                remote_token_expires_at=remote_expires_at,
            )

            return {
                'result': True,
                'cached': False,
                'status': 'OK',
                'efatura_active': efatura,
                'banking_active': banking,
                'dealer_title': dealer_info.get('title', ''),
                'last_check': (
                    cred.last_health_check_at.isoformat()
                    if cred.last_health_check_at else None
                ),
                # FAZ B.3 — UI'a token bilgisi de döndür
                'token': {
                    'status': remote_status,
                    'is_active': bool(token_info.get('is_active')),
                    'expires_at': token_info.get('expires_at'),
                    'expires_soon': bool(token_info.get('expires_soon')),
                    'suspension_count': int(remote_susp or 0),
                    'warning': cred.remote_token_warning,
                },
                'msg': data.get('message', 'Bağlantı başarılı.'),
            }

        # ── 2xx ama success=False (e-Süreç iç hatası) ────────────────
        cred.update_health(status='ERROR')

        error_info = data.get('error', {})
        error_msg = (
            error_info.get('message', '')
            or data.get('error_msg', '')
            or data.get('message', '')
            or 'e-Süreç beklenmeyen yanıt formatı.'
        )

        return {
            'result': False,
            'cached': False,
            'status': 'ERROR',
            'efatura_active': False,
            'banking_active': False,
            'msg': error_msg,
            'error_code': error_info.get('code', ''),
        }


# ============================================================================
# 6. EsurecProvisioningService — Otomatik Token Üretimi (Automated Provisioning)
# ============================================================================

class EsurecProvisioningService:
    """
    Kuyum Plus → e-Süreç provision-tenant endpoint'ine S2S istek atar.
    YALNIZCA Katman 1 (HMAC) header'ları gönderir.
    Katman 2 (X-Tenant-Token) gönderilMEZ çünkü bu çağrının amacı
    zaten ilk kez token almaktır.

    URL Tutarlılığı:
        ESurecClient._request() metodu kullanılır. Bu, ESurecBankingClient'ın
        banking endpoint'lerinde (fetch_transactions, mark_read) kullandığı
        aynı mekanizmadır. URL oluşturma: f"{base_url}{endpoint}"
        Bu sayede base_url + path her zaman birebir eşleşir.

    Akış:
        1. Mağazanın firmasından VKN alınır.
        2. ESurecClient._request('POST', endpoint, payload) çağrılır.
           (HMAC header'ları _request() tarafından otomatik eklenir.)
        3. Dönen raw token Fernet ile şifrelenerek EsurecTenantCredential'a kaydedilir.

    Kullanım:
        service = EsurecProvisioningService()
        result = service.provision(store)
        # {'result': True, 'msg': '...', 'dealer_title': '...', 'expires_at': '...'}
    """

    # ── e-Süreç endpoint path (trailing slash zorunlu — Django kuralı) ──
    PROVISION_ENDPOINT = '/api/v1/external/provision-tenant/'

    def provision(self, store) -> dict:
        """
        Belirtilen mağaza için e-Süreç'ten otomatik token talep eder
        ve dönen token'ı Fernet şifreli olarak EsurecTenantCredential'a kaydeder.

        Args:
            store: Stores model instance (store.company.tax_number zorunlu)

        Returns:
            {
                'result': bool,
                'msg': str,
                'dealer_title': str,   # (başarılı ise) e-Süreç'teki firma adı
                'expires_at': str,     # (başarılı ise) token son kullanma tarihi
            }
        """
        # ── VKN kontrolü ──────────────────────────────────────────────
        company = getattr(store, 'company', None)
        if not company:
            return {
                'result': False,
                'msg': 'Mağazaya bağlı firma bulunamadı.',
            }

        vkn = str(company.tax_number or '').strip()
        if not vkn:
            return {
                'result': False,
                'msg': 'Firmanın vergi numarası (VKN/TCKN) tanımlı değil. '
                       'Önce firma bilgilerinden VKN giriniz.',
            }

        # ── ESurecClient üzerinden S2S isteği ─────────────────────────
        # ESurecClient._request() kullanılır — bu banking endpoint'leri ile
        # AYNI URL oluşturma mekanizmasıdır:
        #   url = f"{self.base_url}{endpoint}"
        #   headers = self._auth_headers()   ← SADECE Katman 1 (HMAC)
        # X-Tenant-Token EKLENMEz çünkü _request() onu göndermez.
        try:
            client = ESurecClient()
        except ValueError as e:
            return {
                'result': False,
                'msg': f'e-Süreç bağlantı yapılandırması eksik: {str(e)[:200]}',
            }

        payload = {'seller_vkn': vkn}

        log.info(
            "[Banking] Provisioning isteği: endpoint=%s vkn=%s store=%s",
            self.PROVISION_ENDPOINT, vkn, store.id,
        )

        # _request() HMAC header'ları otomatik ekler ve JSON parse eder.
        # Hata durumunda {'result': False, 'error_msg': '...'} dict döner.
        # Başarılı durumda e-Süreç'in JSON yanıtını dict olarak döner.
        data = client._request('POST', self.PROVISION_ENDPOINT, payload)

        if not data:
            return {
                'result': False,
                'msg': 'e-Süreç provision endpoint yanıt vermedi.',
            }

        # ── _request() 2xx dışı yanıtları {'result': False, ...} olarak döner ──
        # e-Süreç'in standart hata formatı: {'success': False, 'error': {...}}
        # Ancak _request() bunu {'result': False, 'error_msg': '...'} olarak normalleştirir.
        if data.get('result') is False or data.get('success') is False:
            error_info = data.get('error', {})
            error_msg = (
                error_info.get('message', '')
                or data.get('error_msg', '')
                or data.get('msg', '')
                or 'Token üretim isteği başarısız.'
            )
            return {
                'result': False,
                'msg': error_msg,
                'error_code': error_info.get('code', '') or data.get('error_code', ''),
            }

        # ── Token çıkar ──────────────────────────────────────────────
        # api_success_response: body.update(data) kullanır, token root seviyededir.
        raw_token = data.get('tenant_token', '')
        if not raw_token:
            return {
                'result': False,
                'msg': 'e-Süreç başarılı yanıt döndü ancak token içermiyor. '
                       'Lütfen e-Süreç yöneticinize başvurun.',
            }

        # ── Token'ı Fernet şifreli olarak kaydet ─────────────────────
        from apps.banking.models import EsurecTenantCredential
        from datetime import timedelta as _td

        try:
            cred, _created = EsurecTenantCredential.objects.get_or_create(
                store=store,
                defaults={'is_active': False},
            )

            # Fernet setter: cred.tenant_token = raw_token → tenant_token_enc alanına yazar
            cred.tenant_token = raw_token
            cred.is_active = True

            # ── Token süresini belirle ────────────────────────────────────
            expires_at_str = data.get('expires_at', '')
            if expires_at_str:
                from django.utils.dateparse import parse_datetime
                parsed = parse_datetime(expires_at_str)
                cred.token_expires_at = parsed or (timezone.now() + _td(days=90))
            else:
                cred.token_expires_at = timezone.now() + _td(days=90)

            # ── Health check cache'i sıfırla ──────────────────────────────
            cred.last_health_status = EsurecTenantCredential.HealthStatus.UNKNOWN
            cred.last_health_check_at = None

            # ── Dealer bilgilerini cache'le (varsa) ──────────────────────
            dealer_title = data.get('dealer_title', '')
            dealer_id = data.get('dealer_id', '')
            if dealer_id:
                cred.esurec_dealer_uuid = dealer_id

            cred.save()

        except Exception as e:
            log.exception(
                "[Banking] Provision token kaydetme hatası: store=%s vkn=%s hata=%s",
                store.id, vkn, e,
            )
            return {
                'result': False,
                'msg': 'e-Süreç token kaydedilirken bir hata oluştu. '
                       'Lütfen sistem yöneticisine başvurun.',
            }

        dealer_title = data.get('dealer_title', '')

        log.info(
            "[Banking] Provision OK: store=%s company=%s vkn=%s dealer=%s",
            store.id, company.title, vkn, dealer_title,
        )

        return {
            'result': True,
            'msg': f'Token başarıyla oluşturuldu ve kaydedildi. '
                   f'(e-Süreç Firma: {dealer_title})',
            'dealer_title': dealer_title,
            'expires_at': str(cred.token_expires_at),
        }


# ============================================================================
# 7. POSCommissionService — POS Komisyon Hesaplama Motoru (FAZ 4)
# ============================================================================

class POSCommissionService:
    """
    POS terminali bazında taksit komisyon oranları hesaplama motoru.

    Kullanım:
        svc = POSCommissionService()
        result = svc.calculate(bank_account, amount=Decimal('1000'), installment_count=3)
        # → {'rate': Decimal('2.49'), 'commission': Decimal('24.90'),
        #    'net_amount': Decimal('975.10'), 'maturity_date': date(2026, 4, 27),
        #    'source': 'exact'}
    """

    def calculate(self, bank_account, amount, installment_count=1,
                  card_type='GENERIC', txn_date=None):
        """
        Verilen POS hesabı, tutar ve taksit sayısına göre komisyon hesaplar.

        Arama önceliği (fallback zinciri):
        1. Tam eşleşme: bank_account + card_type + installment_count
        2. Genel kart tipi: bank_account + GENERIC + installment_count
        3. Tek çekim fallback: bank_account + card_type + 1
        4. En genel: bank_account + GENERIC + 1
        5. Hesap toleransı: bank_account.reconciliation_tolerance'ı oran gibi kullan

        Returns:
            dict: {'rate', 'commission', 'net_amount', 'maturity_date', 'maturity_days', 'source'}
        """
        from apps.banking.models import POSCommissionRate
        from datetime import timedelta

        if txn_date is None:
            txn_date = timezone.localtime(timezone.now()).date()

        amount = Decimal(str(amount))

        # Arama zinciri
        lookup_chain = [
            # 1. Tam eşleşme
            {'bank_account': bank_account, 'card_type': card_type,
             'installment_count': installment_count, 'is_active': True},
            # 2. Genel kart tipi + aynı taksit
            {'bank_account': bank_account, 'card_type': 'GENERIC',
             'installment_count': installment_count, 'is_active': True},
        ]

        # Taksit > 1 ise, tek çekim fallback'leri de ekle
        if installment_count > 1:
            lookup_chain.extend([
                # 3. Aynı kart tipi + tek çekim
                {'bank_account': bank_account, 'card_type': card_type,
                 'installment_count': 1, 'is_active': True},
                # 4. Genel + tek çekim
                {'bank_account': bank_account, 'card_type': 'GENERIC',
                 'installment_count': 1, 'is_active': True},
            ])

        source_labels = ['exact', 'generic_card', 'single_fallback', 'generic_single']

        for i, filters in enumerate(lookup_chain):
            rate_obj = POSCommissionRate.objects.filter(**filters).first()
            if rate_obj:
                commission = (amount * rate_obj.commission_rate / Decimal('100')).quantize(Decimal('0.01'))
                net_amount = amount - commission
                maturity = txn_date + timedelta(days=rate_obj.maturity_days)
                return {
                    'rate': rate_obj.commission_rate,
                    'commission': commission,
                    'net_amount': net_amount,
                    'maturity_date': maturity,
                    'maturity_days': rate_obj.maturity_days,
                    'source': source_labels[i] if i < len(source_labels) else 'fallback',
                }

        # 5. Hicbir oran bulunamazsa → %0 komisyon uygula (varsayilan)
        # NOT: tolerance_fallback kaldirildi (Faz 7).
        # reconciliation_tolerance komisyon orani degildir, yaniltici sonuc uretiyordu.
        return {
            'rate': Decimal('0'),
            'commission': Decimal('0'),
            'net_amount': amount,
            'maturity_date': txn_date + timedelta(days=1),
            'maturity_days': 1,
            'source': 'none',
        }

    def get_rates_for_account(self, bank_account):
        """Verilen POS hesabının tüm aktif komisyon oranlarını döndürür."""
        from apps.banking.models import POSCommissionRate
        return POSCommissionRate.objects.filter(
            bank_account=bank_account, is_active=True
        ).order_by('card_type', 'installment_count')

    def save_rate(self, bank_account, card_type, installment_count,
                  commission_rate, maturity_days=1):
        """
        Komisyon oranı oluşturur veya günceller.
        unique_together: (bank_account, card_type, installment_count)
        """
        from apps.banking.models import POSCommissionRate
        obj, created = POSCommissionRate.objects.update_or_create(
            bank_account=bank_account,
            card_type=card_type,
            installment_count=installment_count,
            defaults={
                'commission_rate': Decimal(str(commission_rate)),
                'maturity_days': int(maturity_days),
                'is_active': True,
            }
        )
        return obj, created

    def delete_rate(self, rate_id):
        """Komisyon oranını soft-delete yapar (is_active=False)."""
        from apps.banking.models import POSCommissionRate
        updated = POSCommissionRate.objects.filter(id=rate_id).update(is_active=False)
        return updated > 0


# ============================================================================
# 8. ReconciliationService — Ödeme ↔ Banka Hareketi Mutabakat Motoru (FAZ 3)
# ============================================================================

class ReconciliationService:
    """
    Payment (iç kayıt) ile BankTransaction (dış kayıt) arasında
    otomatik mutabakat eşleştirmesi yapar.

    Akış:
      1. PENDING statüsündeki Payment kayıtlarını bul
      2. Her Payment için: bank_account.iban üzerinden BankTransaction adaylarını bul
      3. Skorlama algoritmasıyla en iyi adayı seç
      4. Skor ≥ 85 ise otomatik eşle

    Skor Hesaplama (0–100):
      +40 : Tutar tam eşleşti
      +25 : Tutar tolerans içinde (BankAccount.reconciliation_tolerance)
      +10 : Tarih farkı ≤ 1 gün
      +5  : Tarih farkı ≤ 3 gün
      +30 : Aynı IBAN üzerinden işlem

      Sadece gelen (plus_minus=1 / DEBIT) BankTransaction adayları değerlendirilir.

    Eşikler:
      AUTO_MATCH_THRESHOLD = 85   → Otomatik eşle
      SUGGEST_THRESHOLD    = 60   → Öneri olarak göster (UI'da kullanılır)
    """

    AUTO_MATCH_THRESHOLD = 85
    SUGGEST_THRESHOLD = 60

    def __init__(self, store):
        self.store = store

    # ------------------------------------------------------------------
    # PUBLIC: Tek ödeme için mutabakat
    # ------------------------------------------------------------------
    def reconcile_transaction(self, payment) -> dict:
        """
        Tek bir PENDING Payment için en iyi BankTransaction eşleşini bulur.

        Returns:
            {
                'status': 'matched' | 'discrepancy' | 'pending' | 'skipped',
                'score': int,
                'bank_txn_id': str | None,
                'diff': Decimal | None,
            }
        """
        from apps.process.models import Payment

        if payment.reconciliation_status != Payment.ReconciliationStatus.PENDING:
            return {
                'status': 'skipped',
                'score': 0,
                'bank_txn_id': None,
                'diff': None,
            }

        candidates = self._resolve_candidates(payment)
        if not candidates.exists():
            return {
                'status': 'pending',
                'score': 0,
                'bank_txn_id': None,
                'diff': None,
            }

        best_txn, best_score = self._auto_match(payment, candidates)

        if best_txn and best_score >= self.AUTO_MATCH_THRESHOLD:
            # Faz 4: net_amount varsa komisyon sonrası tutarla karşılaştır
            expected_amount = payment.net_amount if payment.net_amount else payment.amount
            diff = best_txn.amount - expected_amount
            status = self._apply_match(payment, best_txn, best_score, diff)
            return {
                'status': status,
                'score': best_score,
                'bank_txn_id': str(best_txn.id),
                'diff': diff,
            }

        return {
            'status': 'pending',
            'score': best_score,
            'bank_txn_id': str(best_txn.id) if best_txn else None,
            'diff': None,
        }

    # ------------------------------------------------------------------
    # PUBLIC: Mağaza için toplu mutabakat
    # ------------------------------------------------------------------
    def reconcile_all_pending(self) -> dict:
        """
        Mağazanın tüm PENDING ödemelerini toplu olarak mutabakat yapar.

        Race condition korunması:
          - select_for_update(skip_locked=True) ile satır kilidi
          - transaction.atomic() ile atomik blok

        Returns:
            {'matched': int, 'discrepancy': int, 'pending': int}
        """
        from apps.process.models import Payment

        matched = 0
        discrepancy = 0
        pending = 0

        with db_transaction.atomic():
            payments = (
                Payment.objects
                .select_for_update(skip_locked=True)
                .filter(
                    reconciliation_status=Payment.ReconciliationStatus.PENDING,
                    bank_account__store=self.store,
                    bank_account__isnull=False,
                )
                .select_related('bank_account')
            )

            for payment in payments:
                result = self.reconcile_transaction(payment)
                if result['status'] == 'matched':
                    matched += 1
                elif result['status'] == 'discrepancy':
                    discrepancy += 1
                else:
                    pending += 1

        log.info(
            "[Reconciliation] store=%s tamamlandı: matched=%d, discrepancy=%d, pending=%d",
            self.store.id, matched, discrepancy, pending,
        )

        return {
            'matched': matched,
            'discrepancy': discrepancy,
            'pending': pending,
        }

    # ------------------------------------------------------------------
    # PUBLIC: Manuel eşleştirme
    # ------------------------------------------------------------------
    def manual_match(self, payment, bank_txn, user) -> dict:
        """
        Personelin elle Payment ile BankTransaction'ı eşleştirmesi.

        Returns:
            {'result': bool, 'msg': str, 'status': str}
        """
        from apps.process.models import Payment

        diff = bank_txn.amount - payment.amount
        tolerance = Decimal('0.50')
        if payment.bank_account:
            tolerance = payment.bank_account.reconciliation_tolerance

        with db_transaction.atomic():
            if abs(diff) <= tolerance:
                payment.reconciliation_status = Payment.ReconciliationStatus.MATCHED
            else:
                payment.reconciliation_status = Payment.ReconciliationStatus.DISCREPANCY

            payment.matched_bank_transaction = bank_txn
            payment.reconciliation_diff = diff
            payment.reconciled_at = timezone.now()
            payment.reconciled_by = user
            payment.save(update_fields=[
                'reconciliation_status',
                'matched_bank_transaction',
                'reconciliation_diff',
                'reconciled_at',
                'reconciled_by',
            ])

        status_label = 'Eşleşti' if payment.reconciliation_status == Payment.ReconciliationStatus.MATCHED else 'Uyuşmazlık'
        return {
            'result': True,
            'msg': f'Manuel mutabakat tamamlandı ({status_label}). Fark: {diff} TL',
            'status': payment.reconciliation_status,
        }

    # ------------------------------------------------------------------
    # PRIVATE: Aday BankTransaction'ları bul
    # ------------------------------------------------------------------
    def _resolve_candidates(self, payment):
        """
        Payment.bank_account.iban üzerinden eşleşebilecek
        BankTransaction adaylarını döner.

        Kriterler:
          - Aynı mağazaya ait
          - Gelen işlem (plus_minus=1, DEBIT)
          - Henüz başka bir Payment ile eşleşmemiş
          - Son 7 gün içinde (performans için)
        """
        from datetime import timedelta

        if not payment.bank_account or not payment.bank_account.iban:
            return BankTransaction.objects.none()

        iban = payment.bank_account.iban
        cutoff = timezone.now() - timedelta(days=7)

        return BankTransaction.objects.filter(
            store=self.store,
            iban=iban,
            plus_minus=BankTransaction.PlusMinus.DEBIT,
            doc_date__gte=cutoff,
        ).exclude(
            matched_payments__isnull=False,
        )

    # ------------------------------------------------------------------
    # PRIVATE: Skorlama algoritması
    # ------------------------------------------------------------------
    def _auto_match(self, payment, candidates):
        """
        Skor bazlı en iyi adayı seçer.

        Returns:
            (best_txn: BankTransaction | None, best_score: int)
        """
        best_txn = None
        best_score = 0

        tolerance = Decimal('0.50')
        if payment.bank_account:
            tolerance = payment.bank_account.reconciliation_tolerance

        payment_date = payment.date
        # Faz 4: Komisyon kesintili net tutarı kullan (varsa), yoksa brüt tutar
        payment_amount = payment.net_amount if payment.net_amount else payment.amount
        payment_iban = payment.bank_account.iban if payment.bank_account else ''

        for txn in candidates:
            score = 0

            # ── Tutar kontrolü (net_amount bazlı — komisyon hesaba katılır) ──
            diff = abs(txn.amount - payment_amount)
            if diff == Decimal('0'):
                score += 40
            elif diff <= tolerance:
                score += 25

            # ── Tarih kontrolü ──
            if payment_date and txn.doc_date:
                day_diff = abs((txn.doc_date.date() - payment_date.date()).days)
                if day_diff <= 1:
                    score += 10
                elif day_diff <= 3:
                    score += 5

            # ── IBAN kontrolü ──
            if payment_iban and txn.iban and payment_iban == txn.iban:
                score += 30

            if score > best_score:
                best_score = score
                best_txn = txn

        return best_txn, best_score

    # ------------------------------------------------------------------
    # PRIVATE: Eşleştirmeyi uygula
    # ------------------------------------------------------------------
    def _apply_match(self, payment, bank_txn, score, diff) -> str:
        """
        Payment kaydını günceller. DB save yapar.
        Tolerans içindeki fark → MATCHED, dışındaki → DISCREPANCY.

        Returns:
            'matched' | 'discrepancy'
        """
        from apps.process.models import Payment

        tolerance = Decimal('0.50')
        if payment.bank_account:
            tolerance = payment.bank_account.reconciliation_tolerance

        if abs(diff) <= tolerance:
            payment.reconciliation_status = Payment.ReconciliationStatus.MATCHED
            result_status = 'matched'
        else:
            payment.reconciliation_status = Payment.ReconciliationStatus.DISCREPANCY
            result_status = 'discrepancy'

        payment.matched_bank_transaction = bank_txn
        payment.reconciliation_diff = diff
        payment.reconciled_at = timezone.now()
        payment.save(update_fields=[
            'reconciliation_status',
            'matched_bank_transaction',
            'reconciliation_diff',
            'reconciled_at',
        ])

        log.info(
            "[Reconciliation] Eşleşti: payment=%s → bank_txn=%s score=%d diff=%s status=%s",
            payment.pk, bank_txn.pk, score, diff, result_status,
        )

        return result_status


# ============================================================================
# DÖVİZ (FX) BAKİYE KATMANI — SSOT: Payment Tablosu
# ----------------------------------------------------------------------------
# YOL 2 (SSOT Refactor) + YOL 3 (Acil Guard) Ortak Servisleri
# Kaynak Plan:
#   - context/doviz_yol2_ssot_refactor_plani.md
#   - context/doviz_yol3_acil_guard_plani.md
#
# Amaç:
#   Hızlı/Perakende ekranı, döviz ürün bakiyesini StockSnapshot yerine
#   _get_fx_breakdown() (Payment SSOT) üzerinden okusun.
#   Checkout aşamasında ise FXBalanceGuard yetersiz bakiyede işlemi bloke eder.
# ============================================================================

# Ürün adı → Döviz kodu eşlemesi (USDTRY → USD).
# fast_views._CURRENCY_PRODUCT_MAP'in tersi; tekrar etmemek için merkezi tek nokta.
CURRENCY_FROM_PRODUCT_NAME = {
    'USDTRY': 'USD',
    'EURTRY': 'EUR',
    'GBPTRY': 'GBP',
    'CADTRY': 'CAD',
    'QARTRY': 'QAR',
    'SARTRY': 'SAR',
    'CHFTRY': 'CHF',
    'AUDTRY': 'AUD',
}


# ────────────────────────────────────────────────────────────────────────
# DÖVİZ SSOT — Tek Doğruluk Kaynağı (Faz 13: Kasa Çoklu-Döviz Düzeltme)
# ────────────────────────────────────────────────────────────────────────
# Aşağıdaki sabitler proje genelinde TEK kaynak olarak kullanılmalıdır.
# fast_views, retail_views, wholesale_views, bank_views bu sabitleri import eder.
# Yeni bir döviz eklenecekse YALNIZCA bu blokta güncelleme yapılır.

# Kod → Ürün adı (örn. 'USD' → 'USDTRY'): kur okuma için kullanılır.
PRODUCT_NAME_FROM_CURRENCY = {code: name for name, code in CURRENCY_FROM_PRODUCT_NAME.items()}

# Geçerli döviz kodları kümesi (TRY dahil). reference whitelist için kullanılır.
SUPPORTED_FX_CURRENCIES = frozenset(set(CURRENCY_FROM_PRODUCT_NAME.values()) | {'TRY'})

# Sentinel kur değerleri — FX kasası "Bakiye Düzeltme" / sentinel-rate akışı için.
# Yazma ve okuma yolları aynı haritayı kullanmalıdır (yazma-okuma desync engeli).
FX_SENTINEL_MAP = {
    'USD': Decimal('0.01'),
    'EUR': Decimal('0.02'),
    'GBP': Decimal('0.03'),
    'CHF': Decimal('0.04'),
    'CAD': Decimal('0.05'),
    'AUD': Decimal('0.06'),
    'JPY': Decimal('0.07'),
    'QAR': Decimal('0.08'),
    'SAR': Decimal('0.09'),
}

# Ters harita: kayıtlı sentinel kur → döviz kodu (okuma yolu).
FX_SENTINEL_REVERSE_MAP = {float(rate): code for code, rate in FX_SENTINEL_MAP.items()}


def get_currency_code_from_product(product) -> Optional[str]:
    """
    is_currency=True olan bir ürünün döviz kodunu döndürür.

    Args:
        product: Products instance

    Returns:
        'USD', 'EUR' vb. veya None (döviz değilse / tanımsız ürün adıysa).
    """
    if not product or not getattr(product, 'is_currency', False):
        return None
    name_key = (getattr(product, 'name', '') or '').upper().strip()
    if name_key in CURRENCY_FROM_PRODUCT_NAME:
        return CURRENCY_FROM_PRODUCT_NAME[name_key]
    # Fallback: prefix yakala (USDTRY-Custom gibi varyasyonlar için)
    for prefix, code in CURRENCY_FROM_PRODUCT_NAME.items():
        if name_key.startswith(prefix):
            return code
    return None


def detect_currency_from_name(name) -> Optional[str]:
    """
    Ürün/string adından döviz kodunu çıkarır (Products instance gerektirmeden).

    SUPPORTED_FX_CURRENCIES içindeki tüm kodlar için prefix taraması yapar
    (USDTRY → USD, SARTRY → SAR, AUDTRY → AUD ...).

    Args:
        name: str — ürün adı veya benzeri string

    Returns:
        'USD', 'EUR' vb. veya None (eşleşme yoksa).
    """
    if not name:
        return None
    key = str(name).upper().strip()
    if key in CURRENCY_FROM_PRODUCT_NAME:
        return CURRENCY_FROM_PRODUCT_NAME[key]
    for prefix, code in CURRENCY_FROM_PRODUCT_NAME.items():
        if key.startswith(prefix):
            return code
    # Tam kod ile başlıyorsa (örn. "USD ..." varyasyonları) → ilgili kodu döndür.
    for code in CURRENCY_FROM_PRODUCT_NAME.values():
        if key.startswith(code):
            return code
    return None


class InsufficientFXBalanceError(Exception):
    """
    FX kasada yetersiz bakiye hatası.
    InsufficientStockError'ın döviz analoğu — view katmanında HTTP 400 ile yakalanmalı.
    """

    def __init__(self, currency, available, requested, account_name=''):
        self.currency = currency
        self.available = Decimal(str(available))
        self.requested = Decimal(str(requested))
        self.account_name = account_name or ''
        msg = (
            f"{self.account_name + ' kasasında ' if self.account_name else ''}"
            f"yetersiz {currency} bakiyesi: "
            f"İstenen {self.requested} {currency}, "
            f"Mevcut {self.available} {currency}"
        )
        super().__init__(msg)


class FXBalanceReader:
    """
    Mağazanın FX kasalarından döviz bakiyelerini okur.
    Tek doğruluk kaynağı: Payment tablosu (bank_views._get_fx_breakdown).

    Yol 2 (SSOT) refactor'ında check_fast_stock() ve get_product_details()
    bu sınıfı kullanır.
    """

    @staticmethod
    def get_all_balances(store) -> dict:
        """
        Mağazanın tüm aktif FX kasalarındaki dövizlerin toplam bakiyesini döndürür.

        Returns:
            {'USD': Decimal('780.00'), 'EUR': Decimal('250.00'), ...}
            Boş dict, FX kasa veya bakiye yoksa.
        """
        if not store:
            return {}

        # Lazy import — dairesel import önlemi (bank_views.py PaymentService'ten import etmesin)
        from apps.banking.bank_views import _get_fx_breakdown

        fx_accounts = BankAccount.objects.filter(
            store=store,
            currency='FX',
            is_active=True,
            is_deleted=False,
        )

        totals = {}
        for acc in fx_accounts:
            breakdown = _get_fx_breakdown(acc) or {}
            for code, val_str in breakdown.items():
                try:
                    val = Decimal(str(val_str))
                except Exception:
                    val = Decimal('0')
                totals[code] = totals.get(code, Decimal('0')) + val
        return totals

    @staticmethod
    def get_balance(store, currency_code) -> Decimal:
        """
        Belirli bir döviz kodunun toplam bakiyesini döndürür.

        Args:
            store: Stores instance
            currency_code: 'USD', 'EUR' vb.

        Returns:
            Decimal — toplam bakiye (mağazanın tüm FX kasalarından toplanmış).
        """
        if not currency_code:
            return Decimal('0')
        balances = FXBalanceReader.get_all_balances(store)
        return balances.get(currency_code.upper(), Decimal('0'))

    @staticmethod
    def get_account_balance(account, currency_code) -> Decimal:
        """
        Tek bir FX kasasının belirli döviz bakiyesini döndürür.
        FXBalanceGuard tarafından spesifik kasa kontrolünde kullanılır.

        Args:
            account: BankAccount instance (currency='FX' olmalı)
            currency_code: 'USD', 'EUR' vb.

        Returns:
            Decimal — bu kasadaki belirli dövizin bakiyesi.
        """
        if not account or not currency_code:
            return Decimal('0')
        from apps.banking.bank_views import _get_fx_breakdown
        breakdown = _get_fx_breakdown(account) or {}
        try:
            return Decimal(str(breakdown.get(currency_code.upper(), '0')))
        except Exception:
            return Decimal('0')


class FXBalanceGuard:
    """
    Checkout sırasında FX kasa bakiye yeterliliğini kontrol eder.
    Yol 3 (Acil Guard) — Payment kayıtları oluşturulmadan ÖNCE çağrılır.

    Davranış:
        - SALE: Bakiye < istenen → InsufficientFXBalanceError
        - PURCHASE: Hiç çağrılmaz (alış için bakiye kontrolü yok)
    """

    @staticmethod
    def check_sufficient(fx_bank_account, currency_code, requested_amount,
                         use_lock: bool = True) -> dict:
        """
        Belirli bir FX kasada belirli bir dövizden istenen miktar var mı kontrol eder.

        Args:
            fx_bank_account: BankAccount instance (currency='FX')
            currency_code: 'USD', 'EUR' vb.
            requested_amount: Decimal — istenen döviz miktarı (pozitif)
            use_lock: True ise BankAccount satırına select_for_update kilidi konur.
                      transaction.atomic bloğu içinde çağrılmalıdır.

        Returns:
            {
                'sufficient': bool,
                'available': Decimal,
                'requested': Decimal,
                'currency': str,
                'bank_account_name': str,
            }

        Raises:
            ValueError — geçersiz hesap/parametre
        """
        if not fx_bank_account:
            raise ValueError("FX banka hesabı belirtilmedi.")
        if fx_bank_account.currency != 'FX':
            raise ValueError(
                f"Hesap FX değil (currency={fx_bank_account.currency}). "
                "FXBalanceGuard yalnızca FX kasalarda çalışır."
            )
        if not currency_code:
            raise ValueError("Döviz kodu belirtilmedi.")

        try:
            requested = Decimal(str(requested_amount))
        except Exception:
            raise ValueError(f"Geçersiz miktar: {requested_amount}")

        # Race condition koruması: aynı anda iki SALE çakışmasın
        if use_lock:
            BankAccount.objects.select_for_update().filter(pk=fx_bank_account.pk).first()

        available = FXBalanceReader.get_account_balance(fx_bank_account, currency_code)
        sufficient = available >= requested

        return {
            'sufficient': bool(sufficient),
            'available': available,
            'requested': requested,
            'currency': currency_code.upper(),
            'bank_account_name': fx_bank_account.name or '',
        }

    @staticmethod
    def assert_sufficient(fx_bank_account, currency_code, requested_amount,
                          use_lock: bool = True) -> None:
        """
        check_sufficient ile aynı, ancak yetersizse InsufficientFXBalanceError fırlatır.
        View katmanı için ergonomik kullanım.
        """
        result = FXBalanceGuard.check_sufficient(
            fx_bank_account=fx_bank_account,
            currency_code=currency_code,
            requested_amount=requested_amount,
            use_lock=use_lock,
        )
        if not result['sufficient']:
            raise InsufficientFXBalanceError(
                currency=result['currency'],
                available=result['available'],
                requested=result['requested'],
                account_name=result['bank_account_name'],
            )
