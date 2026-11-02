import fnmatch
import math
import os
import re
import shutil

import bpy
from mathutils import Matrix, Quaternion, Vector

from .DtsTypes import (
    DetailLevel, Mesh, Object, ObjectState, Primitive, Sequence, Trigger,
    TGE_INDEX_U16_LIMIT)
from .write_report import note
from .action_compat import (
    action_fcurves_array, action_has_fcurves, assign_action_to_id,
    iter_action_fcurves, owner_action_fcurves)
from .util import (
    array_from_fcurves, array_from_fcurves_rotation, fcurves_path_from_rotation,
    ob_location_curves, ob_rotation_curves, ob_scale_curves)

re_lod_size = re.compile(r"(-?\d+)$")
common_col_name = re.compile(r"^(LOS)?[cC]ol-?\d+$")
re_detail_size = re.compile(r"^detail(\d+)$", re.I)
re_detail_index_name = re.compile(r"^detail-\d+$", re.I)
re_blender_suffix = re.compile(r"\.\d+$")
re_col_mesh_name = re.compile(r"^(LOS)?[cC]ol-?(\d+)$")
re_collision_collection = re.compile(r"^(?:collision|col)-?(\d+)$", re.I)
re_los_collection = re.compile(r"^(?:los(?:col)?)-?(\d+)$", re.I)

DTS_DETAIL_PROP = "dts_detail"
DTS_SHAPE_PROP = "dts_shape"
DTS_ROLE_PROP = "dts_role"

_MESH_PREFIXES = (
    ("BBZ::", Mesh.Billboard | Mesh.BillboardZAxis),
    ("BBZ_", Mesh.Billboard | Mesh.BillboardZAxis),
    ("BB::", Mesh.Billboard),
    ("BB_", Mesh.Billboard),
    ("SORT::", None),
    ("SORT_", None),
)

_CFG_SECTIONS = {
    "alwaysexport": "always",
    "alwaysexport*": "always",
    "neverexport": "never",
    "neverexport*": "never",
    "neveranimate": "never_anim",
    "neveranimate*": "never_anim",
}


def is_dummy_name(name):
    return name.lower().startswith("dummy")


IO_SCALE_EPS = 1e-12
IO_SCALE_MIN = 1e-6


def resolve_io_scale(scale):
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if value < IO_SCALE_MIN:
        return 1.0
    return value


def io_scale_is_identity(scale):
    return abs(resolve_io_scale(scale) - 1.0) <= IO_SCALE_EPS


def scale_vec3(vec, scale):
    if vec is None:
        return vec
    vec.x *= scale
    vec.y *= scale
    vec.z *= scale
    return vec


def scale_box(box, scale):
    if box is None:
        return box
    scale_vec3(getattr(box, "min", None), scale)
    scale_vec3(getattr(box, "max", None), scale)
    return box


def scale_bind_matrix16(flat, scale):
    # Row-major 4x4 from Matrix.row. Translation is m03, m13, m23.
    if not flat or len(flat) < 12:
        return
    flat[3] *= scale
    flat[7] *= scale
    flat[11] *= scale


def scale_mesh_world_units(mesh, scale):
    if mesh is None or mesh.get_type() == Mesh.NullType:
        return
    for vert in mesh.verts:
        scale_vec3(vert, scale)
    scale_box(getattr(mesh, "bounds", None), scale)
    scale_vec3(getattr(mesh, "center", None), scale)
    if hasattr(mesh, "radius"):
        mesh.radius = float(mesh.radius) * scale
    for bone in getattr(mesh, "bones", ()):
        if bone and len(bone) > 1:
            scale_bind_matrix16(bone[1], scale)


def scale_shape_world_units(shape, scale):
    factor = resolve_io_scale(scale)
    if io_scale_is_identity(factor):
        return 1.0
    for vec in shape.default_translations:
        scale_vec3(vec, factor)
    for vec in shape.node_translations:
        scale_vec3(vec, factor)
    for vec in shape.ground_translations:
        scale_vec3(vec, factor)
    scale_box(getattr(shape, "bounds", None), factor)
    scale_vec3(getattr(shape, "center", None), factor)
    shape.radius = float(shape.radius) * factor
    if hasattr(shape, "radius_tube"):
        shape.radius_tube = float(shape.radius_tube) * factor
    for mesh in shape.meshes:
        scale_mesh_world_units(mesh, factor)
    print("Applied DTS unit scale {}".format(factor))
    return factor


def scale_dsq_world_units(dsq, scale):
    factor = resolve_io_scale(scale)
    if io_scale_is_identity(factor):
        return 1.0
    for vec in dsq.translations:
        scale_vec3(vec, factor)
    for vec in dsq.ground_translations:
        scale_vec3(vec, factor)
    print("Applied DSQ unit scale {}".format(factor))
    return factor


# ---------------------------------------------------------------- node scale type
# TGE 1.5.1 / DTS v24 node scale, as the engine reads and plays it:
#   * A sequence has ONE scale type (Sequence.flags: UniformScale / AlignedScale /
#     ArbitraryScale). Every node in its scaleMatters reads from that type's array
#     (TSShape::getUniformScale / getAlignedScale / getArbitraryScale: baseScale +
#     node * numKeyframes + key), so one sequence can't mix types per node.
#   * The file format lets different sequences use different types (separate
#     nodeUniformScales / nodeAlignedScales / nodeArbitraryScale* arrays), and DSQ
#     import (TSShape::importSequences) appends a DSQ's arrays the same way, then
#     re-runs TSShape::init().
#   * TSShape::init() gives the whole shape the LARGEST type of every sequence whose
#     flags have any scale bit (scale keys or not), DTS and loaded DSQs together.
#   * TSShapeInstance::handleAnimatedScale codes the pair as seq + 3 * shape, but its
#     cases are written for 3 * seq + shape ("1: uniform -> aligned"). The only
#     pairs that play right are the matching ones (0, 4, 8); a uniform sequence on
#     an aligned shape is code 3 and an aligned one on an arbitrary shape code 7:
#     both hit default: AssertFatal (nothing in a release build), no scale is
#     written, and the nodes keep stale scales from the static smNodeCurrent*Scales
#     buffers (whatever shape was animated last) - stretched / squashed models.
#     handleBlendSequence and the transition code also branch on the SHAPE's type.
#   So every scaled sequence that can end up on one shape has to use one type.
#
# The exporter writes AlignedScale for every sequence that has scale keys, DTS and
# DSQ alike, and no scale type at all for a sequence without scale keys:
#   * Aligned is exactly what is sampled: each node's local matrix is
#     Matrix.decompose()d into translation, rotation and a per-axis scale along the
#     node's own axes, which TGE rebuilds as T * R * S (TSTransform::setMatrix, then
#     applyScale -> MatrixF::scale). Uniform is a subset ((s, s, s), lossless).
#   * Arbitrary (scale along a rotated frame, TSScale: quat + factors) is the only
#     type that can also hold shear, but decompose() never produces it, so it would
#     only add a quaternion per key; and it would turn every aligned DSQ or DTS
#     loaded beside it into code 7.
#   * Sequences with no scale keys carry no scale bit, so they can't change the
#     shape's type (the stock Blockland m.dts and its DSQs have no scale keys: they
#     mix with anything).
#   * Uniform is never written: picking uniform whenever a sequence's (or a file's)
#     keys happen to be uniform is what mixed the types (one DTS with a uniform
#     root and aligned fire / pump actions, or a uniform DTS with an aligned DSQ).
#     The original max2dtsExporter picks per sequence too (ShapeMimic.cc
#     setNodeMembership: arbitrary, else aligned, else uniform), so files from it
#     can still mix; re-export them here.
SCALE_FLAGS = Sequence.UniformScale | Sequence.AlignedScale | Sequence.ArbitraryScale


def write_sequence_scales(seq, pending_scales, aligned_list):
    """One sequence's scale keys (Vector per scaled node per key, node-major) as AlignedScale."""
    seq.flags &= ~SCALE_FLAGS
    if not pending_scales:
        return False
    seq.flags |= Sequence.AlignedScale
    seq.baseScale = len(aligned_list)
    aligned_list.extend(pending_scales)
    return True


def apply_sequence_scales(seq, pending_scales, uniform_list, aligned_list):
    """Kept for callers of the old name: always AlignedScale (see above); uniform_list is not written."""
    return write_sequence_scales(seq, pending_scales, aligned_list)


def apply_shape_sequence_scales(entries, uniform_list, aligned_list):
    """Every sequence of a DTS or DSQ: [(seq, pending_scales), ...], all AlignedScale (see above)."""
    count = 0
    for seq, pending in entries:
        if write_sequence_scales(seq, pending, aligned_list):
            count += 1
    if count:
        note("Node scale: {} sequence(s) with scale keys, written as AlignedScale "
             "(one scale type for the whole file: TGE breaks on mixed types)".format(count))
    return "aligned" if count else None


def name_listed(name, patterns):
    if not name or not patterns:
        return False
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


# TGE 1.5.1 TSIntegerSet: 6 dwords * 32 bits. Sequence *Matters flags
# cannot address node/object index 192+.
TGE_TS_SET_SIZE = 192


def tge_shape_set_error(node_count, object_count=0):
    parts = []
    if node_count > TGE_TS_SET_SIZE:
        parts.append(
            "TGE allows {} nodes (got {}). Dummy / never-export / detail "
            "markers / bounds do not count. {} bones + start01 or "
            "__auto_root__ is {}.".format(
                TGE_TS_SET_SIZE, node_count, TGE_TS_SET_SIZE, TGE_TS_SET_SIZE + 1))
    if object_count > TGE_TS_SET_SIZE:
        parts.append(
            "TGE allows {} objects (got {}). LOD copies of the same mesh "
            "name count as one object.".format(TGE_TS_SET_SIZE, object_count))
    if not parts:
        return None
    return " ".join(parts)


def _text_lines(block_name):
    if block_name not in bpy.data.texts:
        return
    for line in bpy.data.texts[block_name].as_string().split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        yield line


def _plain_name_block(*block_names):
    names = set()
    for block_name in block_names:
        for line in _text_lines(block_name):
            key = line.rstrip(":").replace(" ", "").lower()
            if key in _CFG_SECTIONS:
                continue
            names.add(line)
    return names


def read_node_export_lists():
    always = _plain_name_block("AlwaysExport")
    never = _plain_name_block("NeverExport")
    never_anim = _plain_name_block("NeverAnimate")

    for block_name in ("NodeExport", "dtsScene.cfg"):
        section = None
        for line in _text_lines(block_name):
            key = line.rstrip(":").replace(" ", "").lower()
            if key in _CFG_SECTIONS:
                section = _CFG_SECTIONS[key]
                continue
            if section == "always":
                always.add(line)
            elif section == "never":
                never.add(line)
            elif section == "never_anim":
                never_anim.add(line)

    return always, never, never_anim


def split_mesh_prefix(name):
    upper = name.upper()
    for prefix, flags in _MESH_PREFIXES:
        if upper.startswith(prefix):
            rest = name[len(prefix):]
            if flags is None:
                note("Warning: SORT:: on '{}' ignored (sorted meshes not implemented)".format(name))
                return rest, 0
            return rest, flags
    return name, 0


def mesh_billboard_flags(bobj, name):
    name, flags = split_mesh_prefix(name)
    if bobj is not None:
        if bobj.get("BBZ") or bobj.get("BBZ::"):
            flags |= Mesh.Billboard | Mesh.BillboardZAxis
        elif bobj.get("BB") or bobj.get("BB::"):
            flags |= Mesh.Billboard
    return name, flags


def classify_detail_marker(name):
    stripped, flags = split_mesh_prefix(name)
    match = re_detail_size.match(stripped)
    if not match:
        return None, None
    lod_name = "detail{}".format(match.group(1))
    if flags:
        return "bb_detail", lod_name
    return "detail", lod_name


# TGE v24 packs auto-billboard snapshot settings into Detail.objectDetail
# and sets subshape to -1. TSLastDetail unpacks this on load and snapshots
# the live shape; the DTS does not store the bitmaps.
# Engine (tsShape.cc): equator 0..6, polar 7..12, angle 13..18, dl 19..22,
# dim 23..30, includePoles 31. Polar is the value the engine uses (Max
# wrote polar>>1; we write the engine value so the UI matches snapshots).

AUTO_BB_DEFAULTS = {
    "size": 2,
    "equator_steps": 4,
    "polar_steps": 0,
    "polar_angle": math.pi / 5.0,
    "snapshot_detail": 0,
    "dimension": 64,
    "include_poles": True,
}


def pack_auto_billboard(equator_steps=4, polar_steps=0, polar_angle=None,
                        snapshot_detail=0, dimension=64, include_poles=True):
    if polar_angle is None:
        polar_angle = math.pi / float((((polar_steps >> 1) << 1) + 5))
    equator_steps = max(1, min(int(equator_steps), 127))
    polar_steps = max(0, min(int(polar_steps), 63))
    snapshot_detail = max(0, min(int(snapshot_detail), 15))
    dimension = max(1, min(int(dimension), 255))
    angle_q = int(round(64.0 * float(polar_angle) / (math.pi * 0.5)))
    angle_q = max(0, min(angle_q, 63))
    props = 0
    props |= equator_steps
    props |= polar_steps << 7
    props |= angle_q << 13
    props |= snapshot_detail << 19
    props |= dimension << 23
    if include_poles:
        props |= 1 << 31
    return props


def unpack_auto_billboard(props):
    props = int(props) & 0xFFFFFFFF
    angle_q = (props >> 13) & 0x3F
    return {
        "equator_steps": props & 0x7F,
        "polar_steps": (props >> 7) & 0x3F,
        "polar_angle": 0.5 * math.pi * (1.0 / 64.0) * float(angle_q),
        "snapshot_detail": (props >> 19) & 0x0F,
        "dimension": (props >> 23) & 0xFF,
        "include_poles": (props & 0x80000000) != 0,
    }


def is_auto_billboard_detail(lod):
    return lod is not None and lod.subshape < 0


def real_detail_levels(details):
    return [lod for lod in details if not is_auto_billboard_detail(lod)]


def blender_export_base_name(name):
    """Object name for DTS: drop #LOD and Blender .001, keep the rest."""
    if not name:
        return name
    return strip_blender_suffix(str(name).split("#", 1)[0])


def names_are_same_export_identity(blender_name, stored_name):
    left = blender_export_base_name(blender_name or "")
    right = blender_export_base_name(stored_name or "")
    if not left or not right:
        return False
    return left.lower() == right.lower()


def stored_import_name(ob):
    if ob is None or "name" not in ob:
        return None
    try:
        stored = ob["name"]
    except (KeyError, TypeError):
        return None
    if stored is None:
        return None
    text = str(stored).strip()
    return text or None


def object_export_name(ob):
    """Honor the current Blender name if the user renamed an imported object.

    Import stores the DTS name on ['name'] so magazine.001 still exports as
    magazine. That must not override a real rename (gun / gun3rd).
    """
    blender = blender_export_base_name(getattr(ob, "name", "") or "")
    stored = stored_import_name(ob)
    if stored and names_are_same_export_identity(blender, stored):
        return blender_export_base_name(stored)
    return blender


def object_is_auto_billboard_marker(ob):
    if ob is None or ob.type != "EMPTY":
        return False
    props = getattr(ob, "torque_bb_props", None)
    if props is not None and props.enabled:
        return True
    kind, _ = classify_detail_marker(object_export_name(ob))
    return kind == "bb_detail"


def auto_billboard_settings_from_object(ob):
    if not object_is_auto_billboard_marker(ob):
        return None
    props = getattr(ob, "torque_bb_props", None)
    name = object_export_name(ob)
    kind, lod_name = classify_detail_marker(name)
    size = AUTO_BB_DEFAULTS["size"]
    if props is not None:
        size = props.size
    if kind == "bb_detail" and not (props and props.enabled):
        match = re_lod_size.search(lod_name or "")
        if match:
            size = int(match.group(1))
    settings = dict(AUTO_BB_DEFAULTS)
    settings["size"] = int(size)
    if props is not None:
        settings["equator_steps"] = props.equator_steps
        settings["polar_steps"] = props.polar_steps
        settings["polar_angle"] = math.radians(props.polar_angle)
        settings["snapshot_detail"] = props.snapshot_detail
        settings["dimension"] = props.dimension
        settings["include_poles"] = props.include_poles
    return settings


def apply_auto_billboard_settings(ob, settings, size=None):
    props = getattr(ob, "torque_bb_props", None)
    if props is None:
        return
    props.enabled = True
    if size is None:
        size = settings.get("size", AUTO_BB_DEFAULTS["size"])
    props.size = int(size)
    props.equator_steps = settings["equator_steps"]
    props.polar_steps = settings["polar_steps"]
    props.polar_angle = math.degrees(settings["polar_angle"])
    props.snapshot_detail = settings["snapshot_detail"]
    props.dimension = settings["dimension"]
    props.include_poles = settings["include_poles"]


def setup_auto_billboard_empty(ob):
    """Turn an existing empty into an auto-billboard marker (props + BB::detail name)."""
    if ob is None or getattr(ob, "type", None) != "EMPTY":
        return False
    props = getattr(ob, "torque_bb_props", None)
    if props is None:
        return False

    already = bool(props.enabled)
    size = int(props.size)
    name = object_export_name(ob)
    kind, lod_name = classify_detail_marker(name)
    if kind == "bb_detail":
        match = re_lod_size.search(lod_name or "")
        if match:
            size = int(match.group(1))
    elif not already:
        size = int(AUTO_BB_DEFAULTS["size"])

    if not already:
        apply_auto_billboard_settings(ob, AUTO_BB_DEFAULTS, size)
    else:
        props.size = size
        props.enabled = True

    ob.name = "BB::detail{}".format(int(props.size))
    return True


def append_auto_billboard_details(scene, shape, scope=None):
    seen_sizes = {lod.size for lod in shape.detail_levels}
    used_sizes = set()
    for ob in iter_export_objects(scene, scope):
        settings = auto_billboard_settings_from_object(ob)
        if settings is None:
            continue
        size = float(settings["size"])
        label = ob.name
        if size in used_sizes:
            note("Warning: Extra auto-billboard '{}' at size {} ignored".format(label, int(size)))
            continue
        if size in seen_sizes:
            note("Warning: Auto-billboard '{}' size {} is already a mesh LOD, skipping".format(
                label, int(size)))
            continue
        used_sizes.add(size)
        real_count = len(real_detail_levels(shape.detail_levels))
        snapshot = settings["snapshot_detail"]
        if real_count and snapshot >= real_count:
            note("Warning: Auto-billboard '{}' snapshot detail {} is past {} mesh LODs, using 0".format(
                label, snapshot, real_count))
            snapshot = 0
        packed = pack_auto_billboard(
            settings["equator_steps"],
            settings["polar_steps"],
            settings["polar_angle"],
            snapshot,
            settings["dimension"],
            settings["include_poles"])
        name = "BB::detail{}".format(int(size))
        lod = DetailLevel(
            name=shape.name(name),
            subshape=-1,
            objectDetail=packed,
            size=size)
        shape.detail_levels.append(lod)
        print("Adding auto-billboard LOD '{}' (size {}, {}x{}, {} equator steps)".format(
            name, int(size), settings["dimension"], settings["dimension"],
            settings["equator_steps"]))


def is_detail_marker_name(name):
    kind, _ = classify_detail_marker(name)
    return kind is not None


def chop_trailing_lod(name):
    if not name:
        return name, None

    index = len(name)
    while index > 0 and name[index - 1].isdigit():
        index -= 1
    if index == len(name):
        return name, None

    # Blender's .001 / .002 duplicate suffix is not a Torque LOD size.
    # Torque names are mesh32 or mesh-2, never mesh.32.
    if index > 0 and name[index - 1] == ".":
        return name, None

    sign = 1
    base_end = index
    if index > 0 and name[index - 1] in "-_":
        sign = -1
        base_end = index - 1
    if base_end <= 0:
        return name, None

    return name[:base_end], sign * int(name[index:])


def dsq_node_match_keys(name):
    if not name:
        return ()
    keys = [name.lower()]
    stripped = name.split("#", 1)[0]
    if stripped.lower() not in keys:
        keys.append(stripped.lower())
    base, size = chop_trailing_lod(stripped)
    if size is not None and base.lower() not in keys:
        keys.append(base.lower())
    return tuple(keys)


def resolve_dsq_node_name(name, bone_lower):
    if not name or not bone_lower:
        return None
    for key in dsq_node_match_keys(name):
        if key in bone_lower:
            return bone_lower[key]
    want = name.lower()
    for bone_key, bone_name in bone_lower.items():
        if want in dsq_node_match_keys(bone_name):
            return bone_name
        if bone_key.split("#", 1)[0] == want:
            return bone_name
    return None


def is_default_lod_collection(collection_name):
    return collection_name is None or "collection" in collection_name.lower()


def strip_blender_suffix(name):
    if not name:
        return name
    return re_blender_suffix.sub("", name)


def collision_detail_from_mesh_name(name):
    if not name:
        return None
    match = re_col_mesh_name.match(name)
    if not match:
        return None
    number = int(match.group(2))
    if match.group(1):
        return "LOS-{}".format(number)
    return "Collision-{}".format(number)


def lod_size_from_name(name):
    """Pixel size encoded in a Torque-style name.

    detail32 / detail2 embed a positive size. Collision-1 / LOS-1 are
    utility details (negative size). Max-style 'Detail-1' is only an
    index name and is not a size — that file's size field is often 32.
    """
    if not name:
        return None
    stripped = strip_blender_suffix(name)
    match = re_detail_size.match(stripped)
    if match:
        return int(match.group(1))
    if is_collision_or_los_detail(stripped):
        match = re.search(r"(\d+)$", stripped)
        if match:
            return -int(match.group(1))
    return None


