import bpy, bmesh, os, sys
from math import sqrt, pi
from operator import attrgetter
from itertools import groupby

from .DtsShape import DtsShape
from .DtsTypes import *
from .write_report import (
    IoLog, finish_log, io_meta, normalize_log_level, note, set_active_log)
from .util import fail, resolve_texture, default_materials, evaluate_all, find_reference, \
    fcurves_keyframe_in_range
from .encoded_normals import encode_mesh_normals
from .mesh_optimize import fit_mesh_primitives_to_s16, optimize_exported_mesh
from .shared_export import (
    append_mesh_morph_frame, apply_action_extra, apply_sequence_flags,
    apply_armature_object_track_matters, apply_sequence_membership,
    apply_sequence_scales, apply_shape_sequence_scales, append_auto_billboard_details, append_ground_frames,
    append_sequence_triggers, assign_armature_action, attach_ifl_sidecars,
    classify_detail_marker, export_node_curve_arrays, export_node_fcurves,
    find_bounds_object, find_export_sequences, folded_local_matrix,
    bone_export_world_matrix,
    ground_frame_indices, mesh_object_offset, rest_world_has_scale,
    scale_shape_world_units, tge_shape_set_error, zap_scale_matrix,
    ifl_matters_from_shape, is_auto_billboard_detail, is_bounds_name,
    is_dummy_name, name_listed, blender_export_base_name, object_export_name,
    object_ignored_for_export,
    object_is_auto_billboard_marker,
    read_node_export_lists, collision_detail_from_mesh_name,
    is_collision_or_los_detail, lod_size_from_name, preferred_export_lod,
    export_directory, export_object_scope,
    collection_page_selected_names, iter_export_objects, object_in_export_scope,
    resolve_export_collection, sanitize_dsq_stem, unique_output_path,
    real_detail_levels, resolve_export_vis_source, resolve_mesh_identity,
    sample_export_node, sample_export_vis, sample_world_frames,
    resolve_blend_reference_frame, vis_source_curve_array, vis_values_differ,
    add_morph_overflow_object, apply_bone_morph_proxies,
    collapse_morph_positions, fcurves_bone_morph_keyed_in_range,
    fcurves_shape_key_keyed_in_range, mesh_allows_morph,
    morph_mesh_can_append, morph_verts_differ,
    skin_animated_morph_reason,
    object_has_object_state, reset_scene_shape_keys,
    restore_shape_key_values_only, corner_export_normal,
    sample_morph_positions,
    snapshot_scene_shape_key_values,
    resolve_export_filepath, restore_scene_timeline,
    sequence_eval_times, set_scene_eval_time)

def undup_name(n):
    return n.split("#", 1)[0]


def _edge_is_sharp(mesh, edge):
    attr = mesh.attributes.get("sharp_edge") if hasattr(mesh, "attributes") else None
    if attr is not None:
        try:
            return bool(attr.data[edge.index].value)
        except (AttributeError, IndexError, TypeError):
            pass
    return bool(getattr(edge, "use_edge_sharp", False))


def assign_face_smooth_ids(mesh):
    """Stand-in for Max smoothing groups.

    Flat faces each get a unique id (hard edges never weld). Smooth
    faces flood-fill across non-sharp edges into one island, the way
    a shared Max smoothing-group bit lets those corners merge.
    """
    nfaces = len(mesh.polygons)
    ids = [-1] * nfaces
    if nfaces == 0:
        return ids

    edge_faces = {}
    for poly in mesh.polygons:
        verts = poly.vertices
        count = len(verts)
        for i in range(count):
            a, b = verts[i], verts[(i + 1) % count]
            key = (a, b) if a < b else (b, a)
            bucket = edge_faces.get(key)
            if bucket is None:
                edge_faces[key] = [poly.index]
            else:
                bucket.append(poly.index)

    sharp = set()
    for edge in mesh.edges:
        if not _edge_is_sharp(mesh, edge):
            continue
        a, b = edge.vertices
        sharp.add((a, b) if a < b else (b, a))

    next_id = 1
    for poly in mesh.polygons:
        if ids[poly.index] >= 0:
            continue
        if not poly.use_smooth:
            ids[poly.index] = next_id
            next_id += 1
            continue
        island = next_id
        next_id += 1
        stack = [poly.index]
        ids[poly.index] = island
        while stack:
            face_index = stack.pop()
            verts = mesh.polygons[face_index].vertices
            count = len(verts)
            for i in range(count):
                a, b = verts[i], verts[(i + 1) % count]
                key = (a, b) if a < b else (b, a)
                if key in sharp:
                    continue
                for other in edge_faces.get(key, ()):
                    if ids[other] >= 0:
                        continue
                    if not mesh.polygons[other].use_smooth:
                        continue
                    ids[other] = island
                    stack.append(other)
    return ids


def unique_mesh_export_name(base, lod_name, scene_objects, blender_name):
    if base not in scene_objects or lod_name not in scene_objects[base][1]:
        return base
    if blender_name and blender_name != base:
        if blender_name not in scene_objects or lod_name not in scene_objects[blender_name][1]:
            return blender_name
    suffix = 2
    while True:
        trial = "{}#{}".format(base, suffix)
        if trial not in scene_objects or lod_name not in scene_objects[trial][1]:
            return trial
        suffix += 1


def unique_object_export_name(base, scene_objects, blender_name):
    """A DTS object has one node. Do not reuse a name already on another node."""
    blender = blender_export_base_name(blender_name or "")
    if blender and blender not in scene_objects:
        return blender
    suffix = 2
    while True:
        trial = "{}#{}".format(base, suffix)
        if trial not in scene_objects:
            return trial
        suffix += 1


def orient_spherical_billboard(mesh):
    """TGE looks down +Y. forceFaceCamera() identities the modelview in that
    space, so a spherical BB quad must lie in XZ (face +Y). A Blender plane
    facing +Z is edge-on after that and disappears."""
    if not mesh.verts or not mesh.normals:
        return

    facing = Vector()
    for normal in mesh.normals:
        if normal.dot(facing) >= 0:
            facing += normal
        else:
            facing -= normal
    if facing.length_squared < 1e-12:
        return
    facing.normalize()

    target = Vector((0.0, 1.0, 0.0))
    if facing.dot(target) > 0.999:
        return

    rotation = facing.rotation_difference(target).to_matrix()
    for i, vert in enumerate(mesh.verts):
        mesh.verts[i] = rotation @ vert
    for i, normal in enumerate(mesh.normals):
        mesh.normals[i] = (rotation @ normal).normalized()
    apply_tge_mesh_sphere(mesh)


def apply_tge_mesh_sphere(mesh):
    """Match TSMesh::computeBounds: center is the AABB midpoint, radius
    is the farthest vert from that center — not from the origin."""
    if not mesh.verts:
        mesh.center = Vector()
        mesh.radius = 0
        return
    mesh.bounds = mesh.calculate_bounds_mat(Matrix())
    mesh.center = (mesh.bounds.min + mesh.bounds.max) * 0.5
    mesh.radius = mesh.calculate_radius_mat(Matrix(), mesh.center)

def linearrgb_to_srgb(c):
    if c < 0.0031308:
        if c < 0:
            return 0
        else:
            return c * 12.92
    else:
        return 1.055 * (c ** (1.0 / 2.4)) - 0.055

def skin_bind_matrix(node):
    # TGE: Vb = nodeWorld * initialTransform. After fold, nodeWorld is
    # zap(boneRest) in armature space. Match Max: inv(zap(bone)).
    rest = getattr(node, "rest_world", None)
    if rest is None:
        rest = getattr(node, "matrix_world", None)
    if rest is None:
        return Matrix.Identity(4)
    return zap_scale_matrix(rest).inverted()


def get_vertex_bone(mesh, node):
    for bone_index, (node_index, _) in enumerate(mesh.bones):
        if node_index == node.index:
            return bone_index

    bone_index = len(mesh.bones)
    bind = skin_bind_matrix(node)
    flat_mat = [x for y in bind.row for x in y]
    mesh.bones.append((node.index, flat_mat))
    return bone_index

