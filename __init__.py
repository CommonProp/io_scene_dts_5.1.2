bl_info = {
    "name": "Torque DTS format",
    "author": "port & bansheerubber & irrelevant.irreverent & Eagle517 & aeclaudecode",
    "version": (1, 1, 0),
    "blender": (5, 1, 2),
    "location": "File > Import-Export",
    "description": "Made for Blender 5.1.2. Import-Export DTS/DSQ v24, "
                   "meshes, UVs, materials and textures",
    "warning": "",
    "support": 'COMMUNITY',
    "category": "Import-Export"}

if "bpy" in locals():
    import importlib
    from . import (DtsTypes, DtsShape, DsqFile, util, action_compat,
                   shared_export, write_report, mesh_optimize, import_dts,
                   import_dsq, export_dts, export_dsq, mount_preview)
    importlib.reload(DtsTypes)
    importlib.reload(DtsShape)
    importlib.reload(DsqFile)
    importlib.reload(util)
    importlib.reload(action_compat)
    importlib.reload(shared_export)
    importlib.reload(write_report)
    importlib.reload(mesh_optimize)
    importlib.reload(import_dts)
    importlib.reload(import_dsq)
    importlib.reload(export_dts)
    importlib.reload(export_dsq)
    importlib.reload(mount_preview)
else:
    from . import (DtsTypes, DtsShape, DsqFile, util, action_compat,
                   shared_export, write_report, mesh_optimize, import_dts,
                   import_dsq, export_dts, export_dsq, mount_preview)

is_developer = False
try:
    from .developer import is_developer
except ImportError:
    pass

if is_developer:
    debug_prop_options = set()
else:
    debug_prop_options = {'HIDDEN'}

import bpy
from .write_report import LOG_LEVEL_ITEMS

from bpy.props import (BoolProperty,
                       FloatProperty,
                       IntProperty,
                       StringProperty,
                       EnumProperty,
                       PointerProperty,
                       CollectionProperty,
                       )
from bpy.types import OperatorFileListElement
from bpy_extras.io_utils import (ImportHelper,
                                 ExportHelper,
                                 )

SEQUENCE_MODE_ITEMS = (
    ("ACTIONS", "Individual Actions",
     "One Action per sequence"),
    ("MARKERS", "Markers (one Action)",
     "Lay sequences on one Action with name:start / name:end markers and a Sequences text block"),
)

EXPORT_SCOPE_ITEMS = (
    ("SCENE", "Scene",
     "Export every object in the scene"),
    ("SELECTION", "Selection Only",
     "Export selected objects only"),
    ("COLLECTION", "Collection",
     "Export one collection and its children as a DTS shape"),
    ("COLLECTIONS", "Collections",
     "Write one DTS per checked collection into the chosen folder"),
)

DSQ_EXPORT_SCOPE_ITEMS = EXPORT_SCOPE_ITEMS[:3]

COLLECTION_GROUP_ITEMS = (
    ("MULTI", "Multi-import only",
     "Put each file in its own collection when importing more than one file"),
    ("ALWAYS", "Import",
     "Put each file in its own collection, even when importing a single file"),
    ("DISABLED", "Disabled",
     "Link imported objects into the current collection"),
)


def _file_browser_export_operator(context):
    space = getattr(context, "space_data", None)
    return getattr(space, "active_operator", None)


def _refresh_export_sequence_list(kind, context, sequence_mode, select_marker,
                                 objects=None, show_all=False,
                                 collection_roots=None,
                                 strip_action_suffixes=True):
    from .shared_export import populate_export_sequence_list
    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, "torque_dts_export"):
        return
    populate_export_sequence_list(
        scene, kind, sequence_mode, select_marker,
        objects=objects, show_all=show_all,
        collection_roots=collection_roots,
        strip_action_suffixes=strip_action_suffixes)


def _operator_collection_roots(operator):
    from .shared_export import checked_export_collection_names, export_sequence_collections
    if operator is None:
        return None
    return export_sequence_collections(
        getattr(operator, "export_scope", "SCENE"),
        getattr(operator, "collection", "") or "",
        checked_export_collection_names(operator))


_EXPORT_REFRESHING = False


def _refresh_from_export_operator(kind, operator, context):
    global _EXPORT_REFRESHING
    if operator is None or _EXPORT_REFRESHING:
        return
    _EXPORT_REFRESHING = True
    try:
        _refresh_from_export_operator_impl(kind, operator, context)
    finally:
        _EXPORT_REFRESHING = False


def _refresh_from_export_operator_impl(kind, operator, context):
    scope = getattr(operator, "export_scope", "SCENE")
    if kind == "DTS" and scope == "COLLECTIONS":
        from .shared_export import populate_export_collection_pages
        scene = getattr(context, "scene", None)
        if scene is not None:
            populate_export_collection_pages(operator, scene)
        return
    _refresh_export_sequence_list(
        kind, context,
        getattr(operator, "sequence_mode", "ACTIONS"),
        bool(getattr(operator, "select_marker", False)),
        collection_roots=_operator_collection_roots(operator),
        show_all=bool(getattr(operator, "show_all_actions", False)),
        strip_action_suffixes=bool(getattr(operator, "strip_action_suffixes", True)))


def _update_dts_export_sequences(self, context):
    _refresh_from_export_operator("DTS", self, context)


def _update_dsq_export_sequences(self, context):
    _refresh_from_export_operator("DSQ", self, context)


def _update_export_sequence_item(self, context):
    from .shared_export import apply_export_sequence_item_selection
    apply_export_sequence_item_selection(self)


IO_SCALE_DESCRIPTION = (
    "Uniform world-unit multiplier for translations, mesh size, bounds, "
    "radius, and ground travel. Rotations, sequence scale keys, UVs, and "
    "billboard settings are unchanged. Use the same value for DTS and DSQ"
)


def _context_scene(context):
    scene = getattr(context, "scene", None)
    if scene is not None:
        return scene
    window = getattr(context, "window", None)
    if window is not None:
        return getattr(window, "scene", None)
    return None


def _scene_io_scale(scene, direction):
    io = getattr(scene, "torque_io", None)
    if io is None:
        return 1.0
    if direction == "IMPORT":
        return io.import_scale
    return io.export_scale


def _store_scene_io_scale(scene, direction, scale):
    io = getattr(scene, "torque_io", None)
    if io is None:
        return
    value = float(scale)
    if direction == "IMPORT":
        io.import_scale = value
    else:
        io.export_scale = value


def _sync_scene_import_scale(self, context):
    _store_scene_io_scale(_context_scene(context), "IMPORT", self.scale)


def _sync_scene_export_scale(self, context):
    _store_scene_io_scale(_context_scene(context), "EXPORT", self.scale)


def _update_dts_export_scope(self, context):
    global _EXPORT_REFRESHING
    if _EXPORT_REFRESHING:
        if getattr(self, "export_scope", "SCENE") != "COLLECTION" and self.collection:
            self.collection = ""
        return
    _EXPORT_REFRESHING = True
    try:
        if getattr(self, "export_scope", "SCENE") != "COLLECTION" and self.collection:
            self.collection = ""
        if getattr(self, "export_scope", "SCENE") == "COLLECTIONS":
            from .shared_export import populate_export_collection_list
            scene = getattr(context, "scene", None)
            if scene is not None:
                populate_export_collection_list(self, scene)
        _refresh_from_export_operator_impl("DTS", self, context)
    finally:
        _EXPORT_REFRESHING = False


def _update_dsq_export_scope(self, context):
    if getattr(self, "export_scope", "SCENE") != "COLLECTION" and self.collection:
        self.collection = ""
    _refresh_from_export_operator("DSQ", self, context)


def _update_dts_collection(self, context):
    _refresh_from_export_operator("DTS", self, context)


def _update_dsq_collection(self, context):
    _refresh_from_export_operator("DSQ", self, context)


def _update_dts_show_all_actions(self, context):
    _refresh_from_export_operator("DTS", self, context)


def _update_dts_collection_page(self, context):
    _refresh_from_export_operator("DTS", _file_browser_export_operator(context), context)


def _update_dsq_show_all_actions(self, context):
    _refresh_from_export_operator("DSQ", self, context)


def _update_dts_strip_action_suffixes(self, context):
    _refresh_from_export_operator("DTS", self, context)


def _update_dsq_strip_action_suffixes(self, context):
    _refresh_from_export_operator("DSQ", self, context)


def _update_export_collection_item(self, context):
    _refresh_from_export_operator("DTS", _file_browser_export_operator(context), context)


class TorqueIOProperties(bpy.types.PropertyGroup):
    import_scale: FloatProperty(
        name="Import scale",
        description=IO_SCALE_DESCRIPTION,
        default=1.0,
        min=0.000001,
        soft_max=100.0)
    export_scale: FloatProperty(
        name="Export scale",
        description=IO_SCALE_DESCRIPTION,
        default=1.0,
        min=0.000001,
        soft_max=100.0)


class TorqueExportSequenceItem(bpy.types.PropertyGroup):
    name: StringProperty(name="Name")
    action_name: StringProperty(options={'HIDDEN'})
    kind: StringProperty(options={'HIDDEN'})
    source: StringProperty(default="ACTION", options={'HIDDEN'})
    is_selected: BoolProperty(
        name="Export",
        description="Include this sequence in the export file",
        default=True,
        update=_update_export_sequence_item)
    frame_start: IntProperty(options={'HIDDEN'})
    frame_end: IntProperty(options={'HIDDEN'})
    cyclic: BoolProperty(options={'HIDDEN'})
    blend: BoolProperty(options={'HIDDEN'})
    sync_export_flag: BoolProperty(default=True, options={'HIDDEN'})


