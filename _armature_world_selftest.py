"""Standalone checks for armature object TM on export. Not loaded by Blender."""
import math
import sys
import types
from pathlib import Path

from _empty_rest_selftest import _load, _stub_bpy


def _stub_mathutils():
    mu = types.ModuleType("mathutils")

    class Vector(tuple):
        def __new__(cls, xyz=(0, 0, 0)):
            vals = tuple(float(v) for v in xyz)
            if len(vals) == 2:
                vals = (vals[0], vals[1], 0.0)
            if len(vals) == 3:
                vals = (vals[0], vals[1], vals[2])
            return tuple.__new__(cls, vals)

        @property
        def x(self):
            return self[0]

        @property
        def y(self):
            return self[1] if len(self) > 1 else 0.0

        @property
        def z(self):
            return self[2] if len(self) > 2 else 0.0

        def copy(self):
            return Vector(self)

        def __sub__(self, other):
            return Vector((self[0] - other[0], self[1] - other[1], self[2] - other[2]))

        @property
        def length(self):
            return (self[0] ** 2 + self[1] ** 2 + self[2] ** 2) ** 0.5

    class _Rot3:
        def __init__(self, rows):
            self.rows = rows

        def to_4x4(self):
            r = self.rows
            return Matrix((
                (r[0][0], r[0][1], r[0][2], 0.0),
                (r[1][0], r[1][1], r[1][2], 0.0),
                (r[2][0], r[2][1], r[2][2], 0.0),
                (0.0, 0.0, 0.0, 1.0),
            ))

    class Quaternion:
        def __init__(self, rows):
            self._rows = rows

        def to_matrix(self):
            return _Rot3(self._rows)

    class Matrix:
        def __init__(self, rows=None):
            if rows is None:
                self.rows = [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 0.0, 0.0, 1.0],
                ]
            else:
                self.rows = [[float(v) for v in row] for row in rows]

        @classmethod
        def Identity(cls, size=4):
            return cls()

        @classmethod
        def Translation(cls, vec):
            mat = cls()
            mat.rows[0][3] = float(vec[0])
            mat.rows[1][3] = float(vec[1])
            mat.rows[2][3] = float(vec[2])
            return mat

        @classmethod
        def Diagonal(cls, values):
            mat = cls()
            for index, value in enumerate(values):
                if index < 4:
                    mat.rows[index][index] = float(value)
            return mat

        @classmethod
        def Scale(cls, factor, size=4):
            return cls.Diagonal((factor, factor, factor, 1.0))

        @classmethod
        def Rotation(cls, angle, size, axis):
            x, y, z = float(axis[0]), float(axis[1]), float(axis[2])
            length = (x * x + y * y + z * z) ** 0.5 or 1.0
            x, y, z = x / length, y / length, z / length
            cosine = math.cos(angle)
            sine = math.sin(angle)
            hold = 1.0 - cosine
            return cls((
                (hold * x * x + cosine, hold * x * y - sine * z, hold * x * z + sine * y, 0.0),
                (hold * x * y + sine * z, hold * y * y + cosine, hold * y * z - sine * x, 0.0),
                (hold * x * z - sine * y, hold * y * z + sine * x, hold * z * z + cosine, 0.0),
                (0.0, 0.0, 0.0, 1.0),
            ))

        def copy(self):
            return Matrix(self.rows)

        def __matmul__(self, other):
            if isinstance(other, Matrix):
                result = [[0.0] * 4 for _ in range(4)]
                for row in range(4):
                    for col in range(4):
                        result[row][col] = sum(
                            self.rows[row][k] * other.rows[k][col] for k in range(4))
                return Matrix(result)
            x, y, z = float(other[0]), float(other[1]), float(other[2])
            rx = self.rows[0][0] * x + self.rows[0][1] * y + self.rows[0][2] * z + self.rows[0][3]
            ry = self.rows[1][0] * x + self.rows[1][1] * y + self.rows[1][2] * z + self.rows[1][3]
            rz = self.rows[2][0] * x + self.rows[2][1] * y + self.rows[2][2] * z + self.rows[2][3]
            return Vector((rx, ry, rz))

        def inverted(self):
            n = 4
            a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)]
                 for i, row in enumerate(self.rows)]
            for col in range(n):
                pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
                if abs(a[pivot][col]) < 1e-12:
                    raise ValueError("singular")
                a[col], a[pivot] = a[pivot], a[col]
                scale = a[col][col]
                a[col] = [v / scale for v in a[col]]
                for row in range(n):
                    if row == col:
                        continue
                    factor = a[row][col]
                    a[row] = [a[row][k] - factor * a[col][k] for k in range(2 * n)]
            return Matrix([row[n:] for row in a])

        def decompose(self):
            loc = Vector((self.rows[0][3], self.rows[1][3], self.rows[2][3]))
            scales = []
            for col in range(3):
                axis = Vector((self.rows[0][col], self.rows[1][col], self.rows[2][col]))
                scales.append(axis.length or 1.0)
            rot_rows = (
                (self.rows[0][0] / scales[0], self.rows[0][1] / scales[1], self.rows[0][2] / scales[2]),
                (self.rows[1][0] / scales[0], self.rows[1][1] / scales[1], self.rows[1][2] / scales[2]),
                (self.rows[2][0] / scales[0], self.rows[2][1] / scales[1], self.rows[2][2] / scales[2]),
            )
            return loc, Quaternion(rot_rows), Vector(scales)

    mu.Vector = Vector
    mu.Matrix = Matrix
    mu.Quaternion = Quaternion
    mu.Euler = type("Euler", (), {})
    sys.modules["mathutils"] = mu
    return Vector, Matrix


