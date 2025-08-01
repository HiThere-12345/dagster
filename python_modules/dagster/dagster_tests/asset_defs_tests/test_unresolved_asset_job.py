import hashlib

import dagster as dg
import pytest
from dagster import (
    AssetKey,
    AssetsDefinition,
    AssetSelection,
    BackfillPolicy,
    DagsterEventType,
    in_process_executor,
)
from dagster._core.storage.tags import PARTITION_NAME_TAG
from dagster._core.test_utils import create_test_asset_job


def _all_asset_keys(result):
    mats = [
        event.event_specific_data.materialization
        for event in result.all_events
        if event.event_type_value == "ASSET_MATERIALIZATION"
    ]
    ret = {mat.asset_key for mat in mats}
    assert len(mats) == len(ret)
    return ret


def asset_aware_io_manager():
    class MyIOManager(dg.IOManager):
        def __init__(self):
            self.db = {}

        def handle_output(self, context, obj):
            self.db[context.asset_key] = obj

        def load_input(self, context):
            return self.db.get(context.asset_key)

    io_manager_obj = MyIOManager()

    @dg.io_manager
    def _asset_aware():
        return io_manager_obj

    return io_manager_obj, _asset_aware


def _get_assets_defs(use_multi: bool = False, allow_subset: bool = False):
    """Get a predefined set of assets for testing.

    Dependencies:
        "upstream": {
            "start": set(),
            "a": {"start"},
            "b": set(),
            "c": {"b"},
            "d": {"a", "b"},
            "e": {"c"},
            "f": {"e", "d"},
            "final": {"a", "d"},
        },
        "downstream": {
            "start": {"a"},
            "b": {"c", "d"},
            "a": {"final", "d"},
            "c": {"e"},
            "d": {"final", "f"},
            "e": {"f"},
        }
    """

    @dg.asset
    def start():
        return 1

    @dg.asset
    def a(start):
        return start + 1

    @dg.asset
    def b():
        return 1

    @dg.asset
    def c(b):
        return b + 1

    @dg.multi_asset(
        outs={
            "a": dg.AssetOut(is_required=False),
            "b": dg.AssetOut(is_required=False),
            "c": dg.AssetOut(is_required=False),
        },
        internal_asset_deps={
            "a": {dg.AssetKey("start")},
            "b": set(),
            "c": {dg.AssetKey("b")},
        },
        can_subset=allow_subset,
    )
    def abc_(context, start):
        a = (start + 1) if start else None
        b = 1
        c = b + 1
        out_values = {"a": a, "b": b, "c": c}
        outputs_to_return = (
            sorted(context.op_execution_context.selected_output_names) if allow_subset else "abc"
        )
        for output_name in outputs_to_return:
            yield dg.Output(out_values[output_name], output_name)

    @dg.asset
    def d(a, b):
        return a + b

    @dg.asset
    def e(c):
        return c + 1

    @dg.asset
    def f(d, e):
        return d + e

    @dg.multi_asset(
        outs={
            "d": dg.AssetOut(is_required=False),
            "e": dg.AssetOut(is_required=False),
            "f": dg.AssetOut(is_required=False),
        },
        internal_asset_deps={
            "d": {dg.AssetKey("a"), dg.AssetKey("b")},
            "e": {dg.AssetKey("c")},
            "f": {dg.AssetKey("d"), dg.AssetKey("e")},
        },
        can_subset=allow_subset,
    )
    def def_(context, a, b, c):
        d = (a + b) if a and b else None
        e = (c + 1) if c else None
        f = (d + e) if d and e else None
        out_values = {"d": d, "e": e, "f": f}
        outputs_to_return = (
            sorted(context.op_execution_context.selected_output_names) if allow_subset else "def"
        )
        for output_name in outputs_to_return:
            yield dg.Output(out_values[output_name], output_name)

    @dg.asset
    def final(a, d):
        return a + d

    if use_multi:
        return [start, abc_, def_, final]
    return [start, a, b, c, d, e, f, final]