class TorqueExportCollectionItem(bpy.types.PropertyGroup):
    name: StringProperty(name="Name")
    is_selected: BoolProperty(
        name="Export",
        description="Write a DTS for this collection",
        default=True,
        update=_update_export_collection_item)


class TorqueExportCollectionPage(bpy.types.PropertyGroup):
    name: StringProperty()
    show_all_actions: BoolProperty(
        name="Show All",
        description="List every Action on this page. Checked Actions still write into this collection's DTS",
        default=False,
        update=_update_dts_collection_page)
    action_list: CollectionProperty(type=TorqueExportSequenceItem)
    action_list_index: IntProperty()


class TorqueExportProperties(bpy.types.PropertyGroup):
    kind: StringProperty(options={'HIDDEN'})
    action_list: CollectionProperty(type=TorqueExportSequenceItem)
    action_list_index: IntProperty()
    marker_list: CollectionProperty(type=TorqueExportSequenceItem)
    marker_list_index: IntProperty()


class TORQUE_UL_export_sequences(bpy.types.UIList):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Same as Blender's default UIList: filter UI is open by default.
        self.use_filter_show = True

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        from .shared_export import export_sequence_item_tooltip
        tip = export_sequence_item_tooltip(item, context.scene)
        kind = getattr(data, "kind", "") or getattr(item, "kind", "DTS")
        mode = "MARKERS" if active_propname == "marker_list_index" else "ACTIONS"

        def _tip_op(target, text="", icon='NONE'):
            op = target.operator(
                "torque.export_sequence_info", text=text, icon=icon, emboss=False)
            op.tip = tip
            op.index = index
            op.kind = kind
            op.sequence_mode = mode
            return op

        # One child row: UIList otherwise splits checkbox / name / icons
        # into equal columns and leaves a gap after the check.
        row = layout.row(align=True)
        row.prop(item, "is_selected", text="", icon_only=True)
        name = row.row(align=True)
        name.alignment = 'LEFT'
        _tip_op(name, item.name)
        flags = row.row(align=True)
        flags.alignment = 'RIGHT'
        if item.cyclic:
            _tip_op(flags, icon='FILE_REFRESH')
        if item.blend:
            _tip_op(flags, icon='MODIFIER_ON')
        frames = abs(int(item.frame_end) - int(item.frame_start)) + 1
        _tip_op(flags, str(frames), 'KEYFRAME')

    def draw_filter(self, context, layout):
        # Bind to UIList.filter_name / use_filter_invert (DNA filter_byname /
        # UILST_FLT_EXCLUDE). Scene properties do not refresh File Browser
        # lists the way these do — see interface_template_list.cc + rna_ui.cc.
        row = layout.row()
        sub = row.row(align=True)
        sub.prop(self, "filter_name", text="", icon='VIEWZOOM')
        sub.prop(self, "use_filter_invert", text="", icon='ARROW_LEFTRIGHT')

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flags = []
        order = list(range(len(items)))
        if self.filter_name:
            # Invert is applied by Blender from use_filter_invert; do not
            # pass reverse= here or it double-inverts (see UI_list_item_...).
            flags = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, items, "name")
        if not flags:
            flags = [self.bitflag_filter_item] * len(items)
        return flags, order


class TORQUE_OT_export_sequence_info(bpy.types.Operator):
    """Hover target for export-list rows. Blender 4.3 UIList has no item tooltip;
    Operator.description(context, properties) is the RNA hook (rna_wm.cc)."""
    bl_idname = "torque.export_sequence_info"
    bl_label = "Sequence"
    bl_description = "Sequence export properties"
    bl_options = {'INTERNAL'}

    tip: StringProperty(options={'HIDDEN'})
    kind: StringProperty(options={'HIDDEN'})
    sequence_mode: StringProperty(default="ACTIONS", options={'HIDDEN'})
    index: IntProperty(options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        return properties.tip or "Sequence export properties"

    def execute(self, context):
        from .shared_export import (
            export_sequence_list_propnames, torque_export_pg)
        pg = torque_export_pg(context.scene, self.kind)
        _list_prop, index_prop = export_sequence_list_propnames(self.sequence_mode)
        setattr(pg, index_prop, self.index)
        return {'FINISHED'}


class TORQUE_OT_export_sequences_set_all(bpy.types.Operator):
    bl_idname = "torque.export_sequences_set_all"
    bl_label = "Select Sequences"
    bl_description = "Select or deselect every sequence in the export list"
    bl_options = {'INTERNAL'}

    kind: StringProperty()
    sequence_mode: StringProperty(default="ACTIONS")
    select: BoolProperty(default=True)

    def execute(self, context):
        from .shared_export import (
            current_export_collection_page, export_sequence_list_propnames,
            torque_export_pg)
        operator = _file_browser_export_operator(context)
        if (operator is not None
                and getattr(operator, "export_scope", "") == "COLLECTIONS"
                and (self.sequence_mode or "ACTIONS").upper() == "ACTIONS"):
            page = current_export_collection_page(operator)
            if page is None:
                return {'CANCELLED'}
            for item in page.action_list:
                item.is_selected = self.select
            return {'FINISHED'}
        pg = torque_export_pg(context.scene, self.kind)
        list_prop, _index_prop = export_sequence_list_propnames(self.sequence_mode)
        for item in getattr(pg, list_prop):
            item.is_selected = self.select
        return {'FINISHED'}


class TORQUE_UL_export_collections(bpy.types.UIList):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_filter_show = True

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "is_selected", text="", icon_only=True)
        row.label(text=item.name)

    def draw_filter(self, context, layout):
        row = layout.row()
        sub = row.row(align=True)
        sub.prop(self, "filter_name", text="", icon='VIEWZOOM')
        sub.prop(self, "use_filter_invert", text="", icon='ARROW_LEFTRIGHT')

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flags = []
        order = list(range(len(items)))
        if self.filter_name:
            flags = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, items, "name")
        if not flags:
            flags = [self.bitflag_filter_item] * len(items)
        return flags, order


class TORQUE_OT_export_collections_set_all(bpy.types.Operator):
    bl_idname = "torque.export_collections_set_all"
    bl_label = "Select Collections"
    bl_description = "Select or deselect every collection in the export list"
    bl_options = {'INTERNAL'}

    select: BoolProperty(default=True)

    def execute(self, context):
        operator = _file_browser_export_operator(context)
        items = getattr(operator, "collection_list", None) if operator is not None else None
        if items is None:
            return {'CANCELLED'}
        for item in items:
            item.is_selected = self.select
        return {'FINISHED'}


class TORQUE_OT_export_sequences_refresh(bpy.types.Operator):
    bl_idname = "torque.export_sequences_refresh"
    bl_label = "Refresh"
    bl_description = "Rebuild the sequence list from the current scene"
    bl_options = {'INTERNAL'}

    kind: StringProperty()
    sequence_mode: StringProperty(default="ACTIONS")
    select_marker: BoolProperty(default=False)
    export_scope: StringProperty(default="SCENE")
    collection: StringProperty(default="")
    show_all: BoolProperty(default=False)
    checked_collections: StringProperty(default="")

    def execute(self, context):
        from .shared_export import (
            checked_export_collection_names, export_sequence_collections,
            populate_export_collection_list, populate_export_collection_pages)
        scene = getattr(context, "scene", None)
        if scene is None:
            return {'CANCELLED'}
        names = [name for name in (self.checked_collections or "").split("\n") if name]
        if self.kind == "DTS" and self.export_scope == "COLLECTIONS":
            global _EXPORT_REFRESHING
            operator = _file_browser_export_operator(context)
            if operator is None or not hasattr(operator, "collection_list"):
                return {'CANCELLED'}
            if _EXPORT_REFRESHING:
                return {'CANCELLED'}
            _EXPORT_REFRESHING = True
            try:
                populate_export_collection_list(operator, scene)
                populate_export_collection_pages(operator, scene)
            finally:
                _EXPORT_REFRESHING = False
            return {'FINISHED'}
        operator = _file_browser_export_operator(context)
        _refresh_export_sequence_list(
            self.kind, context, self.sequence_mode, self.select_marker,
            collection_roots=export_sequence_collections(
                self.export_scope, self.collection, names),
            show_all=self.show_all,
            strip_action_suffixes=bool(
                getattr(operator, "strip_action_suffixes", True)))
        return {'FINISHED'}


class TORQUE_OT_export_collection_page(bpy.types.Operator):
    bl_idname = "torque.export_collection_page"
    bl_label = "Collection Page"
    bl_description = "Show Embed sequences for the previous or next checked collection"
    bl_options = {'INTERNAL'}

    delta: IntProperty(default=1)

    def execute(self, context):
        operator = _file_browser_export_operator(context)
        pages = getattr(operator, "collection_pages", None) if operator is not None else None
        if not pages:
            return {'CANCELLED'}
        count = len(pages)
        operator.collection_page_index = (
            operator.collection_page_index + self.delta) % count
        return {'FINISHED'}


