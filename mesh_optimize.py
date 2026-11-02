import math

from .DtsTypes import Mesh, Primitive, TGE_PRIM_S16_MAX, TGE_TRI_LIST_MAX_ELEMS
from .write_report import note

SAME_VERT_TOL = 0.00005
SAME_TVERT_TOL = 0.00005
SAME_NORM_TOL = 0.005

_CELL_NEIGHBORS = tuple(
    (dx, dy, dz)
    for dx in (-1, 0, 1)
    for dy in (-1, 0, 1)
    for dz in (-1, 0, 1))

# Max stripper.cc QUICK_STRIP (limitStripLength off). Hoppe 16-entry
# FIFO cache, no simK lookahead — that path is compiled out of Max.
STRIP_CACHE_SIZE = 16
STRIP_ADJACENCY_WEIGHT = 5.0
STRIP_NOSWAP_WEIGHT = 3.0
STRIP_ALREADY_CACHED_WEIGHT = 1.0
# Keep for TGE stitch only if the strip has at least 4 faces (6 verts).
# Shorter runs become one leftover triangle list per material.
STRIP_KEEPER_MIN_ELEMS = 6
TGE_INDEX_BUFFER_MAX = TGE_PRIM_S16_MAX + TGE_PRIM_S16_MAX  # 65534


def _pack_xyz(points):
    xs = [p.x for p in points]
    ys = [p.y for p in points]
    zs = [p.z for p in points]
    return xs, ys, zs


def collapse_mesh_vertices(mesh):
    count = len(mesh.verts)
    if count == 0:
        return 0

    # parent[i] is an older vertex i welded onto. That target can weld
    # again later, so lookups must walk to a vertex that stayed kept.
    parent = list(range(count))
    keep = [True] * count

    px, py, pz = _pack_xyz(mesh.verts)
    n_normals = len(mesh.normals)
    n_tverts = len(mesh.tverts)
    if n_normals:
        nx, ny, nz = _pack_xyz(mesh.normals)
    else:
        nx = ny = nz = ()
    if n_tverts:
        tu = [t.x for t in mesh.tverts]
        tv = [t.y for t in mesh.tverts]
    else:
        tu = tv = ()

    vtol = SAME_VERT_TOL
    ntol = SAME_NORM_TOL
    ttol = SAME_TVERT_TOL
    # Weld on pos + UV + exact smooth. Skins also require the same
    # vertId. Keep Blender/split normals: do not weld a hard-edge
    # split just because Max would ignore the normal after recompute.
    use_smooth = len(getattr(mesh, "smooth_ids", ())) == count
    use_vid = (
        mesh.get_type() == Mesh.SkinType and
        len(getattr(mesh, "vert_ids", ())) == count)
    use_norm = n_normals > 0
    smooth_ids = mesh.smooth_ids if use_smooth else ()
    vert_ids = mesh.vert_ids if use_vid else ()

    grid = {}
    cells = [None] * count
    for i in range(count):
        key = (math.floor(px[i] / vtol), math.floor(py[i] / vtol), math.floor(pz[i] / vtol))
        cells[i] = key
        bucket = grid.get(key)
        if bucket is None:
            grid[key] = [i]
        else:
            bucket.append(i)

    for i in range(count - 1, 0, -1):
        ix, iy, iz = cells[i]
        ipx, ipy, ipz = px[i], py[i], pz[i]
        has_n = i < n_normals
        has_t = i < n_tverts
        if has_n:
            inx, iny, inz = nx[i], ny[i], nz[i]
        if has_t:
            itu, itv = tu[i], tv[i]

        best_j = None
        for dx, dy, dz in _CELL_NEIGHBORS:
            bucket = grid.get((ix + dx, iy + dy, iz + dz))
            if not bucket:
                continue
            for j in bucket:
                if j >= i or not keep[j]:
                    continue
                if best_j is not None and j >= best_j:
                    continue
                if abs(px[j] - ipx) >= vtol or abs(py[j] - ipy) >= vtol or abs(pz[j] - ipz) >= vtol:
                    continue
                if has_t and j < n_tverts:
                    if abs(tu[j] - itu) >= ttol or abs(tv[j] - itv) >= ttol:
                        continue
                if use_smooth and smooth_ids[i] != smooth_ids[j]:
                    continue
                if use_vid and vert_ids[i] != vert_ids[j]:
                    continue
                if use_norm and has_n and j < n_normals:
                    if abs(nx[j] - inx) >= ntol or abs(ny[j] - iny) >= ntol or abs(nz[j] - inz) >= ntol:
                        continue
                best_j = j
                if j == 0:
                    break
            if best_j == 0:
                break

        if best_j is not None:
            parent[i] = best_j
            keep[i] = False

    if all(keep):
        return 0

    n_vids = len(getattr(mesh, "vert_ids", ()))
    n_lids = len(getattr(mesh, "loop_ids", ()))
    n_sids = len(getattr(mesh, "smooth_ids", ()))
    acc_normals = None
    if n_normals:
        zero = mesh.normals[0] - mesh.normals[0]
        acc_normals = [zero.copy() for _ in range(count)]
        for i in range(count):
            if i >= n_normals:
                continue
            root = i
            while not keep[root]:
                root = parent[root]
            acc_normals[root] = acc_normals[root] + mesh.normals[i]

    new_index = {}
    new_verts = []
    new_tverts = []
    new_normals = []
    new_enormals = []
    new_vert_ids = []
    new_loop_ids = []
    new_smooth_ids = []

    for i in range(count):
        if not keep[i]:
            continue
        new_index[i] = len(new_verts)
        new_verts.append(mesh.verts[i])
        if i < n_tverts:
            new_tverts.append(mesh.tverts[i])
        if acc_normals is not None:
            summed = acc_normals[i]
            if summed.length_squared > 1e-16:
                new_normals.append(summed.normalized())
            elif i < n_normals:
                new_normals.append(mesh.normals[i])
        if i < len(mesh.enormals):
            new_enormals.append(mesh.enormals[i])
        if i < n_vids:
            new_vert_ids.append(mesh.vert_ids[i])
        if i < n_lids:
            new_loop_ids.append(mesh.loop_ids[i])
        if i < n_sids:
            new_smooth_ids.append(mesh.smooth_ids[i])

    remap = [0] * count
    for i in range(count):
        index = i
        while not keep[index]:
            index = parent[index]
        remap[i] = new_index[index]

    def map_index(index):
        if 0 <= index < count:
            return remap[index]
        return index

    mesh.verts = new_verts
    if mesh.tverts:
        mesh.tverts = new_tverts
    if mesh.normals:
        mesh.normals = new_normals
    if mesh.enormals:
        mesh.enormals = new_enormals
    if n_vids:
        mesh.vert_ids = new_vert_ids
    if n_lids:
        mesh.loop_ids = new_loop_ids
    if n_sids:
        mesh.smooth_ids = new_smooth_ids
    mesh.indices = [map_index(index) for index in mesh.indices]

    if mesh.influences:
        mesh.influences = _finalize_skin_influences(mesh.influences, keep, remap, count)

    mesh.vertsPerFrame = len(mesh.verts)
    return count - len(mesh.verts)


