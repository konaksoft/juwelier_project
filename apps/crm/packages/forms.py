from django import forms
from apps.crm.packages.models import SaaSModule


class SaaSModuleForm(forms.ModelForm):
    dependencies = forms.ModelMultipleChoiceField(
        queryset=SaaSModule.objects.filter(is_active=True),
        required=False,
        widget=forms.SelectMultiple(attrs={
            'class': 'form-select',
            'data-control': 'select2',
            'data-placeholder': 'Bağımlı modülleri seçin...',
            'data-allow-clear': 'true',
            'multiple': 'multiple',
        }),
        label='Bağımlılıklar',
        help_text='Bu modül seçildiğinde otomatik seçilmesi gereken diğer modüller.',
    )

    class Meta:
        model = SaaSModule
        fields = [
            'name', 'slug', 'description', 'icon',
            'license_price', 'currency',
            'price_monthly', 'price_yearly',
            'is_core', 'is_active', 'order',
            'dependencies',
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Örn: Perakende Satış',
            }),
            'slug': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Örn: perakende-satis',
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Modülün kısa açıklaması...',
            }),
            'icon': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'fa-solid fa-store',
            }),
            'license_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
            }),
            'currency': forms.Select(attrs={
                'class': 'form-select',
            }),
            'price_monthly': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
            }),
            'price_yearly': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01',
                'min': '0',
            }),
            'order': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': '0',
            }),
            'is_core': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
            'is_active': forms.CheckboxInput(attrs={
                'class': 'form-check-input',
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['dependencies'].queryset = SaaSModule.objects.filter(
                is_active=True
            ).exclude(pk=self.instance.pk)