@pytest.mark.parametrize(
    "job_selection,use_multi,expected_error",
    [
        ("*", False, None),
        ("*", True, None),
        ("e", False, None),
        ("e", True, (dg.DagsterInvalidSubsetError, "")),
        (
            "x",
            False,
            (dg.DagsterInvalidSubsetError, r"were selected"),
        ),
        (
            "x",
            True,
            (dg.DagsterInvalidSubsetError, r"were selected"),
        ),
        (
            ["start", "x"],
            False,
            (dg.DagsterInvalidSubsetError, r"were selected"),
        ),
        (
            ["start", "x"],
            True,
            (dg.DagsterInvalidSubsetError, r"were selected"),
        ),
        (["d", "e", "f"], False, None),
        (["d", "e", "f"], True, None),
        (["start+"], False, None),
        (
            ["start+"],
            True,
            (
                dg.DagsterInvalidSubsetError,
                r"When building job, the AssetsDefinition 'abc_' contains asset keys "
                r"\[AssetKey\(\['a'\]\), AssetKey\(\['b'\]\), AssetKey\(\['c'\]\)\] and check keys \[\], but"
                r" attempted to "
                r"select only assets \[AssetKey\(\['a'\]\)\] and checks \[\]",
            ),
        ),
    ],
)
def test_resolve_subset_job_errors(job_selection, use_multi, expected_error):
    if expected_error:
        expected_class, expected_message = expected_error
        with pytest.raises(expected_class, match=expected_message):
            create_test_asset_job(_get_assets_defs(use_multi), selection=job_selection)
    else:
        assert create_test_asset_job(_get_assets_defs(use_multi), selection=job_selection)


@pytest.mark.parametrize(
    "job_selection,expected_assets",
    [
        (None, "a,b,c"),
        ("a+", "a,b"),
        ("+c", "b,c"),
        (["a", "c"], "a,c"),
        (AssetSelection.assets("a", "c") | AssetSelection.assets("c", "b"), "a,b,c"),
    ],
)
def test_simple_graph_backed_asset_subset(job_selection, expected_assets):
    @dg.op
    def one():
        return 1

    @dg.op
    def add_one(x):
        return x + 1

    @dg.op(out=dg.Out(io_manager_key="asset_io_manager"))
    def create_asset(x):
        return x * 2

    @dg.graph
    def a():
        return create_asset(add_one(add_one(one())))

    @dg.graph
    def b(a):
        return create_asset(add_one(add_one(a)))

    @dg.graph
    def c(b):
        return create_asset(add_one(add_one(b)))

    a_asset = AssetsDefinition.from_graph(a)
    b_asset = AssetsDefinition.from_graph(b)
    c_asset = AssetsDefinition.from_graph(c)

    _, io_manager_def = asset_aware_io_manager()
    final_assets = dg.with_resources(
        [a_asset, b_asset, c_asset], {"asset_io_manager": io_manager_def}
    )

    # run once so values exist to load from
    create_test_asset_job(final_assets).execute_in_process()

    # now build the subset job
    job = create_test_asset_job(final_assets, selection=job_selection)

    result = job.execute_in_process()

    expected_asset_keys = set(dg.AssetKey(a) for a in expected_assets.split(","))

    # make sure we've generated the correct set of keys
    assert _all_asset_keys(result) == expected_asset_keys

    if dg.AssetKey("a") in expected_asset_keys:
        # (1 + 1 + 1) * 2
        assert result.output_for_node("a.create_asset") == 6
    if dg.AssetKey("b") in expected_asset_keys:
        # (6 + 1 + 1) * 8
        assert result.output_for_node("b.create_asset") == 16
    if dg.AssetKey("c") in expected_asset_keys:
        # (16 + 1 + 1) * 2
        assert result.output_for_node("c.create_asset") == 36


