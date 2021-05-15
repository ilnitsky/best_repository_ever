from functools import partialmethod
import itertools
import time
import json
import math
import re
from collections import defaultdict
from pathlib import Path
import os

import redis
from redis.client import Pipeline
import celery
from celery.exceptions import Ignore, Reject, CeleryError

import SPARQLWrapper
import pandas as pd
import numpy as np
import seaborn as sns
import matplotlib.pyplot as plt
from lxml import etree as ET
import fastcluster
from scipy.cluster import hierarchy

from protein_fetcher import orthodb_get, uniprot_get, ortho_data_get
from db import db, cond_cas

DEBUG = 'DEBUG' in os.environ

ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
NS = {
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "": "http://www.phyloxml.org"
}


def queueinfo_upd(task_id, stage, client=None):
    cond_cas(
        if_key=f"/tasks/{task_id}/stage/{stage}/status",
        equals_value="Enqueued",
        set_key="/queueinfo/last_launched_id",
        to_value_of_this_key_if_larger=f"/tasks/{task_id}/stage/{stage}/current",
        client=client,
    )


app = celery.Celery('worker', broker='redis://redis/1', backend='redis://redis/1')

DATA_PATH = Path.cwd() / "user_data"
if not DATA_PATH.exists():
    raise RuntimeError("No data dir!")

INVALID_PROT_IDS = re.compile(r"[^A-Za-z0-9\-\n \t]+")


def fake_delay(): pass
#region utils
def fake_delay():
    import random
    time.sleep(random.randint(100, 500)/1000)
    pass

def split_into_groups(iterable, group_len):
    it = iter(iterable)
    accum = 0
    while True:
        accum += group_len
        this_len = round(accum)
        accum -= this_len
        res = tuple(itertools.islice(it, this_len))
        if not res:
            break
        yield res



def chunker(items:list, min_items_per_worker, max_workers, max_items_per_request=None, dedupe=True):
    if dedupe:
        items = list(dict.fromkeys(items))
    chunk_count = min(
        max(1,math.ceil(len(items) / min_items_per_worker)),
        max_workers,
    )
    items_per_chunk = math.ceil(len(items)/chunk_count)
    if max_items_per_request is not None:
        subchunks_per_chunk = math.ceil(items_per_chunk/max_items_per_request)
    else:
        subchunks_per_chunk = 1

    total_slots = chunk_count * subchunks_per_chunk

    items_per_subchunk = len(items)/total_slots

    yield from split_into_groups(
        split_into_groups(items, items_per_subchunk),
        subchunks_per_chunk,
    )
#endregion


#region dbm
class RollbackException(Exception):
    pass

class ReportErrorException(Exception):
    def __init__(self, message=None):
        super().__init__()
        self.message = message

