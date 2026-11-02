import bpy
import os
from .DtsShape import DtsShape
from .DtsTypes import *
from .write_report import (
    IoLog, finish_log, io_meta, normalize_log_level, note, set_active_log)
from .util import default_materials, resolve_fallback_texture, resolve_texture, get_rgb_colors, fail, \
    ob_location_curves, ob_scale_curves, ob_rotation_curves, ob_rotation_data, evaluate_all
from .shared_export import (
    add_imported_morph_shape_keys, apply_auto_billboard_settings,
    apply_imported_morph_actions, apply_imported_morph_timeline,
    assign_vis_value, decode_trigger_state,
    details_for_mesh_slot, ensure_import_detail_collection,
    lod_import_collection_name,
    ensure_import_shape_collection, import_armature_sequences,
    mesh_frame_vert_count, mesh_frame_slice, mesh_import_shading_frame,
    scale_shape_world_units,
    import_empty_arbitrary_scale, import_object_visibility,
    import_parent_collection, is_auto_billboard_detail, key_object_vis,
    link_object_to_collection, sequence_object_vis_values,
    unpack_auto_billboard)

import operator
from itertools import zip_longest, count
from functools import reduce
from random import random

def grouper(iterable, n, fillvalue=None):
    "Collect data into fixed-length chunks or blocks"
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return zip_longest(*args, fillvalue=fillvalue)

def import_auto_billboard_empties(shape, parent_collection):
    for lod in shape.detail_levels:
        if not is_auto_billboard_detail(lod):
            continue
        size = int(lod.size)
        name = dedup_name(bpy.data.objects, "BB::detail{}".format(size))
        ob = bpy.data.objects.new(name, None)
        ob.empty_display_type = "PLAIN_AXES"
        ob.empty_display_size = 0.35
        apply_auto_billboard_settings(ob, unpack_auto_billboard(lod.objectDetail), size)
        link_object_to_collection(ob, parent_collection)
        print("Imported auto-billboard LOD '{}' (size {})".format(name, size))


def dedup_name(group, name):
    if name not in group:
        return name

    for suffix in count(2):
        new_name = name + "#" + str(suffix)

        if new_name not in group:
            return new_name

def import_material(color_source, dmat, filepath):
    bmat = bpy.data.materials.new(dedup_name(bpy.data.materials, dmat.name))
    bmat.use_nodes = True
    bmat.roughness = 1

    texname = resolve_texture(filepath, dmat.name)
    used_fallback = False
    if texname is None:
        texname = resolve_fallback_texture(dmat.name)
        used_fallback = texname is not None
        if used_fallback:
            note("Using bundled fallback texture '{}'".format(
                dmat.name.split("#", 1)[0]))

    if texname is not None:
        teximg = None
        try:
            teximg = bpy.data.images.load(texname)
        except Exception:
            print("Cannot load image", texname)

        if teximg is not None:
            bsdf = bmat.node_tree.nodes["Principled BSDF"]
            tex = bmat.node_tree.nodes.new("ShaderNodeTexImage")
            tex.image = teximg
            bmat.node_tree.links.new(bsdf.inputs["Base Color"], tex.outputs["Color"])

        # Try to figure out a diffuse color for solid shading
        if teximg is not None and teximg.size[0] <= 16 and teximg.size[1] <= 16:
            if teximg.alpha_mode != "NONE":
                pixels = grouper(teximg.pixels, 4)
            else:
                pixels = grouper(teximg.pixels, 3)

            color = pixels.__next__()

            for other in pixels:
                if other != color:
                    break
            else:
                bmat.diffuse_color = color[:3] + (1,)
    elif dmat.name.lower() in default_materials:
        bmat.diffuse_color = default_materials[dmat.name.lower()]
    else: # give it a random color
        bmat.diffuse_color = color_source.__next__()

    if dmat.flags & Material.SelfIlluminating:
        bmat.torque_props.use_shadeless = True
    if dmat.flags & Material.Translucent:
        bmat.torque_props.use_transparency = True

    if dmat.flags & Material.Additive:
        bmat.torque_props.blend_mode = "ADDITIVE"
    elif dmat.flags & Material.Subtractive:
        bmat.torque_props.blend_mode = "SUBTRACTIVE"
    else:
        bmat.torque_props.blend_mode = "NONE"

    bmat.torque_props.s_wrap = bool(dmat.flags & Material.SWrap)
    bmat.torque_props.t_wrap = bool(dmat.flags & Material.TWrap)
    bmat.torque_props.never_env_map = bool(dmat.flags & Material.NeverEnvMap)
    if dmat.flags & Material.IFLMaterial:
        bmat.torque_props.use_ifl = True

    if dmat.flags & Material.NoMipMap:
        bmat.torque_props.no_mip_mapping = True
    if dmat.flags & Material.MipMapZeroBorder:
        bmat.torque_props.mip_map_zero_border = True

    return bmat


