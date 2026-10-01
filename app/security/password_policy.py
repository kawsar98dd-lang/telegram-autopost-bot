"""Password strength policy (length + blocklist, following current NIST guidance)."""

from __future__ import annotations

MIN_LENGTH = 12
MAX_LENGTH = 128

_COMMON = {
    "password", "password1", "password123", "passw0rd", "123456789012", "1234567890123", "qwertyuiop12",
    "qwertyuiopas", "qwerty123456", "letmein12345", "welcome12345", "admin1234567", "administrator",
    "iloveyou1234", "changeme1234", "abc123456789", "trustno1trustno1", "000000000000", "111111111111",
    "aaaaaaaaaaaa", "telegram1234", "telegramposter", "autoposter123",
}


def password_problems(password: str, email: str = "") -> list[str]:
    problems: list[str] = []
    if len(password) < MIN_LENGTH:
        problems.append(f"Use at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        problems.append(f"Use at most {MAX_LENGTH} characters.")
    if problems:
        return problems
    lowered = password.lower()
    if lowered in _COMMON or any(lowered.startswith(c) and len(c) >= 10 and len(lowered) <= len(c) + 3 for c in _COMMON):
        problems.append("This password is too common.")
    if len(set(lowered)) < 5:
        problems.append("Use a greater variety of characters.")
    local = email.split("@", 1)[0].lower()
    if len(local) >= 4 and local in lowered:
        problems.append("The password must not contain your email name.")
    if lowered.isdigit():
        problems.append("Use more than digits.")
    return problems
