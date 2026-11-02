"""Standalone checks for split DSQ filenames. Not loaded by Blender."""
import importlib.util
import os
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

    assert shared.sanitize_dsq_stem("root") == "root"
    assert shared.sanitize_dsq_stem("bad:name") == "bad_name"
    path = shared.dsq_split_filepath(
        os.path.join("out", "dummy.dsq"), "root", "ae_", "")
    assert path.endswith(os.path.join("out", "ae_root.dsq")), path
    path = shared.dsq_split_filepath(
        os.path.join("out", "dummy.dsq"), "fire", "", "_fp")
    assert path.endswith(os.path.join("out", "fire_fp.dsq")), path
    used = set()
    first = shared.unique_output_path(os.path.join("out", "root.dsq"), used)
    second = shared.unique_output_path(os.path.join("out", "root.dsq"), used)
    assert first.endswith(os.path.join("out", "root.dsq"))
    assert second.endswith(os.path.join("out", "root#2.dsq"))

    import bpy
    bpy.data.filepath = os.path.join("proj", "m16.blend")
    assert shared.resolve_export_filepath("", ".dts").endswith("m16.dts")
    bpy.data.filepath = ""
    assert shared.resolve_export_filepath("", ".dts") == "untitled.dts"
    assert shared.resolve_export_filepath("", ".dsq") == "untitled.dsq"
    folder = "C:\\out\\player\\"
    untitled_dsq = shared.resolve_export_filepath(folder, ".dsq")
    assert untitled_dsq.endswith(os.path.join("player", "untitled.dsq")), untitled_dsq
    split = shared.dsq_split_filepath(folder, "root", "ae_", "")
    assert split.endswith(os.path.join("player", "ae_root.dsq")), split
    print("OK dsq split names")


if __name__ == "__main__":
    main()
