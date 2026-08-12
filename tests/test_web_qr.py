"""Tests for drover.server.web.qr -- half-block rendering and the pair URL."""

from __future__ import annotations

from drover.server.web.qr import pairing_url, qr_lines, qr_matrix

_GLYPHS = set(" █▀▄")


def test_matrix_is_square_and_boolean():
    matrix = qr_matrix("drover://127.0.0.1:7080?v=1&code=K7QP-2M4X")
    assert len(matrix) == len(matrix[0])
    assert all(isinstance(cell, bool) for row in matrix for cell in row)


def test_lines_use_only_half_block_glyphs():
    lines = qr_lines("drover://127.0.0.1:7080?v=1&code=K7QP-2M4X")
    assert lines
    assert set("".join(lines)) <= _GLYPHS


def test_two_matrix_rows_collapse_into_one_text_line():
    data = "drover://127.0.0.1:7080?v=1&code=K7QP-2M4X"
    matrix = qr_matrix(data)
    lines = qr_lines(data)
    assert len(lines) == (len(matrix) + 1) // 2
    assert all(len(line) == len(matrix[0]) for line in lines)


def test_pairing_url_is_compact_and_ordered():
    url = pairing_url("100.64.0.10:7080", "K7QP-2M4X", "home-fleet")
    assert url == "drover://100.64.0.10:7080?v=1&code=K7QP-2M4X&n=home-fleet"


def test_pairing_url_marks_tls():
    url = pairing_url("example.test:443", "K7QP-2M4X", "home-fleet", tls=True)
    assert url.endswith("&tls=1")


def test_pairing_url_escapes_a_fleet_name_with_spaces():
    url = pairing_url("127.0.0.1:7080", "K7QP-2M4X", "my fleet")
    assert "n=my%20fleet" in url
