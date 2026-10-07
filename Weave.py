#! python3

import Rhino
import Rhino.Geometry as rg
import Rhino.Input as ri
import Rhino.Input.Custom as ric
import Rhino.UI

import scriptcontext as sc

import Eto.Forms as forms
import Eto.Drawing as drawing

import System.Drawing

import math


# ============================================================
# SETTINGS NEW v6
# ============================================================

DEFAULT_SPACING = 2.0
DEFAULT_DIAMETER = 1.0
DEFAULT_DEPTH = 0.25
DEFAULT_HEIGHT = 0.25
DEFAULT_ANGLE = 0.0
DEFAULT_PATCH_SIZE = 20.0

PREVIEW_SAMPLES_PER_SEGMENT = 5


# ============================================================
# SELECTION
# ============================================================

def select_face():
    go = ric.GetObject()
    go.SetCommandPrompt("Select planar or spherical Brep face")

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

    if face is not None:
        return face

    brep = objref.Brep()

    if brep.Faces.Count == 1:
        return brep.Faces[0]

    # whole polysurface picked: take the face under the click
    pt = objref.SelectionPoint()

    if not pt.IsValid:
        print("Click directly on the face to weave.")
        return None

    def distance(f):

        if not point_is_on_face(f, pt):
            return math.inf

        return f.PointAt(*face_uv(f, pt)).DistanceTo(pt)

    return min(brep.Faces, key=distance)


def classify_face(face):

    surf = face.UnderlyingSurface()
    tol = sc.doc.ModelAbsoluteTolerance

    ok, sphere = surf.TryGetSphere(tol)

    if ok:
        return "sphere", sphere

    ok, plane = surf.TryGetPlane(tol)

    if ok:
        return "plane", plane

    return None, None


def matching_faces(face, face_type, surface_data):

    # picked face first, then all faces of its Brep on the same sphere / plane

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

        return (
            abs(surface_data.DistanceTo(other.Origin)) < tol and
            surface_data.ZAxis.IsParallelTo(other.ZAxis) != 0
        )

    return [face] + [
        f for f in face.Brep.Faces
        if f.FaceIndex != face.FaceIndex and same(f)
    ]


# ============================================================
# PICK PREVIEW LOCATION
# ============================================================

def pick_preview_point(face):

    gp = ric.GetPoint()
    gp.SetCommandPrompt("Click where you want the weave preview")

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


# ============================================================
# WEAVE GEOMETRY
# ============================================================

def create_local_frame(face, center, angle):

    uv = face_uv(face, center)

    if uv is None:
        return None

    ok, plane = face.FrameAt(*uv)

    if not ok:
        return None

    x = plane.XAxis
    y = plane.YAxis

    # swapping X and Y flips Z: respect flipped Brep faces
    if face.OrientationIsReversed:
        x, y = y, x

    # rotate the weave grid around the surface normal
    a = math.radians(angle)

    return rg.Plane(
        plane.Origin,
        x * math.cos(a) + y * math.sin(a),
        y * math.cos(a) - x * math.sin(a)
    )


def strand_point(
    face_type,
    surface_data,
    frame,
    family,
    c,
    t,
    offset
):

    # c: strand coordinate, t: position along strand
    # plane: both in mm
    # sphere: both angles in radians, mapped stereographically from the
    # antipode, so every strand is a circle through the antipode

    if family == 0:
        along, across = frame.XAxis, frame.YAxis
    else:
        along, across = frame.YAxis, frame.XAxis

    if face_type == "plane":

        return (
            frame.Origin +
            along * t +
            across * c +
            frame.ZAxis * offset
        )

    sphere = surface_data

    # tan(angle / 2) keeps arc spacing exact along both main axes
    a = 2.0 * math.tan(t * 0.5)
    b = 2.0 * math.tan(c * 0.5)
    q = a * a + b * b

    direction = (
        along * (4.0 * a) +
        across * (4.0 * b) +
        frame.ZAxis * (4.0 - q)
    ) / (4.0 + q)

    return sphere.Center + direction * (sphere.Radius + offset)


def weave_wave(
    family,
    strand_index,
    phase,
    depth,
    height
):

    # integer phase k = crossing with strand k of the other family
    w = math.cos(math.pi * (phase + strand_index))

    # opposite phase for the crossing family
    if family == 1:
        w = -w

    return -depth + height * w


