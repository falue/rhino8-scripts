#! python3

import Rhino
import Rhino.Geometry as rg
import Rhino.Input.Custom as ric
import Rhino.UI

import scriptcontext as sc

import Eto.Forms as forms
import Eto.Drawing as drawing

import System.Drawing

import math


# ============================================================
# SELECTION
# ============================================================

def select_face():

    go = ric.GetObject()
    go.SetCommandPrompt("Select planar, cylindrical or spherical Brep face")

    go.GeometryFilter = (
        Rhino.DocObjects.ObjectType.Surface |
        Rhino.DocObjects.ObjectType.Brep
    )

    go.SubObjectSelect = True
    go.Get()

    if go.CommandResult() != Rhino.Commands.Result.Success:
        return None

    objref = go.Object(0)

    face = objref.Face()
    pt = objref.SelectionPoint()

    if face is None:

        brep = objref.Brep()

        if brep.Faces.Count == 1:
            face = brep.Faces[0]

        elif not pt.IsValid:
            print("Click directly on the face to knurl.")
            return None

        else:
            # whole polysurface picked: take the face under the click
            face = face_at(brep.Faces, pt)

    if face.Brep.IsSolid:
        return face

    # STEP imports may come as loose surfaces, often in a block:
    # join them so the face belongs to the whole part
    obj = objref.Object()

    if isinstance(obj, Rhino.DocObjects.InstanceObject):
        objects = obj.InstanceDefinition.GetObjects()
    else:
        objects = [o for o in sc.doc.Objects if o.Visible]

    # join copies: joining consumes the document's own geometry and
    # crashes Rhino on the next redraw
    joined = rg.Brep.JoinBreps(
        [
            o.DuplicateGeometry() for o in objects
            if isinstance(o.Geometry, rg.Brep)
        ],
        sc.doc.ModelAbsoluteTolerance
    )

    if not pt.IsValid:
        u, v = face.Domain(0).Mid, face.Domain(1).Mid
        pt = face.PointAt(u, v)

    return face_at([f for b in joined for f in b.Faces], pt)


def face_at(faces, pt):

    # face under point pt

    def distance(f):

        if not point_is_on_face(f, pt):
            return math.inf

        return f.PointAt(*face_uv(f, pt)).DistanceTo(pt)

    return min(faces, key=distance)


def classify_face(face):

    surf = face.UnderlyingSurface()
    tol = sc.doc.ModelAbsoluteTolerance

    ok, sphere = surf.TryGetSphere(tol)

    if ok:
        return "sphere", sphere

    ok, cylinder = surf.TryGetCylinder(tol)

    if ok:
        return "cylinder", cylinder

    ok, plane = surf.TryGetPlane(tol)

    if ok:
        return "plane", plane

    return None, None


def matching_faces(face, face_type, surface_data):

    # picked face first, then all faces of its Brep on the same
    # sphere / cylinder / plane

    tol = sc.doc.ModelAbsoluteTolerance

    def same(f):

        other_type, other = classify_face(f)

        if other_type != face_type:
            return False

        if face_type == "sphere":
            return (
                other.Center.DistanceTo(surface_data.Center) < tol and
                abs(other.Radius - surface_data.Radius) < tol
            )

        if face_type == "cylinder":

            axis = rg.Line(
                surface_data.Center,
                surface_data.Center + surface_data.Axis
            )

            return (
                surface_data.Axis.IsParallelTo(other.Axis) != 0 and
                axis.DistanceTo(other.Center, False) < tol and
                abs(other.Radius - surface_data.Radius) < tol
            )

        return (
            abs(surface_data.DistanceTo(other.Origin)) < tol and
            surface_data.ZAxis.IsParallelTo(other.ZAxis) != 0
        )

    return [face] + [
        f for f in face.Brep.Faces
        if f.FaceIndex != face.FaceIndex and same(f)
    ]


# ============================================================
# PICK POINT ON FACE
# ============================================================

