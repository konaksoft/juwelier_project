## BankAccount Model

- `AccountType`: POS, BANK, CASH
- `BankAccount.currency`: `max_length=10, default='TRY'` — choices constraint YOK, dinamik büyür
- FX Sentinel exchange_rate değerleri: USD=0.01, EUR=0.02, GBP=0.03, CAD=0.04, QAR=0.05, SAR=0.06
- FX kasası tespiti: `account.currency == 'FX'` veya sentinel exchange_rate kontrolü

## Payment Model

- `is_cancelled`, `cancelled_at`
- `commission_rate_applied`, `commission_amount`, `net_amount`, `maturity_date`
- `installment_count`
- Payment tablosunda `store` alanı YOK (`Payment.objects.create()` çağrılarında `store=...` geçirilmez)

## Payment SSOT Kuralı

- Tüm kasa bakiyeleri `Payment` tablosundan hesaplanır: `total_in - total_out`
- Filtre: `is_cancelled=False`
- `get_bank_balance_qs(store)` her zaman `is_cancelled=False` filtresi kullanır
- is_approved=False kayıtlar bakiyeye dahil edilmez

## FX Bakiye Hesabı

- `_get_fx_breakdown(fx_bank_account)` — Payment.reference prefix'lerinden ([USD], [EUR]) FX bakiye kırılımı
- String döndürür — karşılaştırma için Decimal'e çevrilmeli
- `FXBalanceReader.get_all_balances(store)` — tüm FX bakiyeleri
- `FXBalanceReader.get_balance(store, code)` — belirli döviz kodu
- `BankAccount.objects.select_for_update()` ile race condition koruması

## FXBalanceGuard

- `FXBalanceGuard.check_sufficient(fx_bank_account, currency_code, requested_amount, use_lock=True)`
- `FXBalanceGuard.assert_sufficient()` — yetersizse InsufficientFXBalanceError fırlatır
- Guard SADECE SALE (çıkış) için çalışır, PURCHASE (giriş) için ÇALIŞMAZ
- `transaction.atomic` bloğu içinde çağrılmalı

## InsufficientFXBalanceError

```
class InsufficientFXBalanceError(Exception):
    def __init__(self, currency, available, requested, account_name):
        self.currency = currency
        self.available = available
        self.requested = requested
        self.account_name = account_name
```
- HTTP 400 döndürülür (500 değil, iş hatası)
- `error_code: 'INSUFFICIENT_FX_BALANCE'`

## Döviz Ürünleri

- `is_currency=True` flag: USDTRY, EURTRY gibi ürünleri altın/takıdan ayırır
- `CURRENCY_FROM_PRODUCT_NAME = {'USDTRY': 'USD', 'EURTRY': 'EUR', ...}`
- `get_currency_code_from_product(product)` yardımcı fonksiyon
- is_currency=True → has×kur çarpımı YAPILMAZ, `final_buy_tl = buy_price_tl` direkt
- StockSnapshot: is_currency ürünler için stok bilgisi ölü veri — bakiye Payment'tan okunur
- `daily_stock_integrity_check` is_currency=True ürünleri EXCLUDES

## check_fast_stock() FX Branch

- `mode='FX_BALANCE'`: stok yerine FX bakiye kontrolü
- `mode='FX_BYPASS'`: PURCHASE işlemlerinde kontrol atlanır
- `get_product_details()`: FX ürünler için `fx_currency` + `fx_balance` alanları döner; `stock_pieces=0`

## UX/Frontend FX Kuralları

- Döviz SALE için: `pointer-events:none` KALDIRILDI; sadece `opacity:0.6 + kırmızı metin` — backend guard yakalar
- `hsPerGram` bloğu Döviz kategorisi için bypass edilir
- retail `openPopup()` FX branch: `salePriceTl/buyPriceTl` → hs inputs (1:1 oran)
- `get_categories_with_products()` da FX bakiye alır (retail asimetri düzeltmesi)

## Virman (Transfer)

- `process_no` format: `VIR-YYYYMMDDHHmmSS`
- `cancel_row()` / `cancel_group()`: `Payment.is_cancelled=True` atomik olarak set eder

## POSCommissionService

- `calculate()`: fallback zinciri → exact → generic_card → single_fallback → generic_single → %0 (none)
- `tolerance_fallback` KALDIRILDI — artık %0 varsayılan
- `get_rates_for_account()`, `save_rate()`, `delete_rate(is_active=False)`
- Backend `POSCommissionService.calculate()` yeniden çağrılır — frontend'den gelen net_amount, commission_rate, commission_amount KABUL EDİLMEZ (güvenlik)
- Hesaplama başarısız olursa %0 komisyon ile devam edilir

## Toptancı Kasa Entegrasyonu (FAZ 23)

- `Process.bank_account FK` — kasa kalemleri için; `product=None` olduğunda kasa kalemidir
- `Process.payment_currency` — FX kasası için döviz kodu; normal kasalarda boş
- Migration: `0011_process_bank_account_payment_currency.py`
- Bakiye kontrolü SADECE ÇIKIŞ (SALE) için; GİRİŞ (PURCHASE) için kontrol YOK
- Döviz ürünleri ürün listesinden SADECE GİRİŞ yapılabilir; ÇIKIŞ butonu disable
- Döviz çıkışı SADECE Kasalar kategorisinden kasa seçilerek yapılır (çift kayıt riski önlemi)
- "Kasalar" sanal kategorisi: sadece frontend JS'te oluşturulur, DB'ye eklenmez; `data-kasalar="1"` ile ayrıştırılır
- `add_wholesale_cash_item()`: bakiye kontrolü SADECE ÇIKIŞ için; GİRİŞ'te bypass
- `complete_process_wholesale()`: kasa kalemi → Payment + SupplierLedger; ürün kalemi → StockLedger + SupplierLedger + Fatura
- PURCHASE → is_output=False + ENTRY; SALE → is_output=True + EXIT
- `cancel_row()`: Process CANCELED + SupplierLedger is_active=False + Payment is_cancelled=True

## FX Breakdown Endpoint

- `GET /banking/fx-breakdown?account_id=X`
- Response: `{"result": true, "data": [{"currency": "USD", "balance": 1250.00}, ...]}`
- `openFxSelector()` → bu endpoint'i çağırır

## Defense in Depth (3 Katman)

- Katman 1: UI disabled (opacity:0.6 + kırmızı metin)
- Katman 2: AJAX check_fast_stock (FX_BALANCE mode)
- Katman 3: Checkout FXBalanceGuard.assert_sufficient()
- Yol 2 (SSOT) tamamlandıktan sonra da guard kalır — defense in depth

## Process State Machine

- `IN_PROGRESS` → Sepette (düzenlenebilir)
- `COMPLETED` → Tamamlanmış (stok/cari işlenmiş)
- `CANCELED` → İptal edilmiş (geri alınmış)

## Toptancı Endpoint Haritası

| URL | Açıklama |
|-----|----------|
| `/process/add-wholesale-cash-item` POST | Kasa kalemini sepete ekle |
| `/process/complete-process-wholesale` POST | Tüm sepeti tamamla |
| `/banking/bank-accounts?include_balance=true` GET | Bakiyeli hesap listesi |
| `/banking/fx-breakdown?account_id=X` GET | FX döviz bazlı bakiye |
| `/process/get-sales-wholesale` GET | Sepet DataTable verileri |