def _finalize_skin_influences(influences, keep, remap, count):
    """Match Max ShapeMimic after collapseVertices + TGE updateSkin.

    Max remaps vertexIndex, then drops duplicate (vertex, bone) tuples
    without adding weights. TGE updateSkin() multiplies the bind-pose
    vert by each weight and accumulates; it never renormalizes. It also
    zeros a vert when vertexIndex changes, so tuples must be grouped
    by vertex (Max re-sorts by vertex, then bone).
    """
    kept = []
    seen = {}
    for vertex_index, bone_index, weight in influences:
        if vertex_index < 0 or vertex_index >= count or not keep[vertex_index]:
            continue
        key = (remap[vertex_index], bone_index)
        prev = seen.get(key)
        if prev is not None:
            if abs(prev - weight) > 0.01:
                note("Warning: welded skin verts disagree on bone {} weight ({:.4f} vs {:.4f})".format(
                    bone_index, prev, weight))
            continue
        seen[key] = weight
        kept.append((key[0], bone_index, weight))
    kept.sort(key=lambda item: (item[0], item[1]))
    return kept


def drop_degenerate_faces(mesh):
    if not mesh.primitives:
        return 0

    dropped = 0
    new_indices = []
    new_prims = []
    for prim in mesh.primitives:
        if prim.numElements < 3:
            continue
        start = prim.firstElement
        end = start + prim.numElements
        chunk = mesh.indices[start:end]
        if not (prim.type & Primitive.Strip) and not (prim.type & Primitive.Fan):
            kept = []
            for i in range(0, len(chunk) - 2, 3):
                a, b, c = chunk[i], chunk[i + 1], chunk[i + 2]
                if a == b or b == c or a == c:
                    dropped += 1
                    continue
                kept.extend((a, b, c))
            chunk = kept
        if len(chunk) < 3:
            continue
        new_prims.append(Primitive(len(new_indices), len(chunk), prim.type))
        new_indices.extend(chunk)

    mesh.indices = new_indices
    mesh.primitives = new_prims
    return dropped


