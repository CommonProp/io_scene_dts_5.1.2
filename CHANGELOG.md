# Changelog

## 1.1.0

- **Node scale is written as aligned (per-axis) scale for every sequence that has scale keys**, in DTS and DSQ export, and sequences without scale keys get no scale type. Before, each sequence was written as uniform whenever its own keys happened to be uniform, so one file could mix uniform and aligned sequences. TGE 1.5.1 can't play that mix (the shape takes the largest type, and a uniform sequence on an aligned shape falls through `TSShapeInstance::handleAnimatedScale` without writing any scale), which stretched parts of the model while those sequences played. See "Node scale" in the README.
- New `_scale_type_selftest.py`.

## 1.0.0

- Blender 5.1.2 port of io_scene_dts: DTS / DSQ v24 import and export with Actions or markers, armatures and empties, skins, visibility, IFL, morphs, triggers, blend sequences, auto-billboards, TGE-style mesh optimisation, limit checks, logs, mount simulation and the Torque panels.
