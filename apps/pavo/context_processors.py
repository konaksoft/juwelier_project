from apps.pavo.models import PavoTerminal


def pavo_terminal(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"pavo_terminal": None}
    store_id = getattr(request.user, "store_id", None)
    if not store_id:
        return {"pavo_terminal": None}
    term = (
        PavoTerminal.objects.filter(store_id=store_id, is_active=True)
        .order_by("-updated_at")
        .first()
    )
    return {"pavo_terminal": term}