def _strip_tris(strip):
    """TGE TSMesh::buildPolyList strip unwind (XOR walk)."""
    tris = []
    for j in range(2, len(strip)):
        if (j % 2) == 0:
            tri = (strip[j - 2], strip[j - 1], strip[j])
        else:
            tri = (strip[j - 1], strip[j - 2], strip[j])
        if tri[0] != tri[1] and tri[1] != tri[2] and tri[0] != tri[2]:
            tris.append(tri)
    return tris


def _list_faces(chunk):
    faces = []
    for i in range(0, len(chunk) - 2, 3):
        a, b, c = chunk[i], chunk[i + 1], chunk[i + 2]
        if a != b and b != c and a != c:
            faces.append((a, b, c))
    return faces


def _material_type_bits(prim_type, strip):
    flags = prim_type & Primitive.MaterialMask
    if prim_type & Primitive.NoMaterial:
        flags |= Primitive.NoMaterial
    flags |= Primitive.Indexed
    if strip:
        flags |= Primitive.Strip
    else:
        flags |= Primitive.Triangles
    return flags


class _MaxStripper:
    """Max stripper.cc QUICK_STRIP: 16-entry FIFO + adjacency/noswap/cache score.

    Adjacency is hashed by directed opposite edges (same rule as
    Stripper::setAdjacency: shared edge in opposite order, not all
    three verts). limitStripLength stays off.
    """

    def __init__(self, faces):
        self.faces = faces
        self.n = len(faces)
        self.used = [False] * self.n
        self.num_adj = [0] * self.n
        self.adj = [set() for _ in range(self.n)]
        self._build_adjacency()
        self.cache = [-1] * STRIP_CACHE_SIZE
        self.cache_misses = 0
        self.recent = []
        self.current_face = -1
        self.strip = []

    def _build_adjacency(self):
        edge_faces = {}
        for index, face in enumerate(self.faces):
            a, b, c = face
            for edge in ((a, b), (b, c), (c, a)):
                bucket = edge_faces.get(edge)
                if bucket is None:
                    edge_faces[edge] = [index]
                else:
                    bucket.append(index)

        for index, face in enumerate(self.faces):
            a, b, c = face
            for opposite in ((b, a), (c, b), (a, c)):
                for other in edge_faces.get(opposite, ()):
                    if other <= index:
                        continue
                    oa, ob, oc = self.faces[other]
                    if (a == oa or a == ob or a == oc) and \
                            (b == oa or b == ob or b == oc) and \
                            (c == oa or c == ob or c == oc):
                        continue
                    self.adj[index].add(other)
                    self.adj[other].add(index)
                    self.num_adj[index] += 1
                    self.num_adj[other] += 1

    def _clone_growth(self):
        other = _MaxStripper.__new__(_MaxStripper)
        other.faces = self.faces
        other.n = self.n
        other.adj = self.adj
        other.used = self.used[:]
        other.num_adj = self.num_adj[:]
        other.cache = self.cache[:]
        other.cache_misses = self.cache_misses
        other.recent = self.recent[:]
        other.current_face = self.current_face
        other.strip = []
        return other

    def _cache_cold(self):
        return all(value < 0 for value in self.cache)

    def _add_to_cache(self, vertex):
        if vertex not in self.cache:
            self.cache_misses += 1
        del self.cache[0]
        self.cache.append(vertex)

    def _add_to_cache_from_back(self, vertex, pos_from_back):
        del self.cache[0]
        self.cache.insert(len(self.cache) - pos_from_back, vertex)

    def _mark_used(self, face):
        self.used[face] = True
        self.num_adj[face] = 0
        for other in self.adj[face]:
            if self.used[other]:
                continue
            self.num_adj[other] -= 1

    def _best_start_max(self):
        for face in self.recent:
            if not self.used[face]:
                return face
        best_face = -1
        best_score = -1
        for index, face in enumerate(self.faces):
            if self.used[index]:
                continue
            in_cache = 0
            for vert in face:
                if vert in self.cache:
                    in_cache += 1
            score = (in_cache << 4) + self.num_adj[index]
            if score > best_score:
                best_face = index
                best_score = score
        return best_face

    def _best_start_min(self):
        best_face = -1
        best_adj = None
        for index in range(self.n):
            if self.used[index]:
                continue
            adj = self.num_adj[index]
            if best_adj is None or adj < best_adj:
                best_face = index
                best_adj = adj
        return best_face

    def _start_face(self, face):
        a, b, c = self.faces[face]
        self.strip = [a, b, c]
        self._add_to_cache(a)
        self._add_to_cache(b)
        self._add_to_cache(c)
        self._mark_used(face)
        self.current_face = face
        self.recent = []

    def _rotate_face(self):
        self.strip[0], self.strip[1], self.strip[2] = (
            self.strip[1], self.strip[2], self.strip[0])
        self.cache[-3], self.cache[-2], self.cache[-1] = (
            self.cache[-2], self.cache[-1], self.cache[-3])

    def _swap_needed(self, old0, old1):
        last = self.cache[-1]
        prev = self.cache[-2]
        return (last != old0 or prev != old1) and (last != old1 or prev != old0)

    def _get_verts(self, face):
        idx0, idx1, idx2 = self.faces[face]
        last3 = (self.cache[-1], self.cache[-2], self.cache[-3])
        prev = []
        add = -1
        for vert in (idx0, idx1, idx2):
            if vert in last3:
                prev.append(vert)
            else:
                add = vert
        if add < 0 or len(prev) != 2:
            return None
        if idx1 == add:
            return prev[1], prev[0], add
        return prev[0], prev[1], add

    def _face_has_edge(self, face, idx0, idx1):
        v0, v1, v2 = self.faces[face]
        return (
            (v0 == idx0 and v1 == idx1) or
            (v1 == idx0 and v2 == idx1) or
            (v2 == idx0 and v0 == idx1))

    def _adjacent_faces(self, face):
        idx0, idx1, idx2 = self.faces[face]
        found = [-1, -1, -1]
        found_adj = [-1, -1, -1]
        edges = ((idx1, idx0), (idx2, idx1), (idx0, idx2))
        for other in self.adj[face]:
            if self.used[other]:
                continue
            matched = -1
            for slot, edge in enumerate(edges):
                if self._face_has_edge(other, edge[0], edge[1]):
                    matched = slot
                    break
            if matched < 0:
                continue
            if found_adj[matched] < 0 or self.num_adj[other] < found_adj[matched]:
                found[matched] = other
                found_adj[matched] = self.num_adj[other]
        return found

    def _score(self, face, ignore_order):
        if face < 0:
            return -100000.0
        verts = self._get_verts(face)
        if verts is None:
            return -100000.0
        old0, old1, add = verts
        score = -self.num_adj[face] * STRIP_ADJACENCY_WEIGHT
        if not ignore_order and not self._swap_needed(old0, old1):
            score += STRIP_NOSWAP_WEIGHT
        if add in self.cache:
            score += STRIP_ALREADY_CACHED_WEIGHT
        return score

    def _can_go(self, face):
        if face < 0:
            return False
        last = self.strip[-1]
        a, b, c = self.faces[face]
        return last == a or last == b or last == c

    def _add_face(self):
        face0, face1, face2 = self._adjacent_faces(self.current_face)
        if len(self.strip) != 3:
            if not self._can_go(face0):
                face0 = -1
            if not self._can_go(face1):
                face1 = -1
            if not self._can_go(face2):
                face2 = -1
        if face0 < 0 and face1 < 0 and face2 < 0:
            return False

        ignore_order = len(self.strip) == 3
        score0 = self._score(face0, ignore_order)
        score1 = self._score(face1, ignore_order)
        score2 = self._score(face2, ignore_order)
        if score0 >= score1 and score0 >= score2:
            best = face0
        elif score1 >= score0 and score1 >= score2:
            best = face1
        else:
            best = face2
        if best < 0:
            return False

        verts = self._get_verts(best)
        if verts is None:
            return False
        old0, old1, add = verts

        if len(self.strip) == 3:
            spins = 0
            while self._swap_needed(old0, old1):
                self._rotate_face()
                spins += 1
                if spins == 3:
                    return False
            self.strip.append(add)
            self._add_to_cache(add)
        else:
            if self._swap_needed(old0, old1):
                dup = self.strip[-3]
                self.strip.insert(len(self.strip) - 1, dup)
                self._add_to_cache_from_back(dup, 1)
            self.strip.append(add)
            self._add_to_cache(add)

        if face0 >= 0 and face0 != best:
            self.recent.append(face0)
        if face1 >= 0 and face1 != best:
            self.recent.append(face1)
        if face2 >= 0 and face2 != best:
            self.recent.append(face2)

        self._mark_used(best)
        self.current_face = best
        return True

    def _grow_from(self, face):
        self._start_face(face)
        while self._add_face():
            pass
        return self.strip

    def _unused_faces(self):
        return [self.faces[i] for i in range(self.n) if not self.used[i]]

    def make_strips(self):
        strips = []
        while True:
            if self._cache_cold() and not self.recent:
                face_max = self._best_start_max()
                if face_max < 0:
                    break
                face_min = self._best_start_min()
                if face_min >= 0 and face_min != face_max:
                    sim_max = self._clone_growth()
                    strip_max = sim_max._grow_from(face_max)
                    sim_min = self._clone_growth()
                    strip_min = sim_min._grow_from(face_min)
                    max_key = (sim_max.cache_misses, -len(strip_max))
                    min_key = (sim_min.cache_misses, -len(strip_min))
                    winner = sim_min if min_key < max_key else sim_max
                    self.used = winner.used
                    self.num_adj = winner.num_adj
                    self.cache = winner.cache
                    self.cache_misses = winner.cache_misses
                    self.recent = winner.recent
                    self.current_face = winner.current_face
                    strips.append(list(winner.strip))
                    continue
                self.recent = []
                strips.append(list(self._grow_from(face_max)))
                continue

            face = self._best_start_max()
            self.recent = []
            if face < 0:
                break
            strips.append(list(self._grow_from(face)))
        leftover = self._unused_faces()
        return strips, leftover


