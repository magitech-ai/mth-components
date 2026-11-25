"""Unit test for tools packages exports."""

import mth_components.tools.codeact as codeact_pkg


def test_package_exports() -> None:
    """Verifies that tool packages export expected symbols."""
    assert hasattr(codeact_pkg, "CodeActExecutor")
    assert hasattr(codeact_pkg, "apply")
