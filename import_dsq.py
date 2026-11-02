import os
import bpy
from math import ceil

from .DsqFile import DsqFile
from .DtsTypes import Sequence, Quaternion, Vector
from .util import fail, ob_location_curves, ob_scale_curves, ob_rotation_curves, ob_rotation_data, \
  evaluate_all, find_reference
from .write_report import (
    IoLog, finish_log, io_meta, normalize_log_level, note, set_active_log)
from .shared_export import (
    decode_trigger_state, import_armature_sequences, import_empty_arbitrary_scale,
    resolve_dsq_node_name, scale_dsq_world_units, shape_collection_for_object)

def get_free_name(name, taken):
  if name not in taken:
    return name

  suffix = 1

  while True:
    name_try = name + "." + str(suffix)

    if name_try not in taken:
      return name_try

    suffix += 1


def dsq_filename_sequence_name(filepath):
  stem = os.path.splitext(os.path.basename(filepath))[0]
  if stem.lower().startswith("m_") and len(stem) > 2:
    return stem[2:]
  return stem


def dsq_import_sequence_names(filepath, sequences):
  from_file = dsq_filename_sequence_name(filepath)
  names = []
  used = set()
  single = len(sequences) == 1
  for seq in sequences:
    if single and from_file:
      name = from_file
    else:
      name = seq.name or from_file or "sequence"
    key = name.lower()
    if key in used:
      name = "{}_{}".format(from_file or name, seq.name or "seq")
      key = name.lower()
    names.append(name)
    used.add(key)
  return names

# action.fcurves.new(data_path, array_index)
# action.fcurves[].keyframe_points.add(number)
# action.fcurves[].keyframe_points[].interpolation = "LINEAR"
# action.fcurves[].keyframe_points[].co

