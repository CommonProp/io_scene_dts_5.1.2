"""Standalone checks for unused mesh-parent collapse. Not loaded by Blender."""
import types
from pathlib import Path

from _empty_rest_selftest import _load, _seq, _stub_bpy
from _stripper_selftest import _stub_mathutils


def _shape(names, parents, objects, sequences=None):
    nodes = []
    for i, parent in enumerate(parents):
        nodes.append(types.SimpleNamespace(name=i, parent=parent))
    objs = []
    for name_index, node_index in objects:
        objs.append(types.SimpleNamespace(name=name_index, node=node_index))
    return types.SimpleNamespace(
        names=names,
        nodes=nodes,
        objects=objs,
        sequences=list(sequences or []),
    )


def main():
    _stub_mathutils()
    _stub_bpy()
    addon = Path(__file__).resolve().parent
    pkg = "io_scene_dts"
    module = types.ModuleType(pkg)
    module.__path__ = [str(addon)]
    import sys
    sys.modules[pkg] = module
    _load("DtsTypes", addon, pkg)
    _load("encoded_normals", addon, pkg)
    _load("write_report", addon, pkg)
    _load("util", addon, pkg)
    shared = _load("shared_export", addon, pkg)

    names = ["Torso", "armor100", "armor", "Mount0", "root"]
    # 0 Torso, 1 armor100 -> Torso, 2 Mount0 -> Torso
    base = _shape(
        names,
        parents=[-1, 0, 0],
        objects=[(2, 1)],
    )
    collapsed = shared.unused_mesh_parent_nodes(base)
    assert collapsed == {1}, collapsed
    assert shared.kept_node_parent(base, 1, collapsed) == 0

    mount_only = _shape(names, parents=[-1, 0, 0], objects=[])
    assert 2 not in shared.unused_mesh_parent_nodes(mount_only)
    assert shared.unused_mesh_parent_nodes(mount_only) == set()

    with_child = _shape(
        ["Torso", "armor100", "armor", "extra"],
        parents=[-1, 0, 0, 1],
        objects=[(2, 1)],
    )
    assert 1 not in shared.unused_mesh_parent_nodes(with_child)

    skirt = _shape(
        ["Hip", "skirtHip100", "skirtTrimLeft100", "skirtHip", "skirtTrimLeft"],
        parents=[-1, 0, 1],
        objects=[(3, 1), (4, 2)],
    )
    skirt_collapsed = shared.unused_mesh_parent_nodes(skirt)
    assert skirt_collapsed == {1, 2}, skirt_collapsed
    assert shared.kept_node_parent(skirt, 2, skirt_collapsed) == 0

    run = _seq(rotation=[False, True, False])
    run.nameIndex = 4
    animated = _shape(
        ["Torso", "armor100", "armor", "Mount0", "run"],
        parents=[-1, 0, 0],
        objects=[(2, 1)],
        sequences=[run],
    )
    assert 1 not in shared.unused_mesh_parent_nodes(animated)
    assert 1 in shared.unused_mesh_parent_nodes(animated, ignore_animation=True)

    dest_root = _seq(rotation=[True, True, True], translation=[True, True, True])
    dest_root.nameIndex = 4
    dest_shape = _shape(
        ["Torso", "armor100", "armor", "Mount0", "root"],
        parents=[-1, 0, 0],
        objects=[(2, 1)],
        sequences=[dest_root],
    )
    assert shared.sequence_is_rest_dest_clip(dest_root, "root") is True
    assert 1 in shared.unused_mesh_parent_nodes(dest_shape)

    exact = _shape(
        ["Torso", "armor", "armor"],
        parents=[-1, 0],
        objects=[(2, 1)],
    )
    exact.names = ["Torso", "armor"]
    exact.nodes = [
        types.SimpleNamespace(name=0, parent=-1),
        types.SimpleNamespace(name=1, parent=0),
    ]
    exact.objects = [types.SimpleNamespace(name=1, node=1)]
    assert shared.unused_mesh_parent_nodes(exact) == {1}

    empty_root = _seq()
    assert shared.imported_empty_rest_force_transform(empty_root, "root") is True
    assert shared.sequence_is_rest_dest_clip(empty_root, "root") is True

    # Collapse stores None in bone_names; Blender 5.1 pose.bones.get(None) raises.
    names = ["Torso", None, "Mount0"]
    assert shared.imported_pose_bone_name(names, 0) == "Torso"
    assert shared.imported_pose_bone_name(names, 1) is None
    assert shared.imported_pose_bone_name(names, -1) is None
    assert shared.imported_pose_bone_name(names, 9) is None

    print("OK unused mesh parent collapse")


if __name__ == "__main__":
    main()
