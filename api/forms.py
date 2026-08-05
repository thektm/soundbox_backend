from decimal import Decimal

from django import forms

from .models import PlayConfiguration


class ArtistPlayIncomeSettingsForm(forms.Form):
    """Small configuration form used by the dedicated Django admin page."""

    normal_play_income = forms.DecimalField(
        label="Income per normal play",
        help_text="Amount credited to the artist for a valid play from a normal/free listener.",
        max_digits=12,
        decimal_places=8,
        min_value=Decimal("0"),
        max_value=Decimal("9999.99999999"),
        widget=forms.NumberInput(
            attrs={
                "min": "0",
                "max": "9999.99999999",
                "step": "0.00000001",
                "inputmode": "decimal",
                "autocomplete": "off",
                "placeholder": "0.00000000",
            }
        ),
    )
    premium_play_income = forms.DecimalField(
        label="Income per premium play",
        help_text="Amount credited to the artist for a valid play from a premium listener.",
        max_digits=12,
        decimal_places=8,
        min_value=Decimal("0"),
        max_value=Decimal("9999.99999999"),
        widget=forms.NumberInput(
            attrs={
                "min": "0",
                "max": "9999.99999999",
                "step": "0.00000001",
                "inputmode": "decimal",
                "autocomplete": "off",
                "placeholder": "0.00000000",
            }
        ),
    )

    @classmethod
    def from_configuration(cls, configuration, **kwargs):
        return cls(
            initial={
                "normal_play_income": configuration.free_play_worth,
                "premium_play_income": configuration.premium_play_worth,
            },
            **kwargs,
        )

    def save(self, configuration: PlayConfiguration) -> PlayConfiguration:
        if not self.is_valid():
            raise ValueError("Cannot save an invalid artist play income form.")

        configuration.free_play_worth = self.cleaned_data["normal_play_income"]
        configuration.premium_play_worth = self.cleaned_data["premium_play_income"]
        configuration.save(
            update_fields=(
                "free_play_worth",
                "premium_play_worth",
                "updated_at",
            )
        )
        return configuration