def load(operator, context, filepath,
         sequence_mode="ACTIONS",
         debug_report=False,
         log_level="DISABLED",
         timeline_expand=False,
         scale=1.0):
  dsq = DsqFile()
  log = IoLog(normalize_log_level(log_level, debug_report))
  set_active_log(log)
  node_binds = []

  with open(filepath, "rb") as fd:
    dsq.read(fd)

  scale_dsq_world_units(dsq, scale)

  def _finish():
    meta = io_meta(
        "import dsq", filepath,
        sequence_mode=sequence_mode,
        scale=scale,
        file_version=getattr(dsq, "version", 24),
        node_binds=node_binds or None)
    finish_log(filepath, log, dsq=dsq, meta=meta)
    set_active_log(None)

  print("Resolving nodes...")

  found_obs = {}

  # Find all our candidate nodes
  # DSQ is case-insensitive, that's why we can't just [] lookup
  for ob in context.scene.objects:
    if ob.type in ("EMPTY", "ARMATURE"):
      name = ob.name.lower()

      if name in found_obs:
        note("Warning: Nodes with varying capitalization found ('{}', '{}'), ignoring second".format(found_obs[name].name, ob.name))
        continue

      found_obs[name] = ob

  bone_armatures = []
  for ob in context.scene.objects:
    if ob.type != "ARMATURE":
      continue
    bone_lower = {bone.name.lower(): bone.name for bone in ob.data.bones}
    mapped = [resolve_dsq_node_name(name, bone_lower) for name in dsq.nodes]
    matched = sum(1 for name in mapped if name)
    if matched:
      bone_armatures.append((matched, ob, bone_lower, mapped))

  if bone_armatures:
    bone_armatures.sort(key=lambda item: item[0], reverse=True)
    matched, armature, bone_lower, node_names = bone_armatures[0]
    missing = [name for name, mapped in zip(dsq.nodes, node_names) if not mapped]
    if missing:
      note("Warning: DSQ '{}' has {} node(s) with no bone; skipping: {}".format(
          os.path.basename(filepath), len(missing), ", ".join(missing)))
    defaults = []
    identity_rot = Quaternion((1, 0, 0, 0))
    for bone_name in node_names:
      if not bone_name:
        defaults.append((Vector(), identity_rot))
        continue
      bone = armature.data.bones[bone_name]
      mat = bone.matrix_local
      if bone.parent:
        mat = bone.parent.matrix_local.inverted() @ mat
      location, rotation, _ = mat.decompose()
      defaults.append((location, rotation))
    import_armature_sequences(
      armature, dsq.sequences, dsq_import_sequence_names(filepath, dsq.sequences),
      node_names, dsq.translations, dsq.rotations,
      dsq.uniform_scales, dsq.aligned_scales,
      defaults, None, context.scene,
      sequence_mode=sequence_mode,
      source_triggers=dsq.triggers,
      arbitrary_scale_factors=dsq.arbitrary_scale_factors,
      arbitrary_scale_rots=dsq.arbitrary_scale_rots,
      timeline_expand=timeline_expand,
      source="DSQ",
      collection=shape_collection_for_object(armature))
    node_binds.extend(zip(dsq.nodes, node_names))
    _finish()
    return {"FINISHED"}

  nodes = [None] * len(dsq.nodes)
  node_missing = []

  # Now associate DSQ node indices with Blender objects
  empty_lower = {key: key for key in found_obs}
  for index, name in enumerate(dsq.nodes):
    mapped = resolve_dsq_node_name(name, empty_lower)
    if mapped is None:
      node_missing.append(name)
      continue
    nodes[index] = found_obs[mapped]

  node_binds.extend((name, nodes[i].name if nodes[i] else None) for i, name in enumerate(dsq.nodes))
  if node_missing and not any(nodes):
    _finish()
    return fail(operator, "The following nodes from the DSQ file could not be found in your scene:\n" + ", ".join(node_missing))
  if node_missing:
    note("Warning: DSQ '{}' has {} node(s) with no empty; skipping: {}".format(
        os.path.basename(filepath), len(node_missing), ", ".join(node_missing)))

  # Now, find all the existing sequence names so we can rename duplicates
  # Also find out where the last user-defined animation data is
  last_frame = 1
  scene_sequences = set()

  for marker in context.scene.timeline_markers:
    last_frame = max(last_frame, int(ceil(marker.frame + 10)))

    if ":" not in marker.name:
      continue

    name, what = marker.name.rsplit(":", 1)
    scene_sequences.add(name)

  for action in bpy.data.actions:
    last_frame = max(last_frame, int(ceil(action.frame_range[1] + 10)))

  if "Sequences" in bpy.data.texts:
    for line in bpy.data.texts["Sequences"].as_string().split("\n"):
      line = line.strip()

      if not line or line == "strict" or ":" not in line:
        continue

      name, flags = line.split(":", 1)
      scene_sequences.add(name)

  sequences_text = []
  reference_frame = find_reference(context.scene)

  # Create Blender keyframes and markers for each sequence
  seq_names = dsq_import_sequence_names(filepath, dsq.sequences)
  for seq, raw_name in zip(dsq.sequences, seq_names):
    name = get_free_name(raw_name, scene_sequences)
    scene_sequences.add(name)
    print("found seq", seq.name, "to", name)

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

    def matters_nodes(matters):
      found = []
      for i, flag in enumerate(matters):
        if not flag:
          continue
        found.append(nodes[i] if i < len(nodes) else None)
      return found

    nodesRotation = matters_nodes(seq.rotationMatters)
    nodesTranslation = matters_nodes(seq.translationMatters)
    nodesScale = matters_nodes(seq.scaleMatters)

    step = 1
    baked_nodes = set()
    if seq.flags & Sequence.ArbitraryScale and nodesScale:
      if seq.flags & Sequence.Blend and reference_frame is None:
        return fail(operator, "Missing 'reference' marker for blend animation '{}'".format(name))
      node_defaults = {}
      for ob in nodes:
        if ob is None:
          continue
        if ob.rotation_mode == "QUATERNION":
          rest_rot = Quaternion(ob.rotation_quaternion)
        else:
          rest_rot = ob.matrix_local.to_quaternion()
        node_defaults[ob] = (ob.location.copy(), rest_rot, ob)
      baked_nodes = import_empty_arbitrary_scale(
        seq, name, last_frame, step,
        nodes, node_defaults,
        nodesTranslation, nodesRotation, nodesScale,
        dsq.translations, dsq.rotations,
        dsq.arbitrary_scale_factors, dsq.arbitrary_scale_rots,
        bool(seq.flags & Sequence.Blend),
        reference_frame, evaluate_all)

    for mattersIndex, ob in enumerate(nodesTranslation):
      if ob is None or ob in baked_nodes:
        continue
      curves = ob_location_curves(ob)

      for frameIndex in range(seq.numKeyframes):
        vec = dsq.translations[seq.baseTranslation + mattersIndex * seq.numKeyframes + frameIndex]
        if seq.flags & Sequence.Blend:
          if reference_frame is None:
            return fail(operator, "Missing 'reference' marker for blend animation '{}'".format(name))
          ref_vec = Vector(evaluate_all(curves, reference_frame))
          vec = ref_vec + vec

        for curve in curves:
          curve.keyframe_points.add(1)
          key = curve.keyframe_points[-1]
          key.interpolation = "LINEAR"
          key.co = (last_frame + frameIndex * step, vec[curve.array_index])

    for mattersIndex, ob in enumerate(nodesRotation):
      if ob is None or ob in baked_nodes:
        continue
      mode, curves = ob_rotation_curves(ob)

      for frameIndex in range(seq.numKeyframes):
        rot = dsq.rotations[seq.baseRotation + mattersIndex * seq.numKeyframes + frameIndex]
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
          key.co = (last_frame + frameIndex * step, rot[curve.array_index])

    for mattersIndex, ob in enumerate(nodesScale):
      if ob is None or ob in baked_nodes:
        continue
      curves = ob_scale_curves(ob)

      for frameIndex in range(seq.numKeyframes):
        index = seq.baseScale + mattersIndex * seq.numKeyframes + frameIndex

        if seq.flags & Sequence.UniformScale:
          s = dsq.uniform_scales[index]
          scale = s, s, s
        elif seq.flags & Sequence.AlignedScale:
          scale = dsq.aligned_scales[index]
        else:
          note("Warning: Invalid scale flags found in sequence")
          break

        for curve in curves:
          curve.keyframe_points.add(1)
          key = curve.keyframe_points[-1]
          key.interpolation = "LINEAR"
          key.co = (last_frame + frameIndex * step, scale[curve.array_index])

    start_frame = last_frame
    end_frame = last_frame + seq.numKeyframes
    context.scene.timeline_markers.new(name + ":start", frame=start_frame)
    context.scene.timeline_markers.new(name + ":end", frame=end_frame)

    if seq.numTriggers:
      span = max(end_frame - start_frame, 1)
      for i in range(seq.numTriggers):
        trigger = dsq.triggers[seq.firstTrigger + i]
        state = decode_trigger_state(trigger.state)
        frame = start_frame + int(round(trigger.pos * span))
        context.scene.timeline_markers.new(
          "{}:trigger {}".format(name, state), frame=frame)

    last_frame += seq.numKeyframes + 10

  if "Sequences" in bpy.data.texts:
    sequences_buf = bpy.data.texts["Sequences"]
  else:
    sequences_buf = bpy.data.texts.new("Sequences")

  if not sequences_buf.as_string():
    sequences_buf.from_string("\n".join(sequences_text))
  else:
    sequences_buf.from_string(sequences_buf.as_string() + "\n" + "\n".join(sequences_text))

  _finish()
  return {"FINISHED"}