class DBManager():
    def __init__(self, stage, task_id, version, progress_interval=1):
        self.stage = stage
        self.task_id = task_id
        self.version = version

        self._curr_incr = 0
        self._total_incr = 0
        self._can_use_relative_progress = 0
        self.last_progress = 0
        self.progress_interval = progress_interval

    def report_error(self, message, cancel_rest=True):
        @self.tx
        def _(pipe:Pipeline):
            pipe.watch(f"/tasks/{self.task_id}/stage/{self.stage}/status")
            status = pipe.get(f"/tasks/{self.task_id}/stage/{self.stage}/status")
            pipe.multi()
            if status == "Error":
                pipe.append(f"/tasks/{self.task_id}/stage/{self.stage}/message", f'; {message}')
            else:
                pipe.mset({
                    f"/tasks/{self.task_id}/stage/{self.stage}/message": message,
                    f"/tasks/{self.task_id}/stage/{self.stage}/status": "Error",
                    f"/tasks/{self.task_id}/stage/{self.stage}/total": -2,
                })
            if cancel_rest:
                pipe.incr(f"/tasks/{self.task_id}/stage/{self.stage}/version")


    def run_code(self, func, *args, cancel_on_error=True, **kwargs):
        """Runs func with error reporting on exceptions"""
        try:
            res = func(*args, **kwargs)
            return res
        except CeleryError:
            raise
        except ReportErrorException as e:
            self.report_error(e.message, cancel_rest=cancel_on_error)
            if e.__cause__ is not None:
                e = e.__cause__
            raise Reject(e, requeue=False) from e
        except Exception as e:
            msg = "Internal server error"
            if DEBUG:
                msg += ": " + repr(e)
            self.report_error(msg, cancel_rest=cancel_on_error)
            raise Reject(e, requeue=False) from e

    def tx(self, func=None, allow_read_only=False, retry_delay=0) -> list:
        """Decorator immediately runs the function in transaction mode
        function must not call pipe.execute():
            return normally to get pipe.execute()'s results assigned to the function name
            raise any Exception to discard transaction and reraise that execption
            raise RollbackException to discard transaction and exit with result "None"

        function may be rerun multiple times in case watch error is triggered, be careful with side-effects

        function must accept 1 argument: pipeline in immediate execution state
        if allow_read_only=True function must accept a second argument: can_write (bool)
            in case can_write is false function must not perform any write operations on the object task_id
        """
        if func is None:
            return partialmethod(self.run_tx, allow_read_only=allow_read_only, retry_delay=retry_delay)

        with db.pipeline(transaction=True) as pipe:
            pipe_execute = pipe.execute
            pipe.execute = None
            while True:
                try:
                    pipe.watch(f"/tasks/{self.task_id}/stage/{self.stage}/version")
                    version = int(pipe.get(f"/tasks/{self.task_id}/stage/{self.stage}/version"))
                    can_write = version == self.version
                    if allow_read_only:
                        args = (pipe, can_write)
                    else:
                        if not can_write:
                            raise Ignore()
                        args = (pipe,)
                    try:
                        func(*args)
                    except RollbackException:
                        res = None
                    except Exception:
                        raise
                    else:
                        res = pipe_execute()
                    return res
                except redis.WatchError:
                    if retry_delay:
                        time.sleep(retry_delay)
                    continue

    def __enter__(self):
        self._can_use_relative_progress += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._can_use_relative_progress == 1:
            if self._curr_incr or self._total_incr:
                self.progress(flush=True)
        self._can_use_relative_progress -= 1

    # pylint: disable=unused-argument
    def set_progress(self, current=None, total=None, message=None, pipe: Pipeline = None):
        l = locals()
        upd = {
            f"/tasks/{self.task_id}/stage/{self.stage}/{k}": l[k]
            for k in ('current', 'total', 'message')
            if l[k] is not None
        }
        if upd:
            if pipe is None:
                @self.tx
                def _(pipe: redis.client.Pipeline):
                    pipe.multi()
                    pipe.mset(upd)
            else:
                pipe.mset(upd)

        self._curr_incr = 0
        self._total_incr = 0
        return True

    def progress(self, incr_curr=0, incr_total=0, message=None, flush=False):
        if self._can_use_relative_progress == 0:
            # Can't trust that the progress would be flushed later, so force-flushing now
            flush = True
        self._curr_incr += incr_curr
        self._total_incr += incr_total
        if not (message or flush):
            # apply time limits
            if time.time() - self.last_progress < self.progress_interval:
                return False

        if self._curr_incr or self._total_incr or message is not None:
            @self.tx
            def _(pipe: redis.client.Pipeline):
                pipe.multi()
                if message is not None:
                    pipe.set(f"/tasks/{self.task_id}/stage/{self.stage}/message", message)
                if self._curr_incr != 0:
                    pipe.incrby(f"/tasks/{self.task_id}/stage/{self.stage}/current", self._curr_incr)
                if self._total_incr != 0:
                    pipe.incrby(f"/tasks/{self.task_id}/stage/{self.stage}/total", self._total_incr)
            self._curr_incr = 0
            self._total_incr = 0

        self.last_progress = time.time()
        return True

    def launch_task(self, stage:str, task_id:str = None):
        should_launch_task = True
        if task_id is None:
            task_id = self.task_id
        # Trying to set status to enqueued if the task isn't already running
        @self.tx
        def _(pipe: Pipeline):
            nonlocal should_launch_task
            version = pipe.incr(f"/tasks/{task_id}/stage/{stage}/version")
            pipe.unlink(f"/tasks/{task_id}/stage/{stage}/status")
            pipe.watch(f"/tasks/{task_id}/stage/{stage}/status")
            if should_launch_task:
                app.signature(
                    f'tasks.build_{stage}',
                    args=(task_id, version)
                ).apply_async()
                should_launch_task = False

            status = pipe.get(f"/tasks/{task_id}/stage/{stage}/status")
            if status is not None:
                # Task has already modified it to something else, so we are not enqueued
                return
            # Task is still in the queue
            pipe.multi()
            pipe.mset({
                f"/tasks/{task_id}/stage/{stage}/status": 'Enqueued',
                f"/tasks/{task_id}/stage/{stage}/total": -3,
            })
            pipe.incr("/queueinfo/cur_id")
            pipe.execute_command("COPY", "/queueinfo/cur_id", f"/tasks/{task_id}/stage/{stage}/current", "REPLACE")


