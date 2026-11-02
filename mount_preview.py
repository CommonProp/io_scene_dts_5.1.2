import os

import bpy
from bpy.props import EnumProperty
from mathutils import Matrix


MOUNT_PREVIEW_ITEMS = (
    ("dummybow", "Bow", ""),
    ("dummybricktool", "Brick Tool", ""),
    ("dummygun", "Gun", ""),
    ("dummyhammer", "Hammer", ""),
    ("dummyprinter", "Printer", ""),
    ("dummypushbroom", "Push Broom", ""),
    ("dummyrocketl", "Rocket L", ""),
    ("dummyspear", "Spear", ""),
    ("dummyspraycan", "Spraycan", ""),
    ("dummysword", "Sword", ""),
    ("dummywand", "Wand", ""),
    ("dummywrench", "Wrench", ""),
)


def dummyitems_blend_path():
    return os.path.join(os.path.dirname(__file__), "assets", "dummyitems.blend")


def reset_local_trs(ob):
    ob.location = (0.0, 0.0, 0.0)
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    ob.rotation_euler = (0.0, 0.0, 0.0)
    ob.scale = (1.0, 1.0, 1.0)


def apply_basis_to_mesh(ob):
    """Bake leftover object TRS into verts so library axes match the mesh."""
    mesh = getattr(ob, "data", None)
    if ob.type != "MESH" or mesh is None:
        reset_local_trs(ob)
        return
    basis = ob.matrix_basis.copy()
    if basis != Matrix.Identity(4):
        mesh.transform(basis)
        mesh.update()
    reset_local_trs(ob)


def inverse_world_scale(parent):
    _loc, _rot, scale = parent.matrix_world.decompose()
    sx = scale.x if abs(scale.x) > 1e-8 else 1.0
    sy = scale.y if abs(scale.y) > 1e-8 else 1.0
    sz = scale.z if abs(scale.z) > 1e-8 else 1.0
    return (1.0 / sx, 1.0 / sy, 1.0 / sz)


def parent_identity(ob, parent, parent_type="OBJECT", parent_bone=""):
    """Identity local in parent space so object +Y matches bone/empty +Y.

    Blender bone-parent space is the tail (same rotation as the head).
    Offset (0, -length, 0) puts the origin on the Torque node (head).
    """
    ob.parent = parent
    ob.parent_type = parent_type
    ob.parent_bone = parent_bone if parent_type == "BONE" else ""
    ob.matrix_parent_inverse = Matrix.Identity(4)
    reset_local_trs(ob)
    if parent_type == "BONE":
        pose = parent.pose.bones.get(parent_bone)
        if pose is not None:
            # Rest length in bone space; parent TM already has pose scale.
            ob.location = (0.0, -float(pose.bone.length), 0.0)
    ob.scale = inverse_world_scale(parent)


def append_dummy_item(name):
    path = dummyitems_blend_path()
    if not os.path.isfile(path):
        raise FileNotFoundError("Missing mount preview library: {}".format(path))
    with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
        if name not in data_from.objects:
            raise KeyError("dummyitems.blend has no object '{}'".format(name))
        data_to.objects = [name]
    ob = data_to.objects[0]
    if ob is None:
        raise KeyError("Failed to append '{}' from dummyitems.blend".format(name))
    mesh = getattr(ob, "data", None)
    if mesh is not None and hasattr(mesh, "materials"):
        mesh.materials.clear()
    apply_basis_to_mesh(ob)
    return ob


def link_object(context, ob):
    layer_coll = getattr(context.view_layer, "active_layer_collection", None)
    collection = getattr(layer_coll, "collection", None)
    if collection is None:
        collection = context.scene.collection
    if ob.name not in collection.objects:
        collection.objects.link(ob)


def create_mount_preview(context, item_name):
    ob = append_dummy_item(item_name)
    link_object(context, ob)
    active = context.view_layer.objects.active
    pose_bone = context.active_pose_bone
    if (pose_bone is not None and active is not None
            and active.type == "ARMATURE"):
        parent_identity(ob, active, "BONE", pose_bone.name)
        return ob
    if active is not None and active.type == "EMPTY":
        parent_identity(ob, active, "OBJECT")
        return ob
    bpy.data.objects.remove(ob, do_unlink=True)
    raise RuntimeError("Select a bone or empty")


class TORQUE_OT_mount_preview_create(bpy.types.Operator):
    bl_idname = "torque.mount_preview_create"
    bl_label = "Create"
    bl_description = "Append the selected dummy item at this bone or empty"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        bone = context.active_pose_bone
        active = context.view_layer.objects.active
        if bone is not None and active is not None and active.type == "ARMATURE":
            return True
        return active is not None and active.type == "EMPTY"

    def execute(self, context):
        item = context.scene.torque_mount_preview_item
        try:
            create_mount_preview(context, item)
        except (FileNotFoundError, KeyError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        return {"FINISHED"}


class TorqueMountPreviewBonePanel(bpy.types.Panel):
    bl_idname = "BONE_PT_torque_mount_preview"
    bl_label = "Torque Mount Simulation"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "bone"

    @classmethod
    def poll(cls, context):
        return context.active_pose_bone is not None

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(context.scene, "torque_mount_preview_item", text="Item")
        layout.operator(TORQUE_OT_mount_preview_create.bl_idname, text="Create")


class TorqueMountPreviewObjectPanel(bpy.types.Panel):
    bl_idname = "OBJECT_PT_torque_mount_preview"
    bl_label = "Torque Mount Simulation"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        ob = context.view_layer.objects.active
        return ob is not None and ob.type == "EMPTY"

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(context.scene, "torque_mount_preview_item", text="Item")
        layout.operator(TORQUE_OT_mount_preview_create.bl_idname, text="Create")


classes = (
    TORQUE_OT_mount_preview_create,
    TorqueMountPreviewBonePanel,
    TorqueMountPreviewObjectPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.torque_mount_preview_item = EnumProperty(
        name="Item",
        description="Dummy item to parent at the selected bone or empty",
        items=MOUNT_PREVIEW_ITEMS,
        default="dummygun")


def unregister():
    if hasattr(bpy.types.Scene, "torque_mount_preview_item"):
        del bpy.types.Scene.torque_mount_preview_item
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
