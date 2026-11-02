"""Standalone checks for DTS export-by-collection helpers. Not loaded by Blender."""
import importlib.util
import sys
import types
from pathlib import Path

from _stripper_selftest import _stub_mathutils


class FakeCollections(list):
    def get(self, name):
        for collection in self:
            if collection.name == name:
                return collection
        return None


class FakeCol(dict):
    def __init__(self, name, stored=None, shape=False, children=None, objects=None):
        self.name = name
        self.children = list(children or ())
        self.all_objects = list(objects or ())
        if stored is not None:
            self["dts_detail"] = stored
        if shape:
            self["dts_shape"] = name


class FakeOb:
    def __init__(self, name, collections=None, ob_type="MESH"):
        self.name = name
        self.type = ob_type
        self.users_collection = list(collections or ())
        self.parent = None
        self.modifiers = []
        self.data = None
        self.animation_data = None
        self._selected = False

    def select_get(self):
        return self._selected


class FakeBone:
    def __init__(self, name):
        self.name = name


class FakeArmature(FakeOb):
    def __init__(self, name, bones):
        super().__init__(name, ob_type="ARMATURE")
        self.data = types.SimpleNamespace(bones=list(bones))


class FakeCurve:
    def __init__(self, data_path):
        self.data_path = data_path
        self.keyframe_points = [types.SimpleNamespace(co=(1.0, 0.0))]


class FakeAction:
    def __init__(self, name, paths):
        self.name = name
        self.fcurves = [FakeCurve(path) for path in paths]
        self.use_frame_range = False
        self.use_cyclic = False
        self._ids = {}
        self.torque_seq_props = types.SimpleNamespace(
            export=True,
            export_dts=True,
            export_dsq=True,
            force_transform=False,
            force_scale=False,
            force_vis=False,
            force_morph=False,
            collection=None,
        )

    def get(self, key, default=""):
        return self._ids.get(key, default)

    def __setitem__(self, key, value):
        self._ids[key] = value


def _stub_bpy(collections):
    bpy = types.ModuleType("bpy")
    bpy.data = types.SimpleNamespace(
        filepath="",
        objects=[],
        actions=[],
        collections=collections,
        scenes=[],
    )
    sys.modules["bpy"] = bpy
    return bpy


def _load(name, addon, pkg):
    spec = importlib.util.spec_from_file_location(
        "{}.{}".format(pkg, name), addon / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["{}.{}".format(pkg, name)] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    _stub_mathutils()
    player = FakeCol("player", shape=True)
    detail = FakeCol("detail100")
    detail_dup = FakeCol("detail100.001")
    weapon = FakeCol("weapon", shape=True)
    player.children.append(detail)
    weapon.children.append(detail_dup)
    collections = FakeCollections([player, detail, weapon, detail_dup])
    _stub_bpy(collections)

    addon = Path(__file__).resolve().parent
    pkg = "io_scene_dts_export_collection_test"
    module = types.ModuleType(pkg)
    module.__path__ = [str(addon)]
    sys.modules[pkg] = module
    _load("DtsTypes", addon, pkg)
    _load("encoded_normals", addon, pkg)
    _load("write_report", addon, pkg)
    _load("util", addon, pkg)
    _load("action_compat", addon, pkg)
    shared = _load("shared_export", addon, pkg)

    assert shared.canonical_detail_name("detail100.001") == "detail100"
    assert shared.canonical_detail_name("Collision-1.001") == "Collision-1"
    assert shared.lift_export_root(player) is player
    assert shared.lift_export_root(detail) is player
    assert shared.lift_export_root(detail_dup) is weapon
    assert shared.resolve_export_collection("player") is player
    assert shared.resolve_export_collection("detail100") is player
    assert shared.resolve_export_collection(limit_collection=weapon) is weapon

    mesh = FakeOb("gun", [detail, detail_dup])
    assert shared.preferred_export_lod(mesh) == "detail100"
    assert shared.preferred_export_lod(mesh, player) == "detail100"
    assert shared.preferred_export_lod(mesh, weapon) == "detail100"

    other = FakeOb("mag", [detail_dup])
    assert shared.preferred_export_lod(other, player) is None
    assert shared.preferred_export_lod(other, weapon) == "detail100"

    ignore = FakeCol("__ignore__")
    master = FakeCol("Scene Collection", children=[player, weapon, ignore])
    player_arm = FakeArmature("player_arm", [FakeBone("RHand"), FakeBone("Head")])
    weapon_arm = FakeArmature("weapon_arm", [FakeBone("MountPoint")])
    player.all_objects = [player_arm]
    weapon.all_objects = [weapon_arm]
    scene = types.SimpleNamespace(collection=master, objects=[player_arm, weapon_arm])
    bpy = sys.modules["bpy"]
    bpy.data.actions = []

    listed = [col.name for col in shared.listable_export_collections(scene)]
    assert listed == ["player", "weapon"]

    scene_col = FakeCol("Scene Collection")
    loose_arm = FakeOb("loose_arm", [scene_col], "ARMATURE")
    assert shared.shape_collection_for_object(loose_arm) is None
    grouped_arm = FakeOb("grouped_arm", [player, scene_col], "ARMATURE")
    assert shared.shape_collection_for_object(grouped_arm) is player

    action = FakeAction("root", ['pose.bones["RHand"].location'])
    action.torque_seq_props.collection = player
    blank = FakeAction("orphan", ['pose.bones["RHand"].location'])
    bpy.data.actions = [action, blank]
    assert shared.action_mapped_collection(action) is player
    assert shared.action_visible_for_collections(action, [player])
    assert not shared.action_visible_for_collections(action, [weapon])
    assert shared.action_visible_for_collections(blank, [weapon])
    assert not shared.action_visible_for_collections(blank, [player])
    assert list(shared.iter_listable_actions(scene, [player])) == [action]
    assert list(shared.iter_listable_actions(scene, [weapon])) == [blank]
    assert list(shared.iter_listable_actions(scene, [weapon], show_all=True)) == [
        action, blank]
    assert list(shared.iter_listable_actions(scene, None)) == [action, blank]
    assert shared.export_sequence_collections("SELECTION") is None
    assert shared.export_sequence_collections("SCENE") is None
    assert shared.export_sequence_collections("COLLECTION", "player") == [player]

    nested = FakeAction("nested", ['pose.bones["RHand"].location'])
    nested.torque_seq_props.collection = detail
    assert shared.action_mapped_collection(nested) is player

    fire_a = FakeAction("Fire", ['pose.bones["RHand"].location'])
    fire_b = FakeAction("Fire.001", ['pose.bones["RHand"].location'])
    fire_a.torque_seq_props.collection = player
    fire_b.torque_seq_props.collection = weapon
    bpy.data.actions = [fire_a, fire_b, blank]
    assert list(shared.iter_listable_actions(scene, [player])) == [fire_a]
    assert list(shared.iter_listable_actions(scene, [weapon])) == [fire_b]

    mesh = FakeOb("body")
    mesh.parent = player_arm
    mesh.modifiers = [types.SimpleNamespace(type="ARMATURE", object=player_arm)]
    mesh._selected = True
    scene.objects = [player_arm, weapon_arm, mesh]
    selected = shared.export_action_objects(scene, "SELECTION")
    assert player_arm in selected and mesh in selected
    assert weapon_arm not in selected

    print("OK export collection scope")


if __name__ == "__main__":
    main()