def create_weave_strands(
    faces,
    face_type,
    surface_data,
    center,
    spacing,
    depth,
    height,
    angle,
    patch_size=None
):

    # patch_size None = whole face
    # returns strands as lists of (yarn point, pill bottom point, normal)

    frame = create_local_frame(faces[0], center, angle)

    if frame is None:
        return []

    if face_type == "sphere":

        sphere = surface_data

        # point Z away from the sphere centre
        if (frame.Origin - sphere.Center) * frame.ZAxis < 0:
            frame = rg.Plane(frame.Origin, frame.YAxis, frame.XAxis)

        # angles must stay inside (-pi, pi): pi is the antipode
        step = spacing / sphere.Radius
        strand_max = int(math.ceil(math.pi / step)) - 1
        sample_max = int(
            math.ceil(math.pi / step * PREVIEW_SAMPLES_PER_SEGMENT)
        ) - 1
        mm_per_unit = sphere.Radius

    else:

        step = spacing

        extent = max(
            frame.Origin.DistanceTo(p)
            for f in faces
            for p in f.GetBoundingBox(True).GetCorners()
        )

        strand_max = int(math.ceil(extent / step))
        sample_max = strand_max * PREVIEW_SAMPLES_PER_SEGMENT
        mm_per_unit = 1.0

    if patch_size is not None:

        half = patch_size * 0.5
        limit = int(math.ceil(half / mm_per_unit / step))

        strand_max = min(strand_max, limit)
        sample_max = min(sample_max, limit * PREVIEW_SAMPLES_PER_SEGMENT)

    sample_step = step / PREVIEW_SAMPLES_PER_SEGMENT

    strands = []

    for family in (0, 1):

        for k in range(-strand_max, strand_max + 1):

            c = k * step

            segments = [[]]

            for m in range(-sample_max, sample_max + 1):

                t = m * sample_step

                base = strand_point(
                    face_type, surface_data, frame, family, c, t, 0.0
                )

                inside = (
                    (patch_size is None or base.DistanceTo(frame.Origin) <= half)
                    and any(point_is_on_face(f, base) for f in faces)
                )

                if inside:

                    offset = weave_wave(
                        family,
                        k,
                        t / step,
                        depth,
                        height
                    )

                    segments[-1].append((
                        strand_point(
                            face_type, surface_data, frame, family, c, t, offset
                        ),
                        strand_point(
                            face_type, surface_data, frame, family, c, t,
                            -depth - height
                        ),
                        strand_point(
                            face_type, surface_data, frame, family, c, t, 1.0
                        ) - base
                    ))

                elif segments[-1]:
                    segments.append([])

            strands.extend(seg for seg in segments if len(seg) >= 2)

    return strands


def strand_curve(strand):

    return rg.Curve.CreateInterpolatedCurve(
        [sample[0] for sample in strand],
        3,
        rg.CurveKnotStyle.Chord
    )


# ============================================================
# DISPLAY CONDUIT
# ============================================================

class WeavePreviewConduit(Rhino.Display.DisplayConduit):

    def __init__(self):

        super().__init__()

        self.geometry = []
        self.material = Rhino.Display.DisplayMaterial(
            System.Drawing.Color.Orange
        )

        self.bounds = rg.BoundingBox.Empty


    def set_geometry(self, geometry):

        self.geometry = geometry

        bbox = rg.BoundingBox.Empty

        for g in geometry:
            bbox.Union(g.GetBoundingBox(True))

        self.bounds = bbox


    def CalculateBoundingBox(self, e):

        if self.bounds.IsValid:
            e.IncludeBoundingBox(self.bounds)


    def PostDrawObjects(self, e):

        for g in self.geometry:

            if isinstance(g, rg.Mesh):
                e.Display.DrawMeshShaded(g, self.material)
            else:
                e.Display.DrawCurve(g, System.Drawing.Color.DeepSkyBlue, 2)


# ============================================================
# SOLIDS
# ============================================================

def create_yarn_geometry(strands, diameter, solids, pill):

    if not solids:
        return [strand_curve(strand) for strand in strands]

    return [
        create_yarn_mesh(strand, diameter * 0.5, pill)
        for strand in strands
    ]


