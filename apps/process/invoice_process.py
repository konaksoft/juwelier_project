import logging
from decimal import Decimal, ROUND_HALF_UP

from django.utils import timezone

# Modeller
from apps.invoices.models import *

log = logging.getLogger(__name__)


def _dec(x, q='0.01'):
    """Yerel güvenli Decimal dönüşümü."""
    try:
        return Decimal(str(x)).quantize(Decimal(q), rounding=ROUND_HALF_UP)
    except:
        return Decimal('0.00')


def create_invoice_from_process(
        store,
        customer,
        process,
        product,
        operation_type,
        is_pos_flow,
        pavo_invoice_no,
        pavo_inquiry_data,
        pavo_sale_number,
        paid_total,
        pos_reference,
        qty,
        is_gram_bullion,
        unit_price,
        labor_net
):
    """
    Bir işlem (Process) kaydına bağlı olarak Fatura ve Kalemlerini oluşturur.
    Hem Hızlı İşlem hem de Perakende modüllerinde ortak kullanılır.
    """

    # 1. Fatura Tipi ve Durumu Belirleme
    final_type = Invoice.Type.PURCHASE if operation_type == 'PURCHASE' else Invoice.Type.SALE

    # Eğer Alış işlemiyse veya POS'tan resmi fatura kesildiyse durumu ISSUED (Kesildi) yap.
    # Aksi takdirde DRAFT (Taslak) olarak kalsın.
    final_status = Invoice.Status.ISSUED if (
            operation_type == 'PURCHASE' or (is_pos_flow and pavo_invoice_no)) else Invoice.Status.DRAFT

    # 2. Sıradaki Fatura Numarasını ve Sırasını Bulma
    nxt = Invoice.next_number_for(store)
    invoice_no = nxt[0] if isinstance(nxt, (list, tuple)) and len(nxt) > 0 else None
    seq = nxt[1] if isinstance(nxt, (list, tuple)) and len(nxt) > 1 else None

    if seq is None:
        try:
            seq = int(Invoice.objects.filter(store=store).order_by('-sequence_no').values_list('sequence_no',
                                                                                               flat=True).first() or 0) + 1
        except:
            seq = 1

    # 3. Fatura Başlığını (Header) Oluşturma
    inv = Invoice.objects.create(
        store=store,
        customer=customer,
        process=process,
        invoice_no=str(invoice_no or ''),
        sequence_no=seq,
        issue_date=timezone.now(),
        invoice_type=final_type,
        status=final_status,
        currency=MoneyCurrency.TRY,
        exrate_to_try=Decimal('1.000000'),
        hs_to_try=None,
        is_einvoice=True if (is_pos_flow and pavo_invoice_no) else False,
        pavo_sale_number=pavo_sale_number or '',
        pavo_invoice_no=pavo_invoice_no or '',
        pavo_sale_data=pavo_inquiry_data if is_pos_flow else {},
        paid_total=paid_total,
        notes=("POS_REF:" + pos_reference if pos_reference else "")
    )

    # 4. Ürün Kalemini (Metal Item) Ekleme
    metal_item = InvoiceItem.objects.create(
        invoice=inv,
        product=product,
        product_name=getattr(product, 'name', '') or 'Ürün',
        barcode=getattr(product, 'barcode', '') or '',
        jewelry_type=getattr(product, 'jewelry_type', '') or '',
        is_gram_bullion=is_gram_bullion,
        quantity=_dec(qty if is_gram_bullion else Decimal(int(qty)), '0.001'),
        unit=InvoiceItem.Unit.GRAM if is_gram_bullion else InvoiceItem.Unit.PIECE,
        unit_price=_dec(unit_price, '0.001'),
        discount_rate=Decimal('0.00'),
        vat_rate=Decimal('0.00'),
        notes="KDVK 17/4-g – ham metal" if final_type == Invoice.Type.SALE else "Gider Pusulası Kalemi"
    )
    metal_item.recompute(save=True)

    # 5. İşçilik Kalemini (Labor Item) Ekleme (Varsa)
    if labor_net > 0 and final_type == Invoice.Type.SALE:
        labor_item = InvoiceItem.objects.create(
            invoice=inv,
            product=None,
            product_name=(getattr(product, 'name', 'Ürün') + " – İşçilik"),
            quantity=Decimal('1.000'),
            unit=InvoiceItem.Unit.PIECE,
            unit_price=_dec(labor_net, '0.001'),
            discount_rate=Decimal('0.00'),
            vat_rate=Decimal('20.00'),
            notes="KDVK 23/e – özel matrah (işçilik)"
        )
        labor_item.recompute(save=True)

    # Fatura Toplamlarını Güncelle
    inv.recompute_totals(save=True)

    # 6. E-Fatura Kontör Düşümü (Opsiyonel)
    try:
        st, _ = StoreEInvoiceSettings.objects.get_or_create(store=store)
        if st.enabled and inv.is_einvoice:
            st.consume(1)
    except Exception as e:
        log.warning(f"E-Fatura kontör düşümü yapılamadı: {e}")

    return inv


