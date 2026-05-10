import base64
import hashlib  # <--- EKLENDİ (Hash için)
import os
from io import BytesIO  # <--- EKLENDİ (PDF'i RAM'de oluşturmak için)
from django.views.decorators.csrf import ensure_csrf_cookie
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.core.files.base import ContentFile
# Gerekli Importlar (En üste ekleyin)
from django.core.files.storage import default_storage  # EKLENDİ
from django.core.mail import EmailMessage  # PDF gönderimi için gerekli
from django.db import transaction
from django.http import HttpResponse
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.template.loader import render_to_string
from xhtml2pdf import pisa  # <--- WeasyPrint yerine bunu kullanıyoruz

from apps.activity_logs.views import write_log
from apps.crm.packages.models import Packages, Permission, PackagePermissionMatrix
from apps.crm.proposals.models import *
# Servisinizi ve Modelleri import edin
from apps.definitions.sms_profiles.services import NetgsmService
from apps.settings.send_mail import *  # PDF gönderimi için gereklifrom django.db import transaction
from apps.settings.send_mail import EmailService
from apps.stores.models import Stores, Company
from .models import *

# IP Adresini Doğru Almak İçin Yardımcı Fonksiyon
def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip


# Sözleşme Metnini Hazırlayan Yardımcı Fonksiyon (Kod tekrarını önlemek için)
# apps/definitions/contracts/views.py

def _prepare_contract_html(process):
    """
    Veritabanındaki sözleşme şablonunu dinamik verilerle doldurur.
    Ayrıca Teklif Notlarını da ekler.
    """
    contract_text = process.contract.description or ""
    today_str = timezone.now().strftime("%d.%m.%Y")

    replacements = {
        '{{ company.title }}': process.company.title or "-",
        '{{ company.address }}': process.company.address or "-",
        '{{ company.tax_number }}': process.company.tax_number or "-",
        '{{ company.phone }}': process.company.phone or "-",
        '{{ company.email }}': process.company.email or "-",
        '{{ company.district }}': process.company.district or "",
        '{{ company.city }}': process.company.city or "",
        '{% now "d.m.Y" %}': today_str,
    }

    if process.proposal:
        p = process.proposal
        replacements['{{ proposal.no }}'] = p.proposal_no
        replacements['{{ proposal.date }}'] = p.date.strftime('%d.%m.%Y') if p.date else "-"
        replacements['{{ proposal.grand_total }}'] = f"{p.grand_total:,.2f} {p.currency}"

        # Ürün Tablosu
        items_html = """
        <table border='1' cellpadding='5' cellspacing='0' style='width:100%; border-collapse:collapse; margin:10px 0; font-size:10pt;'>
            <tr style='background-color:#f0f0f0;'>
                <th>Ürün/Hizmet</th><th style='text-align:center;'>Adet</th><th style='text-align:right;'>Tutar</th>
            </tr>
        """
        for item in p.items.all():
            items_html += f"<tr><td>{item.description}</td><td style='text-align:center;'>{item.quantity}</td><td style='text-align:right;'>{item.total_price:,.2f} {p.currency}</td></tr>"
        items_html += "</table>"
        replacements['{{ proposal.items_table }}'] = items_html

        # --- EKLENEN KISIM: NOTLAR ---
        # Eğer teklifte not varsa, bunu HTML formatında değişkene atıyoruz
        if p.notes:
            notes_html = f"""
            <div style="margin-top:10px; padding:10px; background-color:#fffbeb; border:1px solid #f59e0b; border-radius:4px;">
                <strong>ÖZEL ŞARTLAR:</strong><br>
                {p.notes.replace(chr(10), '<br>')}
            </div>
            """
        else:
            notes_html = ""  # Not yoksa boş kalsın

        replacements['{{ proposal.notes }}'] = notes_html
        # -----------------------------

    for placeholder, value in replacements.items():
        contract_text = contract_text.replace(placeholder, str(value))
        contract_text = contract_text.replace(placeholder.replace(' ', ''), str(value))

    return contract_text


