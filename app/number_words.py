"""Number-to-words for PP/CA documents (Phase 12), e.g. "(Pak Rupees Two
Hundred Forty Nine Thousand One Hundred Thirty Eight and Paisa Twelve
Only)" - matches the sample PP.doc/CA.doc's amount-in-words format.
International (thousand/million) grouping, not Indian lakh/crore -
that's what the sample documents use.
"""

_ONES = [
    "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
    "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
    "Seventeen", "Eighteen", "Nineteen",
]
_TENS = [
    "", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety",
]
_SCALES = [(1_000_000_000, "Billion"), (1_000_000, "Million"), (1_000, "Thousand"), (100, "Hundred")]


def _under_1000_to_words(n: int) -> str:
    if n == 0:
        return ""
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        tens_word = _TENS[n // 10]
        if n % 10:
            tens_word += " " + _ONES[n % 10]
        parts.append(tens_word)
    elif n > 0:
        parts.append(_ONES[n])
    return " ".join(parts)


def number_to_words(n: int) -> str:
    """Integer -> English words, e.g. 249138 -> "Two Hundred Forty Nine
    Thousand One Hundred Thirty Eight"."""
    if n == 0:
        return "Zero"
    if n < 0:
        return "Minus " + number_to_words(-n)

    parts = []
    for scale_value, scale_name in _SCALES[:-1]:  # Billion/Million/Thousand
        if n >= scale_value:
            count = n // scale_value
            parts.append(_under_1000_to_words(count) + " " + scale_name)
            n %= scale_value
    if n > 0:
        parts.append(_under_1000_to_words(n))
    return " ".join(p for p in parts if p)


def ordinal(n: int) -> str:
    """12 -> "12th", 1 -> "1st", 23 -> "23rd" (for agreement dates)."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def amount_in_words(rupees: float) -> str:
    """249138.12 -> "Pak Rupees Two Hundred Forty Nine Thousand One
    Hundred Thirty Eight and Paisa Twelve Only" (no surrounding
    parentheses - callers that want them, like the CA template, add
    their own since not every use of this needs them)."""
    whole = int(rupees)
    paisa = round((rupees - whole) * 100)
    if paisa == 100:  # rounding carried over a rupee
        whole += 1
        paisa = 0

    words = f"Pak Rupees {number_to_words(whole)}"
    if paisa:
        words += f" and Paisa {number_to_words(paisa)}"
    words += " Only"
    return words