def add_vertex_influences(ob, armature, node_lookup, mesh, vert, vertex_index):
    influences = []
    total_weight = 0

    for group in vert.groups:
        vertex_group = ob.vertex_groups[group.group]

        bone = armature.data.bones.get(vertex_group.name)
        if bone is None:
            continue

        node = node_lookup.get(bone)
        if not node:
            continue

        influences.append((node, group.weight))
        total_weight += group.weight

    if total_weight == 0:
        weight_multiplier = 1
    else:
        weight_multiplier = 1 / total_weight

    for node, weight in influences:
        mesh.influences.append((
            vertex_index,
            get_vertex_bone(mesh, node),
            weight * weight_multiplier))

def export_material(mat, shape):
    # print("Exporting material", mat.name)

    material_index = len(shape.materials)
    flags = 0

    if mat.torque_props.use_shadeless:
        flags |= Material.SelfIlluminating
    if mat.torque_props.use_transparency:
        flags |= Material.Translucent
    if mat.torque_props.blend_mode == "ADDITIVE":
        flags |= Material.Additive
    elif mat.torque_props.blend_mode == "SUBTRACTIVE":
        flags |= Material.Subtractive

    s_wrap, t_wrap = material_wrap_flags(mat)
    if s_wrap:
        flags |= Material.SWrap
    if t_wrap:
        flags |= Material.TWrap

    if mat.torque_props.never_env_map or mat.torque_props.env_mapping <= 0.0:
        flags |= Material.NeverEnvMap

    if mat.torque_props.no_mip_mapping:
        flags |= Material.NoMipMap
    if mat.torque_props.mip_map_zero_border:
        flags |= Material.MipMapZeroBorder

    if mat.torque_props.use_ifl:
        flags |= Material.IFLMaterial

        # TODO: keep IFL materials in a table by name?
        # what would duplicates do?

        ifl_index = len(shape.iflmaterials)
        ifl_name = (mat.torque_props.ifl_name or "").strip() or undup_name(mat.name)
        ifl = IflMaterial(
            name=shape.name(ifl_name),
            slot=material_index)
        shape.iflmaterials.append(ifl)

    material = Material(name=undup_name(mat.name), flags=flags)
    material.bl_mat = mat
    material.detailScale = mat.torque_props.detail_scale
    if flags & Material.NeverEnvMap:
        material.reflectance = 0.0
    else:
        material.reflectance = mat.torque_props.env_mapping

    shape.materials.append(material)

    return material_index


def find_blender_material(name):
    name = (name or "").strip()
    if not name:
        return None
    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    for other in bpy.data.materials:
        if undup_name(other.name) == name:
            return other
    return None


def ensure_extra_map_material(name, aux_flag, shape, material_table):
    name = (name or "").strip()
    if not name:
        return -1

    bl_mat = find_blender_material(name)
    if bl_mat is not None:
        if bl_mat not in material_table:
            material_table[bl_mat] = export_material(bl_mat, shape)
            shape.materials[material_table[bl_mat]].flags |= aux_flag
        return material_table[bl_mat]

    for i, dmat in enumerate(shape.materials):
        if dmat.name == name or undup_name(dmat.name) == name:
            return i

    shape.materials.append(Material(
        name=name,
        flags=Material.SWrap | Material.TWrap | Material.NeverEnvMap | aux_flag))
    return len(shape.materials) - 1


def apply_material_extra_maps(shape, material_table):
    for i, dmat in enumerate(shape.materials):
        bl_mat = getattr(dmat, "bl_mat", None)
        if bl_mat is not None and bl_mat not in material_table:
            material_table[bl_mat] = i

    pending = list(shape.materials)
    for dmat in pending:
        bl_mat = getattr(dmat, "bl_mat", None)
        if bl_mat is None:
            continue
        props = bl_mat.torque_props
        ensure_extra_map_material(props.detail_map, Material.DetailMap, shape, material_table)
        ensure_extra_map_material(props.bump_map, Material.BumpMap, shape, material_table)
        ensure_extra_map_material(
            props.reflectance_map, Material.ReflectanceMap, shape, material_table)

    for i, dmat in enumerate(shape.materials):
        bl_mat = getattr(dmat, "bl_mat", None)
        if bl_mat is None:
            continue
        props = bl_mat.torque_props
        env = max(0.0, min(1.0, props.env_mapping))
        dmat.detailScale = props.detail_scale
        if env <= 0.0:
            dmat.flags |= Material.NeverEnvMap
            dmat.reflectance = 0.0
        else:
            dmat.flags &= ~Material.NeverEnvMap
            dmat.reflectance = env

        dmat.detailMap = ensure_extra_map_material(
            props.detail_map, Material.DetailMap, shape, material_table)
        dmat.bumpMap = ensure_extra_map_material(
            props.bump_map, Material.BumpMap, shape, material_table)
        dmat.reflectanceMap = ensure_extra_map_material(
            props.reflectance_map, Material.ReflectanceMap, shape, material_table)

        if dmat.detailMap >= 0:
            dmat.flags |= Material.DetailMap
        if dmat.bumpMap >= 0:
            dmat.flags |= Material.BumpMap
        if dmat.reflectanceMap >= 0:
            dmat.flags |= Material.ReflectanceMap
        elif env > 0.0:
            dmat.reflectanceMap = i

def seq_float_eq(a, b):
    return all(abs(i - j) < 0.000001 for i, j in zip(a, b))

