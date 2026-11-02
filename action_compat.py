"""Blender 5.1 Action slots / channelbags. Legacy Action.fcurves is gone."""


def iter_action_fcurves(action):
    if action is None:
        return
    found = False
    for layer in getattr(action, "layers", None) or []:
        for strip in getattr(layer, "strips", None) or []:
            for bag in getattr(strip, "channelbags", None) or []:
                for curve in getattr(bag, "fcurves", None) or []:
                    found = True
                    yield curve
    if found:
        return
    for curve in getattr(action, "fcurves", None) or []:
        yield curve


def action_has_fcurves(action):
    for _curve in iter_action_fcurves(action):
        return True
    return False


def assign_action_to_id(owner, action):
    if owner is None or action is None:
        return False
    data = getattr(owner, "animation_data", None)
    if data is None:
        create = getattr(owner, "animation_data_create", None)
        if create is None:
            return False
        data = create()
    if getattr(data, "action", None) is action:
        return True
    try:
        data.action = action
        return True
    except RuntimeError:
        return False


def owner_action_fcurves(owner):
    if owner is None:
        return []
    data = getattr(owner, "animation_data", None)
    if data is None or getattr(data, "action", None) is None:
        return []
    action = data.action
    slot = getattr(data, "action_slot", None)
    if slot is not None:
        for layer in getattr(action, "layers", None) or []:
            for strip in getattr(layer, "strips", None) or []:
                lookup = getattr(strip, "channelbag", None)
                bag = None
                if callable(lookup):
                    try:
                        bag = lookup(slot, ensure=False)
                    except TypeError:
                        try:
                            bag = lookup(slot)
                        except Exception:
                            bag = None
                    except Exception:
                        bag = None
                if bag is not None:
                    return list(getattr(bag, "fcurves", None) or [])
    return list(iter_action_fcurves(action))


def action_fcurve_ensure(action, owner, data_path, index=0):
    if action is None or not data_path:
        return None
    if owner is not None:
        assign_action_to_id(owner, action)
        ensure = getattr(action, "fcurve_ensure_for_datablock", None)
        if callable(ensure):
            return ensure(owner, data_path, index=index)
    for curve in (owner_action_fcurves(owner) if owner is not None
                  else list(iter_action_fcurves(action))):
        if (curve.data_path or "") == data_path and getattr(curve, "array_index", 0) == index:
            return curve
    curves = getattr(action, "fcurves", None)
    if curves is not None:
        try:
            return curves.new(data_path, index=index)
        except Exception:
            return None
    return None


def action_fcurves_array(action, data_path, array_count, owner=None):
    curves = [None] * array_count
    existing = (owner_action_fcurves(owner) if owner is not None
                else list(iter_action_fcurves(action)))
    for curve in existing:
        index = getattr(curve, "array_index", -1)
        if (curve.data_path or "") == data_path and 0 <= index < array_count:
            curves[index] = curve
    for index, curve in enumerate(curves):
        if curve is None:
            curves[index] = action_fcurve_ensure(action, owner, data_path, index)
    return curves