@pytest.mark.parametrize("use_multi", [True, False])
@pytest.mark.parametrize(
    "job_selection,expected_assets,prefixes",
    [
        ("*", "start,a,b,c,d,e,f,final", None),
        ("a", "a", None),
        ("b+", "b,c,d", None),
        ("+f", "f,d,e", None),
        ("++f", "f,d,e,c,a,b", None),
        ("start*", "start,a,d,f,final", None),
        (["+a", "b+"], "start,a,b,c,d", None),
        (["*c", "final"], "b,c,final", None),
        ("*", "start,a,b,c,d,e,f,final", ["core", "models"]),
        ("core/models/a", "a", ["core", "models"]),
        ("core/models/b+", "b,c,d", ["core", "models"]),
        ("+core/models/f", "f,d,e", ["core", "models"]),
        ("++core/models/f", "f,d,e,c,a,b", ["core", "models"]),
        ("core/models/start*", "start,a,d,f,final", ["core", "models"]),
        (["+core/models/a", "core/models/b+"], "start,a,b,c,d", ["core", "models"]),
        (["*core/models/c", "core/models/final"], "b,c,final", ["core", "models"]),
        (AssetSelection.all(), "start,a,b,c,d,e,f,final", None),
        (AssetSelection.assets("a", "b", "c"), "a,b,c", None),
        (AssetSelection.assets("f").upstream(depth=1), "f,d,e", None),
        (AssetSelection.assets("f").upstream(depth=2), "f,d,e,c,a,b", None),
        (AssetSelection.assets("start").downstream(), "start,a,d,f,final", None),
        (
            AssetSelection.assets("a").upstream(depth=1)
            | AssetSelection.assets("b").downstream(depth=1),
            "start,a,b,c,d",
            None,
        ),
        (
            AssetSelection.assets("c").upstream() | AssetSelection.assets("final"),
            "b,c,final",
            None,
        ),
        (AssetSelection.all(), "start,a,b,c,d,e,f,final", ["core", "models"]),
        (
            AssetSelection.assets("core/models/a").upstream(depth=1)
            | AssetSelection.assets("core/models/b").downstream(depth=1),
            "start,a,b,c,d",
            ["core", "models"],
        ),
        (
            [
                AssetKey.from_user_string("core/models/a"),
                AssetKey.from_user_string("core/models/b"),
            ],
            "a,b",
            ["core", "models"],
        ),
    ],
)
def test_define_selection_job(job_selection, expected_assets, use_multi, prefixes):
    _, io_manager_def = asset_aware_io_manager()
    # for these, if we have multi assets, we'll always allow them to be subset
    prefixed_assets = _get_assets_defs(use_multi=use_multi, allow_subset=use_multi)
    # apply prefixes
    for prefix in reversed(prefixes or []):
        prefixed_assets = [
            assets_def.with_attributes(
                asset_key_replacements={
                    key: key.with_prefix(prefix)
                    for key in set(assets_def.keys_by_input_name.values()) | set(assets_def.keys)
                },
            )
            for assets_def in prefixed_assets
        ]

    final_assets = dg.with_resources(
        prefixed_assets,
        resource_defs={"io_manager": io_manager_def},
    )

    # run once so values exist to load from
    create_test_asset_job(final_assets).execute_in_process()

    # now build the subset job
    job = create_test_asset_job(final_assets, selection=job_selection)
    with dg.instance_for_test() as instance:
        result = job.execute_in_process(instance=instance)
        planned_asset_keys = {
            record.event_log_entry.dagster_event.event_specific_data.asset_key  # pyright: ignore[reportAttributeAccessIssue,reportOptionalMemberAccess]
            for record in instance.get_records_for_run(
                run_id=result.run_id,
                of_type=DagsterEventType.ASSET_MATERIALIZATION_PLANNED,
            ).records
        }

    expected_asset_keys = set(
        dg.AssetKey([*(prefixes or []), a]) for a in expected_assets.split(",")
    )
    # make sure we've planned on the correct set of keys
    assert planned_asset_keys == expected_asset_keys

    # make sure we've generated the correct set of keys
    assert _all_asset_keys(result) == expected_asset_keys

    if use_multi:
        expected_outputs = {
            "start": 1,
            "abc_.a": 2,
            "abc_.b": 1,
            "abc_.c": 2,
            "def_.d": 3,
            "def_.e": 3,
            "def_.f": 6,
            "final": 5,
        }
    else:
        expected_outputs = {
            "start": 1,
            "a": 2,
            "b": 1,
            "c": 2,
            "d": 3,
            "e": 3,
            "f": 6,
            "final": 5,
        }

    # check if the output values are as we expect
    for output, value in expected_outputs.items():
        asset_name = output.split(".")[-1]
        if asset_name in expected_assets.split(","):
            # dealing with multi asset
            if output != asset_name:
                node_def_name = output.split(".")[0]
                keys_for_node = {dg.AssetKey([*(prefixes or []), c]) for c in node_def_name[:-1]}
                selected_keys_for_node = keys_for_node.intersection(expected_asset_keys)
                if (
                    selected_keys_for_node != keys_for_node
                    # too much of a pain to explicitly encode the cases where we need to create a
                    # new node definition
                    and not result.job_def.has_node_named(node_def_name)
                ):
                    node_def_name += (
                        "_subset_"
                        + hashlib.md5(
                            (str(list(sorted(selected_keys_for_node)))).encode()
                        ).hexdigest()[-5:]
                    )
                assert result.output_for_node(node_def_name, asset_name)
            # dealing with regular asset
            else:
                assert result.output_for_node(output, "result") == value