def _prepare_scope_data(process):
    """
    Sözleşme için modül/paket kapsam verilerini hazırlar.

    Mantık:
    - process.package varsa → Paket bazlı müşteri: müşteri-odaklı ABC
      pattern'li yetkiler ile sözleşme kapsam tablosu (Faz 59).
    - process.package yok ama teklifte paket kalemi var → Teklif paket
      bazlı kabul edilir (Yol B / Faz 58): aynı sözleşme kapsam tablosu.
    - process.package da yok teklifte paket de yok → Paketsiz müşteri:
      sadece satın alınan modüller ve özellikleri listelenir
      (build_proposal_module_table).

    Faz 59 değişikliği: Paket bazlı senaryoda artık build_module_scope_table
    yerine build_contract_scope_table kullanılır. Eski fonksiyon SaaSModule
    M2M üzerinden gezdiği için müşteriye teknik kodlar (add_custody vb.)
    gösteriyor + müşteri-odaklı ABC permission'larının çoğunu kaçırıyordu.

    Returns:
        dict: {
            'packages': list,           # Package nesneleri (paket/sözleşme modu)
            'module_rows': list,        # Permission satırları (modül modu) /
                                        # düz permission satırları (sözleşme modu)
            'display_mode': str,        # 'package' veya 'module'
            'is_contract_view': bool,   # Faz 59: True ise template düz tablo
                                        # render eder (ABC pattern listesi)
        }
    """
    packages = []
    module_rows = []
    display_mode = 'module'
    is_contract_view = False

    if not process.proposal:
        return {
            'packages': packages,
            'module_rows': module_rows,
            'display_mode': display_mode,
            'is_contract_view': is_contract_view,
        }

    from apps.crm.packages.services import (
        build_contract_scope_table,
        build_proposal_module_table,
    )

    if process.package:
        # Paket bazlı müşteri → Sözleşme kapsam tablosu (ABC pattern)
        packages, module_rows = build_contract_scope_table()
        display_mode = 'package'
        is_contract_view = True
    else:
        # Yol B (Faz 58): process.package boş — teklif kalemlerinde paket var mı?
        proposal_package_ids = list(
            process.proposal.items.filter(package__isnull=False)
            .values_list('package_id', flat=True).distinct()
        )

        if proposal_package_ids:
            # Teklif paket içeriyor → sözleşme kapsam tablosu
            packages, module_rows = build_contract_scope_table()
            display_mode = 'package'
            is_contract_view = True
        else:
            # Saf modül teklifi → satın alınan modüller tablosu
            module_rows = build_proposal_module_table(process.proposal)
            display_mode = 'module'

    return {
        'packages': packages,
        'module_rows': module_rows,
        'display_mode': display_mode,
        'is_contract_view': is_contract_view,
    }


@ensure_csrf_cookie
def public_contract_view(request, token):
    process = get_object_or_404(ContractProcess, token=token)

    if process.status == 'SIGNED':
        return render(request, 'management/definitions/contracts/public_success.html')

    # Kapsam Verilerini Hazırla (Faz 12.8 — Paket/Modül Ayrımı)
    scope_data = _prepare_scope_data(process)

    # Sözleşme metnini hazırla
    contract_text = _prepare_contract_html(process)

    ctx = {
        'process': process,
        'company': process.company,
        'contract_html': contract_text,
        'packages': scope_data['packages'],
        'module_rows': scope_data['module_rows'],
        'display_mode': scope_data['display_mode'],
        'is_contract_view': scope_data.get('is_contract_view', False),
    }
    return render(request, 'management/definitions/contracts/public_sign_page.html', ctx)


