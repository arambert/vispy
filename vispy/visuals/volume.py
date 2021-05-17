# -*- coding: utf-8 -*-
# Copyright (c) Vispy Development Team. All Rights Reserved.
# Distributed under the (new) BSD License. See LICENSE.txt for more info.

"""
About this technique
--------------------

In Python, we define the six faces of a cuboid to draw, as well as
texture cooridnates corresponding with the vertices of the cuboid. 
The back faces of the cuboid are drawn (and front faces are culled)
because only the back faces are visible when the camera is inside the 
volume.

In the vertex shader, we intersect the view ray with the near and far 
clipping planes. In the fragment shader, we use these two points to
compute the ray direction and then compute the position of the front
cuboid surface (or near clipping plane) along the view ray.

Next we calculate the number of steps to walk from the front surface
to the back surface and iterate over these positions in a for-loop.
At each iteration, the fragment color or other voxel information is 
updated depending on the selected rendering method.

It is important for the texture interpolation is 'linear' for most volumes,
since with 'nearest' the result can look very ugly; however for volumes with
discrete data 'nearest' is sometimes appropriate. The wrapping should be
clamp_to_edge to avoid artifacts when the ray takes a small step outside the
volume.

The ray direction is established by mapping the vertex to the document
coordinate frame, adjusting z to +/-1, and mapping the coordinate back.
The ray is expressed in coordinates local to the volume (i.e. texture
coordinates).

"""
from ._scalable_textures import CPUScaledTexture3D, GPUScaledTextured3D
from ..gloo import VertexBuffer, IndexBuffer
from ..gloo.texture import should_cast_to_f32
from . import Visual
from .shaders import Function
from ..color import get_colormap

import numpy as np

# todo: implement more render methods (port from visvis)
# todo: allow anisotropic data
# todo: what to do about lighting? ambi/diffuse/spec/shinynes on each visual?


# Vertex shader
VERT_SHADER = """
attribute vec3 a_position;
// attribute vec3 a_texcoord;
uniform vec3 u_shape;

// varying vec3 v_texcoord;
varying vec3 v_position;
varying vec4 v_nearpos;
varying vec4 v_farpos;

void main() {
    // v_texcoord = a_texcoord;
    v_position = a_position;
    
    // Project local vertex coordinate to camera position. Then do a step
    // backward (in cam coords) and project back. Voila, we get our ray vector.
    vec4 pos_in_cam = $viewtransformf(vec4(v_position, 1));

    // intersection of ray and near clipping plane (z = -1 in clip coords)
    pos_in_cam.z = -pos_in_cam.w;
    v_nearpos = $viewtransformi(pos_in_cam);
    
    // intersection of ray and far clipping plane (z = +1 in clip coords)
    pos_in_cam.z = pos_in_cam.w;
    v_farpos = $viewtransformi(pos_in_cam);
    
    gl_Position = $transform(vec4(v_position, 1.0));
}
"""  # noqa

