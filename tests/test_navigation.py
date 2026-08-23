from __future__ import annotations

from contextlib import nullcontext

from weatherman.navigation import render_app_navigation


class FakeStreamlit:
    def __init__(self) -> None:
        self.sidebar = nullcontext()
        self.markdown_calls: list[str] = []
        self.page_link_calls: list[tuple[str, str, str]] = []
        self.divider_calls = 0

    def markdown(self, text: str) -> None:
        self.markdown_calls.append(text)

    def page_link(self, page: str, *, label: str, icon: str) -> None:
        self.page_link_calls.append((page, label, icon))

    def divider(self) -> None:
        self.divider_calls += 1


def test_navigation_uses_internal_streamlit_pages() -> None:
    fake = FakeStreamlit()

    render_app_navigation(fake)

    assert fake.markdown_calls == []
    assert fake.page_link_calls == []
    assert fake.divider_calls == 0