#endregion

def decode_str(item:bytes, default='') -> str:
    if item:
        return item.decode()
    else:
        return default

def decode_int(item:bytes, default=0) -> int:
    if item:
        return int(item)
    else:
        return default




@app.task()
def build_table(task_id, version):
    dbm = DBManager("table", task_id, version)
    dbm.run_code(do_build_table, dbm, task_id, version)

def do_build_table(dbm: DBManager, task_id, version):
    prot_ids = None
    stage = 'table'
    fake_delay()
    @dbm.tx
    def res(pipe: Pipeline):
        nonlocal prot_ids
        queueinfo_upd(task_id, stage, client=pipe)
        prot_req = pipe.get(f"/tasks/{task_id}/request/proteins")

        prot_ids = list(dict.fromkeys( # removing duplicates
            INVALID_PROT_IDS.sub("", decode_str(prot_req)).upper().split(),
        ))

        pipe.multi()
        pipe.set(f"/tasks/{task_id}/stage/{stage}/status", "Executing")
        dbm.set_progress(
            current=0,
            total=len(prot_ids),
            message="Getting proteins",
            pipe=pipe,
        )
        pipe.get(f"/tasks/{task_id}/request/dropdown1")

    task_dir = DATA_PATH / task_id
    task_dir.mkdir(exist_ok=True)

    fake_delay()
    level = decode_str(res[-1]).split('-')[0]
    prot_ids : list

    # Filter out already cached proteins
    cur_time = int(time.time())
    with db.pipeline(transaction=False) as pipe:
        for prot_id in prot_ids:
            pipe.set(f"/cache/uniprot/{level}/{prot_id}/accessed", cur_time, xx=True)

        # If the protein doesn't exist - the set command returns None.
        # We get those proteins and return their IDs so they could be fetched
        prots_to_fetch = [
            prot_ids[i]
            for i, was_set in enumerate(pipe.execute())
            if not was_set
        ]
    dbm.set_progress(current=len(prot_ids) - len(prots_to_fetch))
    fake_delay()

    protein_fetch = celery.group(
        _fetch_proteins.s(
            task_id,
            version,
            prot_chunk,
            level,
        )
        for prot_chunk in chunker(
            prots_to_fetch, min_items_per_worker=20,
            max_workers=5, max_items_per_request=200, # 200
        )
    )

    pipeline = (
        protein_fetch |
        _get_orthogroups.s(
            task_id,
            version,
            prot_ids,
            level,
        )
    )
    pipeline.apply_async()


@app.task()
def _fetch_proteins(task_id, version, prot_ids, level):
    dbm = DBManager("table", task_id, version)
    return dbm.run_code(do_fetch_proteins, dbm, task_id, prot_ids, level, cancel_on_error=False)