# Fragment shader
FRAG_SHADER = """
// uniforms
uniform $sampler_type u_volumetex;
uniform vec3 u_shape;
uniform vec2 clim;
uniform float gamma;
uniform float u_threshold;
uniform float u_relative_step_size;

//varyings
// varying vec3 v_texcoord;
varying vec3 v_position;
varying vec4 v_nearpos;
varying vec4 v_farpos;

// uniforms for lighting. Hard coded until we figure out how to do lights
const vec4 u_ambient = vec4(0.2, 0.2, 0.2, 1.0);
const vec4 u_diffuse = vec4(0.8, 0.2, 0.2, 1.0);
const vec4 u_specular = vec4(1.0, 1.0, 1.0, 1.0);
const float u_shininess = 40.0;

//varying vec3 lightDirs[1];

// global holding view direction in local coordinates
vec3 view_ray;

float rand(vec2 co)
{{
    // Create a pseudo-random number between 0 and 1.
    // http://stackoverflow.com/questions/4200224
    return fract(sin(dot(co.xy ,vec2(12.9898, 78.233))) * 43758.5453);
}}

float colorToVal(vec4 color1)
{{
    return color1.r; // todo: why did I have this abstraction in visvis?
}}

vec4 applyColormap(float data) {{
    if (clim.x < clim.y) {{
        data = clamp(data, clim.x, clim.y);
    }} else {{
        data = clamp(data, clim.y, clim.x);
    }}

    data = (data - clim.x) / (clim.y - clim.x);
    return $cmap(pow(data, gamma));
}}


vec4 calculateColor(vec4 betterColor, vec3 loc, vec3 step)
{{   
    // Calculate color by incorporating lighting
    vec4 color1;
    vec4 color2;
    
    // View direction
    vec3 V = normalize(view_ray);
    
    // calculate normal vector from gradient
    vec3 N; // normal
    color1 = $sample( u_volumetex, loc+vec3(-step[0],0.0,0.0) );
    color2 = $sample( u_volumetex, loc+vec3(step[0],0.0,0.0) );
    N[0] = colorToVal(color1) - colorToVal(color2);
    betterColor = max(max(color1, color2),betterColor);
    color1 = $sample( u_volumetex, loc+vec3(0.0,-step[1],0.0) );
    color2 = $sample( u_volumetex, loc+vec3(0.0,step[1],0.0) );
    N[1] = colorToVal(color1) - colorToVal(color2);
    betterColor = max(max(color1, color2),betterColor);
    color1 = $sample( u_volumetex, loc+vec3(0.0,0.0,-step[2]) );
    color2 = $sample( u_volumetex, loc+vec3(0.0,0.0,step[2]) );
    N[2] = colorToVal(color1) - colorToVal(color2);
    betterColor = max(max(color1, color2),betterColor);
    float gm = length(N); // gradient magnitude
    N = normalize(N);
    
    // Flip normal so it points towards viewer
    float Nselect = float(dot(N,V) > 0.0);
    N = (2.0*Nselect - 1.0) * N;  // ==  Nselect * N - (1.0-Nselect)*N;
    
    // Get color of the texture (albeido)
    color1 = betterColor;
    color2 = color1;
    // todo: parametrise color1_to_color2
    
    // Init colors
    vec4 ambient_color = vec4(0.0, 0.0, 0.0, 0.0);
    vec4 diffuse_color = vec4(0.0, 0.0, 0.0, 0.0);
    vec4 specular_color = vec4(0.0, 0.0, 0.0, 0.0);
    vec4 final_color;
    
    // todo: allow multiple light, define lights on viewvox or subscene
    int nlights = 1; 
    for (int i=0; i<nlights; i++)
    {{ 
        // Get light direction (make sure to prevent zero devision)
        vec3 L = normalize(view_ray);  //lightDirs[i]; 
        float lightEnabled = float( length(L) > 0.0 );
        L = normalize(L+(1.0-lightEnabled));
        
        // Calculate lighting properties
        float lambertTerm = clamp( dot(N,L), 0.0, 1.0 );
        vec3 H = normalize(L+V); // Halfway vector
        float specularTerm = pow( max(dot(H,N),0.0), u_shininess);
        
        // Calculate mask
        float mask1 = lightEnabled;
        
        // Calculate colors
        ambient_color +=  mask1 * u_ambient;  // * gl_LightSource[i].ambient;
        diffuse_color +=  mask1 * lambertTerm;
        specular_color += mask1 * specularTerm * u_specular;
    }}
    
    // Calculate final color by componing different components
    final_color = color2 * ( ambient_color + diffuse_color) + specular_color;
    final_color.a = color2.a;
    
    // Done
    return final_color;
}}

// for some reason, this has to be the last function in order for the
// filters to be inserted in the correct place...

void main() {{
    vec3 farpos = v_farpos.xyz / v_farpos.w;
    vec3 nearpos = v_nearpos.xyz / v_nearpos.w;

    // Calculate unit vector pointing in the view direction through this
    // fragment.
    view_ray = normalize(farpos.xyz - nearpos.xyz);

    // Compute the distance to the front surface or near clipping plane
    float distance = dot(nearpos-v_position, view_ray);
    distance = max(distance, min((-0.5 - v_position.x) / view_ray.x,
                            (u_shape.x - 0.5 - v_position.x) / view_ray.x));
    distance = max(distance, min((-0.5 - v_position.y) / view_ray.y,
                            (u_shape.y - 0.5 - v_position.y) / view_ray.y));
    distance = max(distance, min((-0.5 - v_position.z) / view_ray.z,
                            (u_shape.z - 0.5 - v_position.z) / view_ray.z));

    // Now we have the starting position on the front surface
    vec3 front = v_position + view_ray * distance;

    // Decide how many steps to take
    int nsteps = int(-distance / u_relative_step_size + 0.5);
    float f_nsteps = float(nsteps);
    if( nsteps < 1 )
        discard;

    // Get starting location and step vector in texture coordinates
    vec3 step = ((v_position - front) / u_shape) / f_nsteps;
    vec3 start_loc = front / u_shape;

    // For testing: show the number of steps. This helps to establish
    // whether the rays are correctly oriented
    //gl_FragColor = vec4(0.0, f_nsteps / 3.0 / u_shape.x, 1.0, 1.0);
    //return;

    {before_loop}

    // This outer loop seems necessary on some systems for large
    // datasets. Ugly, but it works ...
    vec3 loc = start_loc;
    int iter = 0;
    while (iter < nsteps) {{
        for (iter=iter; iter<nsteps; iter++)
        {{
            // Get sample color
            vec4 color = $sample(u_volumetex, loc);
            float val = color.r;

            {in_loop}

            // Advance location deeper into the volume
            loc += step;
        }}
    }}

    {after_loop}

    /* Set depth value - from visvis TODO
    int iter_depth = int(maxi);
    // Calculate end position in world coordinates
    vec4 position2 = vertexPosition;
    position2.xyz += ray*shape*float(iter_depth);
    // Project to device coordinates and set fragment depth
    vec4 iproj = gl_ModelViewProjectionMatrix * position2;
    iproj.z /= iproj.w;
    gl_FragDepth = (iproj.z+1.0)/2.0;
    */
}}


"""  # noqa


