# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Bad Company 2 Frostbite Importer",
    "author": "BC2 Blender Contributors",
    "version": (0, 8, 0),
    "blender": (5, 2, 0),
    "location": "File > Import > Bad Company 2; 3D View > Sidebar > Frostbite",
    "description": (
        "Import Frostbite 1 assets and maps from BC2 and BF1943 sources"
    ),
    "category": "Import-Export",
    "doc_url": "https://github.com/AnirohDev/FBOneTools",
}


def register():
    from . import blender_addon

    blender_addon.register()


def unregister():
    from . import blender_addon

    blender_addon.unregister()
