from apps.roles.models import *


def get_user_permissions(request):
    if not request.user.is_authenticated:
        return {'user_permissions': []}

    if request.user.is_superuser:
        return {'user_permissions': ['ALL']}

    if not hasattr(request.user, 'role_id'):
        return {'user_permissions': []}

    permissions = RoleDetail.objects.filter(
        role_id=request.user.role_id,
        status=True
    ).values_list('permission__code', flat=True)

    return {'user_permissions': list(permissions)}