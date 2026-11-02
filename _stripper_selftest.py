"""Standalone checks for the TGE/Max stripper. Not loaded by Blender."""
import importlib.util
import sys
import types
from pathlib import Path


def _stub_mathutils():
    mu = types.ModuleType("mathutils")

    class Vector(tuple):
        def __new__(cls, xyz=(0, 0, 0)):
            vals = tuple(float(v) for v in xyz)
            if len(vals) == 2:
                vals = (vals[0], vals[1], 0.0)
            return tuple.__new__(cls, vals)

        @property
        def x(self):
            return self[0]

        @property
        def y(self):
            return self[1]

        @property
        def z(self):
            return self[2]

        def __sub__(self, other):
            return Vector((self[0] - other[0], self[1] - other[1], self[2] - other[2]))

        def __add__(self, other):
            return Vector((self[0] + other[0], self[1] + other[1], self[2] + other[2]))

        @property
        def length(self):
            return (self[0] ** 2 + self[1] ** 2 + self[2] ** 2) ** 0.5

        @property
        def length_squared(self):
            return self[0] ** 2 + self[1] ** 2 + self[2] ** 2

        def normalized(self):
            length = self.length or 1.0
            return Vector((self[0] / length, self[1] / length, self[2] / length))

        def copy(self):
            return Vector(self)

        def cross(self, other):
            return Vector((
                self[1] * other[2] - self[2] * other[1],
                self[2] * other[0] - self[0] * other[2],
                self[0] * other[1] - self[1] * other[0]))

        def normalize(self):
            return None

    class Matrix:
        def __matmul__(self, other):
            return other

        def to_3x3(self):
            return self

    mu.Vector = Vector
    mu.Matrix = Matrix
    mu.Euler = type("Euler", (), {})
    mu.Quaternion = type("Quaternion", (), {})
    sys.modules["mathutils"] = mu
    return Vector