def draw_export_collections_picker(layout, operator):
    box = layout.box()
    box.label(text="Collections", icon='OUTLINER_COLLECTION')
    box.label(text="Each checked collection writes its own DTS")
    row = box.row(align=True)
    row.label(text="Select")
    all_op = row.operator("torque.export_collections_set_all", text="All", icon='CHECKBOX_HLT')
    all_op.select = True
    none_op = row.operator("torque.export_collections_set_all", text="None", icon='CHECKBOX_DEHLT')
    none_op.select = False
    items = operator.collection_list
    rows = max(3, min(len(items), 12))
    box.template_list(
        "TORQUE_UL_export_collections", "",
        operator, "collection_list",
        operator, "collection_list_index", rows=rows)
    if items:
        selected = sum(1 for item in items if item.is_selected)
        box.label(text="{} of {} selected".format(selected, len(items)))


def draw_export_sequence_picker(layout, context, kind, sequence_mode, select_marker,
                               operator=None):
    from .shared_export import (
        checked_export_collection_names, current_export_collection_page,
        export_sequence_list_propnames, torque_export_pg)
    pg = torque_export_pg(context.scene, kind)
    scope = getattr(operator, "export_scope", "SCENE") if operator is not None else "SCENE"
    paged = (
        kind == "DTS"
        and scope == "COLLECTIONS"
        and (sequence_mode or "ACTIONS").upper() == "ACTIONS")
    page = current_export_collection_page(operator) if paged else None
    box = layout.box()
    if kind == "DSQ":
        box.label(text="Write sequences", icon='ACTION')
        box.label(text="Checked Actions are written into this DSQ")
    else:
        box.label(text="Embed sequences", icon='ACTION')
        box.label(text="Checked Actions are written into this DTS")
        box.label(text="Leave all unchecked for a mesh-only shape")

    if paged:
        pages = operator.collection_pages
        nav = box.row(align=True)
        prev = nav.row(align=True)
        prev.enabled = len(pages) > 1
        prev_op = prev.operator("torque.export_collection_page", text="", icon='TRIA_LEFT')
        prev_op.delta = -1
        if page is not None:
            nav.label(text="{}  {} of {}".format(
                page.name, operator.collection_page_index + 1, len(pages)))
        else:
            nav.label(text="No collections selected")
        nxt = nav.row(align=True)
        nxt.enabled = len(pages) > 1
        nxt_op = nxt.operator("torque.export_collection_page", text="", icon='TRIA_RIGHT')
        nxt_op.delta = 1

    row = box.row(align=True)
    refresh = row.operator("torque.export_sequences_refresh", text="Refresh", icon='FILE_REFRESH')
    refresh.kind = kind
    refresh.sequence_mode = sequence_mode
    refresh.select_marker = select_marker
    if operator is not None:
        refresh.export_scope = scope
        refresh.collection = getattr(operator, "collection", "") or ""
        refresh.show_all = bool(getattr(operator, "show_all_actions", False))
        refresh.checked_collections = "\n".join(checked_export_collection_names(operator))
        if (sequence_mode or "ACTIONS").upper() == "ACTIONS" and scope == "COLLECTION":
            row.prop(operator, "show_all_actions", text="Show All", toggle=True)
        elif paged and page is not None:
            row.prop(page, "show_all_actions", text="Show All", toggle=True)
        if (sequence_mode or "ACTIONS").upper() == "ACTIONS":
            box.prop(operator, "strip_action_suffixes", toggle=True)

    row = box.row(align=True)
    row.label(text="Select")
    all_op = row.operator("torque.export_sequences_set_all", text="All", icon='CHECKBOX_HLT')
    all_op.kind = kind
    all_op.sequence_mode = sequence_mode
    all_op.select = True
    none_op = row.operator("torque.export_sequences_set_all", text="None", icon='CHECKBOX_DEHLT')
    none_op.kind = kind
    none_op.sequence_mode = sequence_mode
    none_op.select = False

    if paged:
        items = page.action_list if page is not None else []
        if page is not None:
            rows = max(3, min(len(items), 12))
            box.template_list(
                "TORQUE_UL_export_sequences", "page",
                page, "action_list", page, "action_list_index", rows=rows)
    else:
        list_prop, index_prop = export_sequence_list_propnames(sequence_mode)
        items = getattr(pg, list_prop)
        rows = max(3, min(len(items), 12))
        box.template_list(
            "TORQUE_UL_export_sequences", kind,
            pg, list_prop, pg, index_prop, rows=rows)
    if items:
        selected = sum(1 for item in items if item.is_selected)
        box.label(text="{} of {} selected".format(selected, len(items)))
    elif sequence_mode == "MARKERS":
        box.label(text="No name:start / name:end markers")
    else:
        box.label(text="No Actions with pose or shape-key animation")


class ImportDTS(bpy.types.Operator, ImportHelper):
    """Load a Torque DTS File"""
    bl_idname = "import_scene.dts"
    bl_label = "Import DTS"
    bl_options = {'PRESET', 'UNDO'}
    filename_ext = ".dts"

    filter_glob: StringProperty(
        default="*.dts",
        options={'HIDDEN'},
        )
    files: CollectionProperty(
        name="File Path",
        type=OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'},
        )
    directory: StringProperty(
        subtype='DIR_PATH',
        options={'HIDDEN', 'SKIP_SAVE'},
        )

    collection_group: EnumProperty(
        name="Group in collection",
        description="Put each imported DTS in a collection named after the file",
        items=COLLECTION_GROUP_ITEMS,
        default="MULTI",
        )

    reference_keyframe: BoolProperty(
        name="Reference keyframe",
        description="Set a keyframe with the reference pose for blend animations",
        default=True,
        )

    import_sequences: BoolProperty(
        name="Import sequences",
        description="Automatically add keyframes for embedded sequences",
        default=True,
        )

    sequence_mode: EnumProperty(
        name="Sequence layout",
        description="How imported sequences are stored in Blender. Dummy-only shapes always use markers unless Force armature is on",
        items=SEQUENCE_MODE_ITEMS,
        default="ACTIONS",
        )

    use_armature: BoolProperty(
        name="Force armature",
        description="Import every node as an armature bone. Skinned DTS files already do this automatically; uncheck to import dummy-only shapes as empties",
        default=True,
        )

    scale: FloatProperty(
        name="Scale",
        description=IO_SCALE_DESCRIPTION,
        default=1.0,
        min=0.000001,
        soft_max=100.0,
        update=_sync_scene_import_scale,
        )

    log_level: EnumProperty(
        name="Log file",
        description="Write a timestamped import/export .txt next to the file. Full includes verts and animation tracks and can be large",
        items=LOG_LEVEL_ITEMS,
        default="DISABLED",
        )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Group in collection")
        layout.prop(self, "collection_group", expand=True)
        layout.prop(self, "reference_keyframe")
        layout.prop(self, "import_sequences")
        col = layout.column()
        col.enabled = self.import_sequences
        col.prop(self, "sequence_mode", expand=True)
        layout.prop(self, "use_armature")
        layout.prop(self, "scale")
        layout.prop(self, "log_level")

    def invoke(self, context, event):
        self.scale = _scene_io_scale(context.scene, "IMPORT")
        return super().invoke(context, event)

    def execute(self, context):
        from . import import_dts
        from .shared_export import (
            import_operator_filepaths, should_group_import_collection)

        _store_scene_io_scale(context.scene, "IMPORT", self.scale)
        paths = import_operator_filepaths(self)
        if not paths:
            self.report({'ERROR'}, "No DTS files selected")
            return {'CANCELLED'}

        keywords = self.as_keywords(ignore=(
            "filter_glob", "split_mode", "filepath", "files", "directory",
            "filename", "collection_group"))
        group = should_group_import_collection(self.collection_group, len(paths))
        for index, path in enumerate(paths):
            import_dts.load(
                self, context, filepath=path, group_in_collection=group,
                timeline_expand=index > 0, **keywords)
        return {'FINISHED'}

class ImportDSQ(bpy.types.Operator, ImportHelper):
    """Load a Torque DSQ File"""
    bl_idname = "import_scene.dsq"
    bl_label = "Import DSQ"
    bl_options = {'PRESET', 'UNDO'}
    filename_ext = ".dsq"

    filter_glob: StringProperty(
        default="*.dsq",
        options={'HIDDEN'},
        )
    files: CollectionProperty(
        name="File Path",
        type=OperatorFileListElement,
        options={'HIDDEN', 'SKIP_SAVE'},
        )
    directory: StringProperty(
        subtype='DIR_PATH',
        options={'HIDDEN', 'SKIP_SAVE'},
        )

    sequence_mode: EnumProperty(
        name="Sequence layout",
        description="How imported sequences are stored in Blender. Armatures can use either layout; empty rigs always use markers",
        items=SEQUENCE_MODE_ITEMS,
        default="ACTIONS",
        )

    scale: FloatProperty(
        name="Scale",
        description=IO_SCALE_DESCRIPTION,
        default=1.0,
        min=0.000001,
        soft_max=100.0,
        update=_sync_scene_import_scale,
        )

    log_level: EnumProperty(
        name="Log file",
        description="Write a timestamped import/export .txt next to the file. Full includes animation tracks and can be large",
        items=LOG_LEVEL_ITEMS,
        default="DISABLED",
        )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "sequence_mode", expand=True)
        layout.prop(self, "scale")
        layout.prop(self, "log_level")

    def invoke(self, context, event):
        self.scale = _scene_io_scale(context.scene, "IMPORT")
        return super().invoke(context, event)

    def execute(self, context):
        from . import import_dsq
        from .shared_export import import_operator_filepaths

        _store_scene_io_scale(context.scene, "IMPORT", self.scale)
        paths = import_operator_filepaths(self)
        if not paths:
            self.report({'ERROR'}, "No DSQ files selected")
            return {'CANCELLED'}

        keywords = self.as_keywords(ignore=(
            "filter_glob", "split_mode", "filepath", "files", "directory",
            "filename"))
        for index, path in enumerate(paths):
            import_dsq.load(
                self, context, filepath=path,
                timeline_expand=index > 0, **keywords)
        return {'FINISHED'}