def lod_import_collection_name(shape, lod):
    raw = ""
    if lod.name is not None and 0 <= lod.name < len(shape.names):
        raw = shape.names[lod.name] or ""
    kind = detail_collection_kind(raw)
    if kind in ("collision", "los"):
        return canonical_detail_name(raw) or raw
    if lod.size < 0:
        return canonical_detail_name(raw) or raw or "detail{}".format(int(lod.size))
    return "detail{}".format(int(lod.size))


def canonical_detail_name(name):
    if not name:
        return None
    stripped = strip_blender_suffix(name)
    if stripped == "__ignore__":
        return "__ignore__"
    match = re_collision_collection.match(stripped)
    if match:
        return "Collision-{}".format(int(match.group(1)))
    match = re_los_collection.match(stripped)
    if match:
        return "LOS-{}".format(int(match.group(1)))
    match = re_detail_size.match(stripped)
    if match:
        return "detail{}".format(match.group(1))
    return None


def detail_collection_kind(name):
    canonical = canonical_detail_name(name) or name or ""
    lower = canonical.lower()
    if lower.startswith("collision"):
        return "collision"
    if lower.startswith("los"):
        return "los"
    if lower.startswith("detail"):
        return "detail"
    return "detail"


def is_collision_or_los_detail(name):
    return detail_collection_kind(name) in ("collision", "los")


def resolve_collection_lod_name(collection):
    """Current collection name wins. Imported dts_detail is only a fallback."""
    if collection is None:
        return None
    if collection.get(DTS_SHAPE_PROP) or collection.get(DTS_ROLE_PROP):
        return None
    name = getattr(collection, "name", None) or ""
    canonical = canonical_detail_name(name)
    if canonical:
        return canonical
    stripped = strip_blender_suffix(name)
    if name == "__ignore__" or stripped == "__ignore__":
        return "__ignore__"
    stored = collection.get(DTS_DETAIL_PROP)
    if stored:
        stored_text = str(stored).strip()
        if stored_text == "__ignore__":
            return "__ignore__"
        stored_canon = canonical_detail_name(stored_text)
        if stored_canon and (
                names_are_same_export_identity(name, stored_text)
                or is_default_lod_collection(name)):
            return stored_canon
        if stored_text and names_are_same_export_identity(name, stored_text):
            return stored_text
    if is_default_lod_collection(name):
        return None
    return name


def preferred_export_lod(bobj, export_root=None):
    tree = collection_descendant_set(export_root) if export_root is not None else None
    details = []
    for collection in getattr(bobj, "users_collection", ()) or ():
        if tree is not None and collection not in tree:
            continue
        lod_name = resolve_collection_lod_name(collection)
        if lod_name == "__ignore__":
            return "__ignore__"
        if lod_name:
            details.append(lod_name)
    if details:
        names = set(details)
        if len(names) > 1:
            note("Warning: Mesh {} is in multiple detail collections ({})".format(
                bobj.name, ", ".join(sorted(names))))
        return details[0]
    return None


def object_ignored_for_export(bobj):
    if bobj is None:
        return False
    if is_dummy_name(getattr(bobj, "name", "") or ""):
        return True
    if is_dummy_name(object_export_name(bobj)):
        return True
    return preferred_export_lod(bobj) == "__ignore__"


def iter_named_collections():
    seen = set()
    data = getattr(bpy.data, "collections", None)
    if data is not None:
        for collection in data:
            if collection is None or id(collection) in seen:
                continue
            seen.add(id(collection))
            yield collection
    scenes = getattr(bpy.data, "scenes", None)
    if scenes is None:
        return
    for scene in scenes:
        master = getattr(scene, "collection", None)
        if master is None or id(master) in seen:
            continue
        seen.add(id(master))
        yield master


def collection_children(collection):
    if collection is None:
        return ()
    children = getattr(collection, "children", None)
    if not children:
        return ()
    return tuple(children)


def collection_parents(collection):
    if collection is None:
        return ()
    parents = []
    for parent in iter_named_collections():
        if parent is collection:
            continue
        if any(child is collection for child in collection_children(parent)):
            parents.append(parent)
    return tuple(parents)


class _IdentitySet:
    """Membership by object identity."""

    def __init__(self, items=()):
        self._ids = {}
        for item in items:
            self.add(item)

    def add(self, item):
        self._ids[id(item)] = item

    def __contains__(self, item):
        return id(item) in self._ids

    def __iter__(self):
        return iter(self._ids.values())

    def __len__(self):
        return len(self._ids)


def collection_descendant_set(root):
    if root is None:
        return None
    found = _IdentitySet((root,))
    stack = [root]
    while stack:
        current = stack.pop()
        for child in collection_children(current):
            if child in found:
                continue
            found.add(child)
            stack.append(child)
    return found


def collection_looks_like_lod(collection):
    if collection is None:
        return False
    if collection.get(DTS_SHAPE_PROP) or collection.get(DTS_ROLE_PROP):
        return False
    if collection.get(DTS_DETAIL_PROP):
        return True
    return resolve_collection_lod_name(collection) not in (None, "__ignore__")


def lift_export_root(collection):
    if collection is None:
        return None
    if collection.get(DTS_SHAPE_PROP):
        return collection
    if not collection_looks_like_lod(collection):
        return collection
    for parent in collection_parents(collection):
        if parent.get(DTS_SHAPE_PROP):
            if parent is not collection:
                note("Note: Export collection '{}' is a LOD folder; using shape '{}'"
                     .format(collection.name, parent.name))
            return parent
    return collection


def lookup_named_collection(name):
    if not name:
        return None
    data = getattr(bpy.data, "collections", None)
    getter = getattr(data, "get", None)
    if callable(getter):
        found = getter(name)
        if found is not None:
            return found
    for collection in iter_named_collections():
        if getattr(collection, "name", None) == name:
            return collection
    return None


def resolve_export_collection(collection=None, limit_collection=None):
    raw = None
    if isinstance(collection, str):
        name = collection.strip()
        if name:
            raw = lookup_named_collection(name)
            if raw is None:
                note("Warning: Export collection '{}' was not found; exporting the whole scene"
                     .format(name))
                return None
    elif collection is not None:
        raw = collection
    if raw is None and limit_collection is not None:
        if isinstance(limit_collection, str):
            name = limit_collection.strip()
            raw = lookup_named_collection(name) if name else None
        else:
            raw = limit_collection
    return lift_export_root(raw)


def export_object_scope(export_root):
    if export_root is None:
        return None
    objects = getattr(export_root, "all_objects", None)
    if objects is None:
        return None
    return set(objects)


def object_in_export_scope(ob, scope):
    return scope is None or ob in scope


def iter_export_objects(scene, scope=None):
    if scope is None:
        objects = getattr(scene, "objects", ()) or ()
    else:
        objects = scope
    for ob in objects:
        yield ob


def deformer_armatures(ob):
    found = []
    if ob is None:
        return found
    parent = getattr(ob, "parent", None)
    while parent is not None:
        if getattr(parent, "type", None) == "ARMATURE":
            found.append(parent)
            break
        parent = getattr(parent, "parent", None)
    for modifier in getattr(ob, "modifiers", ()) or ():
        if getattr(modifier, "type", None) != "ARMATURE":
            continue
        target = getattr(modifier, "object", None)
        if target is not None and getattr(target, "type", None) == "ARMATURE":
            found.append(target)
    return found


def _scene_object_iter(scene, objects=None):
    if objects is None:
        return getattr(scene, "objects", ()) or ()
    return objects


def listable_export_collections(scene):
    root = getattr(scene, "collection", None)
    if root is None:
        return []
    found = []
    seen = set()
    stack = list(collection_children(root))
    while stack:
        collection = stack.pop()
        key = id(collection)
        if key in seen:
            continue
        seen.add(key)
        stack.extend(collection_children(collection))
        if collection_looks_like_lod(collection):
            continue
        if resolve_collection_lod_name(collection) == "__ignore__":
            continue
        if (getattr(collection, "name", "") or "") == "__ignore__":
            continue
        found.append(collection)
    found.sort(key=lambda item: (getattr(item, "name", "") or "").casefold())
    return found


def populate_export_collection_list(operator, scene):
    items = getattr(operator, "collection_list", None)
    if items is None:
        return
    previous = {item.name: bool(item.is_selected) for item in items}
    items.clear()
    for collection in listable_export_collections(scene):
        name = getattr(collection, "name", "") or ""
        if not name:
            continue
        row = items.add()
        row.name = name
        row.is_selected = previous.get(name, True)


def checked_export_collection_names(operator):
    items = getattr(operator, "collection_list", None)
    if not items:
        return []
    return [item.name for item in items if item.is_selected]


def export_action_objects(scene, export_scope="SCENE", collection="",
                          collection_names=None):
    mode = (export_scope or "SCENE").upper()
    if mode == "SELECTION":
        selected = set()
        for ob in getattr(scene, "objects", ()) or ():
            if not getattr(ob, "select_get", lambda: False)():
                continue
            selected.add(ob)
            selected.update(deformer_armatures(ob))
        return selected
    if mode == "COLLECTION":
        root = resolve_export_collection(collection)
        return export_object_scope(root)
    if mode == "COLLECTIONS":
        union = set()
        for name in collection_names or ():
            root = resolve_export_collection(name)
            scoped = export_object_scope(root)
            if scoped:
                union.update(scoped)
        return union
    if isinstance(collection, str) and collection.strip():
        root = resolve_export_collection(collection)
        return export_object_scope(root)
    return None


def mesh_shape_key_names(objects):
    names = set()
    for ob in objects or ():
        if getattr(ob, "type", None) != "MESH":
            continue
        key = getattr(getattr(ob, "data", None), "shape_keys", None)
        if key is None:
            continue
        for block in getattr(key, "key_blocks", ()) or ():
            name = getattr(block, "name", None)
            if name:
                names.add(name)
    return names


def _collect_anim_actions(anim, bucket):
    if anim is None:
        return
    if getattr(anim, "action", None) is not None:
        bucket.add(anim.action)
    for track in getattr(anim, "nla_tracks", []) or []:
        for strip in getattr(track, "strips", []) or []:
            if getattr(strip, "action", None) is not None:
                bucket.add(strip.action)


def export_action_owners(scene, objects=None):
    source = _scene_object_iter(scene, objects)
    bone_names = set()
    assigned = set()
    shape_assigned = set()
    for ob in source:
        if getattr(ob, "type", None) == "ARMATURE":
            bones = getattr(getattr(ob, "data", None), "bones", ()) or ()
            bone_names.update(
                getattr(bone, "name", "") for bone in bones if getattr(bone, "name", None))
            _collect_anim_actions(getattr(ob, "animation_data", None), assigned)
        if getattr(ob, "type", None) != "MESH" or getattr(ob, "data", None) is None:
            continue
        key = getattr(ob.data, "shape_keys", None)
        _collect_anim_actions(
            None if key is None else getattr(key, "animation_data", None),
            shape_assigned)
    return {
        "bone_names": bone_names,
        "assigned": assigned,
        "shape_assigned": shape_assigned,
        "shape_key_names": set(),
    }


def assign_action_collection(action, collection):
    props = getattr(action, "torque_seq_props", None)
    if action is None or props is None or collection is None:
        return
    try:
        props.collection = collection
    except (AttributeError, TypeError):
        pass


def shape_collection_for_object(ob):
    """Shape collection created for this object, or None.

    Do not fall back to Scene Collection / the active collection. Ungrouped
    imports leave Actions unmapped so Collection export does not filter them.
    """
    if ob is None:
        return None
    for collection in getattr(ob, "users_collection", ()) or ():
        stored = collection.get(DTS_SHAPE_PROP) if hasattr(collection, "get") else None
        if stored:
            return collection
    return None


def action_mapped_collection(action):
    props = getattr(action, "torque_seq_props", None)
    raw = getattr(props, "collection", None) if props is not None else None
    if raw is None:
        return None
    return lift_export_root(raw) or raw


def export_sequence_collections(export_scope="SCENE", collection="",
                                collection_names=None):
    mode = (export_scope or "SCENE").upper()
    if mode == "COLLECTION":
        root = resolve_export_collection(collection)
        return None if root is None else [root]
    if mode == "COLLECTIONS":
        roots = []
        for name in collection_names or ():
            root = resolve_export_collection(name)
            if root is not None:
                roots.append(root)
        return roots
    return None


def collection_roots_have_mapped_actions(roots):
    root_ids = {id(root) for root in roots or () if root is not None}
    if not root_ids:
        return False
    for action in getattr(bpy.data, "actions", ()) or ():
        mapped = action_mapped_collection(action)
        if mapped is not None and id(mapped) in root_ids:
            return True
    return False


def action_visible_for_collections(action, roots, has_mapped=None):
    root_ids = {id(root) for root in roots or () if root is not None}
    if not root_ids:
        return False
    if has_mapped is None:
        has_mapped = collection_roots_have_mapped_actions(roots)
    mapped = action_mapped_collection(action)
    if mapped is not None:
        return id(mapped) in root_ids
    return not has_mapped


def resolve_mesh_identity(name, collection_name, bobj=None):
    name, flags = mesh_billboard_flags(bobj, name)
    # Parse LOD / collision from the unsuffixed name so a duplicated Col-1.001
    # stays a collision mesh. Same-LOD name clashes are uniquified on export.
    name = strip_blender_suffix(name)

    if collection_name == "__ignore__":
        return name, collection_name, flags

    if collection_name and not is_default_lod_collection(collection_name):
        base, size = chop_trailing_lod(name)
        col_match = re_lod_size.search(collection_name)
        if size is not None and col_match and int(col_match.group(1)) == size:
            return base, collection_name, flags
        return name, collection_name, flags

    col_detail = collision_detail_from_mesh_name(name)
    if col_detail:
        return name, col_detail, flags

    base, size = chop_trailing_lod(name)
    if size is not None:
        return base, "detail{}".format(size), flags
    return name, "detail32", flags


def link_collection_child(parent, child):
    if child.name in parent.children:
        return
    try:
        parent.children.link(child)
    except RuntimeError:
        pass


def apply_collection_style(collection, kind):
    colors = {
        "collision": "COLOR_01",
        "los": "COLOR_05",
        "detail": "COLOR_04",
    }
    if hasattr(collection, "color_tag"):
        collection.color_tag = colors.get(kind, "NONE")
    collection.hide_render = kind in ("collision", "los")


def import_parent_collection(context):
    if context.collection is not None:
        return context.collection
    return context.scene.collection


def import_operator_filepaths(operator):
    paths = []
    files = getattr(operator, "files", None)
    directory = getattr(operator, "directory", "") or ""
    if files:
        for entry in files:
            name = getattr(entry, "name", "")
            if not name:
                continue
            paths.append(os.path.join(directory, name) if directory else name)
    filepath = getattr(operator, "filepath", "") or ""
    if filepath and not paths:
        paths.append(filepath)
    unique = []
    seen = set()
    for path in paths:
        if not path or os.path.isdir(path):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def should_group_import_collection(mode, file_count):
    mode = (mode or "MULTI").upper()
    if mode in ("ALWAYS", "IMPORT"):
        return True
    if mode == "DISABLED":
        return False
    return file_count > 1


def ensure_import_shape_collection(parent, shape_name):
    collection = bpy.data.collections.new(shape_name)
    collection[DTS_SHAPE_PROP] = shape_name
    if hasattr(collection, "color_tag"):
        collection.color_tag = "COLOR_03"
    if parent is not None:
        link_collection_child(parent, collection)
    return collection


def _existing_named_collection(name):
    collection = bpy.data.collections.get(name)
    if collection is None:
        return None
    if collection.get(DTS_SHAPE_PROP):
        return None
    return collection


def _child_detail_collection(parent, detail_name):
    if parent is None:
        return None
    for child in parent.children:
        if child.get(DTS_SHAPE_PROP):
            continue
        if child.get(DTS_DETAIL_PROP) == detail_name or child.name == detail_name:
            return child
    return None


def ensure_import_detail_collection(parent, detail_name, cache):
    if detail_name in cache:
        return cache[detail_name]
    scoped = bool(parent is not None and parent.get(DTS_SHAPE_PROP))
    if scoped:
        collection = _child_detail_collection(parent, detail_name)
        if collection is None:
            collection = bpy.data.collections.new(detail_name)
    else:
        collection = _existing_named_collection(detail_name)
        if collection is None:
            collection = bpy.data.collections.new(detail_name)
    collection[DTS_DETAIL_PROP] = detail_name
    apply_collection_style(collection, detail_collection_kind(detail_name))
    link_collection_child(parent, collection)
    cache[detail_name] = collection
    return collection


def link_object_to_collection(obj, collection):
    if obj.name in collection.objects:
        return
    try:
        collection.objects.link(obj)
    except RuntimeError:
        pass


def details_for_mesh_slot(shape, mesh_index, lod_by_mesh):
    lods = [lod for lod in real_detail_levels(shape.detail_levels) if lod.objectDetail == mesh_index]
    if lods:
        return lods
    fallback = lod_by_mesh.get(mesh_index)
    if fallback is not None:
        return [fallback]
    return []


def parse_trigger_token(text, default_frame=None):
    parts = text.split()
    if not parts:
        return None
    if parts[0].lower() != "trigger":
        return None
    if len(parts) < 2:
        note("Warning: trigger token '{}' needs a state (1..5 or -1..-5)".format(text))
        return None
    try:
        state = int(parts[1])
        frame = int(parts[2]) if len(parts) > 2 else default_frame
    except ValueError:
        note("Warning: invalid trigger token '{}'".format(text))
        return None
    if frame is None:
        note("Warning: trigger '{}' needs a frame".format(text))
        return None
    return (state, frame)


def encode_trigger(state, pos):
    if state == 0 or abs(state) > 32:
        note("Warning: trigger state {} is out of range".format(state))
        return None
    if state < 0:
        bits = 1 << (-state - 1)
    else:
        bits = (1 << (state - 1)) | Trigger.StateOn
    return Trigger(bits, pos)


def decode_trigger_state(state):
    if isinstance(state, tuple):
        state = state[0] if state else 0
    state = int(state)
    bits = state & 0x1F
    if bits == 0:
        return 0
    number = bits.bit_length()
    if not (state & Trigger.StateOn):
        return -number
    return number


def finalize_triggers(triggers):
    off = 0
    for trigger in triggers:
        if not (trigger.state & Trigger.StateOn):
            off |= trigger.state & 0x1F
    for trigger in triggers:
        if trigger.state & off:
            trigger.state |= Trigger.InvertOnReverse
    triggers.sort(key=lambda trigger: trigger.pos)
    return triggers


# DTS/Max sequences are 0-based. Keep Blender keys and the scene
# timeline on the same origin even though Blender defaults to frame 1.
SEQUENCE_START_FRAME = 0


def trigger_pos(frame, frame_start, frame_end):
    denom = max(frame_end - frame_start, 1)
    pos = (float(frame) - float(frame_start)) / denom
    return min(max(pos, 0.0), 1.0)


def sequence_trigger_frame(pos, num_keyframes, start_frame=SEQUENCE_START_FRAME):
    span = max(int(num_keyframes) - 1, 1)
    return int(start_frame) + int(round(float(pos) * span))


def imported_sequence_triggers(seq, triggers, start_frame=SEQUENCE_START_FRAME):
    rows = []
    if not getattr(seq, "numTriggers", 0) or not triggers:
        return rows
    for i in range(seq.numTriggers):
        index = seq.firstTrigger + i
        if index < 0 or index >= len(triggers):
            break
        trigger = triggers[index]
        state = getattr(trigger, "state", trigger[0] if isinstance(trigger, tuple) else 0)
        pos = getattr(trigger, "pos", trigger[1] if isinstance(trigger, tuple) else 0)
        if isinstance(pos, tuple):
            pos = pos[0] if pos else 0
        rows.append((
            decode_trigger_state(state),
            sequence_trigger_frame(pos, seq.numKeyframes, start_frame),
        ))
    return rows


def fit_scene_timeline_to_actions(scene, actions, expand=False):
    last = None
    for action in actions:
        if action is None:
            continue
        _, end = action_frame_span(action)
        if last is None or end > last:
            last = end
    if last is None:
        return
    scene.frame_start = SEQUENCE_START_FRAME
    if expand:
        last = max(int(scene.frame_end), int(last))
    scene.frame_end = int(last)
    scene.frame_current = SEQUENCE_START_FRAME


def apply_sequence_flags(seq, flags, frame_range, scene_fps, extra=None):
    extra = extra if extra is not None else {}
    extra.setdefault("triggers", [])
    extra.setdefault("ignore_ground", False)
    extra.setdefault("blend_ref_frame", None)
    extra.setdefault("ground_frame_rate", None)

    for part in flags:
        flag, *data = part.split(" ", 1)
        flag_l = flag.lower()
        data = data[0] if data else ""

        if flag_l == "priority":
            seq.priority = int(data)
        elif flag_l == "cyclic":
            seq.flags |= Sequence.Cyclic
        elif flag_l == "blend":
            seq.flags |= Sequence.Blend
        elif flag_l == "duration":
            seq.duration = float(data)
        elif flag_l in ("framerate", "fps"):
            fps = float(data)
            if fps > 0:
                seq.duration = frame_range / fps
        elif flag_l in ("ignoreground", "ignore_ground"):
            extra["ignore_ground"] = True
        elif flag_l in ("groundframerate", "ground_frame_rate"):
            extra["ground_frame_rate"] = float(data)
        elif flag_l in ("blendreferenceframe", "blendref", "blend_reference_frame"):
            extra["blend_ref_frame"] = int(data)
        elif flag_l in ("enablemorph", "enable_morph", "morph"):
            extra["enable_morph"] = True
        elif flag_l in ("nomorph", "disablemorph", "disable_morph"):
            extra["enable_morph"] = False
        elif flag_l in ("forcemorph", "force_morph"):
            extra["force_morph"] = True
        elif flag_l == "trigger":
            parsed = parse_trigger_token("trigger " + data)
            if parsed:
                extra["triggers"].append(parsed)
        else:
            note("Warning: Unknown flag '{}' (used by sequence '{}')".format(
                part, getattr(seq, "name", None) or seq.nameIndex))

    return extra


