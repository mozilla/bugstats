# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import base64
import unicodecsv as csv
import datetime
import functools
import icalendar
from jinja2 import Environment, FileSystemLoader
import shutil
import requests
import re
import six
try:
    from urllib.request import urlopen
except ImportError:
    from urllib import urlopen
import whatthepatch
from dateutil.relativedelta import relativedelta
from libmozdata.bugzilla import Bugzilla
from libmozdata.socorro import ProductVersions
from libmozdata import utils, hgmozilla
from libmozdata.connection import Query
import tempfile
from . import config
from . import mail


NIGHTLY_PAT = Bugzilla.get_landing_patterns(channels=['nightly'])
PAR_PAT = re.compile('\([^\)]*\)')
BRA_PAT = re.compile('\[[^\]]*\]')
DIA_PAT = re.compile('<[^>]*>')
UTC_PAT = re.compile('UTC\+[^ \t]*')
COL_PAT = re.compile(':[^:]*')
SOFTVISION_PAT = re.compile('[^@]+@softvision')
MOZREVIEW_URL_PAT = 'https://reviewboard.mozilla.org/r/([0-9]+)/'

PATCH_INFO = {'changes_size': 0,
              'test_changes_size': 0,
              'changes_add': 0,
              'changes_del': 0}


# This code is used to help release managers during the code freeze week


def get_bz_params(v, date):
    end_date = utils.get_date(date, -1)
    status = ['cf_status_firefox{}'.format(v - i) for i in range(3)]
    tracking = 'cf_tracking_firefox{}'.format(v)
    fields = ['id', 'product', 'component', 'assigned_to',
              'assigned_to_detail', 'status', 'resolution', 'summary',
              'priority', 'severity', 'keywords',
              'cf_qa_whiteboard', 'cf_crash_signature']
    fields += status
    fields += [tracking]
    params = {'include_fields': fields,
              'f1': status[0],
              'o1': 'changedafter',
              'v1': date,
              'f2': status[0],
              'o2': 'changedbefore',
              'v2': end_date,
              'f3': status[0],
              'o3': 'changedto',
              'v3': 'fixed',
              'f4': 'resolution',
              'o4': 'equals',
              'v4': 'FIXED'}

    return params


def get_start_date(date):
    if isinstance(date, six.string_types):
        date = utils.get_date_ymd(date)

    ics = requests.get('https://calendar.google.com/calendar/ical/mozilla.com_dbq84anr9i8tcnmhabatstv5co%40group.calendar.google.com/public/basic.ics') # NOQA
    cal = icalendar.Calendar.from_ical(ics.text)
    dates = [ev['DTSTART'].dt for ev in cal.walk() if 'SUMMARY' in ev and 'Beta->Release' in ev['SUMMARY']]
    iso_date = date.isocalendar()
    for d in dates:
        isod = d.isocalendar()
        if isod[0] == iso_date[0] and isod[1] == iso_date[1]:
            # date hass in the same year and week as d
            return d.strftime('%Y-%m-%d')


def get_major():
    allversions = ProductVersions.get_all_versions()
    allversions = allversions['nightly']
    major = max(allversions.keys())
    return major


def decompose(comp):
    if ':' in comp:
        i = comp.index(':')
        return comp[:i], comp[(i + 1):]
    else:
        return comp, ''


def is_qf_p1(whiteboard):
    for tok in whiteboard.split(','):
        tok = tok.strip().replace(' ', '')
        if tok == '[qf:p1]':
            return True
    return False


def bug_handler(bug, data):
    if bug['product'] in config.get_products_blacklist():
        return
    if bug['component'] in config.get_components_blacklist():
        return

    bugid = bug['id']
    del bug['id']
    data[bugid] = bug
    bug['comp_first'], bug['comp_second'] = decompose(bug['component'])
    bug['land'] = {}
    assigned_to = bug.get('assigned_to', '')
    if assigned_to:
        name = bug.get('assigned_to_detail', {}).get('real_name', '')
        if name:
            bug['assigned_to'] = name
        else:
            bug['assigned_to'] = assigned_to
    else:
        bug['assigned_to'] = ''

    if 'cf_crash_signature' in bug:
        ccs = bug['cf_crash_signature']
        del bug['cf_crash_signature']
    else:
        ccs = ''
    bug['isacrash'] = ccs != ''
    bug['quantum'] = is_qf_p1(bug['cf_qa_whiteboard'])
    del bug['cf_qa_whiteboard']


def comment_handler(bug, bugid, data):
    r = Bugzilla.get_landing_comments(bug['comments'], [], NIGHTLY_PAT)
    d = {}
    for i in r:
        revision = i['revision']
        d[revision] = {'date': None, 'backedout': False, 'bugid': bugid}

    data[int(bugid)]['land'] = d


def history_handler(flag, date, invalids, history, data):
    bugid = int(history['id'])
    history = history['history']
    data[bugid]['softvision'] = False
    valid = False
    if history:
        for changes in history:
            for change in changes['changes']:
                if change['removed'] == 'RESOLVED' and change['added'] == 'VERIFIED':
                    who = changes['who']
                    m = SOFTVISION_PAT.search(who)
                    if m:
                        data[bugid]['softvision'] = True
                if change['field_name'] == flag and change['added'] == 'fixed':
                    when = utils.get_date_ymd(changes['when'])
                    tomorrow = date + relativedelta(days=1)
                    valid = date <= when < tomorrow

    if not valid:
        invalids.append(bugid)


def patch_analysis(patch):
    info = PATCH_INFO.copy()

    def _is_test(path):
        term = ('ini', 'list', 'in', 'py', 'json', 'manifest')
        return 'test' in path and not path.endswith(term)

    for diff in whatthepatch.parse_patch(patch):
        if diff.header and diff.changes:
            h = diff.header
            # old_path = h.old_path[2:] if h.old_path.startswith('a/') else h.old_path
            new_path = h.new_path[2:] if h.new_path.startswith('b/') else h.new_path

            # Calc changes additions & deletions
            counts = [(
                old is None and new is not None,
                new is None and old is not None
            ) for old, new, _ in diff.changes]
            counts = list(zip(*counts))  # inverse zip
            info['changes_add'] += sum(counts[0])
            info['changes_del'] += sum(counts[1])

            # TODO: Split C/C++, Rust, Java, JavaScript, build system changes
            if _is_test(new_path):
                info['test_changes_size'] += len(diff.changes)
            else:
                info['changes_size'] += len(diff.changes)

    return info


def attachment_handler(attachments, bugid, data):
    info = {}
    for attachment in attachments:
        if sum(flag['name'] == 'review' and flag['status'] == '+' for flag in attachment['flags']) == 0:
            continue

        patch_data = None

        if attachment['is_patch'] == 1 and attachment['is_obsolete'] == 0:
            patch_data = base64.b64decode(attachment['data']).decode('ascii', 'ignore')
        elif attachment['content_type'] == 'text/x-review-board-request' and attachment['is_obsolete'] == 0:
            mozreview_url = base64.b64decode(attachment['data']).decode('utf-8')
            review_num = re.search(MOZREVIEW_URL_PAT, mozreview_url).group(1)
            mozreview_raw_diff_url = 'https://reviewboard.mozilla.org/r/' + review_num + '/diff/raw/'

            response = urlopen(mozreview_raw_diff_url)
            patch_data = response.read().decode('ascii', 'ignore')

        if patch_data is not None:
            i = patch_analysis(patch_data)
            info[attachment['id']] = i

    new_info = {}
    data[int(bugid)]['patches'] = new_info
    for k in PATCH_INFO.keys():
        new_info[k] = sum(v[k] for v in info.values())


def get_hg(bugs):
    url = hgmozilla.Revision.get_url('nightly')
    queries = []
    bug_pattern = re.compile('[\t ]*[Bb][Uu][Gg][\t ]*([0-9]+)')
    backout_pattern = re.compile('^back(ed)? ?out', re.I)

    def handler_rev(json, data):
        push = json['pushdate'][0]
        push = datetime.datetime.utcfromtimestamp(push)
        push = utils.as_utc(push)
        data['date'] = utils.get_date_str(push)
        data['backedout'] = json.get('backedoutby', '') != ''
        if not data['backedout']:
            m = backout_pattern.search(json['desc'])
            if m:
                data['backedout'] = True
        m = bug_pattern.search(json['desc'])
        if not m or m.group(1) != data['bugid']:
            data['bugid'] = ''

    for info in bugs.values():
        for rev, i in info['land'].items():
            queries.append(Query(url, {'node': rev}, handler_rev, i))

    if queries:
        hgmozilla.Revision(queries=queries).wait()

    for info in bugs.values():
        info['landed_patches'] = [v['backedout'] for v in info['land'].values()].count(False)


def display_list(l):
    if isinstance(l, list):
        return ','.join(l)
    return l


def get_better_name(name):
    def repl(m):
        if m.start(0) == 0:
            return m.group(0)
        return ''

    if name.startswith('Nobody;'):
        s = 'Nobody'
    else:
        s = PAR_PAT.sub('', name)
        s = BRA_PAT.sub('', s)
        s = DIA_PAT.sub('', s)
        s = COL_PAT.sub(repl, s)
        s = UTC_PAT.sub('', s)
        s = s.strip()
        if s.startswith(':'):
            s = s[1:]
    return s.encode('utf-8').decode('utf-8')


def prepare(major, bugs):
    def sort(p):
        info = p[1]
        return (info['product'], info['component'],
                -info['landed_patches'], -info['patches']['changes_size'],
                -info['patches']['test_changes_size'], -p[0])

    data = []
    for bugid, info in sorted(bugs.items(), key=sort):
        d = {'bug': {'id': bugid,
                     'link': Bugzilla.get_links(bugid),
                     'summary': info['summary']},
             'product': info['product'],
             'component': info['component'],
             'assignee': get_better_name(info['assigned_to']),
             'patches': info['landed_patches'],
             'addlines': info['patches']['changes_add'],
             'rmlines': info['patches']['changes_del'],
             'size': info['patches']['changes_size'],
             'test_size': info['patches']['test_changes_size'],
             'priority': info['priority'],
             'severity': info['severity'],
             'tracking': info['cf_tracking_firefox{}'.format(major)],
             'status': {major - 2: info['cf_status_firefox{}'.format(major - 2)],
                        major - 1: info['cf_status_firefox{}'.format(major - 1)],
                        major: info['cf_status_firefox{}'.format(major)]},
             'qaverified': 'Yes' if info['softvision'] else 'No',
             'crash': 'Yes' if info['isacrash'] else 'No',
             'quantum': 'Yes' if info['quantum'] else 'No',
             'keywords': display_list(info['keywords'])}

        data.append(d)

    return data


def make_csv(date, major, bugs):
    directory = tempfile.mkdtemp()
    name = '{}/nightly_bugs_{}.csv'.format(directory, date)
    with open(name, 'wb') as In:
        w = csv.writer(In, delimiter=',')
        w.writerow(['Bug', 'Product', 'Component', 'Assignee', '# of patches', 'Added lines', 'Removed lines', 'Changes size', 'Tests size', 'Priority', 'Severity', 'Tracking {}'.format(major), 'Status {}'.format(major - 2), 'Status {}'.format(major - 1), 'Status {}'.format(major), 'SV', 'QF', 'Crash', 'Keywords'])
        for d in bugs:
            w.writerow([d['bug']['id'], d['product'], d['component'],
                        d['assignee'], d['patches'], d['addlines'],
                        d['rmlines'], d['size'], d['test_size'],
                        d['priority'], d['severity'], d['tracking'],
                        d['status'][major - 2], d['status'][major - 1],
                        d['status'][major], d['qaverified'], d['quantum'],
                        d['crash'], d['keywords']])
    return name, directory


def get_bugs(date, major, date_range):
    if major == -1:
        major = get_major()
    date = utils.get_date_ymd(date)
    if not date_range:
        start_date = get_start_date(date)
        start_date = utils.get_date_ymd(start_date)
        end_date = start_date + relativedelta(days=6)
    else:
        dates = date_range.split('|')
        dates = map(lambda x: utils.get_date_ymd(x.strip(' ')), dates)
        start_date, end_date = tuple(dates)

    if start_date <= date <= end_date:
        sdate = utils.get_date_str(date)
        data = {}
        Bugzilla(get_bz_params(major, sdate),
                 bughandler=bug_handler,
                 bugdata=data).get_data().wait()
        flag = 'cf_status_firefox{}'.format(major)

        bugids = list(data.keys())
        invalids = []
        Bugzilla(bugids=bugids,
                 commenthandler=comment_handler,
                 commentdata=data,
                 historyhandler=functools.partial(history_handler, flag, date, invalids),
                 historydata=data,
                 attachmenthandler=attachment_handler,
                 attachmentdata=data,
                 attachment_include_fields=['id', 'data', 'is_obsolete', 'creation_time', 'flags', 'is_patch', 'content_type'],
                 comment_include_fields=['text']).get_data().wait()

        for invalid in invalids:
            del data[invalid]

        get_hg(data)
        return major, prepare(major, data)

    return major, []


def send_email(emails=[], date='today', major=-1, date_range=''):
    major, data = get_bugs(date, major, date_range)
    if data:
        date = utils.get_date(date)
        env = Environment(loader=FileSystemLoader('templates'))
        template = env.get_template('cfw_email')
        body = template.render(major=major,
                               date=date,
                               data=data,
                               enumerate=enumerate)

        title = 'Bugs fixed in nightly {} the {}'.format(major, date)
        body = body.encode('utf-8')
        if emails:
            f, d = make_csv(date, major, data)
            mail.send(emails, title, body, html=True, files=[f])
            shutil.rmtree(d)
        else:
            with open('/tmp/foo.html', 'w') as Out:
                Out.write(body)
            print('Title: %s' % title)
            print('Body:')
            #print(body)
    else:
        print('No data for {}'.format(date))


if __name__ == '__main__':
    description = 'Get bug stats for code freeze week'
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('-e', '--email', dest='emails',
                        action='store', nargs='+',
                        default=[], help='emails')
    parser.add_argument('-d', '--date', dest='date',
                        action='store', default='today', help='date')
    parser.add_argument('-m', '--major', dest='major', type=int,
                        action='store', default=-1, help='Major version of nightly')
    parser.add_argument('-r', '--range', dest='range',
                        action='store', default='', help='Date range XXXX-XX-XX|XXXX-XX-XX')
    args = parser.parse_args()
    send_email(emails=args.emails, date=args.date, major=args.major, date_range=args.range)


# wget "https://docs.google.com/spreadsheets/d/1Rn-F3Kg_1_VznIxxXkAGGL8mVMSAdamZZI4f1O2r8HA/gviz/tq?tqx=out:csv&sheet=in%2055"
