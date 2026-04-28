# örnek: apps/process/utils.py
from django.template.loader import render_to_string
from weasyprint import HTML
from io import BytesIO
import os
from django.conf import settings

def build_transaction_pdf(process, *, template_name="emails/transaction_detail.html") -> str:
    """
    E-posta HTML'inden PDF üretir ve /media/tmp/ altına kaydedip dosya yolunu döner.
    process: senin işlem objen (context'i sen doldur)
    """
    ctx = {
        "subject": f"İşlem #{process.number}",
        "customer": process.customer,
        "date_str": process.date.strftime("%d.%m.%Y"),
        "process_no": process.number,
        "items": process.items,           # kendi queryset'in
        "payments": process.payments_ctx, # dict'e dönüştürülmüş hali
        "totals": process.totals_ctx,     # dict
        "message_intro": "İşleminizin özeti aşağıdadır.",
    }
    html = render_to_string(template_name, ctx)
    pdf_bytes = HTML(string=html, base_url=str(settings.BASE_DIR)).write_pdf()

    out_dir = os.path.join(settings.MEDIA_ROOT, "tmp")
    os.makedirs(out_dir, exist_ok=True)
    file_path = os.path.join(out_dir, f"islem_{process.number}.pdf")
    with open(file_path, "wb") as f:
        f.write(pdf_bytes)
    return file_path