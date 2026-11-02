"""Standalone checks for DTS/DSQ log reports. Not loaded by Blender."""
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from _stripper_selftest import _load_addon, _stub_mathutils


def main():
    Vector = _stub_mathutils()
    dts, _opt = _load_addon()
    report = sys.modules["io_scene_dts.write_report"]
    Mesh, Primitive, DetailLevel = dts.Mesh, dts.Primitive, dts.DetailLevel
    from io_scene_dts.DtsShape import DtsShape
    from io_scene_dts.DsqFile import DsqFile
    from io_scene_dts.DtsTypes import Sequence, ObjectState, Node, Object

    shape = DtsShape()
    shape.dtsVersion = 24
    shape.exporterVersion = 0
    shape.names.extend(["base", "detail32", "mesh1"])
    shape.nodes.append(Node(0, -1))
    shape.default_translations.append(Vector((0, 0, 0)))
    shape.default_rotations.append(type("Q", (), {"x": 0, "y": 0, "z": 0, "w": 1})())
    shape.objects.append(Object(2, 1, 0, 0))
    shape.detail_levels.append(DetailLevel(1, 0, 0, 32, polyCount=1))
    shape.subshapes.append(type("S", (), {
        "firstNode": 0, "numNodes": 1, "firstObject": 0, "numObjects": 1})())
    mesh = Mesh(Mesh.StandardType)
    mesh.verts = [Vector((0, 0, 0)), Vector((1, 0, 0)), Vector((0, 1, 0))]
    mesh.normals = [Vector((0, 0, 1))] * 3
    mesh.enormals = [14, 14, 14]
    mesh.tverts = [Vector((0, 0)), Vector((1, 0)), Vector((0, 1))]
    mesh.indices = [0, 1, 2]
    mesh.primitives = [Primitive(0, 3, Primitive.Triangles | Primitive.Indexed)]
    mesh.center = Vector((0.5, 0.5, 0))
    mesh.radius = 1
    mesh.vert_ids = [0, 1, 2]
    mesh.smooth_ids = [1, 1, 1]
    shape.meshes.append(mesh)
    shape.objectstates.append(ObjectState(1, 0, 0))
    shape.smallest_size = 32
    shape.radius = 1
    shape.log_radius_source = "visible-mesh"

    seq = Sequence()
    seq.nameIndex = 0
    seq.numKeyframes = 1
    seq.duration = 0.1
    seq.rotationMatters = [False]
    seq.translationMatters = [False]
    seq.scaleMatters = [False]
    seq.visMatters = [False]
    seq.frameMatters = [False]
    seq.matFrameMatters = [False]
    seq.iflMatters = []
    shape.sequences.append(seq)

    with tempfile.TemporaryDirectory() as tmp:
        lite = Path(tmp) / "lite.txt"
        full = Path(tmp) / "full.txt"
        report.write_debug_report(str(lite), shape, {
            "operation": "export dts",
            "file_version": 24,
        }, report.LOG_LITE, ["Warning: test note"])
        report.write_debug_report(str(full), shape, {
            "operation": "export dts",
            "file_version": 24,
        }, report.LOG_FULL)
        lite_text = lite.read_text(encoding="utf-8")
        full_text = full.read_text(encoding="utf-8")
        assert "log_level = LITE" in lite_text
        assert "Warning: test note" in lite_text
        assert "smallest_size_s32 = 32" in lite_text
        assert "Object x detail" in lite_text
        assert "<omitted>" not in lite_text
        assert "v0 " not in lite_text
        assert "v0  " in full_text
        assert "vert_id=0" in full_text
        assert "smooth_id=1" in full_text
        print("OK dts lite/full")

        dsq = DsqFile()
        dsq.version = 24
        dsq.nodes = ["base"]
        dsq_seq = Sequence()
        dsq_seq.name = "root"
        dsq_seq.numKeyframes = 1
        dsq_seq.duration = 0.1
        dsq_seq.rotationMatters = [True]
        dsq_seq.translationMatters = [False]
        dsq_seq.scaleMatters = [False]
        dsq_seq.visMatters = [False]
        dsq.sequences.append(dsq_seq)
        dsq.rotations.append(type("Q", (), {"x": 0, "y": 0, "z": 0, "w": 1})())
        dsq_path = Path(tmp) / "anim.txt"
        report.write_dsq_report(str(dsq_path), dsq, {
            "operation": "export dsq",
            "file_version": 24,
            "node_binds": [("base", "base")],
        }, report.LOG_FULL)
        text = dsq_path.read_text(encoding="utf-8")
        assert "base -> base" in text
        assert "rot 0 base:" in text
        print("OK dsq full")

        stamp = report.datetime(2026, 8, 25, 17, 12, 8)
        first = report.unique_log_path(
            str(Path(tmp) / "player.dts"), "export dts", now=stamp, exists=lambda _p: False)
        assert first.endswith("player.dts.export-20260825-171208#1.txt")
        taken = {first}
        second = report.unique_log_path(
            str(Path(tmp) / "player.dts"), "export dts", now=stamp,
            exists=lambda path: path in taken)
        assert second.endswith("#2.txt")
        imported = report.unique_log_path(
            str(Path(tmp) / "player.dts"), "import dts", now=stamp, exists=lambda _p: False)
        assert "player.dts.import-20260825-171208#1.txt" in imported
        print("OK unique log names")

        fail_dts = Path(tmp) / "player.dts"
        log = report.IoLog(report.LOG_LITE)
        log.notes.append("Note: Seeking to reference frame at 0")
        log.notes.append("Exporting sequence root")
        report.finish_log(
            str(fail_dts), log, shape=shape,
            meta={"operation": "export dts"},
            error="Missing start marker for sequence 'root'")
        fail_logs = list(fail_dts.parent.glob("player.dts.export-*.txt"))
        assert len(fail_logs) == 1
        fail_text = fail_logs[0].read_text(encoding="utf-8")
        assert "Note: Seeking to reference frame at 0" in fail_text
        assert "Exporting sequence root" in fail_text
        assert "nodes = 1" in fail_text
        assert fail_text.rstrip().endswith("Missing start marker for sequence 'root'")
        assert "Export failed" in fail_text
        print("OK export fail keeps notes and reason")

        early = Path(tmp) / "anim.dsq"
        early_log = report.IoLog(report.LOG_FULL)
        early_log.notes.append("Warning: something first")
        report.set_active_log(early_log)
        report.record_export_error("No sequences selected for DSQ export")
        report.set_active_log(None)
        report.finish_log(
            str(early), early_log,
            meta={"operation": "export dsq"}, error=early_log.error)
        early_logs = list(early.parent.glob("anim.dsq.export-*.txt"))
        assert len(early_logs) == 1
        early_text = early_logs[0].read_text(encoding="utf-8")
        assert "Warning: something first" in early_text
        assert "Error: No sequences selected for DSQ export" in early_text
        assert early_text.rstrip().endswith("No sequences selected for DSQ export")
        print("OK early fail notes-only log")

        silent = Path(tmp) / "off.dts"
        report.finish_log(str(silent), report.IoLog(report.LOG_DISABLED), error="nope")
        assert not list(silent.parent.glob("off.dts*.txt"))
        print("OK disabled writes no fail log")

    print("all log self-tests passed")


if __name__ == "__main__":
    main()
