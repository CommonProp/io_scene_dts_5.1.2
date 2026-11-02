import bpy, os, sys
from math import sqrt, pi
from operator import attrgetter
from itertools import groupby

from .DsqFile import DsqFile
from .DtsTypes import *
from .util import fail, evaluate_all, find_reference, fcurves_keyframe_in_range
from .write_report import (
    IoLog, finish_log, io_meta, normalize_log_level, note, set_active_log)
from .shared_export import (
    action_shape_key_names, apply_action_extra, apply_sequence_flags,
    apply_sequence_membership,
    apply_sequence_scales, apply_shape_sequence_scales, append_ground_frames, append_sequence_triggers,
    assign_armature_action, classify_detail_marker, dsq_entry_curve_arrays,
    dsq_entry_display_name, dsq_entry_fcurves, dsq_entry_rest, dsq_entry_sample,
    dsq_split_filepath, export_directory, export_sequence_collections,
    find_bounds_object, find_export_sequences,
    apply_armature_object_track_matters, bone_export_world_matrix,
    dsq_entry_needs_object_tm_sample, folded_local_matrix, ground_frame_indices,
    resolve_export_filepath,
    scale_dsq_world_units, tge_shape_set_error, unique_output_path,
    is_bounds_name, is_dummy_name, name_listed, object_export_name,
    object_ignored_for_export, object_in_export_scope,
    object_is_auto_billboard_marker,
    read_node_export_lists,
    resolve_blend_reference_frame, restore_scene_timeline, sample_world_frames)

def save(operator, context, filepath,
         select_marker=False,
         sequence_mode="ACTIONS",
         debug_report=False,
         log_level="DISABLED",
         export_kind="DSQ",
         scale=1.0,
         split_sequences=False,
         filename_prefix="",
         filename_suffix="",
         export_scope="SCENE",
         collection="",
         show_all_actions=False,
         strip_action_suffixes=True):
    print("Exporting scene to DSQ")
    if split_sequences:
        write_path = filepath
        log_path = os.path.join(export_directory(filepath) or "", "dsq")
    else:
        write_path = resolve_export_filepath(filepath, ".dsq")
        log_path = write_path
    log = IoLog(normalize_log_level(log_level, debug_report))
    set_active_log(log)
    collection = (collection or "").strip()
    mode = (export_scope or "SCENE").upper()
    if mode == "COLLECTION" and not collection:
        note("Warning: Export Collection is selected but no collection was chosen; exporting the whole scene")
    collection_roots = None
    if mode == "COLLECTION":
        collection_roots = export_sequence_collections(mode, collection)
    show_all = bool(show_all_actions)
    strip_suffixes = bool(strip_action_suffixes)
    meta = io_meta(
        "export dsq", write_path,
        export_scope=mode,
        select_marker=select_marker,
        sequence_mode=sequence_mode,
        scale=scale,
        split_sequences=split_sequences,
        filename_prefix=filename_prefix,
        filename_suffix=filename_suffix,
        collection=collection,
        file_version=24)
    with restore_scene_timeline(getattr(context, "scene", None)):
        try:
            if split_sequences:
                return _save_dsq_split(
                    operator, context, write_path, log,
                    select_marker, sequence_mode, export_kind, scale,
                    filename_prefix, filename_suffix,
                    collection_roots=collection_roots, show_all_actions=show_all,
                    strip_action_suffixes=strip_suffixes)
            return _save_dsq(
                operator, context, write_path, log,
                select_marker, sequence_mode, export_kind, scale,
                collection_roots=collection_roots, show_all_actions=show_all,
                strip_action_suffixes=strip_suffixes)
        except Exception as exc:
            if not log.error:
                log.error = str(exc)
                note("Error:", exc)
            raise
        finally:
            finish_log(log_path, log, dsq=log.dsq, meta=meta, error=log.error)
            set_active_log(None)


