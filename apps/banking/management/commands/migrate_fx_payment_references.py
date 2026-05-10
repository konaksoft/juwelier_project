# ============================================================================
# DOSYA: apps/banking/management/commands/migrate_fx_payment_references.py
# KONUM: Kuyum Plus (jewelery_project)
# VERSİYON: v1 — Faz 13.1: FX Payment Reference Geriye Yönelik Göç
#
# AMAÇ:
#   Faz 13 öncesi yazılmış, FX kasada bulunan ve `reference` alanı boş olan
#   Payment kayıtlarına geriye yönelik `[KOD]` etiketi yazmak.
#
#   Bu kayıtlar `_get_fx_breakdown` tarafından kur aralığı tahmini ile yanlış
#   sınıflandırılıyordu (GBP→QAR, SAR→QAR, CHF→EUR vb.). Faz 13'te
#   `_extract_fx_code_from_reference` whitelist'i SSOT'a (SUPPORTED_FX_CURRENCIES)
#   bağlandığından, reference dolduktan sonra bu kayıtlar doğru okunacaktır.
#
# STRATEJİ:
#   1. Etkilenen Payment kümesini bul: bank_account.currency='FX' AND
#      reference IS NULL/'' AND currency_amount > 0
#   2. Her kayıt için process_no → Process → Process.product zinciriyle
#      `get_currency_code_from_product()` çağır.
#   3. Doğru kod tespit edilebildiyse: `reference='[KOD] Faz 13.1 Göç'`
#   4. Tespit edilemediyse (orphan): `reference='[BELIRLENEMEDI] Faz 13.1 Göç'`
#   5. exchange_rate alanı DOKUNULMAZ — tarihsel kur korunur.
#
# KULLANIM:
#   python manage.py migrate_fx_payment_references --dry-run
#   python manage.py migrate_fx_payment_references --store-id=<UUID>
#   python manage.py migrate_fx_payment_references --batch-size=500
#
# GÜVENLİK:
#   - İdempotent: reference dolu kayıtlara dokunmaz.
#   - --dry-run: Sadece raporlama, yazma yok.
#   - --store-id: Belirli bir mağaza için sınırlandırma (test güvencesi).
#   - Batch processing: Büyük tablolarda transaction süresini sınırlar.
#   - Önce/sonra FX breakdown raporu konsola yazılır.
#
# ÖN KOŞUL:
#   Bu komuttan ÖNCE Payment tablosunun yedeği alınmış olmalıdır.
#   Komut idempotent olsa da idempotensi yalnızca aynı kod tarafından korunur;
#   kullanıcı manuel düzeltmesi yapmadıysa rollback için yedek tek savunmadır.
# ============================================================================