def _extra_map_name(idx, dmat, dmat_list, materials):
    if idx is None or idx < 0 or idx >= len(dmat_list):
        return ""
    target = dmat_list[idx]
    if target is dmat:
        return ""
    target_bmat = materials.get(target)
    if target_bmat is not None:
        return target_bmat.name
    return target.name


def import_material_extra_maps(shape, materials):
    dmat_list = list(shape.materials)
    for dmat in dmat_list:
        bmat = materials.get(dmat)
        if bmat is None:
            continue
        props = bmat.torque_props
        props.detail_scale = dmat.detailScale
        if dmat.flags & Material.NeverEnvMap:
            props.env_mapping = 0.0
            props.never_env_map = True
        else:
            props.env_mapping = max(0.0, min(1.0, float(dmat.reflectance)))
            props.never_env_map = False
        props.detail_map = _extra_map_name(dmat.detailMap, dmat, dmat_list, materials)
        props.bump_map = _extra_map_name(dmat.bumpMap, dmat, dmat_list, materials)
        props.reflectance_map = _extra_map_name(
            dmat.reflectanceMap, dmat, dmat_list, materials)

class index_pass:
    def __getitem__(self, item):
        return item

def _usable_tri(tri, vert_count):
    a, b, c = tri
    if a == b or b == c or a == c:
        return False
    return 0 <= a < vert_count and 0 <= b < vert_count and 0 <= c < vert_count


def _tri_winding_score(tri, verts, normals):
    a, b, c = (Vector(verts[i]) for i in tri)
    face_n = (b - a).cross(c - a)
    if face_n.length_squared < 1e-16:
        return -2.0
    face_n.normalize()
    if not normals:
        return 0.0
    total = 0.0
    for index in tri:
        normal = Vector(normals[index])
        if normal.length_squared < 1e-16:
            continue
        total += face_n.dot(normal.normalized())
    return total / 3.0


def _winding_key(tri):
    a, b, c = tri
    return min((a, b, c), (b, c, a), (c, a, b))


def _add_unique_face(face_map, two_sided, tri, dmat, verts, normals):
    key = frozenset(tri)
    score = _tri_winding_score(tri, verts, normals)
    prev = face_map.get(key)
    # Strip stitches often emit the same three verts with the opposite
    # winding. That is not Max two-sided (both sides are real faces).
    if (prev is not None and dmat is not None
            and _winding_key(tri) != _winding_key(prev[0])
            and score > 0.1 and prev[2] > 0.1):
        two_sided.add(dmat)
    if prev is None or score > prev[2]:
        face_map[key] = (tri, dmat, score)