class ExportDTS(bpy.types.Operator, ExportHelper):
    """Save a Torque DTS File"""

    bl_idname = "export_scene.dts"
    bl_label = 'Export DTS'
    bl_options = {'PRESET'}
    filename_ext = ".dts"
    check_extension = True

    filter_glob: StringProperty(
        default="*.dts",
        options={'HIDDEN'},
        )

    export_scope: EnumProperty(
        name="Export",
        description="What to include in this DTS file",
        items=EXPORT_SCOPE_ITEMS,
        default="SCENE",
        update=_update_dts_export_scope,
        )

    collection: StringProperty(
        name="Collection",
        description="Collection to export as this DTS. A detail or collision folder uses its parent shape collection when that parent is a DTS shape",
        default="",
        update=_update_dts_collection,
        )

    collection_list: CollectionProperty(type=TorqueExportCollectionItem)
    collection_list_index: IntProperty()
    collection_pages: CollectionProperty(type=TorqueExportCollectionPage)
    collection_page_index: IntProperty()

    show_all_actions: BoolProperty(
        name="Show All",
        description="List every Action, not only those mapped to this collection. Checked Actions still write; meshes and nodes stay scoped",
        default=False,
        update=_update_dts_show_all_actions,
        )

    strip_action_suffixes: BoolProperty(
        name="Strip suffixes",
        description="Remove Blender .001 and #2-style suffixes from written sequence names. Off keeps the Action name unchanged",
        default=True,
        update=_update_dts_strip_action_suffixes,
        )

    sequence_mode: EnumProperty(
        name="Sequence source",
        description="Export sequences from individual Actions, or from timeline markers and the Sequences text block",
        items=SEQUENCE_MODE_ITEMS,
        default="ACTIONS",
        update=_update_dts_export_sequences,
        )

    select_marker: BoolProperty(
        name="Pre-check selected markers",
        description="When using markers, start with only the selected timeline markers checked",
        default=False,
        update=_update_dts_export_sequences,
        )

    blank_material: BoolProperty(
        name="Blank material",
        description="Add a blank material to meshes with none assigned",
        default=True,
        )

    generate_texture: EnumProperty(
        name="Generate textures",
        description="Automatically generate solid color textures for materials",
        default="disabled",
        items=(
            ("disabled", "Disabled", "Do not generate any textures"),
            ("custom-missing", "Custom (if missing)", "Generate textures for non-default material names if not already present"),
            ("custom-always", "Custom (always)", "Generate textures for non-default material names"),
            ("all-missing", "All (if missing)", "Generate textures for all materials if not already present"),
            ("all-always", "All (always)", "Generate textures for all materials"))
        )

    apply_modifiers: BoolProperty(
        name="Apply modifiers",
        description="Apply modifiers to meshes",
        default=True,
        )

    optimize_meshes: BoolProperty(
        name="Optimize meshes",
        description="Weld matching vertices and convert triangles to strips, like the TGE exporters. Smaller DTS files",
        default=True,
        )

    scale: FloatProperty(
        name="Scale",
        description=IO_SCALE_DESCRIPTION,
        default=1.0,
        min=0.000001,
        soft_max=100.0,
        update=_sync_scene_export_scale,
        )

    log_level: EnumProperty(
        name="Log file",
        description="Write a timestamped import/export .txt next to the file. Full includes verts and animation tracks and can be large",
        items=LOG_LEVEL_ITEMS,
        default="DISABLED",
        )

    force_export: BoolProperty(
        name="Force Export",
        description="Write the DTS even if it violates a TGE limit (signed 16-bit primitive index, node/object count, or a skin with animated morphs). Those problems still fail in Show Tool / Blockland",
        default=False,
        )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "export_scope", text="Export")
        if self.export_scope == "COLLECTION":
            layout.prop_search(self, "collection", bpy.data, "collections", text="Collection")
        elif self.export_scope == "COLLECTIONS":
            draw_export_collections_picker(layout, self)
        layout.prop(self, "sequence_mode", expand=True)
        col = layout.column()
        col.enabled = self.sequence_mode == "MARKERS"
        col.prop(self, "select_marker")
        layout.prop(self, "blank_material")
        layout.prop(self, "generate_texture")
        layout.prop(self, "apply_modifiers")
        layout.prop(self, "optimize_meshes")
        layout.prop(self, "scale")
        layout.prop(self, "log_level")
        draw_export_sequence_picker(
            layout, context, "DTS", self.sequence_mode, self.select_marker, self)
        layout.prop(self, "force_export")

    def invoke(self, context, event):
        from .shared_export import populate_export_collection_list
        self.scale = _scene_io_scale(context.scene, "EXPORT")
        populate_export_collection_list(self, context.scene)
        _refresh_from_export_operator("DTS", self, context)
        return super().invoke(context, event)

    def execute(self, context):
        from . import export_dts
        from .shared_export import checked_export_collection_names
        _store_scene_io_scale(context.scene, "EXPORT", self.scale)
        keywords = self.as_keywords(ignore=(
            "check_existing", "filter_glob", "collection_list",
            "collection_list_index", "collection_pages",
            "collection_page_index"))
        keywords["collection_names"] = checked_export_collection_names(self)
        return export_dts.save(self, context, **keywords)

class ExportDSQ(bpy.types.Operator, ExportHelper):
    """Save many Torque DSQ Files"""

    bl_idname = "export_scene.dsq"
    bl_label = 'Export DSQ'
    bl_options = {'PRESET'}
    filename_ext = ".dsq"
    check_extension = True

    filter_glob: StringProperty(
        default="*.dsq",
        options={'HIDDEN'},
        )

    export_scope: EnumProperty(
        name="Export",
        description="Which objects supply bones and Actions for this DSQ",
        items=DSQ_EXPORT_SCOPE_ITEMS,
        default="SCENE",
        update=_update_dsq_export_scope,
        )

    collection: StringProperty(
        name="Collection",
        description="Collection whose armatures and meshes own the written sequences",
        default="",
        update=_update_dsq_collection,
        )

    show_all_actions: BoolProperty(
        name="Show All",
        description="List every Action, not only those mapped to this collection. Checked Actions still write; nodes stay scoped",
        default=False,
        update=_update_dsq_show_all_actions,
        )

    strip_action_suffixes: BoolProperty(
        name="Strip suffixes",
        description="Remove Blender .001 and #2-style suffixes from written sequence names. Off keeps the Action name unchanged",
        default=True,
        update=_update_dsq_strip_action_suffixes,
        )

    sequence_mode: EnumProperty(
        name="Sequence source",
        description="Export sequences from individual Actions, or from timeline markers and the Sequences text block",
        items=SEQUENCE_MODE_ITEMS,
        default="ACTIONS",
        update=_update_dsq_export_sequences,
        )

    select_marker: BoolProperty(
        name="Pre-check selected markers",
        description="When using markers, start with only the selected timeline markers checked",
        default=False,
        update=_update_dsq_export_sequences,
        )

    scale: FloatProperty(
        name="Scale",
        description=IO_SCALE_DESCRIPTION,
        default=1.0,
        min=0.000001,
        soft_max=100.0,
        update=_sync_scene_export_scale,
        )

    log_level: EnumProperty(
        name="Log file",
        description="Write a timestamped import/export .txt next to the file. Full includes animation tracks and can be large",
        items=LOG_LEVEL_ITEMS,
        default="DISABLED",
        )

    split_sequences: BoolProperty(
        name="One DSQ per sequence",
        description="Write each checked sequence to its own DSQ in the same folder. "
                    "The filename you pick only chooses the folder; each file is named after the sequence",
        default=False,
        )

    filename_prefix: StringProperty(
        name="File Prefix",
        description="Added in front of each split DSQ name, for example ae_ or m_",
        default="",
        )

    filename_suffix: StringProperty(
        name="File Suffix",
        description="Added after each split DSQ name, before .dsq",
        default="",
        )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "export_scope", text="Export")
        if self.export_scope == "COLLECTION":
            layout.prop_search(self, "collection", bpy.data, "collections", text="Collection")
        layout.prop(self, "sequence_mode", expand=True)
        col = layout.column()
        col.enabled = self.sequence_mode == "MARKERS"
        col.prop(self, "select_marker")
        layout.prop(self, "scale")
        layout.prop(self, "log_level")
        layout.prop(self, "split_sequences")
        col = layout.column()
        col.enabled = self.split_sequences
        col.prop(self, "filename_prefix")
        col.prop(self, "filename_suffix")
        draw_export_sequence_picker(
            layout, context, "DSQ", self.sequence_mode, self.select_marker, self)

    def invoke(self, context, event):
        self.scale = _scene_io_scale(context.scene, "EXPORT")
        _refresh_from_export_operator("DSQ", self, context)
        return super().invoke(context, event)

    def execute(self, context):
        from . import export_dsq
        _store_scene_io_scale(context.scene, "EXPORT", self.scale)
        keywords = self.as_keywords(ignore=("check_existing", "filter_glob"))
        return export_dsq.save(self, context, **keywords)

