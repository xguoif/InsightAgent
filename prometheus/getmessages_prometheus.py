#!/usr/bin/env python
import configparser
import json
import logging
import os
import socket
import sys
import time
import urllib.parse
import http.client
import shlex
import traceback
import glob
import sqlite3
from time import sleep
from sys import getsizeof
from itertools import chain
from optparse import OptionParser
from logging.handlers import QueueHandler
import multiprocessing
from multiprocessing.pool import ThreadPool
from multiprocessing import Pool, TimeoutError

import arrow
import pytz
import regex
import requests

# import ifobfuscate

"""
This script gathers data to send to Insightfinder
"""

# declare a few vars
TRUE = regex.compile(r"T(RUE)?", regex.IGNORECASE)
FALSE = regex.compile(r"F(ALSE)?", regex.IGNORECASE)
SPACES = regex.compile(r"\s+")
SLASHES = regex.compile(r"\/+")
UNDERSCORE = regex.compile(r"\_+")
COLONS = regex.compile(r"\:+")
LEFT_BRACE = regex.compile(r"\[")
RIGHT_BRACE = regex.compile(r"\]")
PERIOD = regex.compile(r"\.")
COMMA = regex.compile(r"\,")
NON_ALNUM = regex.compile(r"[^a-zA-Z0-9]")
FORMAT_STR = regex.compile(r"{(.*?)}")
HOSTNAME = socket.gethostname().partition('.')[0]
ISO8601 = ['%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%dT%H:%M:%S', '%Y%m%dT%H%M%SZ', 'epoch']
JSON_LEVEL_DELIM = '.'
CSV_DELIM = r",|\t"
ATTEMPTS = 3
CACHE_NAME = 'cache.db'


def initialize_cache_connection():
    # connect to local cache
    cache_loc = abs_path_from_cur(CACHE_NAME)
    if os.path.exists(cache_loc):
        cache_con = sqlite3.connect(cache_loc)
        cache_cur = cache_con.cursor()
    else:
        cache_con = sqlite3.connect(cache_loc)
        cache_cur = cache_con.cursor()
        cache_cur.execute('CREATE TABLE "cache" ( "instance"	TEXT NOT NULL UNIQUE, "alias"	TEXT NOT NULL)')

    return cache_con, cache_cur


def start_data_processing(logger, c_config, if_config_vars, agent_config_vars, metric_buffer, track, cache_con,
                          cache_cur, time_now):
    logger.info('Started......')

    # get metrics
    metrics = []
    if len(agent_config_vars['metrics']) == 0:
        url = urllib.parse.urljoin(agent_config_vars['api_url'], 'label/__name__/values')
        response = send_request(logger, url, params={}, proxies=agent_config_vars['proxies'])
        if response != -1:
            result = response.json()
            if result['status'] == 'success':
                metrics = result['data']
    else:
        metrics = agent_config_vars['metrics']

    if agent_config_vars['metrics_whitelist']:
        try:
            db_regex = regex.compile(agent_config_vars['metrics_whitelist'])
            metrics = list(filter(db_regex.match, metrics))
        except Exception as e:
            logger.error(e)

    metrics_with_function = []
    if agent_config_vars['metrics_whitelist_with_function']:
        try:
            db_regex = regex.compile(agent_config_vars['metrics_whitelist_with_function'])
            metrics_with_function = list(filter(db_regex.match, metrics))
        except Exception as e:
            logger.error(e)

    # filter metrics
    if agent_config_vars['metrics_to_ignore'] and len(agent_config_vars['metrics_to_ignore']) > 0:
        metrics = [x for x in metrics if x not in agent_config_vars['metrics_to_ignore']]

    if len(metrics) == 0:
        logger.error('Metric list is empty')
        return

    def get_query_uri(m):
        query = '{}{}'.format(m, agent_config_vars['query_label_selector'])
        if not agent_config_vars['metrics_whitelist_with_function'] or m in metrics_with_function:
            if agent_config_vars['query_with_function'] == 'increase':
                query = 'increase({}[{}s])'.format(query, if_config_vars['sampling_interval'])
        return query

    # parse sql string by params
    thread_pool = ThreadPool(agent_config_vars['thread_pool'])
    logger.debug('history range config: {}'.format(agent_config_vars['his_time_range']))
    if agent_config_vars['his_time_range']:
        logger.debug('Using time range for replay data')
        for timestamp in range(agent_config_vars['his_time_range'][0],
                               agent_config_vars['his_time_range'][1],
                               if_config_vars['sampling_interval']):
            params = [(logger, if_config_vars, agent_config_vars, m, {
                'query': get_query_uri(m),
                'time': timestamp,
            }) for m in metrics]
            results = thread_pool.map(query_messages_prometheus, params)
            result_list = list(chain(*results))
            parse_messages_prometheus(logger, if_config_vars, agent_config_vars, metric_buffer, track, cache_con,
                                      cache_cur,
                                      result_list)

            # clear metric buffer when piece of time range end
            clear_metric_buffer(logger, c_config, if_config_vars, metric_buffer, track)
    else:
        logger.debug('Using current time for streaming data')
        params = [(logger, if_config_vars, agent_config_vars, m, {
            'query': get_query_uri(m),
            'time': time_now,
        }) for m in metrics]
        results = thread_pool.map(query_messages_prometheus, params)
        result_list = list(chain(*results))
        parse_messages_prometheus(logger, if_config_vars, agent_config_vars, metric_buffer, track, cache_con, cache_cur,
                                  result_list)

    thread_pool.close()
    thread_pool.join()
    logger.info('Closed......')