def _almost(a, b, eps=1e-5):
    return abs(a - b) <= eps


def _mat_eq(left, right, eps=1e-5):
    for row in range(4):
        for col in range(4):
            if not _almost(left.rows[row][col], right.rows[row][col], eps):
                return False
    return True


def _translation(mat):
    return (mat.rows[0][3], mat.rows[1][3], mat.rows[2][3])


class _Bone:
    def __init__(self, name, matrix_local, parent=None):
        self.name = name
        self.matrix_local = matrix_local
        self.parent = parent
        self.children = []
        if parent is not None:
            parent.children.append(self)


class _Armature:
    def __init__(self, matrix_world, pose_bones=None):
        self.matrix_world = matrix_world
        self.pose = types.SimpleNamespace(bones=pose_bones or {})


def main():
    Vector, Matrix = _stub_mathutils()
    _stub_bpy()
    addon = Path(__file__).resolve().parent
    pkg = "io_scene_dts_armature_world"
    module = types.ModuleType(pkg)
    module.__path__ = [str(addon)]
    sys.modules[pkg] = module
    if (addon / "action_compat.py").is_file():
        _load("action_compat", addon, pkg)
    _load("DtsTypes", addon, pkg)
    _load("encoded_normals", addon, pkg)
    _load("write_report", addon, pkg)
    _load("util", addon, pkg)
    shared = _load("shared_export", addon, pkg)

    identity = Matrix.Identity(4)
    rot180 = Matrix.Rotation(math.pi, 4, Vector((0, 0, 1)))
    scale30 = Matrix.Scale(30.0, 4)
    root_rest = Matrix.Translation(Vector((1, 0, 0)))
    child_rest = Matrix.Translation(Vector((1, 2, 0)))
    root = _Bone("root", root_rest)
    child = _Bone("child", child_rest, root)

    plain = _Armature(identity)
    turned = _Armature(rot180)
    scaled = _Armature(scale30)

    world_plain = shared.bone_export_world_matrix(plain, root, posed=False)
    world_turned = shared.bone_export_world_matrix(turned, root, posed=False)
    assert _mat_eq(world_plain, root_rest)
    assert _almost(_translation(world_turned)[0], -1.0)
    assert _almost(_translation(world_turned)[1], 0.0)

    world_scaled = shared.bone_export_world_matrix(scaled, root, posed=False)
    assert _almost(_translation(world_scaled)[0], 30.0)

    child_plain = shared.bone_export_local_matrix(plain, child, posed=False)
    child_turned = shared.bone_export_local_matrix(turned, child, posed=False)
    child_scaled = shared.bone_export_local_matrix(scaled, child, posed=False)
    expect_child = shared.safe_invert_matrix(root_rest) @ child_rest
    assert _mat_eq(child_plain, expect_child)
    # Object rotation cancels in parent-relative space; object scale
    # stays in the child translation so DSQ can stretch vs DTS rest.
    assert _mat_eq(child_turned, expect_child)
    plain_t = _translation(child_plain)
    scaled_t = _translation(child_scaled)
    assert _almost(scaled_t[0], plain_t[0] * 30.0)
    assert _almost(scaled_t[1], plain_t[1] * 30.0)
    assert _almost(scaled_t[2], plain_t[2] * 30.0)

    assert shared.armature_object_delta(plain) == (False, False, False)
    assert shared.armature_object_delta(scaled) == (False, False, True)

    class _Seq:
        def __init__(self, count):
            self.rotationMatters = [False] * count
            self.translationMatters = [False] * count

    seq = _Seq(2)
    shared.apply_armature_object_track_matters(seq, {}, (
        (0, scaled, root),
        (1, scaled, child),
    ))
    assert seq.translationMatters == [True, True]
    assert seq.rotationMatters == [False, False]

    skipped = _Seq(2)
    shared.apply_armature_object_track_matters(
        skipped, {"enable_transform": False}, ((0, scaled, root),))
    assert skipped.translationMatters == [False, False]

    root_plain = shared.bone_export_local_matrix(plain, root, posed=False)
    root_turned = shared.bone_export_local_matrix(turned, root, posed=False)
    assert not _mat_eq(root_plain, root_turned)
    assert _almost(_translation(root_turned)[0], -1.0)

    node = types.SimpleNamespace(
        armature=turned,
        bl_ob=root,
        rest_world=world_turned,
        matrix_world=world_turned,
    )
    mesh = types.SimpleNamespace(matrix_world=rot180 @ Matrix.Translation(Vector((1, 0, 3))))
    offset = shared.mesh_object_offset(mesh, node)
    # Same world space as rest: offset is inv(zap(rest)) @ mesh_world, not inv(arm).
    expect = shared.zap_scale_matrix(world_turned).inverted() @ mesh.matrix_world
    assert _mat_eq(offset, expect)

    pose_child = types.SimpleNamespace(
        matrix=child_rest @ Matrix.Scale(4.0, 4))
    growing = _Armature(identity, {"child": pose_child})
    child_rest_local = shared.bone_export_local_matrix(growing, child, posed=False)
    child_pose_local = shared.bone_export_local_matrix(growing, child, posed=True)
    _loc, _rot, rest_s = child_rest_local.decompose()
    _loc, _rot, pose_s = child_pose_local.decompose()
    assert _almost(rest_s[0], 1.0) and _almost(rest_s[1], 1.0)
    assert _almost(pose_s[0], 4.0) and _almost(pose_s[1], 4.0) and _almost(pose_s[2], 4.0)

    print("OK armature object TM")


if __name__ == "__main__":
    main()