def test_define_selection_job_assets_definition_selection():
    @dg.asset
    def asset1(): ...

    @dg.asset
    def asset2(): ...

    @dg.asset
    def asset3(): ...

    all_assets = [asset1, asset2, asset3]

    job1 = create_test_asset_job(all_assets, selection=[asset1, asset2])
    asset_keys = list(job1.asset_layer.executable_asset_keys)
    assert len(asset_keys) == 2
    assert set(asset_keys) == {asset1.key, asset2.key}
    job1.execute_in_process()


def test_root_asset_selection():
    @dg.asset
    def a(source):
        return source + 1

    @dg.asset
    def b(a):
        return a + 1

    # Source asset should not be included in the job
    assert create_test_asset_job([a, b, dg.SourceAsset("source")], selection="*b")


def test_source_asset_selection_missing():
    @dg.asset
    def a(source):
        return source + 1

    @dg.asset
    def b(a):
        return a + 1

    with pytest.raises(dg.DagsterInvalidDefinitionError, match="sources"):
        create_test_asset_job([a, b], selection="*b")


@dg.asset
def foo():
    return 1


def test_executor_def():
    job = create_test_asset_job([foo], executor_def=in_process_executor)
    assert job.executor_def == dg.in_process_executor


def test_tags():
    my_tags = {"foo": "bar"}
    my_run_tags = {"baz": "quux"}
    job = create_test_asset_job([foo], tags=my_tags, run_tags=my_run_tags)
    assert job.tags == my_tags
    assert job.run_tags == my_run_tags


def test_description():
    description = "Some very important description"
    job = create_test_asset_job([foo], description=description)
    assert job.description == description


def _get_partitioned_assets(partitions_def):
    @dg.asset(partitions_def=partitions_def)
    def a():
        return 1

    @dg.asset(partitions_def=partitions_def)
    def b(a):
        return a + 1

    @dg.asset(partitions_def=partitions_def)
    def c(b):
        return b + 1

    return [a, b, c]


def test_config():
    @dg.asset
    def foo():
        return 1

    @dg.asset(config_schema={"val": int})
    def config_asset(context, foo):
        return foo + context.op_execution_context.op_config["val"]

    @dg.asset(config_schema={"val": int})
    def other_config_asset(context, config_asset):
        return config_asset + context.op_execution_context.op_config["val"]

    job = create_test_asset_job(
        [foo, config_asset, other_config_asset],
        config={
            "ops": {
                "config_asset": {"config": {"val": 2}},
                "other_config_asset": {"config": {"val": 3}},
            }
        },
    )

    result = job.execute_in_process()

    assert result.output_for_node("other_config_asset") == 1 + 2 + 3


@pytest.mark.parametrize(
    "selection,config",
    [
        (
            AssetSelection.assets("other_config_asset"),
            {"other_config_asset": {"config": {"val": 3}}},
        ),
        (
            AssetSelection.assets("other_config_asset").upstream(depth=1),
            {
                "config_asset": {"config": {"val": 2}},
                "other_config_asset": {"config": {"val": 3}},
            },
        ),
    ],
)
def test_subselect_config(selection, config):
    @dg.asset(io_manager_key="asset_io_manager")
    def foo():
        return 1

    @dg.asset(config_schema={"val": int}, io_manager_key="asset_io_manager")
    def config_asset(context, foo):
        return foo + context.op_execution_context.op_config["val"]

    @dg.asset(config_schema={"val": int}, io_manager_key="asset_io_manager")
    def other_config_asset(context, config_asset):
        return config_asset + context.op_execution_context.op_config["val"]

    io_manager_obj, io_manager_def = asset_aware_io_manager()
    io_manager_obj.db[dg.AssetKey("foo")] = 1
    io_manager_obj.db[dg.AssetKey("config_asset")] = 1 + 2

    all_assets = dg.with_resources(
        [foo, config_asset, other_config_asset],
        resource_defs={"asset_io_manager": io_manager_def},
    )
    job = create_test_asset_job(all_assets, config={"ops": config}, selection=selection)

    result = job.execute_in_process()

    assert result.output_for_node("other_config_asset") == 1 + 2 + 3


def test_simple_partitions():
    partitions_def = dg.HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = create_test_asset_job(
        _get_partitioned_assets(partitions_def), partitions_def=partitions_def
    )
    assert job.partitions_def == partitions_def