def query_messages_prometheus(args):
    logger, if_config_vars, agent_config_vars, metric, params = args
    logger.info('Starting query metric: ' + metric)

    data = []
    try:
        # execute sql string
        url = urllib.parse.urljoin(agent_config_vars['api_url'], 'query')
        response = send_request(logger, url, params=params, proxies=agent_config_vars['proxies'])
        if response == -1:
            logger.error('Query metric error: ' + metric)
        else:
            result = response.json()
            if result['status'] != 'success':
                logger.error('Query metric error: ' + metric)
            else:
                data = result.get('data').get('result', [])

    except Exception as e:
        logger.error(e)
        logger.error('Query metric error: ' + metric)

    # add metric name in the value
    data = [{**item, 'metric_name': metric} for item in data]

    return data


def parse_messages_prometheus(logger, if_config_vars, agent_config_vars, metric_buffer, track, cache_con, cache_cur,
                              result):
    count = 0
    logger.info('Reading {} messages'.format(len(result)))

    for message in result:
        try:
            logger.debug(message)

            # date_field = message.get('metric').get('__name__')
            date_field = message.get('metric_name')

            # get metric name from `metrics_name_field`
            if agent_config_vars['metrics_name_field']:
                name_fields = [message.get('metric').get(f) for f in agent_config_vars['metrics_name_field'] or []]
                name_fields = [f for f in name_fields if f]
                if len(name_fields) > 0:
                    date_field = '_'.join(name_fields)

            instance = message.get('metric').get(
                agent_config_vars['instance_field'][0] if agent_config_vars['instance_field'] and len(
                    agent_config_vars['instance_field']) > 0 else 'instance')

            # filter by instance whitelist
            if agent_config_vars['instance_whitelist_regex'] \
                    and not agent_config_vars['instance_whitelist_regex'].match(instance):
                continue

            # add device info if has
            device = None
            device_field = agent_config_vars['device_field']
            if device_field and len(device_field) > 0:
                devices = [message.get('metric').get(d) for d in device_field]
                devices = [d for d in devices if d]
                device = devices[0] if len(devices) > 0 else None
            full_instance = make_safe_instance_string(instance, device)

            # check cache for alias
            full_instance = get_alias_from_cache(cache_con, cache_cur, full_instance)

            # get component, and build component instance map info
            component_map = None
            if agent_config_vars['component_field']:
                component = message.get('metric').get(agent_config_vars['component_field'])
                if component:
                    component_map = {"instanceName": full_instance, "componentName": component}

            vector_value = message.get('value')
            timestamp = int(vector_value[0]) * 1000
            data_value = vector_value[1]

            # set offset for timestamp
            timestamp += agent_config_vars['target_timestamp_timezone'] * 1000
            timestamp = str(timestamp)

            key = '{}-{}'.format(timestamp, full_instance)
            if key not in metric_buffer['buffer_dict']:
                metric_buffer['buffer_dict'][key] = {"timestamp": timestamp, "component_map": component_map}

            metric_key = '{}[{}]'.format(date_field, full_instance)
            metric_buffer['buffer_dict'][key][metric_key] = str(data_value)

        except Exception as e:
            logger.warn('Error when parsing message')
            logger.warn(e)
            logger.debug(traceback.format_exc())
            continue

        track['entry_count'] += 1
        count += 1
        if count % 1000 == 0:
            logger.info('Parse {0} messages'.format(count))
    logger.info('Parse {0} messages'.format(count))


