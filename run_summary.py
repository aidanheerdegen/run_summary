#!/usr/bin/env python
"""

Tools to summarise access-om2 runs.

Latest version: https://github.com/aekiss/run_summary
Author: Andrew Kiss https://github.com/aekiss
Apache 2.0 License http://www.apache.org/licenses/LICENSE-2.0.txt
"""

# TODO: collect data on storage use on hh5 (and short?) with du -bs
# TODO: use starting date/time for determining git commit
# TODO: use PAYU_N_RUNS - does this tell you whether the run is part of a sequence? if so we can determine queue wait for runs in a sequence - but sometimes it is None
# TODO: summary stats: specify list of excel commands e.g. ['sum', 'average', 'min', 'max'] as optional third tuple element in output_format and insert formulas for these. Or just calculate them in python? might as well, as Execel will save formulas as values if formulas aren't displayed...

from __future__ import print_function
import sys
try:
    assert sys.version_info >= (3, 3)  # need python >= 3.3 for print flush keyword
except AssertionError:
    print('\nFatal error: Python version too old.')
    print('On NCI, do the following and try again:')
    print('   module use /g/data3/hh5/public/modules; module load conda/analysis3\n')
    raise

import os
import glob  # BUG: fails if payu module loaded - some sort of module clash with re
import subprocess
import datetime
import dateutil.parser
from collections import OrderedDict
import csv
import copy

try:
    import yaml
    import f90nml  # from https://f90nml.readthedocs.io/en/latest/
except ImportError:  # BUG: don't get this exception if payu module loaded, even if on python 2.6.6
    print('\nFatal error: modules not available.')
    print('On NCI, do the following and try again:')
    print('   module use /g/data3/hh5/public/modules; module load conda/analysis3\n')
    raise
import nmltab  # from https://github.com/aekiss/nmltab

def get_sync_path(fname):
    """
    Return GDATADIR path from sync_output_to_gdata.sh.

    fname: sync_output_to_gdata.sh file path

    output: dict

    """
    with open(fname, 'r') as infile:
        for line in infile:
            # NB: subsequent matches will replace earlier ones
            try:
                dir = line.split('GDATADIR=')[1].strip().rstrip('/')
            except IndexError:  # 'GDATADIR=' not found - keep looking
                continue
    return dir