def _load_addon():
    addon = Path(__file__).resolve().parent
    pkg = "io_scene_dts"
    module = types.ModuleType(pkg)
    module.__path__ = [str(addon)]
    sys.modules[pkg] = module

    def load(name):
        spec = importlib.util.spec_from_file_location(
            "{}.{}".format(pkg, name), addon / (name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["{}.{}".format(pkg, name)] = mod
        spec.loader.exec_module(mod)
        return mod

    return load("DtsTypes"), load("mesh_optimize")


def _wind_key(tri):
    a, b, c = tri
    return min((a, b, c), (b, c, a), (c, a, b))


def _grid(nx, ny):
    faces = []

    def vid(x, y):
        return y * (nx + 1) + x

    for y in range(ny):
        for x in range(nx):
            v00, v10 = vid(x, y), vid(x + 1, y)
            v01, v11 = vid(x, y + 1), vid(x + 1, y + 1)
            faces.append((v00, v10, v11))
            faces.append((v00, v11, v01))
    return faces, (nx + 1) * (ny + 1)


def _make_mesh(Mesh, Primitive, Vector, faces, nverts):
    mesh = Mesh(Mesh.StandardType)
    mesh.verts = [Vector((i, i * 0.01, 0.0)) for i in range(nverts)]
    mesh.normals = [Vector((0.0, 0.0, 1.0)) for _ in range(nverts)]
    mesh.enormals = [0] * nverts
    mesh.tverts = [Vector((0.0, 0.0)) for _ in range(nverts)]
    indices = []
    for a, b, c in faces:
        indices.extend((a, b, c))
    mesh.indices = indices
    mesh.primitives = [Primitive(0, len(indices), Primitive.Triangles | Primitive.Indexed)]
    return mesh


def _emitted_faces(opt, mesh):
    keys = []
    for prim in mesh.primitives:
        for tri in opt._unwind_primitive_tris(mesh.indices, prim):
            keys.append(_wind_key(tri))
    return keys


def main():
    Vector = _stub_mathutils()
    dts, opt = _load_addon()
    Mesh, Primitive = dts.Mesh, dts.Primitive

    def check(name, faces, nverts, expect_strip_min=0, expect_prims_max=None):
        mesh = _make_mesh(Mesh, Primitive, Vector, faces, nverts)
        before = [_wind_key(f) for f in faces]
        opt.optimize_exported_mesh(mesh)
        after = _emitted_faces(opt, mesh)
        assert sorted(after) == sorted(before), (
            "{} lost or flipped faces: in={} out={}".format(
                name, len(before), len(after)))
        assert opt._primitives_s16_ok(mesh), "{} failed S16".format(name)
        err = dts.tge_mesh_draw_error(mesh, name)
        assert err is None, "{}: {}".format(name, err)
        n_strip = sum(1 for p in mesh.primitives if p.type & Primitive.Strip)
        if expect_strip_min:
            assert n_strip >= expect_strip_min, "{} expected a strip, got {}".format(
                name, [(p.firstElement, p.numElements, p.type) for p in mesh.primitives])
        if expect_prims_max is not None:
            assert len(mesh.primitives) <= expect_prims_max, "{} prims {}".format(
                name, len(mesh.primitives))
        print("OK", name, "prims", len(mesh.primitives),
              "indices", len(mesh.indices), "strip", n_strip)

    check("isolated", [(0, 1, 2)], 3, expect_prims_max=1)
    check("two_sided", [(0, 1, 2), (0, 2, 1)], 3, expect_prims_max=1)
    faces, nverts = _grid(3, 3)
    check("grid3", faces, nverts, expect_strip_min=1, expect_prims_max=2)
    faces, nverts = _grid(20, 20)
    check("grid20", faces, nverts, expect_strip_min=1, expect_prims_max=2)
    faces, nverts = _grid(80, 76)
    check("grid80x76", faces, nverts, expect_strip_min=1, expect_prims_max=3)

    chunks = list(opt._split_strip_chunks(list(range(40000))))
    assert all(len(c) <= dts.TGE_PRIM_S16_MAX for c in chunks)
    assert len(chunks[0]) == 32766
    assert chunks[1][0] == 32764
    print("OK split_strip_chunks", [len(c) for c in chunks])

    stitched, overflow = opt._stitch_keepers([
        [0, 1, 2, 3, 4, 5],
        [10, 11, 12, 13, 14, 15],
    ])
    assert overflow == []
    assert stitched[:6] == [0, 1, 2, 3, 4, 5]
    # even length 6: [last, firstB] + B
    assert stitched[6:8] == [5, 10]
    assert stitched[8:] == [10, 11, 12, 13, 14, 15]
    print("OK stitch even", stitched)

    stitched, overflow = opt._stitch_keepers([
        [0, 1, 2, 3, 4],
        [10, 11, 12, 13, 14, 15],
    ])
    assert overflow == []
    # odd length 5: [last, last, firstB] + B
    assert stitched[5:8] == [4, 4, 10]
    assert stitched[8:] == [10, 11, 12, 13, 14, 15]
    print("OK stitch odd", stitched)

    islands = [(i * 3, i * 3 + 1, i * 3 + 2) for i in range(200)]
    check("islands200", islands, 600, expect_prims_max=1)

    mesh = _make_mesh(Mesh, Primitive, Vector, [(0, 1, 2), (3, 4, 5)], 6)
    mesh.primitives = [
        Primitive(0, 3, Primitive.Triangles | Primitive.Indexed | 0),
        Primitive(3, 3, Primitive.Triangles | Primitive.Indexed | 1),
    ]
    opt.optimize_exported_mesh(mesh)
    mats = {p.type & Primitive.MaterialMask for p in mesh.primitives}
    assert mats == {0, 1}, mats
    print("OK two materials", len(mesh.primitives), "prims")

    def weld_mesh(mtype, verts, smooth, vids=None, norms=None, uvs=None):
        mesh = Mesh(mtype)
        mesh.verts = [Vector(v) for v in verts]
        mesh.normals = [Vector(n) for n in (norms or [(0, 0, 1)] * len(verts))]
        mesh.enormals = [0] * len(verts)
        mesh.tverts = [Vector(uv) for uv in (uvs or [(0.0, 0.0)] * len(verts))]
        mesh.smooth_ids = list(smooth)
        if vids is not None:
            mesh.vert_ids = list(vids)
        mesh.indices = list(range(len(verts)))
        mesh.primitives = [Primitive(0, len(verts), Primitive.Triangles | Primitive.Indexed)]
        return mesh

    # Same pos+UV+smooth, normals differ — keep the hard-edge split.
    mesh = weld_mesh(
        Mesh.StandardType,
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (-1, 0, 0)],
        [1, 1, 1, 1, 1, 1],
        norms=[(0, 0, 1), (0, 0, 1), (0, 0, 1), (0.2, 0, 0.98), (0.2, 0, 0.98), (0, 0, 1)])
    n = opt.collapse_mesh_vertices(mesh)
    assert n == 0 and len(mesh.verts) == 6, (n, len(mesh.verts))
    print("OK no weld different normals")

    # Same pos+UV+smooth+normal — weld and average.
    mesh = weld_mesh(
        Mesh.StandardType,
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (-1, 0, 0)],
        [1, 1, 1, 1, 1, 1],
        norms=[(0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1)])
    n = opt.collapse_mesh_vertices(mesh)
    assert n == 2 and len(mesh.verts) == 4, (n, len(mesh.verts))
    print("OK weld same-smooth same-normal")

    # Same pos+UV, different smooth — Max keeps both (hard edge).
    mesh = weld_mesh(
        Mesh.StandardType,
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (-1, 0, 0)],
        [1, 1, 1, 2, 2, 2])
    n = opt.collapse_mesh_vertices(mesh)
    assert n == 0 and len(mesh.verts) == 6, (n, len(mesh.verts))
    print("OK no weld different smooth")

    # Skin: coincident, same UV/smooth, different vertId — Max does not weld.
    mesh = weld_mesh(
        Mesh.SkinType,
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (-1, 0, 0)],
        [0, 0, 0, 0, 0, 0],
        vids=[0, 1, 2, 3, 4, 5])
    n = opt.collapse_mesh_vertices(mesh)
    assert n == 0 and len(mesh.verts) == 6, (n, len(mesh.verts))
    print("OK no weld skin different vertId")

    # Skin: same vertId + pos + UV — Max welds.
    mesh = weld_mesh(
        Mesh.SkinType,
        [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (0, 1, 0), (-1, 0, 0)],
        [0, 0, 0, 0, 0, 0],
        vids=[0, 1, 2, 0, 2, 4])
    n = opt.collapse_mesh_vertices(mesh)
    assert n == 2 and len(mesh.verts) == 4, (n, len(mesh.verts))
    print("OK weld skin same vertId")

    print("all stripper self-tests passed")


if __name__ == "__main__":
    main()
