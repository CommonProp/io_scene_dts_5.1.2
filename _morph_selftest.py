"""Standalone checks for morph bake helpers. Not loaded by Blender."""
import importlib.util
import sys
import types
from pathlib import Path

from _empty_rest_selftest import _load, _stub_bpy
from _stripper_selftest import _stub_mathutils


def main():
    Vector = _stub_mathutils()
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

    assert shared.imported_morph_key_name(0, ["talk"], {"talk": 1}) is None
    assert shared.imported_morph_key_name(1, ["talk"], {"talk": 1}) == "talk"
    assert shared.imported_morph_key_name(1, ["talk"], {"talk": 3}) == "talk_f1"
    assert shared.imported_morph_key_name(2, ["talk", "root"], {"talk": 2}) == "talk_f2"
    assert shared.imported_morph_key_name(1, [], {}) == "frame_1"

    a = [Vector((0, 0, 0)), Vector((1, 0, 0))]
    b = [Vector((0, 0, 0)), Vector((1, 0, 0))]
    c = [Vector((0, 0, 0)), Vector((1, 0, 0.1))]
    assert shared.morph_verts_differ(a, b) is False
    assert shared.morph_verts_differ(a, c) is True

    class _Curve:
        def __init__(self, data_path):
            self.data_path = data_path
            self.keyframe_points = [types.SimpleNamespace(co=(1.0, 0.0))]

    class _Action:
        def __init__(self, paths):
            self.fcurves = [_Curve(path) for path in paths]
            self.use_frame_range = False
            self.torque_seq_props = types.SimpleNamespace(export=True)

    morph_only = _Action(['key_blocks["sphere"].value'])
    empty = _Action([])
    assert shared.action_is_listable_sequence(morph_only) is True
    assert shared.action_is_listable_sequence(empty) is False

    assert shared.shape_key_sequence_prefix("Test") == "Test"
    assert shared.shape_key_sequence_prefix("Test_smile") == "Test"
    assert shared.shape_key_sequence_prefix("Test.001") == "Test"
    assert shared.shape_key_sequence_prefix("Test#lod2") == "Test"
    assert shared.shape_key_sequence_prefix("Basis") is None

    key_action = _Action([
        'key_blocks["Test"].value',
        'key_blocks["Test_smile"].value',
        'key_blocks["Test#2"].value',
    ])
    key_action.name = "KeyAction"
    assert shared.action_export_sequence_name(key_action) == "Test"

    mixed = _Action([
        'key_blocks["Test"].value',
        'key_blocks["Talk"].value',
    ])
    mixed.name = "KeyAction"
    assert shared.action_export_sequence_name(mixed) == "KeyAction"

    pose_and_morph = _Action([
        'pose.bones["head"].location',
        'key_blocks["Test"].value',
    ])
    pose_and_morph.name = "walk"
    assert shared.action_export_sequence_name(pose_and_morph) == "walk"

    pose_dup = _Action(['pose.bones["head"].location'])
    pose_dup.name = "Fire.001"
    assert shared.action_export_sequence_name(pose_dup) == "Fire"
    assert shared.action_export_sequence_name(pose_dup, strip_suffixes=False) == "Fire.001"
    pose_dup.name = "Fire#2"
    assert shared.action_export_sequence_name(pose_dup) == "Fire"
    assert shared.action_export_sequence_name(pose_dup, strip_suffixes=False) == "Fire#2"
    pose_dup.name = "Fire.001#2"
    assert shared.action_export_sequence_name(pose_dup) == "Fire"

    class _Key:
        def __init__(self, frame):
            self.co = (float(frame), 0.0)

    class _KeyedCurve:
        def __init__(self, frames):
            self.keyframe_points = [_Key(frame) for frame in frames]

    class _KeyedAction:
        def __init__(self, curves):
            self.fcurves = curves

    keyed = _KeyedAction([
        _KeyedCurve(list(range(0, 200))),
        _KeyedCurve([3, 40, 180]),
    ])
    assert shared.action_keyframe_extent(keyed) == (0.0, 199.0)
    assert shared._fcurve_key_span(keyed.fcurves[1]) == (3.0, 180.0)

    class _Item:
        def __init__(self, name, action_name="", selected=True):
            self.name = name
            self.action_name = action_name
            self.is_selected = selected

    checks = shared.snapshot_export_item_checks([
        _Item("Equip", "Equip", True),
        _Item("Equip", "Equip.001", False),
    ])
    assert checks == {"Equip": True, "Equip.001": False}
    assert shared.restore_export_item_check(
        checks, action=types.SimpleNamespace(name="Equip")) is True
    assert shared.restore_export_item_check(
        checks, action=types.SimpleNamespace(name="Equip.001")) is False
    selected = {"equip"}
    assert shared.action_matches_export_selection(
        types.SimpleNamespace(name="Equip"), "Equip", selected) is True
    assert shared.action_matches_export_selection(
        types.SimpleNamespace(name="Equip.001"), "Equip", selected) is False

    assert shared.sequence_sample_times(0, 25) == list(range(0, 26))
    assert shared.sequence_eval_times(0, 25, {"enable_morph": True}) == list(range(0, 26))
    assert shared.sequence_eval_times(4, 2) == [4]

    pose_action = _Action(['pose.bones["head"].location'])
    key_action = _Action(['key_blocks["Talk"].value'])
    assert shared.action_fcurves_are_key_only(key_action) is True
    assert shared.action_fcurves_are_key_only(pose_action) is False
    assert shared.action_can_assign_to_key(key_action) is True
    assert shared.action_can_assign_to_key(pose_action) is False

    assert shared.shape_key_value_data_path("Test") == 'key_blocks["Test"].value'
    assert shared.shape_key_value_data_path('A"B') == r'key_blocks["A\"B"].value'
    assert shared.current_shape_key_value(None, "Test") == 0.0
    assert shared.clamp_morph_proxy_value(None) == 0.0
    assert shared.clamp_morph_proxy_value(-1) == 0.0
    assert shared.clamp_morph_proxy_value(0.4) == 0.4
    assert shared.clamp_morph_proxy_value(2) == 1.0
    path = shared.bone_morph_prop_path("DummyTest", "Test")
    assert path == 'pose.bones["DummyTest"]["Test"]'
    match = shared.POSE_CUSTOM_PROP_RE.match(path)
    assert match.group(1) == "DummyTest" and match.group(2) == "Test"

    class _SkinMesh:
        def __init__(self, name, skin=True, armature=True):
            self.morph_name = name
            key = types.SimpleNamespace(
                key_blocks={"Basis": object(), "Talk": object()},
                animation_data=None)
            mods = [types.SimpleNamespace(type="ARMATURE")] if armature else []
            self.source_ob = types.SimpleNamespace(
                name=name,
                modifiers=mods,
                data=types.SimpleNamespace(shape_keys=key))
            self._type = shared.Mesh.SkinType if skin else shared.Mesh.StandardType

        def get_type(self):
            return self._type

    class _Shape:
        def __init__(self, meshes):
            self.meshes = meshes

    class _Obj:
        def __init__(self, count):
            self.firstMesh = 0
            self.numMeshes = count

    keyed_action = _Action(['key_blocks["Talk"].value'])
    skin = _SkinMesh("body")
    reason = shared.skin_animated_morph_reason(
        None, _Shape([skin]), _Obj(1), keyed_action, 0, 10)
    assert reason and "body" in reason and "Armature modifier" in reason
    assert shared.skin_animated_morph_reason(
        None, _Shape([_SkinMesh("head", skin=False)]), _Obj(1), keyed_action, 0, 10) is None
    assert shared.skin_animated_morph_reason(
        None, _Shape([skin]), _Obj(1), empty, 0, 10) is None
    assert shared.skin_animated_morph_reason(
        None, _Shape([_SkinMesh("body", armature=False)]), _Obj(1), keyed_action, 0, 10) is None

    assert shared.imported_morph_dummy_name("Monkey") == "DummyMonkey"
    assert shared.imported_morph_dummy_name("Monkey#2") == "DummyMonkey"
    assert shared.imported_morph_dummy_name("DummyTalk") == "DummyTalk"
    assert shared.imported_morph_dummy_name("") == "DummyMorph"

    class _Named:
        def __init__(self, name, dts=None):
            self.name = name
            self._dts = dts

        def get(self, key):
            return self._dts if key == "name" else None

    assert shared.imported_morph_mesh_stem(_Named("Monkey.001", "Monkey")) == "Monkey"
    assert shared.imported_morph_mesh_stem(_Named("Talk#2")) == "Talk"

    keymap = {0: None, 1: "sphere", 2: "cube"}
    assert shared.morph_snapshot_value_track([1, 1, 2, 0], keymap, "sphere") == [
        (1.0,), (1.0,), (0.0,), (0.0,)]
    assert shared.morph_snapshot_value_track([2, 1], keymap, "cube") == [
        (1.0,), (0.0,)]
    assert shared.morph_snapshot_value_track([], keymap, "sphere") == []

    poly = types.SimpleNamespace(use_smooth=False, normal=Vector((0, 0, 1)))
    vert = types.SimpleNamespace(normal=Vector((1, 0, 0)))
    flat = shared.corner_export_normal(None, poly, 0, vert)
    assert abs(flat.x) < 1e-6 and abs(flat.z - 1) < 1e-6
    smooth_poly = types.SimpleNamespace(use_smooth=True, normal=Vector((0, 0, 1)))
    averaged = shared.corner_export_normal(None, smooth_poly, 0, vert)
    assert abs(averaged.x - 1) < 1e-6

    class _Tx:
        def __init__(self, x):
            self.x = float(x)

        def inverted(self):
            return _Tx(-self.x)

        def __matmul__(self, other):
            if isinstance(other, _Tx):
                return _Tx(self.x + other.x)
            return other

    # Live bone slide of -0.456766 (fail1 k34) must not land in rest verts.
    posed = _Tx(-0.456766)
    bind = _Tx(0.0)
    mesh_now = _Tx(-0.456766)
    stripped = shared.bind_space_mesh_matrix(mesh_now, posed, bind)
    assert abs(stripped.x) < 1e-6
    assert abs(shared.bind_space_mesh_matrix(bind, bind, bind).x) < 1e-6

    clump = shared.collapse_morph_positions([
        Vector((0, 0, 0)), Vector((2, 0, 0)), Vector((0, 6, 0))])
    assert len(clump) == 3
    assert all(p.x == clump[0].x and p.y == clump[0].y and p.z == clump[0].z for p in clump)
    assert abs(clump[0].x - (2 / 3)) < 1e-6
    assert abs(clump[0].y - 2) < 1e-6

    class _Mesh:
        def __init__(self, verts, vpf):
            self.verts = verts
            self.vertsPerFrame = vpf

    tight = _Mesh([0] * 3, 3)
    assert shared.morph_mesh_can_append(tight, 2, limit=10) is True
    tight.verts = [0] * 6
    assert shared.morph_mesh_can_append(tight, 2, limit=10) is False
    assert shared.morph_mesh_can_append(tight, 1, limit=10) is True

    class _Shape:
        def __init__(self):
            self.names = ["Suzanne"]

    assert shared.next_morph_overflow_object_name(_Shape(), "Suzanne") == "Suzanne_mfB"
    taken = _Shape()
    taken.names = ["Suzanne", "Suzanne_mfB"]
    assert shared.next_morph_overflow_object_name(taken, "Suzanne") == "Suzanne_mfC"
    assert shared.morph_overflow_name_stem("Suzanne_mfB") == "Suzanne"
    assert shared.morph_overflow_name_stem("Suzanne_mfB_mfB") == "Suzanne"
    stacked = _Shape()
    stacked.names = ["Suzanne", "Suzanne_mfB"]
    assert shared.next_morph_overflow_object_name(stacked, "Suzanne_mfB") == "Suzanne_mfC"

    class _ShadeMesh:
        def __init__(self):
            clump = [Vector((1, 2, 3))] * 3
            real = [Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, 2, 0))]
            self.verts = clump + real
            self.normals = [Vector((0, 0, 1))] * 6
            self.vertsPerFrame = 3
            self.numFrames = 2

    assert shared.mesh_frame_is_collapsed([Vector((0, 0, 0))] * 3) is True
    assert shared.mesh_frame_is_collapsed([
        Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, 1, 0))]) is False
    assert shared.mesh_import_shading_frame(_ShadeMesh()) == 1
    first = _ShadeMesh()
    first.verts = first.verts[3:] + first.verts[:3]
    assert shared.mesh_import_shading_frame(first) == 0

    from io_scene_dts.DtsTypes import Mesh, Object, ObjectState, Primitive

    class _OverflowShape:
        def __init__(self):
            self.names = ["Suzanne"]
            self.meshes = []
            self.objects = []
            self.objectstates = []
            self.subshapes = [types.SimpleNamespace(numObjects=1)]
            self.sequences = [types.SimpleNamespace(baseObjectState=1)]

        def name(self, string):
            key = string.lower()
            for index, item in enumerate(self.names):
                if item.lower() == key:
                    return index
            self.names.append(string)
            return len(self.names) - 1

    source_mesh = Mesh(Mesh.StandardType)
    source_mesh.verts = [Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, 2, 0))]
    source_mesh.normals = [Vector((0, 0, 1))] * 3
    source_mesh.enormals = [0, 0, 0]
    source_mesh.vertsPerFrame = 3
    source_mesh.numFrames = 1
    source_mesh.indices = [0, 1, 2]
    source_mesh.primitives = [Primitive(0, 3, 0)]
    source_mesh.vert_ids = [0, 1, 2]
    source_mesh.loop_ids = [0, 1, 2]
    source_mesh.morph_ob = object()
    source_obj = Object(0, 1, 0, 0)
    overflow = _OverflowShape()
    overflow.meshes = [source_mesh]
    overflow.objects = [source_obj]
    overflow.objectstates = [ObjectState(0.75, 0, 0)]
    collapse = (
        [Vector((0.5, 0.5, 0))] * 3,
        [Vector((0, 0, 1))] * 3,
    )
    real = (
        [Vector((4, 0, 0)), Vector((5, 0, 0)), Vector((4, 2, 0))],
        [Vector((0, 1, 0))] * 3,
    )
    new_obj, err = shared.add_morph_overflow_object(
        overflow, source_obj, {source_mesh: collapse}, {source_mesh: real})
    assert err is None, err
    assert overflow.names[new_obj.name] == "Suzanne_mfB"
    assert new_obj.node == source_obj.node
    assert getattr(new_obj, "vis_source", None) is getattr(source_obj, "vis_source", None)
    cloned = overflow.meshes[new_obj.firstMesh]
    assert cloned.loop_ids == [0, 1, 2]
    assert abs(cloned.normals[0].y - 1) < 1e-6
    assert cloned.numFrames == 2
    assert cloned.verts[0].x == cloned.verts[1].x == cloned.verts[2].x
    assert cloned.verts[3].x == 4
    assert overflow.sequences[0].baseObjectState == 2
    assert overflow.objectstates[1].vis == 0.75
    assert overflow.objectstates[1].frame == 0
    assert overflow.subshapes[0].numObjects == 2
    newer, err = shared.add_morph_overflow_object(
        overflow, new_obj, {cloned: collapse}, {cloned: real})
    assert err is None, err
    assert overflow.names[newer.name] == "Suzanne_mfC"

    print("OK morph helpers")


if __name__ == "__main__":
    main()