def do_fetch_proteins(dbm:DBManager, task_id, prot_ids, level):
    """Task fetches protein info and puts it into the cache, returns og"""
    stage = "table"
    fake_delay()
    prots = defaultdict(list)
    req_ids = set()
    with dbm:
        for prot_id_group in prot_ids:
            req_ids.update(prot_id_group)
            data = orthodb_get(level, prot_id_group)
            dbm.progress(incr_curr=len(data))
            prots.update(data)

    fake_delay()


    sparql_misses = req_ids - prots.keys()
    missing_prots = []

    def write_missing(pipe: Pipeline):
        pipe.multi()
        pipe.append(f"/tasks/{task_id}/stage/{stage}/missing_msg", f"{', '.join(missing_prots)}, ")

    with dbm:
        for prot_id in sparql_misses:
            res = uniprot_get(prot_id)
            fake_delay()
            if res is None:
                missing_prots.append(prot_id)
                incr_curr = 0
                incr_total = -1
            else:
                incr_curr = 1
                incr_total = 0

            if dbm.progress(incr_curr, incr_total) and missing_prots:
                dbm.tx(write_missing)
                missing_prots.clear()

    if missing_prots:
        dbm.tx(write_missing)
        missing_prots.clear()

    cur_time = int(time.time())
    with db.pipeline(transaction=False) as pipe:
        # Add all prots to the cache
        for prot_id in prots:
            pipe.mset(
                {
                    f"/cache/uniprot/{level}/{prot_id}/data": json.dumps(prots[prot_id], separators=(',', ':')),
                    f"/cache/uniprot/{level}/{prot_id}/accessed": cur_time,
                }
            )
            pipe.setnx(f"/cache/uniprot/{level}/{prot_id}/created", cur_time)
        pipe.execute()

    # returning orthogroup id
    return [v[0] for v in prots.values()]


def get_orgs(level):
    parser = ET.XMLParser(remove_blank_text=True)
    tree = ET.parse(f'phyloxml/{level}.xml', parser)
    root = tree.getroot()

    orgs_xml = root.xpath("//pxml:id/..", namespaces={'pxml':"http://www.phyloxml.org"})
    # Assuming only children have IDs
    return [
        name
        for _, name in sorted(
            (int(org_xml.find("id", NS).text), org_xml.find("name", NS).text)
            for org_xml in orgs_xml
        )
    ]


ORTHO_INFO_COL = [
    "label",
    "description",
    "clade",
    "evolRate",
    "totalGenesCount",
    "multiCopyGenesCount",
    "singleCopyGenesCount",
    "inSpeciesCount",
    # "medianExonsCount", "stddevExonsCount",
    "medianProteinLength",
    "stddevProteinLength",
    "og"
]


def new_2():
    stage = "table"
    dbm.set_progress(current=0, total=-1, message="Getting orthogroup info")

    data = list(itertools.chain.from_iterable(
        json.loads(raw_text)
        for raw_text in filter(None, db.mget([
            f"/cache/uniprot/{level}/{prot_id}/data"
            for prot_id in prot_ids
        ]))
    ))
    fake_delay()



    uniprot_df = pd.DataFrame(
        columns=['label', 'Name', 'PID'],
        data=data,
    )

    uniprot_df.replace("", float('nan'), inplace=True)
    uniprot_df.dropna(axis="index", how="any", inplace=True)
    uniprot_df['is_duplicate'] = uniprot_df.duplicated(subset='label')

    og_list = []
    names = []
    uniprot_ACs = []

    # TODO: DataFrame.groupby would be better, but need an example to test
    for row in uniprot_df[uniprot_df.is_duplicate == False].itertuples():
        dup_row_names = uniprot_df[uniprot_df.label == row.label].Name.unique()
        og_list.append(row.label)
        names.append("-".join(dup_row_names))
        uniprot_ACs.append(row.PID)

    dbm.set_progress(total=len(og_list))

    #SPARQL Look For Presence of OGS in Species
    uniprot_df = pd.DataFrame(columns=['label', 'Name', 'UniProt_AC'], data=zip(og_list, names, uniprot_ACs))
    fake_delay()
    task_dir = DATA_PATH / task_id

    uniprot_df.to_csv(task_dir/'OG.csv', sep=';', index=False)


@app.task()
def _get_orthogroups(og_list, task_id, version, prot_ids, level, did_fetch=False):
    dbm = DBManager("table", task_id, version)
    dbm.run_code(do_get_orthogroups, dbm, task_id, version, og_list, prot_ids, level, did_fetch)

