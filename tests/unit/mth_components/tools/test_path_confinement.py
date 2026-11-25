"""The marketplace's private path-confinement helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from mth_components.tools.fs.path_guard import PathEscapeError, confined_path


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sub").mkdir()
    (workspace / "in.txt").write_text("inside", encoding="utf-8")
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    return workspace, secret


def test_confined_path_rejects_dotdot_and_symlink(layout: tuple[Path, Path]) -> None:
    workspace, secret = layout
    assert confined_path(workspace, "sub/../in.txt") == workspace / "in.txt"
    with pytest.raises(PathEscapeError):
        confined_path(workspace, str(secret))
    with pytest.raises(PathEscapeError):
        confined_path(workspace, "sub/../../secret.txt")
    link = workspace / "link"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows ACL
        pytest.skip("symlinks unavailable")
    with pytest.raises(PathEscapeError):
        confined_path(workspace, str(link))