def append_sequence_triggers(seq, trigger_list, dest, frame_start, frame_end):
    pending = []
    for state, frame in trigger_list:
        encoded = encode_trigger(state, trigger_pos(frame, frame_start, frame_end))
        if encoded is not None:
            pending.append(encoded)

    finalize_triggers(pending)
    seq.firstTrigger = len(dest)
    seq.numTriggers = len(pending)
    dest.extend(pending)


def find_seqs(scene, select_marker):
    sequences = {}
    sequence_flags = {}
    sequence_triggers = {}

    for marker in scene.timeline_markers:
        if ":" not in marker.name or (select_marker and not marker.select):
            continue

        name, what = marker.name.rsplit(":", 1)
        what_stripped = what.strip()
        what = what_stripped.lower()

        if what.startswith("trigger"):
            parsed = parse_trigger_token(what, marker.frame)
            if parsed:
                sequence_triggers.setdefault(name, []).append(parsed)
            continue

        if name not in sequences:
            sequences[name] = {}

        if what in sequences[name]:
            note("Warning: Got duplicate '{}' marker for sequence '{}' at frame {} (first was at frame {}), ignoring".format(what, name, marker.frame, sequences[name][what].frame))
            continue

        sequences[name][what] = marker

    if "Sequences" in bpy.data.texts:
        for line in bpy.data.texts["Sequences"].as_string().split("\n"):
            line = line.strip()

            if not line:
                continue

            if ":" not in line:
                print("Invalid line in 'Sequences':", line)
                continue

            name, flags = line.split(":", 1)

            if flags.lstrip():
                flags = tuple(map(lambda f: f.strip(), flags.split(",")))
            else:
                flags = ()

            sequence_flags[name] = flags

    return sequences, sequence_flags, sequence_triggers


re_pose_bone = re.compile(r'^pose\.bones\["(.+)"\]')


def _fcurve_key_span(curve):
    """First and last key times. Blender keeps keyframe_points sorted."""
    points = getattr(curve, "keyframe_points", None)
    if not points:
        return None, None
    try:
        start = float(points[0].co[0])
        end = float(points[len(points) - 1].co[0])
    except (TypeError, IndexError, ValueError):
        return None, None
    if end < start:
        start, end = end, start
    return start, end


def action_keyframe_extent(action):
    first = None
    last = None
    for curve in iter_action_fcurves(action):
        start, end = _fcurve_key_span(curve)
        if start is None:
            continue
        if first is None or start < first:
            first = start
        if last is None or end > last:
            last = end
    return first, last


def action_frame_span(action):
    if action is None:
        return SEQUENCE_START_FRAME, SEQUENCE_START_FRAME
    if getattr(action, "use_frame_range", False):
        start = int(round(action.frame_start))
        end = int(round(action.frame_end))
    else:
        _first, last = action_keyframe_extent(action)
        start = SEQUENCE_START_FRAME
        end = int(round(last)) if last is not None else start
    if end < start:
        end = start
    return start, end


def pose_bone_name_from_path(data_path):
    match = re_pose_bone.match(data_path)
    if match:
        return match.group(1)
    return None


def action_has_pose_curves(action, bone_names=None):
    if action is None or not action_has_fcurves(action):
        return False
    for curve in iter_action_fcurves(action):
        name = pose_bone_name_from_path(curve.data_path)
        if name is None:
            continue
        if bone_names is None or name in bone_names:
            return True
    return False


def scene_armature_bone_names(scene, objects=None):
    return export_action_owners(scene, objects)["bone_names"]


def assigned_armature_actions(scene, objects=None):
    return export_action_owners(scene, objects)["assigned"]


def assigned_shape_key_actions(scene, objects=None):
    return export_action_owners(scene, objects)["shape_assigned"]


def extra_from_action(action):
    extra = {
        "triggers": [],
        "ignore_ground": False,
        "blend_ref_frame": None,
        "ground_frame_rate": None,
        "enable_transform": True,
        "enable_scale": True,
        "enable_vis": True,
        "enable_ifl": True,
        "enable_morph": True,
        "force_transform": False,
        "force_scale": False,
        "force_vis": False,
        "force_morph": False,
        "from_action": True,
    }
    extra["cyclic"] = bool(getattr(action, "use_cyclic", False))
    props = getattr(action, "torque_seq_props", None)
    if props is None:
        return extra
    extra["blend"] = bool(props.blend)
    extra["priority"] = int(props.priority)
    extra["duration"] = float(props.duration) if props.override_duration else 0.0
    extra["frame_rate"] = float(props.frame_rate)
    extra["ignore_ground"] = bool(props.ignore_ground)
    extra["enable_transform"] = bool(props.enable_transform)
    extra["enable_scale"] = bool(props.enable_scale)
    extra["enable_vis"] = bool(props.enable_vis)
    extra["enable_ifl"] = bool(props.enable_ifl)
    extra["enable_morph"] = bool(getattr(props, "enable_morph", True))
    extra["force_transform"] = bool(props.force_transform)
    extra["force_scale"] = bool(props.force_scale)
    extra["force_vis"] = bool(props.force_vis)
    extra["force_morph"] = bool(getattr(props, "force_morph", False))
    if props.ground_frame_rate > 0:
        extra["ground_frame_rate"] = float(props.ground_frame_rate)
    if props.blend and props.blend_ref_frame != 0:
        extra["blend_ref_frame"] = int(props.blend_ref_frame)
    for trigger in props.triggers:
        extra["triggers"].append((int(trigger.state), int(trigger.frame)))
    return extra


def sequence_frame_rate(frame_range, scene_fps, duration_override=0, frame_rate=0):
    if duration_override > 0 and frame_range > 0:
        return float(frame_range) / float(duration_override)
    if frame_rate > 0:
        return float(frame_rate)
    return float(scene_fps) if scene_fps else 0.0


def sequence_duration_seconds(frame_range, scene_fps, duration_override=0, frame_rate=0):
    if duration_override > 0:
        return float(duration_override)
    rate = sequence_frame_rate(frame_range, scene_fps, 0, frame_rate)
    if rate <= 0:
        return 0.0
    return float(frame_range) / rate


def imported_sequence_frame_rate(seq):
    if seq.duration > 0 and seq.numKeyframes > 0:
        return float(seq.numKeyframes) / float(seq.duration)
    return 0.0


def resolve_blend_reference_frame(extra, reference_frame, frame_start, frame_end):
    extra = extra or {}
    stored = extra.get("blend_ref_frame")
    if stored is not None:
        return int(stored)
    if reference_frame is None:
        return None
    ref = int(reference_frame)
    # A marker sitting inside this Action is a key, not the rest pose
    # we baked blend deltas onto.
    if int(frame_start) <= ref <= int(frame_end):
        return None
    return ref


def apply_action_extra(seq, extra, frame_range, scene_fps):
    if extra.get("cyclic"):
        seq.flags |= Sequence.Cyclic
    if extra.get("blend"):
        seq.flags |= Sequence.Blend
    if extra.get("priority") is not None:
        seq.priority = int(extra["priority"])
    seq.duration = sequence_duration_seconds(
        frame_range, scene_fps,
        extra.get("duration") or 0,
        extra.get("frame_rate") or 0)
    return extra


def apply_sequence_membership(seq, extra, node_count, object_count=None):
    extra = extra or {}
    if extra.get("force_transform"):
        seq.rotationMatters = [True] * node_count
        seq.translationMatters = [True] * node_count
    elif extra.get("enable_transform") is False:
        seq.rotationMatters = [False] * node_count
        seq.translationMatters = [False] * node_count
    if extra.get("force_scale"):
        seq.scaleMatters = [True] * node_count
    elif extra.get("enable_scale") is False:
        seq.scaleMatters = [False] * node_count
    # TGE visMatters is a set of objects. DSQ has no object-state block,
    # so leave vis alone unless the caller passed an object count.
    if object_count is not None:
        if extra.get("force_vis"):
            seq.visMatters = [True] * object_count
        elif extra.get("enable_vis") is False:
            seq.visMatters = [False] * object_count
        if extra.get("force_morph"):
            seq.frameMatters = [True] * object_count
        elif extra.get("enable_morph") is False:
            seq.frameMatters = [False] * object_count
    if extra.get("enable_ifl") is False:
        seq.iflMatters = [False] * len(seq.iflMatters)


EXPORT_KIND_DTS = "DTS"
EXPORT_KIND_DSQ = "DSQ"


def torque_export_pg(scene, kind):
    if (kind or EXPORT_KIND_DTS).upper() == EXPORT_KIND_DSQ:
        return scene.torque_dsq_export
    return scene.torque_dts_export


def export_sequence_list_propnames(sequence_mode):
    if (sequence_mode or "ACTIONS").upper() == "MARKERS":
        return "marker_list", "marker_list_index"
    return "action_list", "action_list_index"


def action_included_for_export(props, kind):
    if props is None:
        return True
    if not props.export:
        return False
    kind = (kind or "").upper()
    if kind == EXPORT_KIND_DTS:
        return bool(getattr(props, "export_dts", True))
    if kind == EXPORT_KIND_DSQ:
        return bool(getattr(props, "export_dsq", True))
    return True


def action_has_forced_tracks(props):
    """Dest-key / forced tracks are sequences even with no F-Curves."""
    if props is None:
        return False
    return bool(
        getattr(props, "force_transform", False)
        or getattr(props, "force_scale", False)
        or getattr(props, "force_vis", False)
        or getattr(props, "force_morph", False))


def action_is_listable_sequence(
        action, bone_names=None, assigned=None, shape_assigned=None):
    props = getattr(action, "torque_seq_props", None)
    if props is not None and not props.export:
        return False
    forced = action_has_forced_tracks(props)
    if not action_has_fcurves(action) and not action.use_frame_range and not forced:
        return False
    if forced or action_has_pose_curves(action, bone_names):
        return True
    if action_shape_key_names(action):
        return True
    on_armature = assigned is not None and action in assigned
    on_keys = shape_assigned is not None and action in shape_assigned
    return bool(on_armature or on_keys)


def iter_listable_actions(scene, collection_roots=None, show_all=False):
    owners = export_action_owners(scene, None)
    bone_names = owners["bone_names"]
    assigned = owners["assigned"]
    shape_assigned = owners["shape_assigned"]
    has_mapped = None
    if not show_all and collection_roots is not None:
        has_mapped = collection_roots_have_mapped_actions(collection_roots)
    for action in bpy.data.actions:
        if not action_is_listable_sequence(
                action, bone_names, assigned, shape_assigned):
            continue
        if show_all or collection_roots is None:
            yield action
            continue
        if action_visible_for_collections(action, collection_roots, has_mapped):
            yield action


def apply_export_sequence_item_selection(item):
    if not getattr(item, "sync_export_flag", True):
        return
    if getattr(item, "source", "ACTION") != "ACTION" or not item.action_name:
        return
    action = bpy.data.actions.get(item.action_name)
    if action is None:
        return
    props = getattr(action, "torque_seq_props", None)
    if props is None:
        return
    kind = (item.kind or "").upper()
    if kind == EXPORT_KIND_DTS:
        props.export_dts = bool(item.is_selected)
    elif kind == EXPORT_KIND_DSQ:
        props.export_dsq = bool(item.is_selected)


def selected_export_item_key(item):
    """Stable checkbox key: Action datablock name, else the displayed name."""
    return getattr(item, "action_name", "") or getattr(item, "name", "")


def snapshot_export_item_checks(items):
    previous = {}
    for item in items:
        previous[selected_export_item_key(item)] = bool(item.is_selected)
    return previous


def restore_export_item_check(previous, action=None, display_name="", fallback=True):
    if action is not None:
        action_name = getattr(action, "name", "") or ""
        if action_name in previous:
            return bool(previous[action_name])
        return fallback
    if display_name in previous:
        return bool(previous[display_name])
    return fallback


def action_matches_export_selection(action, written_name, selected):
    """Match a picker selection by Action name, not the stripped write name."""
    if not selected:
        return False
    action_l = (getattr(action, "name", "") or "").lower()
    if action_l in selected:
        return True
    written_l = (written_name or "").lower()
    return bool(written_l) and written_l in selected and action_l == written_l


def _seq_tip_yes_no(value):
    return "Yes" if value else "No"


def export_sequence_item_tooltip(item, scene=None):
    """Artist-facing hover text for a DTS/DSQ export-list row."""
    frame_start = int(getattr(item, "frame_start", 0) or 0)
    frame_end = int(getattr(item, "frame_end", 0) or 0)
    frames = abs(frame_end - frame_start) + 1
    action = bpy.data.actions.get(getattr(item, "action_name", "") or "")
    props = getattr(action, "torque_seq_props", None) if action is not None else None
    cyclic = bool(getattr(action, "use_cyclic", False)) if action is not None else bool(
        getattr(item, "cyclic", False))
    blend = bool(props.blend) if props is not None else bool(getattr(item, "blend", False))
    priority = int(props.priority) if props is not None else 1
    scene_fps = 30.0
    if scene is not None:
        scene_fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
    duration_override = float(props.duration) if props is not None and props.override_duration else 0.0
    frame_rate = float(props.frame_rate) if props is not None else 0.0
    seconds = sequence_duration_seconds(frames, scene_fps, duration_override, frame_rate)
    fps = sequence_frame_rate(frames, scene_fps, duration_override, frame_rate)
    blend_ref = 0
    if props is not None:
        blend_ref = int(props.blend_ref_frame)
    if blend and blend_ref:
        blend_ref_text = "frame {}".format(blend_ref)
    elif blend:
        blend_ref_text = "rest (0 / reference marker)"
    else:
        blend_ref_text = "—"

    lines = [getattr(item, "name", "") or "Sequence"]
    if action is not None and action.name != item.name:
        lines.append("Action: {}".format(action.name))
    elif getattr(item, "source", "ACTION") == "MARKER":
        lines.append("Source: timeline markers")
    mapped = action_mapped_collection(action) if action is not None else None
    mapped_name = getattr(mapped, "name", "") or ""
    if mapped_name:
        lines.append("Collection: {}".format(mapped_name))
    lines.append("Keys: {}  (frames {}–{})".format(frames, frame_start, frame_end))
    if seconds > 0:
        lines.append("Duration: {:.3g} s at {:.4g} fps".format(seconds, fps))
        if duration_override > 0:
            lines.append("Duration override: {:.3g} s".format(duration_override))
    lines.append("Priority: {}".format(priority))
    lines.append("Cyclic: {}".format(_seq_tip_yes_no(cyclic)))
    lines.append("Blend: {}".format(_seq_tip_yes_no(blend)))
    lines.append("Blend ref: {}".format(blend_ref_text))
    if props is not None:
        if props.ignore_ground:
            ground = "ignored"
        elif props.ground_frame_rate > 0:
            ground = "{:g} fps".format(props.ground_frame_rate)
        else:
            ground = "every frame"
        lines.append("Ground: {}".format(ground))
        n_trig = len(props.triggers)
        lines.append("Triggers: {}".format(n_trig if n_trig else "none"))
        lines.append("Include: DTS {} · DSQ {}".format(
            _seq_tip_yes_no(props.export_dts), _seq_tip_yes_no(props.export_dsq)))
        tracks = []
        if props.force_transform:
            tracks.append("transform (forced)")
        elif props.enable_transform:
            tracks.append("transform")
        if props.force_scale:
            tracks.append("scale (forced)")
        elif props.enable_scale:
            tracks.append("scale")
        if props.force_vis:
            tracks.append("vis (forced)")
        elif props.enable_vis:
            tracks.append("vis")
        if props.enable_ifl:
            tracks.append("IFL")
        if getattr(props, "force_morph", False):
            tracks.append("morph (forced)")
        elif getattr(props, "enable_morph", True):
            tracks.append("morph")
        if tracks:
            lines.append("Tracks: {}".format(", ".join(tracks)))
    return "\n".join(lines)


def _fill_export_sequence_item(item, kind, source, name, action=None,
                               frame_start=0, frame_end=0, selected=True,
                               sync_export_flag=True):
    item.name = name
    item.kind = kind
    item.source = source
    item.action_name = action.name if action is not None else ""
    item.frame_start = int(frame_start)
    item.frame_end = int(frame_end)
    item.cyclic = bool(getattr(action, "use_cyclic", False)) if action is not None else False
    props = getattr(action, "torque_seq_props", None) if action is not None else None
    item.blend = bool(props.blend) if props is not None else False
    item.sync_export_flag = bool(sync_export_flag)
    item.is_selected = bool(selected)


def populate_export_sequence_list(scene, kind, sequence_mode, select_marker=False,
                                 objects=None, show_all=False,
                                 collection_roots=None, strip_action_suffixes=True):
    pg = torque_export_pg(scene, kind)
    pg.kind = kind
    list_prop, _index_prop = export_sequence_list_propnames(sequence_mode)
    previous = {}
    for collection_name in ("action_list", "marker_list"):
        previous.update(snapshot_export_item_checks(getattr(pg, collection_name)))
    pg.action_list.clear()
    pg.marker_list.clear()

    if (sequence_mode or "ACTIONS").upper() == "MARKERS":
        markers, _flags, _triggers = find_seqs(scene, False)
        for name, marks in markers.items():
            start = marks.get("start")
            end = marks.get("end")
            if select_marker:
                fallback = any(getattr(mark, "select", False) for mark in marks.values())
            else:
                fallback = True
            selected = restore_export_item_check(
                previous, display_name=name, fallback=fallback)
            _fill_export_sequence_item(
                pg.marker_list.add(), kind, "MARKER", name,
                frame_start=int(round(start.frame)) if start is not None else 0,
                frame_end=int(round(end.frame)) if end is not None else 0,
                selected=selected)
        return pg

    for action in iter_listable_actions(scene, collection_roots, show_all):
        name = action_export_sequence_name(
            action, strip_suffixes=strip_action_suffixes)
        frame_start, frame_end = action_frame_span(action)
        props = getattr(action, "torque_seq_props", None)
        selected = restore_export_item_check(
            previous, action=action,
            fallback=action_included_for_export(props, kind))
        _fill_export_sequence_item(
            pg.action_list.add(), kind, "ACTION", name, action,
            frame_start, frame_end, selected)
    return pg


_POPULATING_COLLECTION_PAGES = False


def populate_export_collection_pages(operator, scene):
    global _POPULATING_COLLECTION_PAGES
    pages = getattr(operator, "collection_pages", None)
    if pages is None or _POPULATING_COLLECTION_PAGES:
        return
    _POPULATING_COLLECTION_PAGES = True
    try:
        _populate_export_collection_pages_impl(operator, scene)
    finally:
        _POPULATING_COLLECTION_PAGES = False


def _populate_export_collection_pages_impl(operator, scene):
    pages = operator.collection_pages
    previous = {}
    show_all = {}
    for page in pages:
        show_all[page.name] = bool(page.show_all_actions)
        previous[page.name] = snapshot_export_item_checks(page.action_list)
    index_name = ""
    if pages and 0 <= operator.collection_page_index < len(pages):
        index_name = pages[operator.collection_page_index].name
    pages.clear()
    strip_suffixes = bool(getattr(operator, "strip_action_suffixes", True))
    for name in checked_export_collection_names(operator):
        root = resolve_export_collection(name)
        if root is None and not show_all.get(name, False):
            continue
        page = pages.add()
        page.name = name
        page.show_all_actions = show_all.get(name, False)
        checks = previous.get(name, {})
        for action in iter_listable_actions(
                scene, None if page.show_all_actions else ([root] if root else []),
                page.show_all_actions):
            seq_name = action_export_sequence_name(
                action, strip_suffixes=strip_suffixes)
            frame_start, frame_end = action_frame_span(action)
            selected = restore_export_item_check(
                checks, action=action, fallback=True)
            _fill_export_sequence_item(
                page.action_list.add(), "DTS", "ACTION", seq_name, action,
                frame_start, frame_end, selected, sync_export_flag=False)
    if index_name:
        for index, page in enumerate(pages):
            if page.name == index_name:
                operator.collection_page_index = index
                return
    operator.collection_page_index = 0 if pages else 0


def current_export_collection_page(operator):
    pages = getattr(operator, "collection_pages", None)
    if not pages:
        return None
    index = int(getattr(operator, "collection_page_index", 0) or 0)
    if index < 0 or index >= len(pages):
        return None
    return pages[index]


def collection_page_selected_names(operator, collection_name):
    pages = getattr(operator, "collection_pages", None)
    if not pages:
        return None
    for page in pages:
        if page.name == collection_name:
            return [
                item.action_name or item.name
                for item in page.action_list
                if item.is_selected]
    return []


def selected_export_sequence_names(scene, kind, sequence_mode):
    pg = torque_export_pg(scene, kind)
    list_prop, _index_prop = export_sequence_list_propnames(sequence_mode)
    return {
        selected_export_item_key(item)
        for item in getattr(pg, list_prop) if item.is_selected
    }


