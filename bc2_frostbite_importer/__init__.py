# SPDX-License-Identifier: GPL-3.0-or-later

bl_info = {
    "name": "Bad Company 2 Frostbite Importer",
    "author": "BC2 Blender Contributors",
    "version": (0, 7, 1),
    "blender": (5, 2, 0),
    "location": "File > Import > Bad Company 2; 3D View > Sidebar > Frostbite",
    "description": (
        "Import Frostbite 1 assets and PC maps from Bad Company 2 disc images"
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
