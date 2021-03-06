import math
import traceback
import numpy as np
from scipy.interpolate import interp1d
import xml.etree.ElementTree as ET

from qgis.core import (
    QgsApplication, QgsPointXY, QgsPoint, QgsRectangle, QgsFeature, QgsGeometry, QgsField, QgsFields,
    QgsProject, QgsUnitTypes, QgsWkbTypes, QgsCoordinateTransform,
    QgsFeatureRequest, QgsNetworkContentFetcherTask, QgsBlockingNetworkRequest,
    QgsExpressionContextScope, QgsExpressionContext, QgsFeatureSink,
    NULL
)

from qgis.PyQt.QtCore import (
    pyqtSlot, pyqtSignal,
    QObject, QDate, QDateTime, QTime, QTimeZone, QUrl, QUrlQuery,
)
from qgis.PyQt.QtNetwork import (
    QNetworkReply, QNetworkRequest
)

from .utils import *

class PredictionManager(QObject):
    """Manager object overseeing the loading and caching of predictions
        organized by station-dates.
    """

    # The time increment used for prediction requests and temporal display control.
    STEP_MINUTES = 30

    progressChanged = pyqtSignal(int)

    # Each manager has a stations and a prediction layer.
    def __init__(self, stationsLayer, predictionsLayer):
        super(QObject,self).__init__()

        # Initialize a map of cached PredictionDataPromises
        self.dataCache = {}

        # also a map of cached PredictionEventPromise
        self.eventCache = {}

        self.stationsLayer = stationsLayer
        self.predictionsLayer = predictionsLayer
        self.activeCount = 0
        self.activeHighWater = 0
        self.blocking = False
        self.savePredictions = True

    def getPromise(self, stationFeature, date, promiseClass, cache):
        key = self.promiseKey(stationFeature, date)
        promise = cache.get(key)
        if promise is None or promise.state == PredictionPromise.RejectedState:
            # we have no cached data promise. so make one. This implicitly requests the data
            # if it is not already in the predictions layer.
            promise = promiseClass(self, stationFeature, date)
            cache[key] = promise

            self.activeCount += 1
            self.activeHighWater = max(self.activeHighWater, self.activeCount)
            self.progressChanged.emit(self.progressValue())

            promise.resolved(self.promiseDone)
            promise.rejected(self.promiseDone)
        return promise

    # Obtain a PredictionDataPromise for 24-hour period starting with the given local-station-time date
    def getDataPromise(self, stationFeature, date):
        promiseClass = CurrentDataPromise if stationFeature['flags'] & StationFlags.Current else TideDataPromise
        return self.getPromise(stationFeature, date, promiseClass, self.dataCache)

    # Obtain a PredictionEventPromise for 24-hour period starting with the given local-station-time date
    def getEventPromise(self, stationFeature, date):
        promiseClass = CurrentEventPromise if stationFeature['flags'] & StationFlags.Current else TideEventPromise
        return self.getPromise(stationFeature, date, promiseClass, self.eventCache)

    # track the conclusion of a promise, successful or otherwise
    def promiseDone(self):
        self.activeCount -= 1
        if self.activeCount == 0:
            self.activeHighWater = 0
        self.progressChanged.emit(self.progressValue())

    # determine the progress value. 100 can mean there is no activity in progress.
    def progressValue(self):
        if self.activeCount == 0:
            return 100
        else:
            return int(100 * (self.activeHighWater - self.activeCount) / self.activeHighWater)

    # Return a list of surface station features included in the given rectangle.
    def getExtentStations(self, rect):
        features = self.stationsLayer.getFeatures(rect)
        return list(filter(lambda f: f['flags'] & StationFlags.Surface, features))

    # Return a station feature by its unique identifier
    def getStation(self, stationId):
        req = QgsFeatureRequest()
        req.setFilterExpression("station = '{}'".format(stationId))
        for f in self.stationsLayer.getFeatures(req):
            return f
        return None

    # Compute a key
    @staticmethod
    def promiseKey(stationFeature, date):
        return stationFeature['station'] + '.' + date.toString('yyyyMMdd')