def do_get_orthogroups(dbm, task_id, version, og_list, prot_ids, level, did_fetch):
    # filter already cached data:
    print(f"do_get_orthogroups: {og_list=}")
    cache_misses = []

    cur_time = int(time.time())
    with db.pipeline(transaction=False) as pipe:
        for og in og_list:
            pipe.hmget(f"/cache/ortho/{og}/data", ORTHO_INFO_COL)
            pipe.set(f"/cache/ortho/{og}/accessed", cur_time, xx=True)
        cache_data = pipe.execute()[::2]

    # If some (or all) data is missing - reload the orthogroup
    for og, data in zip(og_list, cache_data):
        if any(d is None for d in data):
            cache_misses.append(og)

    dbm.progress(incr_curr=len(og)-len(cache_misses))

    if cache_misses:
        # Some orthogroups are missing from cache
        # Schedule a fetch operation for all cache_misses
        # and run this function again
        dbm.set_progress(message="Requesting orthogroup info")
        group = celery.group(
            _fetch_orthogroups.s(
                task_id,
                version,
                ortho_chunk,
                level,
            )
            for ortho_chunk in chunker(
                cache_misses, min_items_per_worker=20,
                max_workers=5, max_items_per_request=200, # 200
            )
        )

        pipeline = (
            group |
            _process_orthogroups.si(
                task_id,
                version,
                prot_ids,
                level,
            )
        )
        pipeline.apply_async()
        return


@app.task()
def _fetch_orthogroups(task_id, version, ortho_chunk, level):
    dbm = DBManager("table", task_id, version)
    dbm.run_code(do_fetch_orthogroups, dbm, task_id, ortho_chunk, level)

def do_fetch_orthogroups(dbm, task_id, ortho_chunk, level):
    for ogs in ortho_chunk:
        og_info = ortho_data_get(ogs, ORTHO_INFO_COL)

        cur_time = int(time.time())

        with dbm, db.pipeline(transaction=False) as pipe:
            for og in ogs:
                info = og_info[og]
                fake_delay()
                if info:
                    dbm.progress(incr_curr=1)
                else:
                    dbm.progress(incr_total=-1)
                pipe.hmset(f"/cache/ortho/{og}/data", info)
                pipe.set(f"/cache/ortho/{og}/accessed", cur_time)
                pipe.setnx(f"/cache/ortho/{og}/created", cur_time)
            pipe.execute()

@app.task()
def _process_orthogroups(task_id, version, ortho_chunk, level):
    dbm = DBManager("table", task_id, version)
    dbm.run_code(do_process_orthogroups, dbm, task_id, ortho_chunk, level)

def do_process_orthogroups(dbm, task_id, ortho_chunk, level):
    og_info_df = pd.DataFrame(
        (
            vals.values()
            for vals in og_info.values()
        ),
        columns=ORTHO_INFO_COL,
    )
    og_info_df = pd.merge(og_info_df, uniprot_df, on='label')

    display_columns = [
        "label",
        "Name",
        "description",
        "clade",
        "evolRate",
        "totalGenesCount",
        "multiCopyGenesCount",
        "singleCopyGenesCount",
        "inSpeciesCount",
        # "medianExonsCount", "stddevExonsCount",
        "medianProteinLength",
        "stddevProteinLength"
    ]
    og_info_df = og_info_df[display_columns]

    #prepare datatable update
    table_data = {
        "data": og_info_df.to_dict('records'),
        "columns": [
            {
                "name": i,
                "id": i,
            }
            for i in og_info_df.columns
        ]
    }
    table = json.dumps(table_data, ensure_ascii=False, separators=(',', ':'))

    @dbm.tx
    def _(pipe: Pipeline):
        pipe.multi()
        pipe.mset({
            f"/tasks/{task_id}/stage/{stage}/status": "Done",
            f"/tasks/{task_id}/stage/{stage}/dash-table": table,
        })