def create_retail_bulk_invoice(
        store,
        customer,
        processes,
        is_pos_flow=False,
        pavo_data=None,
        payment_total=Decimal('0')
):
    """
    Perakende sepetini (Process listesi) alır.

    Mantık:
    1. Listeyi 'Satışlar' (SALE, ORDER_IN) ve 'Alışlar' (PURCHASE, RETURN) olarak ayırır.
    2. Satış varsa -> Tek bir Satış Faturası oluşturur ve kalemleri ekler.
    3. Alış varsa -> Tek bir Gider Pusulası oluşturur ve kalemleri ekler.
    4. View tarafına redirect için öncelikli olarak Satış faturasını döner.
    """
    if not processes or not customer:
        return None

    if pavo_data is None:
        pavo_data = {}

    # 1. Sepeti Ayrıştır
    sales_procs = []
    purchases_procs = []

    for p in processes:
        # İptal edilmiş veya silinmiş kayıtları atla
        if p.is_deleted:
            continue

        if p.transaction_type in ['SALE', 'ORDER_IN']:
            sales_procs.append(p)
        elif p.transaction_type in ['PURCHASE', 'RETURN', 'STOCK_IN']:
            purchases_procs.append(p)

    created_sale_invoice = None
    created_purchase_invoice = None

    # 2. SATIŞLARI İŞLE (FATURA)
    if sales_procs:
        # Satış faturasında ödenen tutar, kasaya/POS'a giren paradır.
        # Eğer takas varsa, ödenen tutar fatura toplamından düşük olabilir (Bakiye Borç kalır, Gider pusulası ile kapanır).
        created_sale_invoice = _create_single_doc(
            store=store,
            customer=customer,
            processes=sales_procs,
            doc_type=Invoice.Type.SALE,
            is_pos_flow=is_pos_flow,
            pavo_data=pavo_data,
            paid_amount=payment_total if payment_total > 0 else Decimal('0')
        )

    # 3. ALIŞLARI İŞLE (GİDER PUSULASI)
    if purchases_procs:
        # Gider pusulası için POS verisi gönderilmez.
        # Gider pusulası genellikle mahsuplaşma aracıdır, paid_amount 0 geçilir, cari hesaptan düşülür.
        created_purchase_invoice = _create_single_doc(
            store=store,
            customer=customer,
            processes=purchases_procs,
            doc_type=Invoice.Type.PURCHASE,
            is_pos_flow=False,
            pavo_data={},
            paid_amount=Decimal('0')
        )

    # View'a dönülecek belge (Öncelik Satış Faturası, yoksa Gider Pusulası)
    return created_sale_invoice if created_sale_invoice else created_purchase_invoice


