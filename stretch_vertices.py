# SPDX-License-Identifier: GPL-3.0-or-later
"""Stretch Vertices — select the vertices of part of a group / component with
a window, then move them like a normal Move (a "stretch").

Same idea as the SketchUp plugin "Stretch By Area": the faces and edges that
touch the selected vertices follow, everything else stays where it is.

How to use
----------
1. Extensions > Stretch Vertices  (Ctrl+Shift+V).  The tool stays active,
   like any drawing tool.
2. Get inside the group or component:
   - select it BEFORE starting the tool (it is opened for you), or
   - click on it with the tool active, or double-click it as usual.
   It also works on loose geometry (nothing opened).
3. Drag a window over the vertices you want.  The window selects THROUGH the
   model (also the vertices hidden behind faces).  Orange squares = selected.
   Ctrl = add, Shift = toggle, Ctrl+Shift = remove (same rules as Select).
4. Click a base point (snaps work), move, click the destination.
   - arrow keys lock an axis, the Measurements box (VCB) accepts a distance
     ("1.5") or a vector ("0.2;0;-0.5") + Enter.
5. Esc: 1st press cancels the move, 2nd clears the selection.

One whole move = ONE undo step.  Editing a COMPONENT changes all its copies
when you leave it (Esc), like any component edit: use Make Unique first if
you want to stretch a single copy.

Nested groups: with a parent group open, the window selects the vertices of
the parent AND of every group / component nested inside it, at any depth.
A nested group that shares its geometry with other copies (a component used
several times) is made unique when you move it, so only YOUR copy changes.

Install: copy this file into
    ~/.local/share/ingetrazo/plugins/            (Linux)
    %APPDATA%\\ingetrazo\\plugins\\                (Windows)
and restart IngeTrazo (or Extensions > Open plugins folder).
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QIcon, QMatrix4x4, QPainter, QPen,
                           QPixmap, QPolygonF, QVector3D)

from core.group import Group, iter_placements, transformed_mesh
from core.history import Command, CompoundCommand, MoveVerticesCommand
from core.topology import _key, fold_nonplanar_faces
from core.i18n import tr
from core.units import fmt_len
from tools.base import Tool

#: Beyond this many vertices the overlay / preview stop drawing (the move
#: itself still works on all of them).
_MAX_DRAWN = 6000


def _mode(modifiers) -> str:
    """Same rules as the Select tool: Ctrl adds, Shift toggles,
    Ctrl+Shift removes, nothing replaces."""
    shift = bool(modifiers & Qt.ShiftModifier)
    ctrl = bool(modifiers & Qt.ControlModifier)
    if shift and ctrl:
        return "remove"
    if ctrl:
        return "add"
    return "toggle" if shift else "replace"


def _wpos(xf, p: QVector3D) -> QVector3D:
    return xf.map(p) if xf is not None else QVector3D(p)


class _NestedStretch(Command):
    """Move the vertices of nested placements (groups / components inside
    the open group) by a WORLD ``delta`` — one undo step.

    ``targets``: ``[(placement, world_matrix)]``; ``keys``: the world
    position keys of the vertices to move. A placement whose mesh is shared
    with another placement gets its own copy first (Make Unique), so the
    other copies stay as they were."""

    def __init__(self, targets, keys, delta) -> None:
        self.targets = list(targets)
        self.keys = set(keys)
        self.delta = QVector3D(delta)
        self._before = None
        self._after = None

    @staticmethod
    def _snap(targets):
        return [(g, g.mesh, g.mesh.capture_state()) for g, _xf in targets]

    @staticmethod
    def _restore(snap, scene) -> None:
        for g, mesh, state in snap:
            g.mesh = mesh
            mesh.restore_state(state)
        scene.version += 1

    def do(self, scene) -> None:
        if self._after is not None:                  # redo
            self._restore(self._after, scene)
            return
        self._before = self._snap(self.targets)
        from collections import Counter
        use = Counter(id(g.mesh) for top in scene.groups
                      for g, _m in iter_placements(top))
        for g, xf in self.targets:
            if use[id(g.mesh)] > 1:                  # shared: make it ours
                g.mesh = transformed_mesh(g.mesh, QMatrix4x4())
            d = self.delta
            if xf is not None:
                inv, ok = xf.inverted()
                if ok:
                    d = inv.mapVector(self.delta)
            for v in list(g.mesh.vertices):
                if _key(_wpos(xf, v.position)) in self.keys:
                    g.mesh.move_vertex(v, d)
            fold_nonplanar_faces(g.mesh)
        scene.version += 1
        self._after = self._snap(self.targets)

    def undo(self, scene) -> None:
        self._restore(self._before, scene)


class StretchVerticesTool(Tool):
    _instance = None
    name = "Stretch Vertices"
    shortcut = "Ctrl+Shift+V"
    description = ("Select the vertices of part of a group or component with "
                   "a window, then move them (stretch).")
    vcb_label = "Distance"

    # Rubber-band window: a drag is a box (on_box_select), a click on_click.
    box_select = True
    # Same drag behaviour as Move: camera-facing vertical plane + axis magnets.
    prefers_vertical_drag = True
    magnetic_axis_deg = 15.0
    screen_axis_px = 9.0

    def __init__(self) -> None:
        self.start_point: QVector3D | None = None   # drives snaps / axis lock
        self.grab: QVector3D | None = None
        self.hover_point: QVector3D | None = None
        # (id(placement), id(vertex)) -> (placement | None, world_matrix, vertex)
        # placement None = the open group's own mesh (scene.mesh).
        self._sel: dict = {}
        self._mesh = None             # the mesh the selection belongs to
        StretchVerticesTool._instance = self      # for the toolbar button

    # ------------------------------------------------------------------ life
    def on_activate(self, viewport) -> None:
        # Extensions menu entries are one-shot: they call on_activate and
        # leave the active tool alone.  Make ourselves the active tool, then
        # come back here (set_active_tool calls on_activate again).
        if getattr(viewport, "active_tool", None) is not self:
            viewport.set_active_tool(self)
            return
        self._reset()
        self._enter_selected_group(viewport)
        self._flash_hint(viewport)

    def on_deactivate(self, viewport) -> None:
        self._reset()

    def _reset(self) -> None:
        self.start_point = None
        self.grab = None
        self.hover_point = None
        self._sel = {}
        self._mesh = None

    def _clear_move(self) -> None:
        self.start_point = None
        self.grab = None
        self.hover_point = None

    # ------------------------------------------------------------- selection
    @staticmethod
    def _mesh_of(viewport, pg):
        return viewport.scene.mesh if pg is None else pg.mesh

    def _live(self, viewport) -> list:
        """Selected entries that still exist (undo, or leaving the group,
        may have invalidated some)."""
        mesh = viewport.scene.mesh
        if mesh is not self._mesh:
            self._sel = {}
            self._mesh = mesh
            return []
        live = {k: e for k, e in self._sel.items()
                if self._mesh_of(viewport, e[0]).registers(e[2])}
        self._sel = live
        return list(live.values())

    @staticmethod
    def _placements(scene):
        """``[(placement | None, world_matrix)]`` of everything the open
        group holds: its own mesh (None) and every nested placement."""
        eg = scene.edit_group
        out = [(None, None)]
        if eg is not None:
            for pg, xf in iter_placements(eg):
                if pg is not eg:
                    out.append((pg, xf))
        return out

    def _enter_selected_group(self, viewport) -> None:
        """A group selected before starting the tool is opened for editing."""
        scene = viewport.scene
        groups = [e for e in scene.selection if isinstance(e, Group)]
        if len(groups) != 1:
            return
        inside = scene.edit_group
        if inside is None or groups[0] in (getattr(inside, "children", None) or ()):
            viewport.begin_group_edit(groups[0])

    def on_box_select(self, viewport, rect, crossing: bool,
                      additive: bool = False, mode: str | None = None) -> None:
        if self.grab is not None:
            return                                  # mid-move: ignore windows
        scene = viewport.scene
        mesh = scene.mesh
        if mesh is not self._mesh:
            self._sel = {}
            self._mesh = mesh
        import numpy as np
        x0, y0, x1, y1 = rect
        selectable = getattr(scene, "entity_selectable", None)
        caught = []                                  # (pg, xf, vertex)
        for pg, xf in self._placements(scene):
            if pg is not None and (getattr(pg, "hidden", False)
                                   or getattr(pg, "locked", False)):
                continue
            verts = list(self._mesh_of(viewport, pg).vertices)
            if not verts:
                continue
            pts = np.array([(v.position.x(), v.position.y(), v.position.z())
                            for v in verts], dtype=float)
            if xf is not None:
                m = np.array(xf.data(), dtype=float).reshape(4, 4).T
                pts = pts @ m[:3, :3].T + m[:3, 3]
            px, py, front = viewport.world_to_pixels(pts)
            inside = front & (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)
            for v, ok in zip(verts, inside):
                if not ok:
                    continue
                # Vertices whose every edge is hidden / locked stay out.
                if (selectable is not None and pg is None and v.edges
                        and not any(selectable(e) for e in v.edges)):
                    continue
                caught.append((pg, xf, v))
        if mode is None:
            mode = "add" if additive else "replace"
        key = lambda c: (id(c[0]), id(c[2]))         # noqa: E731
        if mode == "replace":
            self._sel = {key(c): c for c in caught}
        elif mode == "add":
            self._sel.update({key(c): c for c in caught})
        elif mode == "remove":
            for c in caught:
                self._sel.pop(key(c), None)
        else:                                        # toggle
            for c in caught:
                if key(c) in self._sel:
                    del self._sel[key(c)]
                else:
                    self._sel[key(c)] = c

        n = len(self._sel)
        if n:
            viewport.flash_status(tr(
                "{n} vertices selected. Click a base point, then the "
                "destination.", n=n), 5000)
        else:
            viewport.flash_status(tr("No vertex inside the window."), 2500)
        viewport.update()

    # --------------------------------------------------------------- clicks
    def on_click(self, ctx) -> None:
        viewport = ctx.viewport
        if self.grab is not None:                    # 2nd click: destination
            self._commit(viewport, ctx.world - self.grab)
            return
        if self._live(viewport):                     # 1st click: base point
            self.start_point = QVector3D(ctx.world)
            self.grab = QVector3D(ctx.world)
            self.hover_point = QVector3D(ctx.world)
            viewport.update()
            return
        # Nothing selected: a click on a group / component opens it.
        scene = viewport.scene
        if scene.edit_group is None:
            group = viewport.pick_group(ctx.screen.x(), ctx.screen.y())
            if group is not None:
                viewport.begin_group_edit(group)
                self._reset()
                return
        self._flash_hint(viewport)

    def on_hover(self, ctx) -> None:
        self.hover_point = QVector3D(ctx.world)
        ctx.viewport.update()

    # ------------------------------------------------------- numeric input
    def on_value(self, viewport, value) -> bool:
        if self.grab is None:
            return False
        if isinstance(value, tuple):
            if len(value) != 3:
                return False
            delta = QVector3D(value[0], value[1], value[2])
        else:
            if self.hover_point is None:
                return False
            direction = self.hover_point - self.grab
            if direction.length() < 1e-9:
                return False
            delta = direction.normalized() * value
        self._commit(viewport, delta)
        return True

    # ----------------------------------------------------------------- Esc
    def on_cancel(self, viewport) -> None:
        if self.grab is not None:
            self._clear_move()                       # 1st Esc: drop the move
        elif self._sel:
            self._sel = {}                           # 2nd Esc: clear selection
        viewport.update()

    # --------------------------------------------------------------- commit
    def _commit(self, viewport, delta: QVector3D) -> None:
        entries = self._live(viewport)
        delta = QVector3D(delta)
        if entries and delta.length() > 1e-9:
            own, nested, keys = {}, {}, set()
            for pg, xf, v in entries:
                w = _wpos(xf, v.position)
                if pg is None:
                    own[_key(w)] = QVector3D(w)
                else:
                    nested[id(pg)] = (pg, xf)
                    keys.add(_key(w))
            commands = []
            if nested:
                commands.append(_NestedStretch(nested.values(), keys, delta))
            if own:
                commands.append(MoveVerticesCommand(
                    list(own.values()), delta, carry_textures=True))
            viewport.history.execute(commands[0] if len(commands) == 1
                                     else CompoundCommand(commands))
            refused = getattr(viewport.history, "last_rejected", None)
            if refused:
                viewport.flash_status(refused, 8000)
        self._clear_move()
        self._sel = {}
        viewport.update()

    # ------------------------------------------------------- visual feedback
    def rubber_band_lines(self):
        """The move vector, plus the edges touching the selection drawn at
        their stretched position (nothing in the model changes until the
        second click)."""
        if self.grab is None or self.hover_point is None:
            return []
        d = self.hover_point - self.grab
        segments = [(self.grab, self.hover_point)]
        moving = set(self._sel)
        seen = set()
        for pg, xf, v in self._sel.values():
            for e in v.edges:
                if (id(pg), id(e)) in seen:
                    continue
                seen.add((id(pg), id(e)))
                a = _wpos(xf, e.v0.position)
                b = _wpos(xf, e.v1.position)
                if (id(pg), id(e.v0)) in moving:
                    a = a + d
                if (id(pg), id(e.v1)) in moving:
                    b = b + d
                segments.append((a, b))
                if len(segments) > _MAX_DRAWN:
                    return segments
        return segments

    def value_label(self):
        if self.grab is None or self.hover_point is None:
            return None
        delta = self.hover_point - self.grab
        return (fmt_len(delta.length()), (self.grab + self.hover_point) * 0.5)

    def draw_overlay(self, viewport, painter) -> None:
        """Orange squares on the selected vertices (through the model)."""
        if not self._sel or viewport.scene.mesh is not self._mesh:
            return
        import numpy as np
        entries = list(self._sel.values())[:_MAX_DRAWN]
        d = (self.hover_point - self.grab
             if self.grab is not None and self.hover_point is not None
             else QVector3D())
        ws = [_wpos(xf, v.position) + d for _pg, xf, v in entries]
        pts = np.array([(w.x(), w.y(), w.z()) for w in ws], dtype=float)
        px, py, front = viewport.world_to_pixels(pts)
        painter.setPen(QPen(QColor(255, 255, 255, 230), 1.0))
        painter.setBrush(QColor(255, 120, 0, 235))
        for x, y, ok in zip(px, py, front):
            if ok:
                painter.drawRect(QRectF(x - 3.5, y - 3.5, 7.0, 7.0))

    def status_clause(self) -> str:
        if self.grab is not None:
            return tr("Click the destination (or type a distance).")
        if self._sel:
            return tr("{n} vertices: click a base point, or drag a new "
                      "window.", n=len(self._sel))
        return tr("Drag a window over the vertices (Ctrl add, Shift toggle).")

    def _flash_hint(self, viewport) -> None:
        scene = viewport.scene
        if scene.edit_group is None:
            msg = tr("Stretch Vertices: click a group or component to open "
                     "it, then drag a window over its vertices.")
        else:
            msg = tr("Stretch Vertices: drag a window over the vertices to "
                     "move.")
        viewport.flash_status(msg, 5000)

    # ------------------------------------------------- keep the cursor honest
    def snap_excluded(self):
        """What is in motion, left out of the snap candidates (the cursor
        must not snap onto the geometry being stretched)."""
        if self.grab is None:
            return None
        edges = {id(e) for pg, _x, v in self._sel.values() if pg is None
                 for e in v.edges}
        groups = {id(pg) for pg, _x, _v in self._sel.values()
                  if pg is not None}
        return (edges, groups) if edges or groups else None

    def moving_face_ids(self) -> set:
        if self.grab is None:
            return set()
        return {id(f) for pg, _x, v in self._sel.values() if pg is None
                for e in v.edges for f in e.faces}


# ---------------------------------------------------------------- toolbar
def make_icon(px: int = 64) -> QIcon:
    """A box whose right-hand vertices (orange) are being pulled by an arrow.
    Drawn here, so the extension is one self-contained file."""
    from PySide6.QtWidgets import QApplication
    ink = QApplication.palette().color(QApplication.palette().ColorRole.WindowText)
    orange = QColor(255, 120, 0)
    pm = QPixmap(px, px)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    u = px / 32.0
    pen = QPen(ink, 2.0 * u)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    # front square and back square of a box (oblique view)
    p.drawRect(QRectF(4 * u, 12 * u, 12 * u, 12 * u))
    p.drawLine(QPointF(4 * u, 12 * u), QPointF(9 * u, 7 * u))
    p.drawLine(QPointF(9 * u, 7 * u), QPointF(21 * u, 7 * u))
    p.drawLine(QPointF(16 * u, 12 * u), QPointF(21 * u, 7 * u))
    p.drawLine(QPointF(21 * u, 7 * u), QPointF(21 * u, 19 * u))
    p.drawLine(QPointF(16 * u, 24 * u), QPointF(21 * u, 19 * u))
    # the selected (orange) vertices on the right side
    p.setPen(QPen(Qt.white, 1.0 * u))
    p.setBrush(orange)
    for x, y in ((16, 12), (16, 24), (21, 7), (21, 19)):
        p.drawRect(QRectF((x - 2) * u, (y - 2) * u, 4 * u, 4 * u))
    # the pull: an arrow to the right
    p.setPen(QPen(orange, 2.2 * u, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(QPointF(23 * u, 16 * u), QPointF(29 * u, 16 * u))
    p.setBrush(orange)
    p.drawPolygon(QPolygonF([QPointF(31 * u, 16 * u), QPointF(27 * u, 12.5 * u),
                             QPointF(27 * u, 19.5 * u)]))
    p.end()
    return QIcon(pm)


def setup(app) -> None:
    """Adds the toolbar button (the Extensions menu entry comes with the
    Tool class by itself)."""
    from PySide6.QtWidgets import QToolBar

    def start() -> None:
        tool = StretchVerticesTool._instance or StretchVerticesTool()
        tool.on_activate(app.viewport)

    bar = QToolBar(tr("Stretch Vertices"), app.window)
    bar.setObjectName(f"extension_{app.key}_toolbar")   # stable: remembers its place
    action = bar.addAction(make_icon(), tr("Stretch Vertices"))
    action.setToolTip(tr("Stretch Vertices (Ctrl+Shift+V)\n"
                         "Select vertices with a window and move them."))
    action.triggered.connect(lambda _checked=False: start())
    app.window.addToolBar(bar)
