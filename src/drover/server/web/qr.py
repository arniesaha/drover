"""Terminal QR rendering for pairing codes.

Two matrix rows per text line using Unicode half-blocks, which keeps the code
small enough to scan off a normal terminal window. Density is the whole
problem here: a QR that wraps or scrolls cannot be scanned at all, which is
why the payload below is kept as short as it is.
"""

from __future__ import annotations

from urllib.parse import quote

import segno

_GLYPHS = {
    (True, True): "█",
    (True, False): "▀",
    (False, True): "▄",
    (False, False): " ",
}


def pairing_url(
    host_port: str, code: str, fleet_name: str, *, tls: bool = False
) -> str:
    url = (
        f"drover://{host_port}?v=1&code={quote(code, safe='-')}"
        f"&n={quote(fleet_name, safe='')}"
    )
    return f"{url}&tls=1" if tls else url


def qr_matrix(data: str, *, border: int = 4) -> list[list[bool]]:
    code = segno.make(data, error="m")
    return [[bool(module) for module in row] for row in code.matrix_iter(border=border)]


def qr_lines(data: str, *, border: int = 4) -> list[str]:
    matrix = qr_matrix(data, border=border)
    if not matrix:
        return []
    width = len(matrix[0])
    blank = [False] * width
    lines = []
    for index in range(0, len(matrix), 2):
        top = matrix[index]
        bottom = matrix[index + 1] if index + 1 < len(matrix) else blank
        lines.append("".join(_GLYPHS[(top[x], bottom[x])] for x in range(width)))
    return lines