def get_alias_from_cache(cache_con, cache_cur, alias):
    if cache_cur:
        cache_cur.execute('select alias from cache where instance="%s"' % alias)
        instance = cache_cur.fetchone()
        if instance:
            return instance[0]
        else:
            # Hard coded if alias hasn't been added to cache, add it 
            cache_cur.execute('insert into cache (instance, alias) values ("%s", "%s")' % (alias, alias))
            cache_con.commit()
            return alias


def get_agent_config_vars(logger, config_ini):
    """ Read and parse config.ini """
    """ get config.ini vars """
    if not os.path.exists(config_ini):
        logger.error('No config file found. Exiting...')
        return False
    with open(config_ini) as fp:
        config_parser = configparser.ConfigParser()
        config_parser.read_file(fp)

        prometheus_kwargs = {}
        api_url = ''
        metrics = None
        metrics_whitelist = None
        metrics_to_ignore = None
        query_label_selector = ''
        query_with_function = ''
        metrics_whitelist_with_function = None
        metrics_name_field = None
        instance_whitelist = ''
        instance_whitelist_regex = None
        his_time_range = None
        try:
            # prometheus settings
            prometheus_config = {}
            # only keep settings with values
            prometheus_kwargs = {k: v for (k, v) in list(prometheus_config.items()) if v}

            # handle boolean setting

            # handle required arrays
            if len(config_parser.get('prometheus', 'prometheus_uri')) != 0:
                prometheus_uri = config_parser.get('prometheus', 'prometheus_uri')
                api_url = urllib.parse.urljoin(prometheus_uri, '/api/v1/')
            else:
                return config_error(logger, 'prometheus_uri')

            # metrics
            metrics = config_parser.get('prometheus', 'metrics')
            metrics_whitelist = config_parser.get('prometheus', 'metrics_whitelist')
            metrics_to_ignore = config_parser.get('prometheus', 'metrics_to_ignore')
            query_label_selector = config_parser.get('prometheus', 'query_label_selector') or ''
            query_with_function = config_parser.get('prometheus', 'query_with_function')
            metrics_whitelist_with_function = config_parser.get('prometheus', 'metrics_whitelist_with_function')
            metrics_name_field = config_parser.get('prometheus', 'metrics_name_field')

            # time range
            his_time_range = config_parser.get('prometheus', 'his_time_range')

            # proxies
            agent_http_proxy = config_parser.get('prometheus', 'agent_http_proxy')
            agent_https_proxy = config_parser.get('prometheus', 'agent_https_proxy')

            # message parsing
            data_format = config_parser.get('prometheus', 'data_format').upper()
            # project_field = config_parser.get('agent', 'project_field', raw=True)
            component_field = config_parser.get('prometheus', 'component_field', raw=True)
            instance_field = config_parser.get('prometheus', 'instance_field', raw=True)
            instance_whitelist = config_parser.get('prometheus', 'instance_whitelist')
            device_field = config_parser.get('prometheus', 'device_field', raw=True)
            timestamp_field = config_parser.get('prometheus', 'timestamp_field', raw=True) or 'timestamp'
            target_timestamp_timezone = config_parser.get('prometheus', 'target_timestamp_timezone', raw=True) or 'UTC'
            timestamp_format = config_parser.get('prometheus', 'timestamp_format', raw=True)
            timezone = config_parser.get('prometheus', 'timezone') or 'UTC'
            data_fields = config_parser.get('prometheus', 'data_fields', raw=True)
            thread_pool = config_parser.get('prometheus', 'thread_pool', raw=True)

        except configparser.NoOptionError as cp_noe:
            logger.error(cp_noe)
            return config_error(logger)

        # metrics
        if len(metrics) != 0:
            metrics = [x.strip() for x in metrics.split(';') if x.strip()]
        if len(metrics_to_ignore) != 0:
            metrics_to_ignore = [x.strip() for x in metrics_to_ignore.split(',') if x.strip()]

        if len(query_with_function) != 0 and query_with_function not in ['increase']:
            return config_error(logger, 'target_timestamp_timezone')

        if len(instance_whitelist) != 0:
            try:
                instance_whitelist_regex = regex.compile(instance_whitelist)
            except Exception:
                return config_error(logger, 'instance_whitelist')
        if len(metrics_name_field) != 0:
            metrics_name_field = [x.strip() for x in metrics_name_field.split(',') if x.strip()]

        if len(his_time_range) != 0:
            his_time_range = [x for x in his_time_range.split(',') if x.strip()]
            his_time_range = [int(arrow.get(x).float_timestamp) for x in his_time_range]

        if len(target_timestamp_timezone) != 0:
            target_timestamp_timezone = int(arrow.now(target_timestamp_timezone).utcoffset().total_seconds())
        else:
            return config_error(logger, 'target_timestamp_timezone')

        if timezone:
            if timezone not in pytz.all_timezones:
                return config_error(logger, 'timezone')
            else:
                timezone = pytz.timezone(timezone)

        # data format
        if data_format in {'JSON',
                           'JSONTAIL',
                           'AVRO',
                           'XML'}:
            pass
        else:
            return config_error(logger, 'data_format')

        # proxies
        agent_proxies = dict()
        if len(agent_http_proxy) > 0:
            agent_proxies['http'] = agent_http_proxy
        if len(agent_https_proxy) > 0:
            agent_proxies['https'] = agent_https_proxy

        # fields
        # project_fields = project_field.split(',')
        instance_fields = [x.strip() for x in instance_field.split(',') if x.strip()]
        device_fields = [x.strip() for x in device_field.split(',') if x.strip()]
        timestamp_fields = timestamp_field.split(',')
        if len(data_fields) != 0:
            data_fields = data_fields.split(',')
            # for project_field in project_fields:
            #   if project_field in data_fields:
            #       data_fields.pop(data_fields.index(project_field))
            for instance_field in instance_fields:
                if instance_field in data_fields:
                    data_fields.pop(data_fields.index(instance_field))
            for device_field in device_fields:
                if device_field in data_fields:
                    data_fields.pop(data_fields.index(device_field))
            for timestamp_field in timestamp_fields:
                if timestamp_field in data_fields:
                    data_fields.pop(data_fields.index(timestamp_field))

        if len(thread_pool) != 0:
            thread_pool = int(thread_pool)
        else:
            thread_pool = 20

        # add parsed variables to a global
        config_vars = {
            'prometheus_kwargs': prometheus_kwargs,
            'api_url': api_url,
            'metrics': metrics,
            'metrics_whitelist': metrics_whitelist,
            'metrics_to_ignore': metrics_to_ignore,
            'query_label_selector': query_label_selector,
            'query_with_function': query_with_function,
            'metrics_whitelist_with_function': metrics_whitelist_with_function,
            'metrics_name_field': metrics_name_field,
            'his_time_range': his_time_range,

            'proxies': agent_proxies,
            'data_format': data_format,
            # 'project_field': project_fields,
            'component_field': component_field,
            'instance_field': instance_fields,
            "instance_whitelist_regex": instance_whitelist_regex,
            'device_field': device_fields,
            'data_fields': data_fields,
            'timestamp_field': timestamp_fields,
            'target_timestamp_timezone': target_timestamp_timezone,
            'timezone': timezone,
            'timestamp_format': timestamp_format,
            'thread_pool': thread_pool,
        }

        return config_vars


