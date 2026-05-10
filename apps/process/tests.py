"""
FAZ 18.6: POS Komisyon + Cari Bakiye Test Suite

Test Senaryosu:
  10.000 TL'lik bir sepet. %5 komisyonlu POS cihazı. Müşteriden 10.500 TL kart tahsilatı.
  POS komisyonu müşterinin cari bakiyesini ETKİLEMEMELİDİR.

Assertions:
  1. İşlem başarıyla kaydedilmeli.
  2. Payment: amount=10500, commission_amount=500, net_amount=10000
  3. Müşterinin cari bakiyesinde (payable_hs, receivable_hs) değişiklik OLMAMALI.
"""

import uuid
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model

from apps.process.models import Payment, Process
from apps.products.models import Products
from apps.customers.models import Customers
from apps.stores.models import Stores, Company
from apps.banking.models import BankAccount, POSCommissionRate
from apps.settings.models import StoreConfiguration
from apps.stock_management.models import StockSnapshot

User = get_user_model()


class POSCommissionBalanceTest(TestCase):
    """
    FAZ 18.6: POS komisyonu ödenen tutara dahilken,
    müşterinin cari bakiyesini etkilemediğini doğrular.
    """

    def setUp(self):
        """Test veritabanı kurulumu"""
        # Şirket
        self.company = Company.objects.create(
            company_name='Test Kuyumcu A.Ş.',
            tax_number='1234567890',
        )

        # Mağaza
        self.store = Stores.objects.create(
            store_id='TEST001',
            company=self.company,
            title='Test Mağazası',
        )

        # Mağaza konfigürasyonu
        self.config = StoreConfiguration.objects.create(
            store=self.store,
            enforce_cash_limit=False,
            enforce_invoice_customer=False,
            enforce_masak_identity=False,
            is_safe_approval_required=False,
        )

        # Kullanıcı
        self.user = User.objects.create_user(
            username='test_kuyumcu',
            password='testpass123',
        )
        self.user.store = self.store
        self.user.save()

        # Has Altın ürünü (fiyat referansı)
        self.has_product = Products.objects.create(
            name='Has Altın 24 Ayar',
            sale_price_eur=Decimal('3500.00'),
            buy_price_eur=Decimal('3400.00'),
            sale_price_hs=Decimal('1.000'),
            buy_price_hs=Decimal('1.000'),
            is_gram_bullion=True,
            store=self.store,
        )

        # Test ürünü (satılacak ürün)
        self.product = Products.objects.create(
            name='Test Altın Bilezik',
            sale_price_eur=Decimal('10000.00'),
            buy_price_eur=Decimal('8000.00'),
            sale_price_hs=Decimal('2.857'),
            buy_price_hs=Decimal('2.353'),
            is_gram_bullion=False,
            store=self.store,
        )

        # Stok snapshot
        StockSnapshot.objects.create(
            product=self.product,
            store=self.store,
            stock_pieces=10,
            stock_gram=Decimal('0.000'),
        )

        # POS hesabı (%5 komisyon)
        self.pos_account = BankAccount.objects.create(
            store=self.store,
            name='Test POS',
            account_type='POS',
            currency='TRY',
            is_active=True,
        )

        # POS komisyon oranı (%5 tek çekim)
        POSCommissionRate.objects.create(
            bank_account=self.pos_account,
            card_type='GENERIC',
            installment_count=1,
            commission_rate=Decimal('5.00'),
            maturity_days=1,
        )

        # Müşteri (başlangıç bakiyesi sıfır)
        self.customer = Customers.objects.create(
            first_name='Test',
            last_name='Müşteri',
            payable_hs=Decimal('0.00'),
            receivable_hs=Decimal('0.00'),
        )

        # Nakit kasası
        self.cash_account = BankAccount.objects.create(
            store=self.store,
            name='Test TRY Kasası',
            account_type='CASH',
            currency='TRY',
            is_active=True,
        )

        self.factory = RequestFactory()

    def test_pos_commission_does_not_create_customer_credit(self):
        """
        10.000 TL sepet + %5 POS komisyonu = 10.500 TL kart çekimi.
        Müşterinin cari bakiyesi DEĞİŞMEMELİ.
        """
        from apps.process.fast_views import _process_payments_and_balances

        # Müşterinin başlangıç bakiyesini kaydet
        initial_payable = Decimal(str(self.customer.payable_hs))
        initial_receivable = Decimal(str(self.customer.receivable_hs))

        # 10.500 TL kart ödemesi (10.000 sepet + 500 komisyon)
        post_data = {
            'is_manual_payment': 'true',
            'manual_cash': '0',
            'manual_card': '10500.00',  # Komisyon dahil tutar
            'manual_transfer': '0',
            'bank_account_cash': '',
            'bank_account_card': str(self.pos_account.id),
            'bank_account_transfer': '',
            'installment_count': '1',
            'commission_rate': '5.00',
            'commission_amount': '500.00',  # POS komisyon tutarı
            'net_amount': '10000.00',
            'maturity_date': '',
        }

        request = self.factory.post('/process/add-fast/', post_data)
        request.user = self.user

        process_no = f'TST-{uuid.uuid4().hex[:8].upper()}'
        total_amount = Decimal('10000.00')  # Sepet tutarı (komisyonsuz)

        # Mock PriceService
        with patch('apps.process.fast_views.PriceService') as mock_price:
            mock_price.get_price.return_value = {
                'sell_tl': '3500.00',
                'buy_tl': '3400.00',
            }

            paid_total, eff_type, cash, card, transfer = _process_payments_and_balances(
                request=request,
                process_no=process_no,
                total_amount=total_amount,
                operation_type='SALE',
                is_manual=True,
                is_pos_flow=False,
                payment_type='MANUAL',
                pos_mode='',
                customer=self.customer,
                hs_rate_sale_eur=Decimal('3500.00'),
                hs_rate_buy_eur=Decimal('3400.00'),
                user=self.user,
            )

        # ── ASSERTION 1: İşlem başarılı ──
        self.assertEqual(paid_total, Decimal('10500.00'),
                         "paid_total komisyon dahil 10.500 TL olmalı")

        # ── ASSERTION 2: Payment kaydı doğru ──
        payment = Payment.objects.filter(
            process_no=process_no,
            payment_type='CREDIT_CARD',
        ).first()

        self.assertIsNotNone(payment, "Kart Payment kaydı oluşturulmalı")
        self.assertEqual(payment.amount, Decimal('10500.00'),
                         "Payment.amount = 10.500 TL (komisyon dahil)")
        self.assertEqual(payment.commission_amount, Decimal('500.00'),
                         "Payment.commission_amount = 500 TL")
        self.assertEqual(payment.net_amount, Decimal('10000.00'),
                         "Payment.net_amount = 10.000 TL")

        # ── ASSERTION 3 (EN ÖNEMLİ): Müşteri bakiyesi DEĞİŞMEMELİ ──
        self.customer.refresh_from_db()
        self.assertEqual(
            Decimal(str(self.customer.payable_hs)), initial_payable,
            f"Müşterinin borcu değişmemeli (beklenen: {initial_payable}, "
            f"gerçekleşen: {self.customer.payable_hs})"
        )
        self.assertEqual(
            Decimal(str(self.customer.receivable_hs)), initial_receivable,
            f"Müşterinin alacağı değişmemeli (beklenen: {initial_receivable}, "
            f"gerçekleşen: {self.customer.receivable_hs}). "
            f"Komisyon bedeli alacak olarak YAZILMAMALI!"
        )

    def test_partial_payment_with_commission_creates_correct_debt(self):
        """
        10.000 TL sepet. Müşteri 5.250 TL kart ödüyor (%5 komisyon = 250 TL).
        Net ödeme: 5.000 TL. Kalan: 5.000 TL → Borç olarak yazılmalı.
        Komisyon (250 TL) borcu ETKİLEMEMELİ.
        """
        from apps.process.fast_views import _process_payments_and_balances

        post_data = {
            'is_manual_payment': 'true',
            'manual_cash': '0',
            'manual_card': '5250.00',
            'manual_transfer': '0',
            'bank_account_cash': '',
            'bank_account_card': str(self.pos_account.id),
            'bank_account_transfer': '',
            'installment_count': '1',
            'commission_rate': '5.00',
            'commission_amount': '250.00',
            'net_amount': '5000.00',
            'maturity_date': '',
        }

        request = self.factory.post('/process/add-fast/', post_data)
        request.user = self.user

        process_no = f'TST-{uuid.uuid4().hex[:8].upper()}'
        total_amount = Decimal('10000.00')

        with patch('apps.process.fast_views.PriceService') as mock_price:
            mock_price.get_price.return_value = {
                'sell_tl': '3500.00',
                'buy_tl': '3400.00',
            }

            _process_payments_and_balances(
                request=request,
                process_no=process_no,
                total_amount=total_amount,
                operation_type='SALE',
                is_manual=True,
                is_pos_flow=False,
                payment_type='MANUAL',
                pos_mode='',
                customer=self.customer,
                hs_rate_sale_eur=Decimal('3500.00'),
                hs_rate_buy_eur=Decimal('3400.00'),
                user=self.user,
            )

        self.customer.refresh_from_db()

        # 10.000 - (5.250 - 250) = 5.000 TL eksik ödeme → Borç
        # 5.000 / 3.500 (satış kuru) = 1.429 gr has altın borç
        expected_debt_hs = Decimal('5000') / Decimal('3500')
        actual_payable = Decimal(str(self.customer.payable_hs))

        self.assertGreater(actual_payable, Decimal('0'),
                           "Eksik ödeme olduğu için borç yazılmalı")
        self.assertAlmostEqual(
            float(actual_payable),
            float(expected_debt_hs),
            places=2,
            msg=f"Borç ~{expected_debt_hs:.3f} gr Has olmalı (komisyon hariç hesap)"
        )

        # Alacak sıfır kalmalı
        self.assertEqual(
            Decimal(str(self.customer.receivable_hs)), Decimal('0.00'),
            "Komisyon alacak olarak yazılmamalı"
        )

    def test_cash_payment_no_commission_no_balance_change(self):
        """
        10.000 TL sepet. Müşteri 10.000 TL nakit ödüyor (komisyon yok).
        Bakiye değişmemeli — regresyon testi.
        """
        from apps.process.fast_views import _process_payments_and_balances

        post_data = {
            'is_manual_payment': 'false',
            'paymentType': 'CASH',
            'bank_account_cash': str(self.cash_account.id),
            'bank_account_card': '',
            'bank_account_transfer': '',
            'installment_count': '1',
            'commission_rate': '',
            'commission_amount': '',
            'net_amount': '',
            'maturity_date': '',
        }

        request = self.factory.post('/process/add-fast/', post_data)
        request.user = self.user

        process_no = f'TST-{uuid.uuid4().hex[:8].upper()}'

        with patch('apps.process.fast_views.PriceService') as mock_price:
            mock_price.get_price.return_value = {
                'sell_tl': '3500.00',
                'buy_tl': '3400.00',
            }

            _process_payments_and_balances(
                request=request,
                process_no=process_no,
                total_amount=Decimal('10000.00'),
                operation_type='SALE',
                is_manual=False,
                is_pos_flow=False,
                payment_type='CASH',
                pos_mode='',
                customer=self.customer,
                hs_rate_sale_eur=Decimal('3500.00'),
                hs_rate_buy_eur=Decimal('3400.00'),
                user=self.user,
            )

        self.customer.refresh_from_db()
        self.assertEqual(Decimal(str(self.customer.payable_hs)), Decimal('0.00'))
        self.assertEqual(Decimal(str(self.customer.receivable_hs)), Decimal('0.00'))