def collect_action_sequences(scene, export_kind=None, selected_names=None,
                            objects=None, show_all=False,
                            collection_roots=None, strip_action_suffixes=True):
    owners = export_action_owners(scene, None)
    bone_names = owners["bone_names"]
    assigned = owners["assigned"]
    shape_assigned = owners["shape_assigned"]
    has_mapped = None
    if not show_all and collection_roots is not None:
        has_mapped = collection_roots_have_mapped_actions(collection_roots)
    found = []
    seen = set()
    selected = None
    if selected_names is not None:
        selected = {name.lower() for name in selected_names}
    for action in bpy.data.actions:
        props = getattr(action, "torque_seq_props", None)
        if not action_is_listable_sequence(
                action, bone_names, assigned, shape_assigned):
            continue
        if not show_all and collection_roots is not None:
            if not action_visible_for_collections(
                    action, collection_roots, has_mapped):
                continue
        name = action_export_sequence_name(
            action, bone_names, strip_action_suffixes)
        if selected is not None:
            if not action_matches_export_selection(action, name, selected):
                continue
        elif not action_included_for_export(props, export_kind):
            continue
        key = name.lower()
        if key in seen:
            note("Warning: Duplicate Action sequence name '{}', ignoring '{}'".format(
                name, action.name))
            continue
        seen.add(key)
        frame_start, frame_end = action_frame_span(action)
        found.append({
            "name": name,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "action": action,
            "extra": extra_from_action(action),
            "markers": None,
            "flags": (),
            "triggers": [],
        })
    return found


def marker_sequence_entry(name, marks, flags, triggers):
    return {
        "name": name,
        "frame_start": None,
        "frame_end": None,
        "action": None,
        "extra": None,
        "markers": marks,
        "flags": flags.get(name, ()),
        "triggers": triggers.get(name, []),
    }


_DSQ_FILENAME_BAD = re.compile(r'[<>:"/\\|?*]')


def sanitize_dsq_stem(name):
    text = _DSQ_FILENAME_BAD.sub("_", str(name or "").strip())
    text = text.strip(" .")
    return text or "sequence"


def export_path_is_directory(path):
    if not path:
        return False
    if path.endswith(("/", "\\")) or path.endswith(os.sep):
        return True
    try:
        return os.path.isdir(path)
    except (OSError, TypeError, ValueError):
        return False


def export_directory(path):
    if not path:
        return ""
    if export_path_is_directory(path):
        return os.path.normpath(path)
    return os.path.dirname(os.path.normpath(path))


def blend_export_stem():
    try:
        blend = getattr(bpy.data, "filepath", "") or ""
    except Exception:
        blend = ""
    if blend:
        stem = os.path.splitext(os.path.basename(blend))[0].strip()
        if stem:
            return sanitize_dsq_stem(stem)
    return "untitled"


def resolve_export_filepath(filepath, extension):
    """Fill in a missing export name from the blend file, or untitled."""
    ext = str(extension or "")
    if not ext.startswith("."):
        ext = ".{}".format(ext)
    if not filepath or export_path_is_directory(filepath):
        directory = export_directory(filepath) if filepath else ""
        return os.path.join(directory, blend_export_stem() + ext)
    path = os.path.normpath(filepath)
    directory, name = os.path.split(path)
    stem, _existing = os.path.splitext(name)
    if not stem:
        name = blend_export_stem() + ext
    elif not _existing:
        name = stem + ext
    return os.path.join(directory, name) if directory else name


def dsq_split_filepath(chosen_path, seq_name, prefix="", suffix=""):
    directory = export_directory(chosen_path)
    stem = "{}{}{}".format(prefix or "", sanitize_dsq_stem(seq_name), suffix or "")
    if not stem.strip(" ."):
        stem = "sequence"
    return os.path.join(directory, stem + ".dsq")


def unique_output_path(path, used):
    used = used if used is not None else set()
    if path not in used:
        used.add(path)
        return path
    root, ext = os.path.splitext(path)
    index = 2
    while True:
        trial = "{}#{}{}".format(root, index, ext or ".dsq")
        if trial not in used:
            used.add(trial)
            return trial
        index += 1


def find_export_sequences(scene, select_marker, sequence_mode="ACTIONS",
                          export_kind=None, selected_names=None,
                          objects=None, show_all=False,
                          collection_roots=None, strip_action_suffixes=True):
    mode = (sequence_mode or "ACTIONS").upper()
    names = selected_names
    if mode == "MARKERS":
        if names is None and export_kind and hasattr(scene, "torque_dts_export"):
            items = torque_export_pg(scene, export_kind).marker_list
            if len(items):
                names = {item.name for item in items if item.is_selected}
        sequences = []
        markers, flags, triggers = find_seqs(
            scene, select_marker if names is None else False)
        for name, marks in markers.items():
            if names is not None and name not in names:
                continue
            sequences.append(marker_sequence_entry(name, marks, flags, triggers))
        return sequences
    if names is None and export_kind and hasattr(scene, "torque_dts_export"):
        items = torque_export_pg(scene, export_kind).action_list
        if len(items):
            names = {
                selected_export_item_key(item)
                for item in items if item.is_selected
            }
    return collect_action_sequences(
        scene, export_kind=export_kind, selected_names=names,
        objects=objects, show_all=show_all,
        collection_roots=collection_roots,
        strip_action_suffixes=strip_action_suffixes)


def sequence_flags_line(name, seq):
    flags = ["priority {}".format(seq.priority)]
    if seq.flags & Sequence.Cyclic:
        flags.append("cyclic")
    if seq.flags & Sequence.Blend:
        flags.append("blend")
    if seq.duration > 0 and seq.numKeyframes > 0:
        flags.append("fps {:.6g}".format(seq.numKeyframes / seq.duration))
    return name + ": " + ", ".join(flags)


def append_sequences_text(lines):
    if not lines:
        return
    if "Sequences" in bpy.data.texts:
        buf = bpy.data.texts["Sequences"]
    else:
        buf = bpy.data.texts.new("Sequences")
    existing = buf.as_string()
    added = "\n".join(lines)
    if not existing:
        buf.from_string(added)
    else:
        buf.from_string(existing.rstrip() + "\n" + added)


def next_marker_layout_frame(scene, gap=10):
    last = 10
    for marker in scene.timeline_markers:
        last = max(last, int(round(marker.frame)) + gap)
    for action in bpy.data.actions:
        last = max(last, int(round(action.frame_range[1])) + gap)
    return last


def write_sequence_markers(scene, name, start_frame, end_frame, triggers=None):
    scene.timeline_markers.new(name + ":start", frame=int(start_frame))
    scene.timeline_markers.new(name + ":end", frame=int(end_frame))
    if not triggers:
        return
    for state, frame in triggers:
        scene.timeline_markers.new(
            "{}:trigger {}".format(name, state), frame=int(frame))


def _oneshot_armature_action(armature):
    if armature.animation_data is None:
        armature.animation_data_create()
    action = armature.animation_data.action
    props = getattr(action, "torque_seq_props", None) if action is not None else None
    if action is not None and props is not None and not props.export:
        return action
    action = bpy.data.actions.new(armature.name + "Action")
    props = getattr(action, "torque_seq_props", None)
    if props is not None:
        props.export = False
        props.export_dts = False
        props.export_dsq = False
    armature.animation_data.action = action
    return action


def reset_armature_pose(armature):
    """Clear leftover pose so the next Action cannot inherit the last clip."""
    identity = Matrix.Identity(4)
    for pose_bone in armature.pose.bones:
        pose_bone.matrix_basis = identity
    reset_bone_morph_proxy_values(armature)


def refresh_scene_frame(scene, frame=None):
    """Force a depsgraph evaluate even if the current frame does not change."""
    target = int(scene.frame_current if frame is None else frame)
    if int(scene.frame_current) == target:
        scene.frame_set(target + 1)
    scene.frame_set(target)


def snapshot_scene_timeline(scene, default_frame=0):
    """Remember the playhead so export can put it back."""
    fallback = 0
    try:
        fallback = int(default_frame)
    except (TypeError, ValueError):
        fallback = 0
    if scene is None:
        return fallback, 0.0
    try:
        frame = int(getattr(scene, "frame_current", fallback))
    except (TypeError, ValueError):
        frame = fallback
    try:
        subframe = float(getattr(scene, "frame_subframe", 0.0) or 0.0)
    except (TypeError, ValueError):
        subframe = 0.0
    if subframe < 0.0:
        subframe = 0.0
    return frame, subframe


def restore_scene_timeline_frame(scene, frame, subframe=0.0, default_frame=0):
    """Seek back to the saved playhead, or frame 0 if the save is unusable."""
    if scene is None:
        return
    try:
        target = int(frame)
    except (TypeError, ValueError):
        target = 0
        try:
            target = int(default_frame)
        except (TypeError, ValueError):
            target = 0
    try:
        sub = float(subframe or 0.0)
    except (TypeError, ValueError):
        sub = 0.0
    if sub < 0.0:
        sub = 0.0
    try:
        scene.frame_set(target, subframe=sub)
        return
    except TypeError:
        pass
    except Exception:
        target = 0
        try:
            target = int(default_frame)
        except (TypeError, ValueError):
            target = 0
    try:
        scene.frame_set(target)
    except Exception:
        try:
            scene.frame_set(0)
        except Exception:
            pass


class restore_scene_timeline:
    """Save the current frame at enter; restore it after the whole export."""

    def __init__(self, scene, default_frame=0):
        self.scene = scene
        self.default_frame = 0
        try:
            self.default_frame = int(default_frame)
        except (TypeError, ValueError):
            self.default_frame = 0
        self.frame = self.default_frame
        self.subframe = 0.0

    def __enter__(self):
        self.frame, self.subframe = snapshot_scene_timeline(
            self.scene, self.default_frame)
        return self

    def __exit__(self, exc_type, exc, tb):
        restore_scene_timeline_frame(
            self.scene, self.frame, self.subframe, self.default_frame)
        return False


def set_scene_eval_time(scene, time):
    value = float(time)
    frame = int(math.floor(value + 1e-9))
    subframe = value - frame
    if subframe < 0.0:
        subframe = 0.0
    scene.frame_set(frame, subframe=subframe)


def sequence_sample_times(frame_start, frame_end):
    start = int(frame_start)
    end = int(frame_end)
    if end < start:
        end = start
    return list(range(start, end + 1))


def sequence_eval_times(frame_start, frame_end, extra=None):
    return sequence_sample_times(frame_start, frame_end)


class assign_armature_action:
    def __init__(self, scene, action):
        self.scene = scene
        self.action = action
        self.restore = []
        self.key_restore = []

    def __enter__(self):
        if self.action is not None:
            for ob in self.scene.objects:
                if ob.type != "ARMATURE":
                    continue
                data = ob.animation_data
                if data is None:
                    data = ob.animation_data_create()
                self.restore.append((ob, data.action, data.use_nla))
                data.use_nla = False
                data.action = self.action
                reset_armature_pose(ob)
        bind_sequence_shape_keys(self.scene, self.action, self.key_restore)
        if self.restore or self.key_restore:
            refresh_scene_frame(self.scene)
            apply_bone_morph_proxies(self.scene)
        return self

    def __exit__(self, exc_type, exc, tb):
        restore_sequence_shape_keys(self.key_restore)
        for ob, _action, _use_nla in self.restore:
            data = ob.animation_data
            if data is None:
                continue
            data.use_nla = False
            data.action = None
        for ob in self.scene.objects:
            if ob.type == "ARMATURE":
                reset_armature_pose(ob)
        refresh_scene_frame(self.scene)
        for ob, action, use_nla in self.restore:
            data = ob.animation_data
            if data is None:
                continue
            data.action = action
            data.use_nla = use_nla
        return False


def bone_rest_local_matrix(armature, bone):
    mat = bone.matrix_local.copy()
    if bone.parent:
        mat = bone.parent.matrix_local.inverted() @ mat
    return mat


def pose_basis_from_local(rest_local, anim_local):
    return rest_local.inverted() @ anim_local


SHAPE_KEY_VALUE_RE = re.compile(r'^key_blocks\["(.+)"\]\.value$')
SHAPE_KEY_SEQ_DELIM_RE = re.compile(r"[._#]")
POSE_CUSTOM_PROP_RE = re.compile(r'^pose\.bones\["(.+)"\]\["(.+)"\]$')
MORPH_VERT_EPS = 0.00005


def bone_morph_prop_path(bone_name, prop_name):
    return 'pose.bones["{}"]["{}"]'.format(
        _rna_path_quote(bone_name), _rna_path_quote(prop_name))


def _rna_path_quote(name):
    return (name or "").replace("\\", "\\\\").replace('"', '\\"')


def shape_key_value_data_path(key_name):
    return 'key_blocks["{}"].value'.format(_rna_path_quote(key_name))


def armature_object_for_pose_bone(scene, pbone):
    if pbone is None:
        return None
    owner = getattr(pbone, "id_data", None)
    if owner is not None and getattr(owner, "type", None) == "ARMATURE":
        return owner
    if scene is None or owner is None:
        return None
    for ob in scene.objects:
        if ob.type == "ARMATURE" and (ob.data == owner or ob == owner):
            return ob
    return None


def find_morph_bind_owner_by_pointer(want, scene=None):
    """PoseBone PropertyGroups have no path_from_id (rna_path.cc). Search bones."""
    if want is None:
        return None, None, None
    objects = scene.objects if scene is not None else bpy.data.objects
    for ob in objects:
        if getattr(ob, "type", None) != "ARMATURE" or ob.pose is None:
            continue
        for pbone in ob.pose.bones:
            binds = getattr(pbone, "torque_morph_binds", None)
            if not binds:
                continue
            for item in binds:
                try:
                    if item.as_pointer() == want:
                        return ob, pbone, item
                except Exception:
                    continue
    return None, None, None


def find_morph_bind_owner(bind, scene=None):
    if bind is None:
        return None, None, None
    try:
        want = bind.as_pointer()
    except Exception:
        want = None
    found = find_morph_bind_owner_by_pointer(want, scene)
    if found[2] is not None:
        return found
    objects = scene.objects if scene is not None else bpy.data.objects
    for ob in objects:
        if getattr(ob, "type", None) != "ARMATURE" or ob.pose is None:
            continue
        for pbone in ob.pose.bones:
            binds = getattr(pbone, "torque_morph_binds", None)
            if not binds:
                continue
            for item in binds:
                if item == bind:
                    return ob, pbone, item
    return None, None, None


def pose_bone_from_morph_bind(bind, scene=None):
    armature_ob, pbone, _item = find_morph_bind_owner(bind, scene)
    return armature_ob, pbone


def current_shape_key_value(mesh_ob, key_name):
    if mesh_ob is None or not key_name:
        return 0.0
    key = getattr(getattr(mesh_ob, "data", None), "shape_keys", None)
    if key is None or key_name not in key.key_blocks:
        return 0.0
    return float(key.key_blocks[key_name].value)


def clamp_morph_proxy_value(value):
    try:
        start = 0.0 if value is None else float(value)
    except (TypeError, ValueError):
        return 0.0
    if start < 0.0:
        return 0.0
    if start > 1.0:
        return 1.0
    return start


_MORPH_BIND_INIT = {}


def _morph_bind_pointer(bind):
    if bind is None:
        return None
    try:
        return bind.as_pointer()
    except Exception:
        return None


def capture_bone_morph_init_value(bind):
    """Read the shape key before a driver or leftover proxy can replace it."""
    if bind is None:
        return 0.0
    value = clamp_morph_proxy_value(current_shape_key_value(
        getattr(bind, "mesh", None), getattr(bind, "shape_key", "") or ""))
    want = _morph_bind_pointer(bind)
    if want is not None:
        _MORPH_BIND_INIT[want] = value
    return value


def morph_bind_init_value(bind, mesh=None, key_name=None, fallback=None):
    want = _morph_bind_pointer(bind)
    if want is not None and want in _MORPH_BIND_INIT:
        return _MORPH_BIND_INIT[want]
    if fallback is not None:
        return clamp_morph_proxy_value(fallback)
    if mesh is None and bind is not None:
        mesh = getattr(bind, "mesh", None)
    if not key_name and bind is not None:
        key_name = getattr(bind, "shape_key", "") or ""
    return clamp_morph_proxy_value(current_shape_key_value(mesh, key_name))


def forget_bone_morph_init_value(bind):
    want = _morph_bind_pointer(bind)
    if want is not None:
        _MORPH_BIND_INIT.pop(want, None)


def pose_bone_has_idprop(pbone, name):
    if pbone is None or not name:
        return False
    try:
        return name in pbone.keys()
    except Exception:
        return name in pbone


def set_bound_shape_key_values(scene, mesh, key_name, value):
    if mesh is None or not key_name:
        return
    value = clamp_morph_proxy_value(value)
    if scene is not None:
        targets = morph_proxy_target_meshes(scene, mesh, key_name)
    else:
        targets = [mesh]
    for target in targets:
        key = getattr(getattr(target, "data", None), "shape_keys", None)
        if key is None or key_name not in key.key_blocks:
            continue
        key.key_blocks[key_name].value = value


def bone_morph_property_in_use(pbone, name, skip_bind=None):
    if pbone is None or not name:
        return False
    for item in getattr(pbone, "torque_morph_binds", None) or []:
        if skip_bind is not None and item == skip_bind:
            continue
        if (getattr(item, "shape_key", "") or "") == name:
            return True
    return False


def release_bone_morph_property(pbone, name, skip_bind=None):
    if pose_bone_has_idprop(pbone, name) and not bone_morph_property_in_use(
            pbone, name, skip_bind=skip_bind):
        try:
            del pbone[name]
        except Exception:
            pass


def remove_shape_key_value_driver(mesh_ob, key_name):
    if mesh_ob is None or not key_name:
        return
    key = getattr(getattr(mesh_ob, "data", None), "shape_keys", None)
    if key is None:
        return
    path = shape_key_value_data_path(key_name)
    block = key.key_blocks.get(key_name)
    try:
        if block is not None:
            block.driver_remove("value")
    except (TypeError, ValueError, RuntimeError):
        pass
    try:
        key.driver_remove(path)
    except (TypeError, ValueError, RuntimeError):
        pass


def setup_shape_key_proxy_driver(armature_ob, bone_name, prop_name, mesh_ob, key_name):
    if (armature_ob is None or mesh_ob is None or not bone_name
            or not prop_name or not key_name or key_name == "Basis"):
        return False
    key = getattr(getattr(mesh_ob, "data", None), "shape_keys", None)
    if key is None or key_name not in key.key_blocks:
        return False
    path = shape_key_value_data_path(key_name)
    try:
        key.driver_remove(path)
    except (TypeError, ValueError, RuntimeError):
        pass
    try:
        fcurve = key.driver_add(path)
    except (TypeError, ValueError, RuntimeError):
        return False
    if isinstance(fcurve, (list, tuple)):
        fcurve = fcurve[0] if fcurve else None
    if fcurve is None or fcurve.driver is None:
        return False
    driver = fcurve.driver
    driver.type = "SCRIPTED"
    while driver.variables:
        driver.variables.remove(driver.variables[0])
    var = driver.variables.new()
    var.name = "proxy"
    var.type = "SINGLE_PROP"
    target = var.targets[0]
    target.id_type = "OBJECT"
    target.id = armature_ob
    target.data_path = bone_morph_prop_path(bone_name, prop_name)
    driver.expression = "proxy"
    mesh_ob.data.update_tag()
    armature_ob.update_tag(refresh={"OBJECT", "DATA"})
    return True


def sync_bone_morph_bind_driver(scene, pbone, bind):
    """Keep a viewport driver from the bone property onto the shape key."""
    if bind is not None and (pbone is None or armature_object_for_pose_bone(scene, pbone) is None):
        found_arm, found_bone, found_bind = find_morph_bind_owner(bind, scene)
        if found_bind is not None:
            bind = found_bind
        if found_bone is not None:
            pbone = found_bone
        armature_ob = found_arm
    else:
        armature_ob = armature_object_for_pose_bone(scene, pbone)
    start = morph_bind_init_value(bind)
    old_mesh_name = getattr(bind, "driver_mesh", "") or ""
    old_key = getattr(bind, "driver_key", "") or ""
    if old_mesh_name and old_key and scene is not None:
        old_mesh = scene.objects.get(old_mesh_name)
        if old_mesh is not None:
            for target in morph_proxy_target_meshes(scene, old_mesh, old_key):
                remove_shape_key_value_driver(target, old_key)
        else:
            remove_shape_key_value_driver(None, old_key)
        if old_key != (getattr(bind, "shape_key", "") or ""):
            set_bound_shape_key_values(scene, old_mesh, old_key, 0.0)
            release_bone_morph_property(pbone, old_key, skip_bind=bind)
    mesh = getattr(bind, "mesh", None)
    key_name = getattr(bind, "shape_key", "") or ""
    if pbone is not None and key_name:
        ensure_bone_morph_property(pbone, key_name, start, overwrite=True)
    if (scene is None or armature_ob is None or pbone is None
            or mesh is None or not key_name or key_name == "Basis"):
        if getattr(bind, "driver_mesh", "") or getattr(bind, "driver_key", ""):
            bind.driver_mesh = ""
            bind.driver_key = ""
        return
    attached = False
    for target in morph_proxy_target_meshes(scene, mesh, key_name):
        if setup_shape_key_proxy_driver(
                armature_ob, pbone.name, key_name, target, key_name):
            attached = True
    if not attached and mesh is not None:
        attached = setup_shape_key_proxy_driver(
            armature_ob, pbone.name, key_name, mesh, key_name)
    if attached:
        bind.driver_mesh = mesh.name
        bind.driver_key = key_name
        apply_bone_morph_proxies(scene)


