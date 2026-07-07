"""Small shared utilities."""
import re


def normalize_search_text(text: str) -> str:
    """
    Normalise a string for name search:
      - lowercase
      - fold 'ё' → 'е' (so 'Фёдоров' and 'Федоров' match)
      - collapse repeated whitespace, strip the ends
    """
    if not text:
        return ""
    s = str(text).lower().replace("ё", "е")
    s = re.sub(r"\s+", " ", s).strip()
    return s
