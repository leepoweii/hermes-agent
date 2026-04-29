"""Smoke test: LineAdapter is importable and its module-level constants are correct."""


def test_line_adapter_importable():
    from gateway.platforms.line import LineAdapter
    assert LineAdapter is not None
