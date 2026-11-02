"""Standalone checks for export playhead save/restore. Not loaded by Blender."""
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


class FakeScene:
    def __init__(self, frame=12, subframe=0.25):
        self.frame_current = frame
        self.frame_subframe = subframe
        self.calls = []

    def frame_set(self, frame, subframe=0.0):
        self.calls.append((int(frame), float(subframe)))
        self.frame_current = int(frame)
        self.frame_subframe = float(subframe)


def _load_shared():
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
    return _load("shared_export", addon, pkg)


def main():
    shared = _load_shared()
    snapshot = shared.snapshot_scene_timeline
    restore_frame = shared.restore_scene_timeline_frame
    restore = shared.restore_scene_timeline

    scene = FakeScene(12, 0.25)
    assert snapshot(scene) == (12, 0.25)
    assert snapshot(None) == (0, 0.0)
    assert snapshot(FakeScene("bad", None), default_frame=0) == (0, 0.0)

    with restore(scene):
        scene.frame_set(40, subframe=0.0)
        assert scene.frame_current == 40
    assert scene.frame_current == 12
    assert scene.frame_subframe == 0.25
    assert scene.calls[-1] == (12, 0.25)

    scene = FakeScene(7, 0.0)
    try:
        with restore(scene):
            scene.frame_set(99)
            raise RuntimeError("export failed")
    except RuntimeError:
        pass
    assert scene.frame_current == 7

    scene = FakeScene(3, 0.0)
    restore_frame(scene, None)
    assert scene.frame_current == 0
    restore_frame(scene, "lost")
    assert scene.frame_current == 0
    restore_frame(None, 12)

    print("OK timeline save/restore")


if __name__ == "__main__":
    main()