class SplitMeshIndex(bpy.types.Operator):
    """Split a mesh so each piece stays under TGE's DTS v24 draw limit"""

    bl_idname = "mesh.split_mesh_vindex"
    bl_label = "Split mesh by indices"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        ob = context.active_object
        return ob is not None and ob.type == "MESH"

    def execute(self, context):
        # One mesh can hold two S16 triangle lists (21844 tris).
        # Two-sided export writes a reverse winding, so it uses twice
        # the indices — half that budget.
        from .DtsTypes import TGE_MESH_MAX_PACKED_TRIS

        ob = context.active_object

        if ob is None or ob.type != "MESH":
            self.report({"ERROR"}, "Select a mesh object first")
            return {"FINISHED"}

        two_sided = any(
            slot.material is not None
            and getattr(slot.material, "torque_props", None)
            and slot.material.torque_props.two_sided
            for slot in ob.material_slots)
        limit = TGE_MESH_MAX_PACKED_TRIS // 2 if two_sided else TGE_MESH_MAX_PACKED_TRIS

        me = ob.data
        verts = [vert.co.copy() for vert in me.vertices]
        chunks = []
        current = []
        current_tris = 0

        for poly in me.polygons:
            tris = max(0, poly.loop_total - 2)
            if tris <= 0 or tris > limit:
                continue

            if current and current_tris + tris > limit:
                chunks.append(current)
                current = []
                current_tris = 0

            current.append(list(poly.vertices))
            current_tris += tris

        if current:
            chunks.append(current)

        for faces in chunks:
            out_me = bpy.data.meshes.new(ob.name)
            out_me.from_pydata(verts, [], faces)
            out_me.validate()
            out_me.update()
            out_ob = bpy.data.objects.new(ob.name, out_me)
            context.collection.objects.link(out_ob)

        return {"FINISHED"}

class TORQUE_OT_collapse_unused_mesh_parents(bpy.types.Operator):
    """Remove leftover mesh-only nodes (armor100) and parent the mesh to the real parent"""

    bl_idname = "torque.collapse_unused_mesh_parents"
    bl_label = "Collapse Unused Mesh Parents"
    bl_description = (
        "Remove nodes that only exist to hold a mesh "
        "(for example armor100 / test100) and parent the mesh to that node's parent. "
        "Import the DTS and any DSQs first so animated mesh parents are kept. "
        "Mounts and other nodes with no mesh are left alone. "
        "hideNode still uses the object name (armor), not armor100"
    )
    bl_options = {"REGISTER", "UNDO"}

    ignore_mesh_parent_animation: BoolProperty(
        name="Ignore parent animation",
        description="Also collapse mesh-only parents that have keyframes or actions. "
                    "Off keeps animated parents like an animated test100",
        default=False,
        )

    @classmethod
    def poll(cls, context):
        scene = getattr(context, "scene", None)
        if scene is None:
            return False
        return any(ob.type in {"ARMATURE", "EMPTY"} for ob in scene.objects)

    def execute(self, context):
        from .shared_export import collapse_unused_mesh_parents_in_scene
        collapsed = collapse_unused_mesh_parents_in_scene(
            context, self.ignore_mesh_parent_animation)
        if not collapsed:
            self.report({"INFO"}, "Nothing to collapse")
            return {"FINISHED"}
        self.report(
            {"INFO"},
            "Collapsed {} unused mesh parents".format(len(collapsed)))
        return {"FINISHED"}


class HideBlockheadNodes(bpy.types.Operator):
    """Set all non-default Blockhead model apparel meshes as hidden"""

    bl_idname = "mesh.hide_blockhead_nodes"
    bl_label = "Hide Blockhead nodes on selection"
    bl_options = {"REGISTER", "UNDO"}

    blacklist = (
        "copHat",
        "knitHat",
        "pack",
        "quiver",
        "femChest",
        "epauletsRankB",
        "epauletsRankC",
        "epauletsRankD",
        "epauletsRankA",
        "skirtHip",
        "skirtTrimRight",
        "RHook",
        "RarmSlim",
        "LHook",
        "LarmSlim",
        "PointyHelmet",
        "Helmet",
        "bicorn",
        "scoutHat",
        "FlareHelmet",
        "triPlume",
        "plume",
        "septPlume",
        "tank",
        "armor",
        "cape",
        "Bucket",
        "epaulets",
        "ShoulderPads",
        "Rski",
        "Rpeg",
        "Lski",
        "Lpeg",
        "skirtTrimLeft",
        "Visor",
    )

    def execute(self, context):
        for ob in context.scene.objects:
            if ob.select_get() and ob.type == "MESH" and ob.name in self.blacklist:
                ob.hide_set(True)

        return {"FINISHED"}

def _update_env_mapping(self, context):
    self.never_env_map = self.env_mapping <= 0.0


class TorqueMaterialProperties(bpy.types.PropertyGroup):
    blend_mode: EnumProperty(
        name="Blend mode",
        items=(
            ("ADDITIVE", "Additive",
             "Add the texture on top of the scene. "
             "Glow, fire, lasers, muzzle flash. Black adds nothing"),
            ("SUBTRACTIVE", "Subtractive",
             "Darken the scene by the texture. "
             "Smoke, shadows, dark stains"),
            ("NONE", "Filter",
             "Standard alpha blend (previously called None). "
             "Texture alpha shows what is behind. Glass, windows, faded decals"),
        ),
        default="ADDITIVE")
    s_wrap: BoolProperty(name="S-Wrap", default=True)
    t_wrap: BoolProperty(name="T-Wrap", default=True)
    never_env_map: BoolProperty(name="Never Env Map", default=True)
    two_sided: BoolProperty(
        name="Two-sided",
        description="Export a reversed copy of faces (do not use Blender backface culling; that defaults off)",
        default=False)
    use_ifl: BoolProperty(name="IFL")
    use_transparency: BoolProperty(name="Use Transparency")
    use_shadeless: BoolProperty(name="Shadeless")
    ifl_name: StringProperty(name="Name")
    no_mip_mapping: BoolProperty(name="No Mip Mapping", default=False)
    mip_map_zero_border: BoolProperty(name="Mip Map Zero Border", default=False)
    detail_map: StringProperty(
        name="Detail Map",
        description="Name of the Blender material to use as a detail map. "
                    "That texture is scaled by Detail Scale, then blended with the base. "
                    "Leave empty for none")
    bump_map: StringProperty(
        name="Bump Map",
        description="Name of the Blender material to use as a bump map. "
                    "TGE does not support bumpmapped DTS shapes")
    reflectance_map: StringProperty(
        name="Reflectance Map",
        description="Name of the Blender material to use as a reflectance map. "
                    "Leave empty to use the base texture alpha for environment mapping")
    detail_scale: FloatProperty(
        name="Detail Scale",
        description="Scale of the detail map before it is blended with the base material. "
                    "1 keeps the detail texture the same size as the base",
        default=1.0,
        min=0.0,
        soft_max=16.0)
    env_mapping: FloatProperty(
        name="Environment Mapping",
        description="Amount of environment mapping to apply. 0 for none. "
                    "This 0–1 scale is applied to the texture alpha at each point",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype='FACTOR',
        update=_update_env_mapping)

class TorqueMaterialPanel(bpy.types.Panel):
    bl_idname = "MATERIAL_PT_torque"
    bl_label = "Torque Material"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "material"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return (context.material is not None)

    def draw(self, context):
        layout = self.layout
        obj = context.material

        sublayout = layout.row()
        sublayout.prop(obj.torque_props, "use_shadeless")

        sublayout = layout.row()
        sublayout.prop(obj.torque_props, "use_transparency")
        sublayout = layout.row()
        sublayout.enabled = obj.torque_props.use_transparency
        sublayout.prop(obj.torque_props, "blend_mode", expand=True)

        sublayout = layout.row()
        sublayout.prop(obj.torque_props, "t_wrap")

        sublayout = layout.row()
        sublayout.prop(obj.torque_props, "s_wrap")

        sublayout = layout.row()
        sublayout.prop(obj.torque_props, "two_sided")

        row = layout.row()
        row.prop(obj.torque_props, "use_ifl")
        sublayout = row.column()
        sublayout.enabled = obj.torque_props.use_ifl
        sublayout.prop(obj.torque_props, "ifl_name", text="")
        sublayout = layout.column()
        sublayout.enabled = obj.torque_props.use_ifl

        row = layout.row()
        sublayout = row.column()
        sublayout.prop(obj.torque_props, "no_mip_mapping")
        sublayout = row.column()
        sublayout.enabled = not obj.torque_props.no_mip_mapping
        sublayout.prop(obj.torque_props, "mip_map_zero_border")

        box = layout.box()
        box.label(text="Extra Maps")
        box.prop_search(obj.torque_props, "detail_map", bpy.data, "materials", text="Detail Map")
        box.prop(obj.torque_props, "detail_scale")
        box.prop_search(obj.torque_props, "bump_map", bpy.data, "materials", text="Bump Map")
        box.prop_search(obj.torque_props, "reflectance_map", bpy.data, "materials", text="Reflectance Map")
        box.prop(obj.torque_props, "env_mapping")