def public_send_sms(request):
    """
    1. Adım: İmza Resmini Kaydet ve SMS Gönder (Biyometrik Veri YOK)
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'msg': 'Hatalı istek.'})

    token = request.POST.get('token')
    process = get_object_or_404(ContractProcess, token=token)
    signature_data = request.POST.get('signature_data')

    if process.status == 'SIGNED':
        return JsonResponse({'error': True, 'msg': 'Bu sözleşme zaten imzalanmış.'})

    # 1. İmzayı Kaydet
    if signature_data:
        try:
            format, imgstr = signature_data.split(';base64,')
            ext = format.split('/')[-1]
            file_name = f"{process.token}_{timezone.now().strftime('%Y%m%d%H%M%S')}.{ext}"
            data = ContentFile(base64.b64decode(imgstr), name=file_name)

            process.signature_image = data
            process.save()
        except Exception as e:
            return JsonResponse({'error': True, 'msg': f'İmza kaydedilemedi: {str(e)}'})
    else:
        return JsonResponse({'error': True, 'msg': 'Lütfen imza kutusunu imzalayınız.'})

    # 2. SMS Kodu Üret ve Gönder
    code = str(random.randint(100000, 999999))
    process.verification_code = code
    process.save()

    signer_phone = process.signer_phone
    if not signer_phone:
        return JsonResponse({'error': True, 'msg': 'Kayıtlı telefon numarası bulunamadı.'})

    netgsm = NetgsmService()
    sms_result = netgsm.send_otp(signer_phone, code)

    if sms_result['result']:
        process.sms_uuid = sms_result.get('job_id')
        process.save()

        # Telefonu maskele
        masked_phone = ""
        if len(signer_phone) >= 10:
            clean = signer_phone.replace(" ", "").replace("-", "")[-10:]
            masked_phone = f"{clean[:3]}****{clean[-3:]}"

        return JsonResponse({
            'result': True,
            'masked_phone': masked_phone
        })
    else:
        return JsonResponse({
            'result': False,
            'msg': f"SMS Gönderilemedi: {sms_result['msg']}"
        })


@transaction.atomic
def public_confirm_contract(request):
    token = request.POST.get('token')
    code = request.POST.get('code')
    process = get_object_or_404(ContractProcess, token=token)

    if process.status == 'SIGNED':
        return JsonResponse({'error': True, 'msg': 'Bu sözleşme zaten imzalanmış.'})

    if str(process.verification_code) != str(code):
        return JsonResponse({'error': True, 'msg': 'Hatalı doğrulama kodu.'})

    try:
        # ===========================================================
        # 1. ADIM: İMZA VERİLERİNİ HAZIRLA (PDF OLUŞMADAN ÖNCE!)
        # ===========================================================
        # Bu verileri PDF template'ine göndermeden önce nesne üzerine set etmeliyiz.
        # Henüz save() yapmıyoruz ama template bu değerleri RAM'den okuyacak.

        current_time = timezone.now()
        client_ip = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', 'Unknown')

        process.signed_at = current_time
        process.signer_ip = client_ip
        process.signer_user_agent = user_agent
        # sms_uuid zaten public_send_sms aşamasında kaydedilmişti, o yüzden dolu gelir.

        # ===========================================================
        # 2. ADIM: PDF OLUŞTURMA
        # ===========================================================

        # Kapsam Verilerini Hazırla (Faz 12.8 — Paket/Modül Ayrımı)
        scope_data = _prepare_scope_data(process)

        logo_base64 = None
        try:
            found_path = finders.find('theme/img/new_logo/1.png')
            if found_path:
                path = found_path[0] if isinstance(found_path, (list, tuple)) else found_path
                with open(path, "rb") as image_file:
                    logo_base64 = base64.b64encode(image_file.read()).decode('utf-8')
        except:
            pass

        contract_html_raw = _prepare_contract_html(process)

        pdf_context = {
            'process': process,
            'company': process.company,
            'contract_html': contract_html_raw,
            'packages': scope_data['packages'],
            'module_rows': scope_data['module_rows'],
            'display_mode': scope_data['display_mode'],
            'is_contract_view': scope_data.get('is_contract_view', False),
            'logo_base64': logo_base64,
            'pagesize': 'A4',
        }

        html_string = render_to_string('management/definitions/contracts/pdf_template.html', pdf_context)
        pdf_buffer = BytesIO()
        pisa_status = pisa.CreatePDF(src=html_string, dest=pdf_buffer, encoding='utf-8', link_callback=link_callback)

        if pisa_status.err:
            raise Exception("PDF oluşturulamadı.")

        pdf_value = pdf_buffer.getvalue()
        file_hash = hashlib.sha256(pdf_value).hexdigest()

        # ===========================================================
        # 3. ADIM: KAYIT İŞLEMLERİ
        # ===========================================================

        # PDF'i Diske Kaydet
        file_name = f"signed_contracts/{current_time.strftime('%Y/%m')}/Sozlesme_{process.token}.pdf"
        default_storage.save(file_name, ContentFile(pdf_value))

        # Veritabanını Güncelle (Zaten nesneye set etmiştik, şimdi diğerlerini ekleyip kaydediyoruz)
        process.status = 'SIGNED'
        process.signed_content_snapshot = contract_html_raw
        process.document_hash = file_hash
        process.save()

        # ===========================================================
        # 3.5 ADIM: MAĞAZA AKTİFLEŞTİRME (Faz 12.7 — Faz E)
        # ===========================================================
        # Sözleşme imzalandığında, ilgili mağazayı aktifleştir.
        # Mağaza ContractProcess.store FK üzerinden veya
        # process.company'nin ilk mağazası üzerinden bulunur.
        try:
            _store_to_activate = process.store
            if not _store_to_activate and process.company:
                _store_to_activate = Stores.objects.filter(
                    company=process.company, is_deleted=False, is_active=False
                ).first()
            if _store_to_activate and not _store_to_activate.is_active:
                _store_to_activate.is_active = True
                _store_to_activate.save(update_fields=['is_active'])
        except Exception:
            pass  # Mağaza aktivasyonu başarısız olursa sözleşme işlemi engellenmesin

        # ===========================================================
        # 4. ADIM: MAİL GÖNDERİMİ
        # ===========================================================
        try:
            protocol = 'https' if request.is_secure() else 'http'
            domain = request.get_host()
            download_link = f"{protocol}://{domain}/contracts/download/{process.token}"

            email_subject = f"✅ İmzalı Sözleşme Nüshası - {process.contract.name}"
            email_context = {
                'subject': email_subject,
                'company': process.company,
                'contract_name': process.contract.name,
                'signed_at': process.signed_at,
                'signer_ip': process.signer_ip,
                'document_hash': file_hash,
                'download_link': download_link
            }
            html_message = render_to_string('management/mail_templates/contract_signed_email.html', email_context)

            email = EmailMessage(
                subject=email_subject,
                body=html_message,
                from_email=settings.EMAIL_HOST_USER,
                to=[process.company.email],
            )
            email.content_subtype = "html"
            email.attach(f"Sozlesme_{process.company.title}.pdf", pdf_value, 'application/pdf')
            email.send(fail_silently=False)

        except Exception as mail_err:
            print(f"Otomatik Mail Hatası: {str(mail_err)}")

        if process.process_type == 'DEMO' and process.store:
            process.store.is_active = True
            process.store.subscription_start = timezone.now().date()
            process.store.save()

        return JsonResponse({'result': True})

    except Exception as e:
        return JsonResponse({'error': True, 'msg': f'Sistem hatası: {str(e)}'})


@login_required
def test_sms_send(request):
    """
    Admin panelinden manuel SMS testi yapmak için kullanılır.
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'msg': 'Hatalı istek. Sadece POST kabul edilir.'})

    phone = request.POST.get('phone')
    if not phone:
        return JsonResponse({'error': True, 'msg': 'Lütfen bir telefon numarası girin.'})

    try:
        netgsm = NetgsmService()

        # Test amaçlı 4 haneli rastgele kod
        test_code = str(random.randint(1000, 9999))

        # Netgsm servisini çağır
        # Not: NetgsmService artık yeni OTP yapısını kullanıyor olmalı.
        result = netgsm.send_otp(phone, test_code)

        if result.get('result') is True:
            return JsonResponse({
                'result': True,
                'msg': f'SMS Başarıyla Gönderildi! Kod: {test_code} (Job ID: {result.get("job_id")})'
            })
        else:
            # Hata mesajını kullanıcıya göster
            return JsonResponse({
                'error': True,
                'msg': f'SMS Gönderilemedi: {result.get("msg")}'
            })

    except Exception as e:
        # Beklenmeyen hataları yakala
        return JsonResponse({
            'error': True,
            'msg': f'Sistem Hatası oluştu: {str(e)}'
        })