MIP_SNIPPETS = dict(
    before_loop="""
        float maxval = -99999.0; // The maximum encountered value
        int maxi = 0;  // Where the maximum value was encountered
        """,
    in_loop="""
        if( val > maxval ) {
            maxval = val;
            maxi = iter;
        }
        """,
    after_loop="""
        // Refine search for max value
        loc = start_loc + step * (float(maxi) - 0.5);
        for (int i=0; i<10; i++) {
            maxval = max(maxval, $sample(u_volumetex, loc).r);
            loc += step * 0.1;
        }
        gl_FragColor = applyColormap(maxval);
        """,
)
MIP_FRAG_SHADER = FRAG_SHADER.format(**MIP_SNIPPETS)


TRANSLUCENT_SNIPPETS = dict(
    before_loop="""
        vec4 integrated_color = vec4(0., 0., 0., 0.);
        """,
    in_loop="""
            color = applyColormap(val);
            float a1 = integrated_color.a;
            float a2 = color.a * (1 - a1);
            float alpha = max(a1 + a2, 0.001);
            
            // Doesn't work.. GLSL optimizer bug?
            //integrated_color = (integrated_color * a1 / alpha) + 
            //                   (color * a2 / alpha); 
            // This should be identical but does work correctly:
            integrated_color *= a1 / alpha;
            integrated_color += color * a2 / alpha;
            
            integrated_color.a = alpha;
            
            if( alpha > 0.99 ){
                // stop integrating if the fragment becomes opaque
                iter = nsteps;
            }
        
        """,
    after_loop="""
        gl_FragColor = integrated_color;
        """,
)
TRANSLUCENT_FRAG_SHADER = FRAG_SHADER.format(**TRANSLUCENT_SNIPPETS)