class TorqueSequenceTrigger(bpy.types.PropertyGroup):
    state: IntProperty(
        name="State",
        description="Positive = on, negative = off (TGE trigger bits 1–31)",
        default=1,
        min=-31,
        max=31)
    frame: IntProperty(name="Frame", default=0)


class TORQUE_UL_seq_triggers(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "state", text="State")
        row.prop(item, "frame", text="Frame")


class TORQUE_OT_seq_trigger_add(bpy.types.Operator):
    bl_idname = "torque.seq_trigger_add"
    bl_label = "Add Trigger"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        action = getattr(context, "active_action", None)
        if action is None:
            return {'CANCELLED'}
        props = action.torque_seq_props
        props.triggers.add()
        props.active_trigger = len(props.triggers) - 1
        return {'FINISHED'}


class TORQUE_OT_seq_trigger_remove(bpy.types.Operator):
    bl_idname = "torque.seq_trigger_remove"
    bl_label = "Remove Trigger"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        action = getattr(context, "active_action", None)
        if action is None:
            return {'CANCELLED'}
        props = action.torque_seq_props
        if not props.triggers:
            return {'CANCELLED'}
        index = min(max(props.active_trigger, 0), len(props.triggers) - 1)
        props.triggers.remove(index)
        props.active_trigger = min(index, len(props.triggers) - 1)
        return {'FINISHED'}


def _update_override_duration(self, context):
    if not self.override_duration or self.duration > 0:
        return
    action = getattr(context, "active_action", None)
    if action is None:
        return
    from .shared_export import action_frame_span, sequence_duration_seconds
    frame_start, frame_end = action_frame_span(action)
    frame_range = int(frame_end) - int(frame_start) + 1
    scene_fps = context.scene.render.fps / context.scene.render.fps_base
    self.duration = sequence_duration_seconds(
        frame_range, scene_fps, 0, self.frame_rate)


class TorqueSequenceProperties(bpy.types.PropertyGroup):
    collection: PointerProperty(
        name="Collection",
        type=bpy.types.Collection,
        description="This Action belongs to this collection. Collection export only lists Actions mapped here")
    export: BoolProperty(
        name="Export Sequence",
        description="Show this Action on the DTS/DSQ export lists. Off hides dummy or scratch Actions",
        default=True)
    export_dts: BoolProperty(
        name="Include in DTS",
        description="Write this Action into the DTS when Embed sequences is checked. Leave off for DSQ-only clips",
        default=True)
    export_dsq: BoolProperty(
        name="Include in DSQ",
        description="Write this Action into the DSQ when Write sequences is checked",
        default=True)
    blend: BoolProperty(name="Blend", default=False)
    blend_ref_frame: IntProperty(
        name="Blend Reference Frame",
        description="Frame whose pose is subtracted when Blend is on. "
                    "0 uses rest (or a scene 'reference' marker outside this Action). "
                    "DTS/DSQ files do not store this number",
        default=0)
    priority: IntProperty(name="Priority", default=1, min=0)
    override_duration: BoolProperty(
        name="Override Duration",
        description="Force a play time in seconds instead of Action length / FPS",
        default=False,
        update=_update_override_duration)
    duration: FloatProperty(
        name="Duration",
        description="Seconds the sequence plays in-game when Override Duration is on",
        default=0.0,
        min=0.0)
    frame_rate: FloatProperty(
        name="Frame Rate",
        description="Leave 0 to use the scene FPS. Import sets this so in-game speed matches the DTS",
        default=0.0,
        min=0.0)
    ignore_ground: BoolProperty(name="Ignore Ground", default=False)
    ground_frame_rate: FloatProperty(
        name="Ground Frame Rate",
        description="0 samples every sequence frame",
        default=0.0,
        min=0.0)
    enable_transform: BoolProperty(name="Enable Transform", default=True)
    enable_scale: BoolProperty(name="Enable Scale", default=True)
    enable_vis: BoolProperty(name="Enable Visibility", default=True)
    enable_ifl: BoolProperty(name="Enable IFL", default=True)
    enable_morph: BoolProperty(
        name="Enable Morph",
        description="Bake shape-key / vertex animation on rigid meshes when "
                    "keys exist or verts move. Skins cannot morph in TGE. DTS only",
        default=True)
    force_transform: BoolProperty(
        name="Force Transform",
        description="Write rest (or the current pose) on every node, even without keys. "
                    "Import turns this on for empty non-blend root/land so Player "
                    "transitions have dest keys. Uncheck to write a stock-empty clip",
        default=False)
    force_scale: BoolProperty(name="Force Scale", default=False)
    force_vis: BoolProperty(name="Force Visibility", default=False)
    force_morph: BoolProperty(
        name="Force Morph",
        description="Write mesh frames for every rigid object in this sequence, "
                    "even without shape-key keys. Skins are still skipped",
        default=False)
    triggers: CollectionProperty(type=TorqueSequenceTrigger)
    active_trigger: IntProperty(name="Active Trigger", default=0)


class DOPESHEET_PT_torque_sequence(bpy.types.Panel):
    bl_space_type = 'DOPESHEET_EDITOR'
    bl_region_type = 'UI'
    bl_category = "Action"
    bl_label = "Torque Sequence"
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return bool(getattr(context, "active_action", None))

    def draw(self, context):
        action = context.active_action
        props = action.torque_seq_props
        layout = self.layout
        layout.prop_search(props, "collection", bpy.data, "collections", text="Collection")
        layout.prop(props, "export")
        col = layout.column(align=True)
        col.enabled = props.export
        col.prop(props, "export_dts")
        col.prop(props, "export_dsq")
        layout.prop(props, "blend")
        row = layout.row()
        row.enabled = props.blend
        row.prop(props, "blend_ref_frame")
        layout.prop(props, "priority")
        layout.prop(props, "override_duration")
        if props.override_duration:
            layout.prop(props, "duration")
        layout.prop(props, "frame_rate")
        from .shared_export import (
            action_frame_span, sequence_duration_seconds, sequence_frame_rate)
        frame_start, frame_end = action_frame_span(action)
        frame_range = int(frame_end) - int(frame_start) + 1
        scene_fps = context.scene.render.fps / context.scene.render.fps_base
        duration_override = props.duration if props.override_duration else 0
        seconds = sequence_duration_seconds(
            frame_range, scene_fps, duration_override, props.frame_rate)
        rate = sequence_frame_rate(
            frame_range, scene_fps, duration_override, props.frame_rate)
        if props.override_duration and props.duration > 0:
            layout.label(text="Plays in {:.3g}s (override)".format(seconds))
        else:
            layout.label(text="Plays in {:.3g}s at {:.4g} fps".format(seconds, rate))
        layout.prop(props, "ignore_ground")
        layout.prop(props, "ground_frame_rate")

        col = layout.column(align=True)
        col.label(text="Enable")
        row = col.row(align=True)
        row.prop(props, "enable_transform", text="Transform")
        row.prop(props, "enable_scale", text="Scale")
        row.prop(props, "enable_vis", text="Vis")
        row.prop(props, "enable_ifl", text="IFL")
        row.prop(props, "enable_morph", text="Morph")
        col.label(text="Force")
        row = col.row(align=True)
        row.prop(props, "force_transform", text="Transform")
        row.prop(props, "force_scale", text="Scale")
        row.prop(props, "force_vis", text="Vis")
        row.prop(props, "force_morph", text="Morph")

        layout.label(text="Triggers")
        row = layout.row()
        row.template_list(
            "TORQUE_UL_seq_triggers", "",
            props, "triggers",
            props, "active_trigger",
            rows=2)
        col = row.column(align=True)
        col.operator("torque.seq_trigger_add", icon='ADD', text="")
        col.operator("torque.seq_trigger_remove", icon='REMOVE', text="")


class TorqueVisProperties(bpy.types.PropertyGroup):
    # Kept so older blends with torque_vis_props.vis_value keys still evaluate.
    vis_value: FloatProperty(
        name="Visibility", default=1, min=0, max=1, options={'ANIMATABLE'})


class TorqueAutoBillboardProperties(bpy.types.PropertyGroup):
    enabled: BoolProperty(
        name="Auto-billboard LOD",
        description="Export this empty as a TGE auto-billboard (impostor) detail. "
                    "The engine snapshots the shape on load. Not a mesh BB/BBZ",
        default=False)
    size: IntProperty(
        name="LOD size",
        description="Screen-pixel size where this impostor replaces the meshes. "
                    "Max/Plus default is 2",
        default=2,
        min=0,
        max=2048)
    equator_steps: IntProperty(
        name="Equator steps",
        description="Side views captured around the shape (TGE BB::EQUATOR_STEPS)",
        default=4,
        min=1,
        max=127)
    polar_steps: IntProperty(
        name="Polar steps",
        description="Views from equator toward each pole. 0 is equator only "
                    "(TGE BB::POLAR_STEPS)",
        default=0,
        min=0,
        max=63)
    polar_angle: FloatProperty(
        name="Polar angle",
        description="Latitude in degrees where pole snapshots take over "
                    "(TGE BB::POLAR_ANGLE)",
        default=36.0,
        min=0.0,
        max=90.0)
    snapshot_detail: IntProperty(
        name="Snapshot detail",
        description="Which mesh LOD to photograph, 0 = largest (TGE BB::DL)",
        default=0,
        min=0,
        max=15)
    dimension: IntProperty(
        name="Texture size",
        description="Snapshot resolution in pixels (TGE BB::DIM)",
        default=64,
        min=1,
        max=255)
    include_poles: BoolProperty(
        name="Include poles",
        description="Capture extra top and bottom views (TGE BB::INCLUDE_POLES)",
        default=True)


