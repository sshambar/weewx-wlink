#!/usr/bin/python
# $Id: wlink.py 1783 2018-01-29 11:57:57Z mwall $
# Copyright 2014 Matthew Wall
"""weewx driver for WeatherLink web sites
   download data from a weatherlink site for use in weewx

To use this driver:

1) copy this file to the weewx user directory

   cp wlink.py /home/weewx/bin/user

2) configure weewx.conf

[Station]
    ...
    station_type = WeatherLink
[WeatherLink]
    username = USERNAME
    password = PASSWORD
    driver = user.wlink
"""

# FIXME: there are unresolved timezone issues in this code.  if you grab data
# from a weatherlink site that is in a time zone other than the one in which
# you are running weewx, you will have problems.  the timestamps coming from
# weatherlink are local to the weather station.  the 'loop' data have timestamp
# in the timezone of the machine on which weewx is running.

# FIXME: the archive records from weatherlink often have unusable timestamps.
# the last few records will often have datestamp of 0xff and timestamp of 0xff.
# for now we just skip these, since there is no time information we can use.

# FIXME: the web page parsing is really, really crude.  it will break if there
# are any significant changes to the weatherlink web page structure.
# unfortunately the weatherlink web page is really primitive and gives us
# no structural hints as to the content.

from __future__ import with_statement
import httplib
import re
import socket
import struct
import syslog
import time
import urllib2
from HTMLParser import HTMLParser

import weewx
import weewx.drivers

DRIVER_NAME = "WeatherLink"
DRIVER_VERSION = "0.14"

if weewx.__version__ < "3":
    raise weewx.UnsupportedFeature("weewx 3 is required, found %s" %
                                   weewx.__version__)

def logmsg(dst, msg):
    syslog.syslog(dst, 'wlink: %s' % msg)

def logdbg(msg):
    logmsg(syslog.LOG_DEBUG, msg)

def loginf(msg):
    logmsg(syslog.LOG_INFO, msg)

def logcrt(msg):
    logmsg(syslog.LOG_CRIT, msg)

def logerr(msg):
    logmsg(syslog.LOG_ERR, msg)

def logdev(msg):
#    print msg
    pass

def try_float(x):
    try:
        return float(x)
    except ValueError:
        return None

def loader(config_dict, engine):
    return WeatherLink(**config_dict['WeatherLink'])

