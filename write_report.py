import os
from datetime import datetime

from .DtsTypes import Material, Mesh, Primitive, Sequence, tge_mesh_draw_error
from .encoded_normals import decode_normal

LOG_DISABLED = "DISABLED"
LOG_LITE = "LITE"
LOG_FULL = "FULL"

LOG_LEVEL_ITEMS = (
    (LOG_DISABLED, "Disabled", "Do not write a log file"),
    (LOG_LITE, "Lite", "Write a sidecar stats report next to the file"),
    (LOG_FULL, "Full",
     "Lite plus verts, influences, and animation tracks. Can be large"),
)

_active_log = None


def normalize_log_level(level=None, debug_report=None):
    if isinstance(level, str):
        key = level.strip().upper()
        if key in (LOG_DISABLED, LOG_LITE, LOG_FULL):
            return key
        if key in ("TRUE", "1", "YES"):
            return LOG_LITE
        if key in ("FALSE", "0", "NO", ""):
            return LOG_DISABLED
    if level is True or debug_report is True:
        return LOG_LITE
    return LOG_DISABLED


class IoLog:
    def __init__(self, level=LOG_DISABLED):
        self.level = normalize_log_level(level)
        self.notes = []
        self.meta = {}
        self.error = None
        self.shape = None
        self.dsq = None
        self.written = False

    @property
    def enabled(self):
        return self.level != LOG_DISABLED

    @property
    def full(self):
        return self.level == LOG_FULL

    def note(self, *args):
        text = " ".join(str(arg) for arg in args)
        print(text)
        if self.enabled:
            self.notes.append(text)


def set_active_log(log):
    global _active_log
    _active_log = log


def note(*args):
    text = " ".join(str(arg) for arg in args)
    print(text)
    if _active_log is not None and _active_log.enabled:
        _active_log.notes.append(text)


def record_export_error(message):
    if _active_log is None:
        return
    text = str(message)
    _active_log.error = text
    if not _active_log.enabled:
        return
    line = "Error: {}".format(text)
    if not _active_log.notes or _active_log.notes[-1] != line:
        _active_log.notes.append(line)


def _fmt_vec(value, digits=6):
    if value is None:
        return "None"
    try:
        return "{0:.{n}f} {1:.{n}f} {2:.{n}f}".format(
            float(value.x), float(value.y), float(value.z), n=digits)
    except AttributeError:
        return "{0:.{n}f} {1:.{n}f} {2:.{n}f}".format(
            float(value[0]), float(value[1]), float(value[2]), n=digits)


def _fmt_quat(value, digits=6):
    if value is None:
        return "None"
    return "{0:.{n}f} {1:.{n}f} {2:.{n}f} {3:.{n}f}".format(
        float(value.x), float(value.y), float(value.z), float(value.w), n=digits)


def _fmt_box(box):
    if box is None:
        return "None"
    return "min {}  max {}".format(_fmt_vec(box.min), _fmt_vec(box.max))


def _skin_bind_rest_label(node, initial_transform, epsilon=1e-3):
    bone_world = getattr(node, "matrix_world", None) if node is not None else None
    if bone_world is None or initial_transform is None or len(initial_transform) != 16:
        return None
    world = [float(x) for row in bone_world.row for x in row]
    bind = [float(x) for x in initial_transform]
    product = [0.0] * 16
    for row in range(4):
        for col in range(4):
            product[row * 4 + col] = sum(
                world[row * 4 + k] * bind[k * 4 + col] for k in range(4))
    ident = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
    if all(abs(product[i] - ident[i]) <= epsilon for i in range(16)):
        return "rest identity"
    return "rest NOT identity"


def _name_at(names, index):
    if index is None or index < 0 or index >= len(names):
        return "<none>"
    return names[index]


def _matters_names(flags, names):
    picked = []
    for index, name in enumerate(names):
        if index < len(flags) and flags[index]:
            picked.append(name)
    return " ".join(picked) if picked else "(none)"


def _seq_flag_names(flags):
    names = []
    for name in ("UniformScale", "AlignedScale", "ArbitraryScale", "Blend",
                 "Cyclic", "MakePath", "IflInit", "HasTranslucency"):
        if flags & getattr(Sequence, name):
            names.append(name)
    return " ".join(names) if names else "(none)"


def _mesh_flag_names(mesh):
    names = []
    for name in ("Billboard", "HasDetailTexture", "BillboardZAxis",
                 "UseEncodedNormals"):
        if mesh.get_flags(getattr(Mesh, name)):
            names.append(name)
    return " ".join(names) if names else "(none)"