def _stitch_keepers(keepers):
    """TGE convertToSingleStrip even/odd join. One strip; rest stay keepers."""
    if not keepers:
        return None, []
    stitched = list(keepers[0])
    overflow = []
    for other in keepers[1:]:
        extra = 3 if (len(stitched) & 1) else 2
        if len(stitched) + extra + len(other) > TGE_PRIM_S16_MAX:
            overflow.append(other)
            continue
        if len(stitched) & 1:
            stitched.extend((stitched[-1], stitched[-1], other[0]))
        else:
            stitched.extend((stitched[-1], other[0]))
        stitched.extend(other)
    return stitched, overflow


def _split_strip_chunks(indices):
    """Cut a strip so each piece's count fits S16 without flipping winding.

    A continuation after an even cut k reuses indices[k-2:] so the first
    new tri is (k-2, k-1, k), matching TGE even-j winding. Odd 32767 is
    only used when the piece is the whole remaining strip.
    """
    remaining = list(indices)
    while remaining:
        if len(remaining) <= TGE_PRIM_S16_MAX:
            yield remaining
            return
        cut = TGE_PRIM_S16_MAX - (TGE_PRIM_S16_MAX % 2)
        if cut < 3:
            return
        yield remaining[:cut]
        remaining = remaining[cut - 2:]