@login_required
def contract_view(request):
    """
    Ana Sayfa: Listeleme ve Modal HTML'ini barındırır.
    """
    context = {
        'title': 'Sözleşme Tanımları',
    }

    write_log(request, 'Sözleşmeler', 'Sözleşmeler sayfası görüntülendi.')
    return render(request, 'management/definitions/contracts/index.html', context)


@login_required
def get_all_contracts(request):
    """
    DataTable için Server-Side veri kaynağı.
    """
    draw = int(request.GET.get('draw', 1))
    length = int(request.GET.get('length', 25))
    start = int(request.GET.get('start', 0))
    search_value = request.GET.get('search[value]', '').strip()
    order_column_index = int(request.GET.get('order[0][column]', 0))
    order_dir = request.GET.get('order[0][dir]', 'desc')

    columns = ['id', 'name', 'description', 'created_at', 'is_active']
    order_col = columns[order_column_index] if order_column_index < len(columns) else 'created_at'

    if order_dir == 'desc':
        order_col = '-' + order_col

    queryset = Contracts.objects.filter(is_deleted=False)

    if search_value:
        queryset = queryset.filter(
            Q(name__icontains=search_value) |
            Q(description__icontains=search_value)
        )

    total = queryset.count()

    if length == -1:
        data = queryset.order_by(order_col)
    else:
        data = queryset.order_by(order_col)[start: start + length]

    data_list = []
    for item in data:
        data_list.append({
            'id': str(item.id),
            'name': item.name,
            'description': item.description,
            'created_at': item.created_at.strftime('%Y-%m-%d %H:%M') if item.created_at else '',
            'is_active': item.is_active,
        })

    return JsonResponse({
        "draw": draw,
        "recordsFiltered": total,
        "recordsTotal": total,
        "data": data_list
    })