def next_func():

    ORTHO_INFO_COL = [
        "label",
        "description",
        "clade",
        "evolRate",
        "totalGenesCount",
        "multiCopyGenesCount",
        "singleCopyGenesCount",
        "inSpeciesCount",
        # "medianExonsCount", "stddevExonsCount",
        "medianProteinLength",
        "stddevProteinLength",
        "og"
    ]

    cache_misses = []

    og_info = defaultdict(dict)

    cur_time = int(time.time())
    with db.pipeline(transaction=False) as pipe:
        for og in og_list:
            pipe.hmget(f"/cache/ortho/{og}/data", ORTHO_INFO_COL)
            pipe.set(f"/cache/ortho/{og}/accessed", cur_time, xx=True)
        cache_data = pipe.execute()[::2]

    with dbm:
        for og, data in zip(og_list, cache_data):
            for col_name, val in zip(ORTHO_INFO_COL, data):
                if val is None:
                    cache_misses.append(og)
                    break
                og_info[og][col_name] = val.decode()
            else:
                # extracted from cache
                fake_delay()
                dbm.progress(incr_curr=1)

    if cache_misses:
        dbm.set_progress(message="Requesting orthogroup info")
        og_info = ortho_data_get(cache_misses, ORTHO_INFO_COL)

    cur_time = int(time.time())

    with dbm, db.pipeline(transaction=False) as pipe:
        for og in cache_misses:
            info = og_info[og]
            fake_delay()
            if info:
                dbm.progress(incr_curr=1)
            else:
                dbm.progress(incr_total=-1)
            pipe.hmset(f"/cache/ortho/{og}/data", info)
            pipe.set(f"/cache/ortho/{og}/accessed", cur_time)
            pipe.setnx(f"/cache/ortho/{og}/created", cur_time)
        pipe.execute()

    og_info_df = pd.DataFrame(
        (
            vals.values()
            for vals in og_info.values()
        ),
        columns=ORTHO_INFO_COL,
    )
    og_info_df = pd.merge(og_info_df, uniprot_df, on='label')

    display_columns = [
        "label",
        "Name",
        "description",
        "clade",
        "evolRate",
        "totalGenesCount",
        "multiCopyGenesCount",
        "singleCopyGenesCount",
        "inSpeciesCount",
        # "medianExonsCount", "stddevExonsCount",
        "medianProteinLength",
        "stddevProteinLength"
    ]
    og_info_df = og_info_df[display_columns]

    #prepare datatable update
    table_data = {
        "data": og_info_df.to_dict('records'),
        "columns": [
            {
                "name": i,
                "id": i,
            }
            for i in og_info_df.columns
        ]
    }
    table = json.dumps(table_data, ensure_ascii=False, separators=(',', ':'))

    @dbm.tx
    def _(pipe: Pipeline):
        pipe.multi()
        pipe.mset({
            f"/tasks/{task_id}/stage/{stage}/status": "Done",
            f"/tasks/{task_id}/stage/{stage}/dash-table": table,
        })




@app.task(name='tasks.SPARQLWrapper')
def SPARQLWrapper_Task(task_id, version):
    dbm = DBManager("sparql", task_id, version)
    dbm.run_code(do_SPARQLWrapper_Task, dbm, task_id)