def print_summary_info(logger, if_config_vars, agent_config_vars):
    # info to be sent to IF
    post_data_block = '\nIF settings:'
    for ik, iv in sorted(if_config_vars.items()):
        post_data_block += '\n\t{}: {}'.format(ik, iv)
    logger.debug(post_data_block)

    # variables from agent-specific config
    agent_data_block = '\nAgent settings:'
    for jk, jv in sorted(agent_config_vars.items()):
        agent_data_block += '\n\t{}: {}'.format(jk, jv)
    logger.debug(agent_data_block)


#########################
#   START_BOILERPLATE   #
#########################

def get_cli_config_vars():
    """ get CLI options. use of these options should be rare """
    usage = 'Usage: %prog [options]'
    parser = OptionParser(usage=usage)
    """
    """
    parser.add_option('-c', '--config', action='store', dest='config', default=abs_path_from_cur('conf.d'),
                      help='Path to the config files to use. Defaults to {}'.format(abs_path_from_cur('conf.d')))
    parser.add_option('-p', '--processes', action='store', dest='processes', default=multiprocessing.cpu_count() * 4,
                      help='Number of processes to run')
    parser.add_option('-q', '--quiet', action='store_true', dest='quiet', default=False,
                      help='Only display warning and error log messages')
    parser.add_option('-v', '--verbose', action='store_true', dest='verbose', default=False,
                      help='Enable verbose logging')
    parser.add_option('-t', '--testing', action='store_true', dest='testing', default=False,
                      help='Set to testing mode (do not send data).' +
                           ' Automatically turns on verbose logging')
    parser.add_option('--timeout', action='store', dest='timeout', default=5,
                      help='Minutes of timeout for all processes')
    (options, args) = parser.parse_args()

    config_vars = {
        'config': options.config if os.path.isdir(options.config) else abs_path_from_cur('conf.d'),
        'processes': int(options.processes),
        'testing': False,
        'log_level': logging.INFO,
        'timeout': int(options.timeout) * 60,
    }

    if options.testing:
        config_vars['testing'] = True

    if options.verbose:
        config_vars['log_level'] = logging.DEBUG
    elif options.quiet:
        config_vars['log_level'] = logging.WARNING

    return config_vars