def pick_face_point(face, prompt):

    gp = ric.GetPoint()
    gp.SetCommandPrompt(prompt)

    gp.Constrain(face.Brep, -1, -1, False)

    gp.Get()

    if gp.CommandResult() != Rhino.Commands.Result.Success:
        return None

    return gp.Point()


# ============================================================
# FACE HELPERS
# ============================================================

def face_uv(face, point):

    result = face.ClosestPoint(point)

    if not result:
        return None

    ok, u, v = result

    if not ok:
        return None

    return u, v


def point_is_on_face(face, point):

    uv = face_uv(face, point)

    if uv is None:
        return False

    u, v = uv

    relation = face.IsPointOnFace(
        u,
        v,
        sc.doc.ModelAbsoluteTolerance
    )

    return (
        relation == rg.PointFaceRelation.Interior or
        relation == rg.PointFaceRelation.Boundary
    )


def face_frame(face, point):

    # tangent frame at point, Z = face normal (out of the material)

    ok, plane = face.FrameAt(*face_uv(face, point))

    brep = face.Brep
    tol = sc.doc.ModelAbsoluteTolerance
    probe = plane.Origin + plane.ZAxis * (tol * 10.0)

    # probe inside = odd number of crossings along a ray; unlike
    # IsPointInside this also works on polysurfaces with small gaps
    ray = rg.LineCurve(
        probe,
        probe + plane.ZAxis * brep.GetBoundingBox(True).Diagonal.Length
    )

    points = rg.Intersect.Intersection.CurveBrep(ray, brep, tol)[2]

    # faces meeting at a seam report the same crossing twice
    crossings = []

    for p in points:
        if all(p.DistanceTo(q) > tol for q in crossings):
            crossings.append(p)

    # swapping X and Y flips Z
    if len(crossings) % 2:
        plane = rg.Plane(plane.Origin, plane.YAxis, plane.XAxis)

    return plane


# ============================================================
# KNURL GRID
# ============================================================

