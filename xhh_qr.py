from __future__ import annotations

from html import escape


VERSION = 5
SIZE = VERSION * 4 + 17
DATA_CODEWORDS = 108
ECC_CODEWORDS = 26


def make_qr_svg(text: str, *, scale: int = 6, border: int = 4) -> str:
    data = text.encode("utf-8")
    if len(data) > 106:
        raise ValueError("QR payload is too long")
    codewords = _make_codewords(data)
    matrix = _make_matrix(codewords)
    view_size = (SIZE + border * 2) * scale
    parts = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {view_size} {view_size}" role="img" '
            f'aria-label="{escape("XiaoHeiHe login QR")}">'
        ),
        f'<rect width="{view_size}" height="{view_size}" fill="#fff"/>',
    ]
    for y, row in enumerate(matrix):
        for x, value in enumerate(row):
            if value:
                parts.append(
                    f'<rect x="{(x + border) * scale}" y="{(y + border) * scale}" '
                    f'width="{scale}" height="{scale}" fill="#111"/>',
                )
    parts.append("</svg>")
    return "".join(parts)


def _make_codewords(data: bytes) -> list[int]:
    bits: list[int] = []
    _append_bits(bits, 0b0100, 4)
    _append_bits(bits, len(data), 8)
    for value in data:
        _append_bits(bits, value, 8)

    capacity_bits = DATA_CODEWORDS * 8
    _append_bits(bits, 0, min(4, capacity_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)

    codewords = []
    for i in range(0, len(bits), 8):
        value = 0
        for bit in bits[i : i + 8]:
            value = (value << 1) | bit
        codewords.append(value)

    pad = 0xEC
    while len(codewords) < DATA_CODEWORDS:
        codewords.append(pad)
        pad ^= 0xEC ^ 0x11

    ecc = _reed_solomon_remainder(codewords, ECC_CODEWORDS)
    return codewords + ecc


def _append_bits(bits: list[int], value: int, length: int) -> None:
    for i in reversed(range(length)):
        bits.append((value >> i) & 1)


def _make_matrix(codewords: list[int]) -> list[list[bool]]:
    matrix = [[False for _ in range(SIZE)] for _ in range(SIZE)]
    reserved = [[False for _ in range(SIZE)] for _ in range(SIZE)]
    _draw_function_patterns(matrix, reserved)

    bit_values = [(codeword >> i) & 1 for codeword in codewords for i in range(7, -1, -1)]
    best_matrix: list[list[bool]] | None = None
    best_score: int | None = None
    for mask in range(8):
        candidate = [row[:] for row in matrix]
        _draw_data(candidate, reserved, bit_values, mask)
        _draw_format_bits(candidate, reserved, mask)
        score = _penalty_score(candidate)
        if best_score is None or score < best_score:
            best_score = score
            best_matrix = candidate
    return best_matrix or matrix


def _draw_function_patterns(matrix: list[list[bool]], reserved: list[list[bool]]) -> None:
    _draw_finder(matrix, reserved, 0, 0)
    _draw_finder(matrix, reserved, SIZE - 7, 0)
    _draw_finder(matrix, reserved, 0, SIZE - 7)

    for i in range(8, SIZE - 8):
        _set_function(matrix, reserved, 6, i, i % 2 == 0)
        _set_function(matrix, reserved, i, 6, i % 2 == 0)

    _draw_alignment(matrix, reserved, 30, 30)
    _reserve_format_bits(reserved)
    _set_function(matrix, reserved, 8, 4 * VERSION + 9, True)


def _draw_finder(
    matrix: list[list[bool]],
    reserved: list[list[bool]],
    left: int,
    top: int,
) -> None:
    for dy in range(-1, 8):
        for dx in range(-1, 8):
            x = left + dx
            y = top + dy
            if not (0 <= x < SIZE and 0 <= y < SIZE):
                continue
            black = (
                0 <= dx <= 6
                and 0 <= dy <= 6
                and (
                    dx in {0, 6}
                    or dy in {0, 6}
                    or (2 <= dx <= 4 and 2 <= dy <= 4)
                )
            )
            _set_function(matrix, reserved, x, y, black)


def _draw_alignment(
    matrix: list[list[bool]],
    reserved: list[list[bool]],
    cx: int,
    cy: int,
) -> None:
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            dist = max(abs(dx), abs(dy))
            _set_function(matrix, reserved, cx + dx, cy + dy, dist in {0, 2})


def _reserve_format_bits(reserved: list[list[bool]]) -> None:
    coords = []
    coords.extend((8, i) for i in range(0, 6))
    coords.extend([(8, 7), (8, 8), (7, 8)])
    coords.extend((14 - i, 8) for i in range(9, 15))
    coords.extend((SIZE - 1 - i, 8) for i in range(0, 8))
    coords.extend((8, SIZE - 15 + i) for i in range(8, 15))
    coords.append((8, SIZE - 8))
    for x, y in coords:
        if 0 <= x < SIZE and 0 <= y < SIZE:
            reserved[y][x] = True


def _draw_format_bits(
    matrix: list[list[bool]],
    reserved: list[list[bool]],
    mask: int,
) -> None:
    bits = _format_bits(mask)
    for i in range(0, 6):
        _set_function(matrix, reserved, 8, i, _get_bit(bits, i))
    _set_function(matrix, reserved, 8, 7, _get_bit(bits, 6))
    _set_function(matrix, reserved, 8, 8, _get_bit(bits, 7))
    _set_function(matrix, reserved, 7, 8, _get_bit(bits, 8))
    for i in range(9, 15):
        _set_function(matrix, reserved, 14 - i, 8, _get_bit(bits, i))

    for i in range(0, 8):
        _set_function(matrix, reserved, SIZE - 1 - i, 8, _get_bit(bits, i))
    for i in range(8, 15):
        _set_function(matrix, reserved, 8, SIZE - 15 + i, _get_bit(bits, i))
    _set_function(matrix, reserved, 8, SIZE - 8, True)


def _draw_data(
    matrix: list[list[bool]],
    reserved: list[list[bool]],
    bits: list[int],
    mask: int,
) -> None:
    bit_index = 0
    direction = -1
    x = SIZE - 1
    while x > 0:
        if x == 6:
            x -= 1
        y_range = range(SIZE - 1, -1, -1) if direction == -1 else range(SIZE)
        for y in y_range:
            for dx in range(2):
                xx = x - dx
                if reserved[y][xx]:
                    continue
                bit = bit_index < len(bits) and bits[bit_index] == 1
                bit_index += 1
                matrix[y][xx] = bit ^ _mask(mask, xx, y)
        direction *= -1
        x -= 2


def _set_function(
    matrix: list[list[bool]],
    reserved: list[list[bool]],
    x: int,
    y: int,
    value: bool,
) -> None:
    matrix[y][x] = value
    reserved[y][x] = True


def _mask(mask: int, x: int, y: int) -> bool:
    if mask == 0:
        return (x + y) % 2 == 0
    if mask == 1:
        return y % 2 == 0
    if mask == 2:
        return x % 3 == 0
    if mask == 3:
        return (x + y) % 3 == 0
    if mask == 4:
        return (y // 2 + x // 3) % 2 == 0
    if mask == 5:
        return (x * y) % 2 + (x * y) % 3 == 0
    if mask == 6:
        return ((x * y) % 2 + (x * y) % 3) % 2 == 0
    return ((x + y) % 2 + (x * y) % 3) % 2 == 0


def _format_bits(mask: int) -> int:
    data = (1 << 3) | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((data << 10) | rem) ^ 0x5412


def _get_bit(value: int, index: int) -> bool:
    return ((value >> index) & 1) != 0


def _penalty_score(matrix: list[list[bool]]) -> int:
    score = 0
    for row in matrix:
        score += _run_penalty(row)
    for x in range(SIZE):
        score += _run_penalty([matrix[y][x] for y in range(SIZE)])

    for y in range(SIZE - 1):
        for x in range(SIZE - 1):
            color = matrix[y][x]
            if (
                matrix[y][x + 1] == color
                and matrix[y + 1][x] == color
                and matrix[y + 1][x + 1] == color
            ):
                score += 3

    dark = sum(1 for row in matrix for value in row if value)
    total = SIZE * SIZE
    score += abs(dark * 20 - total * 10) // total * 10
    return score


def _run_penalty(values: list[bool]) -> int:
    score = 0
    run_color = values[0]
    run_len = 1
    for value in values[1:]:
        if value == run_color:
            run_len += 1
            continue
        if run_len >= 5:
            score += run_len - 2
        run_color = value
        run_len = 1
    if run_len >= 5:
        score += run_len - 2
    return score


def _reed_solomon_remainder(data: list[int], degree: int) -> list[int]:
    divisor = _reed_solomon_divisor(degree)
    result = [0] * degree
    for value in data:
        factor = value ^ result.pop(0)
        result.append(0)
        for i, coeff in enumerate(divisor):
            result[i] ^= _gf_multiply(coeff, factor)
    return result


def _reed_solomon_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        result.append(0)
        for j in range(degree):
            result[j] = _gf_multiply(result[j], root)
            if j + 1 < len(result):
                result[j] ^= result[j + 1]
        root = _gf_multiply(root, 0x02)
    return result[:degree]


def _gf_multiply(x: int, y: int) -> int:
    result = 0
    for i in range(8):
        result ^= ((y >> i) & 1) * x
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    return result & 0xFF
