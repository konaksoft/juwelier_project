import mimetypes
import os
import threading
from django.utils import timezone

from django.contrib.auth.decorators import user_passes_test
from django.http import JsonResponse, HttpResponse, Http404
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST, require_GET

from apps.backups.models import *
from apps.backups.services import BackupService  # Önceki adımda yazdığımız servis


def is_superuser(user):
    return user.is_superuser


@user_passes_test(is_superuser)
@require_GET
def get_company_backups(request):
    """DataTables için yedek listesini döner"""
    company_id = request.GET.get('company_id')
    if not company_id:
        return JsonResponse({'data': []})

    # Query
    backups = CompanyBackup.objects.filter(company_id=company_id).order_by('-created_at')

    data = []
    for b in backups:
        data.append({
            'id': str(b.id),
            'created_at': b.created_at.strftime('%Y-%m-%d %H:%M'),
            'file_size': b.file_size or '0 MB',
            'created_by': b.created_by_user or '-',
            'note': b.note or '-',
            'download_url': f"/backups/download/{b.id}/"  # URL yapınıza göre düzenleyin
        })

    return JsonResponse({'data': data})


@user_passes_test(is_superuser)
@require_POST
def restore_backup(request):
    """
    Yedeği geri yükler (Mevcut veriyi siler!)
    """
    backup_id = request.POST.get('backup_id')
    company_id = request.POST.get('company_id')

    if not backup_id or not company_id:
        return JsonResponse({'result': False, 'error_msg': 'Parametreler eksik.'})

    service = BackupService(company_id)
    success, msg = service.restore_backup(backup_id)

    if success:
        return JsonResponse({'result': True, 'msg': msg})
    else:
        return JsonResponse({'result': False, 'error_msg': msg})


@user_passes_test(is_superuser)
def download_backup(request, backup_id):
    """Yedek dosyasını indirir (Browser'ı indirmeye zorlar)"""
    backup = get_object_or_404(CompanyBackup, id=backup_id)

    if not backup.backup_file:
        raise Http404("Dosya bulunamadı")

    file_path = backup.backup_file.path
    if os.path.exists(file_path):
        with open(file_path, 'rb') as fh:
            # application/force-download tarayıcıyı indirmeye zorlar
            mime_type, _ = mimetypes.guess_type(file_path)
            response = HttpResponse(fh.read(), content_type=mime_type or 'application/json')
            response['Content-Disposition'] = f'attachment; filename="{os.path.basename(file_path)}"'
            return response
    raise Http404("Fiziksel dosya sunucuda bulunamadı.")


def _run_backup_in_background(company_id, user_id, note, backup_record_id):
    """Arka planda çalışacak fonksiyon"""
    from apps.backups.models import CompanyBackup
    from apps.backups.services import BackupService
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        user = User.objects.get(id=user_id) if user_id else None
        service = BackupService(company_id)

        # Servis içindeki create_backup metodunu, dosyayı direkt kaydetmek yerine
        # içeriği döndürecek şekilde veya model objesini güncelleyecek şekilde revize etmek gerekebilir.
        # Ancak servisin mevcut yapısını bozmadan şöyle yapabiliriz:

        # Servisin create_backup metodu yeni bir kayıt oluşturuyordu.
        # Biz manuel oluşturduğumuz 'PENDING' kaydını kullanmasını veya güncellemesini sağlamalıyız.
        # Bu örnekte servisin create_backup metodunu biraz değiştirmek en doğrusu olurdu ama
        # Hızlı çözüm olarak: Servisi çağırıp, dönen sonucu bizim bekleyen kaydımıza kopyalayabiliriz.

        real_backup = service.create_backup(note=note, user=user)

        if real_backup:
            # Bekleyen kaydı güncelle (veya servisin oluşturduğu kaydı kullan)
            # Burada servisin oluşturduğu kaydı kullanmak daha kolay.
            # Bekleyen "placeholder" kaydı silebiliriz veya güncelleyebiliriz.

            pending_backup = CompanyBackup.objects.get(id=backup_record_id)

            # Servisin oluşturduğu dosya ve bilgileri bekleyen kayda aktar
            pending_backup.backup_file = real_backup.backup_file
            pending_backup.file_size = real_backup.file_size
            pending_backup.status = 'COMPLETED'
            pending_backup.save()

            # Servisin oluşturduğu mükerrer kaydı sil
            real_backup.delete()

        else:
            raise Exception("Servis yedek oluşturamadı")

    except Exception as e:
        try:
            b = CompanyBackup.objects.get(id=backup_record_id)
            b.status = 'FAILED'
            b.note = f"Hata: {str(e)}"
            b.save()
        except:
            pass


@user_passes_test(is_superuser)
@require_POST
def create_backup(request):
    company_id = request.POST.get('company_id')
    note = request.POST.get('note', '')

    if not company_id:
        return JsonResponse({'result': False, 'error_msg': 'Firma ID eksik.'})

    # 1. Bekleyen (Pending) kaydı oluştur
    backup = CompanyBackup.objects.create(
        company_id=company_id,
        note=note,
        created_by_user=str(request.user),
        status='PENDING',
        file_size='Hesaplanıyor...'
    )

    # 2. İşlemi Thread ile arka plana at
    t = threading.Thread(
        target=_run_backup_in_background,
        args=(company_id, request.user.id, note, backup.id)
    )
    t.setDaemon(True)
    t.start()

    return JsonResponse(
        {'result': True, 'msg': 'Yedekleme işlemi başlatıldı. Lütfen bekleyin...', 'backup_id': backup.id})


@user_passes_test(is_superuser)
@require_GET
def check_backup_status(request):
    """Frontend'in durumu sorması için"""
    backup_id = request.GET.get('backup_id')
    try:
        backup = CompanyBackup.objects.get(id=backup_id)
        return JsonResponse({
            'status': backup.status,
            'download_url': f"/backups/download/{backup.id}/" if backup.status == 'COMPLETED' else None
        })
    except CompanyBackup.DoesNotExist:
        return JsonResponse({'status': 'FAILED', 'error': 'Kayıt bulunamadı'})