def _split_list_chunks(indices):
    remaining = list(indices)
    while remaining:
        take = min(len(remaining), TGE_TRI_LIST_MAX_ELEMS)
        take -= take % 3
        if take < 3:
            return
        yield remaining[:take]
        remaining = remaining[take:]


def _can_append(buffer, count):
    start = len(buffer)
    if start > TGE_PRIM_S16_MAX or count > TGE_PRIM_S16_MAX:
        return False
    if start + count > TGE_INDEX_BUFFER_MAX:
        return False
    return True


def _append_chunks(buffer, prims, flags, chunks):
    leftover = []
    for chunk in chunks:
        if _can_append(buffer, len(chunk)):
            prims.append(Primitive(len(buffer), len(chunk), flags))
            buffer.extend(chunk)
        else:
            leftover.append(chunk)
    return leftover


def _faces_to_list(faces):
    indices = []
    for a, b, c in faces:
        indices.extend((a, b, c))
    return indices


def _classify_and_pack_material(faces, prim_type, buffer, prims):
    strip_flags = _material_type_bits(prim_type, True)
    list_flags = _material_type_bits(prim_type, False)
    leftover_faces = []

    if not faces:
        return leftover_faces

    strips, unused = _MaxStripper(faces).make_strips()
    leftover_faces.extend(unused)

    keepers = []
    for strip in strips:
        if len(strip) >= STRIP_KEEPER_MIN_ELEMS:
            keepers.append(strip)
        else:
            leftover_faces.extend(_strip_tris(strip))

    stitched, overflow = _stitch_keepers(keepers)
    for extra in overflow:
        leftover_faces.extend(_strip_tris(extra))

    if stitched:
        leftover_chunks = _append_chunks(
            buffer, prims, strip_flags, _split_strip_chunks(stitched))
        for chunk in leftover_chunks:
            leftover_faces.extend(_strip_tris(chunk))

    if leftover_faces:
        leftover_chunks = _append_chunks(
            buffer, prims, list_flags, _split_list_chunks(_faces_to_list(leftover_faces)))
        if leftover_chunks:
            print("  warning: leftover triangles did not fit TGE S16 start/count")
    return leftover_faces


