# -*- coding: utf-8 -*-
"""
/***************************************************************************
 CompassRoutes
                                 A QGIS plugin
 This plugin creates layers that automatically label route legs with distance and magnetic bearing
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                             -------------------
        begin                : 2021-03-05
        copyright            : (C) 2021 by Joe Berkovitz
        email                : joseph.berkovitz@gmail.com
        git sha              : $Format:%H$
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 This script initializes the plugin, making it known to QGIS.
"""


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load CompassRoutes class from file CompassRoutes.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .noaa_tidal_predictions import NoaaTidalPredictions
    return NoaaTidalPredictions(iface)