class PredictionPromise(QObject):
    """ Abstract promise-like object that emits a resolved signal when done with something.
        A list of PredictionPromise dependencies is maintained.

    """
    _resolved = pyqtSignal()
    _rejected = pyqtSignal()

    InitialState = 0
    StartedState = 1
    ResolvedState = 2
    RejectedState = 3

    def __init__(self):
        super(PredictionPromise,self).__init__()
        self.state = PredictionPromise.InitialState
        self.dependencies = []

    def start(self):
        if self.state >= PredictionPromise.StartedState:
            return
        self.state = PredictionPromise.StartedState
        self.doStart()
        for p in self.dependencies:
            p.resolved(self.checkDependencies)
            p.rejected(self.checkDependencies)
            p.start()

    def doStart(self):
        # subclasses should override this
        return

    def resolve(self):
        if self.state >= PredictionPromise.ResolvedState:
            return
        self.state = PredictionPromise.ResolvedState
        self._resolved.emit()
        try:
            self._resolved.disconnect()
        except TypeError:
            pass

    def reject(self):
        if self.state >= PredictionPromise.ResolvedState:
            return
        self.state = PredictionPromise.RejectedState
        self._rejected.emit()
        try:
            self._rejected.disconnect()
        except TypeError:
            pass

    def resolved(self, slot):
        if self.state == PredictionPromise.ResolvedState:
            slot()
        elif self.state != PredictionPromise.RejectedState:
            self._resolved.connect(slot)

    def rejected(self, slot):
        if self.state == PredictionPromise.RejectedState:
            slot()
        elif self.state != PredictionPromise.ResolvedState:
            self._rejected.connect(slot)

    # add a promise on which we are dependent. when all dependents are resolved, this one will too.
    def addDependency(self, p):
        self.dependencies.append(p)

    def checkDependencies(self):
        allResolved = True

        for p in self.dependencies:
            if p.state == PredictionPromise.RejectedState:
                self.reject()
                return
            elif p.state != PredictionPromise.ResolvedState:
                allResolved = False

        if allResolved:
            try:
                self.doProcessing()
                self.resolve()
            except Exception as e:
                print('Exception in processing: ', e)
                traceback.print_exc()
                self.reject()    


    def doProcessing(self):
        # subclasses should override this to process dependencies or other intermediate results
        return

class PredictionEventPromise(PredictionPromise):
    """ Promise to obtain event-style predictions for a given station and local date.
        These are cached in the promise and are not saved in a vector layer.
    """

    # initialize this promise for a given manager, station and date.
    def __init__(self, manager, stationFeature, date):
        super(PredictionEventPromise, self).__init__()
        self.manager = manager
        self.stationFeature = stationFeature
        self.predictions = None

        # convert local station timezone QDate to a full UTC QDateTime.
        self.localDate = date
        self.datetime = QDateTime(date, QTime(0,0), stationTimeZone(stationFeature)).toUTC()

    def doProcessing(self):
        self.predictions = self.eventRequest.predictions

class CurrentEventPromise(PredictionEventPromise):
    """ Get all the data needed to resolve this promise
    """
    def doStart(self):
        self.eventRequest = CurrentPredictionRequest(
            self.manager,
            self.stationFeature,
            self.datetime,
            self.datetime.addDays(1),
            CurrentPredictionRequest.EventType)
        self.addDependency(self.eventRequest)

class TideEventPromise(PredictionEventPromise):
    """ Get all the data needed to resolve this promise
    """
    def doStart(self):
        self.eventRequest = TidePredictionRequest(
            self.manager,
            self.stationFeature,
            self.datetime,
            self.datetime.addDays(1),
            TidePredictionRequest.EventType)
        self.addDependency(self.eventRequest)

