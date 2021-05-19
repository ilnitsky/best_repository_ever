import os
import os.path
import time
import json
import secrets

import urllib.parse as urlparse
from dash import Dash, callback_context, no_update
from dash.dependencies import Input, Output, State, DashDependency
import dash_table
import dash_core_components as dcc
import dash_html_components as html
import dash_bootstrap_components as dbc
import redis
import flask

import celery

from phydthree_component import PhydthreeComponent

from . import layout
from . import user

from functools import wraps

app = flask.Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

# external JavaScript files
external_scripts = [
    {
        'src': 'https://code.jquery.com/jquery-2.2.4.min.js',
        'integrity': 'sha256-BbhdlvQf/xTY9gja0Dq3HiwQF8LaCRTXxZKRutelT44=',
        'crossorigin': 'anonymous'
    },
    {
        'src': 'https://d3js.org/d3.v3.min.js',
        # 'integrity': 'sha384-Tc5IQib027qvyjSMfHjOMaLkfuWVxZxUPnCJA7l2mCWNIpG9mGCD8wGNIcPD7Txa',
        # 'crossorigin': 'anonymous'
    },
]
# external CSS stylesheets
external_stylesheets = [
    dbc.themes.UNITED,
]


dash_app = Dash(__name__, server=app, suppress_callback_exceptions=True, external_scripts=external_scripts, external_stylesheets=external_stylesheets)
dash_app.layout = layout.index

def login(dst):
    # TODO: dsiplay login layout, login, redirect to the original destintation
    user.register()
    return dcc.Location(pathname=dst, id="some_id", hash="1", refresh=True)

def new_task():
    user_id = flask.session["USER_ID"]
    t = int(time.time())
    for _ in range(100):
        task_id = secrets.token_hex(16)
        res = user.db.msetnx({
            f"/tasks/{task_id}/user_id": user_id,
            f"/tasks/{task_id}/created": t,
            f"/tasks/{task_id}/accessed": t,
        })
        if res:
            break
    else:
        raise RuntimeError("Failed to create a unique task_id")

    # Possible race condition, task_counter is only for statistics and ordering
    task_no = user.db.incr(f"/users/{user_id}/task_counter")
    user.db.set(f"/tasks/{task_id}/name", f"Request {task_no}")

    # Publishes the task to the system, must be the last action
    user.db.rpush(f"/users/{user_id}/tasks", task_id)

    return task_id

def get_task(task_id):
    """Checks that task_id is valid and active, sets /accessed date to current"""
    if '/' in task_id:
        return False
    res = user.db.set(f"/tasks/{task_id}/accessed", int(time.time()), xx=True)
    return res



@dash_app.callback(
    Output('page-content', 'children'),
    Output('location', 'search'),
    Input('location', 'href'),
)
def router_page(href):
    url = urlparse.urlparse(href)
    pathname = url.path.rstrip('/')
    search = f'?{url.query}'

    if pathname == '/dashboard':
        if not user.is_logged_in():
            return login(pathname), search

        args = urlparse.parse_qs(url.query)
        for arg in args:
            args[arg] = args[arg][0]

        create_task = True
        if 'task_id' in args:
            create_task = not get_task(args['task_id'])

        if create_task:
            args['task_id'] = new_task()
            search = f"?{urlparse.urlencode(args)}"

        return layout.dashboard(args['task_id']), search
    if pathname == '/reports':
        return layout.reports, search
    if pathname == '/blast':
        return layout.blast, search

    return '404', search


# TODO: need this?
@dash_app.callback(Output('dd-output-container', 'children'), [Input('dropdown', 'value')])
def select_level(value):
    return f'Selected "{value}" orthology level'


celery_app = celery.Celery('main', broker='redis://redis/1', backend='redis://redis/1')


