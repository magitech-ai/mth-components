"""Unit tests for the opt-in HMR wrapper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from animas import Context
from animas.fs import FsService, LocalFs
from animas.hmr import HmrService

from mth_components.hmr import component as hmr_component
from mth_components.hmr import resolve_hmr_config
from mth_components.hmr.component import apply


def test_config_mapping(tmp_path: Any) -> None:
    config = resolve_hmr_config(
        {
            "base": str(tmp_path),
            "root": [".", "plugins", "src"],
            "ignored": ["*.pyc"],
            "debounce": 25,
            "watch": {"enabled": False},
            "full_reload": lambda: None,
            "interval": 0.1,
        }
    )
    assert config["root"] == str(Path(tmp_path).resolve())
    assert config["roots"] == [
        str(Path(tmp_path).resolve()),
        str(Path(tmp_path).resolve() / "plugins"),
        str(Path(tmp_path).resolve() / "src"),
    ]
    assert "externals" not in config
    assert config["ignored"] == ["*.pyc"]
    assert config["debounce"] == 25
    assert config["watch"] == {"enabled": False}
    assert callable(config["full_reload"])
    assert config["interval"] == 0.1

    defaults = resolve_hmr_config({})
    assert defaults["root"] == str(Path.cwd().resolve())
    assert defaults["roots"] == [str(Path.cwd().resolve())]
    assert defaults["debounce"] == 100
    assert defaults["ignored"] == ["**/node_modules", "**/.*", "cache", "data"]


@pytest.mark.parametrize(
    "config",
    [
        {"base": 42},
        {"root": []},
        {"root": "plugins"},
        {"root": [42]},
        {"ignored": "*.pyc"},
        {"ignored": [42]},
        {"debounce": "100"},
        {"debounce": -1},
    ],
)
def test_config_rejections(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        resolve_hmr_config(config)


@pytest.mark.asyncio
async def test_component_mounts_hmr(tmp_path: Any) -> None:
    ctx = Context()
    ctx.use(FsService)
    await ctx.flush()
    LocalFs(ctx)
    assert hmr_component.apply.inject == ["fs"]
    await apply(
        ctx,
        base=str(tmp_path),
        root=["."],
        ignored=["*.pyc"],
        debounce=0,
    )
    hmr = ctx.hmr
    assert isinstance(hmr, HmrService)
    assert hmr.root == tmp_path.resolve()
    assert hmr.roots == (tmp_path.resolve(),)
    assert "*.pyc" in hmr.ignored
    await ctx.fiber.dispose()