def stripify_mesh(mesh):
    if not mesh.primitives:
        return

    new_indices = []
    new_prims = []
    for prim in mesh.primitives:
        if prim.numElements < 3:
            continue
        chunk = mesh.indices[prim.firstElement:prim.firstElement + prim.numElements]
        if prim.type & Primitive.Fan:
            faces = []
            dummy = Primitive(0, len(chunk), prim.type)
            for tri in _unwind_primitive_tris(chunk, dummy):
                faces.append(tri)
            _classify_and_pack_material(faces, prim.type, new_indices, new_prims)
            continue
        if prim.type & Primitive.Strip:
            leftover = _append_chunks(
                new_indices, new_prims, prim.type, _split_strip_chunks(chunk))
            if leftover:
                list_flags = _material_type_bits(prim.type, False)
                faces = []
                for piece in leftover:
                    faces.extend(_strip_tris(piece))
                _append_chunks(
                    new_indices, new_prims, list_flags,
                    _split_list_chunks(_faces_to_list(faces)))
            continue

        faces = _list_faces(chunk)
        if not faces:
            continue
        _classify_and_pack_material(faces, prim.type, new_indices, new_prims)

    mesh.indices = new_indices
    mesh.primitives = new_prims


def _unwind_primitive_tris(indices, prim):
    chunk = indices[prim.firstElement:prim.firstElement + prim.numElements]
    if prim.type & Primitive.Strip:
        return _strip_tris(chunk)
    if prim.type & Primitive.Fan:
        tris = []
        for j in range(2, len(chunk)):
            tri = (chunk[0], chunk[j - 1], chunk[j])
            if tri[0] != tri[1] and tri[1] != tri[2] and tri[0] != tri[2]:
                tris.append(tri)
        return tris
    return _list_faces(chunk)


def _prim_material_flags(prim):
    return _material_type_bits(prim.type, False)


def _primitives_s16_ok(mesh):
    for prim in mesh.primitives:
        start = prim.firstElement
        count = prim.numElements
        if start < 0 or count < 0 or start > TGE_PRIM_S16_MAX or count > TGE_PRIM_S16_MAX:
            return False
        if start + count > len(mesh.indices):
            return False
    return True


def fit_mesh_primitives_to_s16(mesh):
    """Last-resort S16 pack. Strips are split in strip space, lists on 3s.

    Does not unwind strips into triangle lists. That invert/hide bug
    came from treating export strips like import winding.
    """
    if not mesh.primitives or _primitives_s16_ok(mesh):
        return False

    new_indices = []
    new_prims = []
    dropped = False
    for prim in mesh.primitives:
        chunk = list(mesh.indices[prim.firstElement:prim.firstElement + prim.numElements])
        if prim.type & Primitive.Fan:
            faces = _unwind_primitive_tris(mesh.indices, prim)
            leftover = _append_chunks(
                new_indices, new_prims, _prim_material_flags(prim),
                _split_list_chunks(_faces_to_list(faces)))
            if leftover:
                dropped = True
                break
            continue
        if prim.type & Primitive.Strip:
            leftover = _append_chunks(
                new_indices, new_prims, prim.type, _split_strip_chunks(chunk))
            if leftover:
                list_flags = _material_type_bits(prim.type, False)
                faces = []
                for piece in leftover:
                    faces.extend(_strip_tris(piece))
                more = _append_chunks(
                    new_indices, new_prims, list_flags,
                    _split_list_chunks(_faces_to_list(faces)))
                if more:
                    dropped = True
                    break
            continue
        leftover = _append_chunks(
            new_indices, new_prims, _prim_material_flags(prim),
            _split_list_chunks(chunk))
        if leftover:
            dropped = True
            break

    if not new_prims:
        return False
    mesh.indices = new_indices
    mesh.primitives = new_prims
    if dropped:
        print("  warning: remaining primitives did not fit TGE S16 start/count")
    return True