class DashProxy():
    def __init__(self, args):
        self._data = {}
        self._input_order = []
        self._output_order = []
        self._outputs = {}
        self.triggered = None
        self.first_load = False
        self.triggered : set

        for arg in args:
            if not isinstance(arg, DashDependency):
                continue
            k = (arg.component_id, arg.component_property)

            if isinstance(arg, (Input, State)):
                self._input_order.append(k)
            elif isinstance(arg, Output):
                self._output_order.append(k)
            else:
                raise RuntimeError("Unknown DashDependency")

    def __getitem__(self, key):
        if key in self._outputs:
            return self._outputs[key]
        return self._data[key]

    def __setitem__(self, key, value):
        self._outputs[key] = value

    def _enter(self, args):
        for k, val in zip(self._input_order, args):
            self._data[k] = val
        triggers = callback_context.triggered

        if len(triggers) == 1 and triggers[0]['prop_id'] == ".":
            self.first_load = True
            self.triggered = set()
        else:
            self.triggered = set(
                tuple(item['prop_id'].rsplit('.', maxsplit=1))
                for item in callback_context.triggered
            )

    def _exit(self):
        res = tuple(
            self._outputs.get(k, no_update)
            for k in self._output_order
        )

        self._outputs.clear()
        self._data.clear()
        self.triggered.clear()

        return res

    @wraps(dash_app.callback)
    @classmethod
    def callback(cls, *args, **kwargs):
        def deco(func):
            dp = cls(args)
            def wrapper(*args2):
                dp._enter(args2)
                func(dp)
                return dp._exit()
            return dash_app.callback(*args, **kwargs)(wrapper)
        return deco

def decode_int(*items:bytes, default=0) -> int:
    if len(items)==1:
        return int(items[0]) if items[0] else default

    return map(
        lambda x: int(x) if x else default,
        items,
    )


def decode_str(*items, default=''):
    if len(items)==1:
        return items[0].decode() if items[0] else default

    return map(
        lambda x: x.decode() if x else default,
        items,
    )

def display_progress(status, total, current, msg):
    pbar = {
        "style": {"height": "30px"}
    }
    if total < 0:
        # Special progress bar modes:
        # -1 unknown length style
        # -2 static message
        # -3 Waiting in the queue
        pbar['max'] = 100
        pbar['value'] = 100
        animate = total != -2 # not static message
        pbar['animated'] = animate
        pbar['striped'] = animate

        if total == -3:
            llid = decode_int(user.db.get("/queueinfo/last_launched_id")) + 1
            if current > llid:
                msg = f"~{current - llid} tasks before yours"
            else:
                msg = "almost running"
    else:
        # normal progressbar
        pbar['animated'] = False
        pbar['striped'] = False
        pbar['max'] = total
        pbar['value'] = current
        msg = f"{msg} ({current}/{total})"

    if status == 'Error':
        pbar['color'] = 'danger'
    else:
        pbar['color'] = 'info'

    return dbc.Row(
            dbc.Col(
                dbc.Progress(
                    children=html.Span(
                        msg,
                        className="justify-content-center d-flex position-absolute w-100",
                        style={"color": "black"},
                    ),
                    **pbar,
                ),
                md=8, lg=6,
            ),
        justify='center')


