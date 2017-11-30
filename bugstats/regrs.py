# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import base64
import datetime
import functools
from jinja2 import Environment, FileSystemLoader
import json
import os
import shutil
import requests
import re
import six
from dateutil.relativedelta import relativedelta
from libmozdata.bugzilla import Bugzilla
from libmozdata.socorro import ProductVersions
from libmozdata import utils, hgmozilla, gmail
from libmozdata.connection import Query
import tempfile

from pprint import pprint

def get_bz_params(v):
    # status_57: (fixed or verified)->affected OR (status_57 == (fixed or verified) AND bug_status == REOPENED)
    status = 'cf_status_firefox{}'.format(v)
    tracking = 'cf_tracking_firefox{}'.format(v)
    fields = ['id', 'status', 'resolution', 'summary', status, tracking]
    params = {'include_fields': fields,
              'j_top': 'OR',
              'f1': 'OP',
              'f2': 'OP',
              'j2': 'OR',
              'f3': status,
              'o3': 'changedfrom',
              'v3': 'fixed',
              'f4': status,
              'o4': 'changedfrom',
              'v4': 'verified',
              'f5': 'CP',
              'f6': status,
              'o6': 'changedto',
              'v6': 'affected',
              'f7': 'CP',
              'f8': 'OP',
              'f9': 'OP',
              'j9': 'OR',
              'f10': status,
              'o10': 'equals',
              'v10': 'fixed',
              'f11': status,
              'o11': 'equals',
              'v11': 'verified',
              'f12': 'CP',
              'f13': 'bug_status',
              'o13': 'equals',
              'v13': 'REOPENED',
              'f14': 'CP'}

    return params


def get_major(channel):
    allversions = ProductVersions.get_all_versions()
    allversions = allversions[channel]
    major = max(allversions.keys())
    return major


def history_handler(date, flag, history, data):
    bugid = int(history['id'])
    history = history['history']
    data[bugid] = False
    for changes in history:
        when = utils.get_date_ymd(changes['when'])
        if date is not None and when != date:
            continue
        for change in changes['changes']:
            if change['field_name'] != flag:
                continue
            added = change['added']
            removed = change['removed']
            if removed in ['verified', 'fixed'] and added in ['---', 'affected']:
                data[bugid] = True


def filter_bugs(data, hdata, status_flag, tracking_flag):
    for bugid, reg in hdata.items():
        if not reg:
            bug = data[bugid]
            status = bug['status']
            if status == 'REOPENED' and bug[status_flag] in ['verified', 'fixed']:
                hdata[bugid] = True


def check_bugs(bugids, treated):
    if treated:
        if os.path.isfile(treated):
            with open(treated, 'r') as In:
                data = json.load(In)
            newbugs = set(bugids) - set(data['treated'])
            if newbugs:
                data['treated'] += list(newbugs)
                with open(treated, 'w') as Out:
                    json.dump(data, Out)
            return newbugs
        else:
            with open(treated, 'w') as Out:
                data = {'treated': list(bugids)}
                json.dump(data, Out)
    return bugids

                
def get_links(major, date='today', treated=''):
    TIMEOUT = 240 # the search query can be long to evaluate
    tracking_flag = 'cf_tracking_firefox{}'.format(major)
    status_flag = 'cf_status_firefox{}'.format(major)
    date = utils.get_date_ymd(date) if date is not None else date

    def bug_handler(bug, data):
        data[bug['id']] = bug

    data = {}
    Bugzilla(get_bz_params(major),
             bughandler=bug_handler,
             bugdata=data,
             timeout=TIMEOUT).get_data().wait()

    bugids = list(data.keys())
    bugids = check_bugs(bugids, treated)
    hdata = {}
    Bugzilla(bugids=bugids,
             historyhandler=functools.partial(history_handler, date, status_flag),
             historydata=hdata).get_data().wait()

    filter_bugs(data, hdata, status_flag, tracking_flag)

    links = [(bugid, Bugzilla.get_links(bugid)) for bugid, reg in hdata.items() if reg]
    links = sorted(links, key=lambda p: p[0])
    
    return links


def send_email(emails=[], treated='', channel='nightly', version=None, date='today'):
    major = get_major(channel) if not version else int(version)
    links = get_links(major, date=None, treated=treated)
    if links:
        #date = utils.get_date(date)
        env = Environment(loader=FileSystemLoader('templates'))
        template = env.get_template('regrs_email')
        body = template.render(major=major,
                               channel=channel,
                               links=links)

        title = 'Bugs reopened in {} {}'.format(channel, major)
        body = body.encode('utf-8')
        if emails:
            gmail.send(emails, title, body, html=True)
        else:
            with open('/tmp/foo.html', 'w') as Out:
                Out.write(body)
            print('Title: %s' % title)
            print('Body:')
            print(body)
    else:
        print('No data for {}'.format(date))


if __name__ == '__main__':
    description = 'Get reopened bugs for a channel'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-c', '--channel', dest='channel', default='nightly')
    parser.add_argument('-v', '--version', dest='version', default=None)
    parser.add_argument('-t', '--treated', dest='treated', default='')
    parser.add_argument('-e', '--email', dest='emails',
                        action='store', nargs='+',
                        default=[], help='emails')
    args = parser.parse_args()
    send_email(emails=args.emails, treated=args.treated, channel=args.channel, version=args.version)