def parse_pbs_log(fname):
    """
    Return dict of items from parsed PBS log file.

    fname: PBS log file path

    output: dict

    example of PBS log file content to parse:
        qsub -q normal -P g40 -l walltime=12600 -l ncpus=2064 -l mem=8256GB -N minimal_01deg_j -l wd -j n -v PAYU_MODULENAME=payu/dev,PYTHONPATH=/projects/access/apps/mnctools/0.1/lib:/projects/access/apps/mnctools/0.1/lib:/projects/access/apps/mnctools/0.1/lib:/projects/v45/apps/payu/dev/lib:/projects/access/apps/mnctools/0.1/lib:/projects/v45/python,PAYU_CURRENT_RUN=137,PAYU_MODULEPATH=/projects/v45/modules,PAYU_N_RUNS=10 -lother=hyperthread -W umask=027 /projects/v45/apps/payu/dev/bin/payu-run
    ...
        git commit -am "2018-10-08 22:32:26: Run 137"
        TODO: Check if commit is unchanged
        ======================================================================================
                          Resource Usage on 2018-10-08 22:32:36:
           Job Id:             949753.r-man2
           Project:            x77
           Exit Status:        0
           Service Units:      20440.40
           NCPUs Requested:    5968                   NCPUs Used: 5968
                                                   CPU Time Used: 20196:31:07
           Memory Requested:   11.66TB               Memory Used: 2.61TB
           Walltime requested: 05:00:00            Walltime Used: 03:25:30
           JobFS requested:    36.43GB                JobFS used: 1.0KB
        ======================================================================================

    """
    def getproject(l):
        return l[1]

    def getpayuversion(l):
        return os.path.dirname(os.path.dirname(l[-1]))
        # return os.path.dirname([s for s in l[0].split(',')[0].split(':')
        #                         if s.find('payu') > -1][0])

    def getpayu(l):
        return l[0].split(',')[0]

    def getpayuint(l):
        return int(l[0].split(',')[0])

    def getrun(l):
        return int(l[4].rstrip('"'))

    def getjob(l):
        return int(l[1].split('.')[0])

    def getint(l):
        return int(l[1])

    def getfloat(l):
        return float(l[1])

    def getsec(l):  # convert hh:mm:ss to sec
        return sum(x * int(t) for x, t in zip([3600, 60, 1], l[1].split(':')))

    def getdatetime(l):  # BUG: doesn't include time zone (can't tell if we're on daylight savings time)
        return l[0]+'T'+l[1].rstrip(':')

    def getbytes(l):  # assumes PBS log info uses binary prefixes - TODO: check
        s = l[1]
        ns = s.strip('BKMGT')  # numerical part
        units = {'B': 1,
                 'KB': 2**10,
                 'MB': 2**20,
                 'GB': 2**30,
                 'TB': 2**40}
        return int(round(float(ns)*units[s[len(ns):]]))

    search_items = {  # keys are strings to search for; items are functions to apply to whitespace-delimited list of strings following key
        'PAYU_CURRENT_RUN': getpayuversion,  # gets path to payu; PAYU_CURRENT_RUN is redundant as this is obtained below from git commit message
        # 'PAYU_CURRENT_RUN=': getpayuint,  # BUG: misses some runs
        'PAYU_MODULENAME=': getpayu,
        'PAYU_MODULEPATH=': getpayu,
        'PAYU_PATH=': getpayu,
        'LD_LIBRARY_PATH=': getpayu,
        'PAYU_N_RUNS=': getpayuint,
        'PYTHONPATH=': getpayu,
# BUG: git commit will be missing if runlog: False in config.yaml - so we won't get run number!
        'git commit': getrun,  # instead of using PAYU_CURRENT_RUN; NB: run with this number might have failed - check Exit Status
        'Resource Usage on': getdatetime,
        'Job Id': getjob,
        'Project': getproject,
        'Exit Status': getint,
        'Service Units': getfloat,
        'NCPUs Requested': getint,
        'NCPUs Used': getint,
        'CPU Time Used': getsec,
        'Memory Requested': getbytes,
        'Memory Used': getbytes,
        'Walltime requested': getsec,
        'Walltime Used': getsec,
        'JobFS requested': getbytes,
        'JobFS used': getbytes}
    parsed_items = search_items.fromkeys(search_items, None)  # set defaults to None

    with open(fname, 'r') as infile:
        for line in infile:
            # NB: subsequent matches will replace earlier ones
            # NB: processes only the first match of each line
            for key, op in search_items.items():
                try:
                    parsed_items[key] = op(line.split(key)[1].split())
                except IndexError:  # key not present in this line
                    continue

    # change to more self-explanatory keys
    rename_keys = {'PAYU_CURRENT_RUN': 'payu version',
                   # 'PAYU_CURRENT_RUN=': 'Run number',
                   'git commit': 'Run number',
                   'Memory Requested': 'Memory Requested (bytes)',
                   'Memory Used': 'Memory Used (bytes)',
                   'Walltime requested': 'Walltime Requested (s)',
                   'Walltime Used': 'Walltime Used (s)',
                   'Resource Usage on': 'Run completion date'}
    for oldkey, newkey in rename_keys.items():
        parsed_items[newkey] = parsed_items.pop(oldkey)

    if parsed_items['Memory Requested (bytes)'] is None:
        parsed_items['Memory Requested (Gb)'] = None
    else:
        parsed_items['Memory Requested (Gb)'] = parsed_items['Memory Requested (bytes)']/2**30

    if parsed_items['Memory Used (bytes)'] is None:
        parsed_items['Memory Used (Gb)'] = None
    else:
        parsed_items['Memory Used (Gb)'] = parsed_items['Memory Used (bytes)']/2**30

    if parsed_items['Walltime Requested (s)'] is None:
        parsed_items['Walltime Requested (hr)'] = None
    else:
        parsed_items['Walltime Requested (hr)'] = parsed_items['Walltime Requested (s)']/3600

    if parsed_items['Walltime Used (s)'] is None:
        parsed_items['Walltime Used (hr)'] = None
    else:
        parsed_items['Walltime Used (hr)'] = parsed_items['Walltime Used (s)']/3600

    try:
        parsed_items['Timeout'] = parsed_items['Walltime Used (s)'] > parsed_items['Walltime Requested (s)']
    except:
        parsed_items['Timeout'] = None

    return parsed_items


