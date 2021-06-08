import asyncio

from aioredis.client import Pipeline
from . import vis_sync

from ..task_manager import DbClient, queue_manager, cancellation_manager
from ..redis import redis, LEVELS

from .tree_heatmap import tree, heatmap

import time
import pandas as pd


@queue_manager.add_handler("/queues/vis")
@cancellation_manager.wrap_handler
async def vis(db: DbClient):
    @db.transaction
    async def res(pipe: Pipeline):
        pipe.multi()
        pipe.set(f"/tasks/{db.task_id}/stage/{db.stage}/status", "Executing")
        pipe.set(f"/tasks/{db.task_id}/stage/{db.stage}/heatmap-message", "waiting")

        db.report_progress(
            current=0,
            total=-1,
            message="getting correlation data",
            pipe=pipe,
        )
        pipe.mget(
            f"/tasks/{db.task_id}/request/blast_enable",
            f"/tasks/{db.task_id}/request/blast_evalue",
            f"/tasks/{db.task_id}/request/blast_pident",
            f"/tasks/{db.task_id}/request/dropdown2"
        )

    blast_enable, blast_evalue, blast_pident, level_id = (await res)[-1]
    blast_enable = bool(blast_enable)
    level_id = int(level_id)
    level, phyloxml_file = LEVELS[level_id]

    organisms, csv_data = await vis_sync.read_org_info(
        phyloxml_file=phyloxml_file,
        og_csv_path=str(db.task_dir/'OG.csv')
    )
    organisms:list[str]
    csv_data: pd.DataFrame

    db.report_progress(
        current=0,
        total=len(csv_data),
    )

    df = pd.DataFrame(data={"Organisms": organisms}, dtype=object)
    df.set_index('Organisms', inplace=True)
    corr_info_to_fetch = {}

    cur_time = int(time.time())
    async with redis.pipeline(transaction=False) as pipe:
        for _, data in csv_data.iterrows():
            label=data['label']
            pipe.hgetall(f"/cache/corr/{level}/{label}/data")
            pipe.set(f"/cache/corr/{level}/{label}/accessed", cur_time, xx=True)

        res = (await pipe.execute())[::2]
        for (_, data), cache in zip(csv_data.iterrows(), res):
            og_name=data['Name']
            if cache:
                df[og_name] = pd.Series(
                    map(int, cache.values()),
                    index=cache.keys(),
                    name=og_name,
                    dtype=int,
                )
                db.report_progress(current_delta=1)
            else:
                corr_info_to_fetch[og_name] = data['label']

        # res["org_name"]["value"]: int(res["count_orthologs"]["value"]

    if corr_info_to_fetch:
        async def progress(items_in_front):
            if items_in_front > 0:
                db.report_progress(
                    message=f"In queue to request correlation data ({items_in_front} tasks in front)",
                )
            elif items_in_front == 0:
                await db.flush_progress(
                    message="Requesting correlation data",
                )

        tasks = [
            vis_sync.get_corr_data(
                name=name,
                label=label,
                level=level,
            )
            for name, label in corr_info_to_fetch.items()
        ]
        tasks[0].set_progress_callback(progress)

        try:
            async with redis.pipeline(transaction=False) as pipe:
                for f in asyncio.as_completed(tasks):
                    og_name, data = await f
                    og_name: str
                    data: dict

                    cur_time = int(time.time())
                    label = corr_info_to_fetch[og_name]
                    pipe.hset(f"/cache/corr/{level}/{label}/data", mapping=data)
                    pipe.set(f"/cache/corr/{level}/{label}/accessed", cur_time)
                    pipe.setnx(f"/cache/corr/{level}/{label}/created", cur_time)

                    df[og_name] = pd.Series(data, name=og_name, dtype=int)
                    db.report_progress(current_delta=1)

                await pipe.execute()
        except:
            for t in tasks:
                t.cancel()
            raise


    @db.transaction
    async def tx(pipe: Pipeline):
        pipe.multi()
        pipe.mset({
            f"/tasks/{db.task_id}/stage/{db.stage}/status": "Waiting",
            f"/tasks/{db.task_id}/stage/{db.stage}/message": "",
            f"/tasks/{db.task_id}/stage/{db.stage}/total": 0,
        })
    await tx

    # interpret the results:
    df.fillna(0, inplace=True)
    df.reset_index(drop=False, inplace=True)


    df_for_heatmap = df.copy()
    df_for_heatmap = df_for_heatmap.iloc[:, 1:]
    df_for_heatmap.columns = csv_data['Name']

    tasks = []
    tasks.append(
        asyncio.create_task(
            heatmap(
                db=db,
                organism_count=len(organisms),
                df=df_for_heatmap,
            )
        )
    )
    del df_for_heatmap

    tasks.append(
        asyncio.create_task(
            tree(
                db=db,

                phyloxml_file=phyloxml_file,
                OG_names=csv_data['Name'],
                df=df,
                organisms=organisms,

                do_blast=blast_enable,
            )
        )
    )


    # TODO: blast


    del csv_data
    del df
    del organisms

    try:
        _, to_blast = await asyncio.gather(*tasks)
    except:
        for task in tasks:
            task.cancel()
        raise

    if not blast_enable:
        @db.transaction
        async def tx(pipe: Pipeline):
            pipe.multi()
            pipe.set(
                f"/tasks/{db.task_id}/stage/{db.stage}/status", "Done",
            )
        await tx
        return


    print("to_blast_viz", to_blast)
    # for prot, tax_ids in to_blast.items():
    #     # TODO: schedule per-process
    #     blast.do_blast(prot, tax_ids)