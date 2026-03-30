import string


_SEQUENCES = (
    string.ascii_lowercase,
    string.digits,
    "qwertyuiop",
    "asdfghjkl",
    "zxcvbnm",
)


def _has_repeated_run(text: str, run_len: int = 4) -> bool:
    if run_len <= 1:
        return False
    count = 1
    for idx in range(1, len(text)):
        if text[idx] == text[idx - 1]:
            count += 1
            if count >= run_len:
                return True
        else:
            count = 1
    return False


def _has_sequence_run(text: str, run_len: int = 4) -> bool:
    if run_len <= 1 or len(text) < run_len:
        return False

    lowered = text.lower()
    for seq in _SEQUENCES:
        reversed_seq = seq[::-1]
        for idx in range(len(lowered) - run_len + 1):
            chunk = lowered[idx : idx + run_len]
            if chunk in seq or chunk in reversed_seq:
                return True
    return False


def validate_password_rules(password: str) -> list[str]:
    errors: list[str] = []

    if len(password) < 8:
        errors.append("Minimal 8 karakter.")
    if not any(ch.isalpha() for ch in password):
        errors.append("Minimal 1 huruf.")
    if not any(ch.isdigit() for ch in password):
        errors.append("Minimal 1 angka.")
    if not any(ch.isupper() for ch in password):
        errors.append("Minimal 1 huruf kapital.")
    if not any(ch.islower() for ch in password):
        errors.append("Minimal 1 huruf kecil.")

    if _has_repeated_run(password, run_len=4) or _has_sequence_run(password, run_len=4):
        errors.append("Tidak boleh ada 4 karakter atau lebih berurutan (contoh: 1111, 1234, abcd, qwer).")

    return errors