def do_SPARQLWrapper_Task(dbm: DBManager, task_id):
    stage='sparql'
    fake_delay()
    @dbm.tx
    def res(pipe: Pipeline):
        queueinfo_upd(task_id, stage, client=pipe)
        pipe.multi()
        pipe.set(f"/tasks/{task_id}/stage/{stage}/status", "Executing")
        dbm.set_progress(
            current=0,
            total=-1,
            message="started sparql request",
            pipe=pipe,
        )
        pipe.get(f"/tasks/{task_id}/request/dropdown2")

    fake_delay()
    taxonomy_level = decode_str(res[-1])
    taxonomy = taxonomy_level.split('-')[0]

    # TODO: injection possible
    organisms = get_orgs(taxonomy_level)

    task_dir = DATA_PATH / task_id

    csv_data = pd.read_csv(task_dir/'OG.csv', sep=';')
    OG_labels = csv_data['label']
    OG_names = csv_data['Name']

    df = pd.DataFrame(data={"Organisms": organisms}, dtype=object)
    df.set_index('Organisms', inplace=True)
    ###


    # # Filter out already cached proteins
    # cur_time = int(time.time())
    # with db.pipeline(transaction=False) as pipe:
    #     for og in OG_labels:
    #         pipe.set(f"/cache/presence/{taxonomy}/{og}/accessed", cur_time, xx=True)

    #     # If the protein doesn't exist - the set command returns None.
    #     # We get those proteins and return their IDs so they could be fetched
    #     presence_to_fetch = [
    #         prot_ids[i]
    #         for i, was_set in enumerate(pipe.execute())
    #         if not was_set
    #     ]
    # dbm.set_progress(current=len(prot_ids) - len(prots_to_fetch))
    # fake_delay()

    ###
    endpoint = SPARQLWrapper.SPARQLWrapper("http://sparql.orthodb.org/sparql")

    for i, (og_label, og_name) in enumerate(zip(OG_labels, OG_names)):
        try:
            endpoint.setQuery(f"""prefix : <http://purl.orthodb.org/>
            select
            (count(?gene) as ?count_orthologs)
            ?org_name
            where {{
            ?gene a :Gene.
            ?gene :name ?Gene_name.
            ?gene up:organism/a ?taxon.
            ?taxon up:scientificName ?org_name.
            ?gene :memberOf odbgroup:{og_label}.
            ?gene :memberOf ?og.
            ?og :ogBuiltAt [up:scientificName "{taxonomy}"].
            }}
            GROUP BY ?org_name
            ORDER BY ?org_name
            """)
            endpoint.setReturnFormat(SPARQLWrapper.JSON)

            data = endpoint.query().convert()["results"]["bindings"]
        except Exception:
            data = ()

        # Small trick: preallocating the length of the arrays
        idx = [None] * len(data)
        vals = [None] * len(data)

        for j, res in enumerate(data):
            idx[j] = res["org_name"]["value"]
            vals[j]= int(res["count_orthologs"]["value"])

        df[og_name] = pd.Series(vals, index=idx, name=og_name, dtype=int)
        dbm.set_progress(
            current=i,
            total=len(OG_labels),
            message="getting correlation data",
        )

    # interpret the results:
    df.fillna(0, inplace=True)

    df.reset_index(drop=False, inplace=True)
    df.to_csv(task_dir / "SPARQLWrapper.csv", index=False)

    dbm.launch_task('heatmap')

    df['Organisms'] = df['Organisms'].astype("category")
    df['Organisms'].cat.set_categories(organisms, inplace=True)
    df.sort_values(["Organisms"], inplace=True)

    df.columns = ['Organisms', *OG_names]
    df = df[df['Organisms'].isin(organisms)]  #Select Main Species
    df = df.iloc[:, 1:]
    df = df[OG_names]

    for column in df:
        df[column] = df[column].astype(float)

    df.to_csv(task_dir / "Presence-Vectors.csv", index=False)

    dbm.launch_task('tree')

    @dbm.tx
    def _(pipe: Pipeline):
        pipe.multi()
        pipe.mset({
            f"/tasks/{task_id}/stage/{stage}/status": "Done",
            f"/tasks/{task_id}/stage/{stage}/message": "",
            f"/tasks/{task_id}/stage/{stage}/total": 0,
        })



@app.task()
def build_tree(task_id, version):
    dbm = DBManager("tree", task_id, version)
    dbm.run_code(do_build_tree, dbm, task_id, version)