class KnurlGrid:

    # maps unrolled coordinates (x, y in mm) to the surface; offset is
    # along the normal out of the material
    # square lattice of pitch p, rotated by angle; cylinders snap the
    # pitch around so the seam closes (angle snaps to 0 or 45 degrees)

    def __init__(self, faces, face_type, surface_data, origin, pitch, angle):

        self.faces = faces
        self.face_type = face_type
        self.pitch = pitch

        # cylinder: lattice repeats every `period` cells around
        self.scale_x = 1.0
        self.period = 0

        frame = face_frame(faces[0], origin)

        corners = [
            p for f in faces
            for p in f.GetBoundingBox(True).GetCorners()
        ]

        if face_type == "plane":

            self.frame = frame

        elif face_type == "cylinder":

            angle = 45.0 if round(angle / 45.0) % 2 else 0.0

            axis = rg.Vector3d(surface_data.Axis)
            axis.Unitize()

            v = frame.Origin - surface_data.Center
            self.along0 = v * axis

            radial = v - axis * self.along0
            radial.Unitize()

            # bore: material normal points towards the axis
            self.sign = 1.0 if frame.ZAxis * radial > 0 else -1.0

            self.center = surface_data.Center
            self.radius = surface_data.Radius
            self.axes = (rg.Vector3d.CrossProduct(axis, radial), axis, radial)

            circumference = 2.0 * math.pi * self.radius
            period = pitch * (math.sqrt(2.0) if angle else 1.0)

            self.period = max(1, round(circumference / period))
            self.scale_x = circumference / (self.period * period)

        else:

            sphere = surface_data

            self.center = sphere.Center
            self.radius = sphere.Radius

            # concave: material normal points towards the centre
            self.sign = (
                1.0 if (frame.Origin - sphere.Center) * frame.ZAxis > 0
                else -1.0
            )

            # frame Z must point away from the centre for the mapping
            if self.sign < 0:
                frame = rg.Plane(frame.Origin, frame.YAxis, frame.XAxis)

            self.frame = frame

        self.angle = angle

        a = math.radians(angle)
        self.cos = math.cos(a)
        self.sin = math.sin(a)

        if face_type == "sphere":

            # angles must stay inside (-pi, pi): pi is the antipode
            limit = math.pi * self.radius
            self.domain = (-limit, limit, -limit, limit)

        else:

            coords = [self.coords(p) for p in corners]

            self.domain = (
                min(c[0] for c in coords), max(c[0] for c in coords),
                min(c[1] for c in coords), max(c[1] for c in coords)
            )

            if face_type == "cylinder":
                limit = math.pi * self.radius + 2.0 * pitch
                self.domain = (-limit, limit) + self.domain[2:]


    def point(self, x, y, offset):

        if self.face_type == "plane":

            f = self.frame

            return f.Origin + f.XAxis * x + f.YAxis * y + f.ZAxis * offset

        if self.face_type == "cylinder":

            tangent, axis, radial = self.axes
            phi = x / self.radius

            return (
                self.center +
                axis * (self.along0 + y) +
                (radial * math.cos(phi) + tangent * math.sin(phi)) *
                (self.radius + self.sign * offset)
            )

        # stereographic from the antipode; tan(angle / 2) keeps arc
        # lengths exact along both main axes
        f = self.frame
        a = 2.0 * math.tan(x / self.radius * 0.5)
        b = 2.0 * math.tan(y / self.radius * 0.5)
        q = a * a + b * b

        direction = (
            f.XAxis * (4.0 * a) +
            f.YAxis * (4.0 * b) +
            f.ZAxis * (4.0 - q)
        ) / (4.0 + q)

        return self.center + direction * (self.radius + self.sign * offset)


    def coords(self, point):

        # inverse of point() at offset 0, also projects nearby points

        if self.face_type == "plane":
            v = point - self.frame.Origin
            return v * self.frame.XAxis, v * self.frame.YAxis

        if self.face_type == "cylinder":
            tangent, axis, radial = self.axes
            v = point - self.center
            return (
                self.radius * math.atan2(v * tangent, v * radial),
                v * axis - self.along0
            )

        f = self.frame
        d = point - self.center
        d.Unitize()

        z = 1.0 + d * f.ZAxis

        return (
            2.0 * self.radius * math.atan2(d * f.XAxis, z),
            2.0 * self.radius * math.atan2(d * f.YAxis, z)
        )


    def lattice(self, u, v):

        # lattice coordinates (in pitches) -> (x, y)

        p = self.pitch

        return (
            p * (u * self.cos - v * self.sin) * self.scale_x,
            p * (u * self.sin + v * self.cos)
        )


    def canon(self, i, j):

        # one key per physical lattice point: wraps around the cylinder

        if not self.period:
            return i, j

        m = i // self.period

        # period vector in lattice units: (n, 0) at 0 deg, (n, -n) at 45 deg
        return i - m * self.period, j + (m * self.period if self.angle else 0)


    def window(self, patch):

        # (xmin, xmax, ymin, ymax) holding all cell centres to test

        if patch is None:
            return self.domain

        center, half = patch

        if self.face_type == "sphere":

            # coordinates wrap around at the antipode
            antipode = self.center - self.frame.ZAxis * self.radius

            if center.DistanceTo(antipode) <= half:
                return self.domain

            u = center - self.center
            u.Unitize()

            # circle of all sphere points at distance half from center
            a = 2.0 * math.asin(half / (2.0 * self.radius))

            circle = rg.Circle(
                rg.Plane(self.center + u * (self.radius * math.cos(a)), u),
                self.radius * math.sin(a)
            )

            coords = [
                self.coords(circle.PointAt(2.0 * math.pi * i / 64))
                for i in range(64)
            ]

            return (
                min(c[0] for c in coords), max(c[0] for c in coords),
                min(c[1] for c in coords), max(c[1] for c in coords)
            )

        cx, cy = self.coords(center)

        if self.face_type == "cylinder":
            return self.domain[:2] + (cy - half, cy + half)

        return (cx - half, cx + half, cy - half, cy + half)


    def cell_range(self, window):

        # lattice cell indices whose cells may lie in window

        xmin, xmax, ymin, ymax = window

        us, vs = [], []

        for x in (xmin, xmax):
            for y in (ymin, ymax):
                x0 = x / self.scale_x
                us.append((x0 * self.cos + y * self.sin) / self.pitch)
                vs.append((y * self.cos - x0 * self.sin) / self.pitch)

        return (
            range(math.floor(min(us)) - 1, math.ceil(max(us)) + 1),
            range(math.floor(min(vs)) - 1, math.ceil(max(vs)) + 1)
        )


    def cell_ok(self, corners, points):

        # sphere: drop cells crossing the antipode or shrunk near it

        if self.face_type != "sphere":
            return True

        limit = math.pi * self.radius

        if any(abs(x) >= limit or abs(y) >= limit for x, y in corners):
            return False

        return min(
            points[k].DistanceTo(points[k - 1]) for k in range(4)
        ) >= self.pitch * 0.5