class WeatherLink(weewx.drivers.AbstractDevice):
    """weewx driver to download data from WeatherLink web sites
    
    Parse the web page for 'loop' data.  This happens every 60 seconds.

    There are two formats:
    A - pre-2015; uses div and span within td
    B - end of 2015; elimited divs and inline styles

    For archive data, make a request to the server.
    """

    rain_bucket_dict = {0:'0.01 inches', 1:'0.2 mm', 2:'0.1 mm'}

    def __init__(self, **stn_dict):
        loginf("version is %s" % DRIVER_VERSION)
        self.username = stn_dict['username']
        self.password = stn_dict['password']
        self.format = stn_dict.get('format', 'B')
        loginf("expecting HTML format '%s'" % self.format)
        self.poll_interval = float(stn_dict.get('poll_interval', 60))
        loginf("polling interval is %s" % self.poll_interval)
        rain_bucket_type = int(stn_dict.get('rain_bucket_type', 0))
        loginf("rain bucket type is %d (%s)" %
               (rain_bucket_type, self.rain_bucket_dict[rain_bucket_type]))
        self.max_tries = int(stn_dict.get('max_tries', 5))
        self.retry_wait = int(stn_dict.get('retry_wait', 30))
        self.raw_data_cache = stn_dict.get('raw_data_cache', None)
        self._archive_interval = stn_dict.get('archive_interval', None)
        if self._archive_interval is not None:
            self._archive_interval = int(self._archive_interval)
            loginf("archive interval is %s seconds" % self._archive_interval)

        if rain_bucket_type == 1:
            _archive_map['rain'] = _archive_map['rainRate'] = _bucket_1
        elif rain_bucket_type == 2:
            _archive_map['rain'] = _archive_map['rainRate'] = _bucket_2
        else:
            _archive_map['rain'] = _archive_map['rainRate'] = _val100

    @property
    def hardware_name(self):
        return "WeatherLink"

    @property
    def archive_interval(self):
        # weewx wants the archive interval in seconds
        if self._archive_interval is None:
            self._archive_interval = self.download_archive_interval() * 60
        return self._archive_interval

    def genLoopPackets(self):
        while True:
            data = self.get_data("http://www.weatherlink.com/user/%s/index.php"
                                 "?view=main&headers=0&type=2" % self.username)
            packet = {
                'dateTime': int(time.time() + 0.5),
                'usUnits': weewx.US, # type 2 is US imperial
                }
            if self.raw_data_cache is not None:
                with open(self.raw_data_cache, "w") as f:
                    f.write(data)
            packet.update(self.parse_page(data))
            yield packet
            time.sleep(self.poll_interval)

    def genArchiveRecords(self, since_ts):
        if since_ts is None:
            since_ts = 0
        ts = _epoch_to_timestamp(since_ts)
        data = self.get_data("http://weatherlink.com/webdl.php"
                             "?timestamp=%s&user=%s&pass=%s&action=data" %
                             (ts, self.username, self.password))
        if data is None:
            return
        logdbg("found %s archive records since %s" %
               ((len(data) / 52), since_ts))
        sidx = 0
        while sidx < len(data):
            record = self.unpack_archive_packet(data[sidx:sidx + 52])
            if record['dateTime']:
                logdbg("yielding: %s" % record)
                yield record
            else:
                logdbg("skipping: %s" % record)
            sidx += 52

    def get_data(self, url):
        for count in range(self.max_tries):
            try:
                response = urllib2.urlopen(url)
                data = response.read()
                return data
            except (urllib2.URLError, socket.error,
                    httplib.BadStatusLine, httplib.IncompleteRead), e:
                logerr('download failed attempt %d of %d: %s' %
                       (count + 1, self.max_tries, e))
                time.sleep(self.retry_wait)
        else:
            logerr('download failed after %d tries' % self.max_tries)
        return None

    def parse_page(self, text):
        """parse weather data from a weatherlink web page"""
        packet = {}
        if text is not None:
            parser = WLParserB()
            if self.format == 'A':
                parser = WLParserA()
            parser.feed(text)
            packet = parser.get_data()
        return packet

    def download_archive_interval(self):
        logdbg('downloading archive interval')
        data = self.get_data("http://weatherlink.com/webdl.php"
                             "?timestamp=0&user=%s&pass=%s&action=headers" %
                             (self.username, self.password))
        if data is None:
            raise weewx.WeeWxIOError("download of archive interval failed")
        for line in data.splitlines():
            if line.find('ArchiveInt=') >= 0:
                logdbg('found archive interval: %s' % line)
                return int(line[11:])
        raise weewx.WeeWxIOError("cannot determine archive interval from %s" %
                                 data)

    # adapted from the implementation in the vantage driver
    def unpack_archive_packet(self, raw_record_string):
        packet_type = ord(raw_record_string[42])
        if packet_type == 0xff:
            archive_format = rec_fmt_A
            data_types = rec_types_A
        elif packet_type == 0x00:
            archive_format = rec_fmt_B
            data_types = rec_types_B
        else:
            raise weewx.UnknownArchiveType("Unknown archive type: 0x%x" %
                                           (packet_type,))
        data_tuple = archive_format.unpack(raw_record_string)
        raw_record = dict(zip(data_types, data_tuple))
        packet = {
            'dateTime': _archive_datetime(raw_record['date_stamp'],
                                          raw_record['time_stamp']),
            'usUnits': weewx.US,
            }
        for t in raw_record:
            func = _archive_map.get(t)
            if func:
                packet[t] = func(raw_record[t])
        packet['interval'] = self.archive_interval / 60
        return packet

# =============================================================================
#                      Decoding routines                                      
# =============================================================================

def _epoch_to_timestamp(epoch):
    """convert unix epoch to davis timestamp"""
    tt = time.localtime(epoch)
    ds = tt[2] + (tt[1] << 5) + ((tt[0] - 2000) << 9)
    ts = tt[3] * 100 + tt[4]
    x = (ds << 16) | ts
    return x

def _archive_datetime(datestamp, timestamp):
    """Returns the epoch time of the archive packet."""
    try:
        time_tuple = ((0xfe00 & datestamp) >> 9,    # year
                      (0x01e0 & datestamp) >> 5,    # month
                      (0x001f & datestamp),         # day
                      timestamp // 100,             # hour
                      timestamp % 100,              # minute
                      0,                            # second
                      0, 0, -1)                     # have OS guess DST
        ts = int(time.mktime(time_tuple))
    except (OverflowError, ValueError, TypeError):
        logerr("cannot make timestamp: ds=%s ts=%s" % (datestamp, timestamp))
        ts = None
    return ts

