import os
import bpy
from .action_compat import action_fcurve_ensure, owner_action_fcurves
from colorsys import hsv_to_rgb
from itertools import count
from fractions import Fraction

texture_extensions = ("png", "jpg", "jpeg", "tga", "bmp", "gif")

default_materials = {
    "black": (0, 0, 0, 255),
    "black25": (191, 191, 191, 255),
    "black50": (128, 128, 128, 255),
    "black75": (64, 64, 64, 255),
    "blank": (255, 255, 255, 255),
    "blue": (0, 0, 255, 255),
    "darkRed": (128, 0, 0, 255),
    "gray25": (64, 64, 64, 255),
    "gray50": (128, 128, 128, 255),
    "gray75": (191, 191, 191, 255),
    "green": (26, 128, 64, 255),
    "lightBlue": (10, 186, 245, 255),
    "lightYellow": (249, 249, 99, 255),
    "palegreen": (125, 136, 104, 255),
    "red": (213, 0, 0, 255),
    "white": (255, 255, 255, 255),
    "yellow": (255, 255, 0, 255)
}

for name, color in default_materials.items():
    default_materials[name] = (color[0] / 255, color[1] / 255, color[2] / 255, color[3] / 255)

for key, value in tuple(default_materials.items()):
    default_materials[key.lower()] = value

def _texture_name_candidates(name):
    if not name:
        return ()
    name = name.split("#", 1)[0]
    name = os.path.basename(name.replace("\\", "/"))
    if not name:
        return ()
    base, ext = os.path.splitext(name)
    if ext and ext[1:].lower() in texture_extensions:
        return (base, name)
    return (name,)


def _texture_search_dirs(filepath):
    dirname = os.path.dirname(os.path.abspath(filepath))
    dirs = []
    seen = set()
    while dirname:
        if dirname not in seen:
            dirs.append(dirname)
            seen.add(dirname)
        if os.path.ismount(dirname):
            break
        parent = os.path.dirname(dirname)
        if not parent or parent == dirname:
            break
        dirname = parent
    return dirs


def resolve_texture(filepath, name):
    # DTS folder first, then its parent (Blockland add-on root), then
    # further ancestors. abspath so a relative import still sees parent.
    names = _texture_name_candidates(name)
    if not names:
        return None

    for dirname in _texture_search_dirs(filepath):
        found = _resolve_texture_in_dir(dirname, names)
        if found is not None:
            return found


def fallback_texture_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "texture_fallbacks")


def _resolve_texture_in_dir(dirname, names):
    if not names or not dirname:
        return None
    for candidate in names:
        texbase = os.path.join(dirname, candidate)
        for extension in texture_extensions:
            texname = texbase + "." + extension
            if os.path.isfile(texname):
                return texname
    try:
        entries = os.listdir(dirname)
    except OSError:
        return None
    wanted = {
        candidate.lower() + "." + extension
        for candidate in names
        for extension in texture_extensions
    }
    for entry in entries:
        if entry.lower() in wanted:
            return os.path.join(dirname, entry)
    return None


def resolve_fallback_texture(name):
    names = _texture_name_candidates(name)
    if not names:
        return None
    return _resolve_texture_in_dir(fallback_texture_dir(), names)

def fractions():
    yield 0

    for k in count():
        i = 2 ** k

        for j in range(1, i, 2):
            yield j / i

def get_hsv_colors():
    for h in fractions():
        yield (h, 0.75, 0.75)

def get_rgb_colors():
    return map(lambda hsv: hsv_to_rgb(*hsv) + (1,), get_hsv_colors())

def action_get_or_new(ob):
  if not ob.animation_data:
    ob.animation_data_create()

  if ob.animation_data.action:
    return ob.animation_data.action

  action = bpy.data.actions.new(ob.name + "Action")
  ob.animation_data.action = action

  return action

def ob_curves_array(ob, data_path, array_count):
  action = action_get_or_new(ob)
  curves = [None] * array_count

  for curve in owner_action_fcurves(ob):
    if curve.data_path != data_path or curve.array_index < 0 or curve.array_index >= array_count:
      continue

    if curves[curve.array_index]:
      pass # TODO: warn if more than one curve for an array slot

    curves[curve.array_index] = curve

  for index, curve in enumerate(curves):
    if curve is None:
      curves[index] = action_fcurve_ensure(action, ob, data_path, index)

  return curves

def ob_location_curves(ob):
  return ob_curves_array(ob, "location", 3)

def ob_scale_curves(ob):
  return ob_curves_array(ob, "scale", 3)

def fcurves_path_from_rotation(ob):
    if ob.rotation_mode == 'QUATERNION':
        return ('rotation_quaternion', 4)
    elif ob.rotation_mode == 'AXIS_ANGLE':
        return ('rotation_axis_angle', 4)
    else:
        return ('rotation_euler', 3)

def ob_rotation_data(ob):
    if ob.rotation_mode == 'QUATERNION':
        return ob.rotation_quaternion
    elif ob.rotation_mode == 'AXIS_ANGLE':
        return ob.rotation_axis_angle
    else:
        return ob.rotation_euler

def ob_rotation_curves(ob):
    data_path, array_count = fcurves_path_from_rotation(ob)
    return ob.rotation_mode, ob_curves_array(ob, data_path, array_count)

def ob_vis_curves(ob): 
    return ob_curves_array(ob, 'torque_vis_value', 1)

def evaluate_all(curves, frame):
    return tuple(map(lambda c: c.evaluate(frame), curves))

def array_from_fcurves(curves, data_path, array_size):
    found = False
    array = [None] * array_size

    for curve in curves:
        if curve.data_path == data_path and curve.array_index != -1:
            array[curve.array_index] = curve
            found = True

    if found:
        return tuple(array)

def array_from_fcurves_rotation(curves, ob):
    data_path, array_count = fcurves_path_from_rotation(ob)
    return array_from_fcurves(curves, data_path, array_count)

def fcurves_keyframe_in_range(curves, start, end):
    for curve in curves:
        if curve != None:
            for keyframe in curve.keyframe_points:
                frame = keyframe.co[0]
                if frame >= start and frame <= end:
                    return True

    return False

def find_reference(scene):
    reference_marker = scene.timeline_markers.get("reference")
    if reference_marker is not None:
        return reference_marker.frame

def fail(operator, message):
    print("Error:", message)
    try:
        from .write_report import record_export_error
        record_export_error(message)
    except Exception:
        pass
    if operator is not None:
        operator.report({"ERROR"}, message)
    return {"FINISHED"}