# ============================================================
# KNURL MESH
# ============================================================

def create_knurl(grid, height, patch=None, clearance=None, tip=0.0):

    # pyramids: apex at offset tip, base corners at tip - height;
    # pyramids crossing the face outline are cut off there
    # clearance None: pyramid surface only (preview)
    # otherwise: closed cutter, capped at offset clearance
    # returns (mesh, pyramid count)

    # mm, keeps cut walls off the neighbouring faces
    inset = 0.01

    def surface(u, v):
        return grid.point(*grid.lattice(u, v), 0.0)

    tol = sc.doc.ModelAbsoluteTolerance

    def on_face(p):

        # the outline counts as outside, a seam between two faces as inside
        edges = 0

        for f in grid.faces:

            relation = f.IsPointOnFace(*face_uv(f, p), tol)

            if relation == rg.PointFaceRelation.Interior:
                return True

            if relation == rg.PointFaceRelation.Boundary:
                edges += 1

        return edges > 1

    base = {}

    def base_point(a2, b2):

        # lattice point at doubled lattice coords: (point, on face)
        if (a2, b2) not in base:
            p = surface(a2 * 0.5, b2 * 0.5)
            base[a2, b2] = (p, on_face(p))

        return base[a2, b2]

    # cell centres just outside the face may still overlap it
    margin = grid.pitch * 1.5
    xmin, xmax, ymin, ymax = grid.window(patch)
    xmin, xmax = xmin - margin, xmax + margin
    ymin, ymax = ymin - margin, ymax + margin

    irange, jrange = grid.cell_range((xmin, xmax, ymin, ymax))

    # canonical keys of kept cells (each also a valid lattice index)
    kept = set()

    for i in irange:
        for j in jrange:

            key = grid.canon(i, j)

            if key in kept:
                continue

            cx, cy = grid.lattice(i + 0.5, j + 0.5)

            if not (xmin <= cx <= xmax and ymin <= cy <= ymax):
                continue

            center, inside = base_point(2 * i + 1, 2 * j + 1)

            if patch is not None and center.DistanceTo(patch[0]) > patch[1]:
                continue

            corners = [(i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)]
            bases = [base_point(2 * a, 2 * b) for a, b in corners]

            if not (inside or any(ok for _, ok in bases)):
                continue

            if not grid.cell_ok(
                [grid.lattice(a, b) for a, b in corners],
                [p for p, _ in bases]
            ):
                continue

            kept.add(key)

    # cells touching only diagonally would share a wall edge four
    # times (non-manifold): drop one of them
    changed = True

    while changed:

        changed = False

        for i, j in list(kept):
            for di, dj in ((1, 1), (1, -1), (-1, 1), (-1, -1)):

                if (
                    grid.canon(i + di, j + dj) in kept and
                    grid.canon(i + di, j) not in kept and
                    grid.canon(i, j + dj) not in kept
                ):
                    kept.discard((i, j))
                    changed = True
                    break

    # surface vertices: (key, u, v, offset, on face)
    crossings = {}

    def crossing(p, q):

        # point where edge p (on face) -> q (off face) leaves the face
        key = ("x",) + tuple(sorted((p[0], q[0])))

        if key not in crossings:

            lo, hi = 0.0, 1.0

            for _ in range(12):

                t = (lo + hi) * 0.5

                if on_face(surface(
                    p[1] + (q[1] - p[1]) * t, p[2] + (q[2] - p[2]) * t
                )):
                    lo = t
                else:
                    hi = t

            length = surface(p[1], p[2]).DistanceTo(surface(q[1], q[2]))
            # never back onto p itself: no zero-length edges
            t = max(lo * 0.5, lo - inset / length)

            crossings[key] = (
                key,
                p[1] + (q[1] - p[1]) * t,
                p[2] + (q[2] - p[2]) * t,
                p[3] + (q[3] - p[3]) * t,
                True
            )

        return crossings[key]

    mesh = rg.Mesh()
    index = {}

    def vertex(d, cap):

        key = (cap, d[0])

        if key not in index:
            index[key] = mesh.Vertices.Add(
                grid.point(
                    *grid.lattice(d[1], d[2]),
                    clearance if cap else d[3]
                )
            )

        return index[key]

    def add_polygon(ids):

        if len(ids) <= 4:
            mesh.Faces.AddFace(*ids)
            return

        for k in range(1, len(ids) - 1):
            mesh.Faces.AddFace(ids[0], ids[k], ids[k + 1])

    # surface edge -> cap edge above it
    edges = {}

    # winding assumes (x, y, offset) is right-handed; fixed below if not
    for i, j in kept:

        apex = (
            ("a",) + grid.canon(i, j), i + 0.5, j + 0.5, tip,
            base_point(2 * i + 1, 2 * j + 1)[1]
        )

        corners = [
            (
                ("c",) + grid.canon(a, b), a, b, tip - height,
                base_point(2 * a, 2 * b)[1]
            )
            for a, b in ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))
        ]

        for k in range(4):

            triangle = [corners[(k + 1) % 4], corners[k], apex]
            polygon = []

            for n in range(3):

                p, q = triangle[n], triangle[(n + 1) % 3]

                if p[4]:
                    polygon.append(p)

                if p[4] != q[4]:
                    polygon.append(crossing(p, q) if p[4] else crossing(q, p))

            if len(polygon) < 3:
                continue

            ids = [vertex(d, False) for d in polygon]
            add_polygon(ids)

            if clearance is None:
                continue

            top = [vertex(d, True) for d in polygon]
            add_polygon(top[::-1])

            for n in range(len(ids)):
                n2 = (n + 1) % len(ids)
                edges[ids[n], ids[n2]] = (top[n], top[n2])

    # walls along the outline of the knurled area
    for (a, b), (ta, tb) in edges.items():

        if (b, a) not in edges:
            mesh.Faces.AddFace(b, a, ta, tb)

    # flip if (x, y, offset) is left-handed, e.g. inside a bore
    o = grid.point(0.0, 0.0, 0.0)

    if rg.Vector3d.CrossProduct(
        grid.point(1.0, 0.0, 0.0) - o,
        grid.point(0.0, 1.0, 0.0) - o
    ) * (grid.point(0.0, 0.0, 1.0) - o) < 0:
        mesh.Flip(True, True, True)

    mesh.Normals.ComputeNormals()

    return mesh, len(kept)


