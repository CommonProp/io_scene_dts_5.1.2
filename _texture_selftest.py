"""Standalone checks for bundled texture fallbacks. Not loaded by Blender."""
import os
import sys
import tempfile
import types
from pathlib import Path

from _empty_rest_selftest import _load, _stub_bpy
from _stripper_selftest import _stub_mathutils


def main():
    _stub_mathutils()
    _stub_bpy()
    addon = Path(__file__).resolve().parent
    pkg = "io_scene_dts"
    module = types.ModuleType(pkg)
    module.__path__ = [str(addon)]
    sys.modules[pkg] = module
    util = _load("util", addon, pkg)

    fallback_dir = util.fallback_texture_dir()
    assert os.path.isdir(fallback_dir)
    assert os.path.normcase(fallback_dir) == os.path.normcase(
        str(addon / "texture_fallbacks"))

    found = util.resolve_fallback_texture("whiteCheck")
    assert found and os.path.isfile(found)
    assert os.path.normcase(os.path.basename(found)) == "whitecheck.png"

    assert os.path.isfile(util.resolve_fallback_texture("blank"))
    assert os.path.isfile(util.resolve_fallback_texture("blank#2"))
    assert os.path.isfile(util.resolve_fallback_texture("WHITEcheck"))
    assert os.path.isfile(util.resolve_fallback_texture("brickTOP"))
    assert os.path.isfile(util.resolve_fallback_texture("AAA-None"))
    assert util.resolve_fallback_texture("myGunCamo") is None
    assert util.resolve_fallback_texture("") is None

    with tempfile.TemporaryDirectory() as tmp:
        dts = os.path.join(tmp, "gun.dts")
        search = util._texture_search_dirs(dts)
        assert fallback_dir not in search
        assert os.path.normcase(fallback_dir) not in [
            os.path.normcase(path) for path in search]
        assert util.resolve_texture(dts, "whiteCheck") != found
        local = os.path.join(tmp, "blank.png")
        with open(local, "wb") as handle:
            handle.write(b"x")
        assert os.path.normcase(util.resolve_texture(dts, "blank")) == os.path.normcase(local)
        assert os.path.normcase(util.resolve_fallback_texture("blank")) != os.path.normcase(local)

    print("OK texture fallbacks")


if __name__ == "__main__":
    main()