def parse_git_log(basepath, datestr):
    """
    Return dict of items from git log from most recent commit before a given date.

    basepath: base directory path string

    datestr: date string

    output: dict
    """
    # possible BUG: what time zone flag should be use? local is problematic if run from overseas....?
    # use Popen for backwards-compatiblity with Python <2.7
    # pretty format is tab-delimited (%x09)
    try:
        p = subprocess.Popen('cd ' + basepath
                             + ' && git log -1 '
                             + '--pretty="format:%H%x09%an%x09%ai%x09%B" '
                             + '`git rev-list -1 --date=local --before="'
                             + datestr + '" HEAD`',  # TODO: add 1 sec to datestr so we don't rely on the delay between git commit and PBS log?
                             stdout=subprocess.PIPE, shell=True)
        log = p.communicate()[0].decode('ascii').split('\t')
        # log = p.communicate()[0].decode('ascii').encode('ascii').split('\t')  # for python 2.6
        log[3] = log[3].strip()  # strip whitespace from message
    except:
        log = [None]*4  # default values in case there's no .git, e.g. if runlog: False in config.yaml
    parsed_items = dict()
    parsed_items['Commit'] = log[0]
    parsed_items['Author'] = log[1]
    parsed_items['Date'] = log[2]
    parsed_items['Message'] = log[3]
    return parsed_items


def parse_mom_time_stamp(paths):
    """
    Return dict of items from parsed MOM time_stamp.out.

    paths: list of base paths

    output: dict parsed from first matching time_stamp.out in paths

    example of MOM time_stamp.out content to parse:
        2001   9   1   0   0   0  Sep
        2001  11   1   0   0   0  Nov

    """
    parsed_items = dict()
    keys = ['Model start time', 'Model end time']
    for path in paths:
        fname = os.path.join(path, 'ocean/time_stamp.out')
        if os.path.isfile(fname):
            parsed_items['Time stamp file'] = fname
            with open(fname, 'r') as infile:
                for key in keys:
                    line = infile.readline()
                    parsed_items[key] = datetime.datetime(
                        *list(map(int, line.split()[0:-1]))).isoformat()
            break
    try:
        d1 = dateutil.parser.parse(parsed_items[keys[0]])
        d2 = dateutil.parser.parse(parsed_items[keys[1]])
        len = d2-d1  # BUG: presumably assumes Gregorian calendar with leap years and time in UTC
        parsed_items['Model run length (s)'] = len.total_seconds()
        parsed_items['Model run length (days)'] = len.total_seconds()/3600/24
    except KeyError:
        pass
    return parsed_items


def parse_config_yaml(paths):
    """
    Return dict of items from parsed config.yaml.

    paths: list of base paths

    output: dict parsed from first matching config.yaml in paths
    """
    parsed_items = dict()
    for path in paths:
        fname = os.path.join(path, 'config.yaml')
        if os.path.isfile(fname):
            with open(fname, 'r') as infile:
                parsed_items = yaml.load(infile, Loader=yaml.FullLoader)
            break
    return parsed_items


def num(s):
    """
    Return input string as int or float if possible, otherwise return string.
    """
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def parse_accessom2_out(paths):
    """
    Return dict of items from parsed access-om2.out.

    paths: list of base paths

    output: dict of timing names, with dict of statistics

    NB: output may also contain bad data from intermingled CICE output.
    """
    parsed_items = dict()
    for path in paths:
        fname = os.path.join(path, 'access-om2.out')
        if os.path.isfile(fname):
            with open(fname, 'r') as infile:
                for l in infile:
                    if l.startswith('Tabulating mpp_clock statistics'):
                        break
                for l in infile:
                    if l.startswith('                                          tmin'):
                        break
                keys = l.split()
                for l in infile:
                    if l.startswith(' MPP_STACK high water mark='):
                        break
                    name = l[0:32].strip()  # relies on name being cropped at 32 chars
                    vals = [num(n) for n in l[32:].split()]
                    parsed_items[name] = dict(zip(keys, vals))
            break
    return parsed_items


def parse_cice_timing(paths):
    """
    Return dict of cice timing info from ice/ice_diag.d.

    paths: list of base paths

    output: dict of timing names, with dict of statistics
    """
#     sample to parse:
# Timing information:
# 
# Timer   1:     Total   10894.88 seconds
#   Timer stats (node): min =    10894.69 seconds
#                       max =    10894.88 seconds
#                       mean=    10894.70 seconds
#   Timer stats(block): min =        0.00 seconds
#                       max =        0.00 seconds
#                       mean=        0.00 seconds
# Timer   2:  TimeLoop   10802.50 seconds
#   Timer stats (node): min =    10802.33 seconds
#                       max =    10802.50 seconds
#                       mean=    10802.33 seconds
#   Timer stats(block): min =        0.00 seconds
#                       max =        0.00 seconds
#                       mean=        0.00 seconds

    parsed_items = dict()
    for path in paths:
        fname = os.path.join(path, 'ice/ice_diag.d')
        if os.path.isfile(fname):
            with open(fname, 'r') as infile:
                for l in infile:
                    if l.startswith('Timing information:'):
                        break
                for l in infile:
                    if l.startswith('Timer'):  # ignore time is it it node max
                        timerkey = ' '.join(l[0:21].split()[2:])
                        parsed_items[timerkey] = dict()
                    else:
                        if l.startswith('  Timer'):
                            typekey = l.split('(')[-1].split(')')[0]
                            parsed_items[timerkey][typekey] = dict()
                        try:
                            key = l.split('=')[0].split()[-1]
                            val = num(l.split()[-2])
                            parsed_items[timerkey][typekey][key] = val
                        except:
                            pass
            break
    return parsed_items