ADDITIVE_SNIPPETS = dict(
    before_loop="""
        vec4 integrated_color = vec4(0., 0., 0., 0.);
        """,
    in_loop="""
        color = applyColormap(val);
        
        integrated_color = 1.0 - (1.0 - integrated_color) * (1.0 - color);
        """,
    after_loop="""
        gl_FragColor = integrated_color;
        """,
)
ADDITIVE_FRAG_SHADER = FRAG_SHADER.format(**ADDITIVE_SNIPPETS)


ISO_SNIPPETS = dict(
    before_loop="""
        vec4 color3 = vec4(0.0);  // final color
        vec3 dstep = 1.5 / u_shape;  // step to sample derivative
        gl_FragColor = vec4(0.0);
    """,
    in_loop="""
        if (val > u_threshold-0.2) {
            // Take the last interval in smaller steps
            vec3 iloc = loc - step;
            for (int i=0; i<10; i++) {
                color = $sample(u_volumetex, iloc);
                if (color.r > u_threshold) {
                    color = calculateColor(color, iloc, dstep);
                    gl_FragColor = applyColormap(color.r);
                    iter = nsteps;
                    break;
                }
                iloc += step * 0.1;
            }
        }
        """,
    after_loop="""
        """,
)

ISO_FRAG_SHADER = FRAG_SHADER.format(**ISO_SNIPPETS)

frag_dict = {
    'mip': MIP_FRAG_SHADER,
    'iso': ISO_FRAG_SHADER,
    'translucent': TRANSLUCENT_FRAG_SHADER,
    'additive': ADDITIVE_FRAG_SHADER,
}


