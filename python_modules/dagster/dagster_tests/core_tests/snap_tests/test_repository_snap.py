import dagster as dg
from dagster._core.definitions.resource_definition import ResourceDefinition
from dagster._core.execution.context.init import InitResourceContext
from dagster._core.remote_representation import JobDataSnap
from dagster._core.remote_representation.external_data import (
    NestedResource,
    NestedResourceType,
    RepositorySnap,
    ResourceJobUsageEntry,
)
from dagster._core.snap import JobSnap


def test_repository_snap_all_props():
    @dg.op
    def noop_op(_):
        pass

    @dg.job
    def noop_job():
        noop_op()

    @dg.repository
    def noop_repo():
        return [noop_job]

    repo_snap = RepositorySnap.from_def(noop_repo)

    assert repo_snap.name == "noop_repo"
    assert len(repo_snap.job_datas) == 1  # pyright: ignore[reportArgumentType]
    assert isinstance(repo_snap.job_datas[0], JobDataSnap)  # pyright: ignore[reportOptionalSubscript]

    job_snapshot = repo_snap.job_datas[0].job  # pyright: ignore[reportOptionalSubscript]
    assert isinstance(job_snapshot, JobSnap)
    assert job_snapshot.name == "noop_job"
    assert job_snapshot.description is None
    assert job_snapshot.tags == {}


def test_repository_snap_definitions_resources_basic():
    @dg.asset
    def my_asset(foo: dg.ResourceParam[str]):
        pass

    defs = dg.Definitions(
        assets=[my_asset],
        resources={"foo": ResourceDefinition.hardcoded_resource("wrapped")},
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)

    assert len(repo_snap.resources) == 1  # pyright: ignore[reportArgumentType]
    assert repo_snap.resources[0].name == "foo"  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.resources[0].resource_snapshot.name == "foo"  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.resources[0].resource_snapshot.description is None  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.resources[0].configured_values == {}  # pyright: ignore[reportOptionalSubscript]