class PredictionDataPromise(PredictionPromise):
    """ Promise to obtain a full set of predictions (events and timeline) for a given station and local date.
    """

    # initialize this promise for a given manager, station and date.
    def __init__(self, manager, stationFeature, date):
        super(PredictionDataPromise, self).__init__()
        self.manager = manager
        self.stationFeature = stationFeature
        self.predictions = None

        # convert local station timezone QDate to a full UTC QDateTime.
        self.localDate = date
        self.datetime = QDateTime(date, QTime(0,0), stationTimeZone(stationFeature)).toUTC()

    """ Get all the data needed to resolve this promise
    """
    def doStart(self):
        # first see if we can pull data from the predictions layer for this station
        self.startTime = self.datetime
        self.endTime = self.datetime.addDays(1)

        # we have to make it a spatial query because there is no easy way to index predictions by station ID
        featureRequest = QgsFeatureRequest()
        stationPt = QgsPointXY(self.stationFeature.geometry().vertexAt(0))
        searchRect = QgsRectangle(stationPt, stationPt)
        searchRect.grow(0.01/60)   # in the neighborhood of .01 nm as 1/60 = 1 arc minute in this proj.
        featureRequest.setFilterRect(searchRect)

        # Refine this with additional time based query conditions
        ctx = featureRequest.expressionContext()
        scope = QgsExpressionContextScope()
        scope.setVariable('startTime', self.startTime)
        scope.setVariable('endTime', self.endTime)
        scope.setVariable('station', self.stationFeature['station'])
        ctx.appendScope(scope)
        featureRequest.setFilterExpression("station = @station and time >= @startTime and time < @endTime")
        featureRequest.addOrderBy('time')

        savedFeatureIterator = self.manager.predictionsLayer.getFeatures(featureRequest)
        savedFeatures = list(savedFeatureIterator)
        if len(savedFeatures) > 0:
            # We have some features, so go ahead and stash them in the layer and resolve this promise
            print ('{}: retrieved {} features from layer'.format(self.stationFeature['station'], len(savedFeatures)))
            self.predictions = savedFeatures
            self.resolve()
        else:
            self.requestData()

    def doProcessing(self):
        self.processRequest()

        # add everything into the predictions layer if enabled
        if self.manager.savePredictions:
            self.manager.predictionsLayer.startEditing()
            self.manager.predictionsLayer.addFeatures(self.predictions, QgsFeatureSink.FastInsert)
            self.manager.predictionsLayer.commitChanges()
            self.manager.predictionsLayer.triggerRepaint()

    def requestData(self):
        return # overridden by subclasses

    def processRequest(self):
        return # overridden by subclasses


class CurrentDataPromise(PredictionDataPromise):
    def requestData(self):
        # The layer didn't have what we wanted, so we must request the data we need.
        # At this point, the situation falls into several possible cases.

        # Case 1: A Harmonic station with known flood/ebb directions. Here
        # we need two requests which can simply be combined and sorted:
        #   1a: EventType, i.e. slack, flood and ebb
        #   1b: SpeedDirType, as velocity can be calculated by projecting along flood/ebb
        #
        # Case 2: A Harmonic station with unknown flood and/or ebb.
        # We actually need to combine 3 requests:
        #   2a: EventType
        #   2b: SpeedDirType, which only provides vector magnitude/angle
        #   2c: VelocityMajorType, which only provides current velocity (but for same times as 2b)

        # Here we set up requests for cases 1 and 2
        if self.stationFeature['flags'] & StationFlags.Reference:
            self.speedDirRequest = CurrentPredictionRequest(
                self.manager,
                self.stationFeature,
                self.startTime,
                self.endTime,
                CurrentPredictionRequest.SpeedDirectionType)
            self.addDependency(self.speedDirRequest)

            self.eventRequest = CurrentPredictionRequest(
                self.manager,
                self.stationFeature,
                self.startTime,
                self.endTime,
                CurrentPredictionRequest.EventType)
            self.addDependency(self.eventRequest)

            floodDir = self.stationFeature['meanFloodDir']
            ebbDir = self.stationFeature['meanEbbDir']
            if floodDir == NULL or ebbDir == NULL:
                self.velocityRequest = CurrentPredictionRequest(
                    self.manager,
                    self.stationFeature,
                    self.startTime,
                    self.endTime,
                    CurrentPredictionRequest.VelocityMajorType)
                self.addDependency(self.velocityRequest)
            else:
                self.velocityRequest = None

        # Case 3: A Subordinate station which only knows its events. Here we need the following:
        #   3a: PredictionEventPromises for this station in a 3-day window surrounding the date of interest
        #   3b: PredictionDataPromises for the reference station in the same 3-day window.
        else:
            self.eventPromises = []
            self.refPromises = []
            refStation = self.manager.getStation(self.stationFeature['refStation'])
            if refStation is None:
                print("Could not find ref station {} for {}".format(self.stationFeature['refStation'], self.stationFeature['station']))
            else:
                for dayOffset in [-1, 0, 1]:
                    windowDate = self.localDate.addDays(dayOffset)
                    dataPromise = self.manager.getDataPromise(refStation, windowDate)
                    self.refPromises.append(dataPromise)
                    self.addDependency(dataPromise)
                    eventPromise = self.manager.getEventPromise(self.stationFeature, windowDate)
                    self.eventPromises.append(eventPromise)
                    self.addDependency(eventPromise)

    def processRequest(self):
        if self.stationFeature['flags'] & StationFlags.Reference:
            # We will always have a speed/direction request
            self.predictions = self.speedDirRequest.predictions

            # If we also had a velocity request with the same number of results
            # try to combine it with this one.
            if (self.velocityRequest is not None
                    and (len(self.velocityRequest.predictions) == len(self.predictions))):
                for i, p in enumerate(self.predictions):
                    p['value'] = self.velocityRequest.predictions[i]['value']

            # Now fold in the events and sort everything by time
            self.predictions.extend(self.eventRequest.predictions)
            self.predictions.sort(key=(lambda p: p['time']))
        else:
            # subordinate-station case: we need to cook up interpolations based on 
            # the 3-day windows of a) subordinate events and b) reference currents.

            print('Interpolating ref station {} for {}'.format(
                self.stationFeature['refStation'],
                self.stationFeature['station']
                ))
            interpolator = PredictionInterpolator(self.stationFeature, self.datetime, self.eventPromises, self.refPromises)

            subTimes = np.linspace(0, 24 * 60 * 60, 24 * 60 // PredictionManager.STEP_MINUTES, False)
            refValues = interpolator.valuesFor(subTimes)
            ebbDir = self.stationFeature['meanEbbDir'] 
            floodDir = self.stationFeature['meanFloodDir'] 

            fields = self.manager.predictionsLayer.fields()
            self.predictions = []
            for i in range(0, len(subTimes)):
                f = QgsFeature(fields)
                f.setGeometry(QgsGeometry(self.stationFeature.geometry()))
                f['station'] = self.stationFeature['station']
                f['depth'] = self.stationFeature['depth']
                f['time'] = self.datetime.addSecs(int(subTimes[i]))
                f['value'] = float(refValues[i])
                f['dir'] = ebbDir if refValues[i] < 0 else floodDir
                f['magnitude'] = abs(f['value'])
                f['flags'] = PredictionFlags.Time | PredictionFlags.Current | PredictionFlags.Surface
                self.predictions.append(f)

            # Now mix in the event data from the central day in the 3-day window and sort everything
            self.predictions.extend(self.eventPromises[1].predictions)
            self.predictions.sort(key=(lambda p: p['time']))

class TideDataPromise(PredictionDataPromise):
    def requestData(self):
        # The layer didn't have what we wanted, so we must request the data we need.
        # At this point, the situation falls into several possible cases.

        # Case 1: A Harmonic station with known flood/ebb directions. Here
        # we need two requests which can simply be combined and sorted:
        #   1a: EventType, i.e. H and L
        #   1b: WaterLevelType
        if self.stationFeature['flags'] & StationFlags.Reference:
            self.waterLevelRequest = TidePredictionRequest(
                self.manager,
                self.stationFeature,
                self.startTime,
                self.endTime,
                TidePredictionRequest.WaterLevelType)
            self.addDependency(self.waterLevelRequest)

            self.eventRequest = TidePredictionRequest(
                self.manager,
                self.stationFeature,
                self.startTime,
                self.endTime,
                TidePredictionRequest.EventType)
            self.addDependency(self.eventRequest)


        # Case 2: A Subordinate station which only knows its events. Here we need the following:
        #   3a: PredictionEventPromises for this station in a 3-day window surrounding the date of interest
        #   3b: PredictionDataPromises for the reference station in the same 3-day window.
        else:
            self.eventPromises = []
            self.refPromises = []
            refStation = self.manager.getStation(self.stationFeature['refStation'])
            if refStation is None:
                print("Could not find ref station {} for {}".format(self.stationFeature['refStation'], self.stationFeature['station']))
            else:
                for dayOffset in [-1, 0, 1]:
                    windowDate = self.localDate.addDays(dayOffset)
                    dataPromise = self.manager.getDataPromise(refStation, windowDate)
                    self.refPromises.append(dataPromise)
                    self.addDependency(dataPromise)
                    eventPromise = self.manager.getEventPromise(self.stationFeature, windowDate)
                    self.eventPromises.append(eventPromise)
                    self.addDependency(eventPromise)

    def processRequest(self):
        if self.stationFeature['flags'] & StationFlags.Reference:
            self.predictions = self.waterLevelRequest.predictions
            self.predictions.extend(self.eventRequest.predictions)
        else:
            # subordinate-station case: we need to cook up interpolations based on 
            # the 3-day windows of a) subordinate events and b) reference currents.

            print('Interpolating ref station {} for {}'.format(
                self.stationFeature['refStation'],
                self.stationFeature['station']
                ))
            interpolator = PredictionInterpolator(self.stationFeature, self.datetime, self.eventPromises, self.refPromises)
            subTimes = np.linspace(0, 24 * 60 * 60, 24 * 60 // PredictionManager.STEP_MINUTES, False)
            refValues = interpolator.valuesFor(subTimes)

            fields = self.manager.predictionsLayer.fields()
            self.predictions = []
            for i in range(0, len(subTimes)):
                f = QgsFeature(fields)
                f.setGeometry(QgsGeometry(self.stationFeature.geometry()))
                f['station'] = self.stationFeature['station']
                f['time'] = self.datetime.addSecs(int(subTimes[i]))
                f['value'] = float(refValues[i])
                f['flags'] = PredictionFlags.Time | PredictionFlags.Surface
                self.predictions.append(f)

            # Now mix in the event data from the central day in the 3-day window and sort everything
            self.predictions.extend(self.eventPromises[1].predictions)

        # sort everything by time
        self.predictions.sort(key=(lambda p: p['time']))

        # determine 24-hour LLW and HHW from the available predictions
        values = [p['value'] for p in self.predictions]
        hhw = max(values)
        llw = min(values)
        lastMagnitude = None
        for p in self.predictions:
            magnitude = (p['value'] - llw) / (hhw - llw)
            if lastMagnitude is not None:
                if magnitude > lastMagnitude:
                    p['flags'] |= PredictionFlags.Rising
                else:
                    p['flags'] |= PredictionFlags.Falling

            p['magnitude'] = magnitude
            lastMagnitude = magnitude

        self.predictions[0]['flags'] |= self.predictions[1]['flags'] & PredictionFlags.Trend

class PredictionInterpolator:
    def __init__(self, stationFeature, datetime, subPromises, refPromises):
        self.stationFeature = stationFeature
        self.datetime = datetime

        self.refData = []
        for refPromise in refPromises:
            for p in refPromise.predictions:
                self.refData.append((self.secsTo(p['time']), p['flags'] & PredictionFlags.Type, p))

        self.subData = []
        for subPromise in subPromises:
            for p in subPromise.predictions:
                self.subData.append((self.secsTo(p['time']), p['flags'] & PredictionFlags.Type, p))

        # segregate reference events into a map of arrays keyed on event type
        self.refMap = {}
        for (ptime, ptype, p) in self.refData:
            eventList = self.refMap.get(ptype)
            if eventList is None:
                eventList = []
                self.refMap[ptype] = eventList
            eventList.append((ptime, ptype, p))

    def valuesFor(self, subTimes):
        factorInterp = self.factorInterpolation()
        valueInterp = self.valueInterpolation()
        timeInterp = self.timeInterpolation()

        refTimes = timeInterp(subTimes)
        refFactors = factorInterp(subTimes)
        if self.stationFeature['flags'] & StationFlags.FixedAdj:
            return np.add(valueInterp(refTimes), refFactors)
        else:
            return np.multiply(valueInterp(refTimes), refFactors)

    def timeInterpolation(self):
        """ return a function that takes an array of time offsets in seconds on this (subordinate) station
            and returns an array of time offsets in seconds on the reference station, relative
            to the start date of this prediction set.
        """

        # search for events, ignoring any initial slack event
        phase = 0    # unknown whether we are in ebb or flood initially
        subTimes = []
        refTimes = []
        for (time, ptype, p) in self.refData:
            if ptype == PredictionFlags.Zero:
                if phase > 0:
                    # slack before ebb (after flood)
                    subTimes.append(time + 60*self.stationFeature['fallingZeroTimeAdj'])
                    refTimes.append(time)
                elif phase < 0:
                    # slack before flood (after ebb)
                    subTimes.append(time + 60*self.stationFeature['risingZeroTimeAdj'])
                    refTimes.append(time)
            elif ptype == PredictionFlags.Max:
                phase = 1
                subTimes.append(time + 60*self.stationFeature['maxTimeAdj'])
                refTimes.append(time)
            elif ptype == PredictionFlags.Min:
                phase = -1
                subTimes.append(time + 60*self.stationFeature['minTimeAdj'])
                refTimes.append(time)

        return interp1d(subTimes, refTimes, 'linear')

    def factorInterpolation(self):
        """ return a function that takes an array of time offsets in seconds on a subordinate station
            and returns an array of reference station correction factors, relative
            to the start date of this prediction set.
        """

        # search for events, ignoring any initial slack event
        subTimes = []
        refFactors = []
        for (time, ptype, p) in self.refData:
            if ptype == PredictionFlags.Max:
                subTimes.append(time + 60*self.stationFeature['maxTimeAdj'])
                refFactors.append(self.stationFeature['maxValueAdj'])
            elif ptype == PredictionFlags.Min:
                subTimes.append(time + 60*self.stationFeature['minTimeAdj'])
                refFactors.append(self.stationFeature['minValueAdj'])

        return interp1d(subTimes, refFactors, 'quadratic')

    def valueInterpolation(self):
        """ return a function that takes an array of offsets from the start time in seconds, and returns an
            array of interpolated velocities from this object's predictions.
        """
        times = []
        values = []
        for (time, ptype, p) in self.refMap[PredictionFlags.Time]:
            try:
                values.append(p['value'])
                times.append(time)
            except ValueError:
                pass  # some values will be outside the range of factorInterpolation()

        return interp1d(times, values, 'cubic')

    def secsTo(self, dt):
        dt.setTimeSpec(Qt.TimeSpec.UTC)
        return self.datetime.secsTo(dt)


# low-level request for data regarding a station feature around a date range
class PredictionRequest(PredictionPromise):

    # construct the request and save its state, but don't send it
    def __init__(self, manager, stationFeature, startTime, endTime):
        super(PredictionRequest, self).__init__()
        self.manager = manager
        self.stationFeature = stationFeature
        self.startTime = startTime
        self.endTime = endTime

    def doStart(self):
        if self.manager.blocking:
            self.content = None
            req = QgsBlockingNetworkRequest()
            errCode = req.get(QNetworkRequest(QUrl(self.url())))
            if errCode == QgsBlockingNetworkRequest.NoError:
                self.content = req.reply().content()
            self.processFinish()
        else:
            self.fetcher = QgsNetworkContentFetcherTask(QUrl(self.url()))
            self.fetcher.fetched.connect(self.processFetch)
            self.fetcher.taskCompleted.connect(self.processFinish)
            self.fetcher.taskTerminated.connect(self.processFinish)
            self.content = None
            QgsApplication.taskManager().addTask(self.fetcher)

    def url(self):
        query = QUrlQuery()
        query.addQueryItem('application', CoopsApplicationName)
        query.addQueryItem('begin_date', self.startTime.toString('yyyyMMdd hh:mm'))
        query.addQueryItem('end_date', self.endTime.addSecs(-1).toString('yyyyMMdd hh:mm'))
        query.addQueryItem('units', 'english')
        query.addQueryItem('time_zone', 'gmt')
        query.addQueryItem('product', self.productName)
        query.addQueryItem('format', 'xml')
        self.addQueryItems(query)

        return self.baseUrl + '?' + query.query()

    def addQueryItems(self, query):
        raise Exception('Override required')

    def parseContent(self, content):
        raise Exception('Override required')

    def processFetch(self):
        self.content = self.fetcher.contentAsString()

    def processFinish(self):
        if self.content is None:
            self.reject()
            return

        try:
            self.predictions = self.parseContent(self.content)
            self.resolve()
        except Exception as e:
            print('Exception in parsing: ', e)
            self.reject()    

class CurrentPredictionRequest(PredictionRequest):
    SpeedDirectionType = 0
    VelocityMajorType = 1
    EventType = 2

    INTERVAL_MAX_SLACK = 'MAX_SLACK'
    INTERVAL_DEFAULT = str(PredictionManager.STEP_MINUTES)
    VEL_TYPE_SPEED_DIR = 'speed_dir'
    VEL_TYPE_DEFAULT = 'default'

    def __init__(self, manager, stationFeature, start, end, requestType):
        # This horrible hack works around a caching bug in CO-OPS where requests with parameters
        # that differ only in the value for `vel_type` are confused with each other. We artificially
        # tweak the end time by -1 minute to defeat this bug, which doesn't change the actual number
        # of delivered predictions.
        if requestType == CurrentPredictionRequest.VelocityMajorType:
            end = end.addSecs(-60)

        super(CurrentPredictionRequest, self).__init__(manager, stationFeature, start, end)
        self.productName = 'currents_predictions'
        self.baseUrl = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
        self.requestType = requestType
        print('{}: Requesting type {} for {}'.format(self.stationFeature['station'],self.requestType,start.toString()))

    def addQueryItems(self, query):
        query.addQueryItem('station', self.stationFeature['id'])
        query.addQueryItem('bin', str(self.stationFeature['bin']))
        if self.requestType == self.SpeedDirectionType:
            query.addQueryItem('vel_type', self.VEL_TYPE_SPEED_DIR)
            query.addQueryItem('interval', self.INTERVAL_DEFAULT)
        elif self.requestType == self.VelocityMajorType:
            query.addQueryItem('vel_type', self.VEL_TYPE_DEFAULT)
            query.addQueryItem('interval', self.INTERVAL_DEFAULT)
        else:
            query.addQueryItem('interval', self.INTERVAL_MAX_SLACK)

    def parseContent(self, content):
        root = ET.fromstring(content) 

        f = None
        layer = self.manager.predictionsLayer
        fields = layer.fields()
        features = []
        floodDir = self.stationFeature['meanFloodDir']
        ebbDir = self.stationFeature['meanEbbDir']

        # Get the list of predictions
        cp = root.findall('cp')
        for prediction in cp:
            dt = QDateTime.fromString(prediction.find('Time').text, 'yyyy-MM-dd hh:mm')
            dt.setTimeSpec(Qt.TimeSpec.UTC)   # just to be clear on this, this is a UTC time

            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry(self.stationFeature.geometry()))

            f['station'] = self.stationFeature['station']
            f['depth'] = parseFloatNullable(prediction.find('Depth').text)
            f['time'] = dt
            
            # we have one of several different possibilities:
            #  - timed measurement, flood/ebb, signed velocity
            #  - max/slack measurement, flood/ebb, signed velocity
            #  - timed measurement, varying angle, unsigned velocity
            directionElement = prediction.find('Direction')
            valflags = PredictionFlags.Current
            if self.stationFeature['flags'] & StationFlags.Surface:
                valflags |= PredictionFlags.Surface
            if directionElement != None:
                direction = parseFloatNullable(directionElement.text)

                magnitude = float(prediction.find('Speed').text)
                valflags |= PredictionFlags.Time

                # synthesize the value along flood/ebb dimension if possible
                if floodDir != NULL and ebbDir != NULL:
                    floodFactor = math.cos(math.radians(floodDir - direction))
                    ebbFactor = math.cos(math.radians(ebbDir - direction))
                    if floodFactor > ebbFactor:
                        value = magnitude * floodFactor
                    else:
                        value = -magnitude * ebbFactor
                else:
                    value = NULL  # we wil have to get this by asking for it explicily

            else:
                vel = float(prediction.find('Velocity_Major').text)
                if (vel >= 0):
                    direction = floodDir
                else:
                    direction = ebbDir
                value = vel
                magnitude = abs(vel)
                typeElement = prediction.find('Type')
                if typeElement != None:
                    valtype = typeElement.text
                    if valtype == 'slack':
                        valflags |= PredictionFlags.Zero
                    elif valtype == 'flood':
                        valflags |= PredictionFlags.Max
                    elif valtype == 'ebb':
                        valflags |= PredictionFlags.Min
                    else:
                        valflags = PredictionFlags.Time
                else:
                    valflags |= PredictionFlags.Time

            f['value'] = value
            f['dir'] = direction
            f['magnitude'] = magnitude
            f['flags'] = valflags
            features.append(f)

        print('{}: Response had {} features'.format(self.stationFeature['station'],len(features)))
        return features

class TidePredictionRequest(PredictionRequest):
    WaterLevelType = 0
    EventType = 1

    INTERVAL_HILO = 'hilo'
    INTERVAL_DEFAULT = str(PredictionManager.STEP_MINUTES)

    def __init__(self, manager, stationFeature, start, end, requestType):
        super(TidePredictionRequest, self).__init__(manager, stationFeature, start, end)
        self.productName = 'predictions'
        self.baseUrl = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter'
        self.requestType = requestType
        print('{}: Requesting type {} for {}'.format(self.stationFeature['station'],self.requestType,start.toString()))

    def addQueryItems(self, query):
        query.addQueryItem('datum', 'MLLW')
        query.addQueryItem('station', self.stationFeature['id'])
        if self.requestType == self.WaterLevelType:
            query.addQueryItem('interval', self.INTERVAL_DEFAULT)
        else:
            query.addQueryItem('interval', self.INTERVAL_HILO)

    def parseContent(self, content):
        root = ET.fromstring(content) 

        f = None
        layer = self.manager.predictionsLayer
        fields = layer.fields()
        features = []
        floodDir = self.stationFeature['meanFloodDir']
        ebbDir = self.stationFeature['meanEbbDir']

        # Get the list of predictions
        pr = root.findall('pr')
        for prediction in pr:
            dt = QDateTime.fromString(prediction.get('t'), 'yyyy-MM-dd hh:mm')
            dt.setTimeSpec(Qt.TimeSpec.UTC)   # just to be clear on this, this is a UTC time

            f = QgsFeature(fields)
            f.setGeometry(QgsGeometry(self.stationFeature.geometry()))

            f['station'] = self.stationFeature['station']
            f['time'] = dt
            
            valflags = PredictionFlags.Surface
            value = float(prediction.get('v'))
            valtype = prediction.get('type')
            if valtype == 'H':
                valflags |= PredictionFlags.Max
            elif valtype == 'L':
                valflags |= PredictionFlags.Min
            else:
                valflags |= PredictionFlags.Time

            f['value'] = value
            f['flags'] = valflags

            features.append(f)

        print('{}: Response had {} features'.format(self.stationFeature['station'],len(features)))
        return features