def get_if_config_vars(logger, config_ini):
    """ get config.ini vars """
    if not os.path.exists(config_ini):
        logger.error('No config file found. Exiting...')
        return False
    with open(config_ini) as fp:
        config_parser = configparser.ConfigParser()
        config_parser.read_file(fp)
        try:
            user_name = config_parser.get('insightfinder', 'user_name')
            license_key = config_parser.get('insightfinder', 'license_key')
            token = config_parser.get('insightfinder', 'token')
            project_name = config_parser.get('insightfinder', 'project_name')
            project_type = config_parser.get('insightfinder', 'project_type').upper()
            sampling_interval = config_parser.get('insightfinder', 'sampling_interval')
            run_interval = config_parser.get('insightfinder', 'run_interval')
            chunk_size_kb = config_parser.get('insightfinder', 'chunk_size_kb')
            if_url = config_parser.get('insightfinder', 'if_url')
            if_http_proxy = config_parser.get('insightfinder', 'if_http_proxy')
            if_https_proxy = config_parser.get('insightfinder', 'if_https_proxy')
        except configparser.NoOptionError as cp_noe:
            logger.error(cp_noe)
            return config_error(logger)

        # check required variables
        if len(user_name) == 0:
            return config_error(logger, 'user_name')
        if len(license_key) == 0:
            return config_error(logger, 'license_key')
        if len(project_name) == 0:
            return config_error(logger, 'project_name')
        if len(project_type) == 0:
            return config_error(logger, 'project_type')

        if project_type not in {
            'METRIC',
            'METRICREPLAY',
            'LOG',
            'LOGREPLAY',
            'INCIDENT',
            'INCIDENTREPLAY',
            'ALERT',
            'ALERTREPLAY',
            'DEPLOYMENT',
            'DEPLOYMENTREPLAY'
        }:
            return config_error(logger, 'project_type')
        is_replay = 'REPLAY' in project_type

        if len(sampling_interval) == 0:
            if 'METRIC' in project_type:
                return config_error(logger, 'sampling_interval')
            else:
                # set default for non-metric
                sampling_interval = 10

        if sampling_interval.endswith('s'):
            sampling_interval = int(sampling_interval[:-1])
        else:
            sampling_interval = int(sampling_interval) * 60

        if len(run_interval) == 0:
            return config_error(logger, 'run_interval')

        if run_interval.endswith('s'):
            run_interval = int(run_interval[:-1])
        else:
            run_interval = int(run_interval) * 60

        # defaults
        if len(chunk_size_kb) == 0:
            chunk_size_kb = 2048  # 2MB chunks by default
        if len(if_url) == 0:
            if_url = 'https://app.insightfinder.com'

        # set IF proxies
        if_proxies = dict()
        if len(if_http_proxy) > 0:
            if_proxies['http'] = if_http_proxy
        if len(if_https_proxy) > 0:
            if_proxies['https'] = if_https_proxy

        config_vars = {
            'user_name': user_name,
            'license_key': license_key,
            'token': token,
            'project_name': project_name,
            'project_type': project_type,
            'sampling_interval': int(sampling_interval),  # as seconds
            'run_interval': int(run_interval),  # as seconds
            'chunk_size': int(chunk_size_kb) * 1024,  # as bytes
            'if_url': if_url,
            'if_proxies': if_proxies,
            'is_replay': is_replay
        }

        return config_vars