@login_required
def save_contract(request):
    if request.method != 'POST':
        return JsonResponse({'error': True, 'error_msg': 'Geçersiz istek.'})

    record_id = request.POST.get('record_id')
    name = request.POST.get('name')
    description = request.POST.get('description')

    try:
        if record_id:
            record = Contracts.objects.get(id=record_id)
            record.name = name
            record.description = description
            record.save()
            write_log(request, 'Sözleşmeler', f'Sözleşme güncellendi: {record.name}')
        else:
            record = Contracts.objects.create(name=name, description=description)
            write_log(request, 'Sözleşmeler', f'Sözleşme eklendi: {record.name}')

        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'error': True, 'error_msg': str(e)})


@login_required
def delete_contract(request):
    if request.method != 'POST':
        return JsonResponse({'error': True})

    ids = request.POST.getlist('ids[]')
    try:
        Contracts.objects.filter(id__in=ids).update(is_deleted=True)
        write_log(request, 'Sözleşmeler', f'{len(ids)} adet sözleşme silindi.')
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'error': True, 'error_msg': str(e)})


@login_required
def change_status_contract(request):
    ids = request.POST.getlist('ids[]')
    try:
        records = Contracts.objects.filter(id__in=ids)
        for r in records:
            r.is_active = not r.is_active
            r.save()
        return JsonResponse({'result': True})
    except Exception as e:
        return JsonResponse({'error': True, 'error_msg': str(e)})


@login_required
def start_contract_process(request):
    """
    Sözleşme Sürecini Başlatan Ana Fonksiyon.
    Sözleşme metnini snapshot olarak alır.
    Domain adresi settings.py üzerinden dinamik gelir.
    """
    if request.method != 'POST':
        return JsonResponse({'error': True, 'msg': 'Hatalı istek'})

    record_id = request.POST.get('store_id')
    contract_id = request.POST.get('contract_id')
    proposal_id = request.POST.get('proposal_id')

    if not record_id or not contract_id:
        return JsonResponse({'error': True, 'msg': 'Eksik parametre.'})

    contract_record = get_object_or_404(Contracts, id=contract_id)

    store_obj = Stores.objects.filter(id=record_id).first()
    company_obj = None
    target_object = None

    if store_obj:
        company_obj = store_obj.company
        target_email = store_obj.email
        target_phone = store_obj.phone
        target_package = store_obj.package
        target_name = store_obj.title
        target_object = store_obj
    else:
        try:
            company_obj = Company.objects.get(id=record_id)
            target_email = company_obj.email
            target_phone = company_obj.phone
            target_package = None
            target_name = company_obj.title
            target_object = company_obj
        except Company.DoesNotExist:
            return JsonResponse({'error': True, 'msg': 'Kayıt bulunamadı (Firma veya Mağaza eşleşmedi).'})

    # Mükerrer Kayıt Kontrolü
    if store_obj:
        active_exists = ContractProcess.objects.filter(
            store=store_obj, contract=contract_record, status='PENDING'
        ).exists()
    else:
        active_exists = ContractProcess.objects.filter(
            company=company_obj, store__isnull=True, contract=contract_record, status='PENDING'
        ).exists()

    if not target_email:
        return JsonResponse({'error': True, 'msg': 'Seçilen kaydın E-posta adresi bulunmuyor.'})

    proposal_obj = None
    if proposal_id:
        proposal_obj = get_object_or_404(Proposals, id=proposal_id)

    # --- SÜREÇ OLUŞTURMA ---
    process = ContractProcess.objects.create(
        company=company_obj,
        store=store_obj,
        package=target_package,
        contract=contract_record,
        proposal=proposal_obj,
        process_type='SALES',
        signer_phone=target_phone,
        # Sözleşme metninin o anki halini kopyalıyoruz
        content_snapshot=contract_record.description
    )

    # --- DEĞİŞİKLİK BURADA: Domain settings'den alınıyor ---
    # settings.APP_DOMAIN sonunda '/' olmamasına dikkat et (ör: https://kuyumplus.com)
    domain = settings.APP_DOMAIN
    sign_link = f"{domain}/contracts/public/view/{process.token}"

    subject = f"Sözleşme Onayı: {contract_record.name}"

    email_sent = EmailService.send(
        user=target_object,
        subject=subject,
        template_name='management/mail_templates/contract_invite.html',
        context={
            'subject': subject,
            'company': company_obj,
            'store_name': target_name,
            'contract_name': contract_record.name,
            'link': sign_link,
            'staff_member': request.user
        }
    )

    if email_sent:
        write_log(request, 'Sözleşmeler', f'{target_name} için "{contract_record.name}" gönderildi.')
        return JsonResponse({'result': True, 'msg': f'"{contract_record.name}" başarıyla gönderildi.'})
    else:
        return JsonResponse({'result': True,
                             'msg': 'Süreç oluşturuldu ancak e-posta ayarları nedeniyle gönderim yapılamamış olabilir.'})