def create_yarn_mesh(strand, radius, pill, sides=16):

    # pill: yarn circle stretched down along the normal to a
    # constant bottom below the surface -> no undercuts
    # otherwise: round tube

    count = len(strand)
    half = sides // 2
    ring = sides + 2

    mesh = rg.Mesh()

    for i, (top, bottom, normal) in enumerate(strand):

        if not pill:
            bottom = top

        prev = strand[max(i - 1, 0)][0]
        nxt = strand[min(i + 1, count - 1)][0]

        side = rg.Vector3d.CrossProduct(normal, nxt - prev)
        side.Unitize()

        # upper half circle around top, lower half around bottom
        for k in range(ring):

            center, a = (top, k) if k <= half else (bottom, k - 1)

            angle = 2.0 * math.pi * a / sides

            mesh.Vertices.Add(
                center +
                side * (radius * math.cos(angle)) +
                normal * (radius * math.sin(angle))
            )

    for i in range(count - 1):

        j = i + 1

        for k in range(ring):

            k2 = (k + 1) % ring

            mesh.Faces.AddFace(
                i * ring + k,
                i * ring + k2,
                j * ring + k2,
                j * ring + k
            )

    for i in (0, count - 1):

        top, bottom, normal = strand[i]

        c = mesh.Vertices.Add((top + bottom) * 0.5 if pill else top)

        for k in range(ring):

            a, b = i * ring + k, i * ring + (k + 1) % ring

            # wind opposite to the tube faces sharing this edge
            if i == 0:
                a, b = b, a

            mesh.Faces.AddFace(c, a, b)

    # top == bottom (pill at its lowest, or round tube) leaves coincident
    # vertices; Rhino refuses to add meshes with degenerate faces
    mesh.Vertices.CombineIdentical(True, True)
    mesh.Faces.CullDegenerateFaces()

    if mesh.SolidOrientation() == -1:
        mesh.Flip(True, True, True)

    mesh.Normals.ComputeNormals()

    return mesh


# ============================================================
# UI
# ============================================================
def make_label(text):
    lbl = forms.Label()
    lbl.Text = text
    return lbl
    
class WeaveDialog(forms.Dialog[bool]):

    def __init__(
        self,
        faces,
        face_type,
        surface_data,
        preview_center,
        conduit
    ):

        super().__init__()

        self.faces = faces
        self.face_type = face_type
        self.surface_data = surface_data
        self.preview_center = preview_center
        self.conduit = conduit

        self.Title = "Weave Preview"
        self.Padding = drawing.Padding(12)
        self.Resizable = False

        self.result_generate = False


        # ====================================================
        # INPUTS
        # ====================================================

        self.spacing = self.make_number(DEFAULT_SPACING)
        self.diameter = self.make_number(DEFAULT_DIAMETER)
        self.depth = self.make_number(DEFAULT_DEPTH)
        self.height = self.make_number(DEFAULT_HEIGHT)
        self.angle = self.make_number(DEFAULT_ANGLE)
        self.patch_size = self.make_number(DEFAULT_PATCH_SIZE)


        # ====================================================
        # PREVIEW TYPE
        # ====================================================

        self.preview_type = forms.DropDown()

        self.preview_type.Items.Add("Solid preview")
        self.preview_type.Items.Add("Centerlines")

        self.preview_type.SelectedIndex = 0

        self.pill = forms.CheckBox()
        self.pill.Text = "Printable pill profile (no undercuts)"
        self.pill.Checked = True


        # ====================================================
        # READOUT
        # ====================================================

        self.readout = forms.Label()
        self.readout.Wrap = forms.WrapMode.Word
        self.readout.Width = 260


        # ====================================================
        # BUTTONS
        # ====================================================

        self.btn_move = forms.Button()
        self.btn_move.Text = "Move preview"
        self.btn_move.Click += self.on_move_preview


        self.btn_generate = forms.Button()
        self.btn_generate.Text = "Generate full weave"
        self.btn_generate.Click += self.on_generate


        self.btn_cancel = forms.Button()
        self.btn_cancel.Text = "Cancel"
        self.btn_cancel.Click += self.on_cancel


        self.DefaultButton = self.btn_generate
        self.AbortButton = self.btn_cancel


        # ====================================================
        # EVENTS
        # ====================================================

        for control in [
            self.spacing,
            self.diameter,
            self.depth,
            self.height,
            self.angle,
            self.patch_size
        ]:
            control.TextChanged += self.on_parameter_changed

        self.preview_type.SelectedIndexChanged += self.on_parameter_changed
        self.pill.CheckedChanged += self.on_parameter_changed


        # ====================================================
        # LAYOUT
        # ====================================================

        layout = forms.DynamicLayout()

        layout.Spacing = drawing.Size(6, 7)

        label_spacing = forms.Label()
        label_spacing.Text = "Spacing"

        label_mm = forms.Label()
        label_mm.Text = "mm"

        layout.AddRow(
            label_spacing,
            self.spacing,
            label_mm
        )
        
        layout.AddRow(
            make_label("Yarn diameter"),
            self.diameter,
            make_label("mm")
        )

        layout.AddRow(
            make_label("Sink depth"),
            self.depth,
            make_label("mm")
        )

        layout.AddRow(
            make_label("Over / under height"),
            self.height,
            make_label("mm")
        )

        layout.AddRow(
            make_label("Angle"),
            self.angle,
            make_label("°")
        )

        layout.AddRow(
            make_label("Preview patch"),
            self.patch_size,
            make_label("mm")
        )

        layout.AddRow(None)

        layout.AddRow(
            make_label("Preview"),
            self.preview_type
        )

        layout.AddRow(None, self.pill)

        layout.AddRow(None)

        layout.AddRow(self.readout)

        layout.AddRow(None)

        layout.AddRow(self.btn_move)

        layout.AddRow(None)

        layout.AddRow(
            self.btn_generate,
            self.btn_cancel
        )

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

        # preview cost grows with (patch / spacing)^2
        spacing = max(
            1.0,
            self.value(self.spacing, DEFAULT_SPACING)
        )

        diameter = max(
            0.05,
            self.value(self.diameter, DEFAULT_DIAMETER)
        )

        depth = max(
            0.0,
            self.value(self.depth, DEFAULT_DEPTH)
        )

        height = max(
            0.0,
            self.value(self.height, DEFAULT_HEIGHT)
        )

        angle = self.value(self.angle, DEFAULT_ANGLE)

        patch_size = max(
            spacing * 2.0,
            self.value(
                self.patch_size,
                DEFAULT_PATCH_SIZE
            )
        )

        return (
            spacing,
            diameter,
            depth,
            height,
            angle,
            patch_size
        )


    # ========================================================
    # PREVIEW
    # ========================================================

    def update_preview(self):

        (
            spacing,
            diameter,
            depth,
            height,
            angle,
            patch_size
        ) = self.parameters()


        strands = create_weave_strands(
            self.faces,
            self.face_type,
            self.surface_data,
            self.preview_center,
            spacing,
            depth,
            height,
            angle,
            patch_size
        )

        self.conduit.set_geometry(
            create_yarn_geometry(
                strands,
                diameter,
                self.preview_type.SelectedIndex == 0,
                self.pill.Checked == True
            )
        )


        radius = diameter * 0.5

        highest = radius - depth + height

        lowest = -radius - depth - height


        self.readout.Text = (
            "Approx. outermost yarn point: "
            "{:+.2f} mm\n"
            "Approx. deepest yarn point: "
            "{:+.2f} mm\n"
            "{} preview strand segments"
        ).format(
            highest,
            lowest,
            len(strands)
        )


        sc.doc.Views.Redraw()


    # ========================================================
    # EVENTS
    # ========================================================

    def on_parameter_changed(self, sender, e):

        self.update_preview()


    def on_move_preview(self, sender, e):

        # Hide the window temporarily so picking is pleasant
        self.Visible = False

        p = pick_preview_point(self.faces[0])

        self.Visible = True

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
# FULL WEAVE
# ============================================================