def abs_path_from_cur(filename=''):
    return os.path.abspath(os.path.join(__file__, os.pardir, filename))


def config_error(logger, setting=''):
    info = ' ({})'.format(setting) if setting else ''
    logger.error('Agent not correctly configured{}. Check config file.'.format(
        info))
    return False


def get_json_size_bytes(json_data):
    """ get size of json object in bytes """
    # return len(bytearray(json.dumps(json_data)))
    return getsizeof(json.dumps(json_data))


def make_safe_instance_string(instance, device=''):
    """ make a safe instance name string, concatenated with device if appropriate """
    # strip underscores
    instance = UNDERSCORE.sub('.', instance)
    instance = COLONS.sub('-', instance)
    # if there's a device, concatenate it to the instance with an underscore
    if device:
        instance = '{}_{}'.format(make_safe_instance_string(device), instance)
    return instance


def make_safe_metric_key(metric):
    """ make safe string already handles this """
    metric = LEFT_BRACE.sub('(', metric)
    metric = RIGHT_BRACE.sub(')', metric)
    metric = PERIOD.sub('/', metric)
    return metric


def make_safe_string(string):
    """
    Take a single string and return the same string with spaces, slashes,
    underscores, and non-alphanumeric characters subbed out.
    """
    string = SPACES.sub('-', string)
    string = SLASHES.sub('.', string)
    string = UNDERSCORE.sub('.', string)
    string = NON_ALNUM.sub('', string)
    return string


def format_command(cmd):
    if not isinstance(cmd, (list, tuple)):  # no sets, as order matters
        cmd = shlex.split(cmd)
    return list(cmd)


def initialize_data_gathering(logger, c_config, if_config_vars, agent_config_vars, time_now):
    metric_buffer = dict()
    track = dict()
    reset_metric_buffer(metric_buffer)
    reset_track(track)
    track['chunk_count'] = 0
    track['entry_count'] = 0

    # get cache
    (cache_con, cache_cur) = initialize_cache_connection()

    start_data_processing(logger, c_config, if_config_vars, agent_config_vars, metric_buffer, track, cache_con,
                          cache_cur, time_now)

    # clear metric buffer when data processing end
    clear_metric_buffer(logger, c_config, if_config_vars, metric_buffer, track)

    logger.info('Total chunks created: ' + str(track['chunk_count']))
    logger.info('Total {} entries: {}'.format(
        if_config_vars['project_type'].lower(), track['entry_count']))

    # close cache
    cache_con.close()
    return True


def clear_metric_buffer(logger, c_config, if_config_vars, metric_buffer, track):
    # move all buffer data to current data, and send
    buffer_values = list(metric_buffer['buffer_dict'].values())

    count = 0
    for row in buffer_values:
        # pop component map info
        component_map = row.pop('component_map')
        if component_map:
            track['component_map_list'].append(component_map)

        track['current_row'].append(row)
        count += 1
        if count % 100 == 0 or get_json_size_bytes(track['current_row']) >= if_config_vars['chunk_size']:
            logger.debug('Sending buffer chunk')
            send_data_wrapper(logger, c_config, if_config_vars, track)

    # last chunk
    if len(track['current_row']) > 0:
        logger.debug('Sending last chunk')
        send_data_wrapper(logger, c_config, if_config_vars, track)

    reset_metric_buffer(metric_buffer)


def reset_metric_buffer(metric_buffer):
    metric_buffer['buffer_key_list'] = []
    metric_buffer['buffer_ts_list'] = []
    metric_buffer['buffer_dict'] = {}

    metric_buffer['buffer_collected_list'] = []
    metric_buffer['buffer_collected_dict'] = {}


def reset_track(track):
    """ reset the track global for the next chunk """
    track['start_time'] = time.time()
    track['line_count'] = 0
    track['current_row'] = []
    track['component_map_list'] = []


################################
# Functions to send data to IF #
################################
def send_data_wrapper(logger, c_config, if_config_vars, track):
    """ wrapper to send data """
    logger.debug('--- Chunk creation time: {} seconds ---'.format(
        round(time.time() - track['start_time'], 2)))
    send_data_to_if(logger, c_config, if_config_vars, track, track['current_row'])
    track['chunk_count'] += 1
    reset_track(track)