@DashProxy.callback(
    Output('table-progress-updater', 'disabled'), # refresh_disabled

    Output('uniprotAC', 'value'),
    Output('dropdown', 'value'),

    Output('output_row', 'children'), # output

    Output('input_version', 'data'),
    Output('submit-button2', 'disabled'),

    Input('submit-button', 'n_clicks'),
    Input('table-progress-updater', 'n_intervals'),

    State('task_id', 'data'),
    State('input_version', 'data'),

    State('uniprotAC', 'value'),
    State('dropdown', 'value'),
)
def table(dp:DashProxy):
    """Perform action (cancel/start building the table)"""
    task_id = dp['task_id', 'data']
    # if dp.first_load:
    #     # update accessed timestamp on the first page load
    #     pipe.set(f"/tasks/{task_id}/accessed", int(time.time()))


    if ('submit-button', 'n_clicks') in dp.triggered:
        # Sending data
        with user.db.pipeline(transaction=True) as pipe:
            pipe.incr(f"/tasks/{task_id}/stage/table/version")
            pipe.execute_command("COPY", f"/tasks/{task_id}/stage/table/version", f"/tasks/{task_id}/stage/table/input_version", "REPLACE")
            pipe.mset({
                f"/tasks/{task_id}/request/proteins": dp['uniprotAC', 'value'],
                f"/tasks/{task_id}/request/dropdown1": dp['dropdown', 'value'],
            })
            # Remove the data
            pipe.unlink(
                f"/tasks/{task_id}/stage/table/dash-table",
                f"/tasks/{task_id}/stage/table/message",
                f"/tasks/{task_id}/stage/table/missing_msg",
                f"/tasks/{task_id}/stage/table/status",
            )
            new_version = pipe.execute()[0]
            dp['input_version', 'data'] = new_version
        dp['submit-button2', 'disabled'] = True
        # enqueuing the task
        celery_app.signature(
            'tasks.build_table',
            args=(task_id, new_version)
        ).apply_async()

        # Trying to set status to enqueued if the task isn't already running
        with user.db.pipeline(transaction=True) as pipe:
            while True:
                try:
                    pipe.watch(f"/tasks/{task_id}/stage/table/status")
                    status = pipe.get(f"/tasks/{task_id}/stage/table/status")
                    if status is not None:
                        # Task has already modified it to something else, so we are not enqueued
                        break
                    # Task is still in the queue
                    pipe.multi()
                    pipe.mset({
                        f"/tasks/{task_id}/stage/table/status": 'Enqueued',
                        f"/tasks/{task_id}/stage/table/total": -3,
                    })
                    pipe.incr("/queueinfo/cur_id")
                    pipe.execute_command("COPY", "/queueinfo/cur_id", f"/tasks/{task_id}/stage/table/current", "REPLACE")
                    pipe.execute()
                    break
                except redis.WatchError:
                    continue

    # fill the output row
    # here because of go click, first launch or interval
    with user.db.pipeline(transaction=True) as pipe:
        while True:
            try:
                pipe.watch(f"/tasks/{task_id}/stage/table/input_version")
                input_version = pipe.get(f"/tasks/{task_id}/stage/table/input_version")
                input_version = decode_int(input_version)

                keys = [
                    f"/tasks/{task_id}/stage/table/status",
                    f"/tasks/{task_id}/stage/table/message",
                    f"/tasks/{task_id}/stage/table/current",
                    f"/tasks/{task_id}/stage/table/total",
                    f"/tasks/{task_id}/stage/table/missing_msg",
                    f"/tasks/{task_id}/stage/table/dash-table",
                ]
                if input_version > dp['input_version', 'data']:
                    # db has newer data, fetch it also
                    keys.extend((
                        f"/tasks/{task_id}/request/proteins",
                        f"/tasks/{task_id}/request/dropdown1",
                        f"/tasks/{task_id}/request/dropdown2",
                    ))
                pipe.multi()
                pipe.set(f"/tasks/{task_id}/accessed", int(time.time()))
                pipe.mget(*keys)
                exec_res = pipe.execute()[-1]
                status, msg, current, total, missing_msg, table_data, *extra = exec_res
                status, msg, missing_msg = decode_str(status, msg, missing_msg)
                current, total = decode_int(current, total)

                if extra:
                    proteins, dropdown, dropdown2 = decode_str(*extra)
                    if input_version:
                        dp['input_version', 'data'] = input_version
                    if proteins:
                        dp['uniprotAC', 'value'] = proteins
                    if dropdown:
                        dp['dropdown', 'value'] = dropdown
                    if dropdown2:
                        dp['dropdown2', 'value'] = dropdown2
                break
            except redis.WatchError:
                continue

    output = []
    if missing_msg:
        output.append(
            dbc.Row(
                dbc.Col(
                    dbc.Alert(
                        f"Unknown proteins: {missing_msg[:-2]}",
                        className="alert-warning",
                    ),
                    md=8, lg=6,
                ),
                justify='center',
            ),
        )
    if status in ('Enqueued', 'Executing', 'Error'):
        output.append(display_progress(status, total, current, msg))
    elif status == 'Done':
        data = json.loads(table_data)
        output.append(
            dbc.Row(dbc.Col(
                html.Div(
                    dash_table.DataTable(**data, filter_action="native"),
                    style={"overflow-x": "scroll"},
                    className="pb-3",
                ),
                md=12,
            ),
            justify='center',
        ))

    dp['submit-button2', 'disabled'] = status != 'Done'
    dp['table-progress-updater', 'disabled'] = (status not in ('Enqueued', 'Executing'))
    dp['output_row', 'children'] = html.Div(children=output)


def launch_task(stage:str, task_id:str, version:int):
    should_launch_task = True
    # Trying to set status to enqueued if the task isn't already running
    with user.db.pipeline(transaction=True) as pipe:
        while True:
            try:
                pipe.watch(f"/tasks/{task_id}/stage/{stage}/status")
                if should_launch_task:
                    celery_app.signature(
                        f'tasks.build_{stage}',
                        args=(task_id, version)
                    ).apply_async()
                    should_launch_task = False

                status = pipe.get(f"/tasks/{task_id}/stage/{stage}/status")
                if status is not None:
                    # Task has already modified it to something else, so we are not enqueued
                    break
                # Task is still in the queue
                pipe.multi()
                pipe.mset({
                    f"/tasks/{task_id}/stage/{stage}/status": 'Enqueued',
                    f"/tasks/{task_id}/stage/{stage}/total": -3,
                })
                pipe.incr("/queueinfo/cur_id")
                pipe.execute_command("COPY", "/queueinfo/cur_id", f"/tasks/{task_id}/stage/{stage}/current", "REPLACE")
                pipe.execute()
                break
            except redis.WatchError:
                continue