# ============================================================
# DISPLAY CONDUIT
# ============================================================

class KnurlPreviewConduit(Rhino.Display.DisplayConduit):

    def __init__(self):

        super().__init__()

        self.mesh = None
        self.material = Rhino.Display.DisplayMaterial(
            System.Drawing.Color.Orange
        )


    def CalculateBoundingBox(self, e):

        if self.mesh is not None:
            e.IncludeBoundingBox(self.mesh.GetBoundingBox(False))


    def PostDrawObjects(self, e):

        if self.mesh is not None:

            # pyramids lie below the face: draw them through it
            e.Display.EnableDepthTesting(False)
            e.Display.DrawMeshShaded(self.mesh, self.material)
            e.Display.EnableDepthTesting(True)


# ============================================================
# UI
# ============================================================

def make_label(text):
    lbl = forms.Label()
    lbl.Text = text
    return lbl


class KnurlDialog(forms.Dialog[bool]):

    PITCH = 1.0
    APEX_ANGLE = 90.0
    GRID_ANGLE = 0.0
    PATCH_SIZE = 10.0

    def __init__(
        self,
        faces,
        face_type,
        surface_data,
        origin,
        conduit
    ):

        super().__init__()

        self.faces = faces
        self.face_type = face_type
        self.surface_data = surface_data
        self.origin = origin
        self.preview_center = origin
        self.conduit = conduit

        self.Title = "Knurl Preview"
        self.Padding = drawing.Padding(12)
        self.Resizable = False

        self.result_generate = False

        self.pitch = self.make_number(self.PITCH)
        self.apex = self.make_number(self.APEX_ANGLE)
        self.angle = self.make_number(self.GRID_ANGLE)
        self.patch_size = self.make_number(self.PATCH_SIZE)

        self.readout = forms.Label()
        self.readout.Wrap = forms.WrapMode.Word
        self.readout.Width = 260

        self.btn_origin = forms.Button()
        self.btn_origin.Text = "Set knurl origin"
        self.btn_origin.Click += self.on_set_origin

        self.btn_move = forms.Button()
        self.btn_move.Text = "Move preview center"
        self.btn_move.Click += self.on_move_preview

        self.btn_generate = forms.Button()
        self.btn_generate.Text = "Generate"
        self.btn_generate.Click += self.on_generate

        self.btn_cancel = forms.Button()
        self.btn_cancel.Text = "Cancel"
        self.btn_cancel.Click += self.on_cancel

        self.DefaultButton = self.btn_generate
        self.AbortButton = self.btn_cancel

        for control in [
            self.pitch,
            self.apex,
            self.angle,
            self.patch_size
        ]:
            control.TextChanged += self.on_parameter_changed

        layout = forms.DynamicLayout()

        layout.Spacing = drawing.Size(6, 7)

        layout.AddRow(make_label("Pitch"), self.pitch, make_label("mm"))
        layout.AddRow(make_label("Apex angle"), self.apex, make_label("°"))
        layout.AddRow(make_label("Grid angle"), self.angle, make_label("°"))
        layout.AddRow(
            make_label("Preview patch"), self.patch_size, make_label("mm")
        )

        layout.AddRow(None)
        layout.AddRow(self.readout)
        layout.AddRow(None)
        layout.AddRow(self.btn_origin, self.btn_move)
        layout.AddRow(None)
        layout.AddRow(self.btn_generate, self.btn_cancel)

        self.Content = layout

        self.update_preview()


    # ========================================================
    # CONTROLS
    # ========================================================

    def make_number(self, value):

        tb = forms.TextBox()
        tb.Text = str(value)
        tb.Width = 75

        return tb


    def value(self, control, fallback):

        try:
            value = float(control.Text)

            if math.isnan(value):
                return fallback

            return value

        except:
            return fallback


    # ========================================================
    # PARAMETERS
    # ========================================================

    def parameters(self):

        # returns (grid, pyramid height, patch size)

        # preview cost grows with (patch / pitch)^2
        pitch = max(0.2, self.value(self.pitch, self.PITCH))

        apex = min(170.0, max(10.0, self.value(self.apex, self.APEX_ANGLE)))

        angle = self.value(self.angle, self.GRID_ANGLE)

        patch_size = max(
            pitch * 2.0,
            self.value(self.patch_size, self.PATCH_SIZE)
        )

        grid = KnurlGrid(
            self.faces,
            self.face_type,
            self.surface_data,
            self.origin,
            pitch,
            angle
        )

        height = pitch * 0.5 / math.tan(math.radians(apex) * 0.5)

        return grid, height, patch_size


    # ========================================================
    # PREVIEW
    # ========================================================

    def update_preview(self):

        grid, height, patch_size = self.parameters()

        mesh, count = create_knurl(
            grid,
            height,
            (self.preview_center, patch_size * 0.5)
        )

        self.conduit.mesh = mesh

        text = (
            "Pyramid height: {:.3f} mm\n"
            "{} preview pyramids"
        ).format(height, count)

        if grid.period:
            text += "\n{} cells around, {:.3f} mm, grid angle {:g}°".format(
                grid.period,
                grid.pitch * grid.scale_x,
                grid.angle
            )

        self.readout.Text = text

        sc.doc.Views.Redraw()


    # ========================================================
    # EVENTS
    # ========================================================

    def on_parameter_changed(self, sender, e):

        self.update_preview()


    def pick(self, prompt):

        # Hide the window temporarily so picking is pleasant
        self.Visible = False

        p = pick_face_point(self.faces[0], prompt)

        self.Visible = True

        return p


    def on_set_origin(self, sender, e):

        p = self.pick("Click the knurl origin")

        if p is not None:
            self.origin = p
            self.update_preview()


    def on_move_preview(self, sender, e):

        p = self.pick("Click the preview center")

        if p is not None:
            self.preview_center = p
            self.update_preview()


    def on_generate(self, sender, e):

        self.result_generate = True
        self.Close(True)


    def on_cancel(self, sender, e):

        self.result_generate = False
        self.Close(False)


