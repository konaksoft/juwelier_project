from django import forms
from django.forms import inlineformset_factory
from apps.crm.proposals.models import Proposals, ProposalItems


class ProposalForm(forms.ModelForm):
    class Meta:
        model = Proposals
        fields = ['lead', 'company', 'title', 'date', 'valid_until', 'status', 'currency', 'discount_amount',
                  'tax_rate', 'notes']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Teklif Başlığı'}),
            'date': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'valid_until': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),

            # Select2 entegrasyonu için data-control
            'lead': forms.Select(
                attrs={'class': 'form-select', 'data-control': 'select2', 'data-placeholder': 'Lead Seçiniz'}),
            'company': forms.Select(
                attrs={'class': 'form-select', 'data-control': 'select2', 'data-placeholder': 'Firma Seçiniz'}),

            'status': forms.Select(attrs={'class': 'form-select'}),
            'currency': forms.Select(attrs={'class': 'form-select'}),

            # js-calc sınıfı frontend hesaplamasını tetikler, step ise ondalık sayı girmeyi sağlar
            'discount_amount': forms.NumberInput(attrs={'class': 'form-control js-calc', 'step': '0.01', 'min': '0'}),
            'tax_rate': forms.NumberInput(attrs={'class': 'form-control js-calc', 'step': '0.01', 'min': '0'}),

            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
        }


class ProposalItemForm(forms.ModelForm):
    class Meta:
        model = ProposalItems
        fields = ['package', 'description', 'quantity', 'unit_price', 'maintenance_included']
        widgets = {
            # Paket seçilince fiyatı getirmek için js-package-select sınıfı
            'package': forms.Select(attrs={'class': 'form-select form-select-sm js-package-select'}),
            'description': forms.TextInput(attrs={'class': 'form-control form-control-sm'}),

            # Otomatik hesaplama için js-calc sınıfı
            'quantity': forms.NumberInput(attrs={'class': 'form-control form-control-sm js-calc', 'min': '1'}),
            'unit_price': forms.NumberInput(attrs={'class': 'form-control form-control-sm js-calc', 'step': '0.01'}),

            'maintenance_included': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


# Inline formset
ProposalItemFormSet = inlineformset_factory(
    Proposals,
    ProposalItems,
    form=ProposalItemForm,
    extra=0,  # Otomatik boş satır istemiyoruz, JS ile ekleyeceğiz
    can_delete=True
)
