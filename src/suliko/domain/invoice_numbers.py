"""Invoice numbers that do not count.

Year and month of issue, a dash, and four random characters: ``2610-4F7K``.
The month tells an accountant where to file it; the suffix means nobody can tell
how many invoices a bureau has issued. Characters that read alike (0/O, 1/I/L)
are left out, since the number gets typed into a bank transfer.
"""

from __future__ import annotations

import secrets
from datetime import date

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SUFFIX_LENGTH = 4


def new_invoice_number(issued_on: date) -> str:
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(SUFFIX_LENGTH))
    return f"{issued_on:%y%m}-{suffix}"