def _save_dsq_split(operator, context, filepath, log,
                   select_marker, sequence_mode, export_kind, scale,
                   filename_prefix, filename_suffix,
                   action_objects=None, show_all_actions=False,
                   collection_roots=None, strip_action_suffixes=True):
    sequences = find_export_sequences(
        context.scene, select_marker, sequence_mode, export_kind=export_kind,
        objects=action_objects, show_all=show_all_actions,
        collection_roots=collection_roots,
        strip_action_suffixes=strip_action_suffixes)
    if not sequences:
        return fail(operator, "No sequences selected for DSQ export")
    used = set()
    result = {"FINISHED"}
    for source in sequences:
        path = unique_output_path(
            dsq_split_filepath(
                filepath, source["name"], filename_prefix, filename_suffix),
            used)
        result = _save_dsq(
            operator, context, path, log,
            select_marker, sequence_mode, export_kind, scale,
            sequences_override=[source],
            action_objects=action_objects, show_all_actions=show_all_actions,
            collection_roots=collection_roots,
            strip_action_suffixes=strip_action_suffixes)
        if result != {"FINISHED"}:
            return result
        note("Wrote DSQ", path)
    return result


def _save_dsq(operator, context, filepath, log,
              select_marker, sequence_mode, export_kind, scale,
              sequences_override=None, action_objects=None,
              show_all_actions=False, collection_roots=None,
              strip_action_suffixes=True):

    scene = context.scene
    dsq = DsqFile()
    log.dsq = dsq

    sequences = sequences_override
    if sequences is None:
        sequences = find_export_sequences(
            context.scene, select_marker, sequence_mode, export_kind=export_kind,
            objects=action_objects, show_all=show_all_actions,
            collection_roots=collection_roots,
            strip_action_suffixes=strip_action_suffixes)
    if not sequences:
        return fail(operator, "No sequences selected for DSQ export")
    always_export, never_export, never_animate = read_node_export_lists()
    scene_fps = context.scene.render.fps / context.scene.render.fps_base

    # Seek to reference frame if present before reading nodes
    reference_frame = find_reference(scene)

    if reference_frame is not None:
        note("Note: Seeking to reference frame at", reference_frame)
        scene.frame_set(reference_frame)

    # Create a DTS node for every armature/empty in the scene
    node_ob = {}
    node_transform = {}

    def add_dsq_node(name, entry):
        node_ob[name] = entry
        node_transform[name] = dsq_entry_rest(entry)
        dsq.nodes.append(name)

    def traverse_node(node, parent_entry=None):
        name = object_export_name(node)
        kind, _ = classify_detail_marker(name)
        if kind is not None or object_is_auto_billboard_marker(node) or is_dummy_name(name) or is_bounds_name(name) or name_listed(name, never_export):
            for child in node.children:
                if child.type == "EMPTY":
                    traverse_node(child, parent_entry)
            return

        rest = node.matrix_world.copy()
        entry = {
            "kind": "empty",
            "object": node,
            "rest_world": rest,
            "rest_local": node.matrix_local.copy(),
            "parent_entry": parent_entry,
            "parent_rest": parent_entry.get("rest_world") if parent_entry else None,
        }
        if name in node_ob:
            note("Warning: Multiple empties export as DSQ node '{}'; keeping the first"
                  .format(name))
            parent_for_children = node_ob[name]
        else:
            add_dsq_node(name, entry)
            parent_for_children = entry

        for child in node.children:
            if child.type == "EMPTY":
                traverse_node(child, parent_for_children)

    def traverse_bones(armature, bones, parent_entry=None):
        for bone in bones:
            if is_dummy_name(bone.name) or name_listed(bone.name, never_export):
                traverse_bones(armature, bone.children, parent_entry)
                continue
            rest = bone_export_world_matrix(armature, bone, posed=False)
            entry = {
                "kind": "bone",
                "armature": armature,
                "bone": bone,
                "rest_world": rest,
                "rest_local": folded_local_matrix(
                    rest, parent_entry.get("rest_world") if parent_entry else None),
                "parent_entry": parent_entry,
                "parent_rest": parent_entry.get("rest_world") if parent_entry else None,
            }
            add_dsq_node(bone.name, entry)
            traverse_bones(armature, bone.children, entry)

    scope = action_objects
    for ob in scene.objects:
        if not object_in_export_scope(ob, scope):
            continue
        if ob.type == "EMPTY" and not ob.parent:
            traverse_node(ob)
        elif ob.type == "ARMATURE" and not name_listed(ob.name, never_export):
            roots = [bone for bone in ob.data.bones if bone.parent is None]
            traverse_bones(ob, roots)

    reference_frame = find_reference(context.scene)
    ground_ob = find_bounds_object(scene)

    # NodeOrder backwards compatibility
    if "NodeOrder" in bpy.data.texts:
        note("Warning: NodeOrder found, using it for backwards compatibility")
        order = bpy.data.texts["NodeOrder"].as_string().split("\n")
        order_key = {name: i for i, name in enumerate(order)}
    else:
        order_key = {}

    def node_order(name):
        if name in order_key:
            return order_key[name]
        entry = node_ob[name]
        if entry["kind"] == "empty":
            return entry["object"].get("nodeIndex", sys.maxsize)
        return sys.maxsize

    dsq.nodes.sort(key=node_order)

    node_index = {name: i for i, name in enumerate(dsq.nodes)}
    auto_root_index = None
    animated_nodes = []

    for name in dsq.nodes:
        entry = node_ob[name]
        fcurves = dsq_entry_fcurves(entry)
        if fcurves and len(fcurves):
            animated_nodes.append(name)

    for bobj in scene.objects:
        if not object_in_export_scope(bobj, scope):
            continue
        if bobj.type != "MESH" or is_bounds_name(bobj.name):
            continue

        if object_ignored_for_export(bobj):
            continue

        if not bobj.parent:
            if auto_root_index is None:
                auto_root_index = len(dsq.nodes)
                dsq.nodes.append("__auto_root__")

    set_reason = tge_shape_set_error(len(dsq.nodes))
    if set_reason:
        return fail(operator, set_reason)

    # one scale type for the whole DSQ (apply_shape_sequence_scales)
    scale_entries = []
    for source in sequences:
        name = source["name"]
        print("Exporting sequence", name)

        if source["action"] is None:
            markers = source["markers"]
            if "start" not in markers:
                return fail(operator, "Missing start marker for sequence '{}'".format(name))
            if "end" not in markers:
                return fail(operator, "Missing end marker for sequence '{}'".format(name))
            frame_start = markers["start"].frame
            frame_end = markers["end"].frame
        else:
            frame_start = source["frame_start"]
            frame_end = source["frame_end"]

        frame_range = int(frame_end) - int(frame_start) + 1

        seq = Sequence()
        seq.name = name
        seq.flags = Sequence.AlignedScale
        seq.priority = 1

        seq.toolBegin = frame_start
        seq.duration = frame_range / scene_fps

        if source["action"] is not None:
            extra = apply_action_extra(seq, source["extra"], frame_range, scene_fps)
            pending_triggers = extra.get("triggers", [])
            if action_shape_key_names(source["action"]):
                note("Warning: Sequence '{}' has shape-key keys. Morph is "
                     "DTS-only; this DSQ will not carry vertex animation."
                     .format(name))
        else:
            extra = apply_sequence_flags(
                seq, source.get("flags", ()), frame_range, scene_fps)
            pending_triggers = list(source.get("triggers", [])) + extra["triggers"]

        seq.numKeyframes = frame_range
        seq.firstGroundFrame = len(dsq.ground_translations)
        seq.baseRotation = len(dsq.rotations)
        seq.baseTranslation = len(dsq.translations)
        seq.baseScale = 0
        seq.baseObjectState = 0
        seq.baseDecalState = 0
        append_sequence_triggers(
            seq, pending_triggers, dsq.triggers, frame_start, frame_end)

        seq.rotationMatters = [False] * len(dsq.nodes)
        seq.translationMatters = [False] * len(dsq.nodes)
        seq.scaleMatters = [False] * len(dsq.nodes)
        seq.decalMatters = [False] * len(dsq.nodes)
        seq.iflMatters = []
        seq.visMatters = [False] * len(dsq.nodes)
        seq.frameMatters = [False] * len(dsq.nodes)
        seq.matFrameMatters = [False] * len(dsq.nodes)

        dsq.sequences.append(seq)

        frame_indices = list(range(int(frame_start), int(frame_end) + 1))
        pending_scales = []
        warned_vis = False

        with assign_armature_action(scene, source["action"]):
            sample_names = []
            for node_name in dsq.nodes:
                if node_name not in node_ob:
                    continue
                fcurves = dsq_entry_fcurves(node_ob[node_name])
                if fcurves and len(fcurves):
                    sample_names.append(node_name)
                elif extra.get("force_transform") or extra.get("force_scale"):
                    sample_names.append(node_name)
                elif dsq_entry_needs_object_tm_sample(node_ob[node_name]):
                    sample_names.append(node_name)

            animation_data = {frame: {} for frame in frame_indices}

            for frame in frame_indices:
                scene.frame_set(frame)
                for node_name in sample_names:
                    animation_data[frame][node_name] = dsq_entry_sample(node_ob[node_name])

            if ground_ob is not None:
                ground_frames = ground_frame_indices(
                    frame_start, frame_end, scene_fps, extra.get("ground_frame_rate"))
                append_ground_frames(
                    seq, dsq.ground_translations, dsq.ground_rotations,
                    sample_world_frames(scene, ground_ob, ground_frames),
                    extra.get("ignore_ground", False))

            blend_ref = resolve_blend_reference_frame(
                extra, reference_frame, frame_start, frame_end)

            for node_name in sample_names:
                if node_name not in node_index:
                    continue
                index = node_index[node_name]
                entry = node_ob[node_name]
                display_name = dsq_entry_display_name(entry)
                if name_listed(display_name, never_animate):
                    continue

                fcurves = dsq_entry_fcurves(entry)
                if fcurves is None:
                    continue

                curves_rotation, curves_translation, curves_scale, curves_vis = (
                    dsq_entry_curve_arrays(entry, fcurves))

                if curves_rotation and fcurves_keyframe_in_range(curves_rotation, frame_start, frame_end):
                    seq.rotationMatters[index] = True

                if curves_translation and fcurves_keyframe_in_range(curves_translation, frame_start, frame_end):
                    seq.translationMatters[index] = True

                if curves_scale and fcurves_keyframe_in_range(curves_scale, frame_start, frame_end):
                    seq.scaleMatters[index] = True

                if (not warned_vis and curves_vis
                        and fcurves_keyframe_in_range(curves_vis, frame_start, frame_end)):
                    note("Warning: Visibility animation on sequence '{}' is ignored in DSQ. "
                          "Embed visibility in the DTS; standalone DSQ has no object-state block."
                          .format(name))
                    warned_vis = True

            apply_sequence_membership(seq, extra, len(dsq.nodes))
            apply_armature_object_track_matters(seq, extra, (
                (node_index[name], node_ob[name]["armature"], node_ob[name]["bone"])
                for name in dsq.nodes
                if name in node_index and name in node_ob
                and node_ob[name].get("kind") == "bone"
                and not name_listed(
                    dsq_entry_display_name(node_ob[name]), never_animate)
            ))

            for node_name in dsq.nodes:
                if node_name not in node_index or node_name not in node_ob:
                    continue
                index = node_index[node_name]
                entry = node_ob[node_name]
                display_name = dsq_entry_display_name(entry)
                if name_listed(display_name, never_animate):
                    continue
                if not (seq.rotationMatters[index] or seq.translationMatters[index]
                        or seq.scaleMatters[index]):
                    continue

                base_translation, base_rotation, base_scale = node_transform[node_name]
                if seq.flags & Sequence.Blend and blend_ref is not None:
                    if blend_ref in animation_data and node_name in animation_data[blend_ref]:
                        base_translation, base_rotation = animation_data[blend_ref][node_name][:2]
                    else:
                        scene.frame_set(blend_ref)
                        base_translation, base_rotation = dsq_entry_sample(entry)[:2]

                for frame in frame_indices:
                    sample = animation_data[frame].get(node_name)
                    if sample is None:
                        sample = dsq_entry_sample(entry)
                    translation, rotation, scale = sample[:3]

                    if seq.translationMatters[index]:
                        if seq.flags & Sequence.Blend:
                            translation -= base_translation
                        dsq.translations.append(translation)

                    if seq.rotationMatters[index]:
                        if seq.flags & Sequence.Blend:
                            rotation = base_rotation.inverted() @ rotation
                        dsq.rotations.append(rotation)

                    if seq.scaleMatters[index]:
                        pending_scales.append(scale)

        scale_entries.append((seq, pending_scales))

    apply_shape_sequence_scales(scale_entries, dsq.uniform_scales, dsq.aligned_scales)

    scale_dsq_world_units(dsq, scale)

    with open(filepath, "wb") as fd:
        dsq.write(fd)

    return {"FINISHED"}
