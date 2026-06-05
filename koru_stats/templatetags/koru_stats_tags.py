from django import template

register = template.Library()


@register.filter
def rknum(value):
    """Formato EVE: coma de miles. Ej: 1234567 → 1,234,567"""
    try:
        v = float(value or 0)
        return f"{v:,.0f}"
    except (ValueError, TypeError):
        return value


@register.filter
def rkisk(value):
    """ISK con sufijo legible estilo EVE. Ej: 1234567890 → 1.23 B"""
    try:
        v = float(value or 0)
        if v >= 1e12:
            return f"{v/1e12:.2f} T"
        if v >= 1e9:
            return f"{v/1e9:.2f} B"
        if v >= 1e6:
            return f"{v/1e6:.2f} M"
        if v >= 1e3:
            return f"{v/1e3:.1f} K"
        return f"{v:,.0f}"
    except (ValueError, TypeError):
        return value


@register.filter
def rkm3(value):
    """m³ con coma de miles estilo EVE. Ej: 43273000 → 43,273,000 m³"""
    try:
        v = float(value or 0)
        return f"{v:,.0f} m³"
    except (ValueError, TypeError):
        return value


@register.filter
def dict_key(d, key):
    """Permite acceder a un dict por clave en templates Django."""
    try:
        return d[key]
    except (KeyError, TypeError):
        return None