def _create_single_doc(store, customer, processes, doc_type, is_pos_flow, pavo_data, paid_amount):
    """
    Tekil bir belge (Fatura veya Gider Pusulası) oluşturur ve kalemleri ekler.
    """

    # Pavo Verileri
    pavo_invoice_no = pavo_data.get('invoice_no', '')
    pavo_sale_number = pavo_data.get('sale_number', '')
    pavo_inquiry_data = pavo_data.get('inquiry_data', {})

    # Durum Belirleme: Perakende işlemi bittiği an fatura kesilmiş sayılır.
    final_status = Invoice.Status.ISSUED

    # E-Fatura Kontrolü: Sadece Satış ise ve POS'tan E-Fatura kesildiyse True
    is_einvoice = bool(is_pos_flow and pavo_invoice_no and doc_type == Invoice.Type.SALE)

    with transaction.atomic():
        # Numara Alma
        nxt = Invoice.next_number_for(store)
        invoice_no = nxt[0] if isinstance(nxt, (list, tuple)) and len(nxt) > 0 else str(nxt)
        seq = nxt[1] if isinstance(nxt, (list, tuple)) and len(nxt) > 1 else 1

        # --- BAŞLIK (HEADER) OLUŞTURMA ---
        inv = Invoice.objects.create(
            store=store,
            customer=customer,
            process=None,  # Çoklu process olduğu için tek bir process'e bağlamıyoruz
            invoice_no=str(invoice_no or ''),
            sequence_no=seq or 1,
            issue_date=timezone.now(),
            invoice_type=doc_type,
            status=final_status,
            currency=MoneyCurrency.TRY,
            exrate_to_try=Decimal('1.000000'),

            # Pavo / POS Alanları
            is_einvoice=is_einvoice,
            pavo_invoice_no=pavo_invoice_no,
            pavo_sale_number=pavo_sale_number,
            pavo_sale_data=pavo_inquiry_data,

            paid_total=_dec(paid_amount),
            notes=f"Perakende {doc_type == Invoice.Type.SALE and 'Satış' or 'Alış/Gider'} İşlemi"
        )

        # --- KALEMLERİ (ITEMS) İŞLE ---
        for p in processes:
            product = p.product
            if not product:
                continue

            # Miktar belirle (Gramlı ürünse gram, değilse adet)
            qty_piece = int(p.piece or 0)
            qty_gram = _dec(p.gram, '0.001')

            is_gram_bullion = (qty_gram > 0)
            qty = qty_gram if is_gram_bullion else Decimal(qty_piece)

            # Unit Type
            unit_type = InvoiceItem.Unit.GRAM if is_gram_bullion else InvoiceItem.Unit.PIECE

            # --- TUTAR HESAPLAMALARI ---
            # Process modelindeki amount, KDV dahil son tutardır.
            process_total = _dec(p.amount)
            process_total_abs = abs(process_total)  # Alışlarda negatif olabilir, pozitife çevir

            # İşçilik Tutarı
            labor_net = _dec(getattr(p, 'labor_amount', 0) or 0)
            labor_net_abs = abs(labor_net)

            # Metal Tutarı = Toplam - İşçilik
            metal_total = process_total_abs - labor_net_abs
            if metal_total < 0: metal_total = Decimal('0')

            # Metal Birim Fiyatı
            unit_price_metal = Decimal('0')
            if qty > 0:
                unit_price_metal = _dec(metal_total / qty, '0.001')

            # --- KALEM 1: METAL (KDV 0 - Altın/Döviz KDV'den muaftır) ---
            if metal_total > 0:
                InvoiceItem.objects.create(
                    invoice=inv,
                    product=product,
                    product_name=getattr(product, 'name', '') or 'Ürün',
                    barcode=getattr(product, 'barcode', '') or '',
                    jewelry_type=getattr(product, 'jewelry_type', '') or '',
                    is_gram_bullion=is_gram_bullion,
                    quantity=_dec(qty, '0.001'),
                    unit=unit_type,
                    unit_price=_dec(unit_price_metal, '0.001'),
                    discount_rate=Decimal('0.00'),
                    vat_rate=Decimal('0.00'),  # Metalde KDV 0
                    notes="KDVK 17/4-g – külçe altın/döviz" if doc_type == Invoice.Type.SALE else "Gider Pusulası - Metal Bedeli"
                ).recompute(save=True)

            # --- KALEM 2: İŞÇİLİK (KDV 20 - Sadece Satışta ve Varsa) ---
            # Alış işlemlerinde (Gider Pusulası) işçilik kalemi genellikle ayrılmaz, toplam tutar yazılır.
            # Ancak satışta işçilik KDV'ye tabidir.
            if labor_net_abs > 0 and doc_type == Invoice.Type.SALE:
                InvoiceItem.objects.create(
                    invoice=inv,
                    product=None,  # İşçilik için stok düşülmez
                    product_name=(getattr(product, 'name', 'Ürün') + " – İşçilik Hizmeti"),
                    quantity=Decimal('1.000'),
                    unit=InvoiceItem.Unit.PIECE,
                    unit_price=_dec(labor_net_abs, '0.001'),
                    discount_rate=Decimal('0.00'),
                    vat_rate=Decimal('20.00'),  # İşçilikte KDV %20
                    notes="KDVK 23/e – özel matrah (işçilik)"
                ).recompute(save=True)

        # Faturanın alt toplamlarını ve genel toplamını güncelle
        inv.recompute_totals(save=True)

        # Kontör Düşümü (Sadece Satış ve E-Fatura ise)
        if doc_type == Invoice.Type.SALE and inv.is_einvoice:
            try:
                st, _ = StoreEInvoiceSettings.objects.get_or_create(store=store)
                if st.enabled:
                    st.consume(1)
            except Exception as e:
                log.warning(f"E-Fatura kontör hatası: {e}")

        return inv
