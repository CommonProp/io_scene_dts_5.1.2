"""Standalone checks for 5.1 Action channelbag helpers. Not loaded by Blender."""
import sys
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
    compat = _load("action_compat", addon, pkg)

    class _Curve:
        def __init__(self, data_path, index=0):
            self.data_path = data_path
            self.array_index = index

    class _Bag:
        def __init__(self, curves):
            self.fcurves = curves

    class _Strip:
        def __init__(self, bags):
            self.channelbags = bags

        def channelbag(self, slot, ensure=False):
            return self.channelbags[0] if self.channelbags else None

    class _Layer:
        def __init__(self, strips):
            self.strips = strips

    class _Action:
        def __init__(self, layers=None, fcurves=None):
            self.layers = layers or []
            if fcurves is not None:
                self.fcurves = fcurves

    pose = _Curve('pose.bones["hand"].location', 0)
    key = _Curve('key_blocks["Talk"].value', 0)
    layered = _Action(layers=[_Layer([_Strip([_Bag([pose, key])])])])
    assert [c.data_path for c in compat.iter_action_fcurves(layered)] == [
        pose.data_path, key.data_path]
    assert compat.action_has_fcurves(layered) is True

    legacy = _Action(fcurves=[key])
    assert list(compat.iter_action_fcurves(legacy)) == [key]
    assert compat.action_has_fcurves(_Action()) is False

    curves = compat.action_fcurves_array(
        layered, 'pose.bones["hand"].location', 1)
    assert curves[0] is pose

    print("OK action compat")


if __name__ == "__main__":
    main()