# ============================================================
# GENERATE
# ============================================================

def generate_knurl(brep, grid, height):

    # tips a hair below the surface: a cutter touching a planar part
    # face only in points breaks mesh booleans
    cutter, count = create_knurl(
        grid,
        height,
        clearance=grid.pitch * 0.5,
        tip=-0.001
    )

    if not cutter.IsClosed:
        print("Knurl: cutter mesh is not closed, boolean may fail")

    mp = rg.MeshingParameters()
    mp.Tolerance = 0.01
    mp.JaggedSeams = False
    mp.SimplePlanes = True

    part = rg.Mesh()

    for m in rg.Mesh.CreateFromBrep(brep, mp):
        part.Append(m)

    part.Vertices.CombineIdentical(True, True)

    # open polysurfaces: close gaps, then orient all faces alike
    if not part.IsClosed:
        part.HealNakedEdges(0.01)
        part.FillHoles()

    part.UnifyNormals()

    if part.SolidOrientation() == -1:
        part.Flip(True, True, True)

    if not part.IsClosed:
        print("Knurl: part mesh is not closed, boolean may fail")

    result = rg.Mesh.CreateBooleanDifference([part], [cutter])

    sc.doc.Objects.UnselectAll()

    if not result:

        # leave the cutter for a manual boolean
        sc.doc.Objects.Select(sc.doc.Objects.Add(cutter))
        sc.doc.Views.Redraw()

        print(
            "Knurl: boolean failed, added the cutter "
            "({} pyramids) instead".format(count)
        )

        return

    for m in result:
        sc.doc.Objects.Select(sc.doc.Objects.Add(m))

    sc.doc.Views.Redraw()

    print("Knurl: {} pyramids, result selected".format(count))


# ============================================================
# MAIN
# ============================================================

def main():

    face = select_face()

    if face is None:
        return

    face_type, surface_data = classify_face(face)

    if face_type is None:

        print(
            "Unsupported surface. "
            "Knurl accepts planar, cylindrical or spherical faces."
        )

        return

    faces = matching_faces(face, face_type, surface_data)

    print(
        "Detected surface type: {}, {} face(s)".format(face_type, len(faces))
    )

    origin = pick_face_point(
        face, "Click the knurl origin (also the preview center)"
    )

    if origin is None:
        return

    conduit = KnurlPreviewConduit()

    conduit.Enabled = True

    dlg = KnurlDialog(
        faces,
        face_type,
        surface_data,
        origin,
        conduit
    )

    try:

        parent = Rhino.UI.RhinoEtoApp.MainWindowForDocument(sc.doc)

        Rhino.UI.EtoExtensions.ShowSemiModal(dlg, sc.doc, parent)

    finally:

        conduit.Enabled = False
        sc.doc.Views.Redraw()

    if dlg.result_generate:

        grid, height, patch_size = dlg.parameters()

        generate_knurl(face.Brep, grid, height)


if __name__ == "__main__":
    main()
