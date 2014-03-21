# -*- coding: utf-8 -*-
# Copyright (c) 2014, Vispy Development Team.
# Distributed under the (new) BSD License. See LICENSE.txt for more info.



from __future__ import division

#from .. import gloo
from .visual import Visual
#from ..util.meshdata import MeshData

class MeshVisual(Visual):
    """
    Displays a 3D triangle mesh.
    """
    def __init__(self, **kwds):
        super(MeshVisual, self).__init__()
        
        glopts = kwds.pop('gl_options', 'translucent')
        self.set_gl_options(glopts)
        
        if kwds:
            self.set_data(**kwds)

    def set_data(self, **kwds):
        kwds['index'] = kwds.pop('faces', kwds.get('index', None))
        super(MeshVisual, self).set_data(**kwds)
    #def set_data(self, pos=None, faces=None, z=0.0, color=(1,1,1,1)):
        #"""
        #*pos* must be array of shape (..., 2) or (..., 3).
        #*z* is only used in the former case.
        #"""
        ## select input component based on pos.shape
        #if pos is not None:
            #if pos.shape[-1] == 2:
                #if not isinstance(self.pos_component, XYPosComponent):
                    #self.pos_component = XYPosComponent()
                #self.pos_component.set_data(xy=pos, z=z, index=faces)
            #elif pos.shape[-1] == 3:
                #if not isinstance(self.pos_component, XYZPosComponent):
                    #self.pos_component = XYZPosComponent()
                #self.pos_component.set_data(pos=pos, index=faces)
            
        #if isinstance(color, tuple):
            #self.fragment_components = [UniformColorComponent(color)]
        #elif isinstance(color, np.ndarray):
            #self.fragment_components = [VertexColorComponent(color)]
            