def _prim_flag_names(prim):
    names = []
    if prim.type & Primitive.Strip:
        names.append("Strip")
    elif prim.type & Primitive.Fan:
        names.append("Fan")
    else:
        names.append("Triangles")
    if prim.type & Primitive.Indexed:
        names.append("Indexed")
    if prim.type & Primitive.NoMaterial:
        names.append("NoMaterial")
    names.append("mat:{}".format(prim.type & Primitive.MaterialMask))
    return " ".join(names)


def _prim_kind_counts(mesh):
    strips = lists = fans = 0
    for prim in mesh.primitives:
        if prim.type & Primitive.Strip:
            strips += 1
        elif prim.type & Primitive.Fan:
            fans += 1
        else:
            lists += 1
    return strips, lists, fans


def _mesh_owner(shape, mesh_index):
    for obj in shape.objects:
        if obj.firstMesh <= mesh_index < obj.firstMesh + obj.numMeshes:
            return _name_at(shape.names, obj.name)
    return "mesh{}".format(mesh_index)


def _write_header(p, meta, level):
    meta = meta or {}
    p("Torque DTS/DSQ log")
    p("log_level = {}".format(level))
    p("operation = {}".format(meta.get("operation", "?")))
    p("addon = {}".format(meta.get("addon", "Torque DTS format")))
    p("addon_version = {}".format(meta.get("addon_version", "?")))
    p("blender = {}".format(meta.get("blender", "?")))
    p("time = {}".format(meta.get("time", datetime.now().isoformat(timespec="seconds"))))
    p("source = {}".format(meta.get("source") or "-"))
    p("dest = {}".format(meta.get("dest") or "-"))
    p("file_version = {}".format(meta.get("file_version", "?")))
    p("exporter_version = {}".format(meta.get("exporter_version", "?")))
    settings = meta.get("settings") or {}
    if settings:
        p("settings:")
        for key in sorted(settings):
            p("  {} = {}".format(key, settings[key]))
    p("")


def _write_notes(p, notes):
    p("Notes ({}):".format(len(notes)))
    if not notes:
        p("  (none)")
    else:
        for line in notes:
            p("  " + line)
    p("")


def _write_failure_footer(p, error):
    if not error:
        return
    p("")
    p("Export failed")
    for line in str(error).splitlines() or [str(error)]:
        p(line)


def _try_ifl_text(dest_path, name):
    if not dest_path or not name:
        return None
    path = os.path.join(os.path.dirname(dest_path), name + ".ifl")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fd:
            return fd.read()
    except OSError:
        return None


def _write_lod_grid(p, shape):
    details = [lod for lod in shape.detail_levels if lod.subshape >= 0]
    headers = []
    for lod in details:
        headers.append("{}@{}".format(_name_at(shape.names, lod.name), int(lod.size)))
    p("Object x detail ({} objects, {} details):".format(
        len(shape.objects), len(details)))
    if headers:
        p("  object  " + "  ".join(headers))
    for obj in shape.objects:
        slots = []
        for lod in details:
            slot = lod.objectDetail
            mesh_index = obj.firstMesh + slot
            if 0 <= slot < obj.numMeshes and 0 <= mesh_index < len(shape.meshes):
                mtype = shape.meshes[mesh_index].get_type()
                slots.append(Mesh.TypeName[mtype] if 0 <= mtype < len(Mesh.TypeName) else str(mtype))
            else:
                slots.append("-")
        p("  {}  {}".format(_name_at(shape.names, obj.name), "  ".join(slots)))
    p("")


