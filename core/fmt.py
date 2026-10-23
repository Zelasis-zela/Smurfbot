import math

# Mise en forme française des nombres : virgule décimale, espace pour les milliers, vrai signe moins
MISSING = "—"
MINUS = "−"  # U+2212


def _number(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def num(x, decimals=0):
    # 1.854 -> "1,85" (decimals=2) ; 2359 -> "2 359" ; None -> "—"
    x = _number(x)
    if x is None:
        return MISSING
    decimals = max(0, int(decimals))
    text = f"{abs(x):,.{decimals}f}".replace(",", " ").replace(".", ",")
    negative = x < 0 and any(c in "123456789" for c in text)
    return MINUS + text if negative else text


def pct(x, decimals=1):
    # 34.12 -> "34,1 %"
    text = num(x, decimals)
    return text if text == MISSING else f"{text} %"


def ratio(x, decimals=1):
    # 1.72 -> "1,7×"
    text = num(x, decimals)
    return text if text == MISSING else f"{text}×"


def signed(n, decimals=0):
    # 17 -> "+17" ; -12 -> "−12" ; 0 -> "0"
    text = num(n, decimals)
    if text == MISSING or text.startswith(MINUS) or not any(c in "123456789" for c in text):
        return text
    return "+" + text
