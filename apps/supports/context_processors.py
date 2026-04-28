
from .models import RequestMessage

def support_notifications(request):

    if not request.user.is_authenticated:
        return {'unread_count': 0}


    if not request.user.is_superuser and not request.user.is_staff:
        count = RequestMessage.objects.exclude(sender_id=request.user.id).filter( is_new_message=True,request__personel_request=request.user).count()


    else:

        count = RequestMessage.objects.exclude(sender_id=request.user.id).filter( is_new_message=True,request__assigned_staff=request.user).count()


    return {
        'unread_count': count
    }