def do_build_tree(dbm: DBManager, task_id, version):
    stage='tree'
    fake_delay()
    @dbm.tx
    def res(pipe: Pipeline):
        queueinfo_upd(task_id, stage, client=pipe)
        pipe.multi()
        pipe.set(f"/tasks/{task_id}/stage/{stage}/status", "Executing")
        dbm.set_progress(
            current=0,
            total=-1,
            message="building tree",
            pipe=pipe,
        )
        pipe.get(f"/tasks/{task_id}/request/dropdown2")

    fake_delay()
    taxonomy_level = decode_str(res[-1])

    task_dir = DATA_PATH / task_id

    #Create organisms list

    # organisms = [x.strip() for x in organisms]

    df4 = None
    @dbm.tx
    def _(pipe: Pipeline):
        nonlocal df4
        df4 = pd.read_csv(task_dir/"Presence-Vectors.csv")
    df4 = df4.clip(upper=1)

    # Slower, but without fastcluster lib
    # linkage = hierarchy.linkage(data_1, method='average', metric='euclidean')
    link = fastcluster.linkage(df4.T.values, method='average', metric='euclidean')
    dendro = hierarchy.dendrogram(link, no_plot=True, color_threshold=-np.inf)

    reordered_ind = dendro['leaves']

    parser = ET.XMLParser(remove_blank_text=True)
    tree = ET.parse(f'phyloxml/{taxonomy_level}.xml', parser)
    root = tree.getroot()
    graphs = ET.SubElement(root, "graphs")
    graph = ET.SubElement(graphs, "graph", type="heatmap")
    ET.SubElement(graph, "name").text = "Presense"
    legend = ET.SubElement(graph, "legend", show="1")

    for col_idx in reordered_ind:
        field = ET.SubElement(legend, "field")
        ET.SubElement(field, "name").text = df4.columns[col_idx]

    gradient = ET.SubElement(legend, "gradient")
    ET.SubElement(gradient, "name").text = "Custom"
    ET.SubElement(gradient, "classes").text = "2"

    data = ET.SubElement(graph, "data")
    for index, row in df4.iterrows():
        values = ET.SubElement(data, "values", {"for":str(index)})
        for col_idx in reordered_ind:
            ET.SubElement(values, "value").text = f"{row[df4.columns[col_idx]] * 100:.0f}"

    @dbm.tx
    def _(pipe: Pipeline):
        tree.write(str(task_dir/'cluser.xml'), xml_declaration=True)

    @dbm.tx
    def _(pipe: Pipeline):
        pipe.multi()
        pipe.mset({
            f"/tasks/{task_id}/stage/{stage}/status": "Done",
            f"/tasks/{task_id}/stage/{stage}/total": 0,
        })

@app.task()
def build_heatmap(task_id, version):
    dbm = DBManager("heatmap", task_id, version)
    dbm.run_code(do_build_heatmap, dbm, task_id, version)

def do_build_heatmap(dbm: DBManager, task_id, version):
    stage = 'heatmap'
    fake_delay()
    @dbm.tx
    def res(pipe: Pipeline):
        queueinfo_upd(task_id, stage, client=pipe)
        pipe.multi()
        pipe.set(f"/tasks/{task_id}/stage/{stage}/status", "Executing")
        dbm.set_progress(
            current=0,
            total=-1,
            message="building heatmap",
            pipe=pipe,
        )
        pipe.get(f"/tasks/{task_id}/request/dropdown2")

    fake_delay()
    taxonomy_level = decode_str(res[-1])
    task_dir = DATA_PATH / task_id

    organisms = get_orgs(taxonomy_level)

    csv_data = pd.read_csv(task_dir / 'OG.csv', sep=';')
    OG_names = csv_data['Name']

    df = pd.read_csv(task_dir / "SPARQLWrapper.csv")
    df = df.iloc[:, 1:]
    df.columns = OG_names
    pres_df = df.apply(pd.value_counts).fillna(0)
    pres_df_zero_values = pres_df.iloc[0, :]
    pres_list = [(1 - item / float(len(organisms))) for item in pres_df_zero_values]

    rgbs = [(1 - i, 0, 0) for i in pres_list]
    sns.set(font_scale=1.2)
    df = df.fillna(0).astype(float)
    # df = df.clip(upper=1)
    df = df.loc[:, (df != 0).any(axis=0)]

    customPalette = sns.color_palette([
        "#f72585","#b5179e","#7209b7","#560bad","#480ca8",
        "#3a0ca3","#3f37c9","#4361ee","#4895ef","#4cc9f0",
    ],as_cmap=True)


    sns.clustermap(
        df.corr(),
        cmap=customPalette,
        metric="correlation",
        figsize=(15, 15),
        col_colors=[rgbs],
        row_colors=[rgbs],
    )

    @dbm.tx
    def _(pipe: Pipeline):
        pipe.multi()
        pipe.mset({
            f"/tasks/{task_id}/stage/{stage}/status": "Done",
        })
        plt.savefig(task_dir/"Correlation.png")