def schedule_bone_morph_driver_sync(scene, bind):
    """Property updates cannot always write drivers; finish on the next tick."""
    if bind is None:
        return
    scene_name = getattr(scene, "name", None)
    try:
        want = bind.as_pointer()
    except Exception:
        want = None

    def _run():
        scn = bpy.data.scenes.get(scene_name) if scene_name else bpy.context.scene
        found_bind = None
        pbone = None
        if want is not None:
            _arm, pbone, found_bind = find_morph_bind_owner_by_pointer(want, scn)
            if found_bind is None:
                _arm, pbone, found_bind = find_morph_bind_owner_by_pointer(want, None)
        else:
            _arm, pbone, found_bind = find_morph_bind_owner(bind, scn)
        if found_bind is not None:
            sync_bone_morph_bind_driver(scn, pbone, found_bind)
        return None

    try:
        bpy.app.timers.register(_run, first_interval=0.0)
    except Exception:
        sync_bone_morph_bind_driver(scene, None, bind)


def clear_bone_morph_bind_driver(scene, bind, pbone=None):
    mesh = getattr(bind, "mesh", None)
    key_name = getattr(bind, "shape_key", "") or getattr(bind, "driver_key", "") or ""
    mesh_name = getattr(bind, "driver_mesh", "") or ""
    restore = morph_bind_init_value(bind, fallback=0.0)
    if mesh is None and scene is not None and mesh_name:
        mesh = scene.objects.get(mesh_name)
    if key_name and scene is not None and mesh is not None:
        for target in morph_proxy_target_meshes(scene, mesh, key_name):
            remove_shape_key_value_driver(target, key_name)
    elif key_name and mesh is not None:
        remove_shape_key_value_driver(mesh, key_name)
    set_bound_shape_key_values(scene, mesh, key_name, restore)
    if pbone is None:
        _arm, pbone = pose_bone_from_morph_bind(bind, scene)
    release_bone_morph_property(pbone, key_name, skip_bind=bind)
    forget_bone_morph_init_value(bind)
    if hasattr(bind, "driver_mesh"):
        bind.driver_mesh = ""
    if hasattr(bind, "driver_key"):
        bind.driver_key = ""


def _update_morph_prop_ui(pbone, name, start):
    try:
        ui = pbone.id_properties_ui(name)
        ui.update(
            min=0.0, max=1.0, soft_min=0.0, soft_max=1.0, default=start,
            description="Torque morph proxy")
    except (AttributeError, TypeError, KeyError):
        pass


def ensure_bone_morph_property(pbone, name, value=None, overwrite=False):
    if pbone is None or not name or name == "Basis":
        return
    start = clamp_morph_proxy_value(value)
    exists = pose_bone_has_idprop(pbone, name)
    if exists and not overwrite:
        _update_morph_prop_ui(pbone, name, start)
        return
    try:
        from rna_prop_ui import rna_idprop_ui_create
        rna_idprop_ui_create(
            pbone,
            name,
            default=start,
            min=0.0,
            max=1.0,
            soft_min=0.0,
            soft_max=1.0,
            description="Torque morph proxy",
        )
        return
    except Exception:
        pass
    pbone[name] = start
    _update_morph_prop_ui(pbone, name, start)


def iter_bone_morph_binds(scene):
    if scene is None:
        return
    for ob in scene.objects:
        if ob.type != "ARMATURE" or ob.pose is None:
            continue
        for pbone in ob.pose.bones:
            binds = getattr(pbone, "torque_morph_binds", None)
            if not binds:
                continue
            for bind in binds:
                yield ob, pbone, bind


def reset_bone_morph_proxy_values(armature):
    if armature is None or armature.pose is None:
        return
    for pbone in armature.pose.bones:
        binds = getattr(pbone, "torque_morph_binds", None)
        if not binds:
            continue
        for bind in binds:
            name = getattr(bind, "shape_key", "") or ""
            if name and name in pbone:
                pbone[name] = 0.0


def imported_morph_mesh_stem(bobj):
    raw = None
    if bobj is not None and hasattr(bobj, "get"):
        raw = bobj.get("name")
    if not raw:
        raw = getattr(bobj, "name", None)
    return (raw or "Morph").split("#", 1)[0].strip() or "Morph"


def morph_proxy_target_meshes(scene, mesh_ob, key_name):
    if scene is None or mesh_ob is None or not key_name:
        return []
    identity = mesh_ob.name.split("#", 1)[0]
    dts_name = mesh_ob.get("name") if hasattr(mesh_ob, "get") else None
    targets = []
    for ob in scene.objects:
        if ob.type != "MESH" or ob.data is None or ob.data.shape_keys is None:
            continue
        if key_name not in ob.data.shape_keys.key_blocks:
            continue
        same_hash = ob == mesh_ob or ob.name.split("#", 1)[0] == identity
        same_dts = bool(dts_name) and hasattr(ob, "get") and ob.get("name") == dts_name
        if same_hash or same_dts:
            targets.append(ob)
    return targets


def apply_bone_morph_proxies(scene):
    """Copy bone custom-prop values onto bound shape keys for this frame."""
    if scene is None:
        return
    for _armature, pbone, bind in iter_bone_morph_binds(scene):
        mesh = getattr(bind, "mesh", None)
        key_name = getattr(bind, "shape_key", "") or ""
        if mesh is None or not key_name or key_name == "Basis":
            continue
        if key_name not in pbone:
            continue
        try:
            value = float(pbone[key_name])
        except (TypeError, ValueError):
            continue
        if value < 0.0:
            value = 0.0
        elif value > 1.0:
            value = 1.0
        for target in morph_proxy_target_meshes(scene, mesh, key_name):
            target.data.shape_keys.key_blocks[key_name].value = value


def fcurves_bone_morph_keyed_in_range(action, scene, mesh_ob, frame_start, frame_end):
    if action is None or scene is None or mesh_ob is None:
        return False
    key = getattr(getattr(mesh_ob, "data", None), "shape_keys", None)
    if key is None:
        return False
    from .util import fcurves_keyframe_in_range
    identity = mesh_ob.name.split("#", 1)[0]
    paths = set()
    for _armature, pbone, bind in iter_bone_morph_binds(scene):
        name = getattr(bind, "shape_key", "") or ""
        mesh = getattr(bind, "mesh", None)
        if not name or name not in key.key_blocks or mesh is None:
            continue
        if mesh != mesh_ob and mesh.name.split("#", 1)[0] != identity:
            continue
        paths.add(bone_morph_prop_path(pbone.name, name))
    if not paths:
        return False
    for curve in iter_action_fcurves(action):
        if (curve.data_path or "") not in paths:
            continue
        if fcurves_keyframe_in_range([curve], frame_start, frame_end):
            return True
    return False


def action_shape_key_names(action):
    names = set()
    if action is None:
        return names
    for curve in iter_action_fcurves(action):
        match = SHAPE_KEY_VALUE_RE.match(curve.data_path or "")
        if match:
            names.add(match.group(1))
    return names


def action_datablock_sequence_name(action, strip_suffixes=True):
    if action is None or not getattr(action, "name", None):
        return ""
    name = action.name
    if not strip_suffixes:
        return name
    return strip_blender_suffix(str(name).split("#", 1)[0])


def shape_key_sequence_prefix(name):
    if not name or name == "Basis":
        return None
    prefix = SHAPE_KEY_SEQ_DELIM_RE.split(name, 1)[0].strip()
    return prefix or None


def action_morph_sequence_name(action):
    prefixes = set()
    for name in action_shape_key_names(action):
        prefix = shape_key_sequence_prefix(name)
        if prefix:
            prefixes.add(prefix)
    if len(prefixes) == 1:
        return next(iter(prefixes))
    return None


def action_export_sequence_name(action, bone_names=None, strip_suffixes=True):
    fallback = action_datablock_sequence_name(action, strip_suffixes)
    if action_has_pose_curves(action, bone_names):
        return fallback
    morph_name = action_morph_sequence_name(action)
    return morph_name or fallback


def reset_shape_key_values(key, keep_names=None):
    if key is None:
        return
    keep_names = keep_names or ()
    for block in key.key_blocks:
        if block.name == "Basis":
            continue
        if block.name in keep_names:
            continue
        block.value = 0.0


def bind_sequence_shape_keys(scene, action, restore):
    """Assign this Action to Key IDs it drives; zero every other shape key."""
    driven = action_shape_key_names(action)
    for ob in scene.objects:
        if ob.type != "MESH" or ob.data is None:
            continue
        key = ob.data.shape_keys
        if key is None:
            continue
        data = key.animation_data
        if data is None:
            data = key.animation_data_create()
        values = [block.value for block in key.key_blocks]
        restore.append((key, data.action, data.use_nla, values))
        data.use_nla = False
        uses_action = bool(driven.intersection(key.key_blocks.keys()))
        data.action = action if uses_action else None
        reset_shape_key_values(key, driven if uses_action else ())


def restore_sequence_shape_keys(restore):
    for key, action, use_nla, values in restore:
        data = key.animation_data
        if data is not None:
            data.action = action
            data.use_nla = use_nla
        for block, value in zip(key.key_blocks, values):
            block.value = value


def reset_scene_shape_keys(scene):
    for ob in scene.objects:
        if ob.type != "MESH" or ob.data is None:
            continue
        reset_shape_key_values(ob.data.shape_keys)


def snapshot_scene_shape_key_values(scene):
    snaps = []
    for ob in scene.objects:
        if ob.type != "MESH" or ob.data is None or ob.data.shape_keys is None:
            continue
        key = ob.data.shape_keys
        snaps.append((key, [block.value for block in key.key_blocks]))
    return snaps


def restore_shape_key_values_only(snaps):
    for key, values in snaps:
        for block, value in zip(key.key_blocks, values):
            block.value = value


def fcurves_shape_key_keyed_in_range(action, key, frame_start, frame_end):
    if action is None or key is None:
        return False
    from .util import fcurves_keyframe_in_range
    for curve in iter_action_fcurves(action):
        match = SHAPE_KEY_VALUE_RE.match(curve.data_path or "")
        if not match or match.group(1) not in key.key_blocks:
            continue
        if fcurves_keyframe_in_range([curve], frame_start, frame_end):
            return True
    return False


def object_has_armature_modifier(bobj):
    if bobj is None:
        return False
    for modifier in getattr(bobj, "modifiers", []) or []:
        if getattr(modifier, "type", None) == "ARMATURE":
            return True
    return False


def mesh_morph_is_keyed(scene, bobj, action, frame_start, frame_end):
    if bobj is None or getattr(bobj, "data", None) is None:
        return False
    key = getattr(bobj.data, "shape_keys", None)
    if key is None:
        return False
    checked = []
    if action is not None:
        checked.append(action)
    key_action = getattr(getattr(key, "animation_data", None), "action", None)
    if key_action is not None and key_action not in checked:
        checked.append(key_action)
    for act in checked:
        if fcurves_shape_key_keyed_in_range(act, key, frame_start, frame_end):
            return True
        if fcurves_bone_morph_keyed_in_range(act, scene, bobj, frame_start, frame_end):
            return True
    if action is None and scene is not None:
        for ob in getattr(scene, "objects", []) or []:
            if getattr(ob, "type", None) != "ARMATURE":
                continue
            act = getattr(getattr(ob, "animation_data", None), "action", None)
            if act is None or act in checked:
                continue
            if fcurves_bone_morph_keyed_in_range(act, scene, bobj, frame_start, frame_end):
                return True
    return False


def object_export_meshes(shape, obj):
    meshes = []
    if shape is None or obj is None:
        return meshes
    first = getattr(obj, "firstMesh", 0) or 0
    count = getattr(obj, "numMeshes", 0) or 0
    total = len(getattr(shape, "meshes", []) or [])
    for i in range(count):
        index = first + i
        if 0 <= index < total:
            meshes.append(shape.meshes[index])
    return meshes


def skin_animated_morph_reason(scene, shape, obj, action, frame_start, frame_end):
    """TGE cannot morph skins. Error when a skinned mesh has keyed morphs."""
    names = []
    seen = set()
    for mesh in object_export_meshes(shape, obj):
        if mesh.get_type() != Mesh.SkinType:
            continue
        bobj = getattr(mesh, "source_ob", None)
        if bobj is None or not object_has_armature_modifier(bobj):
            continue
        if not mesh_morph_is_keyed(scene, bobj, action, frame_start, frame_end):
            continue
        name = getattr(mesh, "morph_name", None) or getattr(bobj, "name", None) or "mesh"
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return None
    quoted = ", ".join("'{}'".format(name) for name in names)
    if len(names) == 1:
        return (
            "Mesh {} has animated morphs and an Armature modifier. "
            "TGE cannot morph skins. Remove the modifier or put the "
            "shape keys on a rigid mesh. Enable Force Export to write "
            "the skin without morph.".format(quoted))
    return (
        "Meshes {} have animated morphs and an Armature modifier. "
        "TGE cannot morph skins. Enable Force Export to write the "
        "skins without morph.".format(quoted))


def mesh_allows_morph(dmesh):
    if dmesh is None:
        return False
    if dmesh.get_type() != Mesh.StandardType:
        return False
    if dmesh.get_flags(Mesh.Billboard):
        return False
    if getattr(dmesh, "morph_ob", None) is None:
        return False
    if not getattr(dmesh, "vert_ids", None):
        return False
    return dmesh.vertsPerFrame > 0


def morph_verts_differ(left, right, eps=MORPH_VERT_EPS):
    if left is None or right is None or len(left) != len(right):
        return True
    for a, b in zip(left, right):
        if (abs(a.x - b.x) >= eps or abs(a.y - b.y) >= eps
                or abs(a.z - b.z) >= eps):
            return True
    return False


def corner_export_normal(mesh, poly, loop_index, vert):
    """Blender viewport normal for this corner (split/custom, else vertex)."""
    if poly is not None and not poly.use_smooth:
        return poly.normal
    corners = getattr(mesh, "corner_normals", None) if mesh is not None else None
    if corners:
        try:
            normal = corners[loop_index].vector
            if normal.length_squared > 1e-16:
                return normal
        except (AttributeError, IndexError, TypeError):
            pass
    loops = getattr(mesh, "loops", None) if mesh is not None else None
    if loops and 0 <= loop_index < len(loops):
        normal = getattr(loops[loop_index], "normal", None)
        if normal is not None and getattr(normal, "length_squared", 1.0) > 1e-16:
            return normal
    return vert.normal


def _triangulate_eval_mesh(mesh):
    import bmesh
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        bmesh.ops.triangulate(bm, faces=bm.faces)
        bm.to_mesh(mesh)
    finally:
        bm.free()


def _loop_poly_map(mesh):
    nloops = len(mesh.loops)
    mapping = [None] * nloops
    for poly in mesh.polygons:
        start = poly.loop_start
        for i in range(start, start + poly.loop_total):
            if 0 <= i < nloops:
                mapping[i] = poly
    return mapping


def sample_morph_positions(bobj, transform, vert_ids, depsgraph,
                           apply_modifiers=True, loop_ids=None):
    """Sample one morph frame in DTS space.

    Rest export writes the Blender corner/face normal (flat faces stay
    flat). Morph frames must use the same corner, not vert.normal —
    that average is what made animation pop to smooth shading.
    """
    if bobj is None or not vert_ids:
        return None, None
    eval_ob = bobj.evaluated_get(depsgraph) if apply_modifiers else bobj
    mesh = eval_ob.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        _triangulate_eval_mesh(mesh)
        nverts = len(mesh.vertices)
        nloops = len(mesh.loops)
        rot = transform.to_3x3()
        use_loops = loop_ids is not None and len(loop_ids) == len(vert_ids)
        loop_poly = _loop_poly_map(mesh) if use_loops else None
        positions = []
        normals = []
        for i, vid in enumerate(vert_ids):
            if vid < 0 or vid >= nverts:
                return None, None
            vert = mesh.vertices[vid]
            positions.append(transform @ vert.co.copy())
            normal = vert.normal
            if use_loops:
                lid = loop_ids[i]
                if lid < 0 or lid >= nloops:
                    return None, None
                normal = corner_export_normal(mesh, loop_poly[lid], lid, vert)
            normals.append((rot @ normal).normalized())
        return positions, normals
    finally:
        eval_ob.to_mesh_clear()


def append_mesh_morph_frame(dmesh, positions, normals):
    from .encoded_normals import encode_mesh_normals
    if len(positions) != dmesh.vertsPerFrame:
        return "Mesh '{}' morph frame has {} verts, rest has {}.".format(
            getattr(dmesh, "morph_name", "mesh"),
            len(positions), dmesh.vertsPerFrame)
    if len(dmesh.verts) + len(positions) >= TGE_INDEX_U16_LIMIT:
        return (
            "Mesh '{}' morph would exceed 65535 verts (DTS v24 index limit). "
            "Use fewer frames or a lower-poly mesh.".format(
                getattr(dmesh, "morph_name", "mesh")))
    dmesh.verts.extend(positions)
    if dmesh.normals:
        dmesh.normals.extend(normals)
    else:
        dmesh.normals = list(normals)
    dmesh.enormals.extend([0] * len(positions))
    dmesh.numFrames += 1
    encode_mesh_normals(dmesh)
    return None


def morph_mesh_can_append(dmesh, extra_frames=1, limit=None):
    if dmesh is None:
        return False
    cap = TGE_INDEX_U16_LIMIT if limit is None else int(limit)
    vpf = int(getattr(dmesh, "vertsPerFrame", 0) or 0)
    if vpf <= 0 or extra_frames <= 0:
        return False
    return len(dmesh.verts) + extra_frames * vpf < cap


def collapse_morph_positions(positions):
    if not positions:
        return []
    count = float(len(positions))
    cx = sum(float(p.x) for p in positions) / count
    cy = sum(float(p.y) for p in positions) / count
    cz = sum(float(p.z) for p in positions) / count
    point = Vector((cx, cy, cz))
    return [point.copy() for _ in positions]


MORPH_OVERFLOW_SUFFIX_RE = re.compile(r"(?:_mf[A-Z]+)+$", re.I)


def morph_overflow_name_stem(base_name):
    raw = (base_name or "mesh").split("#", 1)[0].strip() or "mesh"
    stripped = MORPH_OVERFLOW_SUFFIX_RE.sub("", raw).strip()
    return stripped or raw


def next_morph_overflow_object_name(shape, base_name):
    raw = morph_overflow_name_stem(base_name)
    existing = {name.casefold() for name in getattr(shape, "names", ()) or ()}
    index = 2
    while True:
        suffix = ""
        value = index
        while value > 0:
            value, rem = divmod(value - 1, 26)
            suffix = chr(ord("A") + rem) + suffix
        candidate = "{}_mf{}".format(raw, suffix or "A")
        if candidate.casefold() not in existing:
            return candidate
        index += 1


def _copy_mesh_topology(source):
    cloned = Mesh(source.get_type())
    cloned.type = source.type
    cloned.bounds = source.bounds
    cloned.center = source.center.copy() if hasattr(source.center, "copy") else source.center
    cloned.radius = source.radius
    cloned.numMatFrames = source.numMatFrames
    cloned.parent = getattr(source, "parent", -1)
    cloned.tverts = list(source.tverts)
    cloned.primitives = [
        Primitive(prim.firstElement, prim.numElements, prim.type)
        for prim in source.primitives]
    cloned.indices = list(source.indices)
    cloned.mindices = list(source.mindices)
    cloned.vert_ids = list(getattr(source, "vert_ids", ()) or ())
    cloned.loop_ids = list(getattr(source, "loop_ids", ()) or ())
    cloned.smooth_ids = list(getattr(source, "smooth_ids", ()) or ())
    cloned.morph_ob = getattr(source, "morph_ob", None)
    cloned.morph_transform = getattr(source, "morph_transform", None)
    cloned.morph_name = getattr(source, "morph_name", None)
    cloned.morph_apply_modifiers = getattr(source, "morph_apply_modifiers", True)
    return cloned


def clone_morph_mesh_with_frame(source, positions, normals):
    from .encoded_normals import encode_mesh_normals
    cloned = _copy_mesh_topology(source)
    cloned.vertsPerFrame = source.vertsPerFrame
    cloned.numFrames = 1
    cloned.verts = list(positions)
    cloned.normals = list(normals) if normals else []
    cloned.enormals = [0] * len(positions)
    encode_mesh_normals(cloned)
    return cloned


def _insert_default_object_state(shape, object_index, vis=1.0):
    states = getattr(shape, "objectstates", None)
    if states is None:
        return
    states.insert(object_index, ObjectState(vis, 0, 0))
    for seq in getattr(shape, "sequences", ()) or ():
        base = int(getattr(seq, "baseObjectState", 0) or 0)
        if base >= object_index:
            seq.baseObjectState = base + 1


