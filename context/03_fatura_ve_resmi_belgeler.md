## E-Fatura Genel Kurallar

- `NIHAI_TUKETICI_THRESHOLD_TL = 30,000 TL`
- 30,000 TL altı + TCKN yok → fallback 11111111111 (yabancı uyruk: 22222222222)
- 30,000 TL üstü + TCKN yok → MASAK bloğu hatası (işlem durdurulur)
- B2B (tedarikçi) + boş VKN → HER ZAMAN bloke
- `_validate_customer_for_esurec()` threshold-aware olarak yeniden yazıldı
- `send_invoice_to_gib_task()` de threshold-aware

## Celery Görevleri (E-Fatura)

- `esurec_tasks.py` → `tasks.py` olarak yeniden adlandırıldı (Celery autodiscover sadece tasks.py bulur)
- Celery Beat: `check_esurec_statuses` her 5 dakika
- Celery Beat: `banking delta` her 30 dakika
- `render_invoice_pdf_task`: PDF oluşturma Celery'ye taşındı; `MEDIA_ROOT/Invoices/pdf_cache/` içine kaydedilir
- 3 async endpoint: `POST /invoices/<id>/pdf/async`, `GET /invoices/pdf/status/<task_id>`, `GET /invoices/<id>/pdf/result`

## E-Fatura Hata Kodları

- `[00019]` hatası: e-Süreç'te seri tanımı eksik; doğru hata mesajı eklendi
- `gib_status_code='1400'`: iş mantığı başarısızlığında (sadece timeout değil)
- `retryable=False` → max_retries beklenmeden hemen `_fail_invoice_and_log` çağrılır
- Nihai tüketici belirleme: `_validate_customer_for_esurec()` + `send_invoice_to_gib_task()`

## E-Gider Pusulası Genel Kurallar

- `seller_vkn` tüm gider pusulası endpoint'lerinde zorunlu (dealer isolation)
- `_serialize_expense_voucher()`: iç içe payload (header/supplier/beneficiary/items/totals)
- Tevkifat matrahı: `withholdingTaxableAmount = net_amount` (vat_amount değil)
- `withholding_code` format: `'0009'` (%9 oranı için)

## E-Gider Pusulası Celery Görevleri

- `send_expense_voucher_to_esurec_task`
- `send_expense_voucher_to_gib_task`
- `check_expense_voucher_statuses_task`
- Celery Beat: `check_expense_voucher_statuses` her 4 saatte bir
- `_try_expense_voucher_stuck_recovery`: 30+ dakika 100/1000/1100/1200 durumunda → force 1400

## Birim Kodu Dönüşümü

- `_map_unit_to_ubl()`: GR→GRM, AD→C62, KG→KGM, CM→CMT

## PDF Endpoint'leri

| URL | Method | Açıklama |
|-----|--------|----------|
| `/invoices/<id>/pdf/async` | POST | Async PDF oluşturma başlat |
| `/invoices/pdf/status/<task_id>` | GET | Görev durumu sorgula |
| `/invoices/<id>/pdf/result` | GET | Oluşturulan PDF'i al |

## Fatura Performans

- `invoices length=-1` → server-side cap 500 kayıt
- `start<0` → normalize to 0
- `pavo/views.py`: `select_related('store','customer','supplier','process')` + `prefetch_related('items','items__product')`