def send_data_to_if(logger, c_config, if_config_vars, track, chunk_metric_data):
    send_data_time = time.time()

    # prepare data for metric streaming agent
    data_to_post = initialize_api_post_data(logger, if_config_vars)
    if 'DEPLOYMENT' in if_config_vars['project_type'] or 'INCIDENT' in if_config_vars['project_type']:
        for chunk in chunk_metric_data:
            chunk['data'] = json.dumps(chunk['data'])
    data_to_post[get_data_field_from_project_type(if_config_vars)] = json.dumps(chunk_metric_data)

    # add component mapping to the post data
    track['component_map_list'] = list({v['instanceName']: v for v in track['component_map_list']}.values())
    data_to_post['instanceMetaData'] = json.dumps(track['component_map_list'] or [])

    logger.debug('First:\n' + str(chunk_metric_data[0]))
    logger.debug('Last:\n' + str(chunk_metric_data[-1]))
    logger.info('Total Data (bytes): ' + str(get_json_size_bytes(data_to_post)))
    logger.info('Total Lines: ' + str(track['line_count']))

    # do not send if only testing
    if c_config['testing']:
        return

    # send the data
    post_url = urllib.parse.urljoin(if_config_vars['if_url'], get_api_from_project_type(if_config_vars))
    send_request(logger, post_url, 'POST', 'Could not send request to IF',
                 str(get_json_size_bytes(data_to_post)) + ' bytes of data are reported.',
                 data=data_to_post, verify=False, proxies=if_config_vars['if_proxies'])
    logger.info('--- Send data time: %s seconds ---' % round(time.time() - send_data_time, 2))


def send_request(logger, url, mode='GET', failure_message='Failure!', success_message='Success!',
                 **request_passthrough):
    """ sends a request to the given url """
    # determine if post or get (default)
    requests.packages.urllib3.disable_warnings()
    req = requests.get
    if mode.upper() == 'POST':
        req = requests.post

    req_num = 0
    for req_num in range(ATTEMPTS):
        try:
            response = req(url, **request_passthrough)
            if response.status_code == http.client.OK:
                return response
            else:
                logger.warn(failure_message)
                logger.info('Response Code: {}\nTEXT: {}'.format(
                    response.status_code, response.text))
        # handle various exceptions
        except requests.exceptions.Timeout:
            logger.exception('Timed out. Reattempting...')
            continue
        except requests.exceptions.TooManyRedirects:
            logger.exception('Too many redirects.')
            break
        except requests.exceptions.RequestException as e:
            logger.exception('Exception ' + str(e))
            break

    logger.error('Failed! Gave up after {} attempts.'.format(req_num + 1))
    return -1


def get_agent_type_from_project_type(if_config_vars):
    """ use project type to determine agent type """
    if 'METRIC' in if_config_vars['project_type']:
        if if_config_vars['is_replay']:
            return 'MetricFileReplay'
        else:
            return 'CUSTOM'
    elif if_config_vars['is_replay']:
        return 'LogFileReplay'
    else:
        return 'LogStreaming'
    # INCIDENT and DEPLOYMENT don't use this


def get_data_field_from_project_type(if_config_vars):
    """ use project type to determine which field to place data in """
    # incident uses a different API endpoint
    if 'INCIDENT' in if_config_vars['project_type']:
        return 'incidentData'
    elif 'DEPLOYMENT' in if_config_vars['project_type']:
        return 'deploymentData'
    else:  # MERTIC, LOG, ALERT
        return 'metricData'


def get_api_from_project_type(if_config_vars):
    """ use project type to determine which API to post to """
    # incident uses a different API endpoint
    if 'INCIDENT' in if_config_vars['project_type']:
        return 'incidentdatareceive'
    elif 'DEPLOYMENT' in if_config_vars['project_type']:
        return 'deploymentEventReceive'
    else:  # MERTIC, LOG, ALERT
        return 'customprojectrawdata'