def _write_sequence_tracks(p, seq, names, rotations, translations,
                           uniform_scales, aligned_scales,
                           arbitrary_rots, arbitrary_factors, node_names):
    def dump_track(label, matters, values, base, fmt):
        rank = 0
        for index, name in enumerate(node_names):
            if index >= len(matters) or not matters[index]:
                continue
            p("    {} {} {}:".format(label, index, name))
            start = base + rank * seq.numKeyframes
            for key in range(seq.numKeyframes):
                item_index = start + key
                if 0 <= item_index < len(values):
                    p("      k{}  {}".format(key, fmt(values[item_index])))
                else:
                    p("      k{}  MISSING".format(key))
            rank += 1

    dump_track("rot", seq.rotationMatters, rotations, seq.baseRotation, _fmt_quat)
    dump_track("trn", seq.translationMatters, translations, seq.baseTranslation, _fmt_vec)
    if seq.flags & Sequence.ArbitraryScale:
        dump_track("scale_rot", seq.scaleMatters, arbitrary_rots, seq.baseScale, _fmt_quat)
        dump_track("scale_fac", seq.scaleMatters, arbitrary_factors, seq.baseScale, _fmt_vec)
    elif seq.flags & Sequence.AlignedScale:
        dump_track("scale", seq.scaleMatters, aligned_scales, seq.baseScale, _fmt_vec)
    elif seq.flags & Sequence.UniformScale:
        dump_track("scale", seq.scaleMatters, uniform_scales, seq.baseScale,
                   lambda value: "{:.6f}".format(float(value)))


def _sequence_vis_values(seq, object_index, objectstates, object_count):
    if object_index >= len(seq.visMatters) or not seq.visMatters[object_index]:
        return None
    rank = 0
    for index in range(object_count):
        has_state = (
            (index < len(seq.visMatters) and seq.visMatters[index])
            or (index < len(seq.frameMatters) and seq.frameMatters[index])
            or (index < len(seq.matFrameMatters) and seq.matFrameMatters[index]))
        if not has_state:
            continue
        if index == object_index:
            values = []
            for frame_index in range(seq.numKeyframes):
                state_index = seq.baseObjectState + rank * seq.numKeyframes + frame_index
                if state_index < 0 or state_index >= len(objectstates):
                    return None
                values.append(objectstates[state_index].vis)
            return values
        rank += 1
    return None


def _write_vis_keys(p, shape, seq):
    object_names = [_name_at(shape.names, obj.name) for obj in shape.objects]
    wrote = False
    for obj_index, name in enumerate(object_names):
        values = _sequence_vis_values(
            seq, obj_index, shape.objectstates, len(shape.objects))
        if values is None:
            continue
        wrote = True
        p("    vis {} {}: {}".format(
            obj_index, name, " ".join("{:.4f}".format(float(v)) for v in values)))
    if not wrote:
        p("    vis: (none)")