def test_hooks():
    @dg.asset
    def a():
        pass

    @dg.asset
    def b(a):
        pass

    @dg.success_hook
    def foo(_):
        pass

    @dg.failure_hook
    def bar(_):
        pass

    job = create_test_asset_job([a, b], hooks={foo, bar})
    assert job.hook_defs == {foo, bar}


def test_hooks_with_resources():
    @dg.asset
    def a():
        pass

    @dg.asset
    def b(a):
        pass

    @dg.success_hook(required_resource_keys={"a"})
    def foo(_):
        pass

    @dg.failure_hook(required_resource_keys={"b", "c"})
    def bar(_):
        pass

    job = create_test_asset_job([a, b], hooks={foo, bar}, resources={"a": 1, "b": 2, "c": 3})
    assert job.hook_defs == {foo, bar}

    defs = dg.Definitions(
        assets=[a, b],
        jobs=[dg.define_asset_job("with_hooks", hooks={foo, bar})],
        resources={"a": 1, "b": 2, "c": 3},
    )
    assert defs.resolve_job_def("with_hooks").hook_defs == {foo, bar}

    with pytest.raises(
        dg.DagsterInvalidDefinitionError,
        match="resource with key 'c' required by hook 'bar'",
    ):
        defs = dg.Definitions(
            assets=[a, b],
            jobs=[dg.define_asset_job("with_hooks", hooks={foo, bar})],
            resources={"a": 1, "b": 2},
        ).resolve_job_def("with_hooks")


def test_partitioned_schedule():
    partitions_def = dg.HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = dg.define_asset_job("hourly", partitions_def=partitions_def)

    schedule = dg.build_schedule_from_partitioned_job(job)

    spd = schedule.job.partitions_def
    assert spd == partitions_def


def test_partitioned_schedule_on_repo():
    partitions_def = dg.HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = dg.define_asset_job("hourly", partitions_def=partitions_def)

    schedule = dg.build_schedule_from_partitioned_job(job)

    @dg.repository
    def my_repo():
        return [
            job,
            schedule,
            *_get_partitioned_assets(partitions_def),
        ]

    assert my_repo()


def test_intersecting_partitions_on_repo_invalid():
    partitions_def = dg.HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = dg.define_asset_job("hourly", partitions_def=partitions_def)

    schedule = dg.build_schedule_from_partitioned_job(job)

    @dg.asset(partitions_def=dg.DailyPartitionsDefinition(start_date="2020-01-01"))
    def d(c):
        return c

    with pytest.raises(dg.DagsterInvalidDefinitionError, match="must have the same partitions def"):

        @dg.repository
        def my_repo():
            return [
                job,
                schedule,
                *_get_partitioned_assets(partitions_def),
                d,
            ]

        my_repo.get_all_jobs()


def test_intersecting_partitions_on_repo_valid():
    partitions_def = dg.HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    partitions_def2 = dg.DailyPartitionsDefinition(start_date="2020-01-01")
    job = dg.define_asset_job("hourly", partitions_def=partitions_def, selection="a++")
    job2 = dg.define_asset_job("daily", partitions_def=partitions_def2, selection="d")

    schedule = dg.build_schedule_from_partitioned_job(job)
    schedule2 = dg.build_schedule_from_partitioned_job(job2)

    @dg.asset(partitions_def=partitions_def2)
    def d(c):
        return c

    @dg.repository
    def my_repo():
        return [
            job,
            schedule,
            schedule2,
            *_get_partitioned_assets(partitions_def),
            d,
        ]

    assert my_repo


def test_job_run_request():
    partitions_def = dg.StaticPartitionsDefinition(["a", "b", "c", "d"])

    def partition_fn(partition_key: str):
        return {"ops": {"my_asset": {"config": {"partition": partition_key}}}}

    @dg.static_partitioned_config(partition_keys=partitions_def.get_partition_keys())
    def my_partitioned_config(partition_key: str):
        return partition_fn(partition_key)

    @dg.asset
    def my_asset():
        pass

    my_job = dg.define_asset_job(
        "my_job", "*", config=my_partitioned_config, partitions_def=partitions_def
    )

    for partition_key in ["a", "b", "c", "d"]:
        run_request = my_job.run_request_for_partition(partition_key=partition_key, run_key=None)
        assert run_request.run_config == partition_fn(partition_key)
        assert run_request.tags
        assert run_request.tags.get(PARTITION_NAME_TAG) == partition_key
        assert run_request.job_name == my_job.name

        run_request_with_tags = my_job.run_request_for_partition(
            partition_key=partition_key, run_key=None, tags={"foo": "bar"}
        )
        assert run_request_with_tags.run_config == partition_fn(partition_key)
        assert run_request_with_tags.tags
        assert run_request_with_tags.tags.get(PARTITION_NAME_TAG) == partition_key
        assert run_request_with_tags.tags.get("foo") == "bar"

    my_job_hardcoded_config = dg.define_asset_job(
        "my_job_hardcoded_config",
        "*",
        config={"ops": {"my_asset": {"config": {"partition": "blabla"}}}},
        partitions_def=partitions_def,
    )

    run_request = my_job_hardcoded_config.run_request_for_partition(partition_key="a", run_key=None)
    assert run_request.job_name == my_job_hardcoded_config.name
    assert run_request.run_config == {"ops": {"my_asset": {"config": {"partition": "blabla"}}}}
    assert my_job_hardcoded_config.run_request_for_partition(
        partition_key="a", run_config={"a": 5}
    ).run_config == {"a": 5}