@dash_app.callback(
    Output('progress-updater-2', 'disabled'),
    Input('sparql-working', 'data'),
    Input('heatmap-working', 'data'),
    Input('tree-working', 'data'),
)
def updater_controller(*is_working):
    # While there are tasks - keep updater running
    return not any(is_working)

@DashProxy.callback(
    Output('graphics-version', 'data'), # trigger to launch rendering
    Output('sparql-output-container', 'children'),
    Output('sparql-working', 'data'),
    Output('dropdown2', 'value'),
    Output('input2-version', 'data'),

    Input('submit-button2', 'n_clicks'),
    Input('submit-button2', 'disabled'),
    Input('progress-updater-2', 'n_intervals'),

    State('task_id', 'data'),
    State('dropdown2', 'value'),
    State('input2-version', 'data')
)
def start_heatmap_and_tree(dp:DashProxy):
    # if dp.first_load:
    #     return

    stage = 'sparql'
    task_id = dp['task_id', 'data']

    if ('submit-button2', 'disabled') in dp.triggered:
        if dp['submit-button2', 'disabled']:
            # First stage data was changed, clear the current data
            dp['sparql-output-container', 'children'] = None
            # hide the data
            dp['graphics-version', 'data'] = 0
            # Stop running tasks
            user.db.incr(f"/tasks/{task_id}/stage/{stage}/version")
            return

    if ('submit-button2', 'n_clicks') in dp.triggered:
        if dp['submit-button2', 'disabled']:
            # button was pressed in disabled state??
            return
        # button press triggered

        # hide the data
        dp['graphics-version', 'data'] = 0

        with user.db.pipeline(transaction=True) as pipe:
            # Stops running tasks
            pipe.incr(f"/tasks/{task_id}/stage/{stage}/version")
            pipe.incr(f"/tasks/{task_id}/stage/heatmap/version")
            pipe.incr(f"/tasks/{task_id}/stage/tree/version")
            pipe.mset({
                f"/tasks/{task_id}/stage/heatmap/status": "Waiting",
                f"/tasks/{task_id}/stage/tree/status": "Waiting",
                f"/tasks/{task_id}/request/dropdown2": dp['dropdown2', 'value'],
            })
            # Remove the data
            pipe.unlink(
                f"/tasks/{task_id}/stage/{stage}/message",
                f"/tasks/{task_id}/stage/{stage}/status",
                f"/tasks/{task_id}/stage/heatmap/message",
                f"/tasks/{task_id}/stage/tree/message",
            )
            sparql_ver = pipe.execute()[0]
        dp['input2-version', 'data'] = sparql_ver
        # when sparql is done it will trigger the heatmap and tree tasks
        celery_app.signature(
            'tasks.SPARQLWrapper',
            args=(task_id, sparql_ver)
        ).apply_async()

        # Trying to set status to enqueued if the task isn't already running
        with user.db.pipeline(transaction=True) as pipe:
            while True:
                try:
                    pipe.watch(f"/tasks/{task_id}/stage/{stage}/status")
                    status = pipe.get(f"/tasks/{task_id}/stage/{stage}/status")
                    if status is not None:
                        # Task has already modified it to something else, so we are not enqueued
                        break
                    # Task is still in the queue
                    pipe.multi()
                    pipe.mset({
                        f"/tasks/{task_id}/stage/{stage}/status": 'Enqueued',
                        f"/tasks/{task_id}/stage/{stage}/total": -3,
                    })
                    pipe.incr("/queueinfo/cur_id")
                    pipe.execute_command("COPY", "/queueinfo/cur_id", f"/tasks/{task_id}/stage/{stage}/current", "REPLACE")
                    pipe.execute()
                    break
                except redis.WatchError:
                    continue

    # fill the output row
    # here because of "go" click, first launch or interval refresh
    version, status, msg, current, total, input_version = user.db.mget(
        f"/tasks/{task_id}/stage/{stage}/version",
        f"/tasks/{task_id}/stage/{stage}/status",
        f"/tasks/{task_id}/stage/{stage}/message",
        f"/tasks/{task_id}/stage/{stage}/current",
        f"/tasks/{task_id}/stage/{stage}/total",
        f"/tasks/{task_id}/stage/{stage}/input2-version"
    )

    status, msg = decode_str(status, msg)
    version, current, total, input_version = decode_int(version, current, total, input_version)

    if input_version > dp['input2-version', 'data']:
        input_val, input_version = user.db.mget(
            f"/tasks/{task_id}/request/dropdown2",
            f"/tasks/{task_id}/stage/{stage}/input2-version"
        )
        dp['input2-version', 'data'] = decode_int(input_version)
        dp['dropdown2', 'value'] = decode_str(input_val)

    dp[f'{stage}-working', 'data'] = status in ('Enqueued', 'Executing')

    if status in ('Enqueued', 'Executing', 'Error'):
        dp[f'{stage}-output-container', 'children'] = display_progress(status, total, current, msg)
    elif status == 'Done':
        dp[f'{stage}-output-container', 'children'] = None
        dp['graphics-version', 'data'] = version