def write_debug_report(filepath, shape, meta=None, level=LOG_LITE, notes=None,
                      error=None):
    level = normalize_log_level(level)
    if level == LOG_DISABLED:
        return
    meta = dict(meta or {})
    meta.setdefault("file_version", getattr(shape, "dtsVersion", "?"))
    meta.setdefault("exporter_version", getattr(shape, "exporterVersion", "?"))
    notes = list(notes or [])

    with open(filepath, "w", encoding="utf-8") as fd:
        def p(line=""):
            fd.write(line + "\n")

        def gn(index):
            return _name_at(shape.names, index)

        _write_header(p, meta, level)
        _write_notes(p, notes)

        size_s32 = int(shape.smallest_size)
        p("Shape")
        p("  names = {}".format(len(shape.names)))
        p("  nodes = {}".format(len(shape.nodes)))
        p("  objects = {}".format(len(shape.objects)))
        p("  meshes = {}".format(len(shape.meshes)))
        p("  materials = {}".format(len(shape.materials)))
        p("  sequences = {}".format(len(shape.sequences)))
        p("  ifl = {}".format(len(shape.iflmaterials)))
        p("  ground_frames = {}".format(len(shape.ground_translations)))
        p("  triggers = {}".format(len(shape.triggers)))
        p("  center = {}".format(_fmt_vec(shape.center)))
        p("  radius = {}".format(shape.radius))
        p("  radius_tube = {}".format(shape.radius_tube))
        p("  radius_source = {}".format(
            getattr(shape, "log_radius_source", meta.get("radius_source", "?"))))
        p("  bounds = {}".format(_fmt_box(shape.bounds)))
        p("  smallest_size = {}".format(shape.smallest_size))
        p("  smallest_size_s32 = {}".format(size_s32))
        p("  smallest_size_as_tge_float = {}".format(float(size_s32)))
        p("  smallest_detail_level = {}".format(shape.smallest_detail_level))
        buffers = getattr(shape, "log_buffers", None) or meta.get("buffers")
        if buffers:
            p("  buffers = 32:{} 16:{} 8:{}".format(
                buffers.get("buffer32", "?"),
                buffers.get("buffer16", "?"),
                buffers.get("buffer8", "?")))
        p("  sequence_rotations = {}".format(len(shape.node_rotations)))
        p("  sequence_translations = {}".format(len(shape.node_translations)))
        p("  sequence_uniform_scales = {}".format(len(shape.node_uniform_scales)))
        p("  sequence_aligned_scales = {}".format(len(shape.node_aligned_scales)))
        p("  sequence_arbitrary_scales = {}".format(len(shape.node_arbitrary_scale_factors)))
        p("")

        p("Detail levels ({}):".format(len(shape.detail_levels)))
        for i, lod in enumerate(shape.detail_levels):
            p("  LOD {} {} (size {})".format(i, gn(lod.name), lod.size))
            p("    subshape = {}".format(lod.subshape))
            p("    objectDetail = {}".format(lod.objectDetail))
            if lod.subshape < 0:
                from .shared_export import unpack_auto_billboard  # noqa: PLC0415
                bb = unpack_auto_billboard(lod.objectDetail)
                p("    auto-billboard equator={} polar={} angle={:.4f} dl={} dim={} poles={}".format(
                    bb["equator_steps"], bb["polar_steps"], bb["polar_angle"],
                    bb["snapshot_detail"], bb["dimension"], bb["include_poles"]))
            p("    polyCount = {}".format(lod.polyCount))
            alpha_in = getattr(shape, "alphaIn", None) or getattr(shape, "alpha_in", None)
            alpha_out = getattr(shape, "alphaOut", None) or getattr(shape, "alpha_out", None)
            if alpha_in and i < len(alpha_in) and alpha_in[i] is not None:
                p("    alphaIn = {}".format(alpha_in[i]))
            if alpha_out and i < len(alpha_out) and alpha_out[i] is not None:
                p("    alphaOut = {}".format(alpha_out[i]))
        p("")

        _write_lod_grid(p, shape)

        p("Subshapes ({}):".format(len(shape.subshapes)))
        for i, sub in enumerate(shape.subshapes):
            p("  Subshape {}".format(i))
            node_bits = []
            for index in range(sub.firstNode, sub.firstNode + sub.numNodes):
                if 0 <= index < len(shape.nodes):
                    node_bits.append("{}->{}".format(index, gn(shape.nodes[index].name)))
            obj_bits = []
            for index in range(sub.firstObject, sub.firstObject + sub.numObjects):
                if 0 <= index < len(shape.objects):
                    obj_bits.append("{}->{}".format(index, gn(shape.objects[index].name)))
            p("    nodes = {}".format(", ".join(node_bits)))
            p("    objects = {}".format(", ".join(obj_bits)))
        p("")

        p("Nodes ({}):".format(len(shape.nodes)))
        for i, node in enumerate(shape.nodes):
            if node.parent == -1:
                p("  {} {}".format(i, gn(node.name)))
            else:
                p("  {} {}  ->  {} {}".format(
                    i, gn(node.name), node.parent, gn(shape.nodes[node.parent].name)))
            if i < len(shape.default_translations):
                p("    translation = {}".format(_fmt_vec(shape.default_translations[i])))
            else:
                p("    translation = MISSING")
            if i < len(shape.default_rotations):
                p("    rotation = {}".format(_fmt_quat(shape.default_rotations[i])))
            else:
                p("    rotation = MISSING")
        p("")

        p("Objects ({}):".format(len(shape.objects)))
        for i, obj in enumerate(shape.objects):
            mesh_bits = []
            has_skin = False
            for mesh_index in range(obj.firstMesh, obj.firstMesh + obj.numMeshes):
                mtype = shape.meshes[mesh_index].get_type()
                if mtype == Mesh.SkinType:
                    has_skin = True
                mesh_bits.append("{}:{}".format(mesh_index, Mesh.TypeName[mtype]))
            if obj.node == -1:
                attach = "skin (no object node)" if has_skin else "NOT ATTACHED"
            else:
                attach = "in {} ({})".format(obj.node, gn(shape.nodes[obj.node].name))
            p("  {} {} {}, meshes = {}".format(
                i, gn(obj.name), attach, ", ".join(mesh_bits)))
        p("")

        p("Materials ({}):".format(len(shape.materials)))
        flag_names = (
            "SWrap", "TWrap", "Translucent", "Additive", "Subtractive",
            "SelfIlluminating", "NeverEnvMap", "NoMipMap", "MipMapZeroBorder",
            "IFLMaterial", "IFLFrame", "DetailMap", "BumpMap", "ReflectanceMap")
        for i, mat in enumerate(shape.materials):
            flags = " ".join(name for name in flag_names if mat.flags & getattr(Material, name))
            p("  {} {} ({} {})".format(i, mat.name, mat.flags, flags))
            p("    bumpMap = {}, reflectanceMap = {}, detailMap = {}".format(
                mat.bumpMap, mat.reflectanceMap, mat.detailMap))
            p("    reflectance = {}, detailScale = {}".format(
                mat.reflectance, mat.detailScale))
        p("")

        p("IFL materials ({}):".format(len(shape.iflmaterials)))
        dest = meta.get("dest") or meta.get("source")
        for ifl in shape.iflmaterials:
            name = gn(ifl.name)
            if 0 <= ifl.slot < len(shape.materials):
                mat_name = shape.materials[ifl.slot].name
            else:
                mat_name = "<MISSING>"
            p("  IflMat {}".format(name))
            p("    slot = {} {}, time = {}".format(ifl.slot, mat_name, ifl.time))
            p("    firstFrame = {}, numFrames = {}".format(ifl.firstFrame, ifl.numFrames))
            text = _try_ifl_text(dest, name)
            if text:
                p("    sidecar:")
                for line in text.splitlines() or [""]:
                    p("      " + line)
        p("")

        p("Meshes ({}):".format(len(shape.meshes)))
        for i, mesh in enumerate(shape.meshes):
            mtype = mesh.get_type()
            owner = _mesh_owner(shape, i)
            p("  Mesh {} - {} ({})".format(i, Mesh.TypeName[mtype], owner))
            if mtype == Mesh.NullType:
                continue
            p("    flags = {} {}".format(mesh.get_flags(), _mesh_flag_names(mesh)))
            p("    bounds = {}".format(_fmt_box(mesh.bounds)))
            p("    center = {}".format(_fmt_vec(mesh.center)))
            p("    radius = {}".format(mesh.radius))
            p("    verts = {}".format(len(mesh.verts)))
            p("    tverts = {}".format(len(mesh.tverts)))
            p("    normals = {}".format(len(mesh.normals)))
            p("    enormals = {}".format(len(mesh.enormals)))
            p("    vertsPerFrame = {}".format(mesh.vertsPerFrame))
            p("    numFrames = {}".format(mesh.numFrames))
            strips, lists, fans = _prim_kind_counts(mesh)
            p("    primitives = {} (strip {} list {} fan {})".format(
                len(mesh.primitives), strips, lists, fans))
            p("    indices = {}".format(len(mesh.indices)))
            p("    mindices = {}".format(len(mesh.mindices)))
            if getattr(mesh, "log_two_sided", False):
                p("    two_sided = doubled indices")
            opt = getattr(mesh, "log_optimize", None)
            if opt:
                p("    optimize welded={} dropped={} strip={} list={} indices={}".format(
                    opt.get("welded", 0), opt.get("dropped", 0),
                    opt.get("strips", 0), opt.get("lists", 0), opt.get("indices", 0)))
            reason = tge_mesh_draw_error(mesh, owner)
            p("    tge = {}".format(reason or "ok"))
            if mtype == Mesh.SkinType:
                infl_per_vert = {}
                for vertex_index, _bone, _weight in mesh.influences:
                    infl_per_vert[vertex_index] = infl_per_vert.get(vertex_index, 0) + 1
                max_infl = max(infl_per_vert.values()) if infl_per_vert else 0
                used_bones = {bone for _v, bone, _w in mesh.influences}
                empty_bones = [
                    bone_i for bone_i in range(len(mesh.bones))
                    if bone_i not in used_bones]
                p("    bones = {}".format(len(mesh.bones)))
                p("    influences = {}".format(len(mesh.influences)))
                p("    max_influences_per_vert = {}".format(max_infl))
                p("    bones_with_no_weight = {}".format(
                    empty_bones if empty_bones else "(none)"))
            p("    + Primitives ({}):".format(len(mesh.primitives)))
            for prim in mesh.primitives:
                last = prim.firstElement + prim.numElements - 1
                p("      {}->{} {} {}".format(
                    prim.firstElement, last, prim.type, _prim_flag_names(prim)))
                if level == LOG_FULL:
                    run = mesh.indices[prim.firstElement:prim.firstElement + prim.numElements]
                    p("        indices {}".format(" ".join(str(x) for x in run)))
            if level == LOG_FULL:
                p("    + Vertices ({}) FULL:".format(len(mesh.verts)))
                for vi, vert in enumerate(mesh.verts):
                    normal = mesh.normals[vi] if vi < len(mesh.normals) else None
                    enorm = mesh.enormals[vi] if vi < len(mesh.enormals) else 0
                    uv = mesh.tverts[vi] if vi < len(mesh.tverts) else None
                    decoded = decode_normal(int(enorm) & 255)
                    extra = ""
                    if vi < len(getattr(mesh, "vert_ids", ())):
                        extra += "  vert_id={}".format(mesh.vert_ids[vi])
                    if vi < len(getattr(mesh, "smooth_ids", ())):
                        extra += "  smooth_id={}".format(mesh.smooth_ids[vi])
                    p("      v{}  {}  n {}  e {} ({:.6f} {:.6f} {:.6f})  uv {}{}".format(
                        vi, _fmt_vec(vert),
                        _fmt_vec(normal) if normal is not None else "-",
                        enorm, decoded[0], decoded[1], decoded[2],
                        "{:.6f} {:.6f}".format(float(uv.x), float(uv.y)) if uv is not None else "-",
                        extra))
                if mesh.mindices:
                    p("    + Merge indices: {}".format(
                        " ".join(str(x) for x in mesh.mindices)))
                if mtype == Mesh.SkinType:
                    p("    + Bones ({}) FULL:".format(len(mesh.bones)))
                    for bone_i, (node_index, initial_transform) in enumerate(mesh.bones):
                        node = shape.nodes[node_index] if 0 <= node_index < len(shape.nodes) else None
                        node_name = gn(node.name) if node is not None else "?"
                        rest = _skin_bind_rest_label(node, initial_transform)
                        extra = "  {}".format(rest) if rest else ""
                        p("      bone{} node={} ({}){}".format(
                            bone_i, node_index, node_name, extra))
                        p("        bind {}".format(
                            " ".join("{:.6f}".format(float(x)) for x in initial_transform)))
                    p("    + Influences ({}) FULL:".format(len(mesh.influences)))
                    sums = {}
                    for vertex_index, bone_index, weight in mesh.influences:
                        p("      vert {} bone {} weight {:.6f}".format(
                            vertex_index, bone_index, float(weight)))
                        sums[vertex_index] = sums.get(vertex_index, 0.0) + float(weight)
                    for vertex_index in sorted(sums):
                        p("      weight_sum vert {} {:.6f}".format(
                            vertex_index, sums[vertex_index]))
        p("")

        node_names = [gn(node.name) for node in shape.nodes]
        object_names = [gn(obj.name) for obj in shape.objects]
        ifl_names = [gn(ifl.name) for ifl in shape.iflmaterials]
        p("Sequences ({}):".format(len(shape.sequences)))
        for i, seq in enumerate(shape.sequences):
            p("  {} {}".format(i, gn(seq.nameIndex)))
            p("    flags: {} {}".format(seq.flags, _seq_flag_names(seq.flags)))
            p("    numKeyframes: {}".format(seq.numKeyframes))
            p("    duration: {}".format(seq.duration))
            p("    priority: {}".format(seq.priority))
            p("    firstGroundFrame: {}".format(seq.firstGroundFrame))
            p("    numGroundFrames: {}".format(seq.numGroundFrames))
            p("    baseRotation: {}".format(seq.baseRotation))
            p("    baseTranslation: {}".format(seq.baseTranslation))
            p("    baseScale: {}".format(seq.baseScale))
            p("    baseObjectState: {}".format(seq.baseObjectState))
            p("    firstTrigger: {}".format(seq.firstTrigger))
            p("    numTriggers: {}".format(seq.numTriggers))
            p("    toolBegin: {}".format(seq.toolBegin))
            p("    rotationMatters: {}".format(_matters_names(seq.rotationMatters, node_names)))
            p("    translationMatters: {}".format(_matters_names(seq.translationMatters, node_names)))
            p("    scaleMatters: {}".format(_matters_names(seq.scaleMatters, node_names)))
            p("    visMatters: {}".format(_matters_names(seq.visMatters, object_names)))
            p("    frameMatters: {}".format(_matters_names(seq.frameMatters, object_names)))
            p("    matFrameMatters: {}".format(_matters_names(seq.matFrameMatters, object_names)))
            p("    iflMatters: {}".format(_matters_names(seq.iflMatters, ifl_names)))
            if level == LOG_FULL:
                _write_sequence_tracks(
                    p, seq, shape.names, shape.node_rotations, shape.node_translations,
                    shape.node_uniform_scales, shape.node_aligned_scales,
                    shape.node_arbitrary_scale_rots, shape.node_arbitrary_scale_factors,
                    node_names)
                if seq.numGroundFrames:
                    p("    ground FULL:")
                    for key in range(seq.numGroundFrames):
                        gi = seq.firstGroundFrame + key
                        trn = shape.ground_translations[gi] if gi < len(shape.ground_translations) else None
                        rot = shape.ground_rotations[gi] if gi < len(shape.ground_rotations) else None
                        p("      g{}  {}  {}".format(key, _fmt_vec(trn), _fmt_quat(rot)))
                if seq.numTriggers:
                    p("    triggers FULL:")
                    for key in range(seq.numTriggers):
                        ti = seq.firstTrigger + key
                        if 0 <= ti < len(shape.triggers):
                            trig = shape.triggers[ti]
                            p("      t{}  state {} pos {}".format(key, trig.state, trig.pos))
                _write_vis_keys(p, shape, seq)
        p("")

        p("Object states ({}):".format(len(shape.objectstates)))
        for i, state in enumerate(shape.objectstates):
            p("  {} vis={} frame={} matFrame={}".format(
                i, state.vis, state.frame, state.matFrame))
        p("")

        p("Names ({}):".format(len(shape.names)))
        for i, name in enumerate(shape.names):
            p("  {} = {}".format(i, name))
        _write_failure_footer(p, error)