def _stime(v):
    h = v/100
    m = v%100
    # Return seconds since midnight
    return 3600*h + 60*m

def _big_val(v):
    return float(v) if v != 0x7fff else None

def _big_val10(v):
    return float(v)/10.0 if v != 0x7fff else None

def _big_val100(v):
    return float(v)/100.0 if v != 0xffff else None

def _val100(v):
    return float(v)/100.0

def _val1000(v):
    return float(v)/1000.0

def _val1000Zero(v):
    return float(v)/1000.0 if v != 0 else None

def _little_val(v):
    return float(v) if v != 0x00ff else None

def _little_val10(v):
    return float(v)/10.0 if v != 0x00ff else None

def _little_temp(v):
    return float(v-90) if v != 0x00ff else None

def _null(v):
    return v

def _null_float(v):
    return float(v)

def _null_int(v):
    return int(v)

def _windDir(v):
    return float(v) * 22.5 if v!= 0x00ff else None

# Rain bucket type "1", a 0.2 mm bucket
def _bucket_1(v):
    return float(v)*0.00787401575

def _bucket_1_None(v):
    return float(v)*0.00787401575 if v != 0xffff else None

# Rain bucket type "2", a 0.1 mm bucket
def _bucket_2(v):
    return float(v)*0.00393700787

def _bucket_2_None(v):
    return float(v)*0.00393700787 if v != 0xffff else None

_archive_map={'barometer'      : _val1000Zero,
              'inTemp'         : _big_val10,
              'outTemp'        : _big_val10,
              'highOutTemp'    : lambda v : float(v/10.0) if v != -32768 else None,
              'lowOutTemp'     : _big_val10,
              'inHumidity'     : _little_val,
              'outHumidity'    : _little_val,
              'windSpeed'      : _little_val,
              'windDir'        : _windDir,
              'windGust'       : _null_float,
              'windGustDir'    : _windDir,
              'rain'           : _val100,
              'rainRate'       : _val100,
              'ET'             : _val1000,
              'radiation'      : _big_val,
              'highRadiation'  : _big_val,
              'UV'             : _little_val10,
              'highUV'         : _little_val10,
              'extraTemp1'     : _little_temp,
              'extraTemp2'     : _little_temp,
              'extraTemp3'     : _little_temp,
              'soilTemp1'      : _little_temp,
              'soilTemp2'      : _little_temp,
              'soilTemp3'      : _little_temp,
              'soilTemp4'      : _little_temp,
              'leafTemp1'      : _little_temp,
              'leafTemp2'      : _little_temp,
              'extraHumid1'    : _little_val,
              'extraHumid2'    : _little_val,
              'soilMoist1'     : _little_val,
              'soilMoist2'     : _little_val,
              'soilMoist3'     : _little_val,
              'soilMoist4'     : _little_val,
              'leafWet1'       : _little_val,
              'leafWet2'       : _little_val,
              'leafWet3'       : _little_val,
              'leafWet4'       : _little_val,
              'forecastRule'   : _null,
              'readClosed'     : _null,
              'readOpened'     : _null}

rec_format_A =[('date_stamp',              'H'), ('time_stamp',    'H'), ('outTemp',    'h'),
               ('highOutTemp',             'h'), ('lowOutTemp',    'h'), ('rain',       'H'),
               ('rainRate',                'H'), ('barometer',     'H'), ('radiation',  'H'),
               ('number_of_wind_samples',  'H'), ('inTemp',        'h'), ('inHumidity', 'B'),
               ('outHumidity',             'B'), ('windSpeed',     'B'), ('windGust',   'B'),
               ('windGustDir',             'B'), ('windDir',       'B'), ('UV',         'B'),
               ('ET',                      'B'), ('invalid_data',  'B'), ('soilMoist1', 'B'),
               ('soilMoist2',              'B'), ('soilMoist3',    'B'), ('soilMoist4', 'B'),
               ('soilTemp1',               'B'), ('soilTemp2',     'B'), ('soilTemp3',  'B'),
               ('soilTemp4',               'B'), ('leafWet1',      'B'), ('leafWet2',   'B'),
               ('leafWet3',                'B'), ('leafWet4',      'B'), ('extraTemp1', 'B'),
               ('extraTemp2',              'B'), ('extraHumid1',   'B'), ('extraHumid2','B'),
               ('readClosed',              'H'), ('readOpened',    'H'), ('unused',     'B')]