class TorqueAutoBillboardPanel(bpy.types.Panel):
    bl_idname = "EMPTY_PT_torque_auto_billboard"
    bl_label = "Torque Auto-Billboard"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        ob = context.view_layer.objects.active
        return ob is not None and ob.type == "EMPTY"

    def draw_header(self, context):
        self.layout.prop(context.object.torque_bb_props, "enabled", text="")

    def draw(self, context):
        props = context.object.torque_bb_props
        layout = self.layout
        layout.use_property_split = True
        layout.operator("torque.add_auto_billboard", text="Setup Selected Empty")
        name = context.object.name.split("#", 1)[0]
        from .shared_export import classify_detail_marker
        named = classify_detail_marker(name)[0] == "bb_detail"
        if named and not props.enabled:
            layout.label(text="Exports from the BB::detail name")
        col = layout.column()
        col.enabled = props.enabled or named
        col.prop(props, "size")
        col.prop(props, "equator_steps")
        col.prop(props, "polar_steps")
        col.prop(props, "polar_angle")
        col.prop(props, "snapshot_detail")
        col.prop(props, "dimension")
        col.prop(props, "include_poles")


class TORQUE_OT_add_auto_billboard(bpy.types.Operator):
    bl_idname = "torque.add_auto_billboard"
    bl_label = "Setup Auto-Billboard"
    bl_description = "Mark the selected empty as a TGE impostor LOD: enable settings and name it BB::detailN"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if any(ob.type == "EMPTY" for ob in context.selected_objects):
            return True
        ob = context.object
        return ob is not None and ob.type == "EMPTY"

    def execute(self, context):
        from .shared_export import setup_auto_billboard_empty
        targets = [ob for ob in context.selected_objects if ob.type == "EMPTY"]
        if not targets and context.object is not None and context.object.type == "EMPTY":
            targets = [context.object]
        count = 0
        for ob in targets:
            if setup_auto_billboard_empty(ob):
                count += 1
        if not count:
            self.report({'ERROR'}, "Select an empty to set up as an auto-billboard")
            return {'CANCELLED'}
        if count == 1:
            self.report({'INFO'}, "Set up '{}' as an auto-billboard".format(targets[0].name))
        else:
            self.report({'INFO'}, "Set up {} auto-billboard empties".format(count))
        return {'FINISHED'}

def _torque_morph_mesh_poll(_self, ob):
    return ob is not None and ob.type == "MESH" and ob.data is not None


def _torque_morph_bind_update(self, context):
    from .shared_export import (
        capture_bone_morph_init_value, schedule_bone_morph_driver_sync)
    capture_bone_morph_init_value(self)
    schedule_bone_morph_driver_sync(context.scene, self)


class TorqueBoneMorphBind(bpy.types.PropertyGroup):
    mesh: PointerProperty(
        name="Mesh",
        description="Mesh whose shape key this bone property drives",
        type=bpy.types.Object,
        poll=_torque_morph_mesh_poll,
        update=_torque_morph_bind_update)
    shape_key: StringProperty(
        name="Shape key",
        description="Shape key driven by this bone's custom property. "
                    "The property uses the same name and is keyed on the armature Action",
        update=_torque_morph_bind_update)
    driver_mesh: StringProperty(options={'HIDDEN'})
    driver_key: StringProperty(options={'HIDDEN'})


class TORQUE_OT_bone_morph_add(bpy.types.Operator):
    bl_idname = "torque.bone_morph_add"
    bl_label = "Add Morph Proxy"
    bl_description = "Drive a mesh shape key from a custom property on this bone"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_pose_bone is not None

    def execute(self, context):
        bone = context.active_pose_bone
        bind = bone.torque_morph_binds.add()
        bone.torque_morph_bind_index = len(bone.torque_morph_binds) - 1
        for ob in context.selected_objects:
            if ob.type == "MESH":
                bind.mesh = ob
                break
        return {'FINISHED'}


class TORQUE_OT_bone_morph_remove(bpy.types.Operator):
    bl_idname = "torque.bone_morph_remove"
    bl_label = "Remove Morph Proxy"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        bone = context.active_pose_bone
        return bone is not None and len(bone.torque_morph_binds) > 0

    def execute(self, context):
        bone = context.active_pose_bone
        index = int(self.index)
        if index < 0 or index >= len(bone.torque_morph_binds):
            return {'CANCELLED'}
        from .shared_export import clear_bone_morph_bind_driver
        clear_bone_morph_bind_driver(
            context.scene, bone.torque_morph_binds[index], bone)
        bone.torque_morph_binds.remove(index)
        bone.torque_morph_bind_index = min(index, len(bone.torque_morph_binds) - 1)
        return {'FINISHED'}


class TORQUE_OT_bone_morph_connect(bpy.types.Operator):
    bl_idname = "torque.bone_morph_connect"
    bl_label = "Connect Morph Driver"
    bl_description = "Attach a Key datablock driver from this bone property to the shape key"
    bl_options = {'REGISTER', 'UNDO'}

    index: IntProperty(options={'HIDDEN'})

    @classmethod
    def poll(cls, context):
        return context.active_pose_bone is not None

    def execute(self, context):
        bone = context.active_pose_bone
        index = int(self.index)
        if bone is None or index < 0 or index >= len(bone.torque_morph_binds):
            return {'CANCELLED'}
        from .shared_export import (
            capture_bone_morph_init_value, schedule_bone_morph_driver_sync)
        bind = bone.torque_morph_binds[index]
        capture_bone_morph_init_value(bind)
        schedule_bone_morph_driver_sync(context.scene, bind)
        return {'FINISHED'}


class TorqueBoneMorphPanel(bpy.types.Panel):
    bl_idname = "BONE_PT_torque_morph"
    bl_label = "Torque Morph Proxy"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "bone"

    @classmethod
    def poll(cls, context):
        return context.active_pose_bone is not None

    def draw(self, context):
        bone = context.active_pose_bone
        layout = self.layout
        layout.use_property_split = True
        layout.operator("torque.bone_morph_add", icon='ADD', text="Add")
        if not bone.torque_morph_binds:
            layout.label(text="Drive a shape key from this bone on the armature Action")
            layout.label(text="Name the bone Dummy* to skip it as a DTS node")
            return
        for index, bind in enumerate(bone.torque_morph_binds):
            box = layout.box()
            header = box.row()
            header.label(text=bind.shape_key or "Morph proxy")
            remove = header.operator(
                "torque.bone_morph_remove", text="", icon='X', emboss=False)
            remove.index = index
            box.prop(bind, "mesh")
            keys = None
            if bind.mesh is not None and bind.mesh.data is not None:
                keys = bind.mesh.data.shape_keys
            if keys is not None:
                box.prop_search(bind, "shape_key", keys, "key_blocks", text="Shape key")
            else:
                box.prop(bind, "shape_key")
            if bind.shape_key and bind.shape_key in bone:
                box.prop(bone, '["{}"]'.format(bind.shape_key), text="Value")
            if (bind.mesh is not None and bind.shape_key
                    and (bind.driver_mesh != bind.mesh.name
                         or bind.driver_key != bind.shape_key)):
                connect = box.operator(
                    "torque.bone_morph_connect", text="Connect driver")
                connect.index = index


class TorqueVisPanel(bpy.types.Panel):
    bl_idname = "EMPTY_PT_torque_vis"
    bl_label = "Torque Visibility"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "object"

    @classmethod
    def poll(cls, context):
        ob = context.view_layer.objects.active
        if ob is None:
            return False
        if ob.type == "EMPTY":
            return True
        from .shared_export import is_bounds_name
        if ob.type != "MESH" or is_bounds_name(ob.name):
            return False
        # Skins and unparented meshes author vis here. Bone-parented
        # pieces use the bone slider; empty-parented pieces use the empty.
        if ob.parent is None:
            return True
        return ob.parent.type == "ARMATURE" and ob.parent_type == "OBJECT"

    def draw(self, context):
        # Direct Object property. A PropertyGroup always has an RNA name
        # that getname_anim_fcurve prints in parens; empty name → "Visibility ()".
        layout = self.layout
        layout.use_property_split = True
        layout.prop(context.view_layer.objects.active, "torque_vis_value")


class TorqueBoneVisPanel(bpy.types.Panel):
    bl_idname = "BONE_PT_torque_vis"
    bl_label = "Torque Visibility"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "bone"

    @classmethod
    def poll(cls, context):
        return context.active_pose_bone is not None

    def draw(self, context):
        # Draw the float on the PoseBone itself. A nested PropertyGroup
        # has no RNA path from the Object ID through a pose bone
        # (blender 4.3.2 rna_path.cc, rna_path_from_ID_to_idpgroup).
        layout = self.layout
        layout.use_property_split = True
        layout.prop(context.active_pose_bone, "torque_vis_value")


def menu_func_import_dts(self, context):
    self.layout.operator(ImportDTS.bl_idname, text="Torque (.dts)")

def menu_func_import_dsq(self, context):
    self.layout.operator(ImportDSQ.bl_idname, text="Torque Sequences (.dsq)")