def link_callback(uri, rel):
    """
    Statik dosyaları hem Local (Dev) hem de Prod ortamında bulmak için
    geliştirilmiş yol dönüştürücü.
    """
    # 1. URL başındaki /static/ veya /media/ kısımlarını temizle
    sUrl = settings.STATIC_URL  # Genellikle '/static/'
    mUrl = settings.MEDIA_URL  # Genellikle '/media/'

    # Statik dosya mı?
    if uri.startswith(sUrl):
        path = uri.replace(sUrl, "")  # '/static/fonts/...' -> 'fonts/...'

        # A) Önce STATICFILES_DIRS içini kontrol et (Geliştirme Ortamı - Local)
        # Genellikle settings.py'de tanımlı olan statik klasörleriniz
        for static_dir in settings.STATICFILES_DIRS:
            full_path = os.path.join(static_dir, path)
            if os.path.isfile(full_path):
                return full_path

        # B) Bulamazsa STATIC_ROOT içini kontrol et (Collectstatic yapılmışsa)
        if settings.STATIC_ROOT:
            full_path = os.path.join(settings.STATIC_ROOT, path)
            if os.path.isfile(full_path):
                return full_path

    # Medya dosyası mı? (İmza, yüklenen resimler vb.)
    elif uri.startswith(mUrl):
        path = uri.replace(mUrl, "")
        full_path = os.path.join(settings.MEDIA_ROOT, path)
        if os.path.isfile(full_path):
            return full_path

    # Hiçbiri değilse, belki tam yoldur, olduğu gibi dene (ama genelde yukarıdakiler yakalar)
    return uri


@login_required
def download_signed_contract_pdf(request, token):
    process = get_object_or_404(ContractProcess, token=token)

    if process.status != 'SIGNED':
        return HttpResponse("Bu sözleşme henüz imzalanmamış.", status=403)

    logo_base64 = None
    try:
        found_path = finders.find('theme/img/new_logo/1.png')
        if found_path:
            logo_path = found_path[0] if isinstance(found_path, (list, tuple)) else found_path
            with open(logo_path, "rb") as image_file:
                logo_base64 = base64.b64encode(image_file.read()).decode('utf-8')
    except:
        pass

    # Kapsam Verilerini Hazırla (Faz 12.8 — Paket/Modül Ayrımı)
    scope_data = _prepare_scope_data(process)

    contract_text = process.signed_content_snapshot if process.signed_content_snapshot else _prepare_contract_html(process)

    context = {
        'process': process,
        'company': process.company,
        'contract_html': contract_text,
        'packages': scope_data['packages'],
        'module_rows': scope_data['module_rows'],
        'display_mode': scope_data['display_mode'],
        'is_contract_view': scope_data.get('is_contract_view', False),
        'pagesize': 'A4',
        'logo_base64': logo_base64,
    }

    html_string = render_to_string('management/definitions/contracts/pdf_template.html', context)

    response = HttpResponse(content_type='application/pdf')
    filename = f"Sozlesme_{process.contract.name}_{process.company.title}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    pisa_status = pisa.CreatePDF(
        src=html_string,
        dest=response,
        encoding='utf-8',
        link_callback=link_callback
    )

    if pisa_status.err:
        return HttpResponse('PDF oluşturulurken bir hata oluştu.', status=500)

    return response