rec_format_B = [('date_stamp',             'H'), ('time_stamp',    'H'), ('outTemp',    'h'),
                ('highOutTemp',            'h'), ('lowOutTemp',    'h'), ('rain',       'H'),
                ('rainRate',               'H'), ('barometer',     'H'), ('radiation',  'H'),
                ('number_of_wind_samples', 'H'), ('inTemp',        'h'), ('inHumidity', 'B'),
                ('outHumidity',            'B'), ('windSpeed',     'B'), ('windGust',   'B'),
                ('windGustDir',            'B'), ('windDir',       'B'), ('UV',         'B'),
                ('ET',                     'B'), ('highRadiation', 'H'), ('highUV',     'B'),
                ('forecastRule',           'B'), ('leafTemp1',     'B'), ('leafTemp2',  'B'),
                ('leafWet1',               'B'), ('leafWet2',      'B'), ('soilTemp1',  'B'),
                ('soilTemp2',              'B'), ('soilTemp3',     'B'), ('soilTemp4',  'B'),
                ('download_record_type',   'B'), ('extraHumid1',   'B'), ('extraHumid2','B'),
                ('extraTemp1',             'B'), ('extraTemp2',    'B'), ('extraTemp3', 'B'),
                ('soilMoist1',             'B'), ('soilMoist2',    'B'), ('soilMoist3', 'B'),
                ('soilMoist4',             'B')]

rec_types_A, fmt_A = zip(*rec_format_A)
rec_types_B, fmt_B = zip(*rec_format_B)
rec_fmt_A = struct.Struct('<' + ''.join(fmt_A))
rec_fmt_B = struct.Struct('<' + ''.join(fmt_B))

compass_points = {'N': 0, 'NNE': 22.5, 'NE': 45, 'ENE': 67.5, 'E': 90,
                  'ESE': 112.5, 'SE': 135, 'SSE': 157.5, 'S': 180,
                  'SSW': 202.5, 'SW': 225, 'WSW': 247.5, 'W': 270,
                  'WNW': 292.5, 'NW': 315, 'NNW': 337.5}