def parse_nml(paths):
    """
    Return dict of items from parsed namelists.

    paths: list of base paths to parse for namelists

    output: dict
    """
    parsed_items = dict()
    parsed_items['accessom2.nml'] = None  # default value for non-YATM run
    for path in paths:
        fnames = [os.path.join(path, 'accessom2.nml')]\
                + glob.glob(os.path.join(path, '*/*.nml'))
        for fname in fnames:
            if os.path.isfile(fname):  # no accessom2.nml for non-YATM run
                parsed_items[fname.split(path)[1].strip('/')] \
                        = f90nml.read(fname)
    return parsed_items


def git_diff(basepath, sha1, sha2):
    """
    Return dict of git-tracked differences between two commits.

    basepath: base directory path string

    sha1, sha2: strings; sha1 should be earlier than or same as sha2
    """
    try:
        p = subprocess.Popen('cd ' + basepath
                             + ' && git diff --name-only ' + sha1 + ' ' + sha2,
                             stdout=subprocess.PIPE, shell=True)
        c = ', '.join(
            p.communicate()[0].decode('ascii').split())
        p = subprocess.Popen('cd ' + basepath
                             + ' && git log --ancestry-path --pretty="%B\%x09" '
                             + sha1 + '..' + sha2,
                             stdout=subprocess.PIPE, shell=True)
        m = [s.strip('\n\\') 
             for s in p.communicate()[0].decode('ascii').split('\t')][0:-1]
        m.reverse()  # put in chronological order
        if len(m) == 0:
            m = None
    except:
        c = None
        m = None
    parsed_items = dict()
    parsed_items['Changed files'] = c
    parsed_items['Messages'] = m  # NB: will be None if there's no direct ancestry path from sha1 to sha2)
    return parsed_items


def dictget(d, l):
    """
    Lookup item in nested dict using a list of keys, or None if non-existent

    d: nested dict
    l: list of keys, or None
    """
    try:
        dl0 = d[l[0]]
    except (KeyError, TypeError):
        return None
    if len(l) == 1:
        return dl0
    return dictget(dl0, l[1:])


def keylists(d):
    """
    Return list of key lists to every leaf node in a nested dict.
    Each key list can be used as an argument to dictget.

    d: nested dict
    """
    l = []
    for k, v in d.items():
        if isinstance(v, dict):
            sublists = keylists(v)
            for sli in sublists:
                l.append([k]+sli)
        else:
            l.append([k])
    return l


def recursive_superset(d):
    """
    Return dict of groups and variables present in any of the input Namelists.

    Intended design:
    Input is a dict of dicts
    Output is a dict whose keys are a supserset of the keys in the input dict's sub-dicts.
    Output values are also supersets if they are dicts

    TODO: finish! Currently just returns one of the sub-dicts

    Parameters
    ----------
    d : dict

    Returns
    -------
    dict

    """
    # copied from nmltab:
    # # if len(nmlall) == 1:  # just do a deep copy of the only value
    # #     nmlsuperset = copy.deepcopy(nmlall[list(nmlall.keys())[0]])
    # # else:
    # nmlsuperset = {}
    # for nml in nmlall:
    #     nmlsuperset.update(nmlall[nml])
    # # nmlsuperset now contains all groups that were in any nml
    # for group in nmlsuperset:
    #     # to avoid the next bit changing the original groups
    #     nmlsuperset[group] = nmlsuperset[group].copy()
    #     # if isinstance(nmlallsuperset[group], list):
    #     #     for gr in nmlall[nml][group]:
    #     #         nmlsuperset[group].update(gr)
    #     for nml in nmlall:
    #         if group in nmlall[nml]:
    #             nmlsuperset[group].update(nmlall[nml][group])
    # # nmlsuperset groups now contain all keys that were in any nml
    # return nmlsuperset
    for v in d.values():
        return v  # dummy for testing