@login_required
def resend_contract_mail(request):
    """
    İmzalı sözleşmeyi, sunucudaki SMTP kullanıcısı üzerinden tekrar gönderir.
    PDF RAM'de oluşturulur ve eklenir.
    """
    process_id = request.POST.get('process_id')

    if not process_id:
        return JsonResponse({'error': True, 'msg': 'İşlem ID bulunamadı.'})

    process = get_object_or_404(ContractProcess, id=process_id)

    if process.status != 'SIGNED':
        return JsonResponse({'error': True, 'msg': 'Sadece imzalanmış sözleşmeler gönderilebilir.'})

    if not process.company.email:
        return JsonResponse({'error': True, 'msg': 'Firmanın kayıtlı bir e-posta adresi yok.'})

    try:
        # 1. PDF İÇERİK VERİLERİNİ HAZIRLA (Faz 12.8 — Paket/Modül Ayrımı)
        scope_data = _prepare_scope_data(process)

        # Logo
        logo_base64 = None
        try:
            found_path = finders.find('theme/img/new_logo/1.png')
            if found_path:
                path = found_path[0] if isinstance(found_path, (list, tuple)) else found_path
                with open(path, "rb") as image_file:
                    logo_base64 = base64.b64encode(image_file.read()).decode('utf-8')
        except:
            pass

        # Sözleşme Metni
        contract_text = process.signed_content_snapshot if process.signed_content_snapshot else _prepare_contract_html(
            process)

        pdf_context = {
            'process': process,
            'company': process.company,
            'contract_html': contract_text,
            'packages': scope_data['packages'],
            'module_rows': scope_data['module_rows'],
            'display_mode': scope_data['display_mode'],
            'is_contract_view': scope_data.get('is_contract_view', False),
            'logo_base64': logo_base64,
            'pagesize': 'A4',
        }

        # 2. PDF OLUŞTUR (RAM)
        html_string = render_to_string('management/definitions/contracts/pdf_template.html', pdf_context)
        pdf_buffer = BytesIO()

        # link_callback fonksiyonunun bu dosyada import edildiğinden/tanımlandığından emin olun
        pisa_status = pisa.CreatePDF(
            src=html_string,
            dest=pdf_buffer,
            encoding='utf-8',
            link_callback=link_callback
        )

        if pisa_status.err:
            raise Exception("PDF oluşturma hatası.")

        pdf_value = pdf_buffer.getvalue()

        # 3. MAİL GÖNDERME
        # Link oluştur
        protocol = 'https' if request.is_secure() else 'http'
        domain = request.get_host()
        download_link = f"{protocol}://{domain}/contracts/download/{process.token}"

        email_subject = f"Hatırlatma: İmzalı Sözleşme Nüshası - {process.contract.name}"

        email_context = {
            'subject': email_subject,
            'company': process.company,
            'contract_name': process.contract.name,
            'signed_at': process.signed_at,
            'signer_ip': process.signer_ip,
            'document_hash': process.document_hash or "Hash Bulunamadı",
            'download_link': download_link
        }

        html_message = render_to_string('management/mail_templates/contract_signed_email.html', email_context)

        # !!! KRİTİK DÜZELTME BURADA !!!
        # from_email olarak settings.EMAIL_HOST_USER kullanıyoruz.
        # Bu sayede 'SMTP kullanıcısı ile gönderen aynı olmalı' kuralına uyuyoruz.

        email = EmailMessage(
            subject=email_subject,
            body=html_message,
            from_email=settings.EMAIL_HOST_USER,  # <-- Kesin çözüm burası
            to=[process.company.email],
        )
        email.content_subtype = "html"

        # PDF Ekle
        filename = f"Sozlesme_{process.company.title}.pdf"
        email.attach(filename, pdf_value, 'application/pdf')

        # Gönder
        email.send(fail_silently=False)

        return JsonResponse({'result': True, 'msg': 'Mail başarıyla gönderildi.'})

    except Exception as e:
        # Hata detayını görmek için
        return JsonResponse({'error': True, 'msg': f'Hata Detayı: {str(e)}'})
