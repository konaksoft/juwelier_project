from django.shortcuts import render
from django.http import JsonResponse
from .models import *

def location_selector_view(request):
    """Ana sayfa: Dropdownları gösterir"""
    return render(request, 'management/definitions/locations/selector.html')

def get_cities(request):
    """
    Tüm şehirleri plaka koduna veya isme göre sıralayıp döner.
    """
    cities = City.objects.all().values('id', 'name', 'plate_code').order_by('plate_code', 'name')
    return JsonResponse({'data': list(cities)})

def get_districts(request):
    """Seçilen şehrin ilçelerini döner"""
    city_id = request.GET.get('city_id')

    if not city_id:
        return JsonResponse({'data': []})

    districts = District.objects.filter(city_id=city_id).values('id', 'name').order_by('name')
    return JsonResponse({'data': list(districts)})

def get_tax_offices(request):
    """
    Seçilen şehrin vergi dairelerini döner.
    ARTIK DISTRICT_ID YERİNE CITY_ID KULLANIYORUZ.
    """
    city_id = request.GET.get('city_id')

    if not city_id:
        return JsonResponse({'data': []})

    # Vergi daireleri doğrudan şehre bağlı olduğu için city_id ile filtreliyoruz
    offices = TaxOffice.objects.filter(city_id=city_id).values('id', 'code', 'name').order_by('name')
    return JsonResponse({'data': list(offices)})