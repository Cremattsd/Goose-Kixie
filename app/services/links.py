# app/services/links.py
import os
from typing import Optional

def contact_link(contact_key: str) -> Optional[str]:
    """
    Build a RealNex deep link for a contact using an env template.
    Set RN_CONTACT_URL_TEMPLATE like:
      https://app.realnex.com/crm/contacts/{key}
    """
    tmpl = os.getenv("RN_CONTACT_URL_TEMPLATE", "").strip()
    if not tmpl or "{key}" not in tmpl:
        return None
    return tmpl.replace("{key}", str(contact_key))
