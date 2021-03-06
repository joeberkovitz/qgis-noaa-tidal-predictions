#!/bin/sh
export QGIS_BUNDLE=/Applications/QGIS-LTR.app/Contents
export QGIS_PREFIX_PATH=${QGIS_BUNDLE}/MacOS
export PATH=${QGIS_PREFIX_PATH}/bin:$PATH
export DYLD_LIBRARY_PATH=`dirname $0`/lib
export GDAL_DRIVER_PATH=${QGIS_PREFIX_PATH}/lib/gdalplugins
export GDAL_DATA=${QGIS_BUNDLE}/Resources/gdal
export PROJ_LIB=${QGIS_BUNDLE}/Resources/proj
export PYQGIS_STARTUP=pyqgis-startup.py
export PYTHONHOME=${QGIS_PREFIX_PATH}
export QT_AUTO_SCREEN_SCALE_FACTOR=1
# Note: this relies on nose having been installed in ${QGIS_PREFIX_PATH}/bin by the pip binary bundled with QGIS.
nosetests $*