class VolumeVisual(Visual):
    """Displays a 3D Volume

    Parameters
    ----------
    vol : ndarray
        The volume to display. Must be ndim==3.
    clim : tuple of two floats | None
        The contrast limits. The values in the volume are mapped to
        black and white corresponding to these values. Default maps
        between min and max.
    method : {'mip', 'translucent', 'additive', 'iso'}
        The render method to use. See corresponding docs for details.
        Default 'mip'.
    threshold : float
        The threshold to use for the isosurface render method. By default
        the mean of the given volume is used.
    relative_step_size : float
        The relative step size to step through the volume. Default 0.8.
        Increase to e.g. 1.5 to increase performance, at the cost of
        quality.
    cmap : str
        Colormap to use.
    gamma : float
        Gamma to use during colormap lookup.  Final color will be cmap(val**gamma).
        by default: 1.
    interpolation : {'linear', 'nearest'}
        Selects method of image interpolation.
    texture_format: numpy.dtype | str | None
        How to store data on the GPU. OpenGL allows for many different storage
        formats and schemes for the low-level texture data stored in the GPU.
        Most common is unsigned integers or floating point numbers.
        Unsigned integers are the most widely supported while other formats
        may not be supported on older versions of OpenGL, WebGL
        (without enabling some extensions), or with older GPUs.
        Default value is ``None`` which means data will be scaled on the
        CPU and the result stored in the GPU as an unsigned integer. If a
        numpy dtype object, an internal texture format will be chosen to
        support that dtype and data will *not* be scaled on the CPU. Not all
        dtypes are supported. If a string, then
        it must be one of the OpenGL internalformat strings described in the
        table on this page: https://www.khronos.org/registry/OpenGL-Refpages/gl4/html/glTexImage2D.xhtml
        The name should have `GL_` removed and be lowercase (ex.
        `GL_R32F` becomes ``'r32f'``). Lastly, this can also be the string
        ``'auto'`` which will use the data type of the provided volume data
        to determine the internalformat of the texture.
        When this is specified (not ``None``) data is scaled on the
        GPU which allows for faster color limit changes. Additionally, when
        32-bit float data is provided it won't be copied before being
        transferred to the GPU. Note this visual is limited to "luminance"
        formatted data (single band). This is equivalent to `GL_RED` format
        in OpenGL 4.0.

    .. versionchanged: 0.7

        Deprecate 'emulate_texture' keyword argument.

    """

    _interpolation_names = ['linear', 'nearest']
    _texture_dtype_format = {
        np.float32: 'r32f',
        np.float64: 'r32f',
        np.uint8: 'r8',
        np.uint16: 'r16',
        # np.uint32: 'r32ui',  # not supported texture format in vispy
        np.int8: 'r8',
        np.int16: 'r16',
        # np.int32: 'r32i',  # not supported texture format in vispy
    }

    def __init__(self, vol, clim=None, method='mip', threshold=None,
                 relative_step_size=0.8, cmap='grays', gamma=1.0,
                 interpolation='linear', texture_format=None):
        # Storage of information of volume
        self._vol_shape = ()
        self._texture_limits = None
        self._gamma = gamma
        self._need_vertex_update = True
        # Set the colormap
        self._cmap = get_colormap(cmap)

        # Create gloo objects
        self._vertices = VertexBuffer()
        self._texcoord = VertexBuffer(
            np.array([
                [0, 0, 0],
                [1, 0, 0],
                [0, 1, 0],
                [1, 1, 0],
                [0, 0, 1],
                [1, 0, 1],
                [0, 1, 1],
                [1, 1, 1],
            ], dtype=np.float32))

        self._interpolation = interpolation
        self._texture = self._create_texture(texture_format, vol)
        # used to store current data for later CPU-side scaling if
        # texture_format is None
        self._last_data = None

        # Create program
        Visual.__init__(self, vcode=VERT_SHADER, fcode="")
        self.shared_program['u_volumetex'] = self._texture
        self.shared_program['a_position'] = self._vertices
        self.shared_program['a_texcoord'] = self._texcoord
        self.shared_program['gamma'] = self._gamma
        self._draw_mode = 'triangle_strip'
        self._index_buffer = IndexBuffer()

        # Only show back faces of cuboid. This is required because if we are 
        # inside the volume, then the front faces are outside of the clipping
        # box and will not be drawn.
        self.set_gl_state('translucent', cull_face=False)

        # Set data
        self.set_data(vol, clim)

        # Set params
        self.method = method
        self.relative_step_size = relative_step_size
        self.threshold = threshold if threshold is not None else vol.mean()
        self.freeze()

    def _create_texture(self, texture_format, data):
        if texture_format is not None:
            tex_cls = GPUScaledTextured3D
        else:
            tex_cls = CPUScaledTexture3D

        # if isinstance(texture_format, str) and texture_format == 'auto':
        #     texture_format = data.dtype.type
        # texture_format = self._get_gl_tex_format(texture_format)

        # tex_kwargs = {}
        # if isinstance(texture_format, str):
        #     tex_kwargs['internalformat'] = texture_format
        #     tex_kwargs['format'] = 'luminance'

        # Use 3D array shape of (10, 10, 10) as placeholder. When data is
        # set later this will be resized.
        # clamp_to_edge means any texture coordinates outside of 0-1 should be
        # clamped to 0 and 1.
        return tex_cls((10, 10, 10), interpolation=self._interpolation,
                       internalformat=texture_format,
                       format='luminance',
                       wrapping='clamp_to_edge')

    def _cpu_scale_data(self, vol):
        if self._clim[1] == self._clim[0]:
            if self._clim[0] != 0.:
                vol *= 1.0 / self._clim[0]
        elif self._clim[0] > self._clim[1]:
            vol *= -1
            vol += self._clim[1]
            vol /= self._clim[1] - self._clim[0]
        else:
            vol -= self._clim[0]
            vol /= self._clim[1] - self._clim[0]

    def set_data(self, vol, clim=None, copy=True):
        """Set the volume data.

        Parameters
        ----------
        vol : ndarray
            The 3D volume.
        clim : tuple | None
            Colormap limits to use. None will use the min and max values.
        copy : bool | True
            Whether to copy the input volume prior to applying clim
            normalization on the CPU. Has no effect if visual was created
            with 'texture_format' not equal to None as data is not modified
            on the CPU and data must already be copied to the GPU.
            Data must be 32-bit floating point data to completely avoid any
            data copying when scaling on the CPU.

        """
        # Check volume
        if not isinstance(vol, np.ndarray):
            raise ValueError('Volume visual needs a numpy array.')
        if not ((vol.ndim == 3) or (vol.ndim == 4 and vol.shape[-1] > 1)):
            raise ValueError('Volume visual needs a 3D image.')

        if clim is not None:
            self._texture.set_clim(clim)
        if self._texture.clim is None:
            self._texture.set_clim((vol.min(), vol.max()))

        # store clims used to normalize _texture data for use in clim_normalized
        # self._texture_limits = self._clim

        # Apply to texture
        if should_cast_to_f32(vol.dtype):
            vol = vol.astype(np.float32)
        self._texture.check_data_format(vol)
        self._last_data = vol
        self._texture.scale_and_set_data(vol, copy=copy)  # will be efficient if vol is same shape
        self.shared_program['clim'] = self._texture.clim_normalized
        self.shared_program['u_shape'] = (vol.shape[2], vol.shape[1],
                                          vol.shape[0])

        shape = vol.shape[:3]
        if self._vol_shape != shape:
            self._vol_shape = shape
            self._need_vertex_update = True
        self._vol_shape = shape

    def _normalize_val(self, val, input_data_dtype):
        is_normalized = self._texture.internalformat is not None and self._texture.internalformat[-1] not in ('f', 'i')
        if not is_normalized:
            return val
        dtype_info = np.iinfo(input_data_dtype)
        dmin = dtype_info.min
        dmax = dtype_info.max
        val = (val - dmin) / (dmax - dmin)
        return val

    def rescale_data(self):
        """Force rescaling of data to the current contrast limits and texture upload.

        If this VolumeVisual was created with ``texture_format`` equal to
        ``None`` then our texture is limited to 8-bits of resolution and
        contrast adjustment is done during rendering by scaling the values
        retrieved from the texture on the GPU (provided that the new contrast
        limits settings are within the range of the clims used when the
        last texture was uploaded), posterization may become visible if the contrast limits
        become *too* small of a fraction of the clims used to normalize the texture.
        This function is a convenience to "force" rescaling of the Texture data to the
        current contrast limits range.
        """
        self.set_data(self._last_data, clim=self._clim)
        self.update()

    @property
    def clim(self):
        """The contrast limits that were applied to the volume data.

        Volume display is mapped from black to white with these values.
        Settable via set_data() as well as @clim.setter.
        """
        return self._texture.clim

    @clim.setter
    def clim(self, value):
        """Set contrast limits used when rendering the image.

        ``value`` should be a 2-tuple of floats (min_clim, max_clim), where each value is
        within the range set by self.clim. If the new value is outside of the (min, max)
        range of the clims previously used to normalize the texture data, then data will
        be renormalized using set_data.
        """
        # TODO: Handle rescaling data if outside of previous range and
        if self._texture.set_clim(value):
            self._need_texture_upload = True
        self.shared_program['clim'] = self._texture.clim_normalized
        self.update()

    @property
    def gamma(self):
        """The gamma used when rendering the image."""
        return self._gamma

    @gamma.setter
    def gamma(self, value):
        """Set gamma used when rendering the image."""
        if value <= 0:
            raise ValueError("gamma must be > 0")
        self._gamma = float(value)
        self.shared_program['gamma'] = self._gamma
        self.update()

    @property
    def cmap(self):
        return self._cmap

    @cmap.setter
    def cmap(self, cmap):
        self._cmap = get_colormap(cmap)
        self.shared_program.frag['cmap'] = Function(self._cmap.glsl_map)
        self.update()

    @property
    def interpolation(self):
        """The interpolation method to use

        Current options are:

            * linear: this method is appropriate for most volumes as it creates
              nice looking visuals.
            * nearest: this method is appropriate for volumes with discrete
              data where additional interpolation does not make sense.
        """
        return self._interpolation

    @interpolation.setter
    def interpolation(self, interp):
        if interp not in self._interpolation_names:
            raise ValueError(
                "interpolation must be one of %s"
                % ', '.join(self._interpolation_names)
            )
        if self._interpolation != interp:
            self._interpolation = interp
            self._texture.interpolation = self._interpolation
            self.update()

    @property
    def method(self):
        """The render method to use

        Current options are:

            * translucent: voxel colors are blended along the view ray until
              the result is opaque.
            * mip: maxiumum intensity projection. Cast a ray and display the
              maximum value that was encountered.
            * additive: voxel colors are added along the view ray until
              the result is saturated.
            * iso: isosurface. Cast a ray until a certain threshold is
              encountered. At that location, lighning calculations are
              performed to give the visual appearance of a surface.  
        """
        return self._method

    @method.setter
    def method(self, method):
        # Check and save
        known_methods = list(frag_dict.keys())
        if method not in known_methods:
            raise ValueError('Volume render method should be in %r, not %r' %
                             (known_methods, method))
        self._method = method
        # Get rid of specific variables - they may become invalid
        if 'u_threshold' in self.shared_program:
            self.shared_program['u_threshold'] = None

        self.shared_program.frag = frag_dict[method]
        self.shared_program.frag['sampler_type'] = self._texture.glsl_sampler_type
        self.shared_program.frag['sample'] = self._texture.glsl_sample
        self.shared_program.frag['cmap'] = Function(self._cmap.glsl_map)
        self.shared_program['texture2D_LUT'] = self.cmap.texture_lut() \
            if (hasattr(self.cmap, 'texture_lut')) else None
        self.update()

    @property
    def threshold(self):
        """The threshold value to apply for the isosurface render method."""
        return self._threshold

    @threshold.setter
    def threshold(self, value):
        self._threshold = float(value)
        if 'u_threshold' in self.shared_program:
            self.shared_program['u_threshold'] = self._threshold
        self.update()

    @property
    def relative_step_size(self):
        """The relative step size used during raycasting.

        Larger values yield higher performance at reduced quality. If
        set > 2.0 the ray skips entire voxels. Recommended values are
        between 0.5 and 1.5. The amount of quality degredation depends
        on the render method.
        """
        return self._relative_step_size

    @relative_step_size.setter
    def relative_step_size(self, value):
        value = float(value)
        if value < 0.1:
            raise ValueError('relative_step_size cannot be smaller than 0.1')
        self._relative_step_size = value
        self.shared_program['u_relative_step_size'] = value

    def _create_vertex_data(self):
        """Create and set positions and texture coords from the given shape

        We have six faces with 1 quad (2 triangles) each, resulting in
        6*2*3 = 36 vertices in total.
        """
        shape = self._vol_shape

        # Get corner coordinates. The -0.5 offset is to center
        # pixels/voxels. This works correctly for anisotropic data.
        x0, x1 = -0.5, shape[2] - 0.5
        y0, y1 = -0.5, shape[1] - 0.5
        z0, z1 = -0.5, shape[0] - 0.5

        pos = np.array([
            [x0, y0, z0],
            [x1, y0, z0],
            [x0, y1, z0],
            [x1, y1, z0],
            [x0, y0, z1],
            [x1, y0, z1],
            [x0, y1, z1],
            [x1, y1, z1],
        ], dtype=np.float32)

        """
          6-------7
         /|      /|
        4-------5 |
        | |     | |
        | 2-----|-3
        |/      |/
        0-------1
        """

        # Order is chosen such that normals face outward; front faces will be
        # culled.
        indices = np.array([2, 6, 0, 4, 5, 6, 7, 2, 3, 0, 1, 5, 3, 7],
                           dtype=np.uint32)

        # Apply
        self._vertices.set_data(pos)
        self._index_buffer.set_data(indices)

    def _compute_bounds(self, axis, view):
        return 0, self._vol_shape[axis]

    def _prepare_transforms(self, view):
        trs = view.transforms
        view.view_program.vert['transform'] = trs.get_transform()

        view_tr_f = trs.get_transform('visual', 'document')
        view_tr_i = view_tr_f.inverse
        view.view_program.vert['viewtransformf'] = view_tr_f
        view.view_program.vert['viewtransformi'] = view_tr_i

    def _prepare_draw(self, view):
        if self._need_vertex_update:
            self._create_vertex_data()