def run_summary(basepath=os.getcwd(), outfile=None, list_available=False,
                dump_all=False):
    """
    Generate run summary
    """
    print('Reading run data from ' + basepath, end='')

    # get jobname from config.yaml -- NB: we assume this is the same for all jobs
    with open(os.path.join(basepath, 'config.yaml'), 'r') as infile:
        jobname = yaml.load(infile, Loader=yaml.FullLoader)['jobname']

    sync_path = get_sync_path(os.path.join(basepath, 'sync_output_to_gdata.sh'))
    if outfile is None:
        outfile = 'run_summary_' + os.path.split(sync_path)[1] + '.csv'

    try:
        p = subprocess.Popen('cd ' + basepath
                             + ' && git rev-parse --abbrev-ref HEAD',
                             stdout=subprocess.PIPE, shell=True)
        git_branch = p.communicate()[0].decode('ascii').strip()
    except:
        git_branch = None

    # get data from all PBS job logs
    run_data = dict()
    # NB: match jobname[:15] because in some cases the pbs log files use a shortened version of the jobname in config.yaml
    # e.g. see /home/157/amh157/payu/025deg_jra55_ryf8485
    # NB: logs in archive may be duplicated in sync_path, in which case the latter is used
    logfiles = glob.glob(os.path.join(basepath, 'archive/pbs_logs', jobname[:15] + '*.o*'))\
             + glob.glob(os.path.join(basepath, jobname[:15] + '*.o*'))\
             + glob.glob(os.path.join(sync_path, 'pbs_logs', jobname[:15] + '*.o*'))
    logfiles = [f for f in logfiles if '_c.o' not in f]  # exclude collation files *_c.o*
    for f in logfiles:
        print('.', end='', flush=True)
        jobid = int(f.split('.o')[1])
        run_data[jobid] = dict()
        run_data[jobid]['PBS log'] = parse_pbs_log(f)
        run_data[jobid]['PBS log']['PBS log file'] = f

    # get run data for all jobs
    for jobid in run_data:
        print('.', end='', flush=True)
        pbs = run_data[jobid]['PBS log']
        date = pbs['Run completion date']  # BUG: would be better to have time when run began, including time zone
        if date is not None:
            run_data[jobid]['git log'] = parse_git_log(basepath, date)
            # BUG: assumes no commits between run start and end
            # BUG: assumes the time zones match - no timezone specified in date - what does git assume? UTC?
            if pbs['Exit Status'] == 0:  # output dir belongs to this job only if Exit Status = 0
                outdir = 'output' + str(pbs['Run number']).zfill(3)
                paths = [os.path.join(sync_path, outdir),
                         os.path.join(basepath, 'archive', outdir)]
                run_data[jobid]['MOM_time_stamp.out'] = parse_mom_time_stamp(paths)
                run_data[jobid]['config.yaml'] = parse_config_yaml(paths)
                run_data[jobid]['namelists'] = parse_nml(paths)
                run_data[jobid]['access-om2.out'] = parse_accessom2_out(paths)
                run_data[jobid]['ice_diag.d'] = parse_cice_timing(paths)

    all_run_data = copy.deepcopy(run_data)  # all_run_data includes failed jobs

    # remove failed jobs from run_data
    for jobid in all_run_data:
        print('.', end='', flush=True)
        pbs = all_run_data[jobid]['PBS log']
        date = pbs['Run completion date']
        if date is None:  # no PBS info in log file
            del run_data[jobid]
        elif pbs['Run number'] is None:  # not a model run log file
            del run_data[jobid]
        elif pbs['Exit Status'] != 0:  # output dir belongs to this job only if Exit Status = 0
            del run_data[jobid]
        elif len(run_data[jobid]['config.yaml']) == 0:  # output dir missing
            del run_data[jobid]

    # (jobid, run number) tuples sorted by run number - re-done below
    jobid_run_tuples = sorted([(k, v['PBS log']['Run number'])
                               for (k, v) in run_data.items()],
                              key=lambda t: t[1])
    if len(jobid_run_tuples) == 0:
        print('\nAborting: no successful jobs?')
        return