def compute_normals_like_max(mesh):
    """Match ShapeMimic::computeNormals after collapse.

    Face normal is cross(v2-v0, v1-v0) (Max CW). A vert receives that
    normal if it is at a corner position and has the same smooth id.
    Island ids are compared by equality, not Max bitfield AND — ids 1
    and 3 would falsely match if we used AND.
    """
    count = len(mesh.verts)
    if count == 0 or not mesh.primitives:
        return
    if len(getattr(mesh, "smooth_ids", ())) != count:
        return

    vtol = SAME_VERT_TOL
    px, py, pz = _pack_xyz(mesh.verts)
    grid = {}
    for i in range(count):
        key = (math.floor(px[i] / vtol), math.floor(py[i] / vtol), math.floor(pz[i] / vtol))
        bucket = grid.get(key)
        if bucket is None:
            grid[key] = [i]
        else:
            bucket.append(i)

    zero = mesh.verts[0] - mesh.verts[0]
    acc = [zero.copy() for _ in range(count)]
    counts = [0] * count
    smooth = mesh.smooth_ids

    def nearby(index):
        key = (math.floor(px[index] / vtol), math.floor(py[index] / vtol), math.floor(pz[index] / vtol))
        found = []
        for dx, dy, dz in _CELL_NEIGHBORS:
            bucket = grid.get((key[0] + dx, key[1] + dy, key[2] + dz))
            if not bucket:
                continue
            for other in bucket:
                if abs(px[other] - px[index]) >= vtol:
                    continue
                if abs(py[other] - py[index]) >= vtol:
                    continue
                if abs(pz[other] - pz[index]) >= vtol:
                    continue
                found.append(other)
        return found

    for prim in mesh.primitives:
        for i0, i1, i2 in _unwind_primitive_tris(mesh.indices, prim):
            v0 = mesh.verts[i0]
            v1 = mesh.verts[i1]
            v2 = mesh.verts[i2]
            nrm = (v2 - v0).cross(v1 - v0)
            if nrm.length * nrm.length <= 0.0000001:
                continue
            nrm.normalize()
            for corner in (i0, i1, i2):
                sid = smooth[corner]
                for other in nearby(corner):
                    if smooth[other] != sid:
                        continue
                    acc[other] = acc[other] + nrm
                    counts[other] += 1

    mesh.normals = []
    for i in range(count):
        if counts[i] and acc[i].length * acc[i].length > 0.0000001:
            mesh.normals.append(acc[i].normalized())
        else:
            mesh.normals.append(zero.copy())

    for i in range(count):
        if counts[i]:
            continue
        for other in nearby(i):
            if not counts[other] or smooth[other] != smooth[i]:
                continue
            mesh.normals[i] = mesh.normals[other]
            counts[i] = 1
            break
    for i in range(count):
        if counts[i]:
            continue
        for other in nearby(i):
            if not counts[other]:
                continue
            mesh.normals[i] = mesh.normals[other]
            break


def optimize_exported_mesh(mesh):
    if mesh.get_type() not in (Mesh.StandardType, Mesh.SkinType):
        return
    if not mesh.verts or not mesh.indices:
        return
    collapsed = collapse_mesh_vertices(mesh)
    dropped = drop_degenerate_faces(mesh)
    # Keep the exported Blender/split normals. Max recomputes after
    # collapse (and zeros skin groups); that is what made the AK
    # receiver/magazine look smeared in Show Tool.
    stripify_mesh(mesh)
    n_strip = sum(1 for p in mesh.primitives if p.type & Primitive.Strip)
    n_list = len(mesh.primitives) - n_strip
    mesh.log_optimize = {
        "welded": collapsed,
        "dropped": dropped,
        "strips": n_strip,
        "lists": n_list,
        "indices": len(mesh.indices),
    }
    print("  welded {} verts, dropped {} degenerate faces, {} strip / {} list prims, {} indices".format(
        collapsed, dropped, n_strip, n_list, len(mesh.indices)))