def test_repository_snap_definitions_resources_nested() -> None:
    class MyInnerResource(dg.ConfigurableResource):
        a_str: str

    class MyOuterResource(dg.ConfigurableResource):
        inner: MyInnerResource

    inner = MyInnerResource(a_str="wrapped")
    defs = dg.Definitions(
        resources={"foo": MyOuterResource(inner=inner)},
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 1

    foo = [data for data in repo_snap.resources if data.name == "foo"]

    assert len(foo) == 1
    assert (
        foo[0].resource_type == "dagster_tests.core_tests.snap_tests.test_repository_snap."
        "test_repository_snap_definitions_resources_nested.<locals>.MyOuterResource"
    )

    assert len(foo[0].nested_resources) == 1
    assert "inner" in foo[0].nested_resources
    assert foo[0].nested_resources["inner"] == NestedResource(
        NestedResourceType.ANONYMOUS, "MyInnerResource"
    )


def test_repository_snap_definitions_resources_nested_top_level() -> None:
    class MyInnerResource(dg.ConfigurableResource):
        a_str: str

    class MyOuterResource(dg.ConfigurableResource):
        inner: MyInnerResource

    inner = MyInnerResource(a_str="wrapped")
    defs = dg.Definitions(
        resources={"foo": MyOuterResource(inner=inner), "inner": inner},
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 2

    foo = [data for data in repo_snap.resources if data.name == "foo"]
    inner = [data for data in repo_snap.resources if data.name == "inner"]

    assert len(foo) == 1
    assert len(inner) == 1

    assert len(foo[0].nested_resources) == 1
    assert "inner" in foo[0].nested_resources
    assert foo[0].nested_resources["inner"] == NestedResource(NestedResourceType.TOP_LEVEL, "inner")
    assert (
        foo[0].resource_type == "dagster_tests.core_tests.snap_tests.test_repository_snap."
        "test_repository_snap_definitions_resources_nested_top_level.<locals>.MyOuterResource"
    )

    assert len(inner[0].parent_resources) == 1
    assert "foo" in inner[0].parent_resources
    assert inner[0].parent_resources["foo"] == "inner"
    assert (
        inner[0].resource_type == "dagster_tests.core_tests.snap_tests.test_repository_snap."
        "test_repository_snap_definitions_resources_nested_top_level.<locals>.MyInnerResource"
    )


def test_repository_snap_definitions_function_style_resources_nested() -> None:
    @dg.resource
    def my_inner_resource() -> str:
        return "foo"

    @dg.resource(required_resource_keys={"inner"})
    def my_outer_resource(context: InitResourceContext) -> str:
        return context.resources.inner + "bar"

    defs = dg.Definitions(
        resources={"foo": my_outer_resource, "inner": my_inner_resource},
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 2

    foo = [data for data in repo_snap.resources if data.name == "foo"]
    inner = [data for data in repo_snap.resources if data.name == "inner"]

    assert len(foo) == 1
    assert len(inner) == 1

    assert len(foo[0].nested_resources) == 1
    assert "inner" in foo[0].nested_resources
    assert foo[0].nested_resources["inner"] == NestedResource(NestedResourceType.TOP_LEVEL, "inner")
    assert (
        foo[0].resource_type
        == "dagster_tests.core_tests.snap_tests.test_repository_snap.my_outer_resource"
    )

    assert len(inner[0].parent_resources) == 1
    assert "foo" in inner[0].parent_resources
    assert inner[0].parent_resources["foo"] == "inner"
    assert (
        inner[0].resource_type
        == "dagster_tests.core_tests.snap_tests.test_repository_snap.my_inner_resource"
    )


def test_repository_snap_definitions_resources_nested_many() -> None:
    class MyInnerResource(dg.ConfigurableResource):
        a_str: str

    class MyOuterResource(dg.ConfigurableResource):
        inner: MyInnerResource

    class MyOutermostResource(dg.ConfigurableResource):
        inner: MyOuterResource

    inner = MyInnerResource(a_str="wrapped")
    outer = MyOuterResource(inner=inner)
    defs = dg.Definitions(
        resources={
            "outermost": MyOutermostResource(inner=outer),
            "outer": outer,
        },
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 2

    outermost = [data for data in repo_snap.resources if data.name == "outermost"]
    assert len(outermost) == 1

    assert len(outermost[0].nested_resources) == 1
    assert "inner" in outermost[0].nested_resources
    assert outermost[0].nested_resources["inner"] == NestedResource(
        NestedResourceType.TOP_LEVEL, "outer"
    )

    outer = [data for data in repo_snap.resources if data.name == "outer"]
    assert len(outer) == 1

    assert len(outer[0].nested_resources) == 1
    assert "inner" in outer[0].nested_resources
    assert outer[0].nested_resources["inner"] == NestedResource(
        NestedResourceType.ANONYMOUS, "MyInnerResource"
    )


def test_repository_snap_definitions_resources_complex():
    class MyStringResource(dg.ConfigurableResource):
        """My description."""

        my_string: str = "bar"

    @dg.asset
    def my_asset(foo: MyStringResource):
        pass

    defs = dg.Definitions(
        assets=[my_asset],
        resources={
            "foo": MyStringResource(
                my_string="baz",
            )
        },
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)

    assert len(repo_snap.resources) == 1  # pyright: ignore[reportArgumentType]
    assert repo_snap.resources[0].name == "foo"  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.resources[0].resource_snapshot.name == "foo"  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.resources[0].resource_snapshot.description == "My description."  # pyright: ignore[reportOptionalSubscript]

    # Ensure we get config snaps for the resource's fields
    assert len(repo_snap.resources[0].config_field_snaps) == 1  # pyright: ignore[reportOptionalSubscript]
    snap = repo_snap.resources[0].config_field_snaps[0]  # pyright: ignore[reportOptionalSubscript]
    assert snap.name == "my_string"
    assert not snap.is_required
    assert snap.default_value_as_json_str == '"bar"'

    # Ensure we get the configured values for the resource
    assert repo_snap.resources[0].configured_values == {  # pyright: ignore[reportOptionalSubscript]
        "my_string": '"baz"',
    }


def test_repository_snap_empty():
    @dg.repository
    def empty_repo():
        return []

    repo_snap = RepositorySnap.from_def(empty_repo)
    assert repo_snap.name == "empty_repo"
    assert len(repo_snap.job_datas) == 0  # pyright: ignore[reportArgumentType]
    assert len(repo_snap.resources) == 0  # pyright: ignore[reportArgumentType]


def test_repository_snap_definitions_env_vars() -> None:
    class MyStringResource(dg.ConfigurableResource):
        my_string: str

    class MyInnerResource(dg.ConfigurableResource):
        my_string: str

    class MyOuterResource(dg.ConfigurableResource):
        inner: MyInnerResource

    class MyInnerConfig(dg.Config):
        my_string: str

    class MyDataStructureResource(dg.ConfigurableResource):
        str_list: list[str]
        str_dict: dict[str, str]

    class MyResourceWithConfig(dg.ConfigurableResource):
        config: MyInnerConfig
        config_list: list[MyInnerConfig]

    @dg.asset
    def my_asset(foo: MyStringResource):
        pass

    defs = dg.Definitions(
        assets=[my_asset],
        resources={
            "foo": MyStringResource(
                my_string=dg.EnvVar("MY_STRING"),
            ),
            "bar": MyStringResource(
                my_string=dg.EnvVar("MY_STRING"),
            ),
            "baz": MyStringResource(
                my_string=dg.EnvVar("MY_OTHER_STRING"),
            ),
            "qux": MyOuterResource(
                inner=MyInnerResource(
                    my_string=dg.EnvVar("MY_INNER_STRING"),
                ),
            ),
            "quux": MyDataStructureResource(
                str_list=[dg.EnvVar("MY_STRING")],  # type: ignore[arg-type]
                str_dict={"foo": dg.EnvVar("MY_STRING"), "bar": dg.EnvVar("MY_OTHER_STRING")},  # type: ignore
            ),
            "quuz": MyResourceWithConfig(
                config=MyInnerConfig(
                    my_string=dg.EnvVar("MY_CONFIG_NESTED_STRING"),
                ),
                config_list=[
                    MyInnerConfig(
                        my_string=dg.EnvVar("MY_CONFIG_LIST_NESTED_STRING"),
                    )
                ],
            ),
        },
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.utilized_env_vars

    env_vars = dict(repo_snap.utilized_env_vars)

    assert len(env_vars) == 5
    assert "MY_STRING" in env_vars
    assert {consumer.name for consumer in env_vars["MY_STRING"]} == {"foo", "bar", "quux"}
    assert "MY_OTHER_STRING" in env_vars
    assert {consumer.name for consumer in env_vars["MY_OTHER_STRING"]} == {"baz", "quux"}
    assert "MY_INNER_STRING" in env_vars
    assert {consumer.name for consumer in env_vars["MY_INNER_STRING"]} == {"qux"}
    assert "MY_CONFIG_NESTED_STRING" in env_vars
    assert {consumer.name for consumer in env_vars["MY_CONFIG_NESTED_STRING"]} == {"quuz"}
    assert "MY_CONFIG_LIST_NESTED_STRING" in env_vars
    assert {consumer.name for consumer in env_vars["MY_CONFIG_LIST_NESTED_STRING"]} == {"quuz"}


def test_repository_snap_definitions_resources_assets_usage() -> None:
    class MyResource(dg.ConfigurableResource):
        a_str: str

    @dg.asset
    def my_asset(foo: MyResource):
        pass

    @dg.asset
    def my_other_asset(foo: MyResource, bar: MyResource):
        pass

    @dg.asset
    def my_third_asset():
        pass

    defs = dg.Definitions(
        assets=[my_asset, my_other_asset, my_third_asset],
        resources={
            "foo": MyResource(a_str="foo"),
            "bar": MyResource(a_str="bar"),
            "baz": MyResource(a_str="baz"),
        },
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 3

    foo = [data for data in repo_snap.resources if data.name == "foo"]
    assert len(foo) == 1

    assert sorted(foo[0].asset_keys_using, key=lambda k: "".join(k.path)) == [
        dg.AssetKey("my_asset"),
        dg.AssetKey("my_other_asset"),
    ]

    bar = [data for data in repo_snap.resources if data.name == "bar"]
    assert len(bar) == 1

    assert bar[0].asset_keys_using == [
        dg.AssetKey("my_other_asset"),
    ]

    baz = [data for data in repo_snap.resources if data.name == "baz"]
    assert len(baz) == 1

    assert baz[0].asset_keys_using == []


def test_repository_snap_definitions_function_style_resources_assets_usage() -> None:
    @dg.resource
    def my_resource() -> str:
        return "foo"

    @dg.asset
    def my_asset(foo: dg.ResourceParam[str]):
        pass

    @dg.asset
    def my_other_asset(foo: dg.ResourceParam[str]):
        pass

    @dg.asset
    def my_third_asset():
        pass

    defs = dg.Definitions(
        assets=[my_asset, my_other_asset, my_third_asset],
        resources={"foo": my_resource},
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 1

    foo = repo_snap.resources[0]

    assert sorted(foo.asset_keys_using, key=lambda k: "".join(k.path)) == [
        dg.AssetKey("my_asset"),
        dg.AssetKey("my_other_asset"),
    ]


def _to_dict(entries: list[ResourceJobUsageEntry]) -> dict[str, list[str]]:
    return {
        entry.job_name: sorted([str(handle) for handle in entry.node_handles]) for entry in entries
    }


def test_repository_snap_definitions_resources_job_op_usage() -> None:
    class MyResource(dg.ConfigurableResource):
        a_str: str

    @dg.op
    def my_op(foo: MyResource):
        pass

    @dg.op
    def my_other_op(foo: MyResource, bar: MyResource):
        pass

    @dg.op
    def my_third_op():
        pass

    @dg.op
    def my_op_in_other_job(foo: MyResource):
        pass

    @dg.job
    def my_first_job() -> None:
        my_op()
        my_other_op()
        my_third_op()

    @dg.job
    def my_second_job() -> None:
        my_op_in_other_job()
        my_op_in_other_job()

    defs = dg.Definitions(
        jobs=[my_first_job, my_second_job],
        resources={"foo": MyResource(a_str="foo"), "bar": MyResource(a_str="bar")},
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 2

    foo = [data for data in repo_snap.resources if data.name == "foo"]
    assert len(foo) == 1

    assert _to_dict(foo[0].job_ops_using) == {
        "my_first_job": ["my_op", "my_other_op"],
        # There's two of these because the same op is used twice in the same job
        "my_second_job": ["my_op_in_other_job", "my_op_in_other_job_2"],
    }

    bar = [data for data in repo_snap.resources if data.name == "bar"]
    assert len(bar) == 1

    assert _to_dict(bar[0].job_ops_using) == {
        "my_first_job": ["my_other_op"],
    }


def test_repository_snap_definitions_resources_job_op_usage_graph() -> None:
    class MyResource(dg.ConfigurableResource):
        a_str: str

    @dg.op
    def my_op(foo: MyResource):
        pass

    @dg.op
    def my_other_op(foo: MyResource, bar: MyResource):
        pass

    @dg.graph
    def my_graph():
        my_op()
        my_other_op()

    @dg.op
    def my_third_op(foo: MyResource):
        pass

    @dg.graph
    def my_other_graph():
        my_third_op()

    @dg.job
    def my_job() -> None:
        my_graph()
        my_other_graph()
        my_op()
        my_op()

    defs = dg.Definitions(
        jobs=[my_job],
        resources={"foo": MyResource(a_str="foo"), "bar": MyResource(a_str="bar")},
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 2

    foo = [data for data in repo_snap.resources if data.name == "foo"]
    assert len(foo) == 1

    assert _to_dict(foo[0].job_ops_using) == {
        "my_job": [
            "my_graph.my_op",
            "my_graph.my_other_op",
            "my_op",
            "my_op_2",
            "my_other_graph.my_third_op",
        ]
    }

    bar = [data for data in repo_snap.resources if data.name == "bar"]
    assert len(bar) == 1

    assert _to_dict(bar[0].job_ops_using) == {"my_job": ["my_graph.my_other_op"]}


def test_asset_check():
    @dg.asset
    def my_asset():
        pass

    @dg.asset_check(asset=my_asset)  # pyright: ignore[reportArgumentType]
    def my_asset_check(): ...

    @dg.asset_check(asset=my_asset)  # pyright: ignore[reportArgumentType]
    def my_asset_check_2(): ...

    defs = dg.Definitions(
        assets=[my_asset],
        asset_checks=[my_asset_check, my_asset_check_2],
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)

    assert len(repo_snap.asset_check_nodes) == 2  # pyright: ignore[reportArgumentType]
    assert repo_snap.asset_check_nodes[0].name == "my_asset_check"  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.asset_check_nodes[1].name == "my_asset_check_2"  # pyright: ignore[reportOptionalSubscript]


def test_asset_check_in_asset_op():
    @dg.asset(
        check_specs=[
            dg.AssetCheckSpec(name="my_other_asset_check", asset="my_asset"),
            dg.AssetCheckSpec(name="my_other_asset_check_2", asset="my_asset"),
        ]
    )
    def my_asset():
        pass

    @dg.asset_check(asset=my_asset)  # pyright: ignore[reportArgumentType]
    def my_asset_check(): ...

    defs = dg.Definitions(
        assets=[my_asset],
        asset_checks=[my_asset_check],
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)

    assert len(repo_snap.asset_check_nodes) == 3  # pyright: ignore[reportArgumentType]
    assert repo_snap.asset_check_nodes[0].name == "my_asset_check"  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.asset_check_nodes[1].name == "my_other_asset_check"  # pyright: ignore[reportOptionalSubscript]
    assert repo_snap.asset_check_nodes[2].name == "my_other_asset_check_2"  # pyright: ignore[reportOptionalSubscript]


def test_asset_check_multiple_jobs():
    @dg.asset(
        check_specs=[
            dg.AssetCheckSpec(name="my_other_asset_check", asset="my_asset"),
        ]
    )
    def my_asset():
        pass

    @dg.asset_check(asset=my_asset)  # pyright: ignore[reportArgumentType]
    def my_asset_check(): ...

    my_job = dg.define_asset_job("my_job", [my_asset])

    defs = dg.Definitions(
        assets=[my_asset],
        asset_checks=[my_asset_check],
        jobs=[my_job],
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.asset_check_nodes
    assert len(repo_snap.asset_check_nodes) == 2
    assert repo_snap.asset_check_nodes[0].name == "my_asset_check"
    assert repo_snap.asset_check_nodes[1].name == "my_other_asset_check"
    assert repo_snap.asset_check_nodes[0].job_names == ["__ASSET_JOB", "my_job"]
    assert repo_snap.asset_check_nodes[1].job_names == ["__ASSET_JOB", "my_job"]


def test_asset_check_multi_asset():
    @dg.multi_asset(
        outs={
            "a": dg.AssetOut(is_required=False),
            "b": dg.AssetOut(is_required=False),
        },
        check_specs=[dg.AssetCheckSpec(name="check_1", asset="a")],
    )
    def my_multi_asset():
        pass

    defs = dg.Definitions(assets=[my_multi_asset])

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.asset_check_nodes
    assert len(repo_snap.asset_check_nodes) == 1
    assert repo_snap.asset_check_nodes[0].name == "check_1"
    assert repo_snap.asset_check_nodes[0].job_names == ["__ASSET_JOB"]


def test_repository_snap_definitions_resources_job_schedule_sensor_usage():
    class MyResource(dg.ConfigurableResource):
        a_str: str

    @dg.asset
    def my_asset(foo: MyResource):
        pass

    @dg.op
    def my_op() -> None:
        pass

    @dg.job
    def my_job() -> None:
        my_op()

    my_asset_job = dg.define_asset_job("my_asset_job", [my_asset])

    @dg.sensor(job=my_job)
    def my_sensor(foo: MyResource):
        pass

    @dg.sensor(job=my_asset_job)
    def my_sensor_two(foo: MyResource, bar: MyResource):
        pass

    @dg.sensor(target=[my_asset])
    def my_sensor_three():
        pass

    @dg.schedule(job=my_job, cron_schedule="* * * * *")
    def my_schedule(foo: MyResource):
        pass

    @dg.schedule(job=my_job, cron_schedule="* * * * *")
    def my_schedule_two(foo: MyResource, baz: MyResource):
        pass

    defs = dg.Definitions(
        resources={
            "foo": MyResource(a_str="foo"),
            "bar": MyResource(a_str="bar"),
            "baz": MyResource(a_str="baz"),
        },
        sensors=[my_sensor, my_sensor_two, my_sensor_three],
        schedules=[my_schedule, my_schedule_two],
    )

    repo = defs.get_repository_def()
    repo_snap = RepositorySnap.from_def(repo)
    assert repo_snap.resources

    assert len(repo_snap.resources) == 3

    foo = next(iter(data for data in repo_snap.resources if data.name == "foo"))

    assert set(foo.schedules_using) == {
        "my_schedule",
        "my_schedule_two",
    }
    assert set(foo.sensors_using) == {"my_sensor", "my_sensor_two"}
    assert {entry.job_name for entry in foo.job_ops_using} == {"my_asset_job"}

    bar = next(iter(data for data in repo_snap.resources if data.name == "bar"))

    assert set(bar.schedules_using) == set()
    assert set(bar.sensors_using) == {"my_sensor_two"}

    baz = next(iter(data for data in repo_snap.resources if data.name == "baz"))

    assert set(baz.schedules_using) == set({"my_schedule_two"})
    assert set(baz.sensors_using) == set()