def test_job_partitions_def_unpartitioned_assets():
    @dg.asset
    def my_asset():
        pass

    my_job = dg.define_asset_job(
        "my_job", partitions_def=dg.DailyPartitionsDefinition(start_date="2020-01-01")
    )

    @dg.repository
    def my_repo():
        return [my_asset, my_job]


def test_op_retry_policy():
    ops_retry_policy = dg.RetryPolicy(max_retries=2)
    tries = {"a": 0, "b": 0}

    @dg.asset
    def a():
        tries["a"] += 1
        raise Exception()

    @dg.asset(retry_policy=dg.RetryPolicy(max_retries=3))
    def b():
        tries["b"] += 1
        raise Exception()

    job1 = create_test_asset_job([a, b], op_retry_policy=ops_retry_policy)
    assert job1.op_retry_policy == ops_retry_policy
    job1.execute_in_process(raise_on_error=False)

    assert tries == {"a": 3, "b": 4}


def test_backfill_policy():
    partitions_def = dg.StaticPartitionsDefinition(["a", "b", "c", "d"])

    @dg.asset(partitions_def=partitions_def, backfill_policy=BackfillPolicy.single_run())
    def foo(): ...

    @dg.asset(partitions_def=partitions_def, backfill_policy=BackfillPolicy.single_run())
    def bar(): ...

    @dg.asset(partitions_def=partitions_def, backfill_policy=BackfillPolicy.multi_run(2))
    def baz(): ...

    @dg.asset
    def qux(): ...

    @dg.asset(partitions_def=partitions_def)
    def pux(): ...

    assert create_test_asset_job([foo, bar]).backfill_policy == BackfillPolicy.single_run()
    # Unpartitioned assets won't affect backfill policy
    assert create_test_asset_job([qux]).backfill_policy == BackfillPolicy.multi_run(1)
    # Null policy normalized to multi_run(1)
    assert create_test_asset_job([qux]).backfill_policy == BackfillPolicy.multi_run(1)
    assert create_test_asset_job([foo, bar, qux]).backfill_policy == BackfillPolicy.single_run()
    assert create_test_asset_job([baz]).backfill_policy == BackfillPolicy.multi_run(2)

    # different backfill policies-- use minimum max_partitions_per_run
    with pytest.warns(Warning, match="materializes assets with varying BackfillPolicies"):
        assert create_test_asset_job(
            [foo, bar, baz, pux]
        ).backfill_policy == BackfillPolicy.multi_run(1)

    # can't do PartitionedConfig for single-run backfills
    with pytest.raises(dg.DagsterInvalidDefinitionError, match="PartitionedConfig"):

        @dg.static_partitioned_config(partition_keys=partitions_def.get_partition_keys())
        def my_partitioned_config(partition_key: str):
            return {"ops": {"foo": {"config": {"partition": partition_key}}}}

        create_test_asset_job([foo], config=my_partitioned_config)


def test_metadata():
    unresolved = dg.define_asset_job("exmaple", metadata={"foo": "bar", "four": 4})
    assert unresolved.metadata

    blanked = unresolved.with_metadata({})
    assert blanked.metadata == {}

    updated = unresolved.with_metadata({**unresolved.metadata, "foo": "baz"})
    assert updated.metadata
    assert updated.metadata["foo"] == "baz"

    defs = dg.Definitions()
    resolved = updated.resolve(defs.resolve_asset_graph())
    assert resolved.metadata["foo"] == dg.TextMetadataValue("baz")
