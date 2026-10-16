# Knurl

Pyramid knurling to diffuse light in transparent SLA prints. Works on planar, cylindrical (inside and outside) and spherical faces.

1. Fusion export STEP
2. Rhino import STEP (default options)
3. Run knurl script, pick the face, then the knurl origin
    - Pyramid tips lie on the original surface, nothing sticks out
    - Pyramids crossing the face outline are cut off there
    - Cylinders snap the pitch around so the seam closes (grid angle 0° or 45°)
    - Spheres keep a small bare spot opposite the origin
4. "Generate" joins the part (STEP imports come as loose surfaces in a block), subtracts the pyramids and adds the result mesh in place (selected), the original stays
5. Rhino export the selected mesh as STL
6. Into Fusion only via Insert → Insert Mesh, units mm
