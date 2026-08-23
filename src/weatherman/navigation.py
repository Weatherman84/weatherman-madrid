from __future__ import annotations

from typing import Any


def render_app_navigation(streamlit: Any) -> None:
    """Leave navigation to Streamlit's native multipage sidebar.

    The explicit emoji links added in v9.5.3 duplicated Streamlit's own page
    navigation. Keeping this compatibility hook as a no-op makes an in-place
    GitHub upload remove the duplicate links without requiring file deletion.
    """
    return None
