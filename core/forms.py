"""
Forms for the core application.
"""
from django import forms
from django.core.exceptions import ValidationError

from .models import Author, Work


class AuthorForm(forms.ModelForm):
    """Form for creating and editing authors."""
    
    class Meta:
        model = Author
        fields = ['name', 'birth_year', 'death_year', 'wikipedia_url']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_year': forms.NumberInput(attrs={'class': 'form-control'}),
            'death_year': forms.NumberInput(attrs={'class': 'form-control'}),
            'wikipedia_url': forms.URLInput(attrs={'class': 'form-control'}),
        }
    
    def clean_death_year(self):
        birth_year = self.cleaned_data.get('birth_year')
        death_year = self.cleaned_data.get('death_year')
        
        if birth_year and death_year and death_year <= birth_year:
            raise ValidationError("Death year must be after birth year.")
        
        return death_year


class WorkForm(forms.ModelForm):
    """Form for creating and editing works."""
    
    class Meta:
        model = Work
        fields = ['title', 'author', 'publication_year', 'form', 'wikipedia_url']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'author': forms.Select(attrs={'class': 'form-control'}),
            'publication_year': forms.NumberInput(attrs={'class': 'form-control'}),
            'form': forms.TextInput(attrs={'class': 'form-control'}),
            'wikipedia_url': forms.URLInput(attrs={'class': 'form-control'}),
        }


class SearchForm(forms.Form):
    """Form for search functionality."""

    q = forms.CharField(
        max_length=255,
        widget=forms.TextInput(attrs={
            'class': 'search-input',
            'placeholder': 'Search...'
        }),
        label='Search'
    )