def write_dsq_report(filepath, dsq, meta=None, level=LOG_LITE, notes=None,
                    error=None):
    level = normalize_log_level(level)
    if level == LOG_DISABLED:
        return
    meta = dict(meta or {})
    meta.setdefault("file_version", getattr(dsq, "version", 24))
    notes = list(notes or [])

    with open(filepath, "w", encoding="utf-8") as fd:
        def p(line=""):
            fd.write(line + "\n")

        _write_header(p, meta, level)
        _write_notes(p, notes)

        p("DSQ")
        p("  nodes = {}".format(len(dsq.nodes)))
        p("  sequences = {}".format(len(dsq.sequences)))
        p("  rotations = {}".format(len(dsq.rotations)))
        p("  translations = {}".format(len(dsq.translations)))
        p("  uniform_scales = {}".format(len(dsq.uniform_scales)))
        p("  aligned_scales = {}".format(len(dsq.aligned_scales)))
        p("  arbitrary_scale_rots = {}".format(len(dsq.arbitrary_scale_rots)))
        p("  arbitrary_scale_factors = {}".format(len(dsq.arbitrary_scale_factors)))
        p("  ground_translations = {}".format(len(dsq.ground_translations)))
        p("  ground_rotations = {}".format(len(dsq.ground_rotations)))
        p("  triggers = {}".format(len(dsq.triggers)))
        binds = meta.get("node_binds")
        if binds:
            p("  node binds:")
            for name, target in binds:
                p("    {} -> {}".format(name, target or "MISSING"))
        p("")

        p("Nodes ({}):".format(len(dsq.nodes)))
        for i, name in enumerate(dsq.nodes):
            p("  {}: {}".format(i, name))
        p("")

        p("Sequences ({}):".format(len(dsq.sequences)))
        for i, seq in enumerate(dsq.sequences):
            p("  {}: {}".format(i, seq.name))
            p("    flags: {} {}".format(seq.flags, _seq_flag_names(seq.flags)))
            p("    numKeyframes = {}".format(seq.numKeyframes))
            p("    duration = {}".format(seq.duration))
            p("    priority = {}".format(seq.priority))
            p("    firstGroundFrame = {}".format(seq.firstGroundFrame))
            p("    numGroundFrames = {}".format(seq.numGroundFrames))
            p("    firstTrigger = {}".format(seq.firstTrigger))
            p("    numTriggers = {}".format(seq.numTriggers))
            p("    toolBegin = {}".format(seq.toolBegin))
            p("    rotationMatters: {}".format(_matters_names(seq.rotationMatters, dsq.nodes)))
            p("    translationMatters: {}".format(_matters_names(seq.translationMatters, dsq.nodes)))
            p("    scaleMatters: {}".format(_matters_names(seq.scaleMatters, dsq.nodes)))
            p("    visMatters: {}".format(_matters_names(seq.visMatters, dsq.nodes)))
            if level == LOG_FULL:
                _write_sequence_tracks(
                    p, seq, dsq.nodes, dsq.rotations, dsq.translations,
                    dsq.uniform_scales, dsq.aligned_scales,
                    dsq.arbitrary_scale_rots, dsq.arbitrary_scale_factors,
                    dsq.nodes)
                if seq.numGroundFrames:
                    p("    ground FULL:")
                    for key in range(seq.numGroundFrames):
                        gi = seq.firstGroundFrame + key
                        trn = dsq.ground_translations[gi] if gi < len(dsq.ground_translations) else None
                        rot = dsq.ground_rotations[gi] if gi < len(dsq.ground_rotations) else None
                        p("      g{}  {}  {}".format(key, _fmt_vec(trn), _fmt_quat(rot)))
                if seq.numTriggers:
                    p("    triggers FULL:")
                    for key in range(seq.numTriggers):
                        ti = seq.firstTrigger + key
                        if 0 <= ti < len(dsq.triggers):
                            trig = dsq.triggers[ti]
                            p("      t{}  state {} pos {}".format(key, trig.state, trig.pos))
        if level == LOG_FULL and dsq.triggers and not any(
                seq.numTriggers for seq in dsq.sequences):
            p("Triggers ({}) FULL:".format(len(dsq.triggers)))
            for i, trig in enumerate(dsq.triggers):
                p("  {}  state {} pos {}".format(i, trig.state, trig.pos))
        _write_failure_footer(p, error)