# crude parser to get data from the weatherlink web page.
# works with html up to at least late 2015
class WLParserA(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.packet = {}
        self.hierarchy = []
        self.last_value = None
        self.last_key = None

    def handle_starttag(self, tag, attrs):
        if tag not in ['meta', 'link', 'br']:
            self.hierarchy.append(tag)

    def handle_endtag(self, tag):
        self.hierarchy.pop()

    def handle_data(self, data):
        data = data.strip()
        if not 'span' in self.hierarchy:
            if len(data):
                self.last_key = data
            return
        if data in compass_points:
            self.packet['windDir'] = compass_points[data]
        elif self.last_value is not None:
            if data == self.last_value:
                self.packet['outTemperature'] = try_float(self.last_value)
            elif data == 'km/h' or data == 'Mph':
                self.packet['windSpeed'] = try_float(self.last_value)
            elif data == '%':
                self.packet['outHumidity'] = try_float(self.last_value)
            self.last_value = None
        elif re.search('\d+mm', data):
            m = re.search('(\d+)mm', data)
            self.packet['rain'] = try_float(m.group(1))
            if self.packet['rain'] is not None:
                self.packet['rain'] /= 10 # weewx wants cm
        elif re.search('[\d.]+mb', data):
            m = re.search('([\d.]+)mb', data)
            self.packet['barometer'] = try_float(m.group(1))
        elif re.search('[\d.]+hPa', data):
            m = re.search('([\d.]+)hPa', data)
            self.packet['barometer'] = try_float(m.group(1))
        elif re.search('[\d.]+"', data):
            m = re.search('([\d.]+)"', data)
            if self.last_key == 'Rain':
                self.packet['rain'] = try_float(m.group(1))
            elif self.last_key == 'Barometer':
                self.packet['barometer'] = try_float(m.group(1))
            self.last_key = None
        else:
            self.last_value = data

    def get_data(self):
        return self.packet

# works with weatherlink html as of 2016
# pick off temperature, wind, humidity, rain, barometer
class WLParserB(HTMLParser):
    def __init__(self):
        HTMLParser.__init__(self)
        self.packet = {}
        self.hierarchy = []
        self.last_key = 'Temperature'

    def handle_starttag(self, tag, attrs):
        if tag not in ['meta', 'link', 'br']:
            self.hierarchy.append(tag)

    def handle_endtag(self, tag):
        self.hierarchy.pop()

    def handle_data(self, data):
        if not 'body' in self.hierarchy:
            return
        logdev('%s' % self.hierarchy)
        data = data.strip()
        logdev('%s' % data)
        if len(data) == 0:
            return
        if data in ['Wind', 'Humidity', 'Rain', 'Barometer']:
            self.last_key = data
            logdev('found key %s' % self.last_key)
            return

        if self.last_key == 'Wind':
            if data == 'Calm':
                self.packet['windSpeed'] = 0
                self.packet['windDir'] = None
            elif re.search('[NSEW]+', data):
                m = re.search('([NSEW]+)', data)
                wdir = m.group(1)
                if wdir in compass_points:
                    self.packet['windDir'] = compass_points[wdir]
                logdev('windDir: %s' % self.packet.get('windDir'))
                # do not reset last_key since we still need a wind speed
            elif re.search('[\d.]+', data):
                m = re.search('([\d.]+)', data)
                self.packet['windSpeed'] = try_float(m.group(1))
                logdev('windSpeed: %s' % self.packet.get('windSpeed')) 
                self.last_key = None
        if self.last_key == 'Temperature':
            self.packet['outTemperature'] = try_float(data)
            logdev('outTemperature: %s' % self.packet['outTemperature'])
            self.last_key = None
        if self.last_key == 'Humidity':
            self.packet['outHumidity'] = try_float(data)
            logdev('outHumidity: %s' % self.packet['outHumidity'])
            self.last_key = None
        if self.last_key == 'Rain':
            if re.search('[\d.]+mm', data):
                m = re.search('([\d.]+)mm', data)
                self.packet['rain'] = try_float(m.group(1))
                if self.packet['rain'] is not None:
                    self.packet['rain'] /= 10 # weewx wants cm
                logdev('rain: %s' % self.packet['rain'])
            elif re.search('[\d.]+"', data):
                m = re.search('([\d.]+)"', data)
                self.packet['rain'] = try_float(m.group(1))
                logdev('rain: %s' % self.packet['rain'])
            self.last_key = None
        if self.last_key == 'Barometer':
            if re.search('[\d.]+mb', data):
                m = re.search('([\d.]+)mb', data)
                self.packet['barometer'] = try_float(m.group(1))
                logdev('barometer: %s' % self.packet['barometer']) # FIXME
            elif re.search('[\d.]+hPa', data):
                m = re.search('([\d.]+)hPa', data)
                self.packet['barometer'] = try_float(m.group(1))
                logdev('barometer: %s' % self.packet['barometer']) # FIXME
            elif re.search('[\d.]+"', data):
                m = re.search('([\d.]+)"', data)
                self.packet['barometer'] = try_float(m.group(1))
                logdev('barometer: %s' % self.packet['barometer'])
            self.last_key = None

    def get_data(self):
        return self.packet

# To test this driver, do the following:
#   PYTHONPATH=/home/weewx/bin python /home/weewx/bin/user/wlink.py
if __name__ == "__main__":
    usage = """%prog [options]"""
    import optparse
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--username', dest='username', default='user',
                      help='weatherlink username')
    parser.add_option('--password', dest='password', default='pass',
                      help='weatherlink password')
    parser.add_option('--test-driver', dest='test_driver', action='store_true',
                      help='test the driver')
    parser.add_option('--test-parser', dest='test_parser', action='store_true',
                      help='test the parser')
    parser.add_option('--filename', dest='filename', default='testfile.xml',
                      help='name of file for test-parser')
    parser.add_option('--format', dest='format', default='B',
                      help='html format, can be A or B')
    (options, args) = parser.parse_args()
    syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))
    if options.test_parser:
        data = []
        with open(options.filename) as f:
            for line in f:
                data.append(line)
        parser = WLParserB()
        if options.format == 'A':
            parser = WLParserA()
        parser.feed(''.join(data))
        print parser.get_data()
    elif options.test_driver:
        import weeutil.weeutil
        station = WeatherLink(username=options.username,
                              password=options.password)
        for p in station.genLoopPackets():
            print weeutil.weeutil.timestamp_to_string(p['dateTime']), p
