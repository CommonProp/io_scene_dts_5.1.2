"""Standalone checks for empty root/land Force Transform. Not loaded by Blender."""
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


def _seq(blend=False, rotation=None, translation=None):
    from io_scene_dts.DtsTypes import Sequence
    seq = Sequence()
    if blend:
        seq.flags |= Sequence.Blend
    seq.rotationMatters = list(rotation if rotation is not None else [False, False])
    seq.translationMatters = list(
        translation if translation is not None else [False, False])
    return seq


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
    check = shared.imported_empty_rest_force_transform

    assert check(_seq(), "root") is True
    assert check(_seq(), "Root") is True
    assert check(_seq(), "land") is True
    assert check(_seq(), "run") is False
    assert check(_seq(blend=True), "root") is False
    assert check(_seq(rotation=[True, False]), "root") is False
    assert check(_seq(translation=[False, True]), "land") is False
    assert not hasattr(shared, "maybe_autofill_empty_rest")

    class _EmptyAction:
        def __init__(self, use_frame_range=True, **forced):
            self.fcurves = []
            self.use_frame_range = use_frame_range
            flags = {
                "export": True,
                "force_transform": False,
                "force_scale": False,
                "force_vis": False,
                "force_morph": False,
            }
            flags.update(forced)
            self.torque_seq_props = types.SimpleNamespace(**flags)

    assert shared.action_has_forced_tracks(None) is False
    assert shared.action_has_forced_tracks(
        types.SimpleNamespace(force_transform=True)) is True
    assert shared.action_has_forced_tracks(
        types.SimpleNamespace(force_scale=True)) is True
    assert shared.action_has_forced_tracks(
        types.SimpleNamespace(force_vis=True)) is True
    assert shared.action_has_forced_tracks(
        types.SimpleNamespace(force_morph=True)) is True
    assert shared.action_is_listable_sequence(_EmptyAction()) is False
    assert shared.action_is_listable_sequence(
        _EmptyAction(force_transform=True)) is True
    assert shared.action_is_listable_sequence(
        _EmptyAction(force_scale=True)) is True
    assert shared.action_is_listable_sequence(
        _EmptyAction(force_vis=True)) is True
    assert shared.action_is_listable_sequence(
        _EmptyAction(force_morph=True)) is True
    assert shared.action_is_listable_sequence(
        _EmptyAction(force_transform=True, use_frame_range=False)) is True

    print("OK empty root/land Force Transform")


if __name__ == "__main__":
    main()