def create_bobj(context, dmesh, materials, shape, obj, obj_index=-1):
    me = bpy.data.meshes.new("Mesh")

    face_map = {}
    two_sided = set()
    material_indices = {}
    vert_count = mesh_frame_vert_count(dmesh)
    shading_frame = mesh_import_shading_frame(dmesh)
    base_verts, base_normals = mesh_frame_slice(dmesh, shading_frame)
    rest_verts = list(dmesh.verts[:vert_count])

    indices_pass = index_pass()

    for prim in dmesh.primitives:
        if prim.type & Primitive.Indexed:
            indices = dmesh.indices
        else:
            indices = indices_pass

        dmat = None

        if not (prim.type & Primitive.NoMaterial):
            dmat = shape.materials[prim.type & Primitive.MaterialMask]

            if dmat not in material_indices:
                material_indices[dmat] = len(me.materials)
                me.materials.append(materials[dmat])

        if prim.type & Primitive.Strip:
            even = True
            for i in range(prim.firstElement + 2, prim.firstElement + prim.numElements):
                if even:
                    tri = (indices[i], indices[i - 1], indices[i - 2])
                else:
                    tri = (indices[i - 2], indices[i - 1], indices[i])
                even = not even
                if _usable_tri(tri, vert_count):
                    _add_unique_face(face_map, two_sided, tri, dmat, base_verts, base_normals)
        elif prim.type & Primitive.Fan:
            even = True
            for i in range(prim.firstElement + 2, prim.firstElement + prim.numElements):
                if even:
                    tri = (indices[i], indices[i - 1], indices[0])
                else:
                    tri = (indices[0], indices[i - 1], indices[i])
                even = not even
                if _usable_tri(tri, vert_count):
                    _add_unique_face(face_map, two_sided, tri, dmat, base_verts, base_normals)
        else: # Default to Triangle Lists (prim.type & Primitive.Triangles)
            for i in range(prim.firstElement + 2, prim.firstElement + prim.numElements, 3):
                tri = (indices[i], indices[i - 1], indices[i - 2])
                if _usable_tri(tri, vert_count):
                    _add_unique_face(face_map, two_sided, tri, dmat, base_verts, base_normals)

    faces = [(tri, dmat) for tri, dmat, _score in face_map.values()]
    for dmat in two_sided:
        bmat = materials.get(dmat)
        if bmat is not None:
            bmat.torque_props.two_sided = True
            note("Material '{}' marked two-sided (opposite windings on the same verts)"
                 .format(getattr(dmat, "name", bmat.name)))
    me.from_pydata(list(base_verts), [], [verts for verts, _dmat in faces])

    for poly, (_verts, dmat) in zip(me.polygons, faces):
        poly.use_smooth = True  # DTS geometry is always smooth shaded
        if dmat:
            poly.material_index = material_indices[dmat]

    if faces and dmesh.tverts:
        uvs = me.uv_layers.new(name="UVMap")
        tverts = dmesh.tverts
        tcount = len(tverts)
        for poly, (verts, _dmat) in zip(me.polygons, faces):
            for j, index in zip(poly.loop_indices, verts):
                if index < 0 or index >= tcount:
                    continue
                uv = tverts[index]
                uvs.data[j].uv = (uv.x, 1 - uv.y)

    # validate() can drop two-sided duplicates and rewrite loops.
    # Custom split normals must be applied after that, or shading
    # looks like a tiny sphere on every face.
    me.validate(clean_customdata=False)
    me.update()

    # Encode custom normals while faces still have area. Blender stores
    # them in the current face LNOR space; a hide-clump rest makes that
    # space degenerate and the extras look broken (flat meshes especially).
    if base_normals and len(base_normals) == len(me.vertices):
        custom = []
        for normal in base_normals:
            vec = Vector(normal)
            custom.append(
                tuple(vec.normalized()) if vec.length_squared > 1e-16
                else (0.0, 0.0, 1.0))
        me.normals_split_custom_set_from_vertices(custom)

    if shading_frame and rest_verts and len(rest_verts) == len(me.vertices):
        for vert, co in zip(me.vertices, rest_verts):
            vert.co = co
        note("Imported '{}' custom normals from morph frame {} "
             "(rest frame is a hide-clump)".format(
                 shape.names[obj.name], shading_frame))
    elif base_normals:
        me.update()

    bobj = bpy.data.objects.new(dedup_name(bpy.data.objects, shape.names[obj.name]), me)
    bobj["name"] = shape.names[obj.name]
    keymap = add_imported_morph_shape_keys(bobj, dmesh, shape, obj_index)
    if keymap:
        bobj["torque_morph_keymap"] = {str(k): v for k, v in keymap.items()}
    return bobj

def file_base_name(filepath):
    return os.path.basename(filepath).rsplit(".", 1)[0]