def add_morph_overflow_object(shape, source_obj, collapse_by_mesh, real_by_mesh):
    names = getattr(shape, "names", ())
    family = getattr(source_obj, "morph_family", None) or source_obj
    family_index = getattr(family, "name", source_obj.name)
    base = names[family_index] if family_index < len(names) else "mesh"
    new_name = next_morph_overflow_object_name(shape, base)
    if len(shape.objects) + 1 > TGE_TS_SET_SIZE:
        return None, (
            "Mesh '{}' morph overflow needs another object, but TGE allows "
            "only {} objects.".format(base, TGE_TS_SET_SIZE))
    first_mesh = len(shape.meshes)
    added = 0
    for slot in range(source_obj.numMeshes):
        index = source_obj.firstMesh + slot
        if index < 0 or index >= len(shape.meshes):
            continue
        source = shape.meshes[index]
        if source.get_type() == Mesh.NullType:
            shape.meshes.append(Mesh(Mesh.NullType))
            added += 1
            continue
        collapse = collapse_by_mesh.get(source)
        real = real_by_mesh.get(source)
        if collapse is None or real is None:
            cloned = _copy_mesh_topology(source)
            cloned.vertsPerFrame = source.vertsPerFrame
            cloned.numFrames = 1
            cloned.verts = list(source.verts[:source.vertsPerFrame])
            cloned.normals = list(source.normals[:source.vertsPerFrame])
            cloned.enormals = [0] * len(cloned.verts)
            shape.meshes.append(cloned)
            added += 1
            continue
        collapse_pos, _collapse_nrm = collapse
        real_pos, real_nrm = real
        # Clump positions so a non-sequence rest pose is invisible,
        # but keep the first real pose's normals. Import builds the
        # Blender mesh from the first non-clump frame; frame-0 normals
        # from a degenerate clump are zero/NaN and bake into custom
        # split data.
        cloned = clone_morph_mesh_with_frame(source, collapse_pos, real_nrm)
        cloned.morph_name = new_name
        err = append_mesh_morph_frame(cloned, real_pos, real_nrm)
        if err:
            return None, err
        shape.meshes.append(cloned)
        added += 1
    new_obj = Object(shape.name(new_name), added, first_mesh, source_obj.node)
    new_obj.has_transparency = getattr(source_obj, "has_transparency", False)
    new_obj.vis_source = getattr(source_obj, "vis_source", None)
    new_obj.morph_family = getattr(source_obj, "morph_family", source_obj)
    new_obj.morph_collapse_frame = None
    source_index = shape.objects.index(source_obj) if source_obj in shape.objects else len(shape.objects)
    rest_vis = 1.0
    if source_index < len(getattr(shape, "objectstates", ()) or ()):
        rest_vis = shape.objectstates[source_index].vis
    shape.objects.append(new_obj)
    _insert_default_object_state(shape, len(shape.objects) - 1, rest_vis)
    if shape.subshapes:
        shape.subshapes[0].numObjects = len(shape.objects)
    return new_obj, None


def imported_morph_key_name(frame_index, users, unique_nonzero_counts):
    if frame_index <= 0:
        return None
    if len(users) == 1:
        name = users[0]
        if unique_nonzero_counts.get(name, 0) <= 1:
            return name
        return "{}_f{}".format(name, frame_index)
    if users:
        return "{}_f{}".format(users[0], frame_index)
    return "frame_{}".format(frame_index)


def mesh_frame_vert_count(dmesh):
    vpf = int(getattr(dmesh, "vertsPerFrame", 0) or 0)
    if vpf <= 0:
        return len(dmesh.verts)
    return vpf


def mesh_frame_is_collapsed(positions, eps=MORPH_VERT_EPS):
    if not positions or len(positions) < 3:
        return False
    origin = positions[0]
    for point in positions:
        if (abs(point.x - origin.x) >= eps or abs(point.y - origin.y) >= eps
                or abs(point.z - origin.z) >= eps):
            return False
    return True


def mesh_frame_slice(dmesh, frame_index):
    vpf = mesh_frame_vert_count(dmesh)
    start = max(0, int(frame_index) * vpf)
    verts = list(dmesh.verts[start:start + vpf])
    normals = []
    if dmesh.normals:
        normals = list(dmesh.normals[start:start + vpf])
    return verts, normals


def mesh_import_shading_frame(dmesh):
    """First mesh frame that is not an overflow hide-clump.

    Overflow extras store a collapsed rest so two heads do not stack
    when no sequence plays. Import must not build custom split normals
    from that degenerate frame.
    """
    vpf = mesh_frame_vert_count(dmesh)
    if vpf <= 0 or not getattr(dmesh, "verts", None):
        return 0
    nframes = int(getattr(dmesh, "numFrames", 1) or 1)
    if nframes < 1:
        nframes = max(1, len(dmesh.verts) // vpf)
    for frame in range(nframes):
        verts, _normals = mesh_frame_slice(dmesh, frame)
        if len(verts) < vpf:
            break
        if not mesh_frame_is_collapsed(verts):
            return frame
    return 0


def sequence_object_frame_indices(seq, object_index, objectstates, object_count):
    if object_index >= len(seq.frameMatters) or not seq.frameMatters[object_index]:
        return None
    rank = object_state_rank(seq, object_index, object_count)
    if rank is None:
        return None
    frames = []
    for key in range(seq.numKeyframes):
        state_index = seq.baseObjectState + rank * seq.numKeyframes + key
        if state_index < 0 or state_index >= len(objectstates):
            frames.append(0)
            continue
        frames.append(int(objectstates[state_index].frame))
    return frames


def add_imported_morph_shape_keys(bobj, dmesh, shape, obj_index):
    vpf = mesh_frame_vert_count(dmesh)
    nframes = int(getattr(dmesh, "numFrames", 1) or 1)
    if vpf <= 0 or nframes < 2:
        return {}
    if len(dmesh.verts) < nframes * vpf:
        nframes = len(dmesh.verts) // vpf
    if nframes < 2 or len(bobj.data.vertices) < vpf:
        return {}

    users = {index: [] for index in range(nframes)}
    unique_nonzero = {}
    object_count = len(shape.objects)
    for seq in shape.sequences:
        name = getattr(seq, "name", None)
        if not name and getattr(seq, "nameIndex", -1) >= 0:
            name = shape.names[seq.nameIndex]
        name = name or "seq"
        used = sequence_object_frame_indices(
            seq, obj_index, shape.objectstates, object_count)
        if not used:
            continue
        unique = set()
        for frame_index in used:
            if 0 <= frame_index < nframes:
                unique.add(frame_index)
                if name not in users[frame_index]:
                    users[frame_index].append(name)
        unique_nonzero[name] = len(unique - {0})

    if bobj.data.shape_keys is None:
        bobj.shape_key_add(name="Basis")
    keymap = {}
    used_names = set(bobj.data.shape_keys.key_blocks.keys())
    for frame_index in range(1, nframes):
        name = imported_morph_key_name(
            frame_index, users[frame_index], unique_nonzero)
        base = name
        suffix = 1
        while name in used_names:
            suffix += 1
            name = "{}.{:03d}".format(base, suffix)
        used_names.add(name)
        key_block = bobj.shape_key_add(name=name)
        start = frame_index * vpf
        count = min(vpf, len(key_block.data), len(dmesh.verts) - start)
        for vert_index in range(count):
            key_block.data[vert_index].co = dmesh.verts[start + vert_index]
        keymap[frame_index] = key_block.name
    if keymap:
        note("Imported {} morph snapshots on '{}'".format(
            len(keymap), bobj.name))
    return keymap


def action_fcurves_are_key_only(action):
    if action is None:
        return True
    for curve in iter_action_fcurves(action):
        if not SHAPE_KEY_VALUE_RE.match(curve.data_path or ""):
            return False
    return True


def action_can_assign_to_key(action):
    if action is None:
        return False
    return action_fcurves_are_key_only(action)


def imported_morph_dummy_name(stem):
    raw = (stem or "Morph").split("#", 1)[0].strip() or "Morph"
    if is_dummy_name(raw):
        return raw
    return "Dummy{}".format(raw)


def morph_snapshot_value_track(frame_ids, keymap, key_name):
    values = []
    for frame_index in frame_ids or ():
        active = keymap.get(int(frame_index)) if keymap else None
        values.append((1.0 if active == key_name else 0.0,))
    return values


def imported_mesh_morph_keymap(bobj):
    if bobj is None:
        return {}
    raw = bobj.get("torque_morph_keymap") if hasattr(bobj, "get") else None
    if raw:
        return {int(k): v for k, v in raw.items()}
    key = getattr(getattr(bobj, "data", None), "shape_keys", None)
    if key is None:
        return {}
    keymap = {}
    for block in key.key_blocks:
        if block.name != "Basis":
            keymap[len(keymap) + 1] = block.name
    return keymap


def ensure_imported_morph_dummy_bone(armature, stem, context=None):
    if armature is None or armature.type != "ARMATURE":
        return None
    bone_name = imported_morph_dummy_name(stem)
    pose = armature.pose.bones.get(bone_name) if armature.pose else None
    if pose is not None:
        return pose
    view = getattr(context, "view_layer", None) if context is not None else None
    prev = view.objects.active if view is not None else None
    mode = getattr(armature, "mode", "OBJECT")
    try:
        if view is not None:
            view.objects.active = armature
            try:
                armature.select_set(True)
            except Exception:
                pass
        if armature.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")
        edit = armature.data.edit_bones.get(bone_name)
        if edit is None:
            edit = armature.data.edit_bones.new(bone_name)
            edit.head = (0.0, 0.0, 0.0)
            edit.tail = (0.0, 0.0, 0.25)
            edit.use_deform = False
        bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if mode == "EDIT" and armature.mode != "EDIT":
            bpy.ops.object.mode_set(mode="EDIT")
        if view is not None and prev is not None:
            view.objects.active = prev
    return armature.pose.bones.get(bone_name) if armature.pose else None


def setup_imported_morph_proxy(scene, armature, pbone, mesh_ob, key_name):
    if pbone is None or mesh_ob is None or not key_name or key_name == "Basis":
        return
    ensure_bone_morph_property(pbone, key_name, 0.0)
    bind = None
    for item in getattr(pbone, "torque_morph_binds", None) or []:
        if getattr(item, "mesh", None) == mesh_ob and (
                getattr(item, "shape_key", "") or "") == key_name:
            bind = item
            break
    if bind is None and hasattr(pbone, "torque_morph_binds"):
        bind = pbone.torque_morph_binds.add()
        bind.mesh = mesh_ob
        bind.shape_key = key_name
    if bind is not None:
        bind.driver_mesh = mesh_ob.name
        bind.driver_key = key_name
    targets = morph_proxy_target_meshes(scene, mesh_ob, key_name) if scene else []
    if not targets:
        targets = [mesh_ob]
    for target in targets:
        setup_shape_key_proxy_driver(
            armature, pbone.name, key_name, target, key_name)


def imported_key_action_for_sequence(key, sequence_action, seq, start_frame, name):
    """Morph FCurves must live on a Key-rooted Action in Blender 4.3."""
    if key is None:
        return None
    if key.animation_data is None:
        key.animation_data_create()
    if action_can_assign_to_key(sequence_action):
        if assign_action_to_id(key, sequence_action):
            return sequence_action
    current = key.animation_data.action
    if current is not None and action_can_assign_to_key(current):
        return current
    action = bpy.data.actions.new(name or "KeyAction")
    action.use_fake_user = True
    if seq is not None:
        end_frame = start_frame + max(int(getattr(seq, "numKeyframes", 1) or 1) - 1, 0)
        apply_imported_action_props(
            action, seq, start_frame, end_frame, name=name)
    if key.animation_data.action is None:
        assign_action_to_id(key, action)
    return action


def imported_key_action_for_timeline(key, name):
    if key is None:
        return None
    if key.animation_data is None:
        key.animation_data_create()
    if key.animation_data.action is not None:
        return key.animation_data.action
    action = bpy.data.actions.new(name or "KeyAction")
    action.use_fake_user = True
    assign_action_to_id(key, action)
    return action


def apply_imported_morph_proxy_tracks(
        shape, object_bobjs, seq, start_frame, action, armature=None,
        scene=None, context=None):
    if not object_bobjs:
        return False
    object_count = len(shape.objects)
    wrote = False
    bound = set()
    keyed = set()
    for obj_index, bobjs in enumerate(object_bobjs):
        used = sequence_object_frame_indices(
            seq, obj_index, shape.objectstates, object_count)
        if not used:
            continue
        for bobj in bobjs:
            if any(getattr(mod, "type", None) == "ARMATURE"
                   for mod in getattr(bobj, "modifiers", []) or []):
                continue
            keymap = imported_mesh_morph_keymap(bobj)
            if not keymap:
                continue
            names = list(dict.fromkeys(keymap.values()))
            pbone = None
            if armature is not None:
                stem = imported_morph_mesh_stem(bobj)
                pbone = ensure_imported_morph_dummy_bone(armature, stem, context)
                for key_name in names:
                    token = (stem, key_name)
                    if token not in bound and pbone is not None:
                        setup_imported_morph_proxy(
                            scene, armature, pbone, bobj, key_name)
                        bound.add(token)
            if armature is not None and pbone is not None and action is not None:
                for key_name in names:
                    path = bone_morph_prop_path(pbone.name, key_name)
                    token = (id(action), path)
                    if token in keyed:
                        continue
                    write_action_array_keys(
                        action, path,
                        morph_snapshot_value_track(used, keymap, key_name),
                        start_frame, interpolation="CONSTANT",
                        owner=armature)
                    keyed.add(token)
                    wrote = True
            elif armature is None:
                key = getattr(getattr(bobj, "data", None), "shape_keys", None)
                morph_action = imported_key_action_for_timeline(
                    key, getattr(bobj, "name", None))
                if morph_action is None:
                    continue
                for key_name in names:
                    write_action_array_keys(
                        morph_action, shape_key_value_data_path(key_name),
                        morph_snapshot_value_track(used, keymap, key_name),
                        start_frame, interpolation="CONSTANT",
                        owner=key)
                    wrote = True
    return wrote


def apply_imported_morph_actions(shape, object_bobjs, actions, seq_starts,
                                sequence_names=None, armature=None,
                                scene=None, context=None):
    if scene is None and context is not None:
        scene = getattr(context, "scene", None)
    if not actions or not object_bobjs:
        return
    object_count = len(shape.objects)
    for seq_index, seq in enumerate(shape.sequences):
        if seq_index >= len(actions) or seq_index >= len(seq_starts):
            continue
        action = actions[seq_index]
        start_frame = seq_starts[seq_index]
        wrote_morph = apply_imported_morph_proxy_tracks(
            shape, object_bobjs, seq, start_frame, action,
            armature=armature, scene=scene, context=context)
        props = getattr(action, "torque_seq_props", None)
        if props is not None and (wrote_morph or any(
                i < len(seq.frameMatters) and seq.frameMatters[i]
                for i in range(object_count))):
            props.enable_morph = True
        if (wrote_morph and armature is not None
                and getattr(getattr(armature, "animation_data", None), "action", None) is None):
            assign_action_to_id(armature, action)


def apply_imported_morph_timeline(shape, object_bobjs, dummy_seq_starts,
                                 scene=None, context=None):
    if scene is None and context is not None:
        scene = getattr(context, "scene", None)
    if not dummy_seq_starts or not object_bobjs:
        return
    for seq, start_frame in dummy_seq_starts:
        apply_imported_morph_proxy_tracks(
            shape, object_bobjs, seq, start_frame, None,
            armature=None, scene=scene, context=context)


EMPTY_REST_FORCE_TRANSFORM_NAMES = frozenset(("root", "land"))


def imported_empty_rest_force_transform(seq, name):
    """Empty non-blend root/land needs dest keys for Player transitions."""
    if not name or name.lower() not in EMPTY_REST_FORCE_TRANSFORM_NAMES:
        return False
    if seq.flags & Sequence.Blend:
        return False
    if any(seq.rotationMatters) or any(seq.translationMatters):
        return False
    return True


def dts_item_name(shape, item):
    names = getattr(shape, "names", None) or ()
    raw = getattr(item, "name", None)
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, int) and 0 <= raw < len(names):
        return names[raw]
    name_index = getattr(item, "nameIndex", None)
    if isinstance(name_index, int) and 0 <= name_index < len(names):
        return names[name_index]
    return ""


def dts_object_node_index(obj):
    node = getattr(obj, "node", None)
    if node is None:
        node = getattr(obj, "nodeIndex", -1)
    try:
        return int(node)
    except (TypeError, ValueError):
        return -1


def dts_node_parent_index(node):
    parent = getattr(node, "parent", None)
    if parent is None:
        parent = getattr(node, "parentIndex", -1)
    try:
        return int(parent)
    except (TypeError, ValueError):
        return -1


def node_has_child_nodes(shape, node_index):
    for node in getattr(shape, "nodes", ()) or ():
        if dts_node_parent_index(node) == node_index:
            return True
    return False


def node_mesh_object_names(shape, node_index):
    names = []
    for obj in getattr(shape, "objects", ()) or ():
        if dts_object_node_index(obj) == node_index:
            name = dts_item_name(shape, obj)
            if name:
                names.append(name)
    return names


def node_name_matches_mesh_parent(node_name, object_names):
    if not node_name or not object_names:
        return False
    wanted = {name.casefold() for name in object_names if name}
    if node_name.casefold() in wanted:
        return True
    base, _size = chop_trailing_lod(node_name)
    return bool(base) and base.casefold() in wanted


def _matters_flag(matters, node_index):
    if not matters or node_index < 0 or node_index >= len(matters):
        return False
    return bool(matters[node_index])


def sequence_is_rest_dest_clip(seq, name):
    """Stock-empty or dest-key-filled non-blend root/land is not unique motion."""
    if imported_empty_rest_force_transform(seq, name):
        return True
    if not name or name.lower() not in EMPTY_REST_FORCE_TRANSFORM_NAMES:
        return False
    if seq.flags & Sequence.Blend:
        return False
    rot = list(getattr(seq, "rotationMatters", ()) or ())
    trans = list(getattr(seq, "translationMatters", ()) or ())
    if not rot and not trans:
        return True
    return bool(rot) and bool(trans) and all(rot) and all(trans)


def node_has_unique_transform_animation(shape, node_index):
    for seq in getattr(shape, "sequences", ()) or ():
        name = dts_item_name(shape, seq)
        if sequence_is_rest_dest_clip(seq, name):
            continue
        if (_matters_flag(getattr(seq, "rotationMatters", None), node_index)
                or _matters_flag(getattr(seq, "translationMatters", None), node_index)
                or _matters_flag(getattr(seq, "scaleMatters", None), node_index)):
            return True
    return False


def _is_soft_unused_mesh_parent(shape, node_index, ignore_animation=False):
    nodes = getattr(shape, "nodes", ()) or ()
    if node_index < 0 or node_index >= len(nodes):
        return False
    if dts_node_parent_index(nodes[node_index]) < 0:
        return False
    object_names = node_mesh_object_names(shape, node_index)
    if not object_names:
        return False
    if not node_name_matches_mesh_parent(dts_item_name(shape, nodes[node_index]), object_names):
        return False
    if not ignore_animation and node_has_unique_transform_animation(shape, node_index):
        return False
    return True


def unused_mesh_parent_nodes(shape, ignore_animation=False):
    nodes = getattr(shape, "nodes", ()) or ()
    soft = {i for i in range(len(nodes))
            if _is_soft_unused_mesh_parent(shape, i, ignore_animation)}
    collapsed = set()
    for index in soft:
        children = [
            i for i, node in enumerate(nodes)
            if dts_node_parent_index(node) == index
        ]
        if any(child not in soft for child in children):
            continue
        collapsed.add(index)
    return collapsed


def kept_node_parent(shape, node_index, collapsed):
    nodes = getattr(shape, "nodes", ()) or ()
    if node_index < 0 or node_index >= len(nodes):
        return -1
    parent = dts_node_parent_index(nodes[node_index])
    seen = set()
    while parent >= 0 and parent in collapsed:
        if parent in seen:
            return -1
        seen.add(parent)
        if parent >= len(nodes):
            return -1
        parent = dts_node_parent_index(nodes[parent])
    return parent


def blender_mesh_match_names(ob):
    raw = (getattr(ob, "name", "") or "").split("#", 1)[0]
    names = {raw}
    stripped = re_blender_suffix.sub("", raw)
    if stripped:
        names.add(stripped)
    base, _size = chop_trailing_lod(stripped or raw)
    if base:
        names.add(base)
    return {name.casefold() for name in names if name}


def _mesh_has_armature_modifier(ob):
    for modifier in getattr(ob, "modifiers", []) or []:
        if getattr(modifier, "type", "") == "ARMATURE":
            return True
    return False


def action_is_force_transform_clip(action):
    props = getattr(action, "torque_seq_props", None)
    return bool(props is not None and getattr(props, "force_transform", False))


def bone_has_unique_scene_animation(bone_name):
    for action in getattr(bpy.data, "actions", []) or []:
        if action_is_force_transform_clip(action):
            continue
        if action_has_pose_curves(action, {bone_name}):
            return True
    return False


def _action_has_any_curves(action):
    if action is None:
        return False
    try:
        from .action_compat import action_has_fcurves
        if action_has_fcurves(action):
            return True
    except ImportError:
        pass
    return bool(getattr(action, "fcurves", None))


def empty_has_unique_scene_animation(ob):
    data = getattr(ob, "animation_data", None)
    if data is None:
        return False
    actions = []
    if getattr(data, "action", None) is not None:
        actions.append(data.action)
    for track in getattr(data, "nla_tracks", []) or []:
        for strip in getattr(track, "strips", []) or []:
            if getattr(strip, "action", None) is not None:
                actions.append(strip.action)
    for action in actions:
        if action_is_force_transform_clip(action):
            continue
        if _action_has_any_curves(action):
            return True
    return False


