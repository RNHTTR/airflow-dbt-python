"""Airflow operators for all dbt commands."""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional, Union

from airflow.exceptions import AirflowException
from airflow.models.baseoperator import BaseOperator
from airflow.models.xcom import XCOM_RETURN_KEY

from airflow_dbt_python.utils.enums import LogFormat

if TYPE_CHECKING:
    from dbt.contracts.results import RunResult

    from airflow_dbt_python.hooks.dbt import DbtHook, DbtTaskResult
    from airflow_dbt_python.utils.enums import Output

base_template_fields = [
    "project_dir",
    "profiles_dir",
    "profile",
    "target",
    "state",
    "vars",
    "env_vars",
]

def _find_all_job_ids(obj):
    result = []

    def _search(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == 'job_id':
                    result.append(v)
                else:
                    _search(v)
        elif isinstance(o, list):
            for item in o:
                _search(item)

    _search(obj)
    return result[0] if len(result) == 1 else result


class DbtBaseOperator(BaseOperator):
    """The basic Airflow dbt operator.

    Defines how to build an argument list and execute a dbt command. Does not set a
    command itself, subclasses should set it.

    Attributes:
        command: The dbt command to execute.
        project_dir: Directory for dbt to look for dbt_profile.yml. Defaults to
            current directory.
        profiles_dir: Directory for dbt to look for profiles.yml. Defaults to
            ~/.dbt.
        profile: Which profile to load. Overrides dbt_profile.yml.
        target: Which target to load for the given profile.
        vars: Supply variables to the project. Should be a YAML string. Overrides
            variables defined in dbt_profile.yml.
        log_cache_events: Flag to enable logging of cache events.
        s3_conn_id: An s3 Airflow connection ID to use when pulling dbt files from s3.
        do_xcom_push_artifacts: A list of dbt artifacts to XCom push.
    """

    template_fields = base_template_fields

    def __init__(
        self,
        # dbt project configuration
        project_dir: Optional[Union[str, Path]] = None,
        profiles_dir: Optional[Union[str, Path]] = None,
        profile: Optional[str] = None,
        target: Optional[str] = None,
        state: Optional[str] = None,
        # Execution configuration
        compiled_target: Optional[Union[os.PathLike, str, bytes]] = None,
        cache_selected_only: Optional[bool] = None,
        fail_fast: Optional[bool] = None,
        single_threaded: Optional[bool] = None,
        threads: Optional[int] = None,
        use_experimental_parser: Optional[bool] = None,
        vars: Optional[dict[str, str]] = None,
        warn_error: Optional[bool] = None,
        # Logging
        debug: Optional[bool] = None,
        log_path: Optional[str] = None,
        log_level: Optional[str] = None,
        log_level_file: Optional[str] = None,
        log_format: LogFormat = LogFormat.DEFAULT,
        log_cache_events: Optional[bool] = False,
        quiet: Optional[bool] = None,
        no_print: Optional[bool] = None,
        record_timing_info: Optional[str] = None,
        # Mutually exclusive
        defer: Optional[bool] = None,
        no_defer: Optional[bool] = None,
        partial_parse: Optional[bool] = False,
        no_partial_parse: Optional[bool] = None,
        introspect: Optional[bool] = None,
        no_introspect: Optional[bool] = None,
        use_colors: Optional[bool] = None,
        no_use_colors: Optional[bool] = None,
        static_parser: Optional[bool] = None,
        no_static_parser: Optional[bool] = None,
        version_check: Optional[bool] = None,
        no_version_check: Optional[bool] = None,
        write_json: Optional[bool] = None,
        write_perf_info: Optional[bool] = None,
        send_anonymous_usage_stats: Optional[bool] = None,
        no_send_anonymous_usage_stats: Optional[bool] = None,
        # Extra features configuration
        dbt_conn_id: Optional[str] = "dbt_conn_id",
        profiles_conn_id: Optional[str] = None,
        project_conn_id: Optional[str] = None,
        do_xcom_push_artifacts: Optional[list[str]] = None,
        upload_dbt_project: bool = False,
        delete_before_upload: bool = False,
        replace_on_upload: bool = False,
        env_vars: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_dir = project_dir
        self.profiles_dir = profiles_dir
        self.profile = profile
        self.target = target
        self.state = state

        self.compiled_target = compiled_target
        self.cache_selected_only = cache_selected_only
        self.fail_fast = fail_fast
        self.single_threaded = single_threaded
        self.threads = threads
        self.use_experimental_parser = use_experimental_parser
        self.vars = vars or {}
        self.warn_error = warn_error

        self.debug = debug
        self.log_path = log_path
        self.log_cache_events = log_cache_events
        self.quiet = quiet
        self.no_print = no_print
        self.log_level = log_level
        self.log_level_file = log_level_file
        self.log_format = log_format
        self.record_timing_info = record_timing_info

        self.dbt_defer = defer
        self.no_defer = no_defer

        self.static_parser = static_parser
        self.no_static_parser = no_static_parser

        self.send_anonymous_usage_stats = send_anonymous_usage_stats
        self.no_send_anonymous_usage_stats = no_send_anonymous_usage_stats

        self.partial_parse = partial_parse
        self.no_partial_parse = no_partial_parse

        self.use_colors = use_colors
        self.no_use_colors = no_use_colors

        self.introspect = introspect
        self.no_introspect = no_introspect

        self.version_check = version_check
        self.no_version_check = no_version_check

        self.write_json = write_json or (
            do_xcom_push_artifacts and "run_results.json" in do_xcom_push_artifacts
        )

        self.write_perf_info = write_perf_info

        self.dbt_conn_id = dbt_conn_id
        self.profiles_conn_id = profiles_conn_id
        self.project_conn_id = project_conn_id
        self.do_xcom_push_artifacts = do_xcom_push_artifacts
        self.upload_dbt_project = upload_dbt_project
        self.delete_before_upload = delete_before_upload
        self.replace_on_upload = replace_on_upload
        self.env_vars = env_vars

        self._dbt_hook: Optional[DbtHook] = None

    def execute(self, context):
        """Execute dbt command with prepared arguments.

        Execution requires setting up a directory with the dbt project files and
        overriding the logging.

        Args:
            context: The Airflow's task context
        """
        from airflow_dbt_python.hooks.dbt import DbtTaskResult

        self.log.info("Running dbt task: %s", self.command)

        result = DbtTaskResult(False, None, {})

        try:
            result = self.dbt_hook.run_dbt_task(
                self.command,
                artifacts=self.do_xcom_push_artifacts,
                **vars(self),
            )
        except Exception as e:
            self.log.exception(
                f"An error has occurred while executing dbt {self.command}", exc_info=e
            )
            raise AirflowException(
                f"An error has occurred while executing dbt {self.command}"
            ) from e

        finally:
            if self.do_xcom_push is True and context.get("ti", None) is not None:
                self.xcom_push_dbt_results(context, result)

        if result.success is not True:
            raise AirflowException(
                f"Dbt {self.command} task finished with an error status"
            )

        serializable_result = self.make_run_results_serializable(result.run_results)
        return serializable_result

    def get_openlineage_facets_on_complete(self, task_instance):
        """
        Retrieve OpenLineage data for a COMPLETE BigQuery job.

        This method retrieves statistics for the specified job_ids using the BigQueryDatasetsProvider.
        It calls BigQuery API, retrieving input and output dataset info from it, as well as run-level
        usage statistics.

        Run facets should contain:
            - ExternalQueryRunFacet
            - BigQueryJobRunFacet

        Job facets should contain:
            - SqlJobFacet if operator has self.sql

        Input datasets should contain facets:
            - DataSourceDatasetFacet
            - SchemaDatasetFacet

        Output datasets should contain facets:
            - DataSourceDatasetFacet
            - SchemaDatasetFacet
            - OutputStatisticsOutputDatasetFacet
        """
        print("---- STARTING OL EXTRACTION -----")
        from airflow.providers.google.cloud.hooks.bigquery import BigQueryHook
        from airflow.providers.openlineage.extractors import OperatorLineage
        from airflow.providers.openlineage.utils.utils import normalize_sql

        from openlineage.client.facet import SqlJobFacet
        from openlineage.common.provider.bigquery import BigQueryDatasetsProvider

        hook = BigQueryHook(gcp_conn_id='google_cloud_default', use_legacy_sql=False)
        client = hook.get_client(project_id=self.hook.project_id)
        xcom_value = task_instance.xcom_pull()

        job_id = _find_all_job_ids(xcom_value)

        if not self.job_id:
            return OperatorLineage()

        if isinstance(self.job_id, str):
            job_ids = [self.job_id]

        inputs, outputs, run_facets = {}, {}, {}
        for job_id in job_ids:
            stats = BigQueryDatasetsProvider(client=client).get_facets(job_id=job_id)
            for input in stats.inputs:
                input = input.to_openlineage_dataset()
                inputs[input.name] = input
            if stats.output:
                output = stats.output.to_openlineage_dataset()
                outputs[output.name] = output
            for key, value in stats.run_facets.items():
                run_facets[key] = value

        job_facets = {}
        if hasattr(self, "sql"):
            job_facets["sql"] = SqlJobFacet(query=normalize_sql(self.sql))

        print("------ WRITING OL DATA -------")

        return OperatorLineage(
            inputs=list(inputs.values()),
            outputs=list(outputs.values()),
            run_facets=run_facets,
            job_facets=job_facets,
        )

    @property
    def command(self) -> str:
        """Return the current dbt command.

        Each subclass of DbtBaseOperator should return its corresponding command.
        """
        raise NotImplementedError()

    @property
    def dbt_hook(self) -> DbtHook:
        """Provides an existing DbtHook or creates one."""
        if self._dbt_hook is None:
            from airflow_dbt_python.hooks.dbt import DbtHook

            self._dbt_hook = DbtHook(
                dbt_conn_id=self.dbt_conn_id,
                project_conn_id=self.project_conn_id,
                profiles_conn_id=self.profiles_conn_id,
            )
        return self._dbt_hook

    def xcom_push_dbt_results(self, context, dbt_results: DbtTaskResult) -> None:
        """Push any dbt results to XCom.

        Args:
            context: The Airflow task's context.
            dbt_results: A namedtuple the results of executing a dbt task, as returned
                by DbtHook.
        """
        serializable_result = self.make_run_results_serializable(
            dbt_results.run_results
        )
        self.xcom_push(context, key=XCOM_RETURN_KEY, value=serializable_result)

        for key, artifact in dbt_results.artifacts.items():
            self.xcom_push(context, key=key, value=artifact)

    def make_run_results_serializable(
        self, result: Optional[RunResult]
    ) -> Optional[dict[Any, Any]]:
        """Makes dbt's run result JSON-serializable.

        Turn dbt's RunResult into a dict of only JSON-serializable types
        Each subclas may implement this method to return a dictionary of
        JSON-serializable types, the default XCom backend. If implementing
        custom XCom backends, this method may be overriden.
        """
        if result is None:
            return result
        if is_dataclass(result) is False:
            return result  # type: ignore
        return asdict(result, dict_factory=run_result_factory)


selection_template_fields = ["select", "exclude"]


class DbtRunOperator(DbtBaseOperator):
    """Executes a dbt run command.

    The run command executes SQL model files against the given target. The
    documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/run.
    """

    template_fields = base_template_fields + selection_template_fields

    def __init__(
        self,
        full_refresh: Optional[bool] = None,
        models: Optional[list[str]] = None,
        select: Optional[list[str]] = None,
        selector_name: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.full_refresh = full_refresh
        self.exclude = exclude
        self.selector_name = selector_name
        self.select = select or models

    @property
    def command(self) -> str:
        """Return the run command."""
        return "run"


class DbtSeedOperator(DbtBaseOperator):
    """Executes a dbt seed command.

    The seed command loads csv files into the  the given target. The
    documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/seed.
    """

    template_fields = base_template_fields + selection_template_fields

    def __init__(
        self,
        full_refresh: Optional[bool] = None,
        select: Optional[list[str]] = None,
        show: Optional[bool] = None,
        exclude: Optional[list[str]] = None,
        selector_name: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.full_refresh = full_refresh
        self.select = select
        self.show = show
        self.exclude = exclude
        self.selector_name = selector_name

    @property
    def command(self) -> str:
        """Return the seed command."""
        return "seed"


class DbtTestOperator(DbtBaseOperator):
    """Executes a dbt test command.

    The test command runs data and/or schema tests. The
    documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/test.
    """

    template_fields = base_template_fields + selection_template_fields

    def __init__(
        self,
        singular: Optional[bool] = None,
        generic: Optional[bool] = None,
        models: Optional[list[str]] = None,
        select: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        selector_name: Optional[str] = None,
        indirect_selection: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.singular = singular
        self.generic = generic
        self.exclude = exclude
        self.selector_name = selector_name
        self.select = select or models
        self.indirect_selection = indirect_selection

    @property
    def command(self) -> str:
        """Return the test command."""
        return "test"


class DbtCompileOperator(DbtBaseOperator):
    """Executes a dbt compile command.

    The compile command generates SQL files. The
    documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/compile.
    """

    template_fields = base_template_fields + selection_template_fields

    def __init__(
        self,
        parse_only: Optional[bool] = None,
        full_refresh: Optional[bool] = None,
        models: Optional[list[str]] = None,
        select: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        selector_name: Optional[str] = None,
        upload_dbt_project: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.parse_only = parse_only
        self.full_refresh = full_refresh
        self.exclude = exclude
        self.selector_name = selector_name
        self.select = select or models
        self.upload_dbt_project = upload_dbt_project

    @property
    def command(self) -> str:
        """Return the compile command."""
        return "compile"


class DbtDepsOperator(DbtBaseOperator):
    """Executes a dbt deps command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/deps.
    """

    def __init__(self, upload_dbt_project: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.upload_dbt_project = upload_dbt_project

    @property
    def command(self) -> str:
        """Return the deps command."""
        return "deps"


class DbtDocsGenerateOperator(DbtBaseOperator):
    """Executes a dbt docs generate command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/cmd-docs.
    """

    def __init__(self, compile=True, upload_dbt_project: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.compile = compile
        self.upload_dbt_project = upload_dbt_project

    @property
    def command(self) -> str:
        """Return the generate command."""
        return "generate"


class DbtCleanOperator(DbtBaseOperator):
    """Executes a dbt clean command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/debug.
    """

    def __init__(
        self,
        upload_dbt_project: bool = True,
        delete_before_upload: bool = True,
        clean_project_files_only: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.upload_dbt_project = upload_dbt_project
        self.delete_before_upload = delete_before_upload
        self.clean_project_files_only = clean_project_files_only

    @property
    def command(self) -> str:
        """Return the clean command."""
        return "clean"


class DbtDebugOperator(DbtBaseOperator):
    """Executes a dbt debug command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/debug.
    """

    def __init__(
        self,
        config_dir: Optional[bool] = None,
        no_version_check: Optional[bool] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.config_dir = config_dir
        self.no_version_check = no_version_check

    @property
    def command(self) -> str:
        """Return the debug command."""
        return "debug"


class DbtSnapshotOperator(DbtBaseOperator):
    """Executes a dbt snapshot command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/snapshot.
    """

    template_fields = base_template_fields + selection_template_fields

    def __init__(
        self,
        select: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        selector_name: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.select = select
        self.exclude = exclude
        self.selector_name = selector_name

    @property
    def command(self) -> str:
        """Return the snapshot command."""
        return "snapshot"


class DbtLsOperator(DbtBaseOperator):
    """Executes a dbt list (or ls) command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/list.
    """

    template_fields = (
        base_template_fields + selection_template_fields + ["resource_types"]
    )

    def __init__(
        self,
        resource_types: Optional[list[str]] = None,
        select: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        selector_name: Optional[str] = None,
        dbt_output: Optional[Output] = None,
        output_keys: Optional[list[str]] = None,
        indirect_selection: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.resource_types = resource_types
        self.select = select
        self.exclude = exclude
        self.selector_name = selector_name
        self.dbt_output = dbt_output
        self.output_keys = output_keys
        self.indirect_selection = indirect_selection

    @property
    def command(self) -> str:
        """Return the list command."""
        return "list"


# Convinience alias
DbtListOperator = DbtLsOperator


class DbtRunOperationOperator(DbtBaseOperator):
    """Executes a dbt run-operation command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/run-operation.
    """

    template_fields = base_template_fields + ["macro", "args"]

    def __init__(
        self,
        macro: str,
        args: Optional[dict[str, str]] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.macro = macro
        self.args = args

    @property
    def command(self) -> str:
        """Return the run-operation command."""
        return "run-operation"


class DbtParseOperator(DbtBaseOperator):
    """Executes a dbt parse command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/parse.
    """

    def __init__(
        self,
        upload_dbt_project: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.upload_dbt_project = upload_dbt_project

    @property
    def command(self) -> str:
        """Return the parse command."""
        return "parse"


class DbtSourceFreshnessOperator(DbtBaseOperator):
    """Executes a dbt source-freshness command.

    The documentation for the dbt command can be found here:
    https://docs.getdbt.com/reference/commands/source.
    """

    def __init__(
        self,
        select: Optional[list[str]] = None,
        dbt_output: Optional[Union[str, Path]] = None,
        exclude: Optional[list[str]] = None,
        selector_name: Optional[str] = None,
        upload_dbt_project: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.select = select
        self.exclude = exclude
        self.selector_name = selector_name
        self.dbt_output = dbt_output
        self.upload_dbt_project = upload_dbt_project

    @property
    def command(self) -> str:
        """Return the source command."""
        return "source"


class DbtBuildOperator(DbtBaseOperator):
    """Executes a dbt build command.

    The build command combines the run, test, seed, and snapshot commands into one. The
    full Documentation for the dbt build command can be found here:
    https://docs.getdbt.com/reference/commands/build.
    """

    template_fields = base_template_fields + selection_template_fields

    def __init__(
        self,
        full_refresh: Optional[bool] = None,
        select: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        selector_name: Optional[str] = None,
        singular: Optional[bool] = None,
        generic: Optional[bool] = None,
        show: Optional[bool] = None,
        indirect_selection: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.full_refresh = full_refresh
        self.select = select
        self.exclude = exclude
        self.selector_name = selector_name
        self.singular = singular
        self.generic = generic
        self.show = show
        self.indirect_selection = indirect_selection

    @property
    def command(self) -> str:
        """Return the build command."""
        return "build"


def run_result_factory(data: list[tuple[Any, Any]]):
    """Dictionary factory for dbt's run_result.

    We need to handle dt.datetime and agate.table.Table.
    The rest of the types should already be JSON-serializable.
    """
    from agate.table import Table

    d = {}
    for key, val in data:
        if isinstance(val, dt.datetime):
            val = val.isoformat()
        elif isinstance(val, Table):
            # agate Tables have a few print methods but they offer plain
            # text representations of the table which are not very JSON
            # friendly. There is a to_json method, but I don't think
            # sending the whole table in an XCOM is a good idea either.
            val = {
                k: v.__class__.__name__
                for k, v in zip(val._column_names, val._column_types)
            }
        d[key] = val
    return d