import logging
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Faz 13.1 — FX kasadaki referanssız Payment kayıtlarına geriye yönelik "
        "[KOD] etiketi yazar. Process→Product zinciri ile döviz kodunu tespit eder; "
        "tespit edilemeyenler [BELIRLENEMEDI] olarak işaretlenir. exchange_rate "
        "alanına dokunmaz."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            default=False,
            help='Yazma yapmaz; yalnızca etkilenecek kayıt sayısını ve önce/sonra '
                 'tahmini bakiye dağılımını raporlar.',
        )
        parser.add_argument(
            '--store-id',
            type=str,
            default=None,
            help='Yalnızca belirli bir mağazanın FX kasalarını işle (UUID). '
                 'Verilmezse tüm mağazalar işlenir.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=500,
            help='Her transaction içinde güncellenecek kayıt sayısı (varsayılan: 500).',
        )

    # ────────────────────────────────────────────────────────────
    # Yardımcılar
    # ────────────────────────────────────────────────────────────

    def _resolve_currency_for_payment(self, payment, process_lookup):
        """
        Payment.process_no üzerinden Process kaydını bul, Process.product
        üzerinden döviz kodunu çıkar.

        Returns:
            (currency_code | None, fallback_used: bool)
              - currency_code: 'USD', 'EUR' vb. veya None (orphan ise)
              - fallback_used: True ise sentinel-rate fallback kullanıldı
        """
        from apps.banking.services import (
            get_currency_code_from_product,
            FX_SENTINEL_REVERSE_MAP,
        )

        process_no = (payment.process_no or '').strip()
        if process_no:
            process = process_lookup.get(process_no)
            if process and process.product:
                code = get_currency_code_from_product(process.product)
                if code:
                    return code, False

        # Birincil zincir başarısızsa sentinel-rate fallback dene
        rate = payment.exchange_rate
        if rate and rate > 0:
            try:
                rate_f = float(rate)
            except Exception:
                rate_f = None
            if rate_f is not None:
                for sentinel, code in FX_SENTINEL_REVERSE_MAP.items():
                    if abs(rate_f - sentinel) < 0.001:
                        return code, True

        return None, False

    def _compute_breakdown_preview(self, payments_qs):
        """
        Verilen Payment queryset için MEVCUT (Faz 13 okuma yolu) FX kırılımını
        hesapla. Konsola önce/sonra karşılaştırması basmak için kullanılır.

        NOT: Faz 13 düzeltmeleri sonrası okuma yolu — yani göç ÖNCESİ tahmini
        bakiye, kayıtlar reference'sızken bile yeni kur aralığı tablosuyla
        hesaplanır. Bu raporlama amaçlıdır; gerçek SSOT _get_fx_breakdown'dur.
        """
        from apps.banking.bank_views import (
            _extract_fx_code_from_reference,
            _guess_fx_code_from_rate,
        )

        breakdown = defaultdict(Decimal)
        for p in payments_qs:
            sign = Decimal('-1') if p.is_output else Decimal('1')
            code = _extract_fx_code_from_reference(p.reference)
            if not code and p.exchange_rate and p.exchange_rate > 0:
                code = _guess_fx_code_from_rate(p.exchange_rate)
            if not code:
                code = 'Döviz'
            breakdown[code] += sign * (p.currency_amount or Decimal('0'))
        return dict(breakdown)

    def _format_breakdown(self, breakdown):
        if not breakdown:
            return '(boş)'
        return ', '.join(
            f'{k}={v}' for k, v in sorted(breakdown.items())
        )

    # ────────────────────────────────────────────────────────────
    # Ana Akış
    # ────────────────────────────────────────────────────────────

    def handle(self, *args, **options):
        from apps.banking.models import BankAccount
        from apps.process.models import Payment, Process

        dry_run = options['dry_run']
        store_id = options.get('store_id')
        batch_size = max(1, int(options['batch_size']))

        self.stdout.write(self.style.MIGRATE_HEADING(
            '═════════════════════════════════════════════════════════════════\n'
            'Faz 13.1 — FX Payment Reference Geriye Yönelik Göç\n'
            '═════════════════════════════════════════════════════════════════'
        ))
        self.stdout.write(f'Mod: {"DRY-RUN (yazma yok)" if dry_run else "GERÇEK YAZMA"}')
        self.stdout.write(f'Batch boyutu: {batch_size}')
        if store_id:
            self.stdout.write(f'Mağaza filtresi: {store_id}')
        self.stdout.write('')

        # 1) Etki alanı: FX kasalardaki referanssız ödemeler
        fx_accounts_qs = BankAccount.objects.filter(
            currency='FX',
            is_active=True,
            is_deleted=False,
        )
        if store_id:
            fx_accounts_qs = fx_accounts_qs.filter(store_id=store_id)
        fx_account_ids = list(fx_accounts_qs.values_list('id', flat=True))

        if not fx_account_ids:
            self.stdout.write(self.style.WARNING('Hiç aktif FX kasası bulunamadı; çıkılıyor.'))
            return

        self.stdout.write(f'Aktif FX kasa sayısı: {len(fx_account_ids)}')

        target_qs = Payment.objects.filter(
            bank_account_id__in=fx_account_ids,
            is_cancelled=False,
            is_approved=True,
        ).filter(
            Q(reference__isnull=True) | Q(reference__exact=''),
        ).exclude(
            currency_amount__isnull=True,
        ).exclude(
            currency_amount=0,
        )

        total_target = target_qs.count()
        self.stdout.write(f'Etkilenecek aday Payment sayısı: {total_target}')
        if total_target == 0:
            self.stdout.write(self.style.SUCCESS(
                'Referanssız FX Payment kaydı bulunamadı. Sistem temiz.'
            ))
            return

        # 2) Önceki dağılım (yalnızca raporlama)
        all_fx_payments_before = Payment.objects.filter(
            bank_account_id__in=fx_account_ids,
            is_cancelled=False,
            is_approved=True,
        ).exclude(currency_amount__isnull=True).exclude(currency_amount=0)

        before_breakdown = self._compute_breakdown_preview(all_fx_payments_before)
        self.stdout.write('')
        self.stdout.write(self.style.HTTP_INFO('GÖÇ ÖNCESİ kırılım:'))
        self.stdout.write(f'  {self._format_breakdown(before_breakdown)}')

        # 3) Process önbelleği: aday Payment'ların process_no'ları için
        process_nos = list(
            target_qs.exclude(process_no__isnull=True)
                     .exclude(process_no__exact='')
                     .values_list('process_no', flat=True).distinct()
        )
        process_lookup = {}
        if process_nos:
            for proc in Process.objects.filter(process_no__in=process_nos).select_related('product'):
                # Aynı process_no birden fazla Process'e bağlanabilir; ilk product'lı
                # olanı tercih et.
                existing = process_lookup.get(proc.process_no)
                if existing is None or (existing.product is None and proc.product is not None):
                    process_lookup[proc.process_no] = proc

        self.stdout.write(f'Process önbelleği: {len(process_lookup)} kayıt')
        self.stdout.write('')

        # 4) Sınıflandırma + güncelleme
        stats = {
            'resolved_via_product': 0,
            'resolved_via_sentinel': 0,
            'unresolved_orphans': 0,
            'updated': 0,
            'skipped_dry_run': 0,
        }
        per_currency = defaultdict(int)

        # ID listesini topla — büyük tablolarda values_list iterator daha güvenli
        candidate_ids = list(target_qs.values_list('id', flat=True))

        for batch_start in range(0, len(candidate_ids), batch_size):
            batch_ids = candidate_ids[batch_start: batch_start + batch_size]
            batch_qs = Payment.objects.filter(id__in=batch_ids)

            updates = []
            for payment in batch_qs:
                code, fallback_used = self._resolve_currency_for_payment(
                    payment, process_lookup,
                )
                if code:
                    if fallback_used:
                        stats['resolved_via_sentinel'] += 1
                    else:
                        stats['resolved_via_product'] += 1
                    new_ref = f'[{code}] Faz 13.1 Göç'
                    per_currency[code] += 1
                else:
                    stats['unresolved_orphans'] += 1
                    new_ref = '[BELIRLENEMEDI] Faz 13.1 Göç — Manuel İnceleme Gerekli'
                    per_currency['BELIRLENEMEDI'] += 1

                updates.append((payment.id, new_ref))

            if dry_run:
                stats['skipped_dry_run'] += len(updates)
                continue

            # Gerçek yazma — her batch ayrı transaction, atomik
            with transaction.atomic():
                for pid, ref in updates:
                    Payment.objects.filter(pk=pid).update(reference=ref)
                stats['updated'] += len(updates)

            log.info(
                'Faz13.1 batch tamamlandı: %s/%s (yazılan: %d)',
                batch_start + len(batch_ids), len(candidate_ids), len(updates),
            )

        # 5) Sonuç raporu
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('SINIFLANDIRMA SONUCU'))
        self.stdout.write(f'  Process→Product zinciri ile çözülen : {stats["resolved_via_product"]}')
        self.stdout.write(f'  Sentinel-rate fallback ile çözülen   : {stats["resolved_via_sentinel"]}')
        self.stdout.write(f'  Yetim (orphan) kayıt                 : {stats["unresolved_orphans"]}')
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'  DRY-RUN: {stats["skipped_dry_run"]} kayıt yazılacaktı (yazılmadı).'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'  YAZILDI: {stats["updated"]} Payment.reference güncellendi.'
            ))

        self.stdout.write('')
        self.stdout.write('Para birimi başına atanan etiket sayısı:')
        for code in sorted(per_currency.keys()):
            self.stdout.write(f'  [{code}]: {per_currency[code]}')

        # 6) Sonraki dağılım (yalnızca yazma yapıldıysa anlamlı)
        if not dry_run:
            after_qs = Payment.objects.filter(
                bank_account_id__in=fx_account_ids,
                is_cancelled=False,
                is_approved=True,
            ).exclude(currency_amount__isnull=True).exclude(currency_amount=0)
            after_breakdown = self._compute_breakdown_preview(after_qs)
            self.stdout.write('')
            self.stdout.write(self.style.HTTP_INFO('GÖÇ SONRASI kırılım:'))
            self.stdout.write(f'  {self._format_breakdown(after_breakdown)}')

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Faz 13.1 göçü tamamlandı.'))

        # 7) Yetim kayıt uyarısı
        if stats['unresolved_orphans'] > 0:
            self.stdout.write('')
            self.stdout.write(self.style.WARNING(
                f"DİKKAT: {stats['unresolved_orphans']} adet orphan kayıt "
                "[BELIRLENEMEDI] olarak işaretlendi. Bu kayıtlar FX bakiye "
                "kırılımında 'Döviz' kovasına düşecek. Manuel inceleme için "
                "Payment.reference='[BELIRLENEMEDI] ...' filtresiyle listeleyiniz."
            ))
