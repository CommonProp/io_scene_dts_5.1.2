"""Standalone checks for mesh export names. Not loaded by Blender."""
import importlib.util
import sys
import types
from pathlib import Path

from _stripper_selftest import _stub_mathutils


def _stub_bpy():
    bpy = types.ModuleType("bpy")
    bpy.data = types.SimpleNamespace(filepath="", objects=[], actions=[])
    sys.modules["bpy"] = bpy


def _load(name, addon, pkg):
    spec = importlib.util.spec_from_file_location(
        "{}.{}".format(pkg, name), addon / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["{}.{}".format(pkg, name)] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeOb(dict):
    def __init__(self, name, stored=None):
        self.name = name
        if stored is not None:
            self["name"] = stored


def main():
    _stub_mathutils()
    _stub_bpy()
    addon = Path(__file__).resolve().parent
    pkg = "io_scene_dts"
    module = types.ModuleType(pkg)
    module.__path__ = [str(addon)]
    sys.modules[pkg] = module
    _load("DtsTypes", addon, pkg)
    _load("encoded_normals", addon, pkg)
    _load("write_report", addon, pkg)
    _load("util", addon, pkg)
    shared = _load("shared_export", addon, pkg)
    name_of = shared.object_export_name

    assert name_of(FakeOb("gun")) == "gun"
    assert name_of(FakeOb("gun3rd")) == "gun3rd"
    assert name_of(FakeOb("magazine.001", "magazine")) == "magazine"
    assert name_of(FakeOb("magazine#100", "magazine")) == "magazine"
    assert name_of(FakeOb("gun", "chassis")) == "gun"
    assert name_of(FakeOb("gun3rd", "chassis")) == "gun3rd"
    assert name_of(FakeOb("mag3rd", "magazine")) == "mag3rd"
    assert shared.names_are_same_export_identity("magazine.001", "magazine")
    assert not shared.names_are_same_export_identity("gun3rd", "chassis")
    assert shared.is_bounds_name("bounds")
    assert shared.is_bounds_name("Bounds")
    assert shared.is_bounds_name("bounds.002")
    assert shared.is_bounds_name("bounds#2")
    assert not shared.is_bounds_name("bound")
    assert not shared.is_bounds_name("Body01_default")
    assert shared.is_dummy_name("dummygun")
    assert shared.is_dummy_name("dummygun.001")
    assert shared.is_dummy_name(name_of(FakeOb("dummygun.001")))
    assert not shared.is_dummy_name("gun")

    class FakeCol:
        def __init__(self, name, stored=None, shape=False):
            self.name = name
            self._data = {}
            if stored is not None:
                self._data["dts_detail"] = stored
            if shape:
                self._data["dts_shape"] = "x"

        def get(self, key):
            return self._data.get(key)

    lod_of = shared.resolve_collection_lod_name
    assert lod_of(FakeCol("detail32", "detail0")) == "detail32"
    assert lod_of(FakeCol("detail100")) == "detail100"
    assert lod_of(FakeCol("detail32.001", "detail32")) == "detail32"
    assert lod_of(FakeCol("Collection", "detail32")) == "detail32"
    assert lod_of(None) is None

    print("OK mesh export names")


if __name__ == "__main__":
    main()