def write_notes_report(filepath, meta=None, level=LOG_LITE, notes=None, error=None):
    level = normalize_log_level(level)
    if level == LOG_DISABLED:
        return
    with open(filepath, "w", encoding="utf-8") as fd:
        def p(line=""):
            fd.write(line + "\n")

        _write_header(p, meta, level)
        _write_notes(p, notes or [])
        _write_failure_footer(p, error)


def log_operation_kind(operation):
    raw = (operation or "").strip().lower()
    if raw.startswith("import"):
        return "import"
    if raw.startswith("export"):
        return "export"
    return "io"


def unique_log_path(filepath, operation=None, now=None, exists=os.path.exists):
    """Sidecar next to the DTS/DSQ: name.dts.export-YYYYMMDD-HHMMSS#1.txt."""
    kind = log_operation_kind(operation)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    prefix = "{}.{kind}-{stamp}#".format(filepath, kind=kind, stamp=stamp)
    index = 1
    while True:
        candidate = "{}{}.txt".format(prefix, index)
        if not exists(candidate):
            return candidate
        index += 1


def io_meta(operation, filepath, **settings):
    try:
        import bpy
        blender = bpy.app.version_string
    except Exception:
        blender = "?"
    reserved = {}
    for key in ("file_version", "exporter_version", "source", "dest", "node_binds"):
        if key in settings:
            reserved[key] = settings.pop(key)
    meta = {
        "operation": operation,
        "addon": "Torque DTS format",
        "addon_version": "1.0.0",
        "blender": blender,
        "source": filepath if "import" in operation else None,
        "dest": filepath,
        "settings": settings,
    }
    meta.update(reserved)
    return meta


