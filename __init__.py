# SPDX-License-Identifier: GPL-3.0-or-later

from .bc2_frostbite_importer import bl_info


def register():
    from .bc2_frostbite_importer import register as register_addon

    register_addon()


def unregister():
    from .bc2_frostbite_importer import unregister as unregister_addon

    unregister_addon()