def process_heatmap_or_tree(stage, dp:DashProxy):
    """Display progress bar, returns task_id and version if we need to render tree/heatmap"""
    if ('progress-updater-2', 'n_intervals') in dp.triggered and len (dp.triggered) == 1:
        # timer-only trigger
        if not dp[f'{stage}-working', 'data']:
            # no need for progress bar, ignore update
            return

    task_id = dp['task_id', 'data']

    if dp['graphics-version', 'data'] == 0:
        if ('graphics-version', 'data') in dp.triggered:
            dp[f'{stage}-output-container', 'children'] = None
            user.db.incr(f"/tasks/{task_id}/stage/{stage}/version")
        return

    # fill the output row
    # here because of sparql finish, first launch or interval refresh
    status, msg, current, total, version = user.db.mget(
        f"/tasks/{task_id}/stage/{stage}/status",
        f"/tasks/{task_id}/stage/{stage}/message",
        f"/tasks/{task_id}/stage/{stage}/current",
        f"/tasks/{task_id}/stage/{stage}/total",
        f"/tasks/{task_id}/stage/{stage}/version"
    ) # TODO: version first == bug
    status, msg = decode_str(status, msg)
    version, current, total = decode_int(version, current, total)

    dp[f'{stage}-working', 'data'] = status in ('Enqueued', 'Executing')
    if status in ('Enqueued', 'Executing', 'Error'):
        dp[f'{stage}-output-container', 'children'] = display_progress(status, total, current, msg)
    elif status == 'Done':
        return task_id, version

@DashProxy.callback(
    Output('heatmap-output-container', 'children'),
    Output('heatmap-working', 'data'),

    Input('progress-updater-2', 'n_intervals'),
    Input('graphics-version', 'data'),

    State('heatmap-working', 'data'),
    State('task_id', 'data'),
)
def heatmap(dp:DashProxy):
    res = process_heatmap_or_tree('heatmap', dp)
    if not res:
        return

    task_id, version = res
    dp[f'heatmap-output-container', 'children'] = dbc.Row(
        dbc.Col(
            html.A(
                html.Img(
                    src=f'/files/{task_id}/Correlation.png?version={version}',
                    style={
                        'width': '100%',
                        'max-width': '1100px',
                    },
                    className="mx-auto",
                ),
                href=f'/files/{task_id}/Correlation.png?version={version}',
                target="_blank",
                className="mx-auto",
            ),
            className="text-center",
        ),
        className="mx-4",
    )

@DashProxy.callback(
    Output('tree-output-container', 'children'),
    Output('tree-working', 'data'),

    Input('progress-updater-2', 'n_intervals'),
    Input('graphics-version', 'data'),

    State('tree-working', 'data'),
    State('task_id', 'data'),
)
def tree(dp:DashProxy):
    res = process_heatmap_or_tree('tree', dp)
    if not res:
        return

    task_id, version = res
    dp[f'tree-output-container', 'children'] = dbc.Row(
        dbc.Col(
            PhydthreeComponent(
                url=f'/files/{task_id}/cluser.xml?nocache={version}',
                height=2000,
            ),
            className="mx-5 mt-3",
        )
    )


@dash_app.server.route('/files/<task_id>/<name>')
def serve_user_file(task_id, name):
    # uid = decode_str(user.db.get(f"/tasks/{task_id}/user_id"))
    # if flask.session.get("USER_ID", '') != uid:
    #     flask.abort(403)
    response = flask.make_response(flask.send_from_directory(f"/app/user_data/{task_id}", name))
    if name.lower().endswith(".xml"):
        response.mimetype = "text/xml"

    return response