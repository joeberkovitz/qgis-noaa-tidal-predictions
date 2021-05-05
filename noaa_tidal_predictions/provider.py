import os
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
from .add_station_layers import AddCurrentStationsLayerAlgorithm

class NoaaTidalPredictionsProvider(QgsProcessingProvider):
    def unload(self):
        QgsProcessingProvider.unload(self)

    def loadAlgorithms(self):
        self.addAlgorithm(AddCurrentStationsLayerAlgorithm())

    def id(self):
        return 'NoaaTidalPredictions'

    def name(self):
        return 'NOAA Tidal Predictions'

    def longName(self):
        return self.name()