import hashlib


def owner_id(issuer: str, subject: str) -> str:
    """Stable owner key: issuer + subject, never a mutable email address."""
    return hashlib.sha256((issuer + "|" + subject).encode()).hexdigest()