def first_image_extension(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "TEX_IMAGE":
            return getattr(node, "extension", None)
    return None

def material_wrap_flags(mat):
    ext = first_image_extension(mat)
    if ext in {"CLIP", "EXTEND"}:
        return False, False
    if ext in {"REPEAT", "MIRROR"}:
        return True, True
    return mat.torque_props.s_wrap, mat.torque_props.t_wrap

def resolve_mesh_parent_node(bobj, node_lookup):
    parent = bobj.parent
    while parent is not None:
        node = node_lookup.get(parent)
        if node and node is not False:
            return node
        parent = parent.parent
    return None

def resolve_armature_attach(armature, node_lookup):
    roots = [bone for bone in armature.data.bones if bone.parent is None]
    for bone in list(roots) + list(armature.data.bones):
        node = node_lookup.get(bone)
        if node and node is not False:
            return node.index
    return None

def ensure_detail_level(shape, scene_lods, lod_name):
    if lod_name in scene_lods:
        return scene_lods[lod_name]

    lod_name_index, lod_name = shape.name_resolve(lod_name)
    lod_size = lod_size_from_name(lod_name)
    if lod_size is None:
        note("Warning: LOD '{}' does not encode a pixel size (Max 'Detail-1' is an index, not size -1); using 32".format(lod_name))
        lod_size = 32

    print("Creating LOD '{}' (size {})".format(lod_name, lod_size))
    scene_lods[lod_name] = DetailLevel(name=lod_name_index, subshape=0, objectDetail=-1, size=lod_size)
    shape.detail_levels.append(scene_lods[lod_name])
    return scene_lods[lod_name]

def register_detail_markers(scene, shape, scene_lods, scope=None):
    for ob in iter_export_objects(scene, scope):
        if ob.type != "EMPTY":
            continue
        name = object_export_name(ob)
        kind, lod_name = classify_detail_marker(name)
        if kind == "detail":
            ensure_detail_level(shape, scene_lods, lod_name)

def export_empty_node(lookup, shape, select_object, ob, parent=-1,
                      always_export=None, never_export=None, scope=None):
    name = object_export_name(ob)

    if not object_in_export_scope(ob, scope):
        for child in ob.children:
            if child.type == "EMPTY":
                export_empty_node(lookup, shape, select_object, child, parent,
                                  always_export, never_export, scope)
        return

    kind, _ = classify_detail_marker(name)
    if kind is not None or object_is_auto_billboard_marker(ob) or is_dummy_name(name) or is_bounds_name(name) or name_listed(name, never_export):
        for child in ob.children:
            if child.type == "EMPTY":
                export_empty_node(lookup, shape, select_object, child, parent,
                                  always_export, never_export, scope)
        return

    if select_object and not ob.select_get() and not name_listed(name, always_export):
        lookup[ob] = False
        for child in ob.children:
            if child.type == "EMPTY":
                export_empty_node(lookup, shape, select_object, child, parent,
                                  always_export, never_export, scope)
        return

    node = Node(shape.name(name), parent)

    node.armature = None
    node.bl_ob = ob
    node.animation_data = ob.animation_data
    node.rest_world = ob.matrix_world.copy()
    node.rest_local = ob.matrix_local.copy()
    parent_rest = getattr(parent, "rest_world", None) if not isinstance(parent, int) else None
    node.matrix = folded_local_matrix(node.rest_world, parent_rest)
    node.matrix_world = node.rest_world
    node.rotation_mode = ob.rotation_mode

    shape.nodes.append(node)
    lookup[ob] = node

    for child in ob.children:
        if child.type == 'EMPTY':
            export_empty_node(lookup, shape, select_object, child, node,
                              always_export, never_export, scope)

def export_bones(lookup, shape, armature, bones, parent=-1, never_export=None):
    for bone in bones:
        if is_dummy_name(bone.name) or name_listed(bone.name, never_export):
            export_bones(lookup, shape, armature, bone.children, parent, never_export)
            continue
        node = Node(shape.name(bone.name), parent)
        node.bone = bone
        node.bl_ob = bone
        node.armature = armature
        node.animation_data = getattr(bone, "animation_data", None)
        node.rest_world = bone_export_world_matrix(armature, bone, posed=False)
        parent_rest = getattr(parent, "rest_world", None) if not isinstance(parent, int) else None
        node.matrix = folded_local_matrix(node.rest_world, parent_rest)
        node.rest_local = node.matrix.copy()
        node.rotation_mode = getattr(bone, "rotation_mode", "QUATERNION")

        shape.nodes.append(node)
        lookup[bone] = node
        export_bones(lookup, shape, armature, bone.children, node, never_export)

def save_nodes(scene, shape, select_object, always_export=None, never_export=None,
               scope=None):
    node_lookup = {}

    # Try to create nodes from empties armature bones
    for ob in iter_export_objects(scene, scope):
        if ob.parent is not None:
            continue

        if ob.type == 'EMPTY':
            export_empty_node(node_lookup, shape, select_object, ob,
                              always_export=always_export, never_export=never_export,
                              scope=scope)
        elif ob.type == 'ARMATURE' and (ob.select_get() or not select_object):
            top_bones = filter(lambda b: b.parent is None, ob.data.bones)
            export_bones(node_lookup, shape, ob, top_bones, never_export=never_export)

    # NodeOrder backwards compatibility
    if "NodeOrder" in bpy.data.texts:
        note("Warning: NodeOrder found, using it for backwards compatibility")
        order = bpy.data.texts["NodeOrder"].as_string().split("\n")
        order_key = {name: i for i, name in enumerate(order)}
    else:
        order_key = {}

    # Sort by node indices from the DTS
    def node_order(n):
        name = shape.names[n.name]
        if name in order_key:
            return order_key[name]
        bl_ob = getattr(n, "bl_ob", None)
        if bl_ob is not None and hasattr(bl_ob, "get"):
            return bl_ob.get("nodeIndex", sys.maxsize)
        return sys.maxsize

    shape.nodes.sort(key=node_order)

    for index, node in enumerate(shape.nodes):
        parent_obj = None
        if not isinstance(node.parent, int):
            parent_obj = node.parent
            try:
                node.parent = shape.nodes.index(parent_obj)
            except ValueError:
                node.parent = -1
                parent_obj = None
        elif node.parent != -1 and 0 <= node.parent < len(shape.nodes):
            parent_obj = shape.nodes[node.parent]

        rest = getattr(node, "rest_world", None)
        if rest is None:
            rest = node.matrix
            node.rest_world = rest
        node.parent_node = parent_obj
        parent_rest = getattr(parent_obj, "rest_world", None) if parent_obj is not None else None
        node.matrix = folded_local_matrix(rest, parent_rest)
        location, rotation, scale = node.matrix.decompose()
        if not seq_float_eq((1, 1, 1), scale):
            node.matrix = Matrix.Translation(location) @ rotation.to_matrix().to_4x4()
            location, rotation, _ = node.matrix.decompose()
        if rest_world_has_scale(rest):
            note("Note: folded rest scale on '{}' into object offset"
                  .format(shape.names[node.name]))

        node.index = index
        node.matrix_world = Matrix.Translation(location) @ rotation.to_matrix().to_4x4()

        shape.default_translations.append(location)
        shape.default_rotations.append(rotation)

    def composed_world(node):
        if getattr(node, "_fold_world_done", False):
            return node.matrix_world
        local = node.matrix_world
        if node.parent != -1:
            local = composed_world(shape.nodes[node.parent]) @ local
        node.matrix_world = local
        node._fold_world_done = True
        return node.matrix_world

    for node in shape.nodes:
        composed_world(node)

    return node_lookup

def save_meshes(scene, shape, node_lookup, select_object,
                blank_material=True, optimize_meshes=True, operator=None,
                force_export=False, export_root=None, scope=None):
    blank_material_index = None
    scene_lods = {}
    scene_objects = {}

    auto_root_index = None
    bounds_ob = None

    for bobj in iter_export_objects(scene, scope):
        if bobj.type != "MESH":
            continue

        if object_ignored_for_export(bobj):
            continue

        if select_object and not bobj.select_get():
            continue

        if is_bounds_name(bobj.name):
            if bounds_ob:
                note("Warning: Multiple 'bounds' objects found - check capitalization")
            bounds_ob = bobj
            continue

        raw_name = object_export_name(bobj)

        collection_name = preferred_export_lod(bobj, export_root)

        name, lod_name, mesh_flags = resolve_mesh_identity(raw_name, collection_name, bobj)

        if lod_name == "__ignore__":
            continue

        transform_mat = None
        armature_modifier = None

        # Try to find an armature modifier on the object
        for modifier in bobj.modifiers:
            if modifier.type != 'ARMATURE':
                continue

            armature_modifier = modifier
            break

        # Max/TGE: skins are not hung off a node. updateSkin already writes
        # shape-space verts; an object node would transform them again.
        if armature_modifier is not None:
            attach_node = -1
            arm = armature_modifier.object
            if arm is None:
                note("Warning: Skin '{}' has no armature object; using matrix_world"
                      .format(bobj.name))
            transform_mat = bobj.matrix_world.copy()
        elif bobj.parent:
            if bobj.parent_type == 'BONE':
                armature = bobj.parent
                bone = armature.data.bones[bobj.parent_bone]

                if bone not in node_lookup:
                    note('Ignoring mesh {} - parent bone {} not included'
                          .format(bobj.name, bone.name))
                    continue

                node = node_lookup[bone]
                attach_node = node.index
            elif bobj.parent_type == 'OBJECT':
                if bobj.parent.type == "ARMATURE":
                    attach_node = resolve_armature_attach(bobj.parent, node_lookup)
                    if attach_node is None:
                        note("Warning: Mesh '{}' armature has no exported bones; using auto-root".format(bobj.name))
                else:
                    parent_node = resolve_mesh_parent_node(bobj, node_lookup)
                    if parent_node is None:
                        if bobj.parent.type != "EMPTY":
                            print("The mesh '{}' has a parent of type '{}' (named '{}'). You can only parent meshes to empties, not other meshes.".format(bobj.name, bobj.parent.type, bobj.parent.name))
                            continue
                        note("Warning: Mesh '{}' parent '{}' was skipped (dummy); attaching to auto-root".format(bobj.name, bobj.parent.name))
                        attach_node = None
                    else:
                        attach_node = parent_node.index
            else:
                note('Warning: Mesh "{}" is using an unsupported parenting type "{}"'
                      .format(bobj.name, bobj.parent_type))
                attach_node = None
        else:
            note("Warning: Mesh '{}' has no parent".format(bobj.name))
            attach_node = None

        if attach_node is None:
            if auto_root_index is None:
                auto_root_index = len(shape.nodes)

                node = Node(shape.name("__auto_root__"))
                node.index = auto_root_index
                node.armature = None
                node.bl_ob = None
                node.parent_node = None
                node.rest_world = Matrix.Identity(4)
                node.matrix = Matrix.Identity(4)
                node.rest_local = node.matrix.copy()
                node.matrix_world = node.matrix

                shape.nodes.append(node)
                shape.default_rotations.append(Quaternion((1, 0, 0, 0)))
                shape.default_translations.append(Vector())

            attach_node = auto_root_index

        if transform_mat is None:
            attach = shape.nodes[attach_node] if attach_node >= 0 else None
            transform_mat = mesh_object_offset(bobj, attach)

        lod_name_index, lod_name = shape.name_resolve(lod_name)
        ensure_detail_level(shape, scene_lods, lod_name)

        if name in scene_objects:
            existing_node = scene_objects[name][0].node
            if attach_node != -1 and existing_node != -1 and attach_node != existing_node:
                split = unique_object_export_name(name, scene_objects, bobj.name)
                if split != name:
                    note("Warning: Mesh '{}' cannot share object '{}' (different parent node); exporting as '{}'".format(
                        bobj.name, name, split))
                    name = split

        export_name = unique_mesh_export_name(name, lod_name, scene_objects, bobj.name)
        if export_name != name:
            note("Warning: Multiple objects '{}' in LOD '{}', exporting '{}' as '{}'".format(
                name, lod_name, bobj.name, export_name))
            name = export_name

        vis_source = resolve_export_vis_source(bobj)

        if name not in scene_objects:
            object = Object(shape.name(name), numMeshes=0, firstMesh=0, node=attach_node)
            object.has_transparency = False
            object.vis_source = vis_source
            shape.objects.append(object)
            scene_objects[name] = (object, {})
        else:
            if attach_node == -1:
                scene_objects[name][0].node = -1
            if getattr(scene_objects[name][0], "vis_source", None) is None:
                scene_objects[name][0].vis_source = vis_source

        for slot in bobj.material_slots:
            if slot.material and slot.material.torque_props.use_transparency:
                scene_objects[name][0].has_transparency = True

        scene_objects[name][1][lod_name] = (bobj, transform_mat, armature_modifier, mesh_flags)

    return scene_lods, scene_objects, bounds_ob

def apply_detail_visibility(shape):
    for lod in shape.detail_levels:
        if is_auto_billboard_detail(lod):
            continue
        count = 0
        slot = lod.objectDetail
        for obj in shape.objects:
            if slot < 0 or slot >= obj.numMeshes:
                continue
            mesh_index = obj.firstMesh + slot
            if 0 <= mesh_index < len(shape.meshes):
                count += mesh_poly_count(shape.meshes[mesh_index])
        lod.polyCount = count

    smallest_size = 0.0
    smallest_dl = 0
    for i, lod in enumerate(shape.detail_levels):
        if is_auto_billboard_detail(lod) or lod.size <= 0:
            continue
        smallest_size = lod.size
        smallest_dl = i
    shape.smallest_size = smallest_size
    shape.smallest_detail_level = smallest_dl


def _empty_box():
    return Box(
        Vector((10e30, 10e30, 10e30)),
        Vector((-10e30, -10e30, -10e30)))


def _expand_box(box, other):
    box.min.x = min(box.min.x, other.min.x)
    box.min.y = min(box.min.y, other.min.y)
    box.min.z = min(box.min.z, other.min.z)
    box.max.x = max(box.max.x, other.max.x)
    box.max.y = max(box.max.y, other.max.y)
    box.max.z = max(box.max.z, other.max.z)


def _box_from_points(points):
    box = _empty_box()
    for point in points:
        box.min.x = min(box.min.x, point.x)
        box.min.y = min(box.min.y, point.y)
        box.min.z = min(box.min.z, point.z)
        box.max.x = max(box.max.x, point.x)
        box.max.y = max(box.max.y, point.y)
        box.max.z = max(box.max.z, point.z)
    return box


def _box_center(box):
    return (box.min + box.max) * 0.5


def _tge_radius(box):
    return (box.max - box.min).length * 0.5


def _tge_tube_radius(box):
    return Vector((box.max.x - box.min.x, box.max.y - box.min.y)).length * 0.5


def _vec3_prop(ob, key):
    value = ob.get(key)
    if value is None:
        return None
    try:
        return Vector((float(value[0]), float(value[1]), float(value[2])))
    except (TypeError, ValueError, IndexError):
        return None


def _boxes_match(left, right, epsilon=1e-4):
    return (
        (left.min - right.min).length <= epsilon and
        (left.max - right.max).length <= epsilon)


def bounds_object_world_box(bounds_ob):
    mat = bounds_ob.matrix_world
    mesh = getattr(bounds_ob, "data", None)
    if mesh is not None and getattr(mesh, "vertices", None) and mesh.vertices:
        return _box_from_points(mat @ vert.co for vert in mesh.vertices)
    return _box_from_points(mat @ Vector(corner) for corner in bounds_ob.bound_box)


def visible_object_detail_slots(shape):
    return {lod.objectDetail for lod in real_detail_levels(shape.detail_levels) if lod.size >= 0}


def visible_meshes_world_box(shape):
    box = _empty_box()
    visible_slots = visible_object_detail_slots(shape)
    found = False

    for obj in shape.objects:
        for mesh_index in range(obj.numMeshes):
            if visible_slots and mesh_index not in visible_slots:
                continue
            mesh = shape.meshes[obj.firstMesh + mesh_index]
            if mesh.get_type() == Mesh.NullType:
                continue
            if obj.node is None or obj.node < 0 or obj.node >= len(shape.nodes):
                mat = Matrix.Identity(4)
            else:
                mat = getattr(shape.nodes[obj.node], "matrix_world", Matrix.Identity(4))
            _expand_box(box, mesh.calculate_bounds_mat(mat))
            found = True

    return box if found else Box(Vector((-0.5, -0.5, -0.5)), Vector((0.5, 0.5, 0.5)))


def apply_imported_bounds_sphere(bounds_ob, box):
    stored_min = _vec3_prop(bounds_ob, "dts_bounds_min")
    stored_max = _vec3_prop(bounds_ob, "dts_bounds_max")
    if stored_min is None or stored_max is None:
        return None
    if "dts_radius" not in bounds_ob or "dts_radius_tube" not in bounds_ob:
        return None
    if not _boxes_match(box, Box(stored_min, stored_max)):
        return None
    return float(bounds_ob["dts_radius"]), float(bounds_ob["dts_radius_tube"])


def compute_bounds(shape, bounds_ob):
    print("Computing bounds")

    if bounds_ob:
        box = bounds_object_world_box(bounds_ob)
        stored = apply_imported_bounds_sphere(bounds_ob, box)
        if stored:
            shape.radius, shape.radius_tube = stored
            shape.log_radius_source = "imported"
            print("Using imported shape radius ({:.6g})".format(shape.radius))
        else:
            shape.radius = _tge_radius(box)
            shape.radius_tube = _tge_tube_radius(box)
            shape.log_radius_source = "bounds-mesh"
            print("Using bounds-mesh radius ({:.6g})".format(shape.radius))
    else:
        box = visible_meshes_world_box(shape)
        shape.radius = _tge_radius(box)
        shape.radius_tube = _tge_tube_radius(box)
        shape.log_radius_source = "visible-mesh"
        print("Using visible-mesh radius ({:.6g})".format(shape.radius))

    shape.bounds = box
    shape.center = _box_center(box)

def object_morph_meshes(shape, obj):
    meshes = []
    for i in range(obj.numMeshes):
        index = obj.firstMesh + i
        if index < 0 or index >= len(shape.meshes):
            continue
        mesh = shape.meshes[index]
        if mesh_allows_morph(mesh):
            meshes.append(mesh)
    return meshes


def _pad_morph_track(seq, frame_ids, nkeys):
    while len(frame_ids) < len(seq.frameMatters):
        frame_ids.append([0] * nkeys)
    while len(seq.frameMatters) < len(frame_ids):
        seq.frameMatters.append(False)
    while len(seq.visMatters) < len(frame_ids):
        seq.visMatters.append(False)
    while len(seq.matFrameMatters) < len(frame_ids):
        seq.matFrameMatters.append(False)
    for row in frame_ids:
        if len(row) < nkeys:
            row.extend([row[-1] if row else 0] * (nkeys - len(row)))


def _sample_object_lod_poses(scene, meshes, frame, apply_modifiers, depsgraph):
    set_scene_eval_time(scene, frame)
    apply_bone_morph_proxies(scene)
    depsgraph.update()
    poses = {}
    for dmesh in meshes:
        use_mod = getattr(dmesh, "morph_apply_modifiers", apply_modifiers)
        pos, nrm = sample_morph_positions(
            dmesh.morph_ob, dmesh.morph_transform, dmesh.vert_ids,
            depsgraph, use_mod, getattr(dmesh, "loop_ids", None))
        if pos is None:
            return None, (
                "Mesh '{}' morph topology changed (vertex or loop count). "
                "Shape keys cannot add or remove verts.".format(
                    getattr(dmesh, "morph_name", "mesh")))
        poses[dmesh] = (pos, nrm)
    return poses, None


def bake_sequence_morph(context, scene, shape, seq, extra, source,
                        frame_indices, frame_start, frame_end, apply_modifiers,
                        force_export=False):
    nkeys = len(frame_indices)
    frame_ids = [[0] * nkeys for _ in shape.objects]
    action = source.get("action")
    force = bool(extra.get("force_morph"))
    enabled = extra.get("enable_morph") is not False
    if not force and not enabled:
        seq.frameMatters = [False] * len(shape.objects)
        return frame_ids, None

    depsgraph = context.evaluated_depsgraph_get()
    for obj_index, obj in enumerate(list(shape.objects)):
        _pad_morph_track(seq, frame_ids, nkeys)
        collapse_frame = getattr(obj, "morph_collapse_frame", None)
        if collapse_frame is not None:
            seq.frameMatters[obj_index] = True
            for i in range(nkeys):
                frame_ids[obj_index][i] = collapse_frame
            continue
        conflict = skin_animated_morph_reason(
            scene, shape, obj, action, frame_start, frame_end)
        if conflict:
            if not force_export:
                return None, conflict
            note("Warning: Force Export ignoring TGE morph/skin rule:", conflict)
        meshes = object_morph_meshes(shape, obj)
        if not meshes:
            seq.frameMatters[obj_index] = False
            continue
        keyed = False
        if action is not None:
            for dmesh in meshes:
                key = getattr(dmesh.morph_ob.data, "shape_keys", None)
                if fcurves_shape_key_keyed_in_range(
                        action, key, frame_start, frame_end):
                    keyed = True
                    break
                if fcurves_bone_morph_keyed_in_range(
                        action, scene, dmesh.morph_ob, frame_start, frame_end):
                    keyed = True
                    break
        source_primary = meshes[0]
        samples = []
        for frame in frame_indices:
            poses, err = _sample_object_lod_poses(
                scene, meshes, frame, apply_modifiers, depsgraph)
            if err:
                return None, err
            samples.append(poses)
        rest = list(source_primary.verts[:source_primary.vertsPerFrame])
        moved = any(
            morph_verts_differ(poses[source_primary][0], rest) for poses in samples)
        if not (force or keyed or moved):
            seq.frameMatters[obj_index] = False
            continue
        seq.frameMatters[obj_index] = True
        last_poses = {
            dmesh: (list(dmesh.verts[:dmesh.vertsPerFrame]),
                    list(dmesh.normals[:dmesh.vertsPerFrame]))
            for dmesh in meshes}
        last_index = 0
        live_obj = obj
        live_index = obj_index
        live_meshes = meshes
        appended = 0
        reused = 0
        for i, poses in enumerate(samples):
            pos, nrm = poses[source_primary]
            if not morph_verts_differ(pos, last_poses[live_meshes[0]][0]):
                reused += 1
                frame_ids[live_index][i] = last_index
                continue
            if not all(morph_mesh_can_append(dmesh, 2) for dmesh in live_meshes):
                collapse_by_mesh = {}
                real_by_mesh = {}
                live_now, err = _sample_object_lod_poses(
                    scene, live_meshes, frame_indices[i], apply_modifiers, depsgraph)
                if err:
                    return None, err
                for dmesh in live_meshes:
                    last_pos, last_nrm = last_poses[dmesh]
                    collapse_pos = collapse_morph_positions(last_pos)
                    collapse_nrm = list(last_nrm) if last_nrm else last_pos
                    if not morph_mesh_can_append(dmesh, 1):
                        return None, (
                            "Mesh '{}' morph would exceed 65535 verts "
                            "(DTS v24 index limit). Use fewer frames or a "
                            "lower-poly mesh.".format(
                                getattr(dmesh, "morph_name", "mesh")))
                    err = append_mesh_morph_frame(dmesh, collapse_pos, collapse_nrm)
                    if err:
                        return None, err
                    collapse_by_mesh[dmesh] = (collapse_pos, collapse_nrm)
                    real_by_mesh[dmesh] = live_now[dmesh]
                live_obj.morph_collapse_frame = live_meshes[0].numFrames - 1
                for k in range(i, nkeys):
                    frame_ids[live_index][k] = live_obj.morph_collapse_frame
                new_obj, err = add_morph_overflow_object(
                    shape, live_obj, collapse_by_mesh, real_by_mesh)
                if err:
                    return None, err
                note("Sequence '{}' morph '{}' overflowed; continued on {} "
                     "({} frames on previous object)"
                     .format(seq.name, getattr(source_primary, "morph_name", "?"),
                             shape.names[new_obj.name], live_meshes[0].numFrames))
                seq.frameMatters.append(True)
                seq.visMatters.append(False)
                seq.matFrameMatters.append(False)
                frame_ids.append([0] * nkeys)
                live_index = len(shape.objects) - 1
                live_obj = new_obj
                live_meshes = object_morph_meshes(shape, new_obj)
                last_index = live_meshes[0].numFrames - 1
                last_poses = {
                    dmesh: (list(dmesh.verts[last_index * dmesh.vertsPerFrame:
                                             (last_index + 1) * dmesh.vertsPerFrame]),
                            list(dmesh.normals[last_index * dmesh.vertsPerFrame:
                                               (last_index + 1) * dmesh.vertsPerFrame]
                                 if dmesh.normals else []))
                    for dmesh in live_meshes}
                appended += 1
                frame_ids[live_index][i] = last_index
                continue
            err = append_mesh_morph_frame(live_meshes[0], pos, nrm)
            if err:
                return None, err
            last_index = live_meshes[0].numFrames - 1
            last_poses[live_meshes[0]] = (pos, nrm)
            appended += 1
            for other in live_meshes[1:]:
                other_pose = poses.get(other)
                if other_pose is None:
                    lod_poses, err = _sample_object_lod_poses(
                        scene, [other], frame_indices[i], apply_modifiers, depsgraph)
                    if err:
                        return None, err
                    other_pose = lod_poses[other]
                err = append_mesh_morph_frame(other, other_pose[0], other_pose[1])
                if err:
                    return None, err
                last_poses[other] = other_pose
            frame_ids[live_index][i] = last_index
        note("Sequence '{}' morph '{}': {} new snapshots, {} holds reused"
             .format(seq.name, getattr(source_primary, "morph_name", "?"), appended, reused))
    _pad_morph_track(seq, frame_ids, nkeys)
    return frame_ids, None


def _dts_export_meta(filepath, mode, export_root, select_object, select_marker,
                     sequence_mode, blank_material, generate_texture,
                     apply_modifiers, optimize_meshes, scale, force_export):
    return io_meta(
        "export dts", filepath,
        export_scope=mode if export_root is None else "COLLECTION",
        select_object=select_object,
        select_marker=select_marker,
        sequence_mode=sequence_mode,
        blank_material=blank_material,
        generate_texture=generate_texture,
        apply_modifiers=apply_modifiers,
        optimize_meshes=optimize_meshes,
        scale=scale,
        force_export=force_export,
        collection=getattr(export_root, "name", "") or "",
        file_version=24)


def _run_save_dts(operator, context, filepath, log_level, debug_report,
                  select_object, select_marker, sequence_mode, blank_material,
                  generate_texture, apply_modifiers, optimize_meshes, export_kind,
                  scale, force_export, export_root, mode,
                  sequences_override=None, action_objects=None,
                  show_all_actions=False, collection_roots=None,
                  strip_action_suffixes=True):
    log = IoLog(normalize_log_level(log_level, debug_report))
    set_active_log(log)
    meta = _dts_export_meta(
        filepath, mode, export_root, select_object, select_marker,
        sequence_mode, blank_material, generate_texture,
        apply_modifiers, optimize_meshes, scale, force_export)
    try:
        return _save_dts(
            operator, context, filepath, log,
            select_object, select_marker, sequence_mode, blank_material,
            generate_texture, apply_modifiers, optimize_meshes, export_kind,
            scale, force_export, export_root,
            sequences_override=sequences_override,
            action_objects=action_objects,
            show_all_actions=show_all_actions,
            collection_roots=collection_roots,
            strip_action_suffixes=strip_action_suffixes)
    except Exception as exc:
        if not log.error:
            log.error = str(exc)
            note("Error:", exc)
        raise
    finally:
        finish_log(filepath, log, shape=log.shape, meta=meta, error=log.error)
        set_active_log(None)


def save(operator, context, filepath,
         select_object=False,
         select_marker=False,
         sequence_mode="ACTIONS",
         blank_material=True,
         generate_texture="disabled",
         apply_modifiers=True,
         optimize_meshes=True,
         debug_report=False,
         log_level="DISABLED",
         export_kind="DTS",
         scale=1.0,
         force_export=False,
         collection="",
         limit_collection=None,
         export_scope="SCENE",
         collection_names=None,
         show_all_actions=False,
         strip_action_suffixes=True):
    print("Exporting scene to DTS")
    collection = (collection or "").strip()
    mode = (export_scope or "SCENE").upper()
    names = [name for name in (collection_names or ()) if name]
    show_all = bool(show_all_actions)
    strip_suffixes = bool(strip_action_suffixes)
    scene = getattr(context, "scene", None)

    def _write(path, export_root, select_object_flag, sequences_override=None,
               collection_roots=None):
        return _run_save_dts(
            operator, context, path, log_level, debug_report,
            select_object_flag, select_marker, sequence_mode, blank_material,
            generate_texture, apply_modifiers, optimize_meshes, export_kind,
            scale, force_export, export_root, mode,
            sequences_override=sequences_override,
            show_all_actions=show_all,
            collection_roots=collection_roots,
            strip_action_suffixes=strip_suffixes)

    with restore_scene_timeline(scene):
        if mode == "COLLECTIONS":
            if not names:
                return fail(operator, "No collections selected")
            folder = export_directory(filepath)
            used = set()
            wrote = 0
            result = {"FINISHED"}
            for name in names:
                export_root = resolve_export_collection(name, limit_collection)
                if export_root is None:
                    note("Warning: Export collection '{}' was not found; skipping"
                         .format(name))
                    continue
                out = unique_output_path(
                    os.path.join(folder, sanitize_dsq_stem(export_root.name) + ".dts"),
                    used)
                if (sequence_mode or "ACTIONS").upper() == "MARKERS":
                    file_seqs = find_export_sequences(
                        scene, select_marker, sequence_mode,
                        export_kind=export_kind)
                else:
                    page_names = collection_page_selected_names(operator, name)
                    if page_names is None:
                        file_seqs = find_export_sequences(
                            scene, select_marker, sequence_mode,
                            export_kind=export_kind,
                            collection_roots=[export_root] if export_root else [],
                            show_all=show_all,
                            strip_action_suffixes=strip_suffixes)
                    else:
                        file_seqs = find_export_sequences(
                            scene, select_marker, sequence_mode,
                            export_kind=export_kind,
                            selected_names=page_names,
                            collection_roots=None,
                            show_all=True,
                            strip_action_suffixes=strip_suffixes)
                result = _write(out, export_root, False, file_seqs)
                if result != {"FINISHED"}:
                    return result
                note("Wrote DTS", out)
                wrote += 1
            if not wrote:
                return fail(operator, "No collections selected")
            return result

        filepath = resolve_export_filepath(filepath, ".dts")
        if mode == "COLLECTION" and not collection:
            note("Warning: Export Collection is selected but no collection was chosen; exporting the whole scene")
        if collection:
            export_root = resolve_export_collection(collection, limit_collection)
            select_object = False
        else:
            export_root = resolve_export_collection("", limit_collection)
            select_object = bool(select_object) or mode == "SELECTION"
        roots = [export_root] if mode == "COLLECTION" and export_root else None
        return _write(filepath, export_root, select_object, collection_roots=roots)


def _save_dts(operator, context, filepath, log,
              select_object, select_marker, sequence_mode, blank_material,
              generate_texture, apply_modifiers, optimize_meshes, export_kind,
              scale, force_export, export_root=None,
              sequences_override=None, action_objects=None,
              show_all_actions=False, collection_roots=None,
              strip_action_suffixes=True):
    scene = context.scene
    active = context.active_object
    shape = DtsShape()
    log.shape = shape
    blank_material_index = None
    scope = export_object_scope(export_root)
    if export_root is not None:
        note("Note: Limiting DTS export to collection '{}'".format(export_root.name))

    reference_frame = find_reference(scene)

    if reference_frame is not None:
        note("Note: Seeking to reference frame at", reference_frame)
        scene.frame_set(reference_frame)

    always_export, never_export, never_animate = read_node_export_lists()

    node_lookup = save_nodes(
        scene, shape, select_object, always_export, never_export, scope=scope)
    mesh_result = save_meshes(
        scene, shape, node_lookup, select_object,
        blank_material=blank_material, optimize_meshes=optimize_meshes,
        operator=operator, force_export=force_export,
        export_root=export_root, scope=scope)
    if isinstance(mesh_result, set):
        return mesh_result
    scene_lods, scene_objects, bounds_ob = mesh_result
    set_reason = tge_shape_set_error(len(shape.nodes), len(shape.objects))
    if set_reason:
        if force_export:
            note("Warning: Force Export ignoring TGE node/object limit:", set_reason)
            if operator is not None:
                operator.report({"WARNING"}, set_reason)
        elif operator is None:
            raise ValueError(set_reason)
        else:
            return fail(operator, set_reason)
    register_detail_markers(scene, shape, scene_lods, scope=scope)
    append_auto_billboard_details(scene, shape, scope=scope)

    # If the shape is empty, add a detail level so it is valid
    if not shape.detail_levels:
        dl = DetailLevel(name=shape.name('detail1'), subshape=0, objectDetail=-1, size=1)
        shape.detail_levels.append(dl)

    # Put objects with transparent materials last. Default objectstates
    # are written after this sort so they stay aligned with objects.
    shape.objects.sort(key=lambda object: object.has_transparency) # TODO: attrgetter

    shape.objectstates = []
    for obj in shape.objects:
        shape.objectstates.append(
            ObjectState(sample_export_vis(getattr(obj, "vis_source", None)), 0, 0))

    # Sort detail levels
    shape.detail_levels.sort(key=attrgetter("size"), reverse=True)

    real_index = 0
    for lod in shape.detail_levels:
        if is_auto_billboard_detail(lod):
            continue
        lod.objectDetail = real_index
        real_index += 1

    print("Adding meshes to objects...")
    shape_key_restore = snapshot_scene_shape_key_values(scene)
    reset_scene_shape_keys(scene)

    material_table = {}
    mesh_lods = real_detail_levels(shape.detail_levels)

    for object, lods in scene_objects.values():
        object.firstMesh = len(shape.meshes)

        for i, lod in enumerate(reversed(mesh_lods)):
            if shape.names[lod.name] in lods:
                object.numMeshes = len(mesh_lods) - i
                break
        else:
            object.numMeshes = 0
            continue

        for i in range(object.numMeshes):
            lod = mesh_lods[i]
            lod_name = shape.names[lod.name]

            if lod_name in lods:
                print("Exporting mesh '{}' (LOD '{}')".format(shape.names[object.name], lod_name))
                bobj, transform_mat, armature_modifier, mesh_flags = lods[lod_name]

                if armature_modifier is None:
                    mesh_type = Mesh.StandardType
                else:
                    mesh_type = Mesh.SkinType
                    armature = armature_modifier.object

                #########################
                ### Welcome to complexity

                # Disable the armature modifier so it does not deform the mesh
                # when writing it to the DTS file
                if armature_modifier is not None:
                    was_show_render = armature_modifier.show_render
                    was_show_viewport = armature_modifier.show_viewport

                    armature_modifier.show_render = False
                    armature_modifier.show_viewport = False

                depsgraph = context.evaluated_depsgraph_get()
                if armature_modifier is not None:
                    depsgraph.update()

                if apply_modifiers:
                    eval_ob = bobj.evaluated_get(depsgraph)
                else:
                    eval_ob = bobj

                try:
                    mesh = eval_ob.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
                    bm = bmesh.new()
                    bm.from_mesh(mesh)
                    bmesh.ops.triangulate(bm, faces=bm.faces)
                    bm.to_mesh(mesh)
                    bm.free()

                    # This is the danger zone
                    # Data from down here may not stay around!

                    dmesh = Mesh(mesh_type)
                    if mesh_flags:
                        dmesh.set_flags(mesh_flags)
                    shape.meshes.append(dmesh)

                    dmesh.matrix_world = bobj.matrix_world
                    dmesh.morph_ob = None
                    dmesh.source_ob = bobj
                    dmesh.morph_transform = transform_mat.copy()
                    dmesh.morph_name = bobj.name
                    dmesh.morph_apply_modifiers = apply_modifiers
                    if mesh_type == Mesh.SkinType:
                        key = getattr(bobj.data, "shape_keys", None)
                        if key is not None and len(key.key_blocks) > 1:
                            note("Warning: Mesh '{}' is a skin with shape keys. "
                                 "TGE cannot morph skins; parent a rigid mesh "
                                 "to a bone.".format(bobj.name))

                    collision_mesh = (
                        is_collision_or_los_detail(lod_name) or
                        collision_detail_from_mesh_name(undup_name(bobj.name)) is not None)
                    if (mesh_type == Mesh.StandardType and not collision_mesh
                            and not (mesh_flags & Mesh.Billboard)):
                        dmesh.morph_ob = bobj

                    # Group all materials by their material_index
                    if collision_mesh:
                        grouped_polys = ((None, tuple(mesh.polygons)),)
                    else:
                        key = attrgetter("material_index")
                        grouped_polys = groupby(sorted(mesh.polygons, key=key), key=key)
                        grouped_polys = tuple(map(lambda t: (t[0], tuple(t[1])), grouped_polys))

                    # Weld islands from Blender smooth/sharp. Max zeros
                    # skin groups then recomputes; that smears grooves
                    # into neighboring flats. Keep authoring islands.
                    face_smooth = assign_face_smooth_ids(mesh)

                    # Create a primitive from each group
                    for material_index, polys in grouped_polys:
                        flags = Primitive.Triangles | Primitive.Indexed
                        bmat = None

                        if collision_mesh:
                            flags |= Primitive.NoMaterial
                        else:
                            if mesh.materials:
                                bmat = mesh.materials[material_index]

                            if bmat:
                                if bmat not in material_table:
                                    material_table[bmat] = export_material(bmat, shape)

                                flags |= material_table[bmat] & Primitive.MaterialMask
                            elif blank_material:
                                if blank_material_index is None:
                                    blank_material_index = len(shape.materials)
                                    shape.materials.append(Material(name="blank",
                                        flags=Material.SWrap | Material.TWrap | Material.NeverEnvMap))

                                flags |= blank_material_index & Primitive.MaterialMask
                            else:
                                flags |= Primitive.NoMaterial

                        firstElement = len(dmesh.indices)
                        two_sided = (not collision_mesh and bmat is not None
                                     and getattr(bmat, "torque_props", None)
                                     and bmat.torque_props.two_sided)

                        for poly in polys:
                            if mesh.uv_layers:
                                uv_layer = mesh.uv_layers[0].data
                            else:
                                uv_layer = None

                            corners = list(zip(reversed(poly.vertices), reversed(poly.loop_indices)))
                            emitted = []

                            for vert_index, loop_index in corners:
                                vertex_index = len(dmesh.verts)
                                dmesh.indices.append(vertex_index)
                                emitted.append(vertex_index)

                                vert = mesh.vertices[vert_index]
                                normal = corner_export_normal(mesh, poly, loop_index, vert)

                                dmesh.verts.append(transform_mat @ vert.co)
                                dmesh.normals.append((transform_mat.to_3x3() @ normal).normalized())
                                dmesh.enormals.append(0)
                                dmesh.vert_ids.append(vert_index)
                                dmesh.loop_ids.append(loop_index)
                                if face_smooth is not None:
                                    dmesh.smooth_ids.append(face_smooth[poly.index])
                                else:
                                    dmesh.smooth_ids.append(0)

                                if uv_layer:
                                    uv = uv_layer[loop_index].uv
                                    dmesh.tverts.append(Vector((uv.x, 1 - uv.y)))
                                else:
                                    dmesh.tverts.append(Vector((0, 0)))

                                if mesh_type == Mesh.SkinType:
                                    add_vertex_influences(bobj, armature,
                                                          node_lookup, dmesh,
                                                          vert, vertex_index)

                            # Max two-sided is a reverse winding on the
                            # same verts, not a second flipped-normal copy.
                            if two_sided:
                                dmesh.indices.extend(reversed(emitted))
                                dmesh.log_two_sided = True

                        numElements = len(dmesh.indices) - firstElement
                        dmesh.primitives.append(Primitive(firstElement, numElements, flags))

                    dmesh.vertsPerFrame = len(dmesh.verts)
                    if optimize_meshes:
                        print("Optimizing mesh '{}'".format(bobj.name))
                        optimize_exported_mesh(dmesh)
                        dmesh.vertsPerFrame = len(dmesh.verts)

                    apply_tge_mesh_sphere(dmesh)

                    if (mesh_flags & Mesh.Billboard) and not (mesh_flags & Mesh.BillboardZAxis):
                        orient_spherical_billboard(dmesh)

                    # After weld/recompute and billboard rotate. Do not set
                    # Mesh.UseEncodedNormals; TGE still prefers float normals.
                    encode_mesh_normals(dmesh)

                    if fit_mesh_primitives_to_s16(dmesh):
                        print("  packed '{}' primitives to fit TGE signed 16-bit start/count ({} indices, {} prims)".format(
                            bobj.name, len(dmesh.indices), len(dmesh.primitives)))

                    reason = tge_mesh_draw_error(dmesh, bobj.name)
                    if reason:
                        if force_export:
                            note("Warning: Force Export ignoring TGE primitive limit:", reason)
                            if operator is not None:
                                operator.report({"WARNING"}, reason)
                        elif operator is None:
                            restore_shape_key_values_only(shape_key_restore)
                            raise ValueError(reason)
                        else:
                            restore_shape_key_values_only(shape_key_restore)
                            return fail(operator, reason)
                finally:
                    eval_ob.to_mesh_clear()

                    if armature_modifier is not None:
                        armature_modifier.show_render = was_show_render
                        armature_modifier.show_viewport = was_show_viewport

                ### Nobody leaves Hotel California
            else:
                # print("Adding Null mesh for object {} in LOD {}".format(shape.names[object.name], lod_name))
                shape.meshes.append(Mesh(Mesh.NullType))

    apply_material_extra_maps(shape, material_table)

    print("Creating subshape with " + str(len(shape.nodes)) + " nodes and " + str(len(shape.objects)) + " objects")
    shape.subshapes.append(Subshape(0, 0, 0, len(shape.nodes), len(shape.objects), 0))

    # Figure out all the things
    compute_bounds(shape, bounds_ob)
    apply_detail_visibility(shape)
    ground_ob = find_bounds_object(scene, bounds_ob, scope=scope)

    attach_ifl_sidecars(
        shape, filepath,
        os.path.dirname(filepath),
        os.path.dirname(bpy.data.filepath) if bpy.data.filepath else None)
    ifl_matters = ifl_matters_from_shape(shape)

    sequences = sequences_override
    if sequences is None:
        sequences = find_export_sequences(
            context.scene, select_marker, sequence_mode, export_kind=export_kind,
            objects=action_objects, show_all=show_all_actions,
            collection_roots=collection_roots,
            strip_action_suffixes=strip_action_suffixes)
    scene_fps = context.scene.render.fps / context.scene.render.fps_base

    # every sequence's scale keys, written after the loop with one scale type for the whole shape
    # (apply_shape_sequence_scales: TGE breaks on mixed uniform / aligned sequences)
    scale_entries = []
    for source in sequences:
        name = source["name"]
        print("Exporting sequence", name)

        if source["action"] is None:
            markers = source["markers"]
            if "start" not in markers:
                restore_shape_key_values_only(shape_key_restore)
                return fail(operator, "Missing start marker for sequence '{}'".format(name))
            if "end" not in markers:
                restore_shape_key_values_only(shape_key_restore)
                return fail(operator, "Missing end marker for sequence '{}'".format(name))
            frame_start = markers["start"].frame
            frame_end = markers["end"].frame
        else:
            frame_start = source["frame_start"]
            frame_end = source["frame_end"]

        frame_range = int(frame_end) - int(frame_start) + 1

        seq = Sequence()
        seq.nameIndex = shape.name(name)
        seq.name = name
        seq.flags = Sequence.AlignedScale
        seq.priority = 1

        seq.toolBegin = frame_start
        seq.duration = frame_range / scene_fps

        if source["action"] is not None:
            extra = apply_action_extra(seq, source["extra"], frame_range, scene_fps)
            pending_triggers = extra.get("triggers", [])
        else:
            extra = apply_sequence_flags(
                seq, source.get("flags", ()), frame_range, scene_fps)
            pending_triggers = list(source.get("triggers", [])) + extra["triggers"]

        frame_indices = sequence_eval_times(frame_start, frame_end, extra)
        seq.numKeyframes = len(frame_indices)
        seq.firstGroundFrame = len(shape.ground_translations)
        seq.baseRotation = len(shape.node_rotations)
        seq.baseTranslation = len(shape.node_translations)
        seq.baseScale = 0
        seq.baseObjectState = len(shape.objectstates)
        seq.baseDecalState = len(shape.decalstates)
        append_sequence_triggers(
            seq, pending_triggers, shape.triggers, frame_start, frame_end)

        seq.rotationMatters = [False] * len(shape.nodes)
        seq.translationMatters = [False] * len(shape.nodes)
        seq.scaleMatters = [False] * len(shape.nodes)
        seq.decalMatters = [False] * len(shape.nodes)
        seq.iflMatters = list(ifl_matters)
        seq.visMatters = [False] * len(shape.objects)
        seq.frameMatters = [False] * len(shape.objects)
        seq.matFrameMatters = [False] * len(shape.objects)

        shape.sequences.append(seq)

        pending_scales = []

        with assign_armature_action(scene, source["action"]):
            animation_data = {frame: {} for frame in frame_indices}
            object_vis = {frame: {} for frame in frame_indices}

            for frame in frame_indices:
                set_scene_eval_time(scene, frame)
                for node in shape.nodes:
                    animation_data[frame][node] = sample_export_node(node)
                for obj in shape.objects:
                    object_vis[frame][obj] = sample_export_vis(
                        getattr(obj, "vis_source", None))

            if ground_ob is not None:
                ground_frames = ground_frame_indices(
                    frame_start, frame_end, scene_fps, extra.get("ground_frame_rate"))
                append_ground_frames(
                    seq, shape.ground_translations, shape.ground_rotations,
                    sample_world_frames(scene, ground_ob, ground_frames),
                    extra.get("ignore_ground", False))

            blend_ref = resolve_blend_reference_frame(
                extra, reference_frame, frame_start, frame_end)

            for ob in shape.nodes:
                index = ob.index
                node = shape.nodes[index]
                node_name = shape.names[node.name]
                if name_listed(node_name, never_animate):
                    continue

                fcurves = export_node_fcurves(ob)
                if fcurves is None:
                    continue

                curves_rotation, curves_translation, curves_scale, _curves_vis = (
                    export_node_curve_arrays(ob, fcurves))

                if curves_rotation and fcurves_keyframe_in_range(curves_rotation, frame_start, frame_end):
                    seq.rotationMatters[index] = True

                if curves_translation and fcurves_keyframe_in_range(curves_translation, frame_start, frame_end):
                    seq.translationMatters[index] = True

                if curves_scale and fcurves_keyframe_in_range(curves_scale, frame_start, frame_end):
                    seq.scaleMatters[index] = True

            if extra.get("enable_vis") is not False:
                for obj_index, obj in enumerate(shape.objects):
                    vis_source = getattr(obj, "vis_source", None)
                    curves = vis_source_curve_array(vis_source)
                    has_keys = bool(
                        curves and fcurves_keyframe_in_range(
                            curves, frame_start, frame_end))
                    values = [object_vis[frame][obj] for frame in frame_indices]
                    if has_keys or vis_values_differ(values):
                        seq.visMatters[obj_index] = True

            apply_sequence_membership(seq, extra, len(shape.nodes), len(shape.objects))
            apply_armature_object_track_matters(seq, extra, (
                (node.index, getattr(node, "armature", None), getattr(node, "bl_ob", None))
                for node in shape.nodes
                if getattr(node, "armature", None) is not None
                and not name_listed(shape.names[node.name], never_animate)
            ))
            morph_frame_ids, morph_error = bake_sequence_morph(
                context, scene, shape, seq, extra, source,
                frame_indices, frame_start, frame_end, apply_modifiers,
                force_export)
            if morph_error:
                restore_shape_key_values_only(shape_key_restore)
                return fail(operator, morph_error)
            while len(seq.visMatters) < len(shape.objects):
                seq.visMatters.append(False)
            while len(seq.frameMatters) < len(shape.objects):
                seq.frameMatters.append(False)
            while len(seq.matFrameMatters) < len(shape.objects):
                seq.matFrameMatters.append(False)
            for obj_index, obj in enumerate(shape.objects):
                if obj_index >= len(seq.visMatters) or not seq.visMatters[obj_index]:
                    continue
                source = getattr(obj, "vis_source", None)
                if source is None:
                    continue
                for other_index, other in enumerate(shape.objects):
                    if getattr(other, "vis_source", None) is source:
                        seq.visMatters[other_index] = True

            for ob in shape.nodes:
                index = ob.index
                node = shape.nodes[index]
                node_name = shape.names[node.name]
                if name_listed(node_name, never_animate):
                    continue
                if not (seq.rotationMatters[index] or seq.translationMatters[index]
                        or seq.scaleMatters[index]):
                    continue

                base_translation, base_rotation, _ = node.matrix.decompose()
                if seq.flags & Sequence.Blend and blend_ref is not None:
                    if blend_ref in animation_data and ob in animation_data[blend_ref]:
                        base_translation, base_rotation = animation_data[blend_ref][ob][:2]
                    else:
                        scene.frame_set(blend_ref)
                        base_translation, base_rotation = sample_export_node(ob)[:2]

                for frame in frame_indices:
                    translation, rotation, scale, _vis = animation_data[frame][ob]

                    if seq.translationMatters[index]:
                        if seq.flags & Sequence.Blend:
                            translation -= base_translation
                        shape.node_translations.append(translation)

                    if seq.rotationMatters[index]:
                        if seq.flags & Sequence.Blend:
                            rotation = base_rotation.inverted() @ rotation
                        shape.node_rotations.append(rotation)

                    if seq.scaleMatters[index]:
                        pending_scales.append(scale)

            for obj_index, obj in enumerate(shape.objects):
                if not object_has_object_state(seq, obj_index):
                    continue
                for i, frame in enumerate(frame_indices):
                    vis = object_vis[frame].get(obj)
                    if vis is None:
                        family = getattr(obj, "morph_family", None)
                        vis = object_vis[frame].get(family)
                    if vis is None:
                        vis = sample_export_vis(getattr(obj, "vis_source", None))
                    if vis < 0.0:
                        vis = 0.0
                    elif vis > 1.0:
                        vis = 1.0
                    frame_id = 0
                    if seq.frameMatters[obj_index] and morph_frame_ids is not None:
                        frame_id = morph_frame_ids[obj_index][i]
                    shape.objectstates.append(ObjectState(vis, frame_id, 0))

        scale_entries.append((seq, pending_scales))

    apply_shape_sequence_scales(
        scale_entries, shape.node_uniform_scales, shape.node_aligned_scales)

    scale_shape_world_units(shape, scale)

    shape.verify()

    with open(filepath, "wb") as fd:
        shape.save(fd)

    write_material_textures(generate_texture, filepath, shape)

    restore_shape_key_values_only(shape_key_restore)
    return {"FINISHED"}

def write_material_textures(mode, filepath, shape):
    if mode == 'disabled':
        return

    f_lookup = mode in ("custom-missing", "all-missing")
    f_custom = mode in ("custom-missing", "custom-always")

    for material in shape.materials:
        if not hasattr(material, "bl_mat"):
            continue

        if f_custom and material.name.lower() in default_materials:
            continue

        if f_lookup and resolve_texture(filepath, material.name) is not None:
            continue

        bl_mat = material.bl_mat
        new_color = [0, 0, 0]

        if bl_mat.use_nodes:
            for node in bl_mat.node_tree.nodes:
                if node.type == "BSDF_PRINCIPLED":
                    color = node.inputs['Base Color'].default_value

                    new_color[0] = linearrgb_to_srgb(color[0])
                    new_color[1] = linearrgb_to_srgb(color[1])
                    new_color[2] = linearrgb_to_srgb(color[2])

                    print(new_color)
        else:
            color = bl_mat.diffuse_color
            new_color[0] = linearrgb_to_srgb(color[0])
            new_color[1] = linearrgb_to_srgb(color[1])
            new_color[2] = linearrgb_to_srgb(color[2])

            print("Using viewport color")

        image = bpy.data.images.new(material.name.lower() + "_generated", 16, 16)
        image.pixels = (new_color[0], new_color[1], new_color[2], 1.0) * 256
        image.filepath_raw = os.path.join(os.path.dirname(filepath), material.name + ".png")
        image.file_format = "PNG"
        image.save()