def initialize_api_post_data(logger, if_config_vars):
    """ set up the unchanging portion of this """
    to_send_data_dict = dict()
    to_send_data_dict['userName'] = if_config_vars['user_name']
    to_send_data_dict['licenseKey'] = if_config_vars['license_key']
    to_send_data_dict['projectName'] = if_config_vars['project_name']
    to_send_data_dict['instanceName'] = HOSTNAME
    to_send_data_dict['agentType'] = get_agent_type_from_project_type(if_config_vars)
    if 'METRIC' in if_config_vars['project_type'] and 'sampling_interval' in if_config_vars:
        to_send_data_dict['samplingInterval'] = str(if_config_vars['sampling_interval'])
    logger.debug(to_send_data_dict)
    return to_send_data_dict


def listener_configurer():
    """ set up logging according to the defined log level """
    # create a logging format
    formatter = logging.Formatter(
        '{ts} [pid {pid}] {lvl} {mod}.{func}():{line} {msg}'.format(
            ts='%(asctime)s',
            pid='%(process)d',
            lvl='%(levelname)-8s',
            mod='%(module)s',
            func='%(funcName)s',
            line='%(lineno)d',
            msg='%(message)s'),
        ISO8601[0])

    # Get the root logger
    root = logging.getLogger()
    # No level or filter logic applied - just do it!
    # root.setLevel(level)

    # route INFO and DEBUG logging to stdout from stderr
    logging_handler_out = logging.StreamHandler(sys.stdout)
    logging_handler_out.setLevel(logging.DEBUG)
    logging_handler_out.setFormatter(formatter)
    root.addHandler(logging_handler_out)

    logging_handler_err = logging.StreamHandler(sys.stderr)
    logging_handler_err.setLevel(logging.WARNING)
    logging_handler_err.setFormatter(formatter)
    root.addHandler(logging_handler_err)


def listener_process(q, c_config):
    listener_configurer()
    while True:
        while not q.empty():
            record = q.get()

            if record.name == 'KILL':
                return

            logger = logging.getLogger(record.name)
            logger.handle(record)
        sleep(1)


def worker_configurer(q, level):
    h = QueueHandler(q)  # Just the one handler needed
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(level)


def worker_process(args):
    (config_file, c_config, time_now, q) = args

    # start sub process
    worker_configurer(q, c_config['log_level'])
    logger = logging.getLogger('worker')
    logger.info("Setup logger in PID {}".format(os.getpid()))
    logger.info("Process start with config: {}".format(config_file))

    if_config_vars = get_if_config_vars(logger, config_file)
    if not if_config_vars:
        return
    agent_config_vars = get_agent_config_vars(logger, config_file)
    if not agent_config_vars:
        return
    print_summary_info(logger, if_config_vars, agent_config_vars)

    # start run
    initialize_data_gathering(logger, c_config, if_config_vars, agent_config_vars, time_now)

    logger.info("Process is done with config: {}".format(config_file))
    return True


if __name__ == "__main__":

    # get config
    cli_config_vars = get_cli_config_vars()

    # logger
    m = multiprocessing.Manager()
    queue = m.Queue()
    listener = multiprocessing.Process(
        target=listener_process, args=(queue, cli_config_vars))
    listener.start()

    # set up main logger following example from work_process
    worker_configurer(queue, cli_config_vars['log_level'])
    main_logger = logging.getLogger('main')

    # variables from cli config
    cli_data_block = '\nCLI settings:'
    for kk, kv in sorted(cli_config_vars.items()):
        cli_data_block += '\n\t{}: {}'.format(kk, kv)
    main_logger.info(cli_data_block)

    # get all config files
    files_path = os.path.join(cli_config_vars['config'], "*.ini")
    config_files = glob.glob(files_path)

    # get args
    utc_time_now = int(arrow.utcnow().float_timestamp)
    arg_list = [(f, cli_config_vars, utc_time_now, queue) for f in config_files]

    # start sub process by pool
    pool = Pool(cli_config_vars['processes'])
    pool_result = pool.map_async(worker_process, arg_list)
    pool.close()

    # wait 5 minutes for every worker to finish
    pool_result.wait(timeout=cli_config_vars['timeout'])

    try:
        results = pool_result.get(timeout=1)
        pool.join()
    except TimeoutError:
        main_logger.error("We lacked patience and got a multiprocessing.TimeoutError")
        pool.terminate()

    # end
    main_logger.info("Now the pool is closed and no longer available")

    # send kill signal
    time.sleep(1)
    kill_logger = logging.getLogger('KILL')
    kill_logger.info('KILL')
    listener.join()
