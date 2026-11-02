"""Standalone checks for the node scale type the exporter writes. Not loaded by Blender.

TGE 1.5.1 only plays a scaled sequence right when its scale type matches the shape's
(the largest type of every sequence on the shape, DTS and DSQs together), so the
exporter writes every sequence with scale keys as AlignedScale and sequences without
scale keys with no scale type (see shared_export.py, "node scale type")."""
import sys
import types
from pathlib import Path

from _stripper_selftest import _stub_mathutils
from _dsq_split_selftest import _stub_bpy, _load


def main():
    _stub_mathutils()
    _stub_bpy()
    addon = Path(__file__).resolve().parent
    pkg = "io_scene_dts"
    module = types.ModuleType(pkg)
    module.__path__ = [str(addon)]
    sys.modules[pkg] = module
    dts = _load("DtsTypes", addon, pkg)
    _load("encoded_normals", addon, pkg)
    _load("write_report", addon, pkg)
    _load("util", addon, pkg)
    shared = _load("shared_export", addon, pkg)
    Seq, V = dts.Sequence, dts.Vector
    U, A, X = Seq.UniformScale, Seq.AlignedScale, Seq.ArbitraryScale

    def seq():
        s = Seq()
        s.flags = A          # what export_dts / export_dsq start every sequence with
        s.baseScale = 0
        return s

    # the KS-23 case: a root with only (1, 1, 1) keys and a fire action squashing one axis
    root, fire, still = seq(), seq(), seq()
    uniform, aligned = [], []
    kind = shared.apply_shape_sequence_scales(
        [(root, [V((1, 1, 1))] * 6), (fire, [V((1, 0.4, 1))] * 7), (still, [])], uniform, aligned)
    assert kind == "aligned", kind
    assert root.flags & (U | A | X) == A and fire.flags & (U | A | X) == A
    assert still.flags & (U | A | X) == 0, "no scale keys: no scale type (doesn't count toward the shape's)"
    assert uniform == [] and len(aligned) == 13
    assert root.baseScale == 0 and fire.baseScale == 6
    assert tuple(aligned[fire.baseScale]) == (1, 0.4, 1)

    # a file whose keys are all uniform is still aligned: a DSQ / DTS loaded beside it may not be
    a, b = seq(), seq()
    uniform, aligned = [], []
    shared.apply_shape_sequence_scales([(a, [V((2, 2, 2))] * 3), (b, [V((0, 0, 0))])], uniform, aligned)
    assert a.flags & (U | A | X) == A and b.flags & (U | A | X) == A
    assert uniform == [] and [tuple(v) for v in aligned] == [(2, 2, 2)] * 3 + [(0, 0, 0)]
    assert b.baseScale == 3

    # a uniform flag left over on a sequence is cleared, other flags are kept
    c = seq()
    c.flags = U | Seq.Blend | Seq.Cyclic
    shared.apply_shape_sequence_scales([(c, [V((1, 1, 1))])], [], [])
    assert c.flags == A | Seq.Blend | Seq.Cyclic, c.flags
    d = seq()
    d.flags = U | Seq.Cyclic
    shared.apply_shape_sequence_scales([(d, [])], [], [])
    assert d.flags == Seq.Cyclic, d.flags

    # nothing scaled anywhere: nothing written, no scale type
    assert shared.apply_shape_sequence_scales([(seq(), [])], [], []) is None
    print("OK node scale type")


if __name__ == "__main__":
    main()