def finish_log(filepath, log, shape=None, dsq=None, meta=None, error=None):
    if log is None or not log.enabled or getattr(log, "written", False):
        return
    if shape is None:
        shape = getattr(log, "shape", None)
    if dsq is None:
        dsq = getattr(log, "dsq", None)
    if error is None:
        error = getattr(log, "error", None)
    meta = dict(meta or {})
    meta.setdefault("log_level", log.level)
    dest = unique_log_path(
        filepath, meta.get("operation") or getattr(log, "meta", {}).get("operation"))
    meta.setdefault("dest", filepath)
    try:
        if shape is not None:
            write_debug_report(dest, shape, meta, log.level, log.notes, error=error)
        elif dsq is not None:
            write_dsq_report(dest, dsq, meta, log.level, log.notes, error=error)
        else:
            write_notes_report(dest, meta, log.level, log.notes, error=error)
        log.written = True
        print("Wrote log {} ({})".format(dest, log.level.lower()))
    except Exception as exc:
        try:
            extra = list(log.notes)
            extra.append("Warning: Log dump failed: {}".format(exc))
            write_notes_report(dest, meta, log.level, extra, error=error or str(exc))
            log.written = True
            print("Wrote log {} ({})".format(dest, log.level.lower()))
        except Exception as write_exc:
            print("Warning: Could not write log {}: {}".format(dest, write_exc))
