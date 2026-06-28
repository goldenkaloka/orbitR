def is_valid_tz_phone(phone_number):
    """Validate a Tanzanian phone number."""
    if not phone_number:
        return False
    p = str(phone_number).replace('+', '').strip()
    if p.startswith('255'): p = p[3:]
    if p.startswith('0'): p = p[1:]
    if not p.isdigit() or len(p) != 9:
        return False
    prefix = p[:2]
    valid_prefixes = {'74', '75', '76', '73', '65', '67', '71', '72', '68', '69', '78', '79', '61', '62'}
    return prefix in valid_prefixes