def _scene_meshes_on_bone(scene, armature, bone_name):
    meshes = []
    for ob in scene.objects:
        if ob.type != "MESH" or _mesh_has_armature_modifier(ob):
            continue
        if is_dummy_name(ob.name) or is_dummy_name(object_export_name(ob)):
            continue
        if ob.parent != armature or ob.parent_type != "BONE":
            continue
        if ob.parent_bone != bone_name:
            continue
        meshes.append(ob)
    return meshes


def _is_soft_unused_scene_bone(scene, armature, bone, ignore_animation=False):
    if bone.parent is None:
        return False, []
    meshes = _scene_meshes_on_bone(scene, armature, bone.name)
    if not meshes:
        return False, []
    mesh_names = set()
    for mesh in meshes:
        mesh_names.update(blender_mesh_match_names(mesh))
    if not node_name_matches_mesh_parent(bone.name, mesh_names):
        return False, []
    if not ignore_animation and bone_has_unique_scene_animation(bone.name):
        return False, []
    return True, meshes


def scene_unused_mesh_parent_bones(scene, armature, ignore_animation=False):
    soft = {}
    for bone in armature.data.bones:
        ok, meshes = _is_soft_unused_scene_bone(
            scene, armature, bone, ignore_animation)
        if ok:
            soft[bone.name] = (bone, meshes)
    victims = []
    for bone_name, (bone, meshes) in soft.items():
        if any(child.name not in soft for child in bone.children):
            continue
        ancestor = bone.parent
        while ancestor is not None and ancestor.name in soft:
            ancestor = ancestor.parent
        if ancestor is None:
            continue
        victims.append((bone_name, ancestor.name, meshes))
    return victims


def _scene_meshes_on_empty(scene, empty):
    meshes = []
    for ob in scene.objects:
        if ob.type != "MESH" or _mesh_has_armature_modifier(ob):
            continue
        if ob.parent != empty:
            continue
        meshes.append(ob)
    return meshes


def scene_unused_mesh_parent_empties(scene, ignore_animation=False):
    soft = {}
    for empty in scene.objects:
        if empty.type != "EMPTY":
            continue
        if empty.parent is None or empty.parent.type != "EMPTY":
            continue
        meshes = _scene_meshes_on_empty(scene, empty)
        if not meshes:
            continue
        mesh_names = set()
        for mesh in meshes:
            mesh_names.update(blender_mesh_match_names(mesh))
        if not node_name_matches_mesh_parent(empty.name, mesh_names):
            continue
        if not ignore_animation and empty_has_unique_scene_animation(empty):
            continue
        soft[empty] = meshes
    victims = []
    for empty, meshes in soft.items():
        child_empties = [child for child in empty.children if child.type == "EMPTY"]
        if any(child not in soft for child in child_empties):
            continue
        ancestor = empty.parent
        while ancestor is not None and ancestor in soft:
            ancestor = ancestor.parent
        if ancestor is None or ancestor.type != "EMPTY":
            continue
        victims.append((empty, ancestor, meshes))
    return victims


def _reparent_mesh_keep_world(mesh, parent, parent_type="OBJECT", parent_bone=""):
    world = mesh.matrix_world.copy()
    mesh.parent = parent
    mesh.parent_type = parent_type
    mesh.parent_bone = parent_bone if parent_type == "BONE" else ""
    mesh.matrix_world = world


def collapse_unused_mesh_parents_in_scene(context, ignore_animation=False):
    scene = context.scene
    collapsed = []
    view = context.view_layer
    prev_active = view.objects.active
    prev_mode = getattr(context, "mode", "OBJECT")

    for armature in [ob for ob in scene.objects if ob.type == "ARMATURE"]:
        victims = scene_unused_mesh_parent_bones(
            scene, armature, ignore_animation)
        if not victims:
            continue
        for _bone_name, parent_name, meshes in victims:
            for mesh in meshes:
                _reparent_mesh_keep_world(
                    mesh, armature, parent_type="BONE", parent_bone=parent_name)
        view.objects.active = armature
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = armature.data.edit_bones
        for bone_name, _parent_name, _meshes in victims:
            bone = edit_bones.get(bone_name)
            if bone is not None:
                edit_bones.remove(bone)
                collapsed.append(bone_name)
        bpy.ops.object.mode_set(mode="OBJECT")

    for empty, parent, meshes in scene_unused_mesh_parent_empties(
            scene, ignore_animation):
        for mesh in meshes:
            _reparent_mesh_keep_world(mesh, parent)
        collapsed.append(empty.name)
        bpy.data.objects.remove(empty, do_unlink=True)

    if prev_active is not None:
        try:
            view.objects.active = prev_active
        except ReferenceError:
            pass
    if prev_mode and prev_mode != "OBJECT":
        try:
            bpy.ops.object.mode_set(mode="OBJECT")
        except RuntimeError:
            pass
    return collapsed


def apply_imported_action_props(action, seq, frame_start, frame_end, triggers=None,
                               source="DTS", name=None):
    action.use_fake_user = True
    action.use_frame_range = True
    action.frame_start = frame_start
    action.frame_end = frame_end
    action.use_cyclic = bool(seq.flags & Sequence.Cyclic)
    props = getattr(action, "torque_seq_props", None)
    if props is None:
        return
    props.export = True
    kind = (source or EXPORT_KIND_DTS).upper()
    # DSQ clips stay out of the DTS unless the user checks them later.
    props.export_dts = kind == EXPORT_KIND_DTS
    props.export_dsq = True
    props.blend = bool(seq.flags & Sequence.Blend)
    # DTS/DSQ store the Blend flag and deltas only. Import applies those
    # onto rest, so 0 (rest / outside reference marker) is the right value.
    props.blend_ref_frame = 0
    props.priority = int(seq.priority)
    props.override_duration = False
    props.duration = 0.0
    props.frame_rate = imported_sequence_frame_rate(seq)
    seq_name = name or getattr(seq, "name", None) or action.name
    props.force_transform = imported_empty_rest_force_transform(seq, seq_name)
    if props.force_transform:
        note("Sequence '{}' has no transform keys; Force Transform enabled. "
             "Uncheck it to export a stock-empty clip.".format(seq_name))
    if triggers:
        for state, frame in triggers:
            item = props.triggers.add()
            item.state = int(state)
            item.frame = int(frame)


def write_action_array_keys(action, data_path, values, start_frame,
                            interpolation="LINEAR", owner=None):
    if not values:
        return
    count = len(values[0])
    curves = action_fcurves_array(action, data_path, count, owner=owner)
    for frame_index, array in enumerate(values):
        for index, curve in enumerate(curves):
            if curve is None:
                continue
            curve.keyframe_points.add(1)
            key = curve.keyframe_points[-1]
            key.interpolation = interpolation
            key.co = (start_frame + frame_index, array[index])


def compose_local_matrix(translation, rotation, scale=None):
    return torque_local_matrix(translation, rotation, scale, None)


def arbitrary_scale_stretch(scale, scale_rot):
    # TGE TSScale: transform = rot * scale * inv(rot)
    rotation = scale_rot.to_matrix().to_4x4()
    stretch = Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))
    return rotation @ stretch @ rotation.inverted()


def torque_local_matrix(translation, rotation, scale=None, scale_rot=None):
    # TGE local node: T * Rnode * (Rscale * S * Rscale^-1)
    mat = Matrix.Translation(translation) @ rotation.to_matrix().to_4x4()
    if scale is None:
        return mat
    if scale_rot is None:
        return mat @ Matrix.Diagonal((scale[0], scale[1], scale[2], 1.0))
    return mat @ arbitrary_scale_stretch(scale, scale_rot)


def decompose_trs_error(mat):
    loc, rot, scl = mat.decompose()
    recomposed = (
        Matrix.Translation(loc)
        @ rot.to_matrix().to_4x4()
        @ Matrix.Diagonal((scl.x, scl.y, scl.z, 1.0)))
    error = 0.0
    for row in range(4):
        for col in range(4):
            error = max(error, abs(mat[row][col] - recomposed[row][col]))
    return loc, rot, scl, error


def sample_sequence_node_local(
        seq, frame_index, default_t, default_r,
        translations, rotations, uniform_scales, aligned_scales,
        trans_rank, rot_rank, scale_rank, is_blend=False,
        arbitrary_factors=None, arbitrary_rots=None):
    translation = default_t.copy()
    rotation = default_r.copy()
    scale = Vector((1.0, 1.0, 1.0))
    scale_rot = None
    if trans_rank is not None:
        translation = translations[
            seq.baseTranslation + trans_rank * seq.numKeyframes + frame_index].copy()
        if is_blend:
            translation = default_t + translation
    if rot_rank is not None:
        rotation = rotations[
            seq.baseRotation + rot_rank * seq.numKeyframes + frame_index].copy()
        if is_blend:
            rotation = default_r @ rotation
    if scale_rank is not None:
        index = seq.baseScale + scale_rank * seq.numKeyframes + frame_index
        if seq.flags & Sequence.UniformScale:
            value = uniform_scales[index]
            scale = Vector((value, value, value))
        elif seq.flags & Sequence.AlignedScale:
            scale = aligned_scales[index].copy()
        elif seq.flags & Sequence.ArbitraryScale:
            if (arbitrary_factors is None or arbitrary_rots is None
                    or index >= len(arbitrary_factors) or index >= len(arbitrary_rots)):
                note("Warning: Arbitrary scale data missing at index {}".format(index))
            else:
                scale = Vector(arbitrary_factors[index])
                scale_rot = Quaternion(arbitrary_rots[index])
    return torque_local_matrix(translation, rotation, scale, scale_rot)


def import_empty_arbitrary_scale(
        seq, seq_name, frame_start, step, nodes, node_defaults,
        nodes_translation, nodes_rotation, nodes_scale,
        translations, rotations, arbitrary_factors, arbitrary_rots,
        is_blend=False, reference_frame=None, evaluate_all=None):
    trans_rank_of = {node: i for i, node in enumerate(nodes_translation)}
    rot_rank_of = {node: i for i, node in enumerate(nodes_rotation)}
    scale_rank_of = {node: i for i, node in enumerate(nodes_scale)}
    warned = set()
    baked = set()
    for node in nodes_scale:
        default_t, default_r, ob = node_defaults[node]
        if is_blend:
            if reference_frame is None or evaluate_all is None:
                continue
            default_t = Vector(evaluate_all(ob_location_curves(ob), reference_frame))
            _mode, rot_curves = ob_rotation_curves(ob)
            default_r = Quaternion(evaluate_all(rot_curves, reference_frame))
        trans_rank = trans_rank_of.get(node)
        rot_rank = rot_rank_of.get(node)
        scale_rank = scale_rank_of[node]
        baked.add(node)
        node_name = getattr(ob, "name", str(node))
        for frame_index in range(seq.numKeyframes):
            mat = sample_sequence_node_local(
                seq, frame_index, default_t, default_r,
                translations, rotations, None, None,
                trans_rank, rot_rank, scale_rank, is_blend,
                arbitrary_factors, arbitrary_rots)
            loc, rot, scl, error = decompose_trs_error(mat)
            if error > ARBITRARY_SHEAR_EPS:
                warn_arbitrary_shear(
                    warned, seq_name, node_name, frame_index, error)
            key_object_trs(ob, loc, rot, scl, frame_start + frame_index * step)
    return baked


def key_object_trs(ob, loc, rot, scl, frame):
    loc_curves = ob_location_curves(ob)
    for curve in loc_curves:
        curve.keyframe_points.add(1)
        key = curve.keyframe_points[-1]
        key.interpolation = "LINEAR"
        key.co = (frame, loc[curve.array_index])

    mode, rot_curves = ob_rotation_curves(ob)
    keyed_rot = rot
    if mode == "AXIS_ANGLE":
        axis, angle = rot.to_axis_angle()
        keyed_rot = (angle, axis.x, axis.y, axis.z)
    elif mode != "QUATERNION":
        keyed_rot = rot.to_euler(mode)
    for curve in rot_curves:
        curve.keyframe_points.add(1)
        key = curve.keyframe_points[-1]
        key.interpolation = "LINEAR"
        key.co = (frame, keyed_rot[curve.array_index])

    scale_curves = ob_scale_curves(ob)
    for curve in scale_curves:
        curve.keyframe_points.add(1)
        key = curve.keyframe_points[-1]
        key.interpolation = "LINEAR"
        key.co = (frame, scl[curve.array_index])


ARBITRARY_SHEAR_EPS = 1e-4


def warn_arbitrary_shear(warned, seq_name, node_name, frame_index, error):
    key = (seq_name, node_name)
    if key in warned:
        return
    warned.add(key)
    note("Warning: Arbitrary scale on '{}' node '{}' frame {} is not a pure TRS "
          "(error {:.6g}); keyed best-fit location/rotation/scale".format(
              seq_name, node_name, frame_index, error))


def import_armature_sequences(
        armature, sequences, sequence_names, node_names,
        translations, rotations, uniform_scales, aligned_scales,
        defaults, trigger_lists=None, scene=None,
        sequence_mode="ACTIONS", source_triggers=None,
        arbitrary_scale_factors=None, arbitrary_scale_rots=None,
        timeline_expand=False, source="DTS", collection=None):
    if armature.animation_data is None:
        armature.animation_data_create()
    first_action = None
    imported_actions = []
    seq_starts = []
    marker_mode = (sequence_mode or "ACTIONS").upper() == "MARKERS"
    shared_action = _oneshot_armature_action(armature) if marker_mode else None
    start_frame = SEQUENCE_START_FRAME
    if marker_mode and scene is not None:
        start_frame = next_marker_layout_frame(scene)
    sequences_text = []
    for seq_index, seq in enumerate(sequences):
        name = sequence_names[seq_index]
        if marker_mode:
            action = shared_action
        else:
            action = bpy.data.actions.new(name)
            action.use_fake_user = True
            assign_action_to_id(armature, action)
        assign_action_collection(action, collection)
        imported_actions.append(action)
        seq_starts.append(start_frame)
        if first_action is None:
            first_action = action
        is_blend = bool(seq.flags & Sequence.Blend)
        rot_nodes = [node_names[i] for i, flag in enumerate(seq.rotationMatters)
                     if flag and i < len(node_names) and node_names[i]]
        trans_nodes = [node_names[i] for i, flag in enumerate(seq.translationMatters)
                       if flag and i < len(node_names) and node_names[i]]
        scale_nodes = [node_names[i] for i, flag in enumerate(seq.scaleMatters)
                       if flag and i < len(node_names) and node_names[i]]

        involved = []
        for node_name in node_names:
            if not node_name:
                continue
            if node_name in rot_nodes or node_name in trans_nodes or node_name in scale_nodes:
                involved.append(node_name)

        bake_arbitrary = bool(seq.flags & Sequence.ArbitraryScale)
        shear_warned = set()
        for node_name in involved:
            bone = armature.data.bones.get(node_name)
            pose = armature.pose.bones.get(node_name)
            if bone is None or pose is None:
                note("Warning: Sequence '{}' node '{}' has no pose bone".format(name, node_name))
                continue
            pose.rotation_mode = "QUATERNION"
            index = node_names.index(node_name)
            default_t, default_r = defaults[index]
            rest_local = compose_local_matrix(default_t, default_r)
            loc_keys = []
            rot_keys = []
            scale_keys = []
            trans_rank = trans_nodes.index(node_name) if node_name in trans_nodes else None
            rot_rank = rot_nodes.index(node_name) if node_name in rot_nodes else None
            scale_rank = scale_nodes.index(node_name) if node_name in scale_nodes else None
            bake_node = bake_arbitrary and scale_rank is not None
            for frame_index in range(seq.numKeyframes):
                anim_local = sample_sequence_node_local(
                    seq, frame_index, default_t, default_r,
                    translations, rotations, uniform_scales, aligned_scales,
                    trans_rank, rot_rank, scale_rank, is_blend,
                    arbitrary_scale_factors, arbitrary_scale_rots)
                if bake_node:
                    _loc, _rot, _scl, error = decompose_trs_error(anim_local)
                    if error > ARBITRARY_SHEAR_EPS:
                        warn_arbitrary_shear(
                            shear_warned, name, node_name, frame_index, error)
                loc, quat, scl = pose_basis_from_local(rest_local, anim_local).decompose()
                if bake_node or trans_rank is not None:
                    loc_keys.append((loc.x, loc.y, loc.z))
                if bake_node or rot_rank is not None:
                    rot_keys.append((quat.w, quat.x, quat.y, quat.z))
                if bake_node or scale_rank is not None:
                    scale_keys.append((scl.x, scl.y, scl.z))
            if loc_keys:
                write_action_array_keys(
                    action, pose_bone_data_path(node_name, "location"), loc_keys,
                    start_frame, owner=armature)
            if rot_keys:
                write_action_array_keys(
                    action, pose_bone_data_path(node_name, "rotation_quaternion"),
                    rot_keys, start_frame, owner=armature)
            if scale_keys:
                write_action_array_keys(
                    action, pose_bone_data_path(node_name, "scale"), scale_keys,
                    start_frame, owner=armature)

        end_frame = start_frame + max(seq.numKeyframes - 1, 0)
        trigger_rows = []
        if source_triggers is not None:
            trigger_rows = imported_sequence_triggers(seq, source_triggers, start_frame)
        elif trigger_lists and seq_index < len(trigger_lists):
            trigger_rows = trigger_lists[seq_index]
        if marker_mode:
            if scene is not None:
                write_sequence_markers(scene, name, start_frame, end_frame, trigger_rows)
            sequences_text.append(sequence_flags_line(name, seq))
            start_frame = end_frame + 30
        else:
            apply_imported_action_props(
                action, seq, start_frame, end_frame, trigger_rows,
                source=source, name=name)
    if first_action is not None and not action_fcurves_are_key_only(first_action):
        assign_action_to_id(armature, first_action)
    if marker_mode:
        append_sequences_text(sequences_text)
        if scene is not None and seq_starts:
            scene.frame_start = min(seq_starts)
            scene.frame_end = start_frame - 30 if start_frame > 30 else start_frame
            scene.frame_current = scene.frame_start
    elif scene is not None:
        fit_scene_timeline_to_actions(scene, imported_actions, expand=timeline_expand)
    return imported_actions, seq_starts


def is_bounds_name(name):
    return blender_export_base_name(name).lower() == "bounds"


def pose_bone_data_path(bone_name, suffix):
    return 'pose.bones["{}"].{}'.format(bone_name, suffix)


def pose_bone_local_matrix(armature, bone):
    pose = armature.pose.bones.get(bone.name)
    if pose is None:
        mat = bone.matrix_local
        if bone.parent:
            mat = bone.parent.matrix_local.inverted() @ mat
        return mat
    mat = pose.matrix.copy()
    if bone.parent:
        parent = armature.pose.bones.get(bone.parent.name)
        if parent is not None:
            mat = parent.matrix.inverted() @ mat
    return mat


def zap_scale_matrix(mat):
    """Max zapScale: keep translation + rotation, drop scale."""
    location, rotation, _ = mat.decompose()
    return Matrix.Translation(location) @ rotation.to_matrix().to_4x4()


def rest_world_has_scale(mat, epsilon=0.01):
    if mat is None:
        return False
    _loc, _rot, scale = mat.decompose()
    return (
        abs(scale.x * scale.x - 1.0) > epsilon
        or abs(scale.y * scale.y - 1.0) > epsilon
        or abs(scale.z * scale.z - 1.0) > epsilon)


def rest_inv_scale_matrix(rest_world):
    # Max row-vector: WT(t) = inv(Tscale(0)) * W(t). Blender is T @ R @ S, so
    # strip rest scale on the right: W(t) @ inv(S0). At rest that is zap(W).
    if rest_world is None:
        return Matrix.Identity(4)
    _loc, _rot, scale = rest_world.decompose()
    sx = scale.x if abs(scale.x) > 1e-8 else 1.0
    sy = scale.y if abs(scale.y) > 1e-8 else 1.0
    sz = scale.z if abs(scale.z) > 1e-8 else 1.0
    return Matrix.Diagonal((1.0 / sx, 1.0 / sy, 1.0 / sz, 1.0))


def safe_invert_matrix(mat):
    """Invert, or drop scale and retry. A node scaled to 0 (2.79 hide)
    has no inverse; TGE still inherits that scale from the local TRS."""
    try:
        return mat.inverted()
    except ValueError:
        try:
            return zap_scale_matrix(mat).inverted()
        except ValueError:
            return Matrix.Identity(4)


def folded_local_matrix(child_world, parent_world=None):
    child_z = zap_scale_matrix(child_world)
    if parent_world is None:
        return child_z
    return safe_invert_matrix(zap_scale_matrix(parent_world)) @ child_z


def folded_local_at_time(child_now, child_rest, parent_now=None, parent_rest=None):
    child_u = child_now @ rest_inv_scale_matrix(child_rest)
    if parent_now is None:
        return child_u
    parent_u = parent_now @ rest_inv_scale_matrix(parent_rest)
    return safe_invert_matrix(parent_u) @ child_u


def blender_object_local_matrix(ob):
    """2.79/2.81: ob.matrix_local.decompose(). Fall back to basis if
    Blender cannot form a parent-relative matrix."""
    if ob is None:
        return Matrix.Identity(4)
    try:
        return ob.matrix_local.copy()
    except ValueError:
        return ob.matrix_basis.copy()


def armature_export_world_matrix(armature):
    if armature is None:
        return Matrix.Identity(4)
    world = getattr(armature, "matrix_world", None)
    if world is None:
        return Matrix.Identity(4)
    return world.copy()