def generate_full_weave(
    faces,
    face_type,
    surface_data,
    center,
    spacing,
    diameter,
    depth,
    height,
    angle,
    solids,
    pill
):

    strands = create_weave_strands(
        faces,
        face_type,
        surface_data,
        center,
        spacing,
        depth,
        height,
        angle
    )

    geometry = create_yarn_geometry(strands, diameter, solids, pill)

    sc.doc.Objects.UnselectAll()

    added = 0

    for g in geometry:

        obj_id = sc.doc.Objects.Add(g)

        if obj_id != System.Guid.Empty:
            sc.doc.Objects.Select(obj_id)
            added += 1

    sc.doc.Views.Redraw()

    print(
        "Weave: added {} of {} objects".format(added, len(geometry))
    )


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
            "This version currently accepts planar or spherical faces."
        )

        return


    faces = matching_faces(face, face_type, surface_data)

    print(
        "Detected surface type: {}, {} face(s)".format(face_type, len(faces))
    )


    center = pick_preview_point(face)

    if center is None:
        return


    conduit = WeavePreviewConduit()

    conduit.Enabled = True

    sc.doc.Views.Redraw()


    dlg = WeaveDialog(
        faces,
        face_type,
        surface_data,
        center,
        conduit
    )


    try:

        parent = Rhino.UI.RhinoEtoApp.MainWindowForDocument(
            sc.doc
        )

        Rhino.UI.EtoExtensions.ShowSemiModal(
            dlg,
            sc.doc,
            parent
        )

    finally:

        conduit.Enabled = False
        sc.doc.Views.Redraw()


    if dlg.result_generate:

        (
            spacing,
            diameter,
            depth,
            height,
            angle,
            patch_size
        ) = dlg.parameters()

        generate_full_weave(
            faces,
            face_type,
            surface_data,
            dlg.preview_center,
            spacing,
            diameter,
            depth,
            height,
            angle,
            dlg.preview_type.SelectedIndex == 0,
            dlg.pill.Checked == True
        )


if __name__ == "__main__":
    main()