import collections
import os
from optparse import OptionParser
import logging
import sys
import subprocess
import csv
import time
import datetime
import json
import socket
import requests
import datetime

def getParameters():
    usage = "Usage: %prog [options]"
    parser = OptionParser(usage=usage)
    parser.add_option("-d", "--directory",
                      action="store", dest="homepath", help="Directory to run from")
    parser.add_option("-w", "--serverUrl",
                      action="store", dest="serverUrl", help="Server Url")
    parser.add_option("-p", "--profilePath",
                      action="store", dest="profilePath", help="Server Url")
    (options, args) = parser.parse_args()

    parameters = {}
    if options.homepath is None:
        parameters['homepath'] = os.getcwd()
    else:
        parameters['homepath'] = options.homepath
    if options.serverUrl == None:
        parameters['serverUrl'] = 'https://app.insightfinder.com'
    else:
        parameters['serverUrl'] = options.serverUrl
    if options.profilePath == None:
        parameters['profilePath'] = '/data/nfsen/profiles-data/zone1_profile/'
    else:
        parameters['profilePath'] = options.profilePath

    return parameters

def getAgentConfigVars():
    configVars = {}
    with open(os.path.join(parameters['homepath'], ".agent.bashrc"), 'r') as configFile:
        fileContent = configFile.readlines()
        if len(fileContent) < 6:
            logger.error("Agent not correctly configured. Check .agent.bashrc file.")
            sys.exit(1)
        # get license key
        licenseKeyLine = fileContent[0].split(" ")
        if len(licenseKeyLine) != 2:
            logger.error("Agent not correctly configured(license key). Check .agent.bashrc file.")
            sys.exit(1)
        configVars['licenseKey'] = licenseKeyLine[1].split("=")[1].strip()
        # get project name
        projectNameLine = fileContent[1].split(" ")
        if len(projectNameLine) != 2:
            logger.error("Agent not correctly configured(project name). Check .agent.bashrc file.")
            sys.exit(1)
        configVars['projectName'] = projectNameLine[1].split("=")[1].strip()
        # get username
        userNameLine = fileContent[2].split(" ")
        if len(userNameLine) != 2:
            logger.error("Agent not correctly configured(username). Check .agent.bashrc file.")
            sys.exit(1)
        configVars['userName'] = userNameLine[1].split("=")[1].strip()
        # get sampling interval
        samplingIntervalLine = fileContent[4].split(" ")
        if len(samplingIntervalLine) != 2:
            logger.error("Agent not correctly configured(sampling interval). Check .agent.bashrc file.")
            sys.exit(1)
        configVars['samplingInterval'] = samplingIntervalLine[1].split("=")[1].strip()
    return configVars

def getReportingConfigVars():
    reportingConfigVars = {}
    with open(os.path.join(parameters['homepath'], "reporting_config.json"), 'r') as f:
        config = json.load(f)
    reporting_interval_string = config['reporting_interval']
    is_second_reporting = False
    if reporting_interval_string[-1:] == 's':
        is_second_reporting = True
        reporting_interval = float(config['reporting_interval'][:-1])
        reportingConfigVars['reporting_interval'] = float(reporting_interval / 60.0)
    else:
        reportingConfigVars['reporting_interval'] = int(config['reporting_interval'])
        reportingConfigVars['keep_file_days'] = int(config['keep_file_days'])
        reportingConfigVars['prev_endtime'] = config['prev_endtime']
        reportingConfigVars['deltaFields'] = config['delta_fields']

    reportingConfigVars['keep_file_days'] = int(config['keep_file_days'])
    reportingConfigVars['prev_endtime'] = config['prev_endtime']
    reportingConfigVars['deltaFields'] = config['delta_fields']
    return reportingConfigVars

def updateDataStartTime():
    if reportingConfigVars['prev_endtime'] != "0":
        startTime = reportingConfigVars['prev_endtime']
        # pad a second after prev_endtime
        startTimeEpoch = 1000 + long(1000 * time.mktime(time.strptime(startTime, "%Y%m%d%H%M%S")));
        end_time_epoch = startTimeEpoch + 1000 * 60 * reportingConfigVars['reporting_interval']
    else:  # prev_endtime == 0
        end_time_epoch = int(time.time()) * 1000
        startTimeEpoch = end_time_epoch - 1000 * 60 * reportingConfigVars['reporting_interval']
    return startTimeEpoch

def getMetricsFromFile(profileFolder, filePath, rawDataMap):
        command = 'nfdump -M ' + profileFolder + '/bittorrent:p2p:sip:rdp:mysql:ms-sql-s:http-alt:microsoft-ds:https:snmp:netbios-ns:loc-srv:ntp:http:dns:telnet:ssh:ftp:chargen:icmp -r ' + filePath + ' -o csv -a -A proto,srcip,srcport,dstip,dstport -m > ' + "out.csv"
        output = subprocess.check_output(command, shell=True)

        with open("out.csv") as metricFile:
            metricCSVReader = csv.reader(metricFile)
            maxLen = 0
            for row in metricCSVReader:
                if metricCSVReader.line_num == 1:
                    print row
                    # Get all the metric names from header
                    fieldnames = row
                    maxLen = len(fieldnames)
                    # get index of the timestamp column
                    for i in range(0, len(fieldnames)):
                        if fieldnames[i] == "ts":
                            timestampIndex = i
                elif metricCSVReader.line_num > 1:
                    # INPackets->ipkt OutPackets->opkt
                    if len(row) < maxLen:
                        continue
                    print row
                    sourceAddress = row[3] + ":" + row[5]
                    destAddress = row[4] + ":" + row[6]
                    protocol = row[7]
                    hostName = sourceAddress + "_" + destAddress + "_" + protocol

                    timestamp = int(time.mktime(datetime.datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S").timetuple())) * 1000
                    duration = row[2]
                    inPackets = row[11]
                    inBytes = row[12]
                    outPackets = row[13]
                    outBytes = row[14]
                    if timestamp in rawDataMap:
                        valueMap = rawDataMap[timestamp]
                    else:
                        valueMap = {}

                    valueMap['Duration[' + hostName + ']:9900'] = duration
                    valueMap['InPackets[' + hostName + ']:9901'] = inPackets
                    valueMap['InBytes[' + hostName + ']:9902'] = inBytes
                    valueMap['OutPackets[' + hostName + ']:9903'] = outPackets
                    valueMap['OutBytes[' + hostName + ']:9904'] = outBytes
                    rawDataMap[timestamp] = valueMap

def closestNumber(n, m) :
    # Find the quotient
    q = n // m

    # Floor closest number
    n1 = m * q

    return n1

#generate filename for current timestamp and  sampling interval
def getFileNameList():
    currentDate = time.strftime("%Y/%m/%d", time.localtime())
    fileNameList = []
    start_time_epoch = long(time.time())
    chunks = int(reportingConfigVars['reporting_interval'] / 5)
    startMin = time.strftime("%Y%m%d%H%M", time.localtime(start_time_epoch))
    closestMinute = closestNumber(int(startMin[-2:]), 5)
    if closestMinute < 10:
        closestMinStr = '0' + str(closestMinute)
        newDate = startMin[:-2] + str(closestMinStr)
    else:
        newDate = startMin[:-2] + str(closestMinute)
    chunks -= 1
    currentTime = datetime.datetime.strptime(newDate, "%Y%m%d%H%M") - datetime.timedelta(minutes=5)
    closestMinute = time.strftime("%Y%m%d%H%M", currentTime.timetuple())
    filename = os.path.join(currentDate, "nfcapd." + newDate)
    fileNameList.append(filename)
    while chunks > 0:
        chunks -= 1
        filename = os.path.join(currentDate, "nfcapd." + closestMinute)
        fileNameList.append(filename)
        currentTime = datetime.datetime.strptime(closestMinute, "%Y%m%d%H%M") - datetime.timedelta(minutes=5)
        closestMinute = time.strftime("%Y%m%d%H%M", currentTime.timetuple())
    return fileNameList

def setloggerConfig():
    # Get the root logger
    logger = logging.getLogger(__name__)
    # Have to set the root logger level, it defaults to logging.WARNING
    logger.setLevel(logging.INFO)
    # route INFO and DEBUG logging to stdout from stderr
    logging_handler_out = logging.StreamHandler(sys.stdout)
    logging_handler_out.setLevel(logging.DEBUG)
    logging_handler_out.addFilter(LessThanFilter(logging.WARNING))
    logger.addHandler(logging_handler_out)

    logging_handler_err = logging.StreamHandler(sys.stderr)
    logging_handler_err.setLevel(logging.WARNING)
    logger.addHandler(logging_handler_err)
    return logger

class LessThanFilter(logging.Filter):
    def __init__(self, exclusive_maximum, name=""):
        super(LessThanFilter, self).__init__(name)
        self.max_level = exclusive_maximum

    def filter(self, record):
        #non-zero return means we log this message
        return 1 if record.levelno < self.max_level else 0

def sendData(metricData):
    sendDataTime = time.time()
    # prepare data for metric streaming agent
    toSendDataDict = {}
    toSendDataDict["metricData"] = json.dumps(metricData)
    toSendDataDict["licenseKey"] = agentConfigVars['licenseKey']
    toSendDataDict["projectName"] = agentConfigVars['projectName']
    toSendDataDict["userName"] = agentConfigVars['userName']
    toSendDataDict["instanceName"] = socket.gethostname().partition(".")[0]
    toSendDataDict["samplingInterval"] = str(int(reportingConfigVars['reporting_interval'] * 60))
    toSendDataDict["agentType"] = "nfdump"

    toSendDataJSON = json.dumps(toSendDataDict)
    logger.debug("TotalData: " + str(len(bytearray(toSendDataJSON))))

    #send the data
    postUrl = parameters['serverUrl'] + "/customprojectrawdata"
    response = requests.post(postUrl, data=json.loads(toSendDataJSON))
    if response.status_code == 200:
        logger.info(str(len(bytearray(toSendDataJSON))) + " bytes of data are reported.")
    else:
        logger.info("Failed to send data.")
    logger.debug("--- Send data time: %s seconds ---" % (time.time() - sendDataTime))

if __name__ == '__main__':
    logger = setloggerConfig()
    parameters = getParameters()
    agentConfigVars = getAgentConfigVars()
    reportingConfigVars = getReportingConfigVars()

    # locate time range and date range
    prevEndtimeEpoch = reportingConfigVars['prev_endtime']
    newPrevEndtimeEpoch = 0
    startTimeEpoch = 0
    startTimeEpoch = updateDataStartTime()

    dataDirectory = 'data/'
    rawDataMap = collections.OrderedDict()
    pcapFileList = getFileNameList()
    for pcapFile in pcapFileList:
        getMetricsFromFile(parameters['profilePath'], pcapFile, rawDataMap)
    metricData = []

    for timestamp in rawDataMap.keys():
        valueMap = rawDataMap[timestamp]
        valueMap['timestamp'] = str(timestamp)
        metricData.append(valueMap)

    sendData(metricData)