# Remove the older jobid if run number is duplicated - assume run was re-done
# (check by date rather than jobid, since jobid sometimes rolls over)
    prev_jobid_run = jobid_run_tuples[0]
    for jobid_run in jobid_run_tuples[1:]:
        if jobid_run[1] == prev_jobid_run[1]:  # duplicated run number
            if run_data[jobid_run[0]]['PBS log']['Run completion date']\
             > run_data[prev_jobid_run[0]]['PBS log']['Run completion date']:
                del run_data[prev_jobid_run[0]]
                prev_jobid_run = jobid_run
            else:
                del run_data[jobid_run[0]]
        else:
            prev_jobid_run = jobid_run

    # re-do (jobid, run number) tuples sorted by run number
    jobid_run_tuples = sorted([(k, v['PBS log']['Run number'])
                               for (k, v) in run_data.items()],
                              key=lambda t: t[1])
    if len(jobid_run_tuples) == 0:
        print('\nAborting: no successful jobs?')
        return

    # jobid keys into run_data sorted by run number
    sortedjobids = [k[0] for k in jobid_run_tuples]

    # allow referencing by submodel name as well as list index
    for jobid in run_data:
        run_data[jobid]['config.yaml']['submodels-by-name'] = dict()
        for sm in run_data[jobid]['config.yaml']['submodels']:
            run_data[jobid]['config.yaml']['submodels-by-name'][sm['name']] = sm

    # make a 'timing' entry to contain model timestep and run length for both MATM and YATM runs
    # run length is [years, months, days, seconds] to accommodate both MATM and YATM
    for jobid in run_data:
        r = run_data[jobid]
        timing = dict()
        if r['namelists']['accessom2.nml'] is None:  # non-YATM run
            timing['Timestep'] = r['config.yaml']['submodels'][1]['timestep']  # MOM timestep
            rt = r['config.yaml']['calendar']['runtime']
            timing['Run length'] = [rt['years'], rt['months'], rt['days'], 0]  # insert 0 seconds
        else:
            timing['Timestep'] = r['namelists']['accessom2.nml']['accessom2_nml']['ice_ocean_timestep']
            rp = r['namelists']['accessom2.nml']['date_manager_nml']['restart_period']
            timing['Run length'] = rp[0:2] + [0] + [rp[2]]  # insert 0 days
        yrs = r['MOM_time_stamp.out']['Model run length (days)']/365.25  # FUDGE: assumes 365.25-day year
        timing['SU per model year'] = r['PBS log']['Service Units']/yrs
        timing['Walltime (hr) per model year'] = r['PBS log']['Walltime Used (hr)']/yrs
        r['timing'] = timing

    # include changes in all git commits since previous run
    for i, jobid in enumerate(sortedjobids):
        print('.', end='', flush=True)
        run_data[jobid]['git diff'] = \
            git_diff(basepath,
                     run_data[sortedjobids[max(i-1, 0)]]['git log']['Commit'],
                     run_data[jobid]['git log']['Commit'])

    # count failed jobs prior to each successful run
    # BUG: always have zero count between two successful runs straddling a jobid rollover
    # BUG: first run also counts all fails after a rollover
    prevjobid = -1
    for i, jobid in enumerate(sortedjobids):
        c = [e for e in all_run_data.keys() if e > prevjobid and e < jobid
             and e not in run_data]
        c.sort()
        run_data[jobid]['PBS log']['Failed previous jobids'] = c
        run_data[jobid]['PBS log']['Failed previous jobs'] = len(c)
        prevjobid = jobid

    if list_available:
        print('\nInformation which can be tabulated if added to output_format:')
        keyliststr = []
        for k in keylists(recursive_superset(run_data)):
            keyliststr.append("['" + "', '".join(k) + "']")
        keyliststr.sort()
        for k in keyliststr:
            print(k)

    if dump_all:
        dumpoutfile = os.path.splitext(outfile)[0]+'.yaml'
        print('\nWriting', dumpoutfile)
        with open(dumpoutfile, 'w') as outf:
            yaml.dump(run_data, outf, default_flow_style=False)

    ###########################################################################
    # Specify the output format here.
    ###########################################################################
    # output_format is a list of (key, value) tuples, one for each column.
    # keys are headers (must be unique)
    # values are lists of keys into run_data (omitting job id)
    #
    # run_data dict structure (use list_available for full details):
    #
    # run_data dict
    #    L___ job ID dict
    #           L___ 'PBS log' dict
    #           L___ 'git log' dict
    #           L___ 'git diff' dict
    #           L___ 'MOM_time_stamp.out' dict
    #           L___ 'config.yaml' dict
    #           L___ 'access-om2.out' dict
    #           L___ 'timing' dict
    #           L___ 'namelists' dict
    #                   L___ 'accessom2.nml' namelist (or None if non-YATM run)
    #                   L___ 'atmosphere/atm.nml' namelist (only if YATM run)
    #                   L___ 'atmosphere/input_atm.nml' namelist (only if MATM run)
    #                   L___ '/ice/cice_in.nml' namelist
    #                   L___ 'ice/input_ice.nml' namelist
    #                   L___ 'ice/input_ice_gfdl.nml' namelist
    #                   L___ 'ice/input_ice_monin.nml' namelist
    #                   L___ 'ocean/input.nml' namelist
    #    L___ job ID dict
    #           L___ ... etc
    output_format = OrderedDict([
        ('Run number', ['PBS log', 'Run number']),
        ('Run start', ['MOM_time_stamp.out', 'Model start time']),
        ('Run end', ['MOM_time_stamp.out', 'Model end time']),
        ('Run length (years, months, days, seconds)', ['timing', 'Run length']),
        ('Run length (days)', ['MOM_time_stamp.out', 'Model run length (days)']),
        ('Job Id', ['PBS log', 'Job Id']),
        ('Failed previous jobs', ['PBS log', 'Failed previous jobs']),
        ('Failed previous jobids', ['PBS log', 'Failed previous jobids']),
        ('Run completion date', ['PBS log', 'Run completion date']),
        ('Queue', ['config.yaml', 'queue']),
        ('Service Units', ['PBS log', 'Service Units']),
        ('Walltime Used (hr)', ['PBS log', 'Walltime Used (hr)']),
        ('SU per model year', ['timing', 'SU per model year']),
        ('Walltime (hr) per model year', ['timing', 'Walltime (hr) per model year']),
        ('Memory Used (Gb)', ['PBS log', 'Memory Used (Gb)']),
        ('NCPUs Used', ['PBS log', 'NCPUs Used']),
        ('MOM NCPUs', ['config.yaml', 'submodels-by-name', 'ocean', 'ncpus']),
        ('CICE NCPUs', ['config.yaml', 'submodels-by-name', 'ice', 'ncpus']),
        ('Fraction of MOM runtime in oasis_recv', ['access-om2.out', 'oasis_recv', 'tfrac']),
        ('Max MOM wait for oasis_recv (s)', ['access-om2.out', 'oasis_recv', 'tmax']),
        ('Max CICE wait for coupler (s)', ['ice_diag.d', 'waiting_o', 'node', 'max']),
        ('Max CICE I/O time (s)', ['ice_diag.d', 'ReadWrite', 'node', 'max']),
        ('MOM tile layout', ['namelists', 'ocean/input.nml', 'ocean_model_nml', 'layout']),
        ('CICE tile distribution', ['namelists', 'ice/cice_in.nml', 'domain_nml', 'distribution_type']),
        ('Timestep (s)', ['timing', 'Timestep']),
        ('MOM barotropic split', ['namelists', 'ocean/input.nml', 'ocean_model_nml', 'barotropic_split']),
        ('CICE dynamic split (ndtd)', ['namelists', 'ice/cice_in.nml', 'setup_nml', 'ndtd']),
        ('ktherm', ['namelists', 'ice/cice_in.nml', 'thermo_nml', 'ktherm']),
        ('Common inputs', ['config.yaml', 'input']),
        ('Atmosphere executable', ['config.yaml', 'submodels-by-name', 'atmosphere', 'exe']),
        ('Atmosphere inputs', ['config.yaml', 'submodels-by-name', 'atmosphere', 'input']),
        ('MOM executable', ['config.yaml', 'submodels-by-name', 'ocean', 'exe']),
        ('MOM inputs', ['config.yaml', 'submodels-by-name', 'ocean', 'input']),
        ('CICE executable', ['config.yaml', 'submodels-by-name', 'ice', 'exe']),
        ('CICE inputs', ['config.yaml', 'submodels-by-name', 'ice', 'input']),
        ('Payu version', ['PBS log', 'payu version']),
        ('Git hash of run', ['git log', 'Commit']),
        ('Commit date', ['git log', 'Date']),
        ('Git-tracked file changes since previous run', ['git diff', 'Changed files']),
        ('Git log messages since previous run', ['git diff', 'Messages']),
        ])
    ###########################################################################

    if True:  # whether to output all namelist changes
        output_format_nmls = OrderedDict()
        nmls_any_runs = set(run_data[list(run_data.keys())[0]]['namelists'].keys())
        nmls_all_runs = nmls_any_runs
        # avoid dict comprehension here to avoid python<2.7 syntax error
        nmls_no_runs = dict([(k, True) for k in nmls_any_runs])  # True for namelists that are None for all runs
        # nmls_no_runs = {k: True for k in nmls_any_runs}  # True for namelists that are None for all runs
        for jobid in run_data:
            run_nmls = run_data[jobid]['namelists']
            nmls_any_runs = set(run_nmls.keys()) | nmls_any_runs
            nmls_all_runs = set(run_nmls.keys()) & nmls_all_runs
            for nml in set(nmls_all_runs):
                if run_nmls[nml] is None:
                    nmls_all_runs.remove(nml)
            for nml in run_nmls:
                newnone = (nml is None)
                if nml in nmls_no_runs:
                    nmls_no_runs[nml] = nmls_no_runs[nml] and newnone
                else:
                    nmls_no_runs.update({nml: newnone})
        for nml in set(nmls_any_runs):
            if nmls_no_runs[nml]:
                nmls_any_runs.remove(nml)

        # add every changed group/variable in nml files that exist in all runs
        for nml in nmls_all_runs:
            # avoid dict comprehension here to avoid python<2.7 syntax error
            nmllistall = dict([(jobid,
                              copy.deepcopy(run_data[jobid]['namelists'][nml]))
                              for jobid in run_data])
            # nmllistall = {jobid: copy.deepcopy(run_data[jobid]['namelists'][nml])
            #               for jobid in run_data}
            groups = nmltab.superset(nmltab.nmldiff(nmllistall))
            for group in groups:
                for var in groups[group]:
                    ngv = [nml, group, var]
                    output_format_nmls.update(OrderedDict([
                        (' -> '.join(ngv), ['namelists'] + ngv)]))

        # add all group/variables in nml files that exist in only some runs
        for nml in nmls_any_runs - nmls_all_runs:
            nmllistall = dict()
            for jobid in run_data:
                if nml in run_data[jobid]['namelists']:
                    if run_data[jobid]['namelists'][nml] is not None:
                        nmllistall.update({jobid:
                              copy.deepcopy(run_data[jobid]['namelists'][nml])})
            groups = nmltab.superset(nmllistall)
            for group in groups:
                for var in groups[group]:
                    ngv = [nml, group, var]
                    output_format_nmls.update(OrderedDict([
                        (' -> '.join(ngv), ['namelists'] + ngv)]))

        # alphabetize
        output_format_nmls = OrderedDict([(k, output_format_nmls[k])
                                 for k in sorted(output_format_nmls.keys())])

        # add output_format entries for every namelist variable that has changed in any run
        output_format.update(output_format_nmls)

    # output csv file according to output_format above
    print('\nWriting', outfile)
    with open(outfile, 'w', newline='') as csvfile:
        csvw = csv.writer(csvfile, dialect='excel')
        csvw.writerow(['Summary report generated by run_summary.py, https://github.com/aekiss/run_summary'])
        csvw.writerow(['report generated:', datetime.datetime.now().replace(microsecond=0).astimezone().isoformat()])
        csvw.writerow(['control directory path:', basepath, 'git branch:', git_branch])
        csvw.writerow(['hh5 output path:', sync_path])
        csvw.writerow(output_format.keys())  # header
        for jobid in sortedjobids:
            csvw.writerow([dictget(run_data, [jobid] + keylist) for keylist in output_format.values()])
    print('Done.')

    return


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=
        'Summarise ACCESS-OM2 runs.\
        Latest version and help: https://github.com/aekiss/run_summary')
    parser.add_argument('-l', '--list',
                        action='store_true', default=False,
                        help='list all data that could be tabulated by adding it to output_format')
    parser.add_argument('-d', '--dump_all',
                        action='store_true', default=False,
                        help='also dump all data to <outfile>.yaml')
    parser.add_argument('-o', '--outfile', type=str,
                        metavar='file',
                        default=None,
                        help="output file path; default is 'run_summary_<dir name on hh5>.csv';\
                        WARNING: will be overwritten")
    parser.add_argument('path', metavar='path', type=str, nargs='*',
                        help='zero or more ACCESS-OM2 control directory paths; default is current working directory')
    args = parser.parse_args()
    lst = vars(args)['list']
    dump_all = vars(args)['dump_all']
    outfile = vars(args)['outfile']
    basepaths = vars(args)['path']  # a list of length >=0 since nargs='*'
    if outfile is None:
        if basepaths is None:
            run_summary(list_available=lst, dump_all=dump_all)
        else:
            for bp in basepaths:
                try:
                    run_summary(basepath=bp, list_available=lst,
                                dump_all=dump_all)
                except:
                    print('\nFailed. Error:', sys.exc_info())
    else:
        if basepaths is None:
            run_summary(outfile=outfile, list_available=lst, dump_all=dump_all)
        else:
            for bp in basepaths:
                try:
                    run_summary(basepath=bp, outfile=outfile, 
                                list_available=lst, dump_all=dump_all)
                except:
                    print('\nFailed. Error:', sys.exc_info())

# TODO: run_diff : git diff between 2 runs