def menu_func_export_dts(self, context):
    self.layout.operator(ExportDTS.bl_idname, text="Torque (.dts)")

def menu_func_export_dsq(self, context):
    self.layout.operator(ExportDSQ.bl_idname, text="Torque Sequences (.dsq)")

def menu_func_split_mesh_index(self, context):
    self.layout.operator(SplitMeshIndex.bl_idname, text="Split mesh by indices (DTS)")


def menu_func_collapse_unused_mesh_parents(self, context):
    self.layout.operator(
        TORQUE_OT_collapse_unused_mesh_parents.bl_idname,
        text="Collapse Unused Mesh Parents")

def register():
    bpy.utils.register_class(TorqueIOProperties)
    bpy.utils.register_class(TorqueExportSequenceItem)
    bpy.utils.register_class(TorqueExportCollectionItem)
    bpy.utils.register_class(TorqueExportCollectionPage)
    bpy.utils.register_class(TorqueExportProperties)
    bpy.utils.register_class(TORQUE_UL_export_sequences)
    bpy.utils.register_class(TORQUE_UL_export_collections)
    bpy.utils.register_class(TORQUE_OT_export_sequence_info)
    bpy.utils.register_class(TORQUE_OT_export_sequences_set_all)
    bpy.utils.register_class(TORQUE_OT_export_collections_set_all)
    bpy.utils.register_class(TORQUE_OT_export_collection_page)
    bpy.utils.register_class(TORQUE_OT_export_sequences_refresh)
    bpy.utils.register_class(ImportDTS)
    bpy.utils.register_class(ImportDSQ)
    bpy.utils.register_class(ExportDTS)
    bpy.utils.register_class(ExportDSQ)
    bpy.utils.register_class(SplitMeshIndex)
    bpy.utils.register_class(TORQUE_OT_collapse_unused_mesh_parents)
    bpy.utils.register_class(HideBlockheadNodes)
    bpy.utils.register_class(TorqueMaterialProperties)
    bpy.utils.register_class(TorqueMaterialPanel)
    bpy.utils.register_class(TorqueVisProperties)
    bpy.utils.register_class(TorqueVisPanel)
    bpy.utils.register_class(TorqueBoneVisPanel)
    bpy.utils.register_class(TorqueBoneMorphBind)
    bpy.utils.register_class(TORQUE_OT_bone_morph_add)
    bpy.utils.register_class(TORQUE_OT_bone_morph_remove)
    bpy.utils.register_class(TORQUE_OT_bone_morph_connect)
    bpy.utils.register_class(TorqueBoneMorphPanel)
    bpy.utils.register_class(TorqueAutoBillboardProperties)
    bpy.utils.register_class(TorqueAutoBillboardPanel)
    bpy.utils.register_class(TORQUE_OT_add_auto_billboard)
    bpy.utils.register_class(TorqueSequenceTrigger)
    bpy.utils.register_class(TORQUE_UL_seq_triggers)
    bpy.utils.register_class(TORQUE_OT_seq_trigger_add)
    bpy.utils.register_class(TORQUE_OT_seq_trigger_remove)
    bpy.utils.register_class(TorqueSequenceProperties)
    bpy.utils.register_class(DOPESHEET_PT_torque_sequence)

    bpy.types.Material.torque_props = PointerProperty(type=TorqueMaterialProperties)

    bpy.types.Object.torque_vis_props = PointerProperty(type=TorqueVisProperties)
    bpy.types.Object.torque_bb_props = PointerProperty(type=TorqueAutoBillboardProperties)
    bpy.types.Object.torque_vis_value = FloatProperty(
        name="Visibility",
        default=1,
        min=0,
        max=1,
        options={'ANIMATABLE'})
    bpy.types.PoseBone.torque_vis_value = FloatProperty(
        name="Visibility",
        default=1,
        min=0,
        max=1,
        options={'ANIMATABLE'})
    bpy.types.PoseBone.torque_morph_binds = CollectionProperty(type=TorqueBoneMorphBind)
    bpy.types.PoseBone.torque_morph_bind_index = IntProperty(default=0)
    bpy.types.Action.torque_seq_props = PointerProperty(type=TorqueSequenceProperties)
    bpy.types.Scene.torque_io = PointerProperty(type=TorqueIOProperties)
    bpy.types.Scene.torque_dts_export = PointerProperty(type=TorqueExportProperties)
    bpy.types.Scene.torque_dsq_export = PointerProperty(type=TorqueExportProperties)
    mount_preview.register()

    bpy.types.TOPBAR_MT_file_import.append(menu_func_import_dts)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import_dsq)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export_dts)
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export_dsq)
    bpy.types.VIEW3D_MT_object.append(menu_func_split_mesh_index)
    bpy.types.VIEW3D_MT_object.append(menu_func_collapse_unused_mesh_parents)
    bpy.types.VIEW3D_MT_edit_mesh.append(menu_func_split_mesh_index)

def unregister():
    bpy.utils.unregister_class(ImportDTS)
    bpy.utils.unregister_class(ImportDSQ)
    bpy.utils.unregister_class(ExportDTS)
    bpy.utils.unregister_class(ExportDSQ)
    bpy.utils.unregister_class(SplitMeshIndex)
    bpy.utils.unregister_class(HideBlockheadNodes)
    bpy.utils.unregister_class(TORQUE_OT_collapse_unused_mesh_parents)
    bpy.utils.unregister_class(TORQUE_OT_add_auto_billboard)
    bpy.utils.unregister_class(TorqueAutoBillboardPanel)
    bpy.utils.unregister_class(TorqueBoneMorphPanel)
    bpy.utils.unregister_class(TORQUE_OT_bone_morph_connect)
    bpy.utils.unregister_class(TORQUE_OT_bone_morph_remove)
    bpy.utils.unregister_class(TORQUE_OT_bone_morph_add)
    bpy.utils.unregister_class(TorqueBoneVisPanel)
    bpy.utils.unregister_class(TorqueVisPanel)
    bpy.utils.unregister_class(TorqueMaterialPanel)
    bpy.utils.unregister_class(DOPESHEET_PT_torque_sequence)
    bpy.utils.unregister_class(TORQUE_OT_seq_trigger_remove)
    bpy.utils.unregister_class(TORQUE_OT_seq_trigger_add)
    bpy.utils.unregister_class(TORQUE_UL_seq_triggers)

    del bpy.types.Material.torque_props
    del bpy.types.Object.torque_vis_props
    if hasattr(bpy.types.Object, "torque_bb_props"):
        del bpy.types.Object.torque_bb_props
    if hasattr(bpy.types.Object, "torque_vis_value"):
        del bpy.types.Object.torque_vis_value
    if hasattr(bpy.types.PoseBone, "torque_vis_value"):
        del bpy.types.PoseBone.torque_vis_value
    if hasattr(bpy.types.PoseBone, "torque_vis_props"):
        del bpy.types.PoseBone.torque_vis_props
    if hasattr(bpy.types.PoseBone, "torque_morph_binds"):
        del bpy.types.PoseBone.torque_morph_binds
    if hasattr(bpy.types.PoseBone, "torque_morph_bind_index"):
        del bpy.types.PoseBone.torque_morph_bind_index
    bpy.utils.unregister_class(TorqueBoneMorphBind)
    if hasattr(bpy.types.Action, "torque_seq_props"):
        del bpy.types.Action.torque_seq_props
    if hasattr(bpy.types.Scene, "torque_io"):
        del bpy.types.Scene.torque_io
    if hasattr(bpy.types.Scene, "torque_dts_export"):
        del bpy.types.Scene.torque_dts_export
    if hasattr(bpy.types.Scene, "torque_dsq_export"):
        del bpy.types.Scene.torque_dsq_export
    mount_preview.unregister()

    bpy.utils.unregister_class(TORQUE_OT_export_sequences_refresh)
    bpy.utils.unregister_class(TORQUE_OT_export_collection_page)
    bpy.utils.unregister_class(TORQUE_OT_export_collections_set_all)
    bpy.utils.unregister_class(TORQUE_OT_export_sequences_set_all)
    bpy.utils.unregister_class(TORQUE_OT_export_sequence_info)
    bpy.utils.unregister_class(TORQUE_UL_export_collections)
    bpy.utils.unregister_class(TORQUE_UL_export_sequences)
    bpy.utils.unregister_class(TorqueExportProperties)
    bpy.utils.unregister_class(TorqueExportCollectionPage)
    bpy.utils.unregister_class(TorqueExportCollectionItem)
    bpy.utils.unregister_class(TorqueExportSequenceItem)
    bpy.utils.unregister_class(TorqueIOProperties)
    bpy.utils.unregister_class(TorqueSequenceProperties)
    bpy.utils.unregister_class(TorqueSequenceTrigger)
    bpy.utils.unregister_class(TorqueAutoBillboardProperties)
    bpy.utils.unregister_class(TorqueVisProperties)
    bpy.utils.unregister_class(TorqueMaterialProperties)

    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_dts)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_dsq)
    bpy.types.VIEW3D_MT_object.remove(menu_func_split_mesh_index)
    bpy.types.VIEW3D_MT_object.remove(menu_func_collapse_unused_mesh_parents)
    bpy.types.VIEW3D_MT_edit_mesh.remove(menu_func_split_mesh_index)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export_dts)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export_dsq)

if __name__ == "__main__":
    register()