def insert_reference(frame, shape_nodes):
    for node in shape_nodes:
        ob = getattr(node, "bl_ob", None)
        if ob is None:
            continue

        curves = ob_location_curves(ob)
        for curve in curves:
            curve.keyframe_points.add(1)
            key = curve.keyframe_points[-1]
            key.interpolation = "LINEAR"
            key.co = (frame, ob.location[curve.array_index])

        curves = ob_scale_curves(ob)
        for curve in curves:
            curve.keyframe_points.add(1)
            key = curve.keyframe_points[-1]
            key.interpolation = "LINEAR"
            key.co = (frame, ob.scale[curve.array_index])

        _, curves = ob_rotation_curves(ob)
        rot = ob_rotation_data(ob)
        for curve in curves:
            curve.keyframe_points.add(1)
            key = curve.keyframe_points[-1]
            key.interpolation = "LINEAR"
            key.co = (frame, rot[curve.array_index])

def load(operator, context, filepath,
         reference_keyframe=True,
         import_sequences=True,
         use_armature=False,
         sequence_mode="ACTIONS",
         debug_report=False,
         log_level="DISABLED",
         group_in_collection=False,
         timeline_expand=False,
         scale=1.0):
    log = IoLog(normalize_log_level(log_level, debug_report))
    set_active_log(log)
    try:
        return _load_dts(
            operator, context, filepath, log,
            reference_keyframe, import_sequences, use_armature,
            sequence_mode, group_in_collection, timeline_expand, scale)
    finally:
        set_active_log(None)