def bone_export_world_matrix(armature, bone, posed=True):
    """Max GetNodeTM: armature object world times bone pose or rest."""
    arm_world = armature_export_world_matrix(armature)
    if bone is None:
        return arm_world
    if posed and armature is not None:
        pose_data = getattr(armature, "pose", None)
        bones = getattr(pose_data, "bones", None) if pose_data is not None else None
        pose = bones.get(bone.name) if bones is not None else None
        if pose is not None:
            return arm_world @ pose.matrix
    rest = getattr(bone, "matrix_local", None)
    if rest is None:
        return arm_world
    return arm_world @ rest


def armature_object_delta(armature):
    """Object-mode loc/rot/scale vs identity. Scale is not written on
    nodes; it is carried in parent-relative translations."""
    world = armature_export_world_matrix(armature)
    loc, rot, scale = world.decompose()
    has_loc = loc.length > 1e-5
    has_scale = (
        abs(scale.x - 1.0) > 1e-4
        or abs(scale.y - 1.0) > 1e-4
        or abs(scale.z - 1.0) > 1e-4)
    has_rot = False
    if hasattr(rot, "rotation_difference"):
        try:
            has_rot = rot.rotation_difference(Quaternion((1.0, 0.0, 0.0, 0.0))).angle > 1e-5
        except (AttributeError, TypeError, ValueError):
            has_rot = False
    return has_loc, has_rot, has_scale


def apply_armature_object_track_matters(seq, extra, items):
    """Keep object-mode armature TM on sequence tracks after F-Curve detect."""
    if extra is not None and extra.get("enable_transform") is False:
        return
    for index, armature, bone in items:
        if armature is None or bone is None:
            continue
        if index < 0:
            continue
        has_loc, has_rot, has_scale = armature_object_delta(armature)
        parented = bool(getattr(bone, "parent", None))
        if has_rot and not parented and index < len(seq.rotationMatters):
            seq.rotationMatters[index] = True
        if has_loc and not parented and index < len(seq.translationMatters):
            seq.translationMatters[index] = True
        if has_scale and index < len(seq.translationMatters):
            seq.translationMatters[index] = True


def dsq_entry_needs_object_tm_sample(entry):
    if entry is None or entry.get("kind") != "bone":
        return False
    has_loc, has_rot, has_scale = armature_object_delta(entry["armature"])
    if has_scale:
        return True
    if (has_loc or has_rot) and not getattr(entry["bone"], "parent", None):
        return True
    return False


def bone_export_local_matrix(armature, bone, posed=True):
    """Parent-relative bone local from world TMs.

    Rest (posed=False) zaps scale so DTS nodes stay unscaled; object
    scale stays in translations. Posed samples strip rest/object scale
    but keep animated pose scale (UniformScale / AlignedScale).
    """
    if bone is None:
        return Matrix.Identity(4)
    child_now = bone_export_world_matrix(armature, bone, posed=posed)
    parent = getattr(bone, "parent", None)
    if not posed:
        if parent is None:
            return zap_scale_matrix(child_now)
        parent_now = bone_export_world_matrix(armature, parent, posed=False)
        return folded_local_matrix(child_now, parent_now)
    child_rest = bone_export_world_matrix(armature, bone, posed=False)
    if parent is None:
        return folded_local_at_time(child_now, child_rest)
    parent_now = bone_export_world_matrix(armature, parent, posed=True)
    parent_rest = bone_export_world_matrix(armature, parent, posed=False)
    return folded_local_at_time(child_now, child_rest, parent_now, parent_rest)


def node_export_local_matrix(node, posed=True):
    """Sample the node the way 2.79/2.81 did: local TRS, not inv(parent)."""
    stored = getattr(node, "rest_local", None)
    if not posed and stored is not None:
        return stored.copy()
    if export_node_is_bone(node):
        return bone_export_local_matrix(node.armature, node.bl_ob, posed=posed)
    bl_ob = getattr(node, "bl_ob", None)
    if bl_ob is not None and hasattr(bl_ob, "matrix_local"):
        return blender_object_local_matrix(bl_ob)
    return node_space_matrix(node, posed=posed)


def node_space_matrix(node, posed=True):
    """Rest or current matrix in the node's export space (armature or world)."""
    if export_node_is_bone(node):
        return bone_export_world_matrix(node.armature, node.bl_ob, posed=posed)
    bl_ob = getattr(node, "bl_ob", None)
    if bl_ob is not None and hasattr(bl_ob, "matrix_world"):
        return bl_ob.matrix_world.copy()
    rest = getattr(node, "rest_world", None)
    if rest is not None:
        return rest.copy()
    return Matrix.Identity(4)


def bind_space_mesh_matrix(mesh_in_shape, posed, bind):
    """Undo live node motion so rest verts stay in bind space."""
    if posed is None or bind is None:
        return mesh_in_shape
    try:
        return bind @ posed.inverted() @ mesh_in_shape
    except (ValueError, AttributeError):
        return mesh_in_shape


def mesh_object_offset(bobj, node):
    """Max computeObjectOffset: inv(zap(nodeRest)) @ meshBind, same space.

    mesh_world follows the current bone/empty pose. Using that as the
    object offset bakes the live slide into every rest vert, so the
    mesh sits beside a correctly centered node. Put the mesh back into
    bind space first; a mid-timeline export must match frame 0.
    """
    mesh_world = bobj.matrix_world
    mesh_in_shape = mesh_world.copy()
    if node is None:
        return mesh_in_shape
    rest = getattr(node, "rest_world", None)
    if rest is None:
        rest = getattr(node, "matrix_world", None)
    if rest is None:
        return mesh_in_shape
    mesh_in_shape = bind_space_mesh_matrix(
        mesh_in_shape,
        node_space_matrix(node, posed=True),
        node_space_matrix(node, posed=False))
    return zap_scale_matrix(rest).inverted() @ mesh_in_shape


def delta_transform(mat_start, mat_now):
    delta = zap_scale_matrix(mat_start).inverted() @ zap_scale_matrix(mat_now)
    location, rotation, _ = delta.decompose()
    return location, rotation


def transforms_differ(samples, eps=0.00001):
    if len(samples) < 2:
        return False
    first_loc, first_rot = samples[0]
    for location, rotation in samples[1:]:
        if (first_loc - location).length > eps:
            return True
        if first_rot.rotation_difference(rotation).angle > eps:
            return True
    return False


def append_ground_frames(seq, translations, rotations, samples, ignore_ground=False):
    seq.firstGroundFrame = len(translations)
    seq.numGroundFrames = 0
    if ignore_ground or not transforms_differ(samples):
        return
    seq.flags |= Sequence.MakePath
    for location, rotation in samples[1:]:
        start_loc, start_rot = samples[0]
        delta_loc, delta_rot = delta_transform(
            Matrix.Translation(start_loc) @ start_rot.to_matrix().to_4x4(),
            Matrix.Translation(location) @ rotation.to_matrix().to_4x4())
        translations.append(delta_loc)
        rotations.append(delta_rot)
    seq.numGroundFrames = len(samples) - 1


def parse_ifl_file(path):
    frames = []
    with open(path, encoding="ascii", errors="replace") as fd:
        for line in fd:
            line = line.strip()
            if not line or line.startswith("//") or line.startswith("#"):
                continue
            parts = line.split()
            name = parts[0]
            duration = 1
            if len(parts) > 1:
                try:
                    duration = int(float(parts[1]))
                except ValueError:
                    duration = 1
            if duration < 1:
                duration = 1
            frames.append((name, duration))
    return frames


def resolve_ifl_path(ifl_name, *search_dirs):
    if not ifl_name:
        return None
    basename = ifl_name if ifl_name.lower().endswith(".ifl") else ifl_name + ".ifl"
    seen = set()
    for directory in search_dirs:
        if not directory:
            continue
        candidate = os.path.normpath(os.path.join(directory, os.path.basename(basename)))
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate
    if os.path.isfile(ifl_name):
        return ifl_name
    return None


def ifl_animates(frames):
    names = [name for name, _ in frames]
    return len(names) > 1 and any(name != names[0] for name in names[1:])


def ifl_matters_from_shape(shape):
    matters = []
    for ifl in shape.iflmaterials:
        frames = getattr(ifl, "frames", None) or []
        matters.append(ifl_animates(frames))
    return matters


def find_bounds_object(scene, mesh_bounds=None, scope=None):
    found = mesh_bounds
    named = []
    for ob in iter_export_objects(scene, scope):
        if is_bounds_name(ob.name):
            named.append(ob)
            if ob.type == "EMPTY":
                found = ob
            elif found is None:
                found = ob
    if len(named) > 1:
        note("Warning: Multiple 'bounds' objects found - check capitalization")
    return found


def ground_frame_indices(frame_start, frame_end, scene_fps, ground_fps):
    frames = list(range(int(frame_start), int(frame_end) + 1))
    if not frames:
        return frames
    if not ground_fps or ground_fps <= 0:
        return frames
    step = max(int(round(float(scene_fps) / float(ground_fps))), 1)
    sampled = frames[::step]
    if sampled[-1] != frames[-1]:
        sampled.append(frames[-1])
    if len(sampled) < 2:
        return frames
    return sampled


def sample_world_frames(scene, ob, frames):
    samples = []
    for frame in frames:
        scene.frame_set(frame)
        location, rotation, _ = ob.matrix_world.decompose()
        samples.append((location.copy(), rotation.copy()))
    return samples


def export_node_is_bone(node):
    return getattr(node, "armature", None) is not None


POSE_VIS_PROP = "torque_vis_value"
OBJECT_VIS_LEGACY_PATH = "torque_vis_props.vis_value"


def sample_vis_value(owner):
    if owner is None:
        return 1.0
    props = getattr(owner, "torque_vis_props", None)
    data = getattr(owner, "animation_data", None)
    action = data.action if data is not None else None
    if props is not None and action is not None:
        if array_from_fcurves(owner_action_fcurves(owner), OBJECT_VIS_LEGACY_PATH, 1):
            return float(props.vis_value)
    if hasattr(owner, POSE_VIS_PROP):
        return float(getattr(owner, POSE_VIS_PROP))
    if props is not None:
        return float(props.vis_value)
    return 1.0


def assign_vis_value(owner, value):
    if owner is None:
        return
    if hasattr(owner, POSE_VIS_PROP):
        setattr(owner, POSE_VIS_PROP, float(value))
        return
    props = getattr(owner, "torque_vis_props", None)
    if props is not None:
        props.vis_value = float(value)


def resolve_export_vis_source(bobj):
    if bobj is None:
        return None
    if bobj.parent_type == "BONE" and bobj.parent and bobj.parent.type == "ARMATURE":
        pose = bobj.parent.pose.bones.get(bobj.parent_bone)
        if pose is not None:
            return ("bone", bobj.parent, pose)
    if bobj.parent and bobj.parent.type == "ARMATURE" and bobj.parent_type == "OBJECT":
        return ("object", bobj)
    if bobj.parent and bobj.parent.type == "EMPTY":
        return ("object", bobj.parent)
    return ("object", bobj)


def sample_export_vis(source):
    if not source:
        return 1.0
    if source[0] == "bone":
        return sample_vis_value(source[2])
    return sample_vis_value(source[1])


def object_vis_curve_array(fcurves):
    curves = array_from_fcurves(fcurves, POSE_VIS_PROP, 1)
    if curves:
        return curves
    return array_from_fcurves(fcurves, OBJECT_VIS_LEGACY_PATH, 1)


def vis_source_curve_array(source):
    if not source:
        return None
    if source[0] == "bone":
        armature, pose = source[1], source[2]
        data = getattr(armature, "animation_data", None)
        if data is None or data.action is None:
            return None
        return array_from_fcurves(
            owner_action_fcurves(armature),
            pose_bone_data_path(pose.name, POSE_VIS_PROP),
            1)
    ob = source[1]
    data = getattr(ob, "animation_data", None)
    if data is None or data.action is None:
        return None
    return object_vis_curve_array(owner_action_fcurves(ob))


def vis_values_differ(values, eps=0.01):
    if not values:
        return False
    first = values[0]
    return any(abs(value - first) > eps for value in values[1:])


def object_has_object_state(seq, index):
    return (
        (index < len(seq.visMatters) and seq.visMatters[index])
        or (index < len(seq.frameMatters) and seq.frameMatters[index])
        or (index < len(seq.matFrameMatters) and seq.matFrameMatters[index]))


def object_state_rank(seq, object_index, object_count):
    rank = 0
    for index in range(object_count):
        if not object_has_object_state(seq, index):
            continue
        if index == object_index:
            return rank
        rank += 1
    return None


def sequence_object_vis_values(seq, object_index, objectstates, object_count):
    if object_index >= len(seq.visMatters) or not seq.visMatters[object_index]:
        return None
    rank = object_state_rank(seq, object_index, object_count)
    if rank is None:
        return None
    values = []
    for frame_index in range(seq.numKeyframes):
        state_index = seq.baseObjectState + rank * seq.numKeyframes + frame_index
        if state_index < 0 or state_index >= len(objectstates):
            return None
        values.append(objectstates[state_index].vis)
    return values


def key_object_vis(ob, values, start_frame):
    if not values or ob is None:
        return
    assign_vis_value(ob, values[0])
    write_action_array_keys(
        _action_for_object(ob), POSE_VIS_PROP,
        [(value,) for value in values], start_frame, owner=ob)


def _action_for_object(ob):
    if ob.animation_data is None:
        ob.animation_data_create()
    if ob.animation_data.action is not None:
        return ob.animation_data.action
    action = bpy.data.actions.new(ob.name + "Action")
    ob.animation_data.action = action
    return action


def imported_pose_bone_name(node_names, node_index):
    """Bone name for an imported node, or None if the index is unused."""
    if node_index is None or node_index < 0 or node_index >= len(node_names):
        return None
    name = node_names[node_index]
    if not isinstance(name, str) or not name:
        return None
    return name


def import_object_visibility(
        shape, sequences, actions, node_names, armature, object_bobjs,
        start_frame=SEQUENCE_START_FRAME, start_frames=None):
    object_count = len(shape.objects)
    objectstates = getattr(shape, "objectstates", None) or []
    for obj_index, obj in enumerate(shape.objects):
        default_vis = 1.0
        if obj_index < len(objectstates):
            default_vis = objectstates[obj_index].vis
        bone_name = imported_pose_bone_name(node_names, obj.node)
        if bone_name and armature is not None:
            pose = armature.pose.bones.get(bone_name)
            if pose is not None:
                assign_vis_value(pose, default_vis)
                continue
        if obj_index < len(object_bobjs):
            for bobj in object_bobjs[obj_index]:
                assign_vis_value(bobj, default_vis)

    keyed_skins = set()
    keyed_bones = set()
    for seq_index, seq in enumerate(sequences):
        action = actions[seq_index] if actions and seq_index < len(actions) else None
        seq_start = start_frame
        if start_frames and seq_index < len(start_frames):
            seq_start = start_frames[seq_index]
        for obj_index, obj in enumerate(shape.objects):
            values = sequence_object_vis_values(
                seq, obj_index, objectstates, object_count)
            if values is None:
                continue
            bone_name = imported_pose_bone_name(node_names, obj.node)
            if bone_name and armature is not None:
                if action is None:
                    continue
                bone_key = (id(action), bone_name, seq_start)
                if bone_key in keyed_bones:
                    continue
                keyed_bones.add(bone_key)
                write_action_array_keys(
                    action,
                    pose_bone_data_path(bone_name, POSE_VIS_PROP),
                    [(value,) for value in values],
                    seq_start, owner=armature)
                continue
            skin_key = (obj_index, seq_start)
            if skin_key in keyed_skins:
                note("Warning: Visibility for skin object {} is already keyed; "
                      "later sequences would overlap on the mesh. Keeping the first."
                      .format(obj_index))
                continue
            keyed_skins.add(skin_key)
            if obj_index < len(object_bobjs):
                for bobj in object_bobjs[obj_index]:
                    key_object_vis(bobj, values, seq_start)


def sample_export_node(node):
    # 2.79/2.81 decomposed matrix_local. inv(parent_world) dies when the
    # parent is scaled to 0 to hide children.
    local = node_export_local_matrix(node, posed=True)
    rest_local = getattr(node, "rest_local", None)
    if rest_local is None:
        rest_local = getattr(node, "matrix", None)
    if rest_local is not None:
        local = local @ rest_inv_scale_matrix(rest_local)
    translation, rotation, scale = local.decompose()
    if export_node_is_bone(node):
        pose = node.armature.pose.bones.get(node.bl_ob.name)
        return translation, rotation, scale, sample_vis_value(pose)
    bl_ob = getattr(node, "bl_ob", None)
    if bl_ob is not None:
        return translation, rotation, scale, sample_vis_value(bl_ob)
    return translation, rotation, scale, 1.0


def export_node_fcurves(node):
    if export_node_is_bone(node):
        return owner_action_fcurves(node.armature) or None
    return owner_action_fcurves(getattr(node, "bl_ob", None)) or None


def dsq_entry_display_name(entry):
    if entry["kind"] == "bone":
        return entry["bone"].name
    return object_export_name(entry["object"])


def dsq_entry_current_world(entry):
    if entry["kind"] == "bone":
        return bone_export_world_matrix(
            entry["armature"], entry["bone"], posed=True)
    return entry["object"].matrix_world.copy()


def dsq_entry_current_local(entry):
    if entry["kind"] == "bone":
        return bone_export_local_matrix(
            entry["armature"], entry["bone"], posed=True)
    return blender_object_local_matrix(entry["object"])


def dsq_entry_rest(entry):
    rest = entry.get("rest_world")
    if rest is None:
        if entry["kind"] == "bone":
            rest = bone_export_world_matrix(
                entry["armature"], entry["bone"], posed=False)
        else:
            rest = entry["object"].matrix_world.copy()
        entry["rest_world"] = rest
    if entry.get("rest_local") is None:
        if entry["kind"] == "bone":
            entry["rest_local"] = bone_export_local_matrix(
                entry["armature"], entry["bone"], posed=False)
        else:
            entry["rest_local"] = blender_object_local_matrix(entry["object"])
    local = folded_local_matrix(rest, entry.get("parent_rest"))
    return local.decompose()


def dsq_entry_sample(entry):
    rest_local = entry.get("rest_local")
    if rest_local is None:
        dsq_entry_rest(entry)
        rest_local = entry.get("rest_local")
    local = dsq_entry_current_local(entry)
    if rest_local is not None:
        local = local @ rest_inv_scale_matrix(rest_local)
    return local.decompose()


def dsq_entry_fcurves(entry):
    if entry["kind"] == "bone":
        return owner_action_fcurves(entry["armature"]) or None
    return owner_action_fcurves(entry["object"]) or None


def dsq_entry_curve_arrays(entry, fcurves):
    if entry["kind"] == "bone":
        bone = entry["bone"]
        pose = entry["armature"].pose.bones.get(bone.name)
        if pose is not None:
            suffix, count = fcurves_path_from_rotation(pose)
            curves_rotation = array_from_fcurves(
                fcurves, pose_bone_data_path(bone.name, suffix), count)
        else:
            curves_rotation = None
        return (
            curves_rotation,
            array_from_fcurves(fcurves, pose_bone_data_path(bone.name, "location"), 3),
            array_from_fcurves(fcurves, pose_bone_data_path(bone.name, "scale"), 3),
            array_from_fcurves(fcurves, pose_bone_data_path(bone.name, POSE_VIS_PROP), 1),
        )
    ob = entry["object"]
    return (
        array_from_fcurves_rotation(fcurves, ob),
        array_from_fcurves(fcurves, "location", 3),
        array_from_fcurves(fcurves, "scale", 3),
        object_vis_curve_array(fcurves),
    )


def export_node_curve_arrays(node, fcurves):
    if export_node_is_bone(node):
        bone = node.bl_ob
        pose = node.armature.pose.bones.get(bone.name)
        if pose is not None:
            suffix, count = fcurves_path_from_rotation(pose)
            curves_rotation = array_from_fcurves(
                fcurves, pose_bone_data_path(bone.name, suffix), count)
        else:
            curves_rotation = None
        return (
            curves_rotation,
            array_from_fcurves(fcurves, pose_bone_data_path(bone.name, "location"), 3),
            array_from_fcurves(fcurves, pose_bone_data_path(bone.name, "scale"), 3),
            array_from_fcurves(fcurves, pose_bone_data_path(bone.name, POSE_VIS_PROP), 1),
        )
    bl_ob = getattr(node, "bl_ob", None) or node
    return (
        array_from_fcurves_rotation(fcurves, bl_ob),
        array_from_fcurves(fcurves, "location", 3),
        array_from_fcurves(fcurves, "scale", 3),
        object_vis_curve_array(fcurves),
    )


def write_ifl_sidecar(source_path, dts_filepath):
    if not source_path or not os.path.isfile(source_path):
        return
    dest = os.path.join(os.path.dirname(dts_filepath), os.path.basename(source_path))
    if os.path.normcase(os.path.abspath(source_path)) == os.path.normcase(os.path.abspath(dest)):
        return
    shutil.copy2(source_path, dest)
    note("Wrote IFL", dest)


def attach_ifl_sidecars(shape, dts_filepath, *search_dirs):
    for ifl in shape.iflmaterials:
        name = shape.names[ifl.name] if isinstance(ifl.name, int) else ifl.name
        path = resolve_ifl_path(name, *search_dirs)
        ifl.source_path = path
        if path is None:
            note("Warning: IFL '{}.ifl' not found next to the blend file or DTS".format(name))
            ifl.frames = []
            continue
        ifl.frames = parse_ifl_file(path)
        ifl.firstFrame = 0
        ifl.numFrames = len(ifl.frames)
        ifl.time = sum(duration for _, duration in ifl.frames)
        write_ifl_sidecar(path, dts_filepath)