def _load_dts(operator, context, filepath, log,
              reference_keyframe, import_sequences, use_armature,
              sequence_mode, group_in_collection, timeline_expand, scale):
    shape = DtsShape()

    with open(filepath, "rb") as fd:
        shape.load(fd)

    if not use_armature:
        use_armature = any(mesh.get_type() == Mesh.SkinType for mesh in shape.meshes)
    if use_armature:
        print("Importing nodes as an armature (skinned mesh or forced)")

    unit_scale = scale_shape_world_units(shape, scale)

    # Create a Blender material for each DTS material
    materials = {}
    color_source = get_rgb_colors()

    for dmat in shape.materials:
        materials[dmat] = import_material(color_source, dmat, filepath)

    # Now assign IFL material properties where needed
    for ifl in shape.iflmaterials:
        mat = materials[shape.materials[ifl.slot]]
        assert mat.torque_props.use_ifl == True
        mat.torque_props.ifl_name = shape.names[ifl.name]

    import_material_extra_maps(shape, materials)

    shape_name = file_base_name(filepath)
    parent_collection = import_parent_collection(context)
    if group_in_collection:
        parent_collection = ensure_import_shape_collection(parent_collection, shape_name)

    detail_collections = {}
    lod_by_mesh = {}
    for lod in shape.detail_levels:
        if is_auto_billboard_detail(lod):
            continue
        lod_by_mesh[lod.objectDetail] = lod
        ensure_import_detail_collection(
            parent_collection, lod_import_collection_name(shape, lod), detail_collections)

    import_auto_billboard_empties(shape, parent_collection)

    node_obs = []
    node_obs_val = {}
    imported_actions = []
    seq_starts = []
    dummy_seq_starts = []
    bone_names = []

    if use_armature:
        root_arm = bpy.data.armatures.new(shape_name)
        root_ob = bpy.data.objects.new(root_arm.name, root_arm)
        root_ob.show_in_front = True

        link_object_to_collection(root_ob, parent_collection)
        context.view_layer.objects.active = root_ob
        root_ob.select_set(True)

        # Calculate armature-space matrix, head and tail for each node
        for i, node in enumerate(shape.nodes):
            node.mat = shape.default_rotations[i].to_matrix()
            node.mat = Matrix.Translation(shape.default_translations[i]) @ node.mat.to_4x4()
            if node.parent != -1:
                node.mat = shape.nodes[node.parent].mat @ node.mat
            # node.head = node.mat.to_translation()
            # node.tail = node.head + Vector((0, 0, 0.25))
            # node.tail = node.mat.to_translation()
            # node.head = node.tail - Vector((0, 0, 0.25))

        bpy.ops.object.mode_set(mode="EDIT")

        edit_bone_table = []

        for i, node in enumerate(shape.nodes):
            bone = root_arm.edit_bones.new(shape.names[node.name])
            # bone.use_connect = True
            # bone.head = node.head
            # bone.tail = node.tail
            bone.head = (0, 0, -0.25 * unit_scale)
            bone.tail = (0, 0, 0)

            if node.parent != -1:
                bone.parent = edit_bone_table[node.parent]

            bone.matrix = node.mat
            bone["nodeIndex"] = i

            edit_bone_table.append(bone)
            bone_names.append(bone.name)

        bpy.ops.object.mode_set(mode="OBJECT")

        if import_sequences:
            node_names = [shape.names[node.name] for node in shape.nodes]
            defaults = list(zip(shape.default_translations, shape.default_rotations))
            imported_actions, seq_starts = import_armature_sequences(
                root_ob, shape.sequences,
                [shape.names[seq.nameIndex] for seq in shape.sequences],
                node_names,
                shape.node_translations, shape.node_rotations,
                shape.node_uniform_scales, shape.node_aligned_scales,
                defaults, None, context.scene,
                sequence_mode=sequence_mode,
                source_triggers=shape.triggers,
                arbitrary_scale_factors=shape.node_arbitrary_scale_factors,
                arbitrary_scale_rots=shape.node_arbitrary_scale_rots,
                timeline_expand=timeline_expand,
                source="DTS",
                collection=parent_collection if group_in_collection else None)
    else:
        if reference_keyframe:
            reference_marker = context.scene.timeline_markers.get("reference")
            if reference_marker is None:
                reference_frame = 0
                context.scene.timeline_markers.new("reference", frame=reference_frame)
            else:
                reference_frame = reference_marker.frame
        else:
            reference_frame = None

        # Create an empty for every node
        for i, node in enumerate(shape.nodes):
            ob = bpy.data.objects.new(dedup_name(bpy.data.objects, shape.names[node.name]), None)
            node.bl_ob = ob
            ob["nodeIndex"] = i
            ob.empty_display_type = "SINGLE_ARROW"
            ob.empty_display_size = 0.5 * unit_scale

            if node.parent != -1:
                ob.parent = node_obs[node.parent]

            ob.location = shape.default_translations[i]
            ob.rotation_mode = "QUATERNION"
            ob.rotation_quaternion = shape.default_rotations[i]
            if shape.names[node.name] == "__auto_root__" and ob.rotation_quaternion.magnitude == 0:
                ob.rotation_quaternion = (1, 0, 0, 0)

            link_object_to_collection(ob, parent_collection)
            node_obs.append(ob)
            node_obs_val[node] = ob

        for obj_index, obj in enumerate(shape.objects):
            if obj.node < 0 or obj.node >= len(node_obs):
                continue
            if obj_index < len(shape.objectstates):
                assign_vis_value(node_obs[obj.node], shape.objectstates[obj_index].vis)

        if reference_keyframe:
            insert_reference(reference_frame, shape.nodes)

    # Try animation? Empty rigs keep markers + Sequences text.
    if import_sequences and not use_armature:
        globalToolIndex = 10
        fps = context.scene.render.fps

        sequences_text = []

        for seq in shape.sequences:
            dummy_seq_starts.append((seq, globalToolIndex))
            name = shape.names[seq.nameIndex]
            print("Importing sequence", name)

            flags = []
            flags.append("priority {}".format(seq.priority))

            if seq.flags & Sequence.Cyclic:
                flags.append("cyclic")

            if seq.flags & Sequence.Blend:
                flags.append("blend")

            if seq.duration > 0 and seq.numKeyframes > 0:
                flags.append("fps {:.6g}".format(seq.numKeyframes / seq.duration))

            if flags:
                sequences_text.append(name + ": " + ", ".join(flags))

            nodesRotation = tuple(map(lambda p: p[0], filter(lambda p: p[1], zip(shape.nodes, seq.rotationMatters))))
            nodesTranslation = tuple(map(lambda p: p[0], filter(lambda p: p[1], zip(shape.nodes, seq.translationMatters))))
            nodesScale = tuple(map(lambda p: p[0], filter(lambda p: p[1], zip(shape.nodes, seq.scaleMatters))))

            step = 1
            baked_nodes = set()
            if seq.flags & Sequence.ArbitraryScale and nodesScale:
                if seq.flags & Sequence.Blend and reference_frame is None:
                    return fail(operator, "Missing 'reference' marker for blend animation '{}'".format(name))
                node_defaults = {}
                for i, node in enumerate(shape.nodes):
                    if node in node_obs_val:
                        node_defaults[node] = (
                            shape.default_translations[i].copy(),
                            shape.default_rotations[i].copy(),
                            node_obs_val[node])
                baked_nodes = import_empty_arbitrary_scale(
                    seq, name, globalToolIndex, step,
                    shape.nodes, node_defaults,
                    nodesTranslation, nodesRotation, nodesScale,
                    shape.node_translations, shape.node_rotations,
                    shape.node_arbitrary_scale_factors,
                    shape.node_arbitrary_scale_rots,
                    bool(seq.flags & Sequence.Blend),
                    reference_frame, evaluate_all)

            for mattersIndex, node in enumerate(nodesTranslation):
                if node in baked_nodes:
                    continue
                ob = node_obs_val[node]
                curves = ob_location_curves(ob)

                for frameIndex in range(seq.numKeyframes):
                    vec = shape.node_translations[seq.baseTranslation + mattersIndex * seq.numKeyframes + frameIndex]
                    if seq.flags & Sequence.Blend:
                        if reference_frame is None:
                            return fail(operator, "Missing 'reference' marker for blend animation '{}'".format(name))
                        ref_vec = Vector(evaluate_all(curves, reference_frame))
                        vec = ref_vec + vec

                    for curve in curves:
                        curve.keyframe_points.add(1)
                        key = curve.keyframe_points[-1]
                        key.interpolation = "LINEAR"
                        key.co = (
                            globalToolIndex + frameIndex * step,
                            vec[curve.array_index])

            for mattersIndex, node in enumerate(nodesRotation):
                if node in baked_nodes:
                    continue
                ob = node_obs_val[node]
                mode, curves = ob_rotation_curves(ob)

                for frameIndex in range(seq.numKeyframes):
                    rot = shape.node_rotations[seq.baseRotation + mattersIndex * seq.numKeyframes + frameIndex]
                    if seq.flags & Sequence.Blend:
                        if reference_frame is None:
                            return fail(operator, "Missing 'reference' marker for blend animation '{}'".format(name))
                        ref_rot = Quaternion(evaluate_all(curves, reference_frame))
                        rot = ref_rot @ rot
                    if mode == 'AXIS_ANGLE':
                        rot = rot.to_axis_angle()
                    elif mode != 'QUATERNION':
                        rot = rot.to_euler(mode)

                    for curve in curves:
                        curve.keyframe_points.add(1)
                        key = curve.keyframe_points[-1]
                        key.interpolation = "LINEAR"
                        key.co = (
                            globalToolIndex + frameIndex * step,
                            rot[curve.array_index])

            for mattersIndex, node in enumerate(nodesScale):
                if node in baked_nodes:
                    continue
                ob = node_obs_val[node]
                curves = ob_scale_curves(ob)

                for frameIndex in range(seq.numKeyframes):
                    index = seq.baseScale + mattersIndex * seq.numKeyframes + frameIndex

                    if seq.flags & Sequence.UniformScale:
                        s = shape.node_uniform_scales[index]
                        vec = (s, s, s)
                    elif seq.flags & Sequence.AlignedScale:
                        vec = shape.node_aligned_scales[index]
                    else:
                        note("Warning: Invalid scale flags found in sequence")
                        break

                    for curve in curves:
                        curve.keyframe_points.add(1)
                        key = curve.keyframe_points[-1]
                        key.interpolation = "LINEAR"
                        key.co = (
                            globalToolIndex + frameIndex * step,
                            vec[curve.array_index])

            keyed_empties = set()
            for obj_index, obj in enumerate(shape.objects):
                values = sequence_object_vis_values(
                    seq, obj_index, shape.objectstates, len(shape.objects))
                if values is None or obj.node < 0 or obj.node >= len(node_obs):
                    continue
                ob = node_obs[obj.node]
                if ob is None:
                    continue
                if ob in keyed_empties:
                    continue
                keyed_empties.add(ob)
                key_object_vis(ob, values, globalToolIndex)

            # Insert a reference frame immediately before the animation
            # insert_reference(globalToolIndex - 2, shape.nodes)

            start_frame = globalToolIndex
            end_frame = globalToolIndex + seq.numKeyframes * step - 1
            context.scene.timeline_markers.new(name + ":start", frame=start_frame)
            context.scene.timeline_markers.new(name + ":end", frame=end_frame)

            if seq.numTriggers:
                span = max(end_frame - start_frame, 1)
                for i in range(seq.numTriggers):
                    trigger = shape.triggers[seq.firstTrigger + i]
                    state = decode_trigger_state(trigger.state)
                    frame = start_frame + int(round(trigger.pos * span))
                    context.scene.timeline_markers.new(
                        "{}:trigger {}".format(name, state), frame=frame)

            globalToolIndex += seq.numKeyframes * step + 30

        if "Sequences" in bpy.data.texts:
            sequences_buf = bpy.data.texts["Sequences"]
        else:
            sequences_buf = bpy.data.texts.new("Sequences")

        sequences_buf.from_string("\n".join(sequences_text))

    # Then put objects in the armatures.
    # Skin objects are stored with node -1 (no single parent). Import them
    # onto the armature; only skip node-less rigid meshes when there is
    # nowhere to attach them.
    object_bobjs = [[] for _ in shape.objects]
    for obj_index, obj in enumerate(shape.objects):
        obj_meshes = [shape.meshes[obj.firstMesh + i] for i in range(obj.numMeshes)]
        has_skin = any(mesh.get_type() == Mesh.SkinType for mesh in obj_meshes)
        if obj.node == -1 and not has_skin and not use_armature:
            note('Warning: Object {} is not attached to a node, ignoring'
                  .format(shape.names[obj.name]))
            continue

        for meshIndex in range(obj.numMeshes):
            mesh = shape.meshes[obj.firstMesh + meshIndex]
            mtype = mesh.get_type()

            if mtype == Mesh.NullType:
                continue

            if mtype != Mesh.StandardType and mtype != Mesh.SkinType:
                note('Warning: Mesh #{} of object {} is of unsupported type {}, ignoring'.format(
                    meshIndex + 1, mtype, shape.names[obj.name]))
                continue

            bobj = create_bobj(context, mesh, materials, shape, obj, obj_index)
            object_bobjs[obj_index].append(bobj)
            if mesh.get_flags(Mesh.BillboardZAxis):
                bobj["BBZ"] = True
            elif mesh.get_flags(Mesh.Billboard):
                bobj["BB"] = True

            add_vertex_groups(mesh, bobj, shape)

            attach = obj.node

            if use_armature:
                bobj.parent = root_ob
                bobj.parent_type = "OBJECT"
                if mtype == Mesh.SkinType:
                    modifier = bobj.modifiers.new('Armature', 'ARMATURE')
                    modifier.object = root_ob
                elif attach != -1 and attach < len(bone_names) and bone_names[attach]:
                    bobj.parent_bone = bone_names[attach]
                    bobj.parent_type = "BONE"
                    if 0 <= attach < len(shape.nodes) and hasattr(shape.nodes[attach], "mat"):
                        bobj.matrix_world = shape.nodes[attach].mat
            elif attach != -1 and attach < len(node_obs):
                bobj.parent = node_obs[attach]

            mesh_lods = details_for_mesh_slot(shape, meshIndex, lod_by_mesh)
            if mesh_lods:
                for lod in mesh_lods:
                    lod_name = lod_import_collection_name(shape, lod)
                    lod_collection = ensure_import_detail_collection(
                        parent_collection, lod_name, detail_collections)
                    link_object_to_collection(bobj, lod_collection)
            else:
                note("Warning: Mesh slot {} of object {} has no detail level; putting it in the scene".format(
                    meshIndex, shape.names[obj.name]))
                link_object_to_collection(bobj, parent_collection)

    if import_sequences and imported_actions:
        apply_imported_morph_actions(
            shape, object_bobjs, imported_actions, seq_starts,
            [shape.names[seq.nameIndex] for seq in shape.sequences],
            armature=root_ob if use_armature else None,
            scene=context.scene, context=context)
    elif import_sequences and dummy_seq_starts:
        apply_imported_morph_timeline(
            shape, object_bobjs, dummy_seq_starts,
            scene=context.scene, context=context)

    if use_armature:
        import_object_visibility(
            shape,
            shape.sequences if import_sequences else [],
            imported_actions,
            bone_names, root_ob, object_bobjs,
            start_frames=seq_starts)
    elif import_sequences:
        keyed_skins = set()
        for seq, start_frame in dummy_seq_starts:
            for obj_index, obj in enumerate(shape.objects):
                if obj.node != -1:
                    continue
                values = sequence_object_vis_values(
                    seq, obj_index, shape.objectstates, len(shape.objects))
                if values is None:
                    continue
                if obj_index in keyed_skins:
                    continue
                keyed_skins.add(obj_index)
                for bobj in object_bobjs[obj_index]:
                    key_object_vis(bobj, values, start_frame)

    # Import a bounds mesh
    me = bpy.data.meshes.new("Mesh")
    me.vertices.add(8)
    me.vertices[0].co = (shape.bounds.min.x, shape.bounds.min.y, shape.bounds.min.z)
    me.vertices[1].co = (shape.bounds.max.x, shape.bounds.min.y, shape.bounds.min.z)
    me.vertices[2].co = (shape.bounds.max.x, shape.bounds.max.y, shape.bounds.min.z)
    me.vertices[3].co = (shape.bounds.min.x, shape.bounds.max.y, shape.bounds.min.z)
    me.vertices[4].co = (shape.bounds.min.x, shape.bounds.min.y, shape.bounds.max.z)
    me.vertices[5].co = (shape.bounds.max.x, shape.bounds.min.y, shape.bounds.max.z)
    me.vertices[6].co = (shape.bounds.max.x, shape.bounds.max.y, shape.bounds.max.z)
    me.vertices[7].co = (shape.bounds.min.x, shape.bounds.max.y, shape.bounds.max.z)
    me.validate()
    me.update()
    ob = bpy.data.objects.new("bounds", me)
    ob.display_type = "BOUNDS"
    ob.hide_render = True
    ob["dts_radius"] = float(shape.radius)
    ob["dts_radius_tube"] = float(shape.radius_tube)
    ob["dts_bounds_min"] = [float(shape.bounds.min.x), float(shape.bounds.min.y), float(shape.bounds.min.z)]
    ob["dts_bounds_max"] = [float(shape.bounds.max.x), float(shape.bounds.max.y), float(shape.bounds.max.z)]
    link_object_to_collection(ob, parent_collection)

    finish_log(filepath, log, shape=shape, meta=io_meta(
        "import dts", filepath,
        reference_keyframe=reference_keyframe,
        import_sequences=import_sequences,
        use_armature=use_armature,
        sequence_mode=sequence_mode,
        group_in_collection=group_in_collection,
        scale=scale,
        file_version=getattr(shape, "dtsVersion", "?")))

    return {"FINISHED"}

def add_vertex_groups(mesh, ob, shape):
    groups = []
    for node, initial_transform in mesh.bones:
        # TODO: Handle initial_transform
        if node == -1:
            groups.append(None)
            continue
        name = shape.names[shape.nodes[node].name]
        group = ob.vertex_groups.get(name)
        if group is None:
            group = ob.vertex_groups.new(name=name)
        groups.append(group)

    for vertex, bone, weight in mesh.influences:
        if bone < 0 or bone >= len(groups) or groups[bone] is None:
            continue
        groups[bone].add((vertex,), weight, 'REPLACE